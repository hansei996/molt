"""`backend.py` 的测试。

这一层的存在理由只有一个：**让 CLI 不必知道自己在跟本地还是远端说话**。
所以这里测的核心不是"每个方法能跑通"，而是**两个后端在同样的操作序列下
必须给出同样的判定**。如果哪天本地能通过、远端被拒，那说明有一边的
fencing 是假的。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from molt.backend import LocalBackend, RemoteBackend, open_backend
from molt.errors import EpochStale, Fatal, LeaseHeld, MoltError
from molt.server import WardenServer
from molt.version import PROTOCOL_VERSION


def _manifest(generation: int = 1) -> dict:
    return {
        "generation": generation,
        "created_at": 1700000000.0,
        "source_host": "UGREEN-776B",
        "source_root": "/mnt/dm-0/astrbot",
        "protocol_version": PROTOCOL_VERSION,
        "astrbot_version": "config-5",
        "platform": "linux",
        "python_version": "3.10.7",
        "artifacts": [],
        "runtime_pip": [],
    }


@pytest.fixture
def live_backend(tmp_path: Path):
    srv = WardenServer(root=tmp_path / "warden", token="tok", port=0)
    host, port = srv.start()
    be = RemoteBackend(
        f"http://127.0.0.1:{port}", "tok", staging=tmp_path / "staging"
    )
    yield be, tmp_path
    srv.stop()


# ---------------------------------------------------------------------------
# open_backend：选错要报错，不能猜
# ---------------------------------------------------------------------------


def test_open_backend_local_by_default(tmp_path: Path) -> None:
    be = open_backend(root=str(tmp_path / "w"))
    assert isinstance(be, LocalBackend)
    assert be.kind == "local"


def test_open_backend_remote_when_url_given(tmp_path: Path) -> None:
    be = open_backend(url="http://127.0.0.1:6186", token="t")
    assert isinstance(be, RemoteBackend)
    assert be.kind == "remote"


def test_open_backend_rejects_both_local_and_remote(tmp_path: Path) -> None:
    """"同时给两个"必须报错，不能挑一个用。

    含糊地猜会让"我以为我在跟 NAS 说话，其实在写本地文件"变成可能，
    而这类错误在分布式系统里是最难发现的——因为每一步都"成功了"。
    """
    with pytest.raises(ValueError) as ei:
        open_backend(root=str(tmp_path), url="http://x:1", token="t")
    assert "无法判断" in str(ei.value)


def test_open_backend_remote_without_token_is_refused() -> None:
    with pytest.raises(ValueError) as ei:
        open_backend(url="http://x:1")
    assert "token" in str(ei.value).lower()


# ---------------------------------------------------------------------------
# 两个后端的判定必须一致
# ---------------------------------------------------------------------------


def test_local_acquire_returns_epoch_int(tmp_path: Path) -> None:
    be = LocalBackend(root=tmp_path / "w")
    epoch = be.acquire("win-a")
    assert isinstance(epoch, int)
    assert epoch == 1


def test_remote_acquire_returns_epoch_int(live_backend) -> None:
    be, _ = live_backend
    epoch = be.acquire("win-a")
    assert isinstance(epoch, int), "CLI 只想要 epoch，不该拿到 dict"
    assert epoch == 1


def test_both_backends_reject_second_holder(tmp_path: Path, live_backend) -> None:
    local = LocalBackend(root=tmp_path / "w-local")
    remote, _ = live_backend

    for be in (local, remote):
        be.acquire("win-a")
        with pytest.raises(LeaseHeld):
            be.acquire("win-b")


def test_both_backends_reject_stale_epoch(tmp_path: Path, live_backend) -> None:
    local = LocalBackend(root=tmp_path / "w-local")
    remote, _ = live_backend

    for be in (local, remote):
        epoch = be.acquire("win-a")
        with pytest.raises(EpochStale):
            be.push("win-a", epoch - 1, _manifest(1))


def test_both_backends_record_generation(tmp_path: Path, live_backend) -> None:
    local = LocalBackend(root=tmp_path / "w-local")
    remote, _ = live_backend

    for be in (local, remote):
        if be.kind == "local":
            be = LocalBackend(root=tmp_path / f"w-{id(be)}")
        epoch = be.acquire("win-a")
        rec = be.push("win-a", epoch, _manifest(1))
        assert rec["generation"] == 1
        assert be.next_generation() == 2


def test_both_backends_agree_on_next_generation(
    tmp_path: Path, live_backend
) -> None:
    local = LocalBackend(root=tmp_path / "w-local")
    remote, _ = live_backend
    assert local.next_generation() == remote.next_generation() == 1


def test_both_backends_release_allows_takeover(
    tmp_path: Path, live_backend
) -> None:
    local = LocalBackend(root=tmp_path / "w-local")
    remote, _ = live_backend

    for be in (local, remote):
        e1 = be.acquire("win-a")
        be.release("win-a", e1)
        e2 = be.acquire("win-b")
        assert e2 == e1 + 1, "交还后新的壳应该拿到更大的 epoch"


# ---------------------------------------------------------------------------
# staging 目录：远端模式下快照归属谁
# ---------------------------------------------------------------------------


def test_remote_staging_is_on_local_disk(live_backend) -> None:
    """远端模式的 staging 必须落在**壳自己**的盘上。

    壳写不了 NAS 的路径，所以"快照在哪"这个问题在远端模式下
    答案只能是本地。这不是妥协，而是边界：快照在传过去之前属于做备份的机器。
    """
    be, tmp = live_backend
    p = be.prepare_generation_dir(1)
    assert p.exists()
    assert p.name == "gen-000001"
    assert str(p).startswith(str(tmp / "staging"))


def test_remote_staging_dir_is_idempotent(live_backend) -> None:
    be, _ = live_backend
    a = be.prepare_generation_dir(3)
    b = be.prepare_generation_dir(3)
    assert a == b
    assert a.exists()


def test_local_staging_lives_under_warden_root(tmp_path: Path) -> None:
    be = LocalBackend(root=tmp_path / "w")
    p = be.prepare_generation_dir(1)
    assert p.parent.parent == tmp_path / "w", (
        "本地模式下 staging 必须和 state.json 同一个文件系统"
    )


# ---------------------------------------------------------------------------
# where() 必须说清自己在跟谁说话
# ---------------------------------------------------------------------------


def test_where_tells_you_who_it_is_talking_to(tmp_path: Path, live_backend) -> None:
    local = LocalBackend(root=tmp_path / "w")
    remote, _ = live_backend
    assert "本地" in local.where()
    assert "远端" in remote.where()
    assert "127.0.0.1" in remote.where()


# ---------------------------------------------------------------------------
# status 结构在两个后端上必须一致（CLI 直接用它渲染）
# ---------------------------------------------------------------------------


def test_status_shape_matches_across_backends(
    tmp_path: Path, live_backend
) -> None:
    local = LocalBackend(root=tmp_path / "w-local")
    remote, _ = live_backend

    local_s = local.status()
    remote_s = remote.status()

    key_fields = {
        "current_epoch",
        "last_accepted_epoch",
        "rejected_write_count",
        "lease",
        "generation_count",
        "latest_generation",
    }
    assert key_fields <= set(local_s)
    assert key_fields <= set(remote_s)

    for s in (local_s, remote_s):
        assert s["lease"]["state"] == "no-lease"
        assert s["generation_count"] == 0
        assert s["latest_generation"] is None


def test_rejected_count_survives_on_both_backends(
    tmp_path: Path, live_backend
) -> None:
    """拒绝计数是"两条鱼在打架"的唯一信号，两个后端都必须记下来。"""
    local = LocalBackend(root=tmp_path / "w-local")
    remote, _ = live_backend

    for be in (local, remote):
        epoch = be.acquire("win-a")
        with pytest.raises(EpochStale):
            be.push("win-a", epoch - 1, _manifest(1))
        s = be.status()
        assert s["rejected_write_count"] == 1, f"{be.kind} 没记下拒绝计数"


# ---------------------------------------------------------------------------
# 远端断线时不能静默成功
# ---------------------------------------------------------------------------


def test_remote_backend_fails_loudly_when_warden_is_down(tmp_path: Path) -> None:
    be = RemoteBackend(
        "http://127.0.0.1:1", "tok", staging=tmp_path / "s"
    )
    be.client.timeout = 0.3
    be.client.retries = 1
    with pytest.raises(Fatal) as ei:
        be.acquire("win-a")
    assert "连不上" in str(ei.value)


def test_remote_status_failure_is_molt_error_not_bare_exception(
    tmp_path: Path,
) -> None:
    """连不上时抛的必须是 MoltError 家族。

    如果让 urllib 的原始异常逃到 CLI，`except MoltError` 就接不住，
    用户会看到一个 traceback 而不是"NAS 上的 Warden 没在跑"。
    """
    be = RemoteBackend("http://127.0.0.1:1", "tok", staging=tmp_path / "s")
    be.client.timeout = 0.3
    be.client.retries = 1
    with pytest.raises(MoltError):
        be.status()
