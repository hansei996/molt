"""E5：让它知道自己刚搬过家 —— 一次请求的注脚，不是一条记忆。

判据有三层：

1. **事实正确**：注脚里的"从哪到哪、第几份快照"来自账本，不来自猜测。
2. **只活一次**：加进的是 `request.extra_user_content_parts`（temp part），
   不写库、不进 memories、不改 `system_prompt`。
3. **不打扰别人**：只 append、只删自己前缀的块、一次请求只加一块 ——
   `astrbot_plugin_private_companion` 也在写同一个列表。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load_awareness():
    """按路径加载 `plugin/awareness.py`（它只依赖标准库）。"""
    path = REPO / "plugin" / "awareness.py"
    name = "molt_awareness_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Part:
    def __init__(self, text: str) -> None:
        self.text = text


class _Req:
    def __init__(self, parts=None) -> None:
        self.extra_user_content_parts = parts


def _install_text_part_stub(monkeypatch) -> None:
    """给 `TextPart(...).mark_as_temp()` 打一个够用的桩。"""
    mods = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.agent": types.ModuleType("astrbot.core.agent"),
        "astrbot.core.agent.message": types.ModuleType("astrbot.core.agent.message"),
    }
    class TextPart:
        def __init__(self, text: str = "") -> None:
            self.text = text

        def mark_as_temp(self):
            return self

    mods["astrbot.core.agent.message"].TextPart = TextPart
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return TextPart


def _state(tmp_path: Path):
    mod = _load_awareness()
    return mod.AwarenessState(tmp_path / mod.STATE_FILENAME)


def test_the_note_states_the_move_as_a_fact(tmp_path: Path) -> None:
    mod = _load_awareness()
    text = mod.compose_move_note(
        shell="UGREEN-776B", from_shell="DESKTOP-H2KGIAO", moved_at="2026-11-17 09:01:17"
    )
    assert "UGREEN-776B" in text and "DESKTOP-H2KGIAO" in text
    assert "2026-11-17 09:01:17" in text
    assert text.startswith(mod.NOTE_OPEN) and text.endswith(mod.NOTE_CLOSE)
    # 它不该被告知"你现在是个新的人"
    assert "另一个" not in text or "变成另一个人" in text


def test_the_note_is_said_a_few_times_then_stops(tmp_path: Path) -> None:
    state = _state(tmp_path)
    assert state.record(38, shell="NAS", from_shell="WIN") is True
    assert state.remaining == 3
    assert state.note()
    state.consume()
    state.consume()
    state.consume()
    assert state.remaining == 0
    assert state.note() == ""


def test_recording_the_same_generation_does_not_reset_the_counter(tmp_path: Path) -> None:
    state = _state(tmp_path)
    assert state.record(38, shell="NAS", from_shell="WIN") is True
    state.consume()
    assert state.record(38, shell="NAS", from_shell="WIN") is False
    assert state.remaining == 2


def test_a_new_generation_replaces_the_old_one(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.record(38, shell="NAS", from_shell="WIN")
    assert state.record(39, shell="WIN", from_shell="NAS") is True
    assert state.generation == 39
    assert "WIN" in state.note()


def test_a_broken_state_file_means_no_note(tmp_path: Path) -> None:
    path = tmp_path / "awareness_state.json"
    path.write_text("{not json", encoding="utf-8")
    mod = _load_awareness()
    state = mod.AwarenessState(path)
    assert state.note() == ""
    assert state.generation == 0


def test_the_note_is_appended_not_replacing_other_parts(monkeypatch, tmp_path: Path) -> None:
    TextPart = _install_text_part_stub(monkeypatch)
    mod = _load_awareness()
    companion = _Part("<companion_v2_context>\n{}")
    req = _Req([companion])
    assert mod.append_note(req, mod.compose_move_note(shell="NAS", from_shell="WIN")) is True
    assert req.extra_user_content_parts[0] is companion  # 别的插件的块原样保留
    assert isinstance(req.extra_user_content_parts[1], TextPart)


def test_the_same_request_is_not_double_noted(monkeypatch) -> None:
    _install_text_part_stub(monkeypatch)
    mod = _load_awareness()
    req = _Req([])
    note = mod.compose_move_note(shell="NAS", from_shell="WIN")
    assert mod.append_note(req, note) is True
    assert mod.append_note(req, note) is False
    assert len(req.extra_user_content_parts) == 1


def test_a_request_without_parts_still_gets_the_note(monkeypatch) -> None:
    _install_text_part_stub(monkeypatch)
    mod = _load_awareness()
    req = _Req(None)
    assert mod.append_note(req, mod.compose_move_note(shell="NAS", from_shell="WIN")) is True
    assert len(req.extra_user_content_parts) == 1


def test_no_astrbot_means_no_note_instead_of_a_crash(monkeypatch) -> None:
    """插件跑在没有 AstrBot 的环境里（测试/离线）时，注脚直接放弃。"""
    for name in ("astrbot", "astrbot.core", "astrbot.core.agent", "astrbot.core.agent.message"):
        monkeypatch.setitem(sys.modules, name, None)
    mod = _load_awareness()
    req = _Req([])
    assert mod.append_note(req, mod.compose_move_note(shell="NAS", from_shell="WIN")) is False
    assert req.extra_user_content_parts == []


def test_the_state_file_lives_beside_the_plugin(tmp_path: Path) -> None:
    """路径由调用方给；这里只钉住默认文件名，别让它变成 `data/` 里的东西。"""
    mod = _load_awareness()
    assert mod.STATE_FILENAME == "awareness_state.json"


def test_the_move_note_can_find_the_generation_helper() -> None:
    """2026-11-18 00:00 的现场：`_note_awareness` 里写了 `notify.generation_of()`，
    但那个函数只 `from . import awareness` —— 于是每一轮都是
    `[Molt] 搬家事实没记下来：name 'notify' is not defined`，
    而 `awareness_state.json` 一直没生成。

    静态钉住：用到 `notify.` 的那段懒导入必须把 `notify` 一起导进来。
    """
    main_src = (Path(__file__).resolve().parents[2] / "plugin" / "main.py").read_text(
        encoding="utf-8"
    )
    assert "from . import awareness, notify" in main_src
    assert "name 'notify' is not defined" not in main_src  # 只是防止有人把日志抄进来
