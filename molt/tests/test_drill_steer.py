"""`tools/drill-steer.py` 的安全边界：默认绝不打断正在跑的回合。

这条测试不联网、不发 steer —— 它只钉住"脚本的安全形状"，
因为那条边界一旦被改松，后果是**打断别人正在等的活**，而且不会有人立刻发现。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "drill-steer.py"


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_the_script_exists() -> None:
    assert SCRIPT.is_file(), f"找不到 {SCRIPT}"


def test_default_picking_refuses_running_sessions() -> None:
    assert "_pick(sessions, allow_running=args.allow_running)" in _source()
    # 默认必须是 False：不给开关就只能在空会话上跑
    assert 'ap.add_argument("--allow-running", action="store_true"' in _source()


def test_interrupting_needs_a_human_typing_yes() -> None:
    text = _source()
    assert "sys.stdin.isatty()" in text, "非交互终端必须直接拒绝"
    assert 'input("> ").strip() == "YES"' in text, "必须要求人手动打 YES"


def test_help_runs_without_touching_anything() -> None:
    got = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert got.returncode == 0, got.stderr
    for flag in ("--peers", "--plan", "--allow-running", "--warden-url"):
        assert flag in got.stdout, f"--help 里应该能看到 {flag}"
