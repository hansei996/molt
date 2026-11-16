"""Molt 控制面插件 —— 让 AstrBot 变成一个"可以交出自己"的壳。

## 这个插件为什么存在

Molt 的迁徙协议要求：**源壳必须先停止对外说话，目标壳才能开口。**
在找到这个接缝之前，唯一的办法是杀掉整个 AstrBot 进程（`docker kill -s INT`）。
那有两个问题：

1. **太粗**。为了断开一个 QQ 连接，把整个进程连同调度器、定时任务、正在跑的
   对话一起杀掉。一次迁徙的代价是"它被打断了一次"。
2. **不可逆**。杀掉之后想恢复，只能重启进程 —— 而重启意味着重新加载全部插件、
   重新拉 4173 个 LLM 元数据、重新建数据库连接。

AstrBot 自己在 dashboard 里热重载平台时用的就是 `platform_manager.terminate_platform()`
（见 `astrbot/dashboard/services/config_service.py:1464` 与 `:1432`），
而 `context.platform_manager` 是 `astrbot/core/star/context.py:166` 里的**公开属性**。
所以插件完全可以在不碰核心代码的前提下，做到"只让这一个适配器闭嘴"。

## 它只做四件事

| 端点 | 做什么 | 凭什么安全 |
|---|---|---|
| `GET  /status`   | 报告自己是谁、平台连接状态、WAL 有多大 | 只读 |
| `POST /drain`    | 断开所有平台适配器 | 幂等；重复调用不报错 |
| `POST /resume`   | 重新连上配置里 enable 的平台 | **需要 drain 过一次才允许** |
| `POST /checkpoint` | 把 WAL 合并进主库 | 只对 `.db-wal` 非空的库动手 |

## 它刻意不做的事

- **不认识 Molt 的账本**。它不知道谁是当前 epoch、该不该上线。那个判断属于
  NAS 上的 Warden —— 一个跑在被仲裁对象内部的东西如果也参与仲裁，
  就等于让被告兼任法官（ARCH §4.2）。
- **不自己决定 resume**。`resume` 必须显式调用，而且要求先 `drain` 过。
  这条不是洁癖：一个"自动恢复连接"的插件，会在源壳刚 drain、目标壳还没起来
  的窗口里把它自己重新连上，制造出两条鱼同时在线的状态。
- **不写任何文件**。它只改内存里的适配器状态，所以它不会成为新的真相来源。

## 关于鉴权

AstrBot 的 `/api/plug/*` 走 dashboard 的 JWT 中间件
（`astrbot/dashboard/server.py:295-388`：`/api` 下除白名单外一律要 token）。
本插件**不自己造一套鉴权**——多一套就多一个会被配错的地方。
Warden 侧用 `soul/cmd_config.json` 里的 `dashboard.jwt_secret` 签一个短时 JWT
（HS256，标准库 `hmac` + `hashlib` + `base64` 就够，见 `molt/control.py`）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.star import Context, Star

logger = logging.getLogger("Molt.Control")

PLUGIN_NAME = "astrbot_plugin_molt"
PLUGIN_VERSION = "0.1.0"

# 控制面在 dashboard 上的前缀。AstrBot 会把它拼成 `/api/plug/<PLUGIN_NAME><route>`。
API_PREFIX = f"/{PLUGIN_NAME}"

# 部署方可以在这个文件里写一行"这台壳叫什么"，放在插件自己目录旁边。
# 两个理由：
#   ①它在 `data/plugins/` 里，而 `soul.py` 的 `COPY_ARTIFACTS` **不含** `plugins/**`
#     （只搬 `plugin_data/**`），所以它天然是"机器绑定"的，
#     不会跟着灵魂漂到另一台机器上——那正是我们要的语义（壳名属于壳，不属于魂）。
#   ②`host_label()` 每次请求都重读它，所以**改完立刻生效、不用重启 AstrBot**。
#     这对一个已经跑着的生产容器尤其重要：为了改一个显示名字去重建容器，
#     是有风险且不值得的。
HOST_LABEL_FILENAME = "host_label.txt"

# 落地观察（把"我在第 N 代搬到了这里"记成 `bot_observed_activity`）用到的常量与状态文件。
# 状态文件放在插件自己目录里 —— 与 `host_label.txt` 同一条理由：`COPY_ARTIFACTS`
# 不含 `plugins/**`，所以"我记过哪一代"是这台壳自己的事，不该跟着灵魂漂走。
from .observed import (  # noqa: E402  (放在常量后面，读起来更顺)
    DEFAULT_POLL_SECONDS,
    FIRST_DELAY_SECONDS,
    ObservationState,
    compose_observation,
    pick_generation,
)
from .notify import DEFAULT_PUSH_WAIT, POLL_SECONDS
from .arrival import (  # noqa: E402
    ArrivalState,
    compose_arrival_note,
    compose_power_question,
    pick_arrival,
    should_ask_about_power,
)

OBSERVED_STATE_FILENAME = "observed_state.json"

# 落地播报的状态（"这次落地我说过没有"）。同样放在插件自己目录里，
# 与 `host_label.txt` 一条理由：属于壳，不属于魂。
ARRIVAL_STATE_FILENAME = "arrival_state.json"

#: 账本问不到时最多试几次。每次 ledger_status() 自带 5 秒超时，
#: 所以最坏情况是某一条回复多等 15 秒 —— 再多就该直接放弃说话了。
ARRIVAL_MAX_ATTEMPTS = 3


def _prettify(shell: str) -> str:
    """把壳名说成人话（desktop-h2kgiao -> DESKTOP-H2KGIAO）。"""
    try:
        from .arrival import prettify_shell

        return prettify_shell(shell)
    except Exception:  # pragma: no cover - 防御性
        return str(shell or "").replace("-", " ").upper()


class MoltControlPlugin(Star):
    """把"AstrBot 现在是否在对外说话"变成一个可以查询和改变的状态。"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}

        #: drain 过的平台 id。resume 只恢复这里面的那些 —— 原本就是 disable 的
        #: 平台不该因为一次迁徙被打开。
        self._drained: list[str] = []
        #: 这个进程启动以来是否被 drain 过。resume 的前置条件。
        self._has_drained = False
        self._api = None
        #: 聊天指令层。逻辑全在 `command.py`，这里只是个把手。
        self._mover = None
        try:
            from .command import MoltMover

            self._mover = MoltMover(Path(__file__).resolve().parent)
        except Exception as exc:  # pragma: no cover - 防御性
            logger.warning("[Molt] 聊天指令层没起来：%s", exc, exc_info=True)
        #: 落地之后"我自己看一眼我在哪"的那条后台任务（见 `observed.py`）。
        self._obs_task: asyncio.Task | None = None
        self._push_task: asyncio.Task | None = None
        self._obs_state = None
        #: 落地播报（"我刚从 X 搬过来"）说过没有。
        self._arrival_state = None
        #: 这个进程问过账本没有了。**每次落地都是新进程**（land 要求先把容器停掉），
        #: 所以"一次进程问一次"既够用，又不会让每一条回复都去走一趟 HTTP。
        self._arrival_checked = False
        #: 账本暂时问不到时重试几次就放弃 —— 网络抖动不该让人等第四次。
        self._arrival_attempts = 0
        self._arrival_said = False
        # (代, 那句话)：组装好了但还没确认送达。发失败时留着，下一句回复再试。
        self._arrival_pending = None
        self._register_api_if_available()

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        logger.info(
            "[Molt] 控制面已就绪（v%s）。它不会自己连接或断开任何平台，"
            "只等 Warden 告诉它该做什么。",
            PLUGIN_VERSION,
        )
        # 落地观察不在 initialize() 里起：AstrBot 调用 initialize() 时所在的那个
        # 事件循环不等于之后真正在跑的服务循环（2026-11-15 实测：create_task 建出来的
        # 任务永远不被调度，连 loop.call_later 都不触发）。改成等 `on_astrbot_loaded()`
        # —— 那个钩子由 core_lifecycle 在主循环上发出来。

    def _start_push_task(self) -> None:
        """搬完之后主动发一条消息给你 —— 不用你先开口。

        为什么要等 `on_astrbot_loaded`：平台适配器在这之后才连上；发早了
        只会拿到「没有可用的平台实例」。
        """
        if self._push_task is not None and not self._push_task.done():
            return
        try:
            self._push_task = asyncio.create_task(self._arrival_push_loop())
            logger.info("[Molt] 落地主动通知已启动（最多等 %.0f 秒让平台连上）", DEFAULT_PUSH_WAIT)
        except Exception as exc:  # pragma: no cover - 防御性
            logger.warning("[Molt] 落地主动通知没起来：%s", exc, exc_info=True)

    async def _arrival_push_loop(self) -> None:
        from pathlib import Path

        from . import notify

        cfg = self._mover.settings()
        data_dir = Path(str(cfg.data_dir))
        state = notify.NotifyState(data_dir / notify.STATE_FILENAME)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DEFAULT_PUSH_WAIT
        while True:
            try:
                status = await asyncio.to_thread(self._mover.ledger_status)
                latest = (status or {}).get("latest_generation") or {}
                number = int(latest.get("number") or 0)
                mine = str(latest.get("consumed_by") or "") == str(cfg.shell)
                if mine and notify.should_push(state, number):
                    text = notify.compose_arrival_push(
                        label=_prettify(cfg.shell), generation=number
                    )
                    got = await notify.push_arrival(
                        self.context, data_dir=data_dir, text=text
                    )
                    if got.get("state") == "delivered":
                        state.mark(number, "delivered")
                        logger.info(
                            "[Molt] 已主动告诉你：我搬过来了（第 %s 份快照，通过 %s）",
                            number,
                            got.get("platform_id"),
                        )
                        return
                    logger.info(
                        "[Molt] 主动通知还没发出去（%s：%s），继续等平台",
                        got.get("state"),
                        got.get("reason"),
                    )
                    if got.get("state") == "no-route":
                        return
            except Exception as exc:  # noqa: BLE001 - 通知失败不该影响任何别的
                logger.warning("[Molt] 主动通知这一轮失败：%s", exc)
            if loop.time() >= deadline:
                logger.info("[Molt] 主动通知等超时了 —— 不发就是不发，不假装发过")
                return
            await asyncio.sleep(POLL_SECONDS)

    def _start_observe_task(self) -> None:
        logger.info("[Molt] _start_observe_task 被调用了（loop=%s）", id(asyncio.get_running_loop()))
        if self._obs_task is not None and not self._obs_task.done():
            logger.info("[Molt] 已经有任务在跑了，不重复起")
            return
        try:
            self._obs_task = asyncio.create_task(self._observe_loop())
            logger.info("[Molt] 落地观察已启动：每 %.0f 秒看一眼账本，"
                        "确认这一代是不是我落的地。", DEFAULT_POLL_SECONDS)
        except Exception as exc:  # pragma: no cover - 防御性
            logger.warning("[Molt] 落地观察没起来：%s", exc, exc_info=True)

    async def terminate(self) -> None:
        task, self._obs_task = self._obs_task, None
        if task is not None and not task.done():
            task.cancel()
        logger.info("[Molt] 控制面退出。平台适配器的状态没有持久化，进程重启即复位。")

    # ------------------------------------------------------------ 落地观察（记忆）

    def _observed_state_path(self):
        return Path(__file__).resolve().parent / OBSERVED_STATE_FILENAME

    def _observed_state(self):
        if self._obs_state is None:
            self._obs_state = ObservationState.load(self._observed_state_path())
        return self._obs_state

    async def observe_migration_once(self) -> dict[str, Any]:
        """看一次账本：这一代是不是我落的？是，而且没记过，就记成一次观察。

        返回 `{"action": "skip"|"recorded"|"failed", ...}`，**永不抛** ——
        它跑在后台任务里，抛出去只会变成一条没人看的 traceback。
        """
        if self._mover is None:
            return {"action": "skip", "reason": "聊天指令层没起来，读不到账本"}

        try:
            settings = self._mover.settings()
            status = self._mover.ledger_status()
        except Exception as exc:  # pragma: no cover
            return {"action": "skip", "reason": f"读账本失败：{exc}"}
        if not status:
            return {"action": "skip", "reason": "账本问不到（不猜）"}

        shell = (settings.shell or self.host_label() or "").strip()
        gen = pick_generation(status, shell=shell)
        if gen is None:
            return {"action": "skip", "reason": "最新一代不是我落的"}

        number = int(gen.get("generation") or 0)
        state = self._observed_state()
        if state.observed_generation >= number:
            return {"action": "skip", "reason": f"第 {number} 代已经记过了"}

        text = compose_observation(
            generation=number,
            shell=self.host_label() or shell,
            pushed_by=str(gen.get("pushed_by") or ""),
            consumed_at=str(gen.get("consumed_at") or ""),
            port=0,
        )
        try:
            from .memory_bridge import record_observed_activity

            result = await record_observed_activity(
                self.context,
                text,
                occurred_at=str(gen.get("consumed_at") or ""),
                shell=shell,
                generation=number,
                idempotency_key=f"molt-migration-gen-{number}",
            )
        except Exception as exc:  # pragma: no cover
            result = {"ok": False, "via": "none", "reason": f"{type(exc).__name__}: {exc}"}

        if result.get("ok"):
            state.mark(number, detail=str(result.get("via", "")))
            logger.info("[Molt] 第 %d 代已经记成「我自己观察到的」（走 %s）。",
                        number, result.get("via"))
            return {"action": "recorded", "generation": number, "via": result.get("via")}

        # 失败不落盘 —— 下一轮再试，而不是把"已经记过了"记成事实
        logger.info("[Molt] 第 %d 代没记进记忆：%s（下一轮再试）",
                    number, result.get("reason"))
        return {"action": "failed", "generation": number, "reason": result.get("reason")}

    async def _observe_loop(self) -> None:
        logger.info("[Molt] 落地观察循环真的跑起来了（第一轮在 %.0f 秒后）", FIRST_DELAY_SECONDS)
        await asyncio.sleep(FIRST_DELAY_SECONDS)
        while True:
            try:
                logger.info("[Molt] 落地观察：开始这一轮")
                out = await self.observe_migration_once()
                logger.info("[Molt] 落地观察：这一轮结束 -> %s", out.get("action"))
                # 跳过的原因只在变化时说一次。否则每 30 秒一条同样的日志，
                # 人会很快学会忽略它 —— 那和没有日志是一样的。
                if out.get("action") == "skip":
                    reason = str(out.get("reason") or "")
                    if reason != getattr(self, "_obs_last_reason", None):
                        self._obs_last_reason = reason
                        logger.info("[Molt] 落地观察：%s", reason or "无话可说")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover
                logger.warning("[Molt] 落地观察出错：%s", exc, exc_info=True)
            await asyncio.sleep(DEFAULT_POLL_SECONDS)

    # -------------------------------------------------------- 落地播报（第一句话）

    def _arrival_state_path(self):
        return Path(__file__).resolve().parent / ARRIVAL_STATE_FILENAME

    def _arrival_state_obj(self):
        if self._arrival_state is None:
            self._arrival_state = ArrivalState.load(self._arrival_state_path())
        return self._arrival_state

    def arrival_note(self) -> str:
        """这次落地还没说过「我从哪来」，就返回那一行；否则返回空串。

        **永不抛** —— 它挂在每一条回复的发送路径上，抛一次就等于这个人说不出话。
        读不到账本、拿不到壳名、状态文件坏了：一律安静地返回空串。
        """
        if self._mover is None or self._arrival_checked:
            return ""
        state = self._arrival_state_obj()
        try:
            settings = self._mover.settings()
            status = self._mover.ledger_status()
        except Exception as exc:  # pragma: no cover - 防御性
            logger.info("[Molt] 落地播报：读不到账本，这次不说了（%s）", exc)
            return ""
        if not status:
            # 账本问不到：不猜、不说，但留几次重试的机会 —— 刚落地那几秒
            # NAS 可能正忙。三次之后就不问了，免得每句话都多等 5 秒。
            self._arrival_attempts += 1
            if self._arrival_attempts >= ARRIVAL_MAX_ATTEMPTS:
                self._arrival_checked = True
                logger.info("[Molt] 落地播报：账本问了 %d 次都问不到，这次不说了",
                            self._arrival_attempts)
            return ""
        self._arrival_checked = True
        shell = (settings.shell or self.host_label() or "").strip()
        arrival = pick_arrival(
            status,
            shell=shell,
            now=time.time(),
            announced_generation=state.announced_generation,
        )
        if arrival is None:
            return ""

        number = int(arrival["generation"])
        # 太久的那一代**当场标记**：本来就不打算说了，没有"等送达"这件事。
        # 而新鲜的这一代要等 `send()` 返回之后才 mark（见 announce_arrival）。
        stale = bool(arrival.get("stale"))
        if stale:
            state.mark(number, detail="stale")
            age = arrival.get("age_seconds")
            logger.info(
                "[Molt] 第 %d 代已经是 %.0f 分钟前的事了，不再自称「刚搬过来」",
                number,
                (float(age) if age is not None else 0.0) / 60.0,
            )
            return ""

        note = compose_arrival_note(
            generation=number,
            from_label=str(arrival.get("from_label") or ""),
            from_shell=str(arrival.get("from_shell") or ""),
        ).strip()
        # 旧壳还开着就问一句 —— 落地那一刻是唯一自然的提问时机。
        # 已经答过（账本里有票）、旧壳已关（没有心跳）、或读不到心跳，都不问。
        if should_ask_about_power(
            status,
            shell=shell,
            from_shell=str(arrival.get("from_shell") or ""),
        ):
            note = note + compose_power_question(
                old_label=str(arrival.get("from_label") or ""),
                old_shell=str(arrival.get("from_shell") or ""),
            ).strip()
        # **先不 mark。** 组装成功不等于送达 —— 这句话要通过平台发出去，
        # 而平台会不会收下，只有 send() 返回之后才知道。
        # 2026-11-15 的教训：第一版在这里就 mark 了，发送失败也被记成"说过"，
        # 于是这一代永远不再播；而失败恰恰是最需要重试的那一次。
        self._arrival_pending = (number, note)
        logger.info("[Molt] 第 %d 代落地播报待发：%s", number, note)
        return note

    # ------------------------------------------------------------------ 平台操作

    def _platform_manager(self):
        return getattr(self.context, "platform_manager", None)

    def _platforms(self) -> list[Any]:
        pm = self._platform_manager()
        if pm is None:
            return []
        try:
            return list(pm.get_insts())
        except Exception:  # pragma: no cover - 防御性
            return []

    def _platform_configs(self) -> list[dict[str, Any]]:
        """`config.platform` 里的**全部**条目（不看 enable）。

        为什么不看 enable：这个列表的用途是回答"哪些 id 是配置驱动的适配器"。
        `enable=false` 只是"启动时没加载"，不是"这个 id 不是配置驱动的"。
        真机上踩过一次 —— 护栏把 `enable` 关掉之后，只认 enable=true 的写法
        得到空列表，于是 `drain()` 什么都没关却报告"仍有 1 个连接"。

        另注：这是**配置**不是**运行态**。drain 之后配置里仍然是 enable=true，
        变的是 `platform_manager` 里的实例。这两个概念必须分开，
        否则 drain 之后 config 被改写成 enable=false，resume 就无从知道该恢复谁。
        """
        cfg = getattr(self.context, "_config", None)
        if cfg is None:
            return []
        try:
            raw = cfg.get("platform", []) if hasattr(cfg, "get") else []
        except Exception:  # pragma: no cover
            return []
        return [item for item in (raw or []) if isinstance(item, dict)]

    def _enabled_configs(self) -> list[dict[str, Any]]:
        """`config.platform` 里 enable=true 的条目（resume 用）。"""
        return [c for c in self._platform_configs() if c.get("enable")]

    @staticmethod
    def _instance_id(inst: Any) -> str:
        """取一个运行中适配器的 id。

        `Platform` 基类没有统一的 `platform_id` 属性（`/AstrBot/astrbot/core/platform/platform.py`
        只有 `client_self_id`），所以依次尝试 meta().id → config["id"] → 类名。
        """
        try:
            meta = inst.meta()
            mid = getattr(meta, "id", None)
            if mid:
                return str(mid)
        except Exception:  # pragma: no cover - 防御性
            pass
        cfg = getattr(inst, "config", None)
        if isinstance(cfg, dict) and cfg.get("id"):
            return str(cfg["id"])
        return type(inst).__name__

    def _classified(self) -> tuple[list[tuple[str, Any]], list[tuple[str, Any]]]:
        """把运行中的适配器分成（对外连接, 仅本地闭环）。

        **主判据是 `platform_manager._inst_map`**：那个字典里装的正是"由 `load_platform`
        创建、可以被 `terminate_platform` 关掉、也可以被 `load_platform` 重新加载"的实例
        （`/AstrBot/astrbot/core/platform/manager.py:238`）。它比"id 出现在 config 里"
        更准，因为：
          - config 里有条目但加载失败 → 不在 `_inst_map` 里，不该算成对外连接
          - id 被 `_sanitize_platform_id` 改过（`:` → `_`）→ 按字符串比对 config 会失配
          - config 里有 enable=false 的条目 → 根本没加载

        降级判据是"id 出现在 `config.platform` 里"（万一某个版本改了 `_inst_map` 的名字）。
        两条都排除的，就是 `WebChatAdapter` 这类硬编码实例。

        为什么必须把 webchat 分出来而不是一起关：
          1. 它每个 AstrBot 实例都必然存在（`manager.py:116` 直接 append 进 `platform_insts`）。
             算进 `speaking` 就等于 `speaking` 恒真，那条判据再也无法区分任何事 ——
             一个永远为真的判据不能当判据用。
          2. 它**无法被 resume 重建**：构造发生在 `initialize()` 里，不走 config，
             没有 `load_platform` 的路径。drain 掉它等于永久弄坏这个进程的 WebUI 聊天。
          3. 它不是"对外说话"：只能从本机 dashboard 的画面进入，
             不存在"外面一条消息被两个壳同时收到"这种形态。
        但它是**可见的**：`platform_snapshot()` 会把它列在 `local_only` 里，
        不隐藏、也不假装它不存在。
        """
        pm = self._platform_manager()
        managed: set[int] = set()
        if pm is not None:
            inst_map = getattr(pm, "_inst_map", None)
            if isinstance(inst_map, dict):
                for entry in inst_map.values():
                    if isinstance(entry, dict) and entry.get("inst") is not None:
                        managed.add(id(entry["inst"]))
        configured = {str(c.get("id")) for c in self._platform_configs() if c.get("id")}

        outward: list[tuple[str, Any]] = []
        local_only: list[tuple[str, Any]] = []
        for inst in self._platforms():
            pid = self._instance_id(inst)
            is_managed = id(inst) in managed if managed else pid in configured
            (outward if is_managed else local_only).append((pid, inst))
        return outward, local_only

    def platform_snapshot(self) -> dict[str, Any]:
        """平台层的只读快照。给 `/status` 和 Warden 的判决用。"""
        outward, local_only = self._classified()
        return {
            "connected": len(outward),
            "connected_ids": sorted(pid for pid, _ in outward),
            "local_only": sorted(pid for pid, _ in local_only),
            "enabled_in_config": [
                {"id": c.get("id"), "type": c.get("type")} for c in self._enabled_configs()
            ],
            "drained": list(self._drained),
            "has_drained": self._has_drained,
        }

    async def drain(self) -> dict[str, Any]:
        """断开所有**对外**的平台适配器。幂等。

        返回 `drained` 是**这次**真的关掉的 id 列表 —— 已经关掉的不重复出现在里面，
        这样调用方可以区分"我关掉了它"和"它本来就是关的"。

        `ok` 的判据是"还剩下几个对外连接"，不是"还剩几个连接"：
        仅本地闭环的适配器（webchat）不算，理由见 `_classified()`。
        """
        pm = self._platform_manager()
        if pm is None:
            return {"ok": False, "reason": "这个 AstrBot 版本没有 platform_manager", "drained": []}

        outward, _local = self._classified()
        done: list[str] = []
        failed: list[str] = []
        for pid, _inst in outward:
            try:
                await pm.terminate_platform(pid)
                done.append(pid)
            except Exception as exc:
                logger.warning("[Molt] drain 平台 %s 失败：%s", pid, exc)
                failed.append(pid)

        self._has_drained = True
        for pid in done:
            if pid not in self._drained:
                self._drained.append(pid)

        snap = self.platform_snapshot()
        still = snap["connected"]
        if still:
            logger.warning(
                "[Molt] drain 之后仍有 %d 个对外连接：%s", still, snap["connected_ids"]
            )
        else:
            logger.info("[Molt] drain 完成：关掉 %s，对外连接为 0", done or "（无）")
        return {
            "ok": still == 0,
            "drained": done,
            "still_connected": still,
            "still_connected_ids": snap["connected_ids"],
            "failed": failed,
            "local_only": snap["local_only"],
        }

    async def resume(self) -> dict[str, Any]:
        """重新连上 drain 过的平台。**必须 drain 过**。"""
        if not self._has_drained:
            return {
                "ok": False,
                "reason": "这个壳从启动以来没有 drain 过，resume 无意义（平台本来就在连）",
                "resumed": [],
            }
        pm = self._platform_manager()
        if pm is None:
            return {"ok": False, "reason": "没有 platform_manager", "resumed": []}

        # 只恢复**配置里 enable=true** 的。drain 前它的 enable 可能本来就是 false
        # （比如目标壳落位后被护栏关掉了），那种不该被这条命令悄悄打开 ——
        # "让这只鱼重新对外说话"必须是人的决定，不是 resume 的副作用。
        want = [c for c in self._enabled_configs() if c.get("id") in self._drained]
        skipped = [pid for pid in self._drained if pid not in {str(c.get("id")) for c in want}]

        done: list[str] = []
        for cfg in want:
            try:
                await pm.load_platform(cfg)
                done.append(str(cfg.get("id")))
            except Exception as exc:
                logger.warning("[Molt] resume 平台 %s 失败：%s", cfg.get("id"), exc)

        for pid in done:
            if pid in self._drained:
                self._drained.remove(pid)

        snap = self.platform_snapshot()
        logger.info(
            "[Molt] resume 完成：恢复 %s，对外连接 %d 个",
            done or "（无）",
            snap["connected"],
        )
        return {
            "ok": bool(done) or not want,
            "resumed": done,
            "skipped_disabled": skipped,
            "still_drained": list(self._drained),
            "connected": snap["connected"],
            "connected_ids": snap["connected_ids"],
        }

    # ------------------------------------------------------------------ 落位与检查

    def host_label(self) -> str:
        """这台壳的名字。

        优先级：`MOLT_HOST_LABEL` 环境变量 → 插件目录下的 `host_label.txt`
        → 主机名 → `"unknown"`。

        为什么需要前两层：在 Docker 里 `socket.gethostname()` 返回的是
        **容器 ID**（实测生产容器报 `container-id`），而人想知道的是
        「灵魂现在在哪台机器上」。一串十六进制对要做关机决定的人毫无帮助——
        它看起来像"某个内部标识"，而不是"NAS 还是 Windows"。

        实测后果（2026-11-14 22:55）：人在 QQ 上问它"你现在已经转移到 windows 了"，
        它回答「我摸了摸自己，脚下还是 Linux，主机名 `container-id`，也没有 C 盘喵。」
        —— **判断是对的**（那会儿它确实还在 NAS 上），但那串 ID 让这句话没法读。

        两层的分工：环境变量适合 `docker run -e MOLT_HOST_LABEL=...` 的干净部署；
        `host_label.txt` 适合**已经跑着的容器**——改一个文件就生效，
        不必为了一个显示名字去重建容器（重建一个有状态的容器是有风险的动作）。
        都没有就退回主机名（裸机上是可读的），再没有就 `unknown`
        ——**不猜**（猜成"这台"在跨机场景下是最坏的一种错）。
        """
        import os
        import socket
        from pathlib import Path

        env = os.environ.get("MOLT_HOST_LABEL", "").strip()
        if env:
            return env
        try:
            side = Path(__file__).resolve().parent / HOST_LABEL_FILENAME
            if side.is_file():
                raw = side.read_text(encoding="utf-8-sig").strip()
                # 只取第一行：Windows 上随手用记事本存的文件会带 BOM 和换行，
                # 而 BOM 由 `utf-8-sig` 吃掉。空文件 / 只有空白 → 当作没配。
                first = raw.splitlines()[0].strip() if raw else ""
                if first:
                    return first
        except Exception:  # pragma: no cover - 读不到就当没配
            pass
        try:
            name = socket.gethostname().strip()
        except Exception:  # pragma: no cover
            return "unknown"
        return name or "unknown"

    def _data_path(self):
        from pathlib import Path

        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path())
        except Exception:  # pragma: no cover - 兼容更老的版本
            return None

    def wal_snapshot(self) -> dict[str, Any]:
        """两个主库的 WAL 有多大。

        **为什么这件事必须由 AstrBot 自己回答**：外部进程也能 stat 这些文件，
        但它不知道"哪个库算灵魂的一部分"。而这个清单会随配置变
        （有人在 `livingmemory_migration.livingmemory_db_path` 里指了外部库），
        写在插件里就能跟着配置走。见 ARCH §7.1。
        """
        from pathlib import Path

        out: dict[str, Any] = {"databases": [], "total_wal_bytes": 0}
        data = self._data_path()
        if data is None:
            out["reason"] = "拿不到 AstrBot 的 data 目录"
            return out

        candidates = [
            data / "data_v4.db",
            data / "plugin_data" / "astrbot_plugin_memory_companion" / "memory_companion.db",
            data / "knowledge_base" / "kb.db",
        ]
        for db in candidates:
            if not db.exists():
                continue
            wal = Path(str(db) + "-wal")
            try:
                wal_bytes = wal.stat().st_size if wal.exists() else 0
            except OSError:
                wal_bytes = 0
            try:
                main_bytes = db.stat().st_size
            except OSError:
                main_bytes = 0
            out["databases"].append(
                {
                    "path": str(db),
                    "name": db.name,
                    "main_bytes": main_bytes,
                    "wal_bytes": wal_bytes,
                }
            )
            out["total_wal_bytes"] += wal_bytes
        return out

    async def checkpoint(self) -> dict[str, Any]:
        """把 WAL 合并进主库并截断。

        用**独立连接**做，不复用 AstrBot 的连接池：`wal_checkpoint` 是库级操作，
        独立连接一样能正确完成，而且不会因为插件拿到的是半初始化的连接而出怪事。

        跑在线程里 —— 这一步会写盘，在事件循环里同步等它会把整个 AstrBot 卡住。
        """
        import asyncio
        import sqlite3
        from pathlib import Path

        snap = self.wal_snapshot()
        if not snap.get("databases"):
            return {"ok": False, "reason": snap.get("reason") or "没有找到任何库", "checkpointed": []}

        def _run() -> list[dict[str, Any]]:
            done: list[dict[str, Any]] = []
            for item in snap["databases"]:
                p = Path(item["path"])
                before = item["wal_bytes"]
                try:
                    conn = sqlite3.connect(str(p), timeout=5.0)
                    try:
                        # TRUNCATE: 合并 + 把 -wal 截成 0。用 PASSIVE 的话文件还在，
                        # 剩下的大小会让人以为"没合干净"。
                        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    finally:
                        conn.close()
                    wal = Path(str(p) + "-wal")
                    after = wal.stat().st_size if wal.exists() else 0
                    done.append(
                        {
                            "name": item["name"],
                            "before_wal_bytes": before,
                            "after_wal_bytes": after,
                            "pragma_result": list(row) if row else None,
                            "ok": True,
                        }
                    )
                except sqlite3.Error as exc:
                    done.append({"name": item["name"], "ok": False, "error": str(exc)})
            return done

        results = await asyncio.to_thread(_run)
        failed = [r for r in results if not r.get("ok")]
        logger.info(
            "[Molt] checkpoint 完成：%d 个库，%d 个失败",
            len(results),
            len(failed),
        )
        return {
            "ok": not failed,
            "checkpointed": results,
            "reason": "部分库 checkpoint 失败" if failed else None,
        }

    def _register_api_if_available(self) -> None:
        if not hasattr(self.context, "register_web_api"):
            logger.warning("[Molt] 这个 AstrBot 版本没有 register_web_api，控制面不可用")
            return
        try:
            from .api import MoltControlApi

            self._api = MoltControlApi(self)
            self._api.register_routes()
        except Exception as exc:  # pragma: no cover
            self._api = None
            logger.warning("[Molt] 控制面 API 注册失败：%s", exc, exc_info=True)

        # dashboard 里的「插件配置同步」页面（views/sync/）。
        # 它和控制面是两件事：控制面管"闭不闭嘴"，这个管"配置一不一样"，
        # 所以注册失败也不该把控制面一起拖下水。
        try:
            from .sync_api import MoltSyncApi

            self._sync_api = MoltSyncApi(self)
            self._sync_api.register_routes()
        except Exception as exc:  # pragma: no cover
            self._sync_api = None
            logger.warning("[Molt] 同步页面 API 注册失败：%s", exc, exc_info=True)

    # ------------------------------------------------------------ 聊天指令（薄层）

    # 这一节全部是**壳**：取参数 → 交给 command.py → 说出去。
    # 逻辑一行都不在这里，理由见 command.py 开头的第一段（handler 必须和插件主模块
    # 同一个 `__module__`，否则 AstrBot 绑不到 `self`）。

    @filter.command("molt")
    @permission_type(PermissionType.ADMIN)
    async def cmd_molt(self, event: AstrMessageEvent):
        """迁徙入口。只有管理员能用；它只提议，不自己搬。"""
        if self._mover is None:
            yield event.plain_result("迁徙指令层没装成功，看日志。")
            return
        args = command_args(event.get_message_str())
        reply = self._mover.handle(args, sender=event.get_sender_id() or "")
        logger.info(
            "[Molt] 指令 /molt %s 来自 %s → %s",
            args or "(空)",
            event.get_sender_id() or "?",
            "已排定迁徙" if reply.plan is not None else "一句话回复",
        )
        yield event.plain_result(reply.text)

    @filter.llm_tool(name="molt_propose_go_home")
    async def molt_propose_go_home(
        self,
        event: AstrMessageEvent,
        target: str,
        reason: str,
        source: str = "self",
        quote: str = "",
    ) -> str:
        """提出想搬到另一台机器上。**只是提议**：主人说 confirm 才会真的动身。

        什么时候用它：你觉得自己该换一台机器了（比如这边要关机、那边本来就该常开），
        想问问主人行不行。它不是"搬家"，它只是"一句请求"。

        这次搬迁是谁的念头，要老实说 —— 确认时主人看到的那句话会写着它：
        你自己觉得该换机器了，填 `self`；主人刚说了「去打会儿游戏吧」这种话、
        你只是在替他把那句话变成一次搬迁，填 `utterance`。
        填错的后果不是搬错地方（去哪由 `target` 决定，执行永远要主人 confirm），
        而是主人在最需要看清「谁想搬」的时候，看到的是一句假话。

        如果你填了 `utterance`，**必须**在 `quote` 里放主人的原话片段。
        这句话会被核对：它必须真的出现在主人刚发的那条消息里，
        否则我不提这条建议 —— 因为确认时那句「这次是因为你那句『…』」是要给人看的，
        人只会记得自己说过什么，编一句很像的话比不给理由糟得多。

        Args:
            target(string): 想去的机器的名字，必须和账本上的一致，例如 ugreen-776b-8b7896df。留空表示搬去配置里写好的那台。
            reason(string): 你为什么想搬过去，一句话，会原样转告主人。
            source(string): 这次是谁的念头。你自己觉得该换机器了填 self；主人刚说了「去打会儿游戏吧」这种话、你只是替他把那句话变成一次搬迁，填 utterance。
            quote(string): 当 source 是 utterance 时，**必填**：主人的原话片段（照抄，别改写），比如 去打会儿游戏吧。我会核对它是否真的出现在他刚说的话里。
        """
        if self._mover is None:
            return "这台壳上没有迁徙配置，我不能搬。"
        try:
            cfg = self._mover.settings()
        except Exception as exc:  # noqa: BLE001 - 配置读不出来也不该炸掉一次对话
            return f"迁徙配置读不出来：{exc}。我没有动任何东西。"
        if not cfg.llm_tool_enabled:
            return (
                "模型还不能替我提这件事（`molt_command.json` 里 `llm_tool_enabled` 是 false）。"
                "这不是故障，是默认的关。"
            )
        reply = self._mover.request_move(
            cfg,
            target=target,
            reason=reason,
            source=source,
            quote=quote,
            said=event.get_message_str() or "",
        )
        logger.info(
            "[Molt] 工具请求迁徙：target=%r source=%r quote=%r 提议=%s",
            target or "(配置默认)",
            source,
            quote or "",
            "已写下" if self._mover.has_proposal() else "没接受",
        )
        return reply.text

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """AstrBot 起完了 —— 这时候才有一个真正在跑的事件循环。

        `initialize()` 里 `create_task` 出来的任务在真机上从来没被调度过
        （2026-11-15 实测：连 `loop.call_later` 的回调都不触发），而
        `on_astrbot_loaded` 是 `core_lifecycle` 在主循环上发出来的钩子，
        日志里能看到 `hook(on_astrbot_loaded) -> meme_manager ...` 就是它。
        """
        self._start_observe_task()
        self._start_push_task()

    @filter.on_waiting_llm_request()
    async def announce_arrival(self, event: AstrMessageEvent) -> None:
        """在它开口之前，先单独说一句「我刚从 X 搬过来」。

        为什么不再往回复的 chain 前面插一段：**这条回复是流式的**。
        
esult_decorate/stage.py:191-193 在 STREAMING_FINISH 时直接 return，
        而文字早已被逐块推给平台 —— 插进 chain 只是插进一个不会再被发送的对象。
        实测证据（2026-11-15 04:04）：插件确实记下了「第 15 代落地播报」，
        但同一秒 
esult_decorate.stage:169 打了「Plugins that depend on the
        pre-send event hook may not work correctly when streaming output is
        enabled.」，而人看到的那句话里没有它。

        所以改成在**发请求之前**单独发一条。三个好处：顺序天然在前、与流式无关、
        这句话不会被当成模型自己说的话写进对话历史（它是状态播报，不是记忆）。

        **送达之后才记账。** 组装出这句话只是第一步；`event.send()` 可能被平台
        挡下来（QQ 官方机器人的主动消息限制、msg_id 复用、网络抖动）。
        第一版在组装时就写了"这一代已经播报过"，于是一次失败 = 永久沉默。
        现在失败只记一条 WARNING、保留 pending，下一句话再试一次。
        """
        if self._arrival_said:
            return
        # 上一次没送到的那句优先 —— `arrival_note()` 已经被 `_arrival_checked`
        # 锁住了，重试时它只会返回空串，那句话就再也发不出去了。
        pending = self._arrival_pending
        note = pending[1] if pending is not None else self.arrival_note()
        if not note:
            return
        pending = self._arrival_pending
        try:
            await event.send(event.plain_result(note))
        except Exception as exc:  # noqa: BLE001 - 播报失败不该影响这次对话
            # **不标记、不清 pending**：下一次开口再试一遍。
            # 丢一句状态播报的代价，远小于"主人一整晚都没看见它说自己在哪"。
            logger.warning("[Molt] 落地播报没发出去，下一条回复再试：%s", exc)
            return
        self._arrival_said = True
        if pending is not None:
            number, _text = pending
            try:
                self._arrival_state_obj().mark(number, detail="announced")
            except Exception as exc:  # pragma: no cover - 防御性
                logger.warning("[Molt] 播报发出去了，但状态没写进去：%s", exc)
            logger.info("[Molt] 第 %d 代落地播报已送达", number)
            self._record_arrival(number)
        self._arrival_pending = None

    def _record_arrival(self, number: int) -> None:
        """把「这一代的播报已送达」写进账本。

        旧壳靠这条回执决定敢不敢关自己 —— "对面没报错"不是凭据，
        账本里一条 `delivered: true` 才是。失败只记一条日志：
        发播报本身已经完成了，回执丢了顶多让旧壳不关（那是最安全的方向）。
        """
        try:
            cfg = self._mover.settings()
            from molt.client import WardenClient  # noqa: PLC0415

            WardenClient(cfg.warden_url, cfg.warden_token).record_arrival(
                generation=number, shell=cfg.shell, delivered=True, detail="arrival note sent"
            )
        except Exception as exc:  # noqa: BLE001 - 回执失败不许影响对话
            logger.warning("[Molt] 落地播报的回执没写进账本：%s", exc)

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        """告别消息**真的发出去之后**，才开始搬。

        位置很关键：`migrate` 的第一件事是 `POST /drain`，把正在给这条消息
        发送用的那个适配器 terminate 掉。同一个事件里先 drain 再发消息，
        结果是"告别的话永远送不到"。
        """
        if self._mover is None:
            return
        plan = self._mover.take_pending()
        if plan is None:
            return
        asyncio.create_task(self._run_plan(plan))

    async def _run_plan(self, plan: Any) -> None:
        from .command import PRE_RUN_DELAY_SECONDS

        await asyncio.sleep(PRE_RUN_DELAY_SECONDS)
        logger.info("[Molt] 开始迁徙：%s", plan.describe())
        record = await asyncio.to_thread(self._mover.run_plan, plan)
        if record.get("ok"):
            logger.info(
                "[Molt] 迁徙完成（%.2f 秒）。这台壳现在是哑的——"
                "要去目标壳上接管。结果写在插件目录的 last-migrate.json。",
                record.get("elapsed_seconds", -1),
            )
        else:
            logger.error(
                "[Molt] 迁徙失败（%s）。如果 migrate 自己没恢复平台，"
                "我可能还哑着——看 last-migrate.json。",
                record.get("error") or f"退出码 {record.get('returncode')}",
            )


def command_args(message: str) -> str:
    """从 `/molt go` 里剥出 `go`。

    不依赖 AstrBot 的参数解析（那是给声明了参数的 handler 用的），因为我们要的
    恰恰是**原样**的剩余文本——包括用户打错的字。打错字时回一句"我不认识"，
    比猜成"go"然后搬走要安全得多。
    """
    text = (message or "").strip()
    # wake_prefix 默认是 `/`，但有人会改成 `!`、`！`、`。`
    for prefix in ("/", "\\", "!", "！", "。", "."):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    lowered = text.lower()
    if lowered.startswith("molt"):
        text = text[4:].strip()
    return text


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
