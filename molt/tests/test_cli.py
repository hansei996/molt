"""CLI 的测试。

重点不是"命令能跑"，而是**它在别人的机器上不会崩**。

这个文件的存在理由，是三个真实的崩溃：

  1. 中文 Windows 的控制台是 GBK。`print("✓")` 直接
     `UnicodeEncodeError: 'gbk' codec can't encode character '\\u2713'`。
     一个中文用户第一次运行就看见 traceback。

  2. `--no-color` 写在子命令后面被 argparse 拒。
     人会写成 `molt doctor --no-color`，不会写成 `molt --no-color doctor`。

  3. `Warden.DEFAULT_ROOT` 写死 `/mnt/dm-0/astrbot/_molt`，
     在 Windows 上变成 `\\mnt\\dm-0\\astrbot\\_molt`——盘符根下的
     不存在、也不该被创建的路径。

三个都是"我这边好好的"，只有换台机器才暴露。所以要有测试。
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from molt import cli
from molt.cli import C, _setup_console, build_parser, main


# ---------------------------------------------------------------------------
# 编码降级
# ---------------------------------------------------------------------------


class _FakeStream(io.TextIOWrapper):
    """一个编码能力受限的假 stdout，用来模拟 GBK 控制台。"""

    def __init__(self, encoding: str):
        self._buf = io.BytesIO()
        super().__init__(self._buf, encoding=encoding, errors="strict")

    def reconfigure(self, **kwargs):  # type: ignore[override]
        # 模拟"切不动"：老终端不接受 reconfigure
        raise OSError("cannot reconfigure")


def test_ascii_mode_replaces_all_symbols():
    """降级之后，任何一个符号都必须能编成 ASCII。"""
    original = {k: getattr(C, k) for k in
                ("S_OK", "S_BAD", "S_WARN", "S_INFO", "S_LINE", "S_BULLET")}
    try:
        C.ascii_mode()
        for attr in original:
            value = getattr(C, attr)
            assert value, f"{attr} 不能为空"
            # 关键断言：能编成 ASCII
            value.encode("ascii")
    finally:
        for k, v in original.items():
            setattr(C, k, v)


def test_setup_console_falls_back_when_gbk(monkeypatch):
    """GBK 控制台上必须自动降级成 ASCII 符号，而不是让调用方崩。"""
    fake = _FakeStream("gbk")
    original = {k: getattr(C, k) for k in
                ("S_OK", "S_BAD", "S_INFO", "S_LINE", "S_BULLET")}
    monkeypatch.setattr(sys, "stdout", fake)
    monkeypatch.setattr(sys, "stderr", fake)
    try:
        _setup_console()
        # 降级后这些符号必须能编进 gbk
        for attr in ("S_OK", "S_BAD", "S_INFO", "S_LINE", "S_BULLET"):
            getattr(C, attr).encode("gbk")
    finally:
        for k, v in original.items():
            setattr(C, k, v)


def test_setup_console_keeps_unicode_when_utf8(monkeypatch):
    """UTF-8 控制台上不应该无谓降级——好看也是有价值的。"""
    fake = _FakeStream("utf-8")
    original = C.S_OK
    monkeypatch.setattr(sys, "stdout", fake)
    monkeypatch.setattr(sys, "stderr", fake)
    try:
        _setup_console()
        assert C.S_OK == "✓"
    finally:
        C.S_OK = original


def _strip_ansi(s: str) -> str:
    import re

    return re.sub(r"\033\[[0-9;]*m", "", s)


def test_output_helpers_survive_ascii_mode():
    """所有输出辅助函数在 ASCII 模式下都必须能跑通且不崩。"""
    original = {k: getattr(C, k) for k in
                ("S_OK", "S_BAD", "S_WARN", "S_INFO", "S_LINE", "S_BULLET")}
    buf = io.StringIO()
    saved = sys.stdout
    try:
        C.ascii_mode()
        sys.stdout = buf
        cli.ok("ok")
        cli.bad("bad")
        cli.warn("warn")
        cli.info("info")
        cli.dim("dim")
        cli.head("标题")
        cli.kv("k", "v")
    finally:
        sys.stdout = saved
        for k, v in original.items():
            setattr(C, k, v)
    out = _strip_ansi(buf.getvalue())
    assert "[OK] ok" in out
    assert "[X] bad" in out
    assert "[!] warn" in out
    assert "- info" in out


# ---------------------------------------------------------------------------
# argparse 行为
# ---------------------------------------------------------------------------


def test_no_color_works_before_subcommand():
    p = build_parser()
    args = p.parse_args(["--no-color", "doctor"])
    assert args.no_color is True


def test_no_color_works_after_subcommand():
    """人行会写成 `molt doctor --no-color`。这曾经直接报错。"""
    p = build_parser()
    args = p.parse_args(["doctor", "--no-color"])
    assert args.no_color is True


def test_no_color_absent_is_falsy_not_missing():
    """`default=argparse.SUPPRESS` 会让属性消失，main() 里 getattr 兜住即可。

    但这里断言的是不崩——属性在或不在都行。
    """
    p = build_parser()
    args = p.parse_args(["doctor"])
    assert getattr(args, "no_color", False) is False


def test_all_subcommands_have_func():
    p = build_parser()
    for argv in (
        ["shell"], ["status"],
        ["checkout", "--data", "/tmp/x", "--shell", "s"],
        ["verify", "--dir", "/tmp/x"],
        ["push", "--dir", "/tmp/x", "--shell", "s", "--epoch", "1"],
        ["release", "--shell", "s", "--epoch", "1"],
        ["doctor"],
    ):
        args = p.parse_args(argv)
        assert callable(getattr(args, "func", None)), f"{argv} 没有 func"


def test_no_subcommand_prints_help(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "molt" in out


# ---------------------------------------------------------------------------
# 平台相关：根目录
# ---------------------------------------------------------------------------


def test_default_root_is_not_a_windows_absolute_nonsense(monkeypatch):
    """默认根目录在 Windows 上不能是 `\\mnt\\dm-0\\...`。"""
    from molt import warden

    monkeypatch.delenv("MOLT_ROOT", raising=False)
    root = warden.default_root()

    if os.name == "nt":
        # 必须是带盘符的绝对路径，且不能落在盘符根下的 mnt 目录
        assert root.is_absolute()
        assert root.drive, f"Windows 上没有盘符：{root}"
        assert "\\mnt\\dm-0" not in str(root)
    else:
        assert str(root).startswith("/")


def test_molt_root_env_wins(monkeypatch, tmp_path):
    from molt import warden

    monkeypatch.setenv("MOLT_ROOT", str(tmp_path))
    assert warden.default_root() == tmp_path


# ---------------------------------------------------------------------------
# 端到端：CLI 真的能驱动 Warden
# ---------------------------------------------------------------------------


def test_status_on_empty_root(tmp_path, capsys):
    rc = main(["status", "--root", str(tmp_path), "--no-color"])
    assert rc == 0
    out = capsys.readouterr().out
    # 状态未知但要说清楚，不能静默
    assert "epoch" in out.lower() or "generation" in out.lower()


def test_doctor_runs_without_crash(tmp_path, capsys):
    rc = main(["doctor", "--root", str(tmp_path), "--no-color"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "molt" in out.lower()


def test_status_json_is_valid_json(tmp_path, capsys):
    main(["status", "--root", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    # 可能混有非 JSON 前导，取最后一段能 parse 的
    payload = json.loads(out.strip())
    assert isinstance(payload, dict)
