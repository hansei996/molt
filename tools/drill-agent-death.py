"""演练：agent 静默死亡 → 6191 消失 → 所有跨机路径断掉 → 守卫报警 → 拉回来。

为什么要有这份演练
------------------
2026-11-18 08:03，Windows 上的 `molt-agent` 静默死了。后果不是"少一个进程"，而是：

* 6191 那个代转小口一起没了 → 鱼在 NAS 上**再也操作不了** DSH；
* 心跳停了 → `/molt machine`、自动搬家、关机票据全都失去前提；
* 而当时**没有任何东西会响** —— 直到我 08:10 做文档审计时才发现。

这份演练把那条链**从死到活**再走一遍，并且每一步都要留下判据。它只在本机做：
杀的是本机的 agent，用的是桩级别的观测（账本告警 + /health + 一条 CLI 的错误文案），
**不碰电源、不碰防火墙、不碰 NAS、不搬家**。

它验什么
--------
1. 前置：peer `/health` 200、agent 进程在（不在就没法演练）
2. 杀掉 agent → peer `/health` 不再是 200（**跨机路径真的断了**）
3. 断掉时 CLI 的报错要**能看懂**：退出码非 0、有「连不上」、**没有 Traceback**
4. 跑一次守卫（`-Force` 绕过去重）→ 账本里出现 `agent-dead`
5. 用 Startup 里的 vbs 拉起 agent → peer `/health` 回到 200（记录耗时）
6. 再跑一次守卫 → 账本里出现 `agent-back`

用法
----
    py tools/drill-agent-death.py --plan     # 只打印会做什么（默认也是 plan）
    py tools/drill-agent-death.py --yes      # 真的杀一次、再拉回来

> `--yes` 之外它一律不动手。因为"杀进程"这件事，脚本不该替人决定。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
AGENT_CONFIG = REPO / "tools" / "molt-agent.json"
GUARD = REPO / "tools" / "agent-guard.ps1"
VBS = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "molt-agent.vbs"
PEER_HEALTH = "http://127.0.0.1:6191/health"


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _run(argv: List[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _health() -> Optional[int]:
    try:
        with urllib.request.urlopen(PEER_HEALTH, timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return None


def _agent_pids() -> List[int]:
    script = (
        "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*molt-agent.py*' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    got = _run(["powershell.exe", "-NoProfile", "-Command", script], timeout=40)
    return [int(line.strip()) for line in (got.stdout or "").splitlines() if line.strip().isdigit()]


def _config() -> Dict[str, Any]:
    return json.loads(AGENT_CONFIG.read_text(encoding="utf-8-sig"))


def _alerts(warden: str, token: str) -> List[Dict[str, Any]]:
    request = urllib.request.Request(f"{warden.rstrip('/')}/status", headers={"X-Molt-Token": token})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    status = payload.get("status") or payload
    return list(status.get("alerts") or [])


def _guard_once() -> str:
    got = _run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(GUARD), "-Force"],
        timeout=120,
    )
    return ((got.stdout or "") + "\n" + (got.stderr or "")).strip()


def _wait_health(*, timeout: float = 90.0) -> float:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if _health() == 200:
            return time.monotonic() - started
        time.sleep(1.0)
    return -1.0


class Drill:
    def __init__(self) -> None:
        self.results: List[Dict[str, Any]] = []

    def check(self, name: str, ok: bool, detail: str) -> bool:
        self.results.append({"name": name, "ok": bool(ok), "detail": detail})
        return ok

    @property
    def failed(self) -> List[Dict[str, Any]]:
        return [item for item in self.results if not item["ok"]]


def plan() -> None:
    print("这次演练会按顺序做这些事（都不碰电源 / 防火墙 / NAS）：")
    for line in (
        "1. 前置：读 tools/molt-agent.json，确认 peer /health 200 且 agent 进程存在",
        "2. 杀掉本机 agent（taskkill /T /F）",
        "3. 确认 6191 不再回 200 —— 跨机那条路真的断了",
        "4. 跑一次 `molt dsh sessions`：要求 rc≠0、报错能看懂、没有 Traceback",
        "5. 跑一次守卫（agent-guard.ps1 -Force）→ 账本应出现 agent-dead",
        "6. 用 Startup 里的 molt-agent.vbs 拉起 agent，等 /health 回到 200",
        "7. 再跑一次守卫 → 账本应出现 agent-back",
    ):
        print("  " + line)
    print("\n看清楚后加 --yes 真跑。")


def run(*, warden: str, token: str, shell: str) -> Drill:
    drill = Drill()

    # ---- 1. 前置 ---------------------------------------------------------
    health = _health()
    drill.check("前置 peer /health", health == 200, f"HTTP {health}")
    pids = _agent_pids()
    drill.check("前置 agent 进程", bool(pids), f"pid={pids}")

    # ---- 2. 杀 -----------------------------------------------------------
    for pid in pids:
        _run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=30)
    time.sleep(2.0)
    health = _health()
    drill.check("杀掉之后 peer 不可达", health != 200, f"HTTP {health}")
    drill.check("杀掉之后 agent 进程消失", not _agent_pids(), f"pid={_agent_pids()}")

    # ---- 3. 断掉时 CLI 的报错 -------------------------------------------
    got = _run(
        [sys.executable, "-m", "molt.cli", "--no-color", "dsh", "sessions", "--shell", shell, "--warden-url", warden, "--warden-token", token],
        timeout=180,
    )
    out = (got.stdout or "") + (got.stderr or "")
    drill.check(
        "断链时 CLI 报错能看懂",
        got.returncode != 0 and "连不上" in out and "Traceback" not in out,
        f"rc={got.returncode} {out.strip().splitlines()[-1][:110] if out.strip() else '(空)'}",
    )

    # ---- 4. 守卫告警 -----------------------------------------------------
    # 注意：账本只保留最近 N 条告警，**长度不会一直涨** —— 拿 len(before)/len(after) 比
    # 会在"已经满 5 条"时得出"没有新告警"的错误结论（这一版就踩过）。按时间戳找。
    started_dead = time.time() - 1.0
    guard_out = _guard_once()
    new = [item for item in _alerts(warden, token) if float(item.get("at") or 0) >= started_dead]
    kinds = [str(item.get("kind")) for item in new if str(item.get("kind"))]
    drill.check("守卫报 agent-dead", "agent-dead" in kinds, f"这一轮新告警 kinds={kinds}；guard 输出：{(guard_out.splitlines() or [''])[0][:80]}")

    # ---- 5. 拉回来 -------------------------------------------------------
    started = time.monotonic()
    _run(["wscript.exe", str(VBS)], timeout=30)
    took = _wait_health(timeout=90.0)
    drill.check("拉起后 peer /health 200", took >= 0, f"{'%.1f 秒' % took if took >= 0 else '90 秒内没回来'}（含进程启动）")

    # ---- 6. 恢复告警 -----------------------------------------------------
    started_back = time.time() - 1.0
    guard_out2 = _guard_once()
    new2 = [item for item in _alerts(warden, token) if float(item.get("at") or 0) >= started_back]
    kinds2 = [str(item.get("kind")) for item in new2 if str(item.get("kind"))]
    drill.check("守卫报 agent-back", "agent-back" in kinds2, f"这一轮新告警 kinds={kinds2}；guard 输出：{(guard_out2.splitlines() or [''])[0][:80]}")
    print(f"（这一轮从「拉起」到「恢复告警」用了 {time.monotonic() - started:.1f} 秒）")
    return drill


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(description="演练：agent 死了 → 6191 没了 → 守卫报警 → 拉回来")
    parser.add_argument("--plan", action="store_true", help="只打印步骤（默认行为）")
    parser.add_argument("--yes", action="store_true", help="真的杀一次再拉回来")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not args.yes:
        plan()
        return 0

    cfg = _config()
    warden = str(cfg.get("warden_url") or "")
    token = str(cfg.get("warden_token") or "")
    shell = str(cfg.get("shell") or "")
    if not (warden and token and shell):
        print("配置里缺 warden_url / warden_token / shell，不能演练。")
        return 2

    drill = run(warden=warden, token=token, shell=shell)
    if args.json:
        print(json.dumps({"results": drill.results, "failed": len(drill.failed)}, ensure_ascii=False, indent=2))
    else:
        for item in drill.results:
            print(f"{'✓' if item['ok'] else '✗'} {item['name']} —— {item['detail']}")
        print()
        print("全部通过：agent 死了会被看见、被记下来、也能被拉回来。" if not drill.failed else f"{len(drill.failed)} / {len(drill.results)} 项没过。")
    return 0 if not drill.failed else 1


if __name__ == "__main__":
    sys.exit(main())
