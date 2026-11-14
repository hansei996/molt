"""启动一个 AstrBot 实例并保持运行（后台），供 curl 验证控制面。

与 boot-landed.ps1 的区别：那个脚本是"启动 -> 等 -> 停掉 -> 比对指纹"的一次性体检，
这个脚本是"启动 -> 保持"，用完后要自己 kill。

用法：
    python boot-keepalive.py <data_dir> <port> <log_path>
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

SRC = Path(r"G:\molt-p0\astrbot-src")
PY = Path(r"G:\molt-p0\venv313\Scripts\python.exe")


def fingerprint(db: Path):
    if not db.exists():
        return None
    import hashlib

    h = hashlib.sha256()
    with db.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return {"bytes": db.stat().st_size, "sha256": h.hexdigest()[:16].upper()}


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    data_dir = Path(sys.argv[1])
    port = sys.argv[2]
    log_path = Path(sys.argv[3])

    if not data_dir.is_dir():
        print(f"[X] 没有这个目录：{data_dir}")
        return 2

    # ASTRBOT_ROOT 必须是 data 的父目录；cwd 必须是源码目录。
    # 这两条搞反过一次，见 ARCH §13.10.5。
    env = dict(os.environ)
    env["ASTRBOT_ROOT"] = str(data_dir.parent)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    dbs = {
        "data_v4.db": data_dir / "data_v4.db",
        "memory_companion.db": data_dir / "plugin_data" / "astrbot_plugin_memory_companion" / "memory_companion.db",
    }
    before = {name: fingerprint(p) for name, p in dbs.items()}
    print("启动前：")
    for name, fp in before.items():
        print(f"  {name:24s} {fp}")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "wb")
    proc = subprocess.Popen(
        [str(PY), "main.py"],
        cwd=str(SRC),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_CONSOLE if hasattr(subprocess, "CREATE_NEW_CONSOLE") else 0,
    )
    print(f"\n已启动 pid={proc.pid}  端口={port}  日志={log_path}")
    print(f"PIDFILE: {log_path.with_suffix('.pid')}")
    log_path.with_suffix(".pid").write_text(str(proc.pid), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
