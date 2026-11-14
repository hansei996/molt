"""generation 消费追踪（consumed_by / consumed_at）的测试。

这一层要回答的问题是"目标壳到底有没有真的把快照取走"，而不是
"打算交给谁"。所以测试的重点不在于字段能不能存下来，而在于三种
会让人误判灵魂位置的边界：

  - 重启之后事实还在不在（账本不能只活在这一进程的内存里）；
  - 同一次消费被重试时，时间线会不会被刷新成假的；
  - 两只壳都声称取走同一代时，程序会不会自作主张选一个。

以及一个反向兼容点：旧 state.json 里没有这两个字段，读它不能炸。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from molt.backend import RemoteBackend
from molt.errors import Fatal, NeedsHuman
from molt.server import WardenServer
from molt.warden import PROTOCOL_VERSION, GenerationRecord, Warden

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def make_warden(tmp_path: Path) -> Warden:
    w = Warden(root=tmp_path / "molt")
    w.load()
    return w


def manifest(generation: int = 1) -> dict:
    """最小合法 manifest：服务端只记元数据，这里不涉及真实归档。"""
    return {
        "generation": generation,
        "created_at": time.time(),
        "source_host": "UGREEN-776B",
        "source_root": "/mnt/dm-0/astrbot/data",
        "protocol_version": PROTOCOL_VERSION,
        "astrbot_version": "4.29.4",
        "platform": "linux",
        "python_version": "3.12.16",
        "artifacts": [],
        "runtime_pip": [],
    }


def pushed_warden(tmp_path: Path, shell_id: str = "win-abc") -> Warden:
    """起一个 Warden，并让它账本里有一代可以标记消费的 generation。"""
    w = make_warden(tmp_path)
    lease = w.acquire(shell_id)
    w.accept_snapshot(
        claimed_epoch=lease.epoch,
        claimed_shell_id=shell_id,
        manifest=manifest(1),
    )
    return w


# ---------------------------------------------------------------------------
# Warden：持久化与幂等
# ---------------------------------------------------------------------------


def test_consumed_fields_survive_reload(tmp_path: Path):
    w = pushed_warden(tmp_path)
    before = time.time()
    w.mark_consumed(1, "win-abc")
    after = time.time()

    # 换一个实例读同一个 root：真的落盘了，而不是只改内存。
    reloaded = Warden(root=tmp_path / "molt")
    reloaded.load()
    rec = reloaded.generations[0]
    assert rec.consumed_by == "win-abc"
    assert before <= rec.consumed_at <= after
    assert rec.to_dict()["consumed_at_readable"]


def test_same_shell_mark_is_idempotent_and_keeps_first_time(tmp_path: Path):
    w = pushed_warden(tmp_path)
    first = w.mark_consumed(1, "win-abc")
    first_at = first.consumed_at

    # 间隔一下再标一次，才能证明 consumed_at 没有被刷新。
    time.sleep(0.02)
    second = w.mark_consumed(1, "win-abc")

    assert second.consumed_by == "win-abc"
    assert second.consumed_at == first_at, "重复标记不能产生假的时间线"

    raw = json.loads((tmp_path / "molt" / "state.json").read_text("utf-8"))
    assert raw["generations"][0]["consumed_at"] == first_at


def test_second_shell_marking_same_generation_needs_human(tmp_path: Path):
    w = pushed_warden(tmp_path)
    w.mark_consumed(1, "win-abc")
    first_at = w.generations[0].consumed_at

    with pytest.raises(NeedsHuman):
        w.mark_consumed(1, "win-xyz")

    # 拒绝之后账本必须还是可解析的、且仍然是第一只壳——
    # 不能因为一次被拒的认领把 state.json 写坏或写脏。
    raw = json.loads((tmp_path / "molt" / "state.json").read_text("utf-8"))
    rec = raw["generations"][0]
    assert rec["consumed_by"] == "win-abc"
    assert rec["consumed_at"] == first_at

    reloaded = Warden(root=tmp_path / "molt")
    reloaded.load()
    assert reloaded.generations[0].consumed_by == "win-abc"


def test_unknown_generation_is_fatal_not_silent(tmp_path: Path):
    w = make_warden(tmp_path)
    with pytest.raises(Fatal):
        w.mark_consumed(99, "win-abc")


# ---------------------------------------------------------------------------
# 向后兼容：旧 state.json
# ---------------------------------------------------------------------------


def test_legacy_state_without_consumed_keys_loads(tmp_path: Path):
    root = tmp_path / "molt"
    root.mkdir()
    # 手工构造 v0.1 的账本：generation 条目里根本没有 consumed_by /
    # consumed_at。读了要能继续用，而不是要人删档重建。
    legacy = {
        "protocol_version": PROTOCOL_VERSION,
        "saved_at": time.time(),
        "saved_at_readable": "2025-01-01 00:00:00",
        "saved_by": "UGREEN-776B",
        "fenced_state": {},
        "generations": [
            {
                "generation": 1,
                "created_at": 1700000000.0,
                "source_host": "UGREEN-776B",
                "pushed_by": "nas-abc",
                "target_shell": "win-abc",
                "epoch": 1,
                "artifact_bytes": 0,
                "manifest_sha256": "",
            }
        ],
    }
    (root / "state.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )

    w = Warden(root=root)
    w.load()
    rec = w.generations[0]

    assert rec.consumed_by == ""
    assert rec.consumed_at == 0.0
    assert rec.to_dict()["consumed_at_readable"] == "", (
        "未消费不能显示 1970 年，那看起来像很久以前被取走了"
    )

    # 旧记录也必须能被标记消费（写回时补上新字段）。
    w.mark_consumed(1, "win-abc")
    reloaded = Warden(root=root)
    reloaded.load()
    assert reloaded.generations[0].consumed_by == "win-abc"


# ---------------------------------------------------------------------------
# server 路由（直接 dispatch，不起套接字）
# ---------------------------------------------------------------------------


@pytest.fixture()
def srv(tmp_path: Path) -> WardenServer:
    return WardenServer(root=tmp_path, token="test-token", port=0)


def test_server_consumed_route_marks_generation(srv: WardenServer):
    lease = srv.dispatch("POST", "/acquire", {"shell_id": "win-abc"}).payload["lease"]
    pushed = srv.dispatch(
        "POST",
        "/push",
        {"shell_id": "win-abc", "epoch": lease["epoch"], "manifest": manifest(1)},
    )
    assert pushed.status == 200

    r = srv.dispatch(
        "POST", "/generations/1/consumed", {"shell_id": "win-abc"}
    )
    assert r.status == 200
    gen = r.payload["generation"]
    assert gen["generation"] == 1
    assert gen["consumed_by"] == "win-abc"
    assert gen["consumed_at"] > 0
    assert gen["consumed_at_readable"]


def test_server_consumed_route_requires_shell_id(srv: WardenServer):
    r = srv.dispatch("POST", "/generations/1/consumed", {})
    assert r.status == 400
    assert r.payload["kind"] == "bad_request"


def test_server_consumed_route_rejects_non_numeric_generation(srv: WardenServer):
    r = srv.dispatch("POST", "/generations/abc/consumed", {"shell_id": "win-abc"})
    assert r.status == 400
    assert r.payload["kind"] == "bad_request"


def test_server_consumed_route_unknown_generation_is_500(srv: WardenServer):
    # 账本里没有这一代属于"两边事实不一致"，归类成 fatal 而不是 4xx：
    # 调用方不该把它当成参数错误重试。
    r = srv.dispatch("POST", "/generations/9/consumed", {"shell_id": "win-abc"})
    assert r.status == 500
    assert r.payload["kind"] == "fatal"


# ---------------------------------------------------------------------------
# RemoteBackend：转发
# ---------------------------------------------------------------------------


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def mark_consumed(self, generation: int, shell_id: str) -> dict:
        self.calls.append((generation, shell_id))
        return {"generation": generation, "consumed_by": shell_id}


def test_remote_backend_forwards_mark_consumed():
    backend = object.__new__(RemoteBackend)
    stub = _StubClient()
    backend.client = stub  # type: ignore[attr-defined]

    result = backend.mark_consumed(7, "win-xyz")

    assert stub.calls == [(7, "win-xyz")]
    assert result == {"generation": 7, "consumed_by": "win-xyz"}


def test_generation_record_from_dict_defaults_consumed_fields():
    rec = GenerationRecord.from_dict(
        {
            "generation": 3,
            "created_at": 1700000000.0,
            "source_host": "nas",
            "pushed_by": "nas-abc",
        }
    )
    assert rec.consumed_by == ""
    assert rec.consumed_at == 0.0
