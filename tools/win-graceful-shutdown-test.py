"""在 Windows 上验证 AstrBot 的优雅停机信号。

背景：Linux 侧已实测 —— SIGTERM 直接死（WAL 8,252,392 B 原封不动、无 "Shutting down"），
SIGINT 走优雅路径（exit 0、WAL 归零、有 "🌈 Shutting down AstrBot..."）。

Windows 没有 SIGTERM。理论上 Windows 的 Ctrl+C（CTRL_C_EVENT）/ Ctrl+Break（CTRL_BREAK_EVENT）
会被 Python 转成 KeyboardInterrupt，再由 asyncio.Runner._on_sigint 转成"取消主任务"，
从而让 InitialLoader.start() 里那个 `except asyncio.CancelledError` 兜住并调用 core_lifecycle.stop()。

但这只是读代码得出的推断。这个脚本就是来证伪它的。

判据（三个都要看，缺一个都不算通过）：
  1. 日志里有 "Shutting down AstrBot"
  2. 退出后 data_v4.db-wal 归零（或文件消失）—— 这是 checkpoint 真的跑过的硬证据
  3. data_v4.db 主文件字节数/内容没有变成"被截断"的状态（quick_check 仍 ok）

用法：
  python win-graceful-shutdown-test.py --data <落位后的 data 目录> --signal break|ctrl_c
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ASTBOT_ROOT_DEFAULT = Path(r"G:\molt-p0\astrbot-src")
PYTHON_DEFAULT = Path(r"G:\molt-p0\venv313\Scripts\python.exe")
PORT_DEFAULT = 6199


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def db_facts(p: Path) -> dict:
    """只读打开，不制造 sidecar（用 immutable=1 也不行——我们要看真实状态，就接受它可能建 -shm）。"""
    out: dict = {"path": str(p), "exists": p.exists()}
    if not p.exists():
        return out
    out["bytes"] = p.stat().st_size
    out["sha256_16"] = sha256_of(p)[:16]
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        try:
            out["quick_check"] = con.execute("PRAGMA quick_check").fetchone()[0]
            out["journal_mode"] = con.execute("PRAGMA journal_mode").fetchone()[0]
            out["tables"] = con.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def sidecars(db: Path) -> dict:
    return {
        suf: (db.with_name(db.name + suf).stat().st_size
              if db.with_name(db.name + suf).exists() else None)
        for suf in ("-wal", "-shm")
    }


def cleanup_sidecars(db: Path) -> list[str]:
    """测试收尾：把只读检查建出来的 sidecar 删掉（只对副本做，这是测试目录）。"""
    gone = []
    for suf in ("-wal", "-shm"):
        p = db.with_name(db.name + suf)
        if p.exists() and p.stat().st_size == 0 or (p.exists() and suf == "-shm"):
            p.unlink()
            gone.append(p.name)
    return gone


def wait_http(port: int, timeout: float = 90.0) -> bool:
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def main() -> int:
    # Windows 中文控制台是 GBK(cp936)：日志里有 emoji（AstrBot 会打 🌈 ✨ ➜），
    # 直接 print 会 UnicodeEncodeError。先尝试 UTF-8，退而用 replace 保命。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--root", default=str(ASTBOT_ROOT_DEFAULT))
    ap.add_argument("--python", default=str(PYTHON_DEFAULT))
    ap.add_argument("--port", type=int, default=PORT_DEFAULT)
    ap.add_argument("--signal", choices=("break", "ctrl_c", "console"), default="console",
                    help=(
                        "console（默认，最贴近真实场景）：给子进程开一个新的控制台窗口，"
                        "然后 attach 上去发 CTRL_C_EVENT，等价于用户在 AstrBot 的窗口里按 Ctrl+C。"
                        "break：对进程组发 CTRL_BREAK_EVENT（会变成 SIGBREAK）。"
                        "ctrl_c：对进程组发 CTRL_C_EVENT（Windows 不允许跨组递送，通常无效）。"
                    ))
    ap.add_argument("--wait-start", type=float, default=90.0)
    ap.add_argument("--wait-exit", type=float, default=60.0)
    args = ap.parse_args()

    data = Path(args.data)
    root = Path(args.root)
    py = Path(args.python)
    log_path = Path(__file__).with_name(
        f"shutdown-test-{args.signal}-{time.strftime('%Y%m%dT%H%M%S')}.log"
    )

    db = data / "data_v4.db"
    mcdb = data / "plugin_data" / "astrbot_plugin_memory_companion" / "memory_companion.db"

    print("=" * 70)
    print(f"Windows 优雅停机实测 · 信号 = {args.signal}")
    print("=" * 70)
    print(f"  data   : {data}")
    print(f"  root   : {root}")
    print(f"  python : {py}")
    print(f"  日志   : {log_path}")
    print()

    for p in (data, root, py):
        if not p.exists():
            print(f"[X] 不存在：{p}")
            return 2

    before_db = db_facts(db)
    before_side = sidecars(db)
    print("启动前：")
    print(f"  data_v4.db   {before_db.get('bytes')} B  {before_db.get('sha256_16')}")
    print(f"  quick_check  {before_db.get('quick_check')}  journal={before_db.get('journal_mode')}")
    print(f"  sidecar      wal={before_side['-wal']} shm={before_side['-shm']}")
    print()

    env = dict(os.environ)
    # ⚠️ 这里踩过一次：把 ASTRBOT_ROOT 设成源码目录，AstrBot 就在 astrbot-src/data/
    # 下自建了一套全新的空库，而我们一直在看的 G:\molt-p0\shutdown-test\data 根本没被用到。
    # 正确关系是：ASTRBOT_ROOT = <data 的父目录>，cwd = 源码目录。
    env["ASTRBOT_ROOT"] = str(data.parent)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    # ⚠️ 三种 flag 的差别，是本轮实测逼出来的：
    #   CREATE_NEW_PROCESS_GROUP      → os.kill(pid, CTRL_BREAK_EVENT) 能送到，但 Python 只把
    #                                   CTRL_BREAK 映射成 SIGBREAK 的默认处理器 → 进程直接死
    #                                   （退出码 3221225786 = 0xC000013A = STATUS_CONTROL_C_EXIT）
    #   CREATE_NEW_PROCESS_GROUP 且发 CTRL_C → Windows 根本不跨组递送，什么都不会发生
    #   CREATE_NEW_CONSOLE            → 子进程有自己真正的控制台窗口，我们 SetConsoleCtrlHandler(None)
    #                                   + FreeConsole + AttachConsole 之后发 CTRL_C_EVENT，
    #                                   这才是"用户在 AstrBot 窗口里按 Ctrl+C"的等价物
    if args.signal == "console":
        creationflags = subprocess.CREATE_NEW_CONSOLE
    else:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    log_fh = log_path.open("wb")
    proc = subprocess.Popen(
        [str(py), "main.py"],
        cwd=str(root),
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    print(f"已启动 pid={proc.pid}，等待 WebUI 就绪（最多 {args.wait_start:.0f} 秒）…")

    ready = wait_http(args.port, args.wait_start)
    mid_db = db_facts(db)
    mid_side = sidecars(db)
    print(f"WebUI 就绪: {ready}")
    print("运行中：")
    print(f"  data_v4.db   {mid_db.get('bytes')} B  {mid_db.get('sha256_16')}")
    print(f"  sidecar      wal={mid_side['-wal']} shm={mid_side['-shm']}")
    print()

    if proc.poll() is not None:
        print(f"[X] 进程已经自己退出了，returncode={proc.returncode}")
        log_fh.close()
        return 3

    # ⚠️ 这里才是被测的那一下
    if args.signal == "console":
        # 真实的"用户在控制台里按 Ctrl+C"：
        # 1) 自己先摘掉 Ctrl+C 处理器，否则这个 Ctrl+C 会同时打到我（脚本自己）身上
        # 2) FreeConsole 离开自己的控制台
        # 3) AttachConsole 挂到子进程的控制台上
        # 4) GenerateConsoleCtrlEvent 把 Ctrl+C 送给那个控制台里的所有进程
        # 5) 再挂回自己的控制台
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
        k32.SetConsoleCtrlHandler.restype = wintypes.BOOL
        k32.FreeConsole.restype = wintypes.BOOL
        k32.AttachConsole.argtypes = [wintypes.DWORD]
        k32.AttachConsole.restype = wintypes.BOOL
        k32.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.GenerateConsoleCtrlEvent.restype = wintypes.BOOL

        ATTACH_PARENT_PROCESS = 0xFFFFFFFF
        ok_handler = k32.SetConsoleCtrlHandler(None, True)  # TRUE = 忽略（移除我们自己）
        ok_free = k32.FreeConsole()
        ok_attach = k32.AttachConsole(proc.pid)
        print(f"attach 到 pid {proc.pid} 的控制台：free={bool(ok_free)} attach={bool(ok_attach)}"
              f"（err={ctypes.get_last_error() if not ok_attach else 0}）")
        ok_gen = k32.GenerateConsoleCtrlEvent(0, 0)  # 0,0 = 整个 attached 控制台
        print(f"GenerateConsoleCtrlEvent(CTRL_C) = {bool(ok_gen)}"
              f"（err={ctypes.get_last_error() if not ok_gen else 0}）")
        k32.FreeConsole()
        k32.AttachConsole(ATTACH_PARENT_PROCESS)
        k32.SetConsoleCtrlHandler(None, False)  # 恢复我们自己的处理器
        if not ok_attach or not ok_gen:
            print("[X] 无法把 Ctrl+C 送进那个控制台")
            proc.kill()
            log_fh.close()
            return 4
    else:
        sig = signal.CTRL_BREAK_EVENT if args.signal == "break" else signal.CTRL_C_EVENT
        print(f"发送 {args.signal}（signal={int(sig)}）→ pid {proc.pid} …")
        try:
            os.kill(proc.pid, sig)
        except OSError as e:
            print(f"[X] 发送失败：{e}")
            proc.kill()
            log_fh.close()
            return 4

    try:
        rc = proc.wait(timeout=args.wait_exit)
        timed_out = False
    except subprocess.TimeoutExpired:
        rc = None
        timed_out = True
        print(f"[!] {args.wait_exit:.0f} 秒内没退出，强杀")
        proc.kill()
        proc.wait(timeout=10)
    log_fh.close()

    after_db = db_facts(db)
    after_side = sidecars(db)
    log_text = log_path.read_text(encoding="utf-8", errors="replace")

    print()
    print("=" * 70)
    print("结果")
    print("=" * 70)
    print(f"  退出码           {rc}{'  （超时被强杀）' if timed_out else ''}")
    print(f"  有 Shutting down {('Shutting down AstrBot' in log_text)}")
    print(f"  有 Terminating   {('Terminating plugin' in log_text)}")
    print(f"  data_v4.db       {after_db.get('bytes')} B  {after_db.get('sha256_16')}")
    print(f"  quick_check      {after_db.get('quick_check')}")
    print(f"  sidecar          wal={after_side['-wal']} shm={after_side['-shm']}")
    print(f"  启动前 sha       {before_db.get('sha256_16')}")
    print(f"  运行中 sha       {mid_db.get('sha256_16')}")

    shutdown_lines = [
        ln for ln in log_text.splitlines()
        if ("Shutting down" in ln or "Terminating plugin" in ln
            or "知识库数据库已关闭" in ln or "KeyboardInterrupt" in ln)
    ]
    if shutdown_lines:
        print()
        print("  日志里的停机相关行：")
        for ln in shutdown_lines[:12]:
            print(f"    {ln.strip()}")

    # 判决
    passed = (
        rc is not None
        and "Shutting down AstrBot" in log_text
        and (after_side["-wal"] in (None, 0))
        and after_db.get("quick_check") == "ok"
    )
    print()
    if passed:
        print("[PASS] Windows 上也有等价的优雅停机信号 —— WAL 已 checkpoint，库仍完好")
    else:
        reasons = []
        if rc is None:
            reasons.append("进程没在超时内退出")
        if "Shutting down AstrBot" not in log_text:
            reasons.append("日志里没有 Shutting down（说明没走优雅路径）")
        if after_side["-wal"] not in (None, 0):
            reasons.append(f"WAL 还剩 {after_side['-wal']} 字节（没 checkpoint）")
        if after_db.get("quick_check") != "ok":
            reasons.append(f"quick_check = {after_db.get('quick_check')}")
        print("[FAIL] " + "；".join(reasons))
        print()
        print("  完整日志尾部：")
        for ln in log_text.splitlines()[-25:]:
            print(f"    {ln}")

    gone = cleanup_sidecars(db)
    if gone:
        print()
        print(f"（已清掉这次只读检查建出来的 sidecar：{', '.join(gone)}）")

    result = {
        "signal": args.signal,
        "returncode": rc,
        "timed_out": timed_out,
        "webui_ready": ready,
        "before": before_db,
        "mid": mid_db,
        "after": after_db,
        "sidecars_before": before_side,
        "sidecars_mid": mid_side,
        "sidecars_after": after_side,
        "has_shutting_down": "Shutting down AstrBot" in log_text,
        "passed": passed,
        "log": str(log_path),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    out = Path(__file__).with_name(f"shutdown-test-{args.signal}.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入 {out}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
