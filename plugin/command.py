"""把"发一条消息"变成"搬一次家"——但只到"提议"为止的那一半。

## 为什么这里没有一行 `astrbot` 导入

`main.py` 里的 handler 必须和 `MoltControlPlugin` **同一个模块**，否则
AstrBot 绑定实例时会找不到它：

    # astrbot/core/star/star_manager.py:1264
    related_handlers = star_handlers_registry.get_handlers_by_module_name(
        metadata.module_path,
    )
    ...
    handler.handler = functools.partial(raw_handler, metadata.star_cls)

它按 `handler.__module__` 匹配，然后 `functools.partial(原始函数, 插件实例)` 绑上去。
也就是说：**装饰器写在哪个模块，那个模块就必须是插件主模块**，否则 handler
永远拿不到 `self`，第一个位置参数会是 `event`——报错信息还会指着一个不相干的
地方（`TypeError: cmd_molt() missing 1 required positional argument`）。

所以这一层刻意做成"纯标准库、零 AstrBot 依赖"：
- `main.py` 只需要四行薄的装饰器方法，把事件转成一次 `handler(text)` 调用；
- 这里的全部逻辑可以脱离 AstrBot 与 quart 测试（`test_plugin_command.py` 就是这么做的）；
- 把它拷到 NAS 上、甚至在容器外用 `python3` 直接跑，也能工作。

## 安全模型：三道闸，缺一不可

1. **指令 + 管理员权限**。`/molt go` 只对 `admins_id` 里的人有效
   （`PermissionType.ADMIN`）。群里任何人说"回家"都不算数。
2. **两步确认，且确认有时效**。`go` 只写一个 120 秒的提议，`confirm` 才真的搬。
   提议存在内存里，进程一重启就没了——这是好意：一个跨重启还活着的"待确认迁徙"
   意味着你昨天随手提的一句，今天重启后突然生效了。
3. **`enabled` 默认是 false**。配置文件不存在 = 没开。这不是谨慎，
   是这一层和 `overlay.py` 的 `platform[].enable` 用同一条理由：
   **漏开 = 机器人不搬（一条日志），漏关 = 它在你没盯着的时候搬了（一次事故）。**

## 它为什么不自己判断"该不该搬"

AstrBot 里有一个 LLM，它能理解"回家吧"这句话——但那正是最不该给它这个权力的地方。
这里的做法是：**LLM 可以提议，人才能执行。**

- `llm_tool_enabled: true` 时，模型可以调 `molt_propose_go_home` 工具，
  而这个工具**只做一件事**：写下同一个 120 秒提议，返回一句话让模型去问主人。
- 真正的执行永远走 `/molt confirm`，**不经过模型**。

即使模型疯了、被提示注入、或者单纯误判，它能造成的最大后果是"问了你一句"。

## 执行发生在"消息发出去之后"

`/molt confirm` 的回复是"好，我走了"。但 `migrate` 的第一件事就是
`POST /drain` 把 QQ 连接断掉——如果两步在同一个事件里做完，
那条告别消息**根本发不出去**（适配器已经被 terminate 了）。

所以：命令 handler 只负责**说**，并把计划挂在一个字段上；
`main.py` 里 `@filter.after_message_sent` 的钩子看到那个字段才真的开跑。
AstrBot 在 `pipeline/respond/stage.py:406` 调这个钩子，位置就在
`await event.send(chain)` 之后。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

#: 指令配置所在的文件名。和 `host_label.txt` 一样放在插件目录里：
#: `data/plugins/**` 不在 `soul.py` 的 `COPY_ARTIFACTS` 里，所以它是**机器绑定**的。
#: 这一条很重要：`warden_token` 和 `control_url` 都属于"这台壳怎么联系权威"，
#: 换个身体就该换一套，绝不能跟着灵魂漂过去。
CONFIG_FILENAME = "molt_command.json"

#: 上一次迁徙的结果。放在插件目录里，方便迁徙失败、鱼哑了之后人还能回来看。
RESULT_FILENAME = "last-migrate.json"

#: 提议的有效期。太短了人来不及回，太长了就成了一个悬着的开关。
DEFAULT_CONFIRM_TTL_SECONDS = 120

#: `migrate` 最长跑多久。真机上一次 1.4–2 秒，但热备大库、上传慢的时候会更久；
#: 900 秒是"防止卡死"而不是"预期耗时"。
MIGRATE_TIMEOUT_SECONDS = 900

#: 告别消息发出去之后，等这么久才开始搬。
#: 为什么不是 0：AstrBot 的 respond 流水线此时还没走完（`after_message_sent`
#: 在 `pipeline/respond/stage.py:406`，后面还有 `event.clear_result()` 等收尾）。
#: 我们自己第一件事就是 `POST /drain` 把自己这个适配器 terminate 掉——
#: 在流水线跑到一半时把它的平台对象拆了，只会换来一段没有意义的 traceback。
#: 两秒足够它自己走完；而这条消息早就发出去了，人不会多等。
PRE_RUN_DELAY_SECONDS = 2.0

# --------------------------------------------------------------------- 参数归一化

#: 说这些话 = "我想搬家"。中英混着收，因为有人在群里打英文，有人在手机上打中文。
GO_WORDS = {"go", "home", "gohome", "回家", "回nas", "迁徙", "搬家", "去windows", "去win"}
CONFIRM_WORDS = {"confirm", "yes", "ok", "确认", "确定", "走"}
CANCEL_WORDS = {"cancel", "no", "取消", "算了", "不走了"}
#: 「你在哪」和「账本怎么样」是两件事，所以两套词、两份输出。
#: 早先它们被并成一套（五个词一份输出），结果是**名字暗示两种内容、实际只有一份**——
#: 一个说自己叫 `where` 却回了一大段账本数字的命令，比没有这个命令更让人困惑。
WHERE_WORDS = {"where", "在哪", "在哪呢"}
STATUS_WORDS = {"status", "状态"}
HELP_WORDS = {"help", "?", "帮助", "怎么用"}


def normalize_args(raw: str) -> str:
    """把用户敲的参数折成四种意图之一，认不出来就返回 `""`。

    故意不做"包含"匹配（`"回家" in text`）：那会让"我不想回家"变成"回家"。
    只认**整词相等**，宁可让人多敲一个字，也不要在这种事情上猜。
    """
    token = (raw or "").strip().lower()
    if not token:
        return ""
    # 允许 `/molt 回家 吧` 这种，但只取第一个词判意图，剩下的忽略。
    first = token.split()[0]
    if first in GO_WORDS:
        return "go"
    if first in CONFIRM_WORDS:
        return "confirm"
    if first in CANCEL_WORDS:
        return "cancel"
    if first in WHERE_WORDS:
        return "where"
    if first in STATUS_WORDS:
        return "status"
    if first in HELP_WORDS:
        return "help"
    return ""


# ------------------------------------------------------------------------- 配置


class ConfigError(Exception):
    """配置读不出来 / 配错了。调用方应当把它整句显示给人看。"""


@dataclass
class Settings:
    """`molt_command.json` 的内容。

    所有字段都有默认值，但**没有 `enabled=true` 就等于没开**。
    """

    enabled: bool = False
    llm_tool_enabled: bool = False

    #: 这台壳在账本里的名字（`molt shell` 会打印它）
    shell: str = ""
    #: 目标壳的名字。要搬去哪台机器。
    target: str = ""

    #: AstrBot 的 data 目录（容器内路径）
    data_dir: str = "/AstrBot/data"
    #: molt 源码包所在目录（`PYTHONPATH` 指它，`cwd` 也是它）
    molt_pkg: str = ""
    #: 跑 molt 的 python。容器里就是 `python3`。
    python: str = "python3"

    #: 权威（Warden）的地址。**容器内不能用 127.0.0.1**——那是容器自己。
    #: 默认写 docker 网桥网关，因为 `_molt/` 不在容器的 bind mount 里，
    #: 只能从网络上够到宿主。
    warden_url: str = "http://172.17.0.1:6186"
    warden_token: str = ""

    #: 控制面（就是本插件）的地址。这个是容器内 127.0.0.1，对的。
    control_url: str = "http://127.0.0.1:6185"
    control_config: str = "/AstrBot/data/cmd_config.json"

    #: 可选：迁徙前先把目标机叫醒。空字符串 = 不发魔术封包。
    wake_mac: str = ""
    wake_host: str = ""

    confirm_ttl_seconds: int = DEFAULT_CONFIRM_TTL_SECONDS

    @staticmethod
    def load(path: Path) -> "Settings":
        if not path.is_file():
            # 文件不存在不是错误：它等价于"没开"。让人为了"关闭"去删一个文件，
            # 比让人为了"关闭"去改一个字段要难出错得多。
            return Settings()
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ConfigError(f"读不了 {path}：{exc}") from exc
        # `cmd_config.json` 有 BOM，我们自己写的文件没有。两头都吃。
        if raw[:3] == b"\xef\xbb\xbf":
            raw = raw[3:]
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"{path.name} 不是合法的 UTF-8 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{path.name} 的顶层必须是对象，实际是 {type(data).__name__}")
        known = {f.name for f in Settings.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        return Settings(**kwargs)

    def problems(self) -> List[str]:
        """检查"开了之后能不能真的跑起来"。返回人话，空列表 = 没问题。"""
        out: List[str] = []
        if not self.shell:
            out.append("shell 没填（这台壳在账本里的名字）")
        if not self.target:
            out.append("target 没填（要搬去哪台）")
        if not self.warden_token:
            out.append("warden_token 没填")
        if not self.data_dir:
            out.append("data_dir 没填")
        if not self.molt_pkg:
            out.append("molt_pkg 没填（molt 源码在哪）")
        elif not Path(self.molt_pkg).is_dir():
            out.append(f"molt_pkg 指向的目录不存在：{self.molt_pkg}")
        if not self.control_config:
            out.append("control_config 没填")
        try:
            int(self.confirm_ttl_seconds)
        except (TypeError, ValueError):
            out.append(f"confirm_ttl_seconds 不是整数：{self.confirm_ttl_seconds!r}")
        return out


# --------------------------------------------------------------------- 计划与结果


@dataclass
class Plan:
    """一次已经确认、等着执行的迁徙。"""

    argv: List[str]
    cwd: str
    env: Dict[str, str]
    where: str = ""

    def describe(self) -> str:
        return " ".join(self.argv)


@dataclass
class Reply:
    """命令层返回给 `main.py` 的一句话 + 一个可选动作。"""

    text: str
    #: 非 None 时，`main.py` 应当在消息发出**之后**开跑它
    plan: Optional[Plan] = None


# --------------------------------------------------------------------- 搬家器本体


class MoltMover:
    """管提议、管执行、管报告。**不认识 AstrBot**，只认识文本和一个目录。"""

    def __init__(
        self,
        plugin_dir: Path,
        *,
        clock: Callable[[], float] = time.time,
        runner: Optional[Callable[..., Any]] = None,
        opener: Optional[Callable[..., Any]] = None,
    ):
        self.plugin_dir = Path(plugin_dir)
        self._clock = clock
        self._runner = runner or subprocess.run
        self._opener = opener or urllib.request.urlopen

        #: 当前待确认的提议：(到期时刻, 谁提的)
        self._proposal: Optional[Tuple[float, str]] = None
        #: 已确认、待执行的计划。`after_message_sent` 取走它。
        self._pending: Optional[Plan] = None

    # -------------------------------------------------------------- 配置与只读信息

    @property
    def config_path(self) -> Path:
        return self.plugin_dir / CONFIG_FILENAME

    def settings(self) -> Settings:
        return Settings.load(self.config_path)

    def ledger_status(self) -> Optional[Dict[str, Any]]:
        """问 Warden 现在谁在线。**问不到就返回 None，不猜。**

        `status` 这一句最容易被写成"读不到就显示上个缓存"，
        而那会让人看着一份旧的"当前在线：NAS"去做关机决定。
        """
        try:
            cfg = self.settings()
        except ConfigError:
            return None
        if not cfg.warden_url:
            return None
        req = urllib.request.Request(
            cfg.warden_url.rstrip("/") + "/status",
            headers={"X-Molt-Token": cfg.warden_token or ""},
            method="GET",
        )
        try:
            with self._opener(req, timeout=5.0) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        status = payload.get("status")
        return status if isinstance(status, dict) else None

    def _self_label(self) -> str:
        """这台壳叫什么。与 `host_label.txt` 同一套规矩（BOM、只取第一行）。"""
        side = self.plugin_dir / "host_label.txt"
        try:
            if side.is_file():
                raw = side.read_text(encoding="utf-8-sig").strip()
                first = raw.splitlines()[0].strip() if raw else ""
                if first:
                    return first
        except OSError:
            pass
        try:
            import socket as _socket

            return _socket.gethostname() or "unknown"
        except OSError:
            return "unknown"

    def _where_lines(self, st: Dict[str, Any]) -> List[str]:
        """「鱼现在在哪」——不用问它，看一眼就知道。"""
        me = self._self_label()
        beats = st.get("heartbeats")
        lines = [f"我现在在 **{me}**。"]
        if not isinstance(beats, dict) or not beats:
            lines.append("其它壳还没有上报过心跳，所以只知道我自己在哪。")
            return lines
        others = []
        for shell_id, beat in sorted(
            beats.items(), key=lambda kv: kv[1].get("age_seconds", 0)
        ):
            if not isinstance(beat, dict):
                continue
            age = beat.get("age_readable") or ""
            state = "哑了" if beat.get("stale") else "在跑"
            mark = "（就是我）" if str(shell_id) == me else ""
            others.append(f"· {shell_id}{mark}：{state}，最后心跳 {age}")
        if others:
            lines.append("")
            lines.extend(others)
        return lines

    def _where_text(self, cfg: Settings) -> str:
        """只回答"你在哪"。

        **不检查 `enabled`**：查位置是只读的，插件装上就该能用。
        "能不能搬家"和"能不能告诉我它在哪"是两件事，把它们绑在一起，
        只会让人为了看一眼位置去开一个会搬家的开关。
        """
        st = self.ledger_status()
        if st is None:
            return f"问不到账本：{cfg.warden_url or '（warden_url 没配）'}\n它可能没在跑，也可能这台壳够不到它。"
        return "\n".join(self._where_lines(st))

    def _status_text(self, cfg: Settings) -> str:
        # 早先这里在 `not cfg.enabled` 时直接返回"入口没开，现在能做的只有查询"，
        # 然后**拒绝查询** —— 一句自己打自己脸的话，而且默认配置下每个人都撞得到。
        st = self.ledger_status()
        if st is None:
            return f"问不到账本：{cfg.warden_url or '（warden_url 没配）'}\n它可能没在跑，也可能这台壳够不到它。"
        lease = st.get("lease") or None
        lines = self._where_lines(st)
        lines += [
            "",
            f"账本 epoch {st.get('current_epoch', '?')}，已登记 {st.get('generation_count', '?')} 代。",
        ]
        # 账本的租约视图用的是 `holder`（没有租约时是 {"state": "no-lease"}）。
        # 早先这里读的是 `shell_id`，于是永远走 else 分支、永远说"没有租约" ——
        # 一个从来不成立的判断，看起来和"真的没有租约"一模一样。
        holder = lease.get("holder") if isinstance(lease, dict) else None
        if holder:
            lines.append(
                f"现在持租约的是 **{holder}**（epoch {lease.get('epoch')}）。有别人在，我不搬。"
            )
        else:
            lines.append("现在没有租约——没有任何壳在搬家的路上。")
        gens = st.get("generations")
        if isinstance(gens, list) and gens:
            last = gens[-1]
            if isinstance(last, dict):
                lines.append(
                    f"最新一代 {last.get('generation', '?')}：{last.get('pushed_by', '?')} → "
                    f"{last.get('target_shell') or '（未指定）'}，"
                    f"{last.get('data_bytes', '?')} 字节。"
                )
        return "\n".join(lines)

    # ------------------------------------------------------------------ 三种意图

    def handle(self, text: str, *, sender: str = "") -> Reply:
        """把一句话变成一次回复（+ 可能的一次执行计划）。

        `text` 是指令**参数**，不含 `/molt` 本身（`main.py` 已经剥掉了）。
        """
        try:
            cfg = self.settings()
        except ConfigError as exc:
            return Reply(f"迁徙配置读不出来：{exc}\n我没有动任何东西。")

        want = normalize_args(text)

        if want == "" or want == "help":
            return Reply(self._help_text(cfg))
        if want == "where":
            return Reply(self._where_text(cfg))
        if want == "status":
            return Reply(self._status_text(cfg))
        if want == "go":
            return self._propose(cfg, sender=sender)
        if want == "confirm":
            return self._confirm(cfg)
        if want == "cancel":
            return self._cancel()
        return Reply(self._help_text(cfg))

    def _help_text(self, cfg: Settings) -> str:
        state = "已开启" if cfg.enabled else "未开启"
        return (
            f"迁徙入口当前{state}。\n"
            "· `/molt where` —— 我现在在哪台机器上、谁还活着\n"
            "· `/molt status` —— 上面那些，加上账本第几代、租约在谁手上\n"
            "· `/molt go` —— 提议搬去 " + (cfg.target or "（target 未配）") + "，我会问你确认\n"
            "· `/molt confirm` —— 真的搬（会先把我的嘴闭上，再说再见）\n"
            "· `/molt cancel` —— 取消还没确认的提议"
        )

    def _propose(self, cfg: Settings, *, sender: str) -> Reply:
        if not cfg.enabled:
            return Reply(
                "迁徙入口没开，我不会搬。\n"
                "打开方式：在插件目录建 `molt_command.json`，`enabled` 设为 true。"
            )
        problems = cfg.problems()
        if problems:
            return Reply(
                "配置不完整，我搬不了：\n· " + "\n· ".join(problems) + "\n改完立刻生效，不用重启。"
            )

        # 已经有租约被别人拿着 → 连提议都不发。让人先看清楚再动。
        st = self.ledger_status()
        if isinstance(st, dict):
            lease = st.get("lease") or None
            if isinstance(lease, dict) and lease.get("shell_id"):
                return Reply(
                    f"现在账本上 `{lease.get('shell_id')}` 正持着租约（{lease.get('epoch')}）。"
                    "有别人在搬，我不插队。"
                )

        ttl = int(cfg.confirm_ttl_seconds)
        self._proposal = (self._clock() + ttl, sender)
        return Reply(
            f"搬去 **{cfg.target}**？\n"
            f"{ttl} 秒内回 `/molt confirm` 我就动身；回 `/molt cancel` 或者不管它，就算了。\n"
            "动身的时候我会先把嘴闭上（断开 QQ），再打包、上传。然后我在这台机器上就不再说话了。"
        )

    def _expired(self) -> bool:
        if self._proposal is None:
            return True
        return self._clock() >= self._proposal[0]

    def _confirm(self, cfg: Settings) -> Reply:
        if not cfg.enabled:
            return Reply("迁徙入口没开，我不会搬。")
        if self._proposal is None:
            return Reply("没有等我确认的迁徙。先 `/molt go`。")
        if self._expired():
            self._proposal = None
            return Reply(
                f"上一次提议已经过期（{int(cfg.confirm_ttl_seconds)} 秒）。"
                "这条时效是刻意的——我不希望很久以前的一句话在某个意外时刻生效。"
            )
        problems = cfg.problems()
        if problems:
            self._proposal = None
            return Reply("配置不完整，我搬不了：\n· " + "\n· ".join(problems))

        was = self._proposal[1]
        self._proposal = None
        plan = build_plan(cfg)
        self._pending = plan
        return Reply(
            "好，我走了。\n"
            "接下来：断开 QQ → 热备数据库 → 打包 → 上传账本 → 交还租约。\n"
            f"搬完我在这台机器上就是哑的了；要去 **{cfg.target}** 上叫我（那边落地之后才会开口）。"
            + (f"\n（这次是你提的）" if was else ""),
            plan=plan,
        )

    def _cancel(self) -> Reply:
        had = self._proposal is not None
        self._proposal = None
        return Reply("取消了，我不搬。" if had else "本来就没有待确认的迁徙。")

    # -------------------------------------------------------------------- 执行

    def take_pending(self) -> Optional[Plan]:
        plan = self._pending
        self._pending = None
        return plan

    def has_pending(self) -> bool:
        return self._pending is not None

    def run_plan(self, plan: Plan) -> Dict[str, Any]:
        """真跑一次 `molt migrate`。**同步函数，调用方要放进线程。**

        为什么用子进程而不是 `import molt` 直接调：
        ① `migrate` 会 `POST /drain` 到本进程，在同一个事件循环里同步等它
           等于自己等自己，必然死锁；
        ② 子进程崩了、被 OOM 杀了，都不影响 AstrBot 本身——
           "搬家程序"不该有能力带走"家"。
        """
        started = self._clock()
        record: Dict[str, Any] = {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
            "argv": list(plan.argv),
            "cwd": plan.cwd,
        }
        try:
            proc = self._runner(
                plan.argv,
                cwd=plan.cwd,
                env=plan.env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=MIGRATE_TIMEOUT_SECONDS,
            )
            record["returncode"] = proc.returncode
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            record["stdout_tail"] = out[-4000:]
            record["stderr_tail"] = err[-4000:]
            record["ok"] = proc.returncode == 0
        except subprocess.TimeoutExpired:
            record["ok"] = False
            record["returncode"] = None
            record["error"] = f"超时（{MIGRATE_TIMEOUT_SECONDS} 秒）"
        except OSError as exc:
            record["ok"] = False
            record["returncode"] = None
            record["error"] = f"起不来：{exc}"
        record["elapsed_seconds"] = round(self._clock() - started, 3)
        record["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        self._write_result(record)
        return record

    def _write_result(self, record: Dict[str, Any]) -> None:
        """写成功率最高的一件事：**给哑了之后的自己留一份遗嘱**。

        如果迁徙失败，鱼是哑的、账本可能什么都没记。人回来第一件事是
        "刚才到底怎么了"，而日志在容器里、会被下一次重启冲掉。
        所以落一份文件在插件目录（机器绑定、不进灵魂）。
        """
        try:
            path = self.plugin_dir / RESULT_FILENAME
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            # 写不下去也不该让一次迁徙失败变成一次异常。
            pass


# --------------------------------------------------------------------------- 工具


def build_plan(cfg: Settings) -> Plan:
    """把配置翻译成一条真的能跑的命令。**逐项显式列出来**，不拼字符串。"""
    argv: List[str] = [
        cfg.python,
        "-m",
        "molt.cli",
        "migrate",
        "--data",
        cfg.data_dir,
        "--shell",
        cfg.shell,
        "--target",
        cfg.target,
        "--warden-url",
        cfg.warden_url,
        "--warden-token",
        cfg.warden_token,
        # 关键：让 migrate 先按住我们自己。它会在热备之前 POST /drain，
        # 然后轮询确认我们真的不说话了，才拍快照。
        "--control-url",
        cfg.control_url,
        "--control-config",
        cfg.control_config,
        "--no-color",
    ]
    if cfg.wake_mac:
        argv += ["--wake-mac", cfg.wake_mac]
        if cfg.wake_host:
            argv += ["--wake-host", cfg.wake_host]

    env = dict(os.environ)
    env["PYTHONPATH"] = cfg.molt_pkg
    # 容器里没有 /etc/localtime 的时区，日志时间会用 UTC；显式给一个，
    # 免得"我几点搬的家"和 AstrBot 自己的日志对不上。
    env.setdefault("TZ", "Asia/Shanghai")
    return Plan(argv=argv, cwd=cfg.molt_pkg, env=env, where=cfg.control_url)
