#!/usr/bin/env python3
"""在 NAS 上后台启动维护窗口保险丝，脱离 SSH 会话。

与 `start-warden.py` 同一个理由：`ssh_exec` 一结束，子进程会收到 SIGHUP；
NAS 是 BusyBox，没有 nohup / setsid / screen / tmux。
唯一可靠的是 `subprocess.Popen` + `start_new_session=True`。

用法：
    python3 tools/start-window-guard.py --minutes 15
    然后窗口结束（或放弃）时：
    python3 tools/start-window-guard.py --cancel
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

GUARD = Path("/mnt/dm-0/astrbot/_molt/tools/window-guard.py")
CANCEL = Path("/mnt/dm-0/astrbot/_molt/window.done")
LOG = Path("/mnt/dm-0/astrbot/_molt/window-guard.log")
PIDFILE = Path("/mnt/dm-0/astrbot/_molt/window-guard.pid")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, ValueError):
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=15.0)
    ap.add_argument("--container", default="astrbot")
    ap.add_argument("--cancel", action="store_true", help="正常结束窗口（不恢复容器）")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.status:
        pid = int(PIDFILE.read_text().strip()) if PIDFILE.exists() else 0
        print(f"pid={pid} alive={_alive(pid) if pid else False} "
              f"cancel_file_exists={CANCEL.exists()}")
        if LOG.exists():
            print("--- 日志尾部 ---")
            print("\n".join(LOG.read_text(encoding="utf-8").splitlines()[-12:]))
        return 0

    if args.cancel:
        CANCEL.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n", encoding="utf-8")
        print(f"已创建取消文件 {CANCEL}（guard 最多 {2:g} 秒后退出，不动容器）")
        time.sleep(3)
        pid = int(PIDFILE.read_text().strip()) if PIDFILE.exists() else 0
        print(f"guard pid={pid} 还在={_alive(pid) if pid else False}")
        return 0

    # 起新窗口前先确认没有旧的在跑
    if PIDFILE.exists():
        old = int(PIDFILE.read_text().strip() or "0")
        if old and _alive(old):
            print(f"[注意] 已经有一个 guard 在跑 pid={old}，先取消它再来：--cancel")
            return 1
        PIDFILE.unlink(missing_ok=True)
    CANCEL.unlink(missing_ok=True)

    cmd = [
        sys.executable,
        "-u",
        str(GUARD),
        "--container",
        args.container,
        "--minutes",
        str(args.minutes),
        "--cancel-file",
        str(CANCEL),
        "--log",
        str(LOG),
    ]
    logf = open(LOG, "ab", buffering=0)
    proc = subprocess.Popen(
        cmd,
        stdout=logf,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    PIDFILE.write_text(str(proc.pid), encoding="utf-8")
    print(f"已启动保险丝 pid={proc.pid}，上限 {args.minutes:g} 分钟")
    time.sleep(1.5)
    if proc.poll() is not None:
        print(f"[FAIL] 立即退出，返回码 {proc.returncode}")
        print(LOG.read_text(encoding="utf-8")[-2000:])
        return 1
    print(f"pid={proc.pid} 仍在运行。正常结束时记得 --cancel")
    return 0


if __name__ == "__main__":
    sys.exit(main())
