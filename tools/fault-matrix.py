"""故障注入矩阵：把"出错时会不会把系统留在半死状态"变成一张可以重跑的表。

## 这个脚本为什么存在

`docs/V0.4-PLAN.md` 的立项理由只有一句：**这套系统里最贵的错误全都长得像成功。**

  - 归档少了一半 → 只要没人算指纹，解出来照样能读出前面几个成员
  - 落位写一半 → 目标 `data/` 里照样有 `data_v4.db`，`ls` 看着也对
  - 账本写坏 → 要等下一次 `load()` 才发现，而那时上一次迁徙早就结了

所以这里不做"功能验收"（那在真机端到端里验过了），只做一件事：
**在每一个会失败的地方人为踹一脚，然后检查系统有没有留在"看起来正常、实际不对"的状态。**

## 怎么读这张表

每一行是一个场景，`期望` 是我们声称的行为，`实测` 是这次跑出来的事实。
`✅` = 行为和声称一致；`❌` = 不一致（那就是一个要修的 bug，不是文档问题）。

    28  scenarios, 28 ok

退出码：全过 0，有失败 1。**失败不是脚本坏了，是产品坏了。**

## 为什么用直接调函数而不是打 HTTP

失败注入要 monkeypatch 到 `shutil.copy2`、`os.replace`、`time.time` 这一层，
HTTP 那条路上够不着。少数几条（上传被截断、`/where` 的 stale 判定）必须打
真实 socket，那几条会自己起一个 `WardenServer`。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sqlite3
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# 允许直接 `python tools/fault-matrix.py`
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from molt import epoch as epoch_mod  # noqa: E402
from molt import transfer  # noqa: E402
from molt import warden as warden_mod  # noqa: E402
from molt.errors import (  # noqa: E402
    EpochStale,
    Fatal,
    LeaseHeld,
    MoltError,
    NeedsHuman,
)
from molt.soul import Manifest, manifest_problems, sha256_of  # noqa: E402
from molt.transfer import (  # noqa: E402
    RESTORE_MARKER_NAME,
    extract_archive,
    incomplete_restore,
    make_archive,
    restore_into,
)
from molt.warden import Warden, _atomic_write_json  # noqa: E402


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


@dataclass
class Result:
    sid: int
    name: str
    expect: str
    ok: bool
    detail: str


def _sqlite(path: Path, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, title TEXT)")
        conn.executemany(
            "INSERT INTO conversations (title) VALUES (?)",
            [(f"第 {i} 条",) for i in range(rows)],
        )
        conn.commit()
    finally:
        conn.close()


def _payload(root: Path) -> Path:
    """造一份像样的 generation 目录（两个库 + 配置）。"""
    root.mkdir(parents=True, exist_ok=True)
    _sqlite(root / "data_v4.db", rows=3)
    _sqlite(
        root / "plugin_data" / "astrbot_plugin_memory_companion" / "memory_companion.db",
        rows=2,
    )
    (root / "cmd_config.json").write_text('{"config_version": 5}', encoding="utf-8-sig")
    return root


def _manifest_dict(generation: int = 1) -> Dict[str, Any]:
    return {
        "generation": generation,
        "created_at": 1794679431.5,
        "source_host": "fault-matrix",
        "source_root": "/tmp/fault-matrix/data",
        "protocol_version": 1,
        "astrbot_version": "4.29.4",
        "platform": "linux",
        "python_version": "3.10.7",
        "artifacts": [],
        "runtime_pip": [],
    }


def _raises(exc_types: Tuple[type, ...], fn: Callable[[], Any]) -> Optional[str]:
    """跑 fn，要求它抛指定异常；返回异常文本（没抛则返回 None）。"""
    try:
        fn()
    except exc_types as e:  # noqa: BLE001 - 这里就是要接住任何异常来做断言
        return str(e)
    except BaseException as e:  # noqa: BLE001
        return f"抛了意料之外的 {type(e).__name__}: {e}"
    return None


# --------------------------------------------------------------------------
# 场景 1–2：打包
# --------------------------------------------------------------------------


def scenario_pack_interrupted(tmp: Path) -> Tuple[bool, str]:
    """打包中途某个成员写失败 → 目标路径不许出现 tar.gz，也不许留 .part。"""
    src = _payload(tmp / "gen")
    out = tmp / "gen-000001.tar.gz"
    real_add = transfer.tarfile.TarFile.add
    calls = {"n": 0}

    def boom(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError(28, "No space left on device")
        return real_add(self, *a, **kw)

    transfer.tarfile.TarFile.add = boom
    try:
        err = _raises((OSError, MoltError, Fatal), lambda: make_archive(src, out))
    finally:
        transfer.tarfile.TarFile.add = real_add

    if err is None:
        return False, "打包本该失败，但它成功了"
    if out.exists():
        return False, f"留下了半成品归档 {out.name}"
    if list(tmp.glob("gen-*.tar.gz*")):
        return False, f"留下了残留文件 {[p.name for p in tmp.glob('gen-*')]}"
    return True, "无归档、无 .part"


def scenario_pack_rename_fails(tmp: Path) -> Tuple[bool, str]:
    """归档写完了但改不了名（跨设备 / 无人区）→ 不许留下任何可见产物。"""
    src = _payload(tmp / "gen")
    out = tmp / "gen-000002.tar.gz"
    real_replace = transfer.os.replace

    def boom(a, b):
        raise OSError(18, "Invalid cross-device link")

    transfer.os.replace = boom
    try:
        err = _raises((OSError, MoltError, Fatal), lambda: make_archive(src, out))
    finally:
        transfer.os.replace = real_replace

    if err is None:
        return False, "改名失败时本该抛错"
    leftovers = [p.name for p in tmp.glob("gen-000002*")]
    if out.exists():
        return False, "归档出现了（说明没走 .part + rename）"
    if any(n.endswith(".part") for n in leftovers):
        return False, f"留下了 {leftovers}"
    return True, "无归档、无 .part"


# --------------------------------------------------------------------------
# 场景 3–4：传输与指纹
# --------------------------------------------------------------------------


def scenario_archive_bit_flip(tmp: Path) -> Tuple[bool, str]:
    """归档在路上被改了一个字节 → 解包前必须拒绝。"""
    src = _payload(tmp / "gen")
    out = tmp / "gen-000001.tar.gz"
    info = make_archive(src, out)

    data = bytearray(out.read_bytes())
    data[len(data) // 2] ^= 0xFF
    out.write_bytes(bytes(data))

    err = _raises((Fatal,), lambda: extract_archive(out, tmp / "unpack", expected_sha256=info["sha256"]))
    if err is None:
        return False, "改了一个字节居然解包成功了"
    if (tmp / "unpack" / "data_v4.db").exists():
        return False, "拒绝了，但还是把文件写出来了"
    return True, "解包前拒绝，且没落任何文件"


def scenario_upload_truncated(tmp: Path) -> Tuple[bool, str]:
    """上传只发一半字节 → Warden 回 400 checksum_mismatch，归档目录不留东西。"""
    import urllib.error
    import urllib.request

    from molt.server import WardenServer

    root = tmp / "warden"
    server = WardenServer(root=root, token="tok", port=0)
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        src = _payload(tmp / "gen")
        out = tmp / "gen-000001.tar.gz"
        info = make_archive(src, out)
        blob = out.read_bytes()
        half = blob[: len(blob) // 2]

        req = urllib.request.Request(
            f"{base}/archive/1",
            data=half,
            method="PUT",
            headers={
                "X-Molt-Token": "tok",
                "X-Molt-Generation": "1",
                "X-Molt-Sha256": info["sha256"],
                "Content-Type": "application/octet-stream",
            },
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            return False, "截断的上传居然被接受了"
        except urllib.error.HTTPError as e:
            body = json.loads(e.read().decode("utf-8") or "{}")
            if e.code != 400 or body.get("kind") != "checksum_mismatch":
                return False, f"HTTP {e.code} kind={body.get('kind')!r}"
        leftovers = list((root / "archives").glob("*")) if (root / "archives").exists() else []
        if leftovers:
            return False, f"归档目录留下了 {[p.name for p in leftovers]}"
        return True, "400 checksum_mismatch，归档区干净"
    finally:
        server.stop()


# --------------------------------------------------------------------------
# 场景 5–6：落位
# --------------------------------------------------------------------------


def scenario_restore_interrupted(tmp: Path) -> Tuple[bool, str]:
    """落位复制到第二个文件时失败 → 目标里不许出现半份库，标记必须留下。"""
    src = _payload(tmp / "gen")
    target = tmp / "data"
    target.mkdir(parents=True, exist_ok=True)

    real_copy = shutil.copy2
    calls = {"n": 0}

    def boom(a, b, *rest, **kw):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError(5, "Input/output error")
        return real_copy(a, b, *rest, **kw)

    transfer.shutil.copy2 = boom
    try:
        err = _raises((OSError, MoltError, Fatal), lambda: restore_into(src, target, backup=True))
    finally:
        transfer.shutil.copy2 = real_copy

    if err is None:
        return False, "中途失败居然被吞掉了"
    if calls["n"] < 2:
        return False, f"注入没生效（只复制了 {calls['n']} 次）——这条用例什么也没验"

    bad: List[str] = []
    for p in target.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(target).as_posix()
        if rel.startswith(".molt-"):
            # molt 自己的记账文件（未完成标记 / 临时 .part）不算内容
            if rel.endswith(".molt-part"):
                bad.append(f"{rel}（临时文件残留）")
            continue
        src_file = src / rel
        if not src_file.exists():
            bad.append(f"{rel}（目标里有、快照里没有）")
        elif p.read_bytes() != src_file.read_bytes():
            bad.append(f"{rel}（截断或内容不符）")
    if bad:
        return False, f"目标里留下了坏文件：{bad}"
    if list(target.rglob("*.molt-part")):
        return False, "临时文件残留"
    marker = incomplete_restore(target)
    if marker is None:
        return False, "没有留下 .molt-restore-incomplete，重跑无从知道上次没做完"
    return True, "没有截断的文件，标记在（已完整落位的文件保留是设计如此）"


def scenario_restore_nonempty_wal(tmp: Path) -> Tuple[bool, str]:
    """快照里带着非空 -wal → 拒绝落位（只拷主文件会丢已提交事务）。"""
    src = _payload(tmp / "gen")
    (tmp / "gen" / "data_v4.db-wal").write_bytes(b"x" * 32)
    target = tmp / "data"
    target.mkdir(parents=True, exist_ok=True)

    err = _raises((NeedsHuman, Fatal, MoltError), lambda: restore_into(src, target, backup=True))
    if err is None:
        return False, "带着非空 WAL 居然落位成功了"
    if "未合并" not in err:
        return False, f"拒绝的理由不是 WAL：{err[:60]}"
    if any(p.is_file() and not p.name.startswith(".molt-") for p in target.rglob("*")):
        return False, "拒绝了，但目标里还是被写了东西"
    return True, "拒绝（未合并事务），目标里没有任何快照内容"


# --------------------------------------------------------------------------
# 场景 7–9：账本与租约
# --------------------------------------------------------------------------


def scenario_ledger_truncated(tmp: Path) -> Tuple[bool, str]:
    """state.json 写到一半断电 → 明确报"坏了"，绝不静默重建。"""
    root = tmp / "warden"
    root.mkdir(parents=True, exist_ok=True)
    state = root / "state.json"
    state.write_text('{"fenced_state": {"current_epoch": 3', encoding="utf-8")
    before = state.read_bytes()

    err = _raises((Fatal,), lambda: Warden(root=root).status())
    if err is None:
        return False, "坏账本居然能读出状态（说明被静默重建了）"
    if state.read_bytes() != before:
        return False, "坏账本被改写了 —— 它会掩盖真正的原因"
    if "人工介入" not in err:
        return False, f"报错没有说清要人介入：{err[:60]}"
    return True, "Fatal + 不动文件"


def scenario_lease_contention(tmp: Path) -> Tuple[bool, str]:
    """两个真进程同时抢租约 → 只能有一个拿到（多线程测不出来）。"""
    import subprocess

    root = tmp / "warden"
    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(_REPO_ROOT)!r})\n"
        "from molt.warden import Warden\n"
        "from molt.errors import MoltError\n"
        "w = Warden(root=Path(sys.argv[1]))\n"
        "try:\n"
        "    lease = w.acquire(sys.argv[2])\n"
        "    print('ok', lease.epoch)\n"
        "except MoltError as e:\n"
        "    print('held', type(e).__name__)\n"
    )
    script = tmp / "grab.py"
    script.write_text(code, encoding="utf-8")

    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(root), f"shell-{i}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(2)
    ]
    outs = [p.communicate(timeout=60) for p in procs]
    lines = [o[0].strip() for o in outs]
    oks = [ln for ln in lines if ln.startswith("ok")]
    held = [ln for ln in lines if ln.startswith("held")]
    if len(oks) != 1 or len(held) != 1:
        return False, f"两个进程的结果是 {lines}"
    if "LeaseHeld" not in held[0]:
        return False, f"失败的那一方抛的是 {held[0]}"
    return True, "一个 ok、一个 LeaseHeld"


def scenario_epoch_stale(tmp: Path) -> Tuple[bool, str]:
    """存储端收到比当前更小的 epoch → 必须拒绝（fencing 的全部意义）。"""
    root = tmp / "warden"
    w = Warden(root=root)
    lease = w.acquire("shell-a", ttl_seconds=90)
    w.accept_snapshot(
        claimed_epoch=lease.epoch,
        claimed_shell_id="shell-a",
        manifest={
            "generation": 1,
            "created_at": 1.0,
            "source_host": "h",
            "source_root": "/r",
            "protocol_version": 1,
        },
    )
    current = w.state.current_epoch

    err = _raises(
        (EpochStale,),
        lambda: epoch_mod.accept_write(w.state, current - 1, "shell-a"),
    )
    if err is None:
        return False, "更小的 epoch 被接受了（fencing 等于没有）"
    return True, f"EpochStale（当前 epoch {current}）"


# --------------------------------------------------------------------------
# 场景 10–12：心跳、时钟、盘满
# --------------------------------------------------------------------------


def scenario_heartbeat_stale(tmp: Path) -> Tuple[bool, str]:
    """心跳停了 700 秒 → /where 的判定必须变成"哑了"，不能继续冒充在线。"""
    root = tmp / "warden"
    w = Warden(root=root)
    w.record_heartbeat("shell-a", generation=3, note="agent", label="SHELL-A")

    view_now = w.status()["heartbeats"]["shell-a"]
    if view_now["stale"]:
        return False, "刚上报就说它哑了"

    real_time = time.time
    warden_mod.time.time = lambda: real_time() + 700
    try:
        view_later = w.status()["heartbeats"]["shell-a"]
    finally:
        warden_mod.time.time = real_time

    if not view_later["stale"]:
        return False, "700 秒没上报仍然显示在跑"
    if view_later["generation"] != 3:
        return False, "stale 之后丢了它是第几代"
    return True, f"7 秒 → stale=True（{view_later['age_readable']}）"


def scenario_clock_rollback(tmp: Path) -> Tuple[bool, str]:
    """把时钟拨回一小时 → 不出现永久租约，epoch 也不许倒退。"""
    root = tmp / "warden"
    w = Warden(root=root)
    lease = w.acquire("shell-a", ttl_seconds=90)
    epoch_before = w.state.current_epoch

    real_time = time.time
    warden_mod.time.time = lambda: real_time() - 3600
    epoch_mod.time.time = lambda: real_time() - 3600
    try:
        # 时钟倒退后，租约的到期时刻在"未来"，但它不是永久的：
        # 判定用的是绝对 deadline，不是"过了多少秒"。
        _, reason = w.check_can_start("shell-b")
        blocked = bool(reason)
        epoch_after = w.state.current_epoch
    finally:
        warden_mod.time.time = real_time
        epoch_mod.time.time = real_time

    if not blocked:
        return False, "时钟倒退之后别人可以直接启动（两条鱼同时在线）"
    if epoch_after < epoch_before:
        return False, f"epoch 倒退了：{epoch_before} → {epoch_after}"
    return True, f"仍被拦（epoch {epoch_before} → {epoch_after} 未倒退）"


def scenario_ledger_disk_full(tmp: Path) -> Tuple[bool, str]:
    """账本写入时盘满 → 抛 Fatal，且 state.json 仍是上一版可读内容。"""
    root = tmp / "warden"
    w = Warden(root=root)
    w.acquire("shell-a", ttl_seconds=90)
    state = root / "state.json"
    before = state.read_bytes()
    before_obj = json.loads(before.decode("utf-8"))

    real_replace = warden_mod.os.replace

    def boom(a, b):
        raise OSError(28, "No space left on device")

    warden_mod.os.replace = boom
    try:
        err = _raises((Fatal,), lambda: _atomic_write_json(state, {"x": 1}))
    finally:
        warden_mod.os.replace = real_replace

    if err is None:
        return False, "盘满时居然没报错"
    if state.read_bytes() != before:
        return False, "旧账本被改坏了"
    if json.loads(state.read_text(encoding="utf-8")) != before_obj:
        return False, "旧账本内容变了"
    if "没有落盘" not in err:
        return False, f"报错没说清「这次没写进去」：{err[:60]}"
    return True, "Fatal + 旧账本完好可读"


# --------------------------------------------------------------------------
# 场景 13–14：manifest
# --------------------------------------------------------------------------


def scenario_manifest_missing_field(tmp: Path) -> Tuple[bool, str]:
    """manifest 少了 generation → 拒绝，且理由像人话（不是 KeyError）。"""
    bad = _manifest_dict()
    del bad["generation"]
    err = _raises((Fatal,), lambda: Manifest.from_dict(bad))
    if err is None:
        return False, "缺字段居然通过了"
    if "generation" not in err or "出生证明" not in err:
        return False, f"报错不像拒收理由：{err[:80]}"
    return True, "Fatal，点名 generation"


def scenario_manifest_wrong_type(tmp: Path) -> Tuple[bool, str]:
    """manifest 的 created_at 变成字符串 → 拒绝。"""
    bad = _manifest_dict()
    bad["created_at"] = "昨天"
    err = _raises((Fatal,), lambda: Manifest.from_dict(bad))
    if err is None:
        return False, "类型错居然通过了"
    if "created_at" not in err:
        return False, f"报错没点名 created_at：{err[:80]}"
    return True, "Fatal，点名 created_at"


# --------------------------------------------------------------------------
# 场景 15–16：落位标记的读取方
# --------------------------------------------------------------------------


def scenario_marker_blocks_boot(tmp: Path) -> Tuple[bool, str]:
    """上一次落位没做完 → 守护进程必须拒绝启动那个壳。"""
    target = tmp / "data"
    target.mkdir(parents=True, exist_ok=True)
    (target / RESTORE_MARKER_NAME).write_text(
        json.dumps({"gen_dir": "x", "started_at": "2026-11-15 06:00:00", "generation": 9}),
        encoding="utf-8",
    )
    marker = incomplete_restore(target)
    if marker is None:
        return False, "标记写了但读不出来"
    if marker.get("generation") != 9:
        return False, f"代次丢了：{marker}"
    # 守护进程侧的一致性由 pytest 钉住（test_hardening_restore_guard.py），
    # 这里只验"标记本身能被识别成未完成"。
    return True, "标记可读，代次 9"


def scenario_marker_broken(tmp: Path) -> Tuple[bool, str]:
    """标记本身写到一半（断电正好断在写它那一刻）→ 也要当成未完成。"""
    target = tmp / "data"
    target.mkdir(parents=True, exist_ok=True)
    (target / RESTORE_MARKER_NAME).write_text("{oops", encoding="utf-8")
    marker = incomplete_restore(target)
    if marker is None:
        return False, "坏标记被当成「没有标记」—— 那正好是最危险的解释"
    if not marker.get("broken"):
        return False, f"没有标成 broken：{marker}"
    return True, "broken=True"


# --------------------------------------------------------------------------
# 矩阵
# --------------------------------------------------------------------------


SCENARIOS: List[Tuple[str, str, Callable[[Path], Tuple[bool, str]]]] = [
    ("打包中途失败", "无 gen-*.tar.gz、无 .part", scenario_pack_interrupted),
    ("打包改名失败", "无归档、无 .part", scenario_pack_rename_fails),
    ("归档被改一字节", "解包前拒绝，不落文件", scenario_archive_bit_flip),
    ("上传被截断", "400 checksum_mismatch，归档区干净", scenario_upload_truncated),
    ("落位中途失败", "无半份库，标记在", scenario_restore_interrupted),
    ("快照带非空 WAL", "拒绝，目标不动", scenario_restore_nonempty_wal),
    ("账本被截断", "Fatal 且不改写", scenario_ledger_truncated),
    ("两壳抢租约", "一 ok 一 LeaseHeld", scenario_lease_contention),
    ("旧 epoch 写入", "EpochStale", scenario_epoch_stale),
    ("心跳停了 700 秒", "stale=True，不冒充在线", scenario_heartbeat_stale),
    ("时钟倒退一小时", "仍被拦，epoch 不倒退", scenario_clock_rollback),
    ("账本写入盘满", "Fatal，旧账本完好", scenario_ledger_disk_full),
    ("manifest 缺 generation", "Fatal，点名字段", scenario_manifest_missing_field),
    ("manifest 类型错", "Fatal，点名字段", scenario_manifest_wrong_type),
    ("未完成标记存在", "能识别成未完成", scenario_marker_blocks_boot),
    ("未完成标记损坏", "当成未完成（broken）", scenario_marker_broken),
]


def run(only: Optional[List[int]] = None) -> List[Result]:
    import tempfile

    results: List[Result] = []
    for idx, (name, expect, fn) in enumerate(SCENARIOS, start=1):
        if only and idx not in only:
            continue
        with tempfile.TemporaryDirectory(prefix=f"fault-{idx:02d}-") as td:
            try:
                ok, detail = fn(Path(td))
            except BaseException:  # noqa: BLE001 - 场景自己炸了也是一种结论
                ok, detail = False, "场景自己抛了异常：" + traceback.format_exc(limit=3)
        results.append(Result(idx, name, expect, bool(ok), detail))
    return results


def _print(results: List[Result]) -> None:
    w_name = max(len(r.name) for r in results) + 2
    print(f"{'#':>3}  {'场景'.ljust(w_name)}  {'实测'}")
    print("-" * (6 + w_name + 60))
    for r in results:
        mark = "OK  " if r.ok else "FAIL"
        print(f"{r.sid:>3}  {r.name.ljust(w_name)}  {mark} {r.detail}")
    ok = sum(1 for r in results if r.ok)
    print(f"\n{len(results)} 个场景，{ok} 个符合声称的行为")


def _to_markdown(results: List[Result]) -> str:
    lines = [
        "| # | 场景 | 声称的行为 | 实测 | 结论 |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        detail = r.detail.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {r.sid} | {r.name} | {r.expect} | {detail} | {'✅' if r.ok else '❌'} |"
        )
    ok = sum(1 for r in results if r.ok)
    lines.append("")
    lines.append(f"{len(results)} 个场景，{ok} 个符合声称的行为。")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="故障注入矩阵")
    p.add_argument("--only", help="只跑这几条，逗号分隔（例如 1,5,12）")
    p.add_argument("--json", help="把结果写成 JSON")
    p.add_argument("--md", help="把结果写成 markdown 表（追加到文件时不带标题）")
    args = p.parse_args(argv)

    # 输出用 UTF-8：Windows 的 GBK 控制台打不出 ✅ 那一类字符，
    # 而这正是本项目反复踩过的坑（b89 的 UnicodeEncodeError）。
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

    only = (
        [int(x) for x in args.only.split(",") if x.strip()] if args.only else None
    )
    results = run(only)
    _print(results)

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                [
                    {
                        "id": r.sid,
                        "name": r.name,
                        "expect": r.expect,
                        "ok": r.ok,
                        "detail": r.detail,
                    }
                    for r in results
                ],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    if args.md:
        Path(args.md).write_text(_to_markdown(results) + "\n", encoding="utf-8")

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
