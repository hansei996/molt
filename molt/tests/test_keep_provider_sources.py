"""`--keep-provider-source`：那台机器上真的跑着这个服务时，别删它。

2026-11-15 真机前提：`workbuddy` 的 `api_base` 是 `http://127.0.0.1:17863/v1`，
看起来像"只在自己那台机器上才有"。但那个网关是 `astrbot_plugin_workbuddy`
在 `initialize()` 里起的本地服务 —— 两台机器都装了这个插件，所以回环地址
在 Windows 上**也是有东西在听的**。默认删掉它是保守，显式点名才保留。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from molt import overlay  # noqa: E402


def _config() -> dict:
    return {
        "provider_sources": [
            {"id": "deepseek", "type": "openai_chat_completion",
             "api_base": "https://api.deepseek.com/v1"},
            {"id": "workbuddy", "type": "openai_chat_completion",
             "api_base": "http://127.0.0.1:17863/v1"},
        ],
        "provider": [
            {"id": "deepseek/deepseek-flash", "provider_source_id": "deepseek"},
            {"id": "workbuddy/deepseek-v4.1-flash", "provider_source_id": "workbuddy"},
        ],
        "platform": [{"id": "qq", "enable": True}],
        "dashboard": {"host": "0.0.0.0", "port": 6185, "jwt_secret": "x" * 40},
    }


def test_by_default_the_loopback_provider_is_dropped():
    patched, notes = overlay.overlay_for_windows(_config(), shell_id="win", port=6199)
    assert [s["id"] for s in patched["provider_sources"]] == ["deepseek"]
    assert [p["id"] for p in patched["provider"]] == ["deepseek/deepseek-flash"]
    assert any("workbuddy" in c for c in notes["changes"])


def test_naming_it_keeps_both_the_source_and_its_provider():
    patched, _ = overlay.overlay_for_windows(
        _config(), shell_id="win", port=6199, keep_provider_sources=["workbuddy"]
    )
    assert [s["id"] for s in patched["provider_sources"]] == ["deepseek", "workbuddy"]
    assert [p["id"] for p in patched["provider"] if p.get("provider_source_id") == "workbuddy"] == [
        "workbuddy/deepseek-v4.1-flash"
    ]


def test_keeping_something_else_changes_nothing():
    patched, _ = overlay.overlay_for_windows(
        _config(), shell_id="win", port=6199, keep_provider_sources=["not-a-source", "  "]
    )
    assert [s["id"] for s in patched["provider_sources"]] == ["deepseek"]


def test_an_explicit_drop_list_still_wins():
    """两个参数同时在时，`drop_provider_sources` 说了算 —— 它更具体。"""
    patched, _ = overlay.overlay_for_windows(
        _config(), shell_id="win", port=6199,
        drop_provider_sources=["workbuddy"], keep_provider_sources=["workbuddy"],
    )
    assert [s["id"] for s in patched["provider_sources"]] == ["deepseek"]


def test_the_platform_guard_is_still_unconditional():
    """保留 provider 不等于放开平台适配器 —— 这两件事必须互不影响。"""
    patched, _ = overlay.overlay_for_windows(
        _config(), shell_id="win", port=6199, keep_provider_sources=["workbuddy"]
    )
    assert patched["platform"][0]["enable"] is False


def test_the_land_command_exposes_the_flag():
    from molt import cli

    parser = cli.build_parser()
    args = parser.parse_args([
        "land", "--generation", "3", "--data", "x",
        "--keep-provider-source", "workbuddy",
        "--keep-provider-source", "other",
    ])
    assert args.keep_provider_source == ["workbuddy", "other"]


def test_the_agent_forwards_it_to_land(tmp_path):
    from molt.daemon import agent as agent_impl

    settings = agent_impl.Settings(
        shell="win", data_dir="G:/data", warden_url="http://w", warden_token="t",
        python="py", keep_provider_sources=["workbuddy"],
    )
    agent = agent_impl.Agent(settings, tmp_path / "agent-state.json")
    argv = agent.land_argv(7)
    assert "--keep-provider-source" in argv
    assert "workbuddy" in argv
