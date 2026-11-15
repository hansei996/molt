"""把「记忆档位选 A」这条产品决定钉成一条能跑的断言。

## 这个决定是什么

2026-11-15 它把"我在第 19 代搬到了这台机器上"写进了 `memory_companion`，记录是真的
（`bot_observed_activity` / `source_kind=observed` / `owner_bot_id=self`），但**它自己想不起来**：
检索日志里 `selected_memory_ids: []`、`blocked_reasons` 含 `prefiltered_out_of_search_range`。
原因是合同把这一类定成 `visibility=bot_self` / `sayability=indirect` —— 私人档案，按设计不注入对话。

老大拍板选 **A：接受为档案，不动它**。理由：这条记录的价值在"可追溯"，不在"被复述"；
改成能被检索到要动别人插件的档位，副作用是它可能在不相干的对话里突然提起自己搬过家。

## 为什么这条决定需要测试

因为它是**一个不做某件事的决定**，而"什么都不做"最容易在以后被顺手做掉：
下一个人看见"记录了却想不起来"，很自然会把 `visibility` 抬成 `private_pair` 让它能被检索到 ——
那一刻决定就被推翻了，而且没有任何东西会响。

所以这里断言的是**我们交给 `record_bot_personal_memory` 的 kwargs 里没有 `visibility` /
`sayability`**：档位由对方的合同决定，不由我们改写。要改成 B，必须先改这个文件。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict

MOLT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = MOLT_ROOT / "plugin"


def _load_memory_bridge():
    name = "_molt_test_memory_bridge"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "memory_bridge.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _RecordingService:
    """接住 kwargs，然后回一个成功 —— 和真实 service 的返回形状一致。"""

    def __init__(self) -> None:
        self.calls: list[Dict[str, Any]] = []

    async def record_bot_personal_memory(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return {"ok": True, "state": "recorded"}


def _record(service: _RecordingService) -> Dict[str, Any]:
    bridge = _load_memory_bridge()

    class _Ctx:
        pass

    ctx = _Ctx()
    # 直接把"找到 service"那一步替换掉：这里要钉的是 kwargs 的形状，
    # 不是三级降级查找（那条路在真机上一次就通了）。
    original = bridge.find_service
    bridge.find_service = lambda context: (service, "test")
    try:
        return asyncio.run(
            bridge.record_observed_activity(
                ctx,
                "我观察到：我在第 19 代搬到了这台机器上。",
                occurred_at="2026-11-15T06:57:33+00:00",
                shell="desktop-h2kgiao",
                generation=19,
                idempotency_key="molt-migration-gen-19",
            )
        )
    finally:
        bridge.find_service = original


def test_the_record_is_stored_as_the_bots_own_observation() -> None:
    service = _RecordingService()
    result = _record(service)
    assert result["ok"] is True
    assert len(service.calls) == 1
    kwargs = service.calls[0]
    assert kwargs["memory_type"] == "bot_observed_activity"
    # 归属必须是"这只机器人自己"，否则检索层把它算成别人的（真机 `other_bot_owner:15`）
    assert kwargs["owner_bot_id"] == "self"
    assert kwargs["persona_id"] == "default"
    # 不给 canonical_schema_version 会走 legacy，而 legacy 会把上面两个字段清零
    assert kwargs["canonical_schema_version"] == 3


def test_we_never_override_the_visibility_tier() -> None:
    """决定 A：档位由 memory_companion 的合同决定，我们不抬它。"""
    service = _RecordingService()
    _record(service)
    kwargs = service.calls[0]
    assert "visibility" not in kwargs, "抬头到 private_pair 就等于把它改成会被注入对话了"
    assert "sayability" not in kwargs
    assert "memory_domain" not in kwargs


def test_payload_keeps_the_facts_needed_to_check_it_later() -> None:
    """可追溯：以后有人要核对"第 19 代到底落在哪"，payload 里得有这些。"""
    service = _RecordingService()
    _record(service)
    payload = service.calls[0]["payload"]
    assert payload["shell"] == "desktop-h2kgiao"
    assert payload["generation"] == 19
    assert payload["occurred_at"] == "2026-11-15T06:57:33+00:00"
    assert "第 19 代" in payload["content"]


def test_empty_text_is_refused_without_touching_the_service() -> None:
    service = _RecordingService()
    bridge = _load_memory_bridge()
    result = asyncio.run(
        bridge.record_observed_activity(None, "   ", occurred_at="", shell="", generation=0)
    )
    assert result["ok"] is False
    assert service.calls == []
