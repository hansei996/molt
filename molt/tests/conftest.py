"""pytest 全局夹具。

## 为什么要有这个文件

`molt/plugin/` 里的插件是**跑在 AstrBot 进程里**的代码，它的第一行 import 就是
`from astrbot.api.star import Context, Star`。而跑测试的那个 Python（3.13 + pytest）
并没有装 AstrBot —— 也不该装：装一个带 122 个 `.so` 的运行时进测试环境，
会让"跑测试"从一个 15 秒的动作变成一件需要慎重考虑的事。

所以这里给 `astrbot.*` 打一个**最小桩**：只提供插件真正用到的那几个名字，
并且**故意保持它们的语义最简**（`Star` 是个空基类，`Context` 啥也不做）。
这里是刻意的：如果桩做得太像，测试验的就不再是"插件的逻辑"，
而是"我对 AstrBot 内部结构的想象"。真正需要精确模仿 manager 行为的地方，
在 `test_plugin_control.py` 里单独写了个有明确契约的假 `PlatformManager`，
而且那个假的契约本身也被一条测试盯着（`test_fake_matches_real_contract`）。
"""

import sys
import types
from pathlib import Path

MOLT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = MOLT_ROOT / "plugin"


def _install_astrbot_stub() -> None:
    """装一个只够 import 通过的 astrbot 桩。已装真货时不覆盖。"""
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []  # 让它看起来像个包

    api = types.ModuleType("astrbot.api")
    api.__path__ = []

    star = types.ModuleType("astrbot.api.star")

    class Star:
        """AstrBot 插件基类的最小替身。"""

        def __init__(self, context=None):
            self.context = context

    class Context:
        """AstrBot 插件上下文的最小替身。"""

    star.Star = Star
    star.Context = Context

    util = types.ModuleType("astrbot.api.util")
    util.__path__ = []

    # ---- astrbot.api.message_components ---------------------------------------
    #
    # 落地播报（`plugin/arrival.py` 的接线）要在**发出去的那条回复前面**插一行字，
    # 做法是往 `result.chain` 里 insert 一个 `Plain`。真实 AstrBot 里这个类就是
    # `astrbot/api/message_components.py`（`from astrbot.core.message.components import *`）。
    # 桩只提供 `Plain(text)` 这个最小形状 —— 测试要验的是"插进去的是哪句话、
    # 插在第几位、插了几次"，不是"Plain 在真实链条里怎么渲染"。
    message_components = types.ModuleType("astrbot.api.message_components")

    class Plain:  # noqa: D101 - 桩
        def __init__(self, text=""):
            self.text = text

        def __repr__(self):  # pragma: no cover - 调试用
            return f"Plain({self.text!r})"

    message_components.Plain = Plain
    api.message_components = message_components

    # ---- astrbot.api.event / astrbot.api.event.filter -------------------------
    #
    # 聊天指令层（`plugin/command.py` 的薄壳在 `main.py` 里）要的是这几个名字：
    # `AstrMessageEvent`、`MessageEventResult`、`filter`、`PermissionType`、
    # `permission_type`。桩刻意让**所有装饰器都是恒等函数**：
    # 测试关心的是"方法存在且能把事件翻成一句话"，不是"装饰器有没有挂到注册表"——
    # 后者只有真的 AstrBot 能回答，假一个注册表只会测到我自己的想象。
    event = types.ModuleType("astrbot.api.event")
    event.__path__ = []

    event_filter = types.ModuleType("astrbot.api.event.filter")
    event_filter.__path__ = []

    class AstrMessageEvent:  # noqa: D101 - 桩
        pass

    class MessageEventResult:  # noqa: D101 - 桩
        pass

    class PermissionType:  # noqa: D101 - 桩
        ADMIN = "admin"
        MEMBER = "member"
        GROUP_ADMIN = "group_admin"
        SHARED_GROUP_ADMIN = "shared_group_admin"

    def _identity_decorator(*_args, **_kwargs):
        def _wrap(func):
            return func

        return _wrap

    for _name in (
        "command",
        "command_group",
        "permission_type",
        "llm_tool",
        "after_message_sent",
        "event_message_type",
        "on_llm_request",
        "on_llm_response",
        "regex",
        "custom_filter",
        "platform_adapter_type",
        "on_astrbot_loaded",
        "on_agent_begin",
        "on_agent_done",
        "on_decorating_result",
        "on_plugin_error",
        "on_plugin_loaded",
        "on_plugin_unloaded",
        "on_platform_loaded",
        "on_waiting_llm_request",
        "on_using_llm_tool",
        "on_llm_tool_respond",
        "button_action",
    ):
        setattr(event_filter, _name, _identity_decorator)

    event_filter.PermissionType = PermissionType
    event_filter.permission_type = _identity_decorator
    event_filter.AstrMessageEvent = AstrMessageEvent

    event.AstrMessageEvent = AstrMessageEvent
    event.MessageEventResult = MessageEventResult
    event.filter = event_filter

    astrbot.api = api
    api.star = star
    api.util = util
    api.event = event

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.star"] = star
    sys.modules["astrbot.api.util"] = util
    sys.modules["astrbot.api.message_components"] = message_components
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.event.filter"] = event_filter


def _install_quart_stub() -> None:
    """装一个只够 `jsonify` 用的 quart 桩。

    `plugin/api.py` 只从 quart 拿一样东西：`jsonify`。它在真实 AstrBot 里返回
    一个 Quart `Response`（`response.status_code` 可写）。这里给一个**最小但语义正确**
    的替身：`.status_code` 默认 200、`.body` 是序列化后的字符串、`.json` 是原对象。

    为什么不干脆把 `api.py` 改成不依赖 quart：那个文件必须是能被 AstrBot 加载的
    真实插件代码，"用框架提供的 jsonify"是它对运行时的真实要求。把它为了测试而
    改写成返回裸 dict，就等于测了一个不是插件的插件。
    """
    if "quart" in sys.modules:
        return

    import json as _json

    class _Response:
        def __init__(self, payload):
            self.json = payload
            self.status_code = 200
            self.body = _json.dumps(payload, ensure_ascii=False)

        def get_data(self, as_text=True):
            return self.body if as_text else self.body.encode("utf-8")

        def __repr__(self):
            return f"<Response {self.status_code} {self.body[:80]}>"

    quart = types.ModuleType("quart")

    def jsonify(payload=None, **kwargs):
        if payload is None:
            payload = kwargs
        elif kwargs:
            merged = dict(payload)
            merged.update(kwargs)
            payload = merged
        return _Response(payload)

    quart.jsonify = jsonify
    quart.Response = _Response
    sys.modules["quart"] = quart


# 装桩：`astrbot`（插件基类）与 `quart`（jsonify）
_install_astrbot_stub()
_install_quart_stub()

# `plugin/main.py` 与 `plugin/api.py` 之间用的是**相对导入**（`from .api import ...`），
# 因为 AstrBot 会以"目录名 = 包名"的方式加载插件（`astrbot_plugin_molt.main`）。
# 测试里必须复现这个加载方式，否则 `main` 是个顶层模块，`from .api` 直接
# `ImportError: attempted relative import with no known parent package` ——
# 而 `_register_api_if_available` 会把这个异常吞成一条警告，
# 于是"插件加载成功"但一个端点都没注册。**真机上也会是这样**，
# 所以这里不是为了让测试通过而迁就，是为了让测试跑在同一条代码路径上。
#
# 做法：把 `plugin/` 目录注册成名为 `astrbot_plugin_molt` 的包（目录名不同无所谓，
# 包名来自 `metadata.yaml` 的 `name:` 字段，AstrBot 就是这么定的）。
import types  # noqa: E402

_PKG_NAME = "astrbot_plugin_molt"

# 建一个空包壳：`__path__` 指向 plugin 目录，子模块（`main` / `api`）按需 import。
# 不执行 `main.py` —— 插件对象的构造由测试自己控制（需要注入假的 context）。
_pkg = types.ModuleType(_PKG_NAME)
_pkg.__path__ = [str(PLUGIN_DIR)]
sys.modules[_PKG_NAME] = _pkg

# 同时也让 `import main` 这种裸写法可用（个别测试直接这么写，方便对照）。
_plugin_str = str(PLUGIN_DIR)
if _plugin_str not in sys.path:
    sys.path.insert(0, _plugin_str)

# `molt` 包本身（`import molt.control` 这类）
_molt_str = str(MOLT_ROOT)
if _molt_str not in sys.path:
    sys.path.insert(0, _molt_str)


# ---------------------------------------------------------------------------
# 总闸：**任何一条测试都不许真的动这台机器的电源**
#
# 由来（2026-11-15 15:11–15:31 真事故）：`test_molt_agent.py` 里那几条关机/睡眠
# 用例靠 `monkeypatch.setattr(mod, "_suspend_now", ...)` 把真动作换掉，而 `mod`
# 当时是 `tools/molt-agent.py` 那个**垫片** —— 它只是把名字复制进自己的 globals，
# 真正执行的是 `molt.daemon.agent`。补丁打在影子上，`_suspend_now()` 真的被调用：
# 那 20 分钟里这台机器被自己的测试挂起了四次，最后一次以 `Kernel-Power 41` /
# `6008`（非正常关机）收场，鱼跟着断线。
#
# 两条教训：①测试加载器必须指向**真模块**，不是垫片；②光靠"每个用例自己记得打
# 补丁"不够 —— 这里再加一道总闸，谁忘了谁就 AssertionError，而不是让跑测试的
# 人电脑被关掉。
# ---------------------------------------------------------------------------
import subprocess as _subprocess  # noqa: E402

import pytest  # noqa: E402

_POWER_WORDS = ("shutdown", "shutdown.exe", "powercfg", "rundll32", "setsuspendstate")


def _refuse_power(what: str):
    def _boom(*_args, **_kwargs):
        raise AssertionError(
            f"测试里不许真的执行电源动作（{what}）。"
            "要测关机/睡眠请 monkeypatch `_suspend_now` 或注入假 runner —— "
            "打在垫片上不会生效，见本文件末尾的说明。"
        )

    return _boom


@pytest.fixture(scope="session", autouse=True)
def _never_touch_the_real_power():
    """把两台守护进程里的电源入口全部换成"一调用就炸"。"""
    from molt.daemon import agent as _agent_impl
    from molt.daemon import homer as _homer_impl

    patches = []

    def _patch(obj, name, value):
        original = getattr(obj, name)
        setattr(obj, name, value)
        patches.append((obj, name, original))

    if hasattr(_agent_impl, "_suspend_now"):
        _patch(_agent_impl, "_suspend_now", _refuse_power("_suspend_now"))

    class _GuardedSubprocess:
        """只拦电源命令，其它照常放行（有些用例要真跑 python）。"""

        def __getattr__(self, name):
            real = getattr(_subprocess, name)
            if name not in ("run", "call", "check_call", "check_output", "Popen"):
                return real

            def _wrapper(*args, **kwargs):
                argv = args[0] if args else kwargs.get("args")
                text = (
                    " ".join(str(x) for x in argv)
                    if isinstance(argv, (list, tuple))
                    else str(argv)
                )
                lowered = text.lower()
                for word in _POWER_WORDS:
                    if word in lowered:
                        raise AssertionError(
                            f"测试里不许真的执行电源动作（{name}: {text}）。请注入假 runner。"
                        )
                return real(*args, **kwargs)

            return _wrapper

    for module in (_agent_impl, _homer_impl):
        if hasattr(module, "subprocess"):
            _patch(module, "subprocess", _GuardedSubprocess())

    try:
        yield
    finally:
        for obj, name, original in reversed(patches):
            setattr(obj, name, original)
