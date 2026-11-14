#!/usr/bin/env python3
"""在 NAS 上后台启动 molt-bounce（往返触发器），脱离 SSH 会话。

与 `start-warden.py` / `start-homer.py` 同一个理由：`ssh_exec` 一结束，
子进程会收到 SIGHUP；NAS 是 BusyBox，没有 nohup / setsid / screen / tmux，
只有 `subprocess.Popen(..., start_new_session=True)` 能让它活到 PPID=1。

用法：
    python3 tools/start-bounce.py            # 后台起
    python3 tools/start-bounce.py --stop     # 停
    python3 tools/start-bounce.py --status   # 看
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PKG = Path("/mnt/dm-0/astrbot/_molt/pkg")
TOOLS = PKG / "tools"
CONFIG = TOOLS / "molt-bounce.json"
LOG = Path("/mnt/dm-0/astrbot/_molt/bounce.log")
PIDFILE = Path("/mnt/dm-0/astrbot/_molt/bounce.pid")


def read_pid() -> int:
    if not PIDFILE.exists():
        return 0
    try:
        return int(PIDFILE.read_text().strip())
    except ValueError:
        return 0


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def status() -> int:
    pid = read_pid()
    if alive(pid):
        print(f"bounce 在跑：pid={pid}")
        return 0
    print("bounce 没在跑（或 pid 文件过期）")
    return 1


def stop() -> int:
    pid = read_pid()
    if not alive(pid):
        print("没有可停的进程")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not alive(pid):
            print(f"已停止 pid={pid}")
            PIDFILE.unlink(missing_ok=True)
            return 0
        time.sleep(0.2)
    print(f"pid={pid} 五秒内没退，改用 SIGKILL")
    os.kill(pid, signal.SIGKILL)
    PIDFILE.unlink(missing_ok=True)
    return 0


def start() -> int:
    old = read_pid()
    if alive(old):
        print(f"已经在跑：pid={old}")
        return 0

    log = LOG.open("ab")
    proc = subprocess.Popen(
        [sys.executable, "-u", str(TOOLS / "molt-bounce.py"), "--config", str(CONFIG)],
        cwd=str(PKG),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    PIDFILE.write_text(str(proc.pid))
    time.sleep(1.5)

    if not alive(proc.pid):
        print(f"起不来：pid={proc.pid} 一秒内就退了")
        print(f"去看日志：{LOG}")
        return 1

    print(f"已启动 pid={proc.pid}")
    print(f"日志：{LOG}")
    print(f"配置：{CONFIG}")
    return 0


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--stop":
        return stop()
    if arg == "--status":
        return status()
    return start()


if __name__ == "__main__":
    sys.exit(main())
