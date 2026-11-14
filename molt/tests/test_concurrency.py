"""真并发验证：两个壳进程同时抢同一个 Warden。

## 为什么必须用真进程，而不是多线程

多线程跑在同一个 Python 解释器里，共享 GIL。GIL 会让"读到旧值再写"
这类竞态**看起来不存在**——因为线程切换点比真实进程少得多。

而本项目要防的正是**跨进程**的竞态：NAS 上的 AstrBot 和 Windows 上的
AstrBot 是两个不同的进程，各自有独立的内存、独立的文件句柄缓存。
用线程测出来的"安全"，在进程之间可能完全不成立。

所以这个测试用 `subprocess` 起两个真的 Python 进程，让它们
**同一时刻**去打同一个 Warden。

## 这个测试想说什么

不是"Warden 很快"，而是：**无论两个进程怎么抢，最终只有一个能写下东西，
而且 epoch 序列不能出现倒退。** 快慢不重要，对不对才重要。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from molt.warden import Warden


PYTHON = sys.executable
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


WORKER_SRC = textwrap.dedent(
    '''
    """一个壳：抢租约 → 写快照 → 报告结果（JSON 打到 stdout）。"""
    import json, sys, time
    from pathlib import Path

    sys.path.insert(0, sys.argv[1])

    from molt.warden import Warden
    from molt.errors import MoltError

    root = Path(sys.argv[2])
    shell_id = sys.argv[3]
    barrier_file = Path(sys.argv[4])
    result_file = Path(sys.argv[5])

    w = Warden(root=root)
    w.load()

    # 自旋等待屏障文件出现，让两个进程尽量同时冲进去
    deadline = time.time() + 15
    while not barrier_file.exists() and time.time() < deadline:
        time.sleep(0.001)

    out = {"shell_id": shell_id}
    try:
        lease = w.acquire(shell_id, ttl_seconds=60)
        out["lease"] = "granted"
        out["epoch"] = lease.epoch
        try:
            rec = w.accept_snapshot(
                claimed_epoch=lease.epoch,
                claimed_shell_id=shell_id,
                manifest={
                    "generation": 1,
                    "created_at": time.time(),
                    "source_host": shell_id,
                    "source_root": "/x",
                    "protocol_version": 1,
                    "artifacts": [],
                },
            )
            out["write"] = "accepted"
            out["generation"] = rec.generation
        except MoltError as e:
            out["write"] = "rejected"
            out["write_error"] = type(e).__name__
    except MoltError as e:
        out["lease"] = "refused"
        out["lease_error"] = type(e).__name__

    result_file.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    '''
).strip()


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "worker.py").write_text(WORKER_SRC, encoding="utf-8")
    return ws


def spawn(
    workspace: Path,
    root: Path,
    shell_id: str,
    barrier: Path,
    result: Path,
) -> subprocess.Popen:
    return subprocess.Popen(
        [
            PYTHON,
            str(workspace / "worker.py"),
            str(REPO_ROOT),
            str(root),
            shell_id,
            str(barrier),
            str(result),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def run_race(tmp_path: Path, workspace: Path, rounds: int = 1) -> list[dict]:
    root = tmp_path / "molt"
    barrier = tmp_path / "GO"
    results: list[dict] = []

    procs = []
    for i in range(2):
        rf = tmp_path / f"result-{i}.json"
        procs.append(
            spawn(workspace, root, f"shell-{chr(65 + i)}", barrier, rf)
        )
        
    time.sleep(0.15)
    barrier.write_text("go", encoding="utf-8")

    for p in procs:
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
            raise
        assert p.returncode == 0, f"worker 退出码 {p.returncode}: {p.stderr.read()}"

    for i in range(2):
        results.append(
            json.loads((tmp_path / f"result-{i}.json").read_text(encoding="utf-8"))
        )
    return results


# --------------------------------------------------------------------------


def test_two_processes_only_one_wins(tmp_path: Path, workspace: Path):
    """两个真的进程同时抢 → 恰好一个拿到租约。"""
    results = run_race(tmp_path, workspace)

    granted = [r for r in results if r.get("lease") == "granted"]
    refused = [r for r in results if r.get("lease") == "refused"]

    assert len(granted) == 1, f"应该恰好一个成功，实际 {granted}（全部={results}）"
    assert len(refused) == 1
    assert refused[0].get("lease_error") == "LeaseHeld"


def test_winner_can_write_loser_cannot(tmp_path: Path, workspace: Path):
    results = run_race(tmp_path, workspace)

    granted = [r for r in results if r.get("lease") == "granted"][0]
    assert granted.get("write") == "accepted"

    loser = [r for r in results if r.get("lease") == "refused"][0]
    assert "write" not in loser      # 它连写入入口都没进


def test_final_state_has_exactly_one_generation(tmp_path: Path, workspace: Path):
    """并发之后，磁盘上的状态必须干净：一次迁徙 = 一条记录。"""
    run_race(tmp_path, workspace)

    w = Warden(root=tmp_path / "molt")
    w.load()
    assert len(w.generations) == 1
    assert w.state.current_epoch == 1
    assert w.state.lease is not None


def test_repeated_races_never_double_grant(tmp_path: Path, workspace: Path):
    """连续跑 5 轮，每轮都必须是恰好一个赢。

    单轮通过可能是运气（进程调度恰好错开）。连跑多轮才说明
    "这是机制保证的"，不是偶然。
    """
    for i in range(5):
        sub = tmp_path / f"r{i}"
        sub.mkdir()
        results = run_race(sub, workspace)
        granted = [r for r in results if r.get("lease") == "granted"]
        assert len(granted) == 1, f"第 {i} 轮出现 {len(granted)} 个赢家"


def test_state_file_never_partially_written_during_race(tmp_path: Path, workspace: Path):
    """并发结束后 state.json 必须是完整合法 JSON。

    这是原子写的意义：如果 Warden 用 write_text 直接覆盖，
    两个进程交错写可能留下半截文件，而它是"谁有资格写灵魂"的唯一依据。
    """
    run_race(tmp_path, workspace)
    raw = (tmp_path / "molt" / "state.json").read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert "fenced_state" in payload
    assert "generations" in payload
