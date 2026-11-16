"""守着 `molt-agent`：每 5 分钟跑一次 `tools/agent-guard.ps1`，只告警、不拉起。

为什么是"一个常驻的小循环"而不是计划任务
----------------------------------------
理想形态是计划任务（机器级、开机就跑、不看我有没有登录）。但这台机器上
`Register-ScheduledTask` 连"当前用户"都要管理员权限（HRESULT 0x80070005），
而我这会话里没有提权通道。所以退一步：

* `tools/install-agent-guard.ps1` 仍然留着 —— 有一天用管理员跑一次，
  就变成真正的计划任务，这个循环可以不再用；
* 在那之前，用**登录时启动**（和 `molt-agent.vbs` 同一个位置）的这个小循环兜住。

它自己不判断死活：判断在 `molt/agentwatch.py`，读数和上报在
`tools/agent-guard.ps1`。这里只负责"每隔一会儿戳它一下"。

用法：
    python tools/agent-guard-loop.py --interval 300 --log G:\\molt-fish\\logs\\agent-guard.log
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUARD = REPO / "tools" / "agent-guard.ps1"


def _log(path: Path, line: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"[{stamp}] {line}\n")
    except OSError:
        pass


def tick(*, timeout: float = 120.0) -> tuple[int, str]:
    """跑一次 guard。返回 (退出码, 合并后的输出)。异常也变成退出码 130。"""
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(GUARD),
    ]
    try:
        got = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        text = (got.stdout or b"").decode("utf-8", "replace").strip()
        err = (got.stderr or b"").decode("utf-8", "replace").strip()
        return got.returncode, (text + ("\n" + err if err else "")).strip()
    except subprocess.TimeoutExpired:
        return 130, f"guard 超过 {timeout:.0f} 秒没结束"
    except OSError as exc:
        return 130, f"guard 起不来：{exc}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="每 N 秒跑一次 agent-guard")
    ap.add_argument("--interval", type=float, default=300.0, help="间隔秒数（默认 300）")
    ap.add_argument(
        "--log",
        default=str(Path("G:/molt-fish/logs/agent-guard.log")),
        help="日志文件",
    )
    ap.add_argument("--once", action="store_true", help="只跑一次就退出")
    args = ap.parse_args(argv)

    log_path = Path(args.log)
    if not GUARD.is_file():
        _log(log_path, f"找不到 {GUARD} —— 这个循环没有意义，退出")
        return 2

    while True:
        code, text = tick()
        # 只在"有事"时写日志：每 5 分钟一行 "alive" 会把真事故淹掉。
        if code != 0 or "=> alive" not in text:
            _log(log_path, f"exit={code} {text.replace(chr(10), ' | ')}")
        if args.once:
            return code
        time.sleep(max(10.0, args.interval))


if __name__ == "__main__":
    sys.exit(main())
