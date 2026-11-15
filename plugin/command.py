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
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

#: 指令配置所在的文件名。和 `host_label.txt` 一样放在插件目录里：
#: `data/plugins/**` 不在 `soul.py` 的 `COPY_ARTIFACTS` 里，所以它是**机器绑定**的。
#: 这一条很重要：`warden_token` 和 `control_url` 都属于"这台壳怎么联系权威"，
#: 换个身体就该换一套，绝不能跟着灵魂漂过去。
CONFIG_FILENAME = "molt_command.json"

#: 上一次迁徙的结果。放在插件目录里，方便迁徙失败、鱼哑了之后人还能回来看。
RESULT_FILENAME = "last-migrate.json"

#: 提议的有效期。太短了人来不及回，太长了就成了一个悬着的开关。
DEFAULT_CONFIRM_TTL_SECONDS = 120

#: 它自己提出「我想搬」时的有效期。30 分钟 —— 因为那是"一个人在忙/在睡"的尺度。
#: 它不降低安全性（`confirm` 永远要人来说），只降低"提议总是过期"的挫败感。
DEFAULT_SELF_PROPOSE_TTL_SECONDS = 1800

#: 发包之后多久内不再重复发。60 秒是"一次开机该等多久"的粗估：
#: 从发包到心跳上报，真机实测 11 秒开机 + 约 60 秒 AstrBot 起来。
#: 比这更频繁地重发不会提高成功率，只会让主人以为它卡住了。
WAKE_COOLDOWN_SECONDS = 60.0

#: 提议是谁提的。`SELF_PROPOSER` 表示"它自己想搬"——`confirm` 时会如实标注，
#: 因为"它想走"和"你让它走"是两件事，哪怕按下去的是同一只手。
SELF_PROPOSER = "self"

#: 触发源：**是谁让"想搬"这件事发生的**。
#:
#: 现在真的会产生提议的只有两种：`human`（人在聊天里敲 `/molt go`）与
#: `self`（模型自己调 `molt_propose_go_home`）。
#:
#: `utterance` 与 `schedule` 是**留给未来的接口**，不是现在能用的功能：
#: 前者是"主人一句话，它自己把那句话翻译成一次搬迁"（"去打会儿游戏吧"），
#: 后者是"到点了"（`0 3 * * *`）。它们一旦实装，走的仍然是同一个
#: `request_move()` —— 同一套校验、同一条 `/molt confirm`、同一个提议槽。
#:
#: **新增一个触发源不该新增一条执行路径。** 执行路径只有一条，
#: 所以 `molt` 的 fencing、租约、先死后生这些性质只需要在一个地方成立。
MOVE_SOURCES: Dict[str, str] = {
    "human": "你提的",
    "self": "我自己想走的",
    "utterance": "你那句话",
    "schedule": "到点了",
}


#: 定时触发的默认窗口：说 `23:30` 时，指的是 23:30 之后这半小时之内。
#: 为什么不是"只在那一分钟"：家用机器的时钟、守护进程 10 秒一轮的轮询、
#: 以及人不可能每次都把时刻掐到分钟，都让"恰好 23:30:00"变成一个不现实的判据。
SCHEDULE_DEFAULT_WINDOW_MINUTES = 30

#: 星期名 → Python 的 `tm_wday`（周一 = 0）。
_WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
}


def _parse_hhmm(text: Any) -> Optional[Tuple[int, int]]:
    """`"23:30"` / `"3:05"` → `(23, 30)`；`"25:00"` 这种不合法返回 None。"""
    raw = str(text or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def schedule_spec(trigger: Any) -> Optional[Dict[str, Any]]:
    """把 `triggers.schedule` 归一成 `{"minutes": 当天第几分钟, "window": 分钟数, "days": set|None}`。

    接受两种写法，因为人写配置的心情不一样：

    - 短写：`"23:30"`
    - 长写：`{"at": "23:30", "window_minutes": 45, "days": ["mon", "tue"]}`

    解析不出来返回 None —— **调用方必须把它当成"我不知道"，不是"不到点"**。
    """
    if isinstance(trigger, str):
        trigger = {"at": trigger}
    if not isinstance(trigger, dict):
        return None
    at = _parse_hhmm(trigger.get("at"))
    if at is None:
        return None
    window = trigger.get("window_minutes", SCHEDULE_DEFAULT_WINDOW_MINUTES)
    try:
        window = int(window)
    except (TypeError, ValueError):
        return None
    if not (1 <= window <= 24 * 60):
        return None
    days_raw = trigger.get("days")
    days: Optional[Set[int]] = None
    if days_raw is not None:
        if not isinstance(days_raw, (list, tuple)) or not days_raw:
            return None
        days = set()
        for item in days_raw:
            if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 6:
                days.add(int(item))
                continue
            key = str(item or "").strip().lower()
            if key in _WEEKDAYS:
                days.add(_WEEKDAYS[key])
                continue
            if key.isdigit() and 0 <= int(key) <= 6:
                days.add(int(key))
                continue
            return None
    return {"minutes": at[0] * 60 + at[1], "window": window, "days": days}


def schedule_problem(trigger: Any) -> str:
    """这条定时规则能不能用；能用返回空串，不能用返回一句人话。"""
    if schedule_spec(trigger) is not None:
        return ""
    return (
        '要一个时刻，比如 `"23:30"` 或 '
        '`{"at": "23:30", "window_minutes": 30, "days": ["mon"]}`，'
        f"我读到的是 `{trigger!r}`"
    )


def schedule_due(trigger: Any, now: float) -> Optional[bool]:
    """按**本机时区**判断"现在是不是在那个窗口里"。

    返回 `None` 表示"判不了"（没配、或配坏了）—— 调用方不许把它当成 False 或 True。
    窗口可以跨午夜（`"23:30"` + 30 分钟 = 到次日 00:00），所以比较是在"一天第几分钟"上做的。
    """
    spec = schedule_spec(trigger)
    if spec is None:
        return None
    lt = time.localtime(now)
    if spec["days"] is not None and lt.tm_wday not in spec["days"]:
        return False
    minutes = lt.tm_hour * 60 + lt.tm_min
    start = int(spec["minutes"])
    span = int(spec["window"])
    return ((minutes - start) % (24 * 60)) < span


def _shell_what(attrs: Dict[str, Any]) -> str:
    """把一条壳属性压成一句人话（`nas · 4070TiS`）。没登记就返回空串。

    返回空串而不是「unknown」：调用方拼进句子里时，空串会让那句话**少一段**，
    而 "unknown" 会让它读起来像"这是个已知的事实"。
    """
    role = str(attrs.get("role") or "").strip()
    if not role:
        return ""
    bits = [role]
    gpu = str(attrs.get("gpu") or "").strip()
    if gpu:
        bits.append(gpu)
    if attrs.get("wakeable") is True:
        bits.append("可唤醒")
    elif attrs.get("wakeable") is False:
        # 用"叫不醒"而不是"不可唤醒"：后者**包含**"可唤醒"这个子串，
        # 断言"没说可唤醒"时就永远为假 —— 一个能悄悄骗过测试的词。
        bits.append("叫不醒")
    return " · ".join(bits)


def source_label(source: str) -> str:
    """把触发源翻成一句能嵌进确认文案的话；不认识就返回空串。

    返回空串而不是"未知"是刻意的：调用方拿到空串就会退回"这次是你提的"这个
    保守说法，而不是在界面上打出 `source=xyz` 这种机器话。
    """
    return MOVE_SOURCES.get(str(source or "").strip(), "")

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

#: 「走」和「关」是两件事：这两个词回答的是**我走了之后那台机器关不关**。
POWER_WORDS = {
    "shutdown": "shutdown",
    "关机": "shutdown",
    "关掉": "shutdown",
    "keep": "keep",
    "留着": "keep",
    "别关": "keep",
}
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
    if first in POWER_WORDS:
        return POWER_WORDS[first]
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

    #: "叫醒那一步"给多久。叫醒之后提议不立即作废 —— 人还要再确认一次才搬。
    #: 600 秒是冷启动 + 登录 + agent 上报的余量，不是"等它一辈子"。
    wake_phase_ttl_seconds: int = 600

    #: 心跳多新才算"它现在真的开着"。**25 秒**，不是 60：
    #: 2026-11-15 23:38 那次就是 59 秒前的心跳被当成"还活着"，
    #: 于是封包一个都没发，鱼被推给一台正在关机的机器。
    alive_seconds: float = 25.0

    #: 它自己提出「我想搬」时的有效期。比 `confirm_ttl_seconds` 长得多，
    #: 因为它提出这句话的时候主人可能在睡觉，而 120 秒是给人当场回话用的。
    #: 长有效期不降低安全性：`confirm` 永远要人来说，而且执行前还会重新过一遍
    #: 配置检查、租约检查、唤醒与落地校验。
    self_propose_ttl_seconds: int = DEFAULT_SELF_PROPOSE_TTL_SECONDS

    #: 未来触发规则的配置槽。**本版本不解释它的内容，只做一件事：**
    #: 里面出现任何这个版本还不认识的东西，就把它当成阻塞项（见
    #: `MoltMover.move_policy_refusals`）。
    #:
    #: 为什么是"不认识就拦"而不是"不认识就忽略"：一个人往配置里写
    #: `"triggers": {"cron": "0 3 * * *"}`，他的意思肯定是"凌晨三点去"。
    #: 如果代码默默忽略，那条规则**永远不会生效，而且不会有任何提示** ——
    #: 他会以为配好了，直到某天发现鱼没动。宁可当场说"这个我还不认识"。
    triggers: Dict[str, Any] = field(default_factory=dict)

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

        #: 当前待确认的提议：(到期时刻, 谁提的, 去哪台)。
        #: **必须把目标一起记住**：它自己想搬去某台机器时，配置里的 `target` 可能不是那台；
        #: 只记"有一个提议"会让 `confirm` 搬去另一个地方 —— 而两边都不会报错。
        self._proposal: Optional[Tuple[float, str, str]] = None
        #: 已确认、待执行的计划。`after_message_sent` 取走它。
        self._pending: Optional[Plan] = None
        #: 上一次"已经请账本发过封包"的时刻与目标。存在的理由很具体：
        #: 2026-11-16 00:58 主人在机器开机期间连发了 5 次 `/molt confirm`，
        #: 每次都会再发一封 —— 那不是"更保险"，只是让人以为它坏了。
        self._last_wake: Optional[Tuple[float, str]] = None

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
        shells = st.get("shells") if isinstance(st.get("shells"), dict) else {}
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
            # 这个壳是什么。**没登记就不写** —— 编一个 role 出来，
            # 等于把"我不知道"说成"我知道"，而这句话会被拿去决定搬去哪。
            what = _shell_what(shells.get(str(shell_id)) or {})
            tail = f"，{what}" if what else ""
            others.append(f"· {shell_id}{mark}：{state}{tail}，最后心跳 {age}")
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
            f"账本 epoch {st.get('current_epoch', '?')}，已登记 {st.get('generation_count', '?')} 份快照。",
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
                    f"最近一次搬家 {last.get('generation', '?')}：{last.get('pushed_by', '?')} → "
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
        if want in POWER_WORDS:
            return self.request_power(cfg, POWER_WORDS[want])
        return Reply(self._help_text(cfg))

    def request_power(self, cfg: Settings, action: str) -> Reply:
        """我走了以后，那台机器关不关 —— 第二张票，和「走不走」分开。

        为什么要分开：人点一次「走」，顺带丢掉没保存的东西，是最难受的一种失败
        —— 因为「走」本身是对的，代价来自一件他没被问过的事。
        """
        st = self.ledger_status()
        if not isinstance(st, dict):
            return Reply("账本问不到，我不知道自己是从哪台机器搬过来的，所以不敢让你关任何机器。")
        gen = st.get("latest_generation") or {}
        if str(gen.get("consumed_by") or "").strip() != cfg.shell:
            return Reply("最近这一次搬家不是我取的 —— 我不知道哪台机器是你的旧壳，不猜。")
        source = str(gen.get("pushed_by") or "").strip()
        if not source or source == cfg.shell:
            return Reply("这一次是我自己推出去的，没有「我刚离开的那台机器」。")
        if not self._post_power(
            cfg,
            shell=source,
            action=action,
            generation=int(gen.get("generation") or 0),
        ):
            return Reply(
                f"票没送到账本（{cfg.warden_url or '未配置'}）。\n"
                f"你可以直接在那台机器上关，或者稍后再回一次 `/molt {action}`。"
            )
        if action == "shutdown":
            return Reply(
                f"好。`{source}` 会在下一轮心跳里看到这张票，然后关机。\n"
                "在那之前它要是改了主意，回 `/molt keep`。"
            )
        return Reply(f"好，`{source}` 留着不关。")

    def _post_power(self, cfg: Settings, *, shell: str, action: str, generation: int) -> bool:
        body = json.dumps(
            {"shell": shell, "action": action, "generation": generation, "by": cfg.shell}
        ).encode("utf-8")
        req = urllib.request.Request(
            cfg.warden_url.rstrip("/") + "/power",
            data=body,
            headers={"Content-Type": "application/json", "X-Molt-Token": cfg.warden_token or ""},
            method="POST",
        )
        try:
            with self._opener(req, timeout=5.0) as resp:
                return 200 <= getattr(resp, "status", 200) < 300
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _help_text(self, cfg: Settings) -> str:
        state = "已开启" if cfg.enabled else "未开启"
        return (
            f"迁徙入口当前{state}。\n"
            "· `/molt where` —— 我现在在哪台机器上、谁还活着\n"
            "· `/molt status` —— 上面那些，加上账本搬过几次、租约在谁手上\n"
            "· `/molt go` —— 提议搬去 " + (cfg.target or "（target 未配）") + "，我会问你确认\n"
            "· `/molt confirm` —— 真的搬（会先把我的嘴闭上，再说再见）\n"
            "· `/molt cancel` —— 取消还没确认的提议\n"
            "· `/molt shutdown` —— 我走了以后，把那台机器关掉（另一张票，和「走」分开）\n"
            "· `/molt keep` —— 我走了以后，那台机器留着别关"
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
            # 字段名是 `holder`，不是 `shell_id`（账本的租约视图只有 holder /
            # state / epoch / remaining_seconds）。读一个永远不存在的键，
            # 等于这个守卫从来没装过 —— 而它看起来和"确实没人在搬"一模一样。
            holder = lease.get("holder") if isinstance(lease, dict) else None
            if holder and lease.get("state") == "active":
                return Reply(
                    f"现在账本上 `{holder}` 正持着租约（epoch {lease.get('epoch')}）。"
                    "有别人在搬，我不插队。"
                )

        ttl = int(cfg.confirm_ttl_seconds)
        self._proposal = (self._clock() + ttl, sender, cfg.target, "human")
        return Reply(
            f"搬去 **{cfg.target}**？\n"
            f"{ttl} 秒内回 `/molt confirm` 我就动身；回 `/molt cancel` 或者不管它，就算了。\n"
            "动身的时候我会先把嘴闭上（断开 QQ），再打包、上传。然后我在这台机器上就不再说话了。"
        )

    # ------------------------------------------------------------- 它自己想去哪

    def known_shells(self, cfg: Settings) -> List[str]:
        """账本里出现过的壳名。**只从账本里取，不从 `cfg` 里猜。**

        `shell_id` 是别人写下来的字符串（`molt migrate --shell` 的决定权在那台机器上），
        所以"名字对不对"只能拿账本核对：心跳里有谁、最近几代从哪来/给了谁、以及我自己。
        """
        out: List[str] = []

        def _add(name: Any) -> None:
            text = str(name or "").strip()
            if text and text not in out:
                out.append(text)

        _add(cfg.shell)
        st = self.ledger_status()
        if isinstance(st, dict):
            beats = st.get("heartbeats")
            if isinstance(beats, dict):
                for shell_id in beats:
                    _add(shell_id)
            latest = st.get("latest_generation")
            if isinstance(latest, dict):
                _add(latest.get("target_shell"))
                _add(latest.get("pushed_by"))
                _add(latest.get("consumed_by"))
        return out

    def request_move(
        self,
        cfg: Settings,
        *,
        target: str = "",
        reason: str = "",
        source: str = SELF_PROPOSER,
        quote: str = "",
        said: str = "",
    ) -> Reply:
        """由**它自己**发起一次"我想搬"。只写提议，绝不执行。

        和 `/molt go` 的差别只有三处，每一处都有理由：

        1. **有效期更长**（`self_propose_ttl_seconds`，默认 30 分钟）——
           它说"我想回去了"的时候，主人可能在睡觉或者在外面。
           这不降低安全性：执行那一步永远要人来说 `confirm`，而且执行前会重新过一遍
           配置、租约、唤醒与落地校验。
        2. **目标可以由它挑**，但**必须在账本里真的存在**。账本上的壳名是别人写下来的
           字符串，它凭空编一个出来，结果是这台机器永远等不到自己那一代，
           而且**不会报错**——只会一直等。
        3. **措辞不同**：`/molt go` 是"你让我搬"，这里是"我想搬"，并且在确认时
           会如实标注这一点（不是你说要走的，是它提的）。
        """
        if not cfg.enabled:
            return Reply(
                "迁徙入口没开，我不会搬。\n"
                "打开方式：在插件目录建 `molt_command.json`，`enabled` 设为 true。"
            )
        problems = cfg.problems()
        if problems:
            return Reply(
                "配置不完整，我搬不了：\n· "
                + "\n· ".join(problems)
                + "\n改完立刻生效，不用重启。"
            )

        st = self.ledger_status()
        if isinstance(st, dict):
            lease = st.get("lease") or None
            # 字段名是 `holder`，不是 `shell_id`（账本的租约视图只有 holder /
            # state / epoch / remaining_seconds）。读一个永远不存在的键，
            # 等于这个守卫从来没装过 —— 而它看起来和"确实没人在搬"一模一样。
            holder = lease.get("holder") if isinstance(lease, dict) else None
            if holder and lease.get("state") == "active":
                return Reply(
                    f"现在账本上 `{holder}` 正持着租约（epoch {lease.get('epoch')}）。"
                    "有别人在搬，我不插队。"
                )

        want = str(target or "").strip() or str(cfg.target or "").strip()
        if not want:
            return Reply("我连要去哪都不知道：配置里没有 target，你也没给目标。")
        if want == str(cfg.shell or "").strip():
            return Reply(f"我就在 **{want}** 上，不用搬。")

        shells = self.known_shells(cfg)
        if want not in shells:
            return Reply(
                f"账本上没有叫 `{want}` 的壳，我不搬过去 —— 那样只会让这边一直等。\n"
                + (
                    "账本上现在有：\n· " + "\n· ".join(shells)
                    if shells
                    else "而且现在一个壳的心跳都读不到（账本连不上？），我不敢挑目标。"
                )
            )

        src = str(source or "").strip()
        if src not in MOVE_SOURCES:
            # 来源不明时连"要不要提"都不该提 —— 即使其它检查全都通过，
            # 我也不该替一个说不清身份的东西发出这条提议：确认时那句
            # 「这次是谁提的」是要给人看的，不能靠猜。
            return Reply(
                f"我不知道这次是谁想搬（`source={src or '空'}`）。\n"
                "不认识的来源我不提。"
            )

        quote_text = " ".join(str(quote or "").split())
        if src == "utterance":
            # 「主人那句话」是唯一一个**引用外部证据**的来源，所以它也是唯一一个
            # 必须在当场核对证据的：编一句很像的话出来，比不给理由糟得多 ——
            # 确认文案里那句「这次是因为你那句『…』」是要给人看的，
            # 而人只会记得自己说过什么。
            if not quote_text:
                return Reply(
                    "你说这是被主人那句话引出来的，但没告诉我是哪句话。\n"
                    "把那句原话一起给我 —— 我要能在对话里指出它。"
                )
            said_text = " ".join(str(said or "").split())
            if said_text and quote_text not in said_text:
                return Reply(
                    f"你引的那句话（{quote_text}）不在主人刚说的话里。\n"
                    "我不拿一句对不上的话当依据。"
                )

        blockers = self.move_policy_refusals(cfg, target=want, source=src)
        if blockers:
            return Reply(
                f"我想搬去 **{want}**，但现在不能提：\n· "
                + "\n· ".join(blockers)
                + "\n这些是你自己在配置里写下的约束。改掉之后我就能提了。"
            )

        # 目标现在开着还是关着 —— 这是主人要的第一句话。
        # 「先告诉他 Windows 是开着还是关着」不是一个客套，它是"要不要先开机"
        # 这个问题的前提；不知道就别问，问了也只是让人猜。
        power, age = self.target_state(st, want)
        attrs0 = None
        if isinstance(st, dict) and isinstance(st.get("shells"), dict):
            attrs0 = st["shells"].get(want)
        what0 = _shell_what(attrs0 or {}) if isinstance(attrs0, dict) else ""

        if power != "on":
            ttl = int(getattr(cfg, "self_propose_ttl_seconds", 1800))
            self._proposal = (self._clock() + ttl, src, want, src, quote_text, "wake")
            state_line = (
                f"最后心跳 {age:.0f} 秒前 —— 那是关着的" if isinstance(age, float)
                else "账本里没有它的心跳 —— 我不知道它开没开机"
            )
            return Reply(
                f"搬去 **{want}** 之前先说清楚：\n"
                f"· {want}：{state_line}"
                + (f"\n· 那边是：{what0}" if what0 else "")
                + f"\n\n要我先把它叫醒吗？回 `/molt confirm` 我就发魔术封包；"
                f"\n它上线之后我会再问你一次「要不要搬」—— **一共两次确认**。"
                f"\n`/molt cancel` 就算了，鱼哪儿都不去。"
            )

        ttl = int(cfg.self_propose_ttl_seconds)
        self._proposal = (self._clock() + ttl, src, want, src, quote_text, "move")
        why = str(reason or "").strip()
        # 目标那台机器是什么。账本里有声明就写，没有就**不编** ——
        # 一句"搬去 nas-1（存疑）"远好过"搬去 nas-1（NAS）"这种凭空断言。
        attrs = None
        if isinstance(st, dict) and isinstance(st.get("shells"), dict):
            attrs = st["shells"].get(want)
        what = _shell_what(attrs or {}) if isinstance(attrs, dict) else ""
        return Reply(
            f"我想搬去 **{want}**。"
            + (f"\n那边是：{what}" if what else "")
            + (f"\n理由：{why}" if why else "\n（没给理由）")
            + f"\n\n你说一句 `/molt confirm`，我就动身；`/molt cancel` 就算了。"
            f"\n这条提议 {ttl // 60} 分钟内有效 —— 你说要搬，才是搬。"
        )

    def move_policy_refusals(
        self, cfg: Settings, *, target: str, source: str = SELF_PROPOSER
    ) -> List[str]:
        """未来所有「什么时候可以搬」的判断都该加在这里。

        现在它只做一件事：**把配置里写着、但本版本还不认识的触发规则当成阻塞项**。

        这一条是给未来留的接口，但它现在就有用：一个人照着文档写
        `"triggers": {"cron": "0 3 * * *"}`，而 `cron` 还没实装时，正确表现是
        "我不搬，并且告诉你为什么"，不是安安静静地不搬。

        实装 `schedule` 那天，往这里加的就该是"现在到点了吗"；
        实装"那句话"那天，加的是"主人的意思是去还是不去"。**它们都只产出阻塞理由，
        不产出执行动作** —— 执行动作永远只有 `confirm` 之后那一条路。
        """
        out: List[str] = []
        triggers = getattr(cfg, "triggers", None)
        if isinstance(triggers, dict):
            for name in sorted(triggers):
                if name not in MOVE_SOURCES:
                    out.append(
                        f"`triggers.{name}` 这个触发源这个版本还不认识"
                        f"（认识的有：{'、'.join(sorted(MOVE_SOURCES))}）"
                    )
                elif name == "schedule":
                    problem = schedule_problem(triggers.get(name))
                    if problem:
                        out.append(f"`triggers.schedule` 我读不懂：{problem}")
                # 其余来源（human / self / utterance）只是"声明"，没有需要校验的形状：
                # `human` 就是那两条命令，`self` 是工具调用，`utterance` 的可归因性
                # 在 request_move 里逐次核对（引用必须能在原话里找到）。
        return out

    def target_state(self, st: Any, shell: str) -> Tuple[str, Optional[float]]:
        """目标现在是开着还是关着。**读不到就说读不到。**

        返回 `("on", age)` / `("off", age)` / `("unknown", None)`。
        `age` 是最后一次心跳距今多少秒。
        """
        if not isinstance(st, dict):
            return ("unknown", None)
        beats = st.get("heartbeats")
        if not isinstance(beats, dict):
            return ("unknown", None)
        entry = beats.get(shell)
        if not isinstance(entry, dict):
            return ("unknown", None)
        age = entry.get("age_seconds")
        try:
            age_f = float(age)
        except (TypeError, ValueError):
            return ("unknown", None)
        threshold = 25.0
        try:
            threshold = float(getattr(self.settings(), "alive_seconds", 25.0))
        except Exception:
            pass
        return ("on" if age_f <= threshold else "off", age_f)

    def wake_target(self, cfg: Settings, shell: str) -> Tuple[bool, str]:
        """请**账本那台机器**发魔术封包。

        为什么不在这里发：容器用的是 bridge 网络，往 255.255.255.255 的广播
        出不了 docker0 —— 包生成得出来、日志也"发了"，但它到不了网卡。
        2026-11-16 00:11 那次就是这么没叫醒的。
        """
        st = self.ledger_status()
        mac = ""
        wakeable: Optional[bool] = None
        if isinstance(st, dict) and isinstance(st.get("shells"), dict):
            attrs = st["shells"].get(shell) or {}
            if isinstance(attrs, dict):
                mac = str(attrs.get("wake_mac") or "").strip()
                if "wakeable" in attrs:
                    wakeable = bool(attrs.get("wakeable"))
        if not mac:
            mac = str(getattr(cfg, "wake_mac", "") or "").strip()
        if not mac:
            return (False, f"账本里没有 {shell} 的 MAC —— 我不知道往哪个网卡发。\n"
                           f"在那台机器上登记一次：`molt declare --shell {shell} --wakeable --wake-mac AA-BB-CC-DD-EE-FF`")
        if wakeable is False:
            return (False, f"账本说 {shell} 不可唤醒（wakeable: false）—— 这台机器只能人手动开。")
        last = self._last_wake
        if last is not None and last[1] == shell:
            age = self._clock() - last[0]
            if 0 <= age < WAKE_COOLDOWN_SECONDS:
                return (True, f"刚才已经发过一封（{age:.0f} 秒前），这次不重复发 —— "
                              f"我在等它报到")
        try:
            # 延迟导入：这一层刻意零 AstrBot 依赖，也不该在模块导入时就去碰
            # `molt.client`（它要 `urllib` 之外的东西，而且要能被单独拷出去跑）。
            # 这里犯过一次真错：直接写 `WardenClient(...)` 而没导入 ——
            # 所有测试都把 `wake_target` 整个换掉了，所以谁也没走到这一行，
            # 真机上回的是 `name 'WardenClient' is not defined`。
            from molt.client import WardenClient  # noqa: PLC0415

            client = WardenClient(cfg.warden_url, cfg.warden_token)
            got = client.wake(mac, host=str(getattr(cfg, "wake_host", "") or ""))
        except Exception as exc:
            return (False, f"让账本发包失败：{exc}")
        sent = got.get("sent") or []
        if not sent:
            return (False, "账本那边一个包都没发出去：" + "；".join(got.get("failed") or []))
        self._last_wake = (self._clock(), shell)
        return (True, f"已由账本发出 {len(sent)} 个封包（{' / '.join(sent)}）")

    def _expired(self) -> bool:
        if self._proposal is None:
            return True
        return self._clock() >= self._proposal[0]

    def has_proposal(self) -> bool:
        """现在有没有一条还没过期的、等人点头的提议。"""
        return self._proposal is not None and not self._expired()

    def _confirm(self, cfg: Settings) -> Reply:
        if not cfg.enabled:
            return Reply("迁徙入口没开，我不会搬。")
        if self._proposal is None:
            return Reply("没有等我确认的迁徙。先 `/molt go`。")
        if self._expired():
            self._proposal = None
            return Reply(
                "上一次提议已经过期"
                f"（`/molt go` 是 {int(cfg.confirm_ttl_seconds)} 秒，"
                f"我自己提的是 {int(cfg.self_propose_ttl_seconds)} 秒）。"
                "这条时效是刻意的——我不希望很久以前的一句话在某个意外时刻生效。"
            )
        problems = cfg.problems()
        if problems:
            self._proposal = None
            return Reply("配置不完整，我搬不了：\n· " + "\n· ".join(problems))

        was = self._proposal[1]
        # 老的四元组里有显式的来源；三元组（v0.5 的 `/molt go`）只能从
        # "谁提的"推：`self` 就是它自己，其它都是人。
        source = (
            self._proposal[3]
            if len(self._proposal) > 3
            else (SELF_PROPOSER if was == SELF_PROPOSER else "human")
        )
        # 目标以**提议里记下的那个**为准：它自己挑的时候，配置里的 target 可能不是那台。
        phase = str(self._proposal[5]) if len(self._proposal) > 5 else "move"
        wanted = self._proposal[2] if len(self._proposal) > 2 else ""
        wanted = str(wanted or "").strip() or cfg.target
        # 那句原话要在清空提议**之前**取出来，否则确认文案里就只剩"你那句话"四个字。
        quote_text = str(self._proposal[4] or "").strip() if len(self._proposal) > 4 else ""

        if phase == "wake":
            # 第一段：叫醒。**它不搬。** 搬是第二段的事，
            # 而第二段之前会再确认一次"它现在真的开着"。
            ok_sent, msg = self.wake_target(cfg, wanted)
            if not ok_sent:
                self._proposal = None
                return Reply(f"没叫醒它：{msg}\n鱼没动，还在这台机器上。")
            ttl = int(getattr(cfg, "wake_phase_ttl_seconds", 600))
            self._proposal = (self._clock() + ttl, self._proposal[1], wanted,
                              self._proposal[3], quote_text, "move")
            return Reply(
                f"好，发包了 —— {msg}。\n"
                f"等它开机（我这边看的是心跳，不是 ping）。\n\n"
                f"它上线之后，你回 `/molt confirm` —— **那一次就是第二次确认，我会直接动身**。\n"
                f"如果还没上线，我会告诉你「它还没上线」，鱼一直在这台机器上。"
            )

        # 第二段：真的搬。但先自己核一遍目标是不是真的起来了 ——
        # 人按 confirm 的时候可能只是"以为它开了"。
        st = self.ledger_status()
        power, age = self.target_state(st, wanted)
        if power != "on":
            state_line = (
                f"最后心跳 {age:.0f} 秒前" if isinstance(age, float) else "账本里没有它的心跳"
            )
            # 它刚才还开着、现在却关了 —— 最常见的成因就是**上一次交接完，关机票执行了**。
            # 于是 `/molt go` 那会儿它还是 "on"（phase=move，问都没问），等按 confirm
            # 时它已经躺下了。这种时候光说"没上线"没用：得就地把它叫醒。
            # 注意：**提议不清、鱼不动**，封包只是让人少走一趟 `/molt go`。
            ok_sent, why = self.wake_target(cfg, wanted)
            if ok_sent:
                return Reply(
                    f"它现在还没上线（{state_line}）。\n"
                    f"我又发了一次魔术封包 —— {why}。\n\n"
                    f"等它上线，你再回一次 `/molt confirm`；那一次我就会动身。\n"
                    f"鱼没动，还在这台机器上。"
                )
            return Reply(
                f"先不搬：**{wanted} 现在还没有上线**（{state_line}）。\n"
                f"我也叫不醒它：{why}\n"
                f"鱼没动，还在这台机器上 —— 点了确认也不会把它推进一台关着的机器里。\n"
                f"等它起来再发 `/molt confirm`；不想搬了就 `/molt cancel`。"
            )

        self._proposal = None
        plan = build_plan(cfg, target=wanted)
        self._pending = plan
        label = source_label(source)
        if source == SELF_PROPOSER:
            note = "\n（这次是我想走的，不是你说要走的 —— 但按下去的是你）"
        elif source == "utterance" and quote_text:
            note = f"\n（这次是因为你那句「{quote_text}」）"
        elif label:
            note = f"\n（这次是{label}）"
        elif was:
            note = "\n（这次是你提的）"
        else:
            note = ""
        return Reply(
            "好，我走了。\n"
            "接下来：断开 QQ → 热备数据库 → 打包 → 上传账本 → 交还租约。\n"
            f"搬完我在这台机器上就是哑的了；要去 **{wanted}** 上叫我（那边落地之后才会开口）。"
            + note,
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


def build_plan(cfg: Settings, *, target: str = "") -> Plan:
    """把配置翻译成一条真的能跑的命令。**逐项显式列出来**，不拼字符串。

    `target` 覆盖配置里的目标 —— 只在"它自己想搬去某台特定的机器"时需要。
    为什么不让调用方事后改 argv：那条命令里的每个参数都是"这次迁徙要搬去哪"的一部分，
    改一个字符串和重新生成一遍，出错的概率不一样。
    """
    where_to = str(target or "").strip() or cfg.target
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
        where_to,
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
