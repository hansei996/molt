#!/usr/bin/env python3
"""在 NAS 上后台启动 Warden HTTP 服务，脱离 SSH 会话。

为什么需要这个包装器：`ssh_exec` 一结束，子进程会跟着收到 SIGHUP。
NAS 是 BusyBox，没有 nohup / setsid / screen / tmux（实测确认过，
只有 start-stop-daemon）。唯一可靠的办法是 `subprocess.Popen` +
`start_new_session=True`，这在本项目的 WOL 观察器上已经验证过
能活到 PPID=1。
"""

import os
import subprocess
import sys
import time
from pathlib import Path

PKG = Path("/mnt/dm-0/astrbot/_molt/pkg/molt")
ROOT = Path("/mnt/dm-0/astrbot/_molt/warden-root")
LOG = Path("/mnt/dm-0/astrbot/_molt/warden.log")
PIDFILE = Path("/mnt/dm-0/astrbot/_molt/warden.pid")
TOKENFILE = Path("/mnt/dm-0/astrbot/_molt/warden.token")
PORT = 6186

def main() -> int:
    # 停止旧进程
    if PIDFILE.exists():
        try:
            old = int(PIDFILE.read_text().strip())
            os.kill(old, 15)
            print(f"已停止旧进程 pid={old}")
            time.sleep(0.5)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        PIDFILE.unlink(missing_ok=True)

    ROOT.mkdir(parents=True, exist_ok=True)

    token = "molt-dev-token-CHANGEME"
    TOKENFILE.write_text(token + "\n")
    os.chmod(TOKENFILE, 0o600)

    env = dict(os.environ)
    # PKG 就是包含 `molt/` 那个包目录的目录，所以 PYTHONPATH 就是它本身
    env["PYTHONPATH"] = str(PKG)

    cmd = [
        sys.executable,
        "-u",
        "-m",
        "molt.cli",
        "serve",
        "--root",
        str(ROOT),
        "--host",
        "0.0.0.0",
        "--port",
        str(PORT),
        "--token",
        token,
    ]

    logf = open(LOG, "ab", buffering=0)
    proc = subprocess.Popen(
        cmd,
        cwd=str(PKG),
        env=env,
        stdout=logf,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    PIDFILE.write_text(str(proc.pid))
    print(f"已启动 pid={proc.pid}  端口={PORT}")
    time.sleep(2.0)

    if proc.poll() is not None:
        print(f"[FAIL] 进程已退出，返回码 {proc.returncode}")
        print("--- 日志 ---")
        print(LOG.read_text()[-3000:])
        return 1

    print(f"pid={proc.pid} 仍在运行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
