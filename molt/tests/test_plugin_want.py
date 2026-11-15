"""插件里的那个工具本身：`molt_propose_go_home` —— 它能被模型调用，但它只说话。

## 为什么这条要单独测

`command.MoltMover.request_move` 能写提议，不代表**模型真的能碰到它**：
AstrBot 的 `@filter.llm_tool` 在注册时会解析函数 docstring 的参数类型
（`star_handler.py:610-694`），写错一个类型名会在**插件加载时**抛异常，
而那种异常的表现是"这个插件的工具不见了"，不是"报错"。

所以这里直接调用那个方法，钉三件事：

1. 没有 mover 时不抛、给一句人话（会话不该因为插件没配好就断）；
2. `llm_tool_enabled: false`（默认）时它明确拒绝，并且说清这不是故障；
3. 开着的时候，它把 `request_move` 的那句话原样交出去 ——
   **工具只负责"说"，执行仍然是 `/molt confirm`**。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from astrbot_plugin_molt import command as cmd


class _Event:
    """工具只需要一个 event 占位；这里刻意什么都不给它。"""

    def get_sender_id(self) -> str:
        return "hansei"


class _FakeMover:
    def __init__(self, cfg: "cmd.Settings", *, text: str = "我想搬去 nas-shell。"):
        self._cfg = cfg
        self._text = text
        self.calls: List[Dict[str, Any]] = []
        self._has = False

    def settings(self) -> "cmd.Settings":
        return self._cfg

    def request_move(self, cfg, *, target="", reason=""):
        self.calls.append({"target": target, "reason": reason})
        self._has = True
        return cmd.Reply(self._text)

    def has_proposal(self) -> bool:
        return self._has


def _plugin(mover: Optional[_FakeMover]):
    from astrbot_plugin_molt.main import MoltControlPlugin

    plugin = MoltControlPlugin.__new__(MoltControlPlugin)
    plugin._mover = mover
    return plugin


def _call(plugin, *, target: str = "", reason: str = "") -> str:
    return asyncio.run(plugin.molt_propose_go_home(_Event(), target, reason))


def _settings(tmp_path: Path, **overrides) -> "cmd.Settings":
    cfg = cmd.Settings(
        enabled=True,
        llm_tool_enabled=True,
        shell="win-shell",
        target="nas-shell",
        data_dir="/AstrBot/data",
        molt_pkg=str(tmp_path),
        warden_url="http://warden:6186",
        warden_token="tok",
        control_config="/AstrBot/data/cmd_config.json",
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def test_no_mover_means_a_sentence_not_a_traceback(tmp_path: Path) -> None:
    text = _call(_plugin(None))
    assert "没有迁徙配置" in text


def test_the_tool_is_closed_until_the_config_says_otherwise(tmp_path: Path) -> None:
    mover = _FakeMover(_settings(tmp_path, llm_tool_enabled=False))
    text = _call(_plugin(mover), target="nas-shell", reason="想回去")
    assert "llm_tool_enabled" in text
    assert mover.calls == []  # 一个字都没往下走


def test_when_open_it_hands_the_request_over_verbatim(tmp_path: Path) -> None:
    mover = _FakeMover(_settings(tmp_path))
    text = _call(_plugin(mover), target="nas-shell", reason="这边要关机了")
    assert text == "我想搬去 nas-shell。"
    assert mover.calls == [{"target": "nas-shell", "reason": "这边要关机了"}]


def test_a_broken_config_does_not_break_the_conversation(tmp_path: Path) -> None:
    class _Boom(_FakeMover):
        def settings(self):
            raise cmd.ConfigError("molt_command.json 不是合法的 UTF-8 JSON")

    text = _call(_plugin(_Boom(_settings(tmp_path))))
    assert "读不出来" in text
    assert "没有动任何东西" in text


def test_the_tool_registration_survives_astrbot_docstring_parsing(tmp_path: Path) -> None:
    """真的把那个方法的 docstring 按 AstrBot 的规矩核一遍。

    `@filter.llm_tool` 在 import 时就跑过了（conftest 里是空装饰器，所以解析没发生），
    而它在真机上会做两件会**抛异常**的事（`star_handler.py:610-694`）：
    参数必须在 docstring 的 `Args:` 里带类型（否则「缺少类型注释」），
    类型必须是 string/number/object/array/boolean 之一（否则「不支持的参数类型」）。
    这两种错的表现都是"这个插件的工具悄悄不见了"，不是一条报错。

    这里不依赖 `docstring_parser`（那是 AstrBot 的依赖，不是我们的），
    只按它的规则核对那两件事 —— 规则很窄，窄到可以直接断言。
    """
    import inspect
    import re

    from astrbot_plugin_molt.main import MoltControlPlugin

    func = MoltControlPlugin.molt_propose_go_home
    doc = inspect.getdoc(func) or ""

    # 1) 函数签名的参数（去掉 self / event）就是工具参数，顺序也要一致
    params = [
        name for name in inspect.signature(func).parameters if name not in ("self", "event")
    ]
    assert params == ["target", "reason"]

    # 2) 每个参数在 `Args:` 段里都有一行 `名字(类型): 说明`
    #    `inspect.getdoc` 会去掉公共缩进，所以这里只看"Args: 之后"那一段，不依赖缩进宽度。
    assert "Args:" in doc
    args_section = doc.split("Args:", 1)[1]
    declared = dict(
        re.findall(r"^\s*(\w+)\((\w+)\):\s*\S", args_section, flags=re.MULTILINE)
    )
    assert set(declared) == set(params), f"docstring 里声明的参数是 {sorted(declared)}"
    supported = {"string", "number", "object", "array", "boolean"}
    for name, type_name in declared.items():
        assert type_name in supported, f"{name} 的类型 {type_name!r} 不在 AstrBot 支持的类型里"

    # 3) 描述不能是空的 —— 空的工具描述等于让模型盲猜什么时候该用它
    assert doc.strip()
    assert not doc.strip().startswith("Args:")

