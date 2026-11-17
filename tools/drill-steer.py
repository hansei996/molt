#!/usr/bin/env python3
"""drill-steer —— 演练 `--steer` 这条链路，**默认绝不去打断一个正在跑的回合**。

为什么要有这个脚本
------------------
`docs/VERIFY.md` 的 G 条写着：「`--steer` 打断正在跑的回合」还没验。
不验的原因不是做不到，而是**做了会踢掉别人正在干的活**（可能是主人自己
正在等的那个回答）。所以把这一条做成"可重跑的演练"，而不是"我替你按一次"：

  * 默认只挑**空着的会话**（`running=false`），发一条打了标记的 steer，
    再回读事件流确认它真的被插进去了（`target: next-step`）。
  * 如果这台机器上此刻**没有空会话**，脚本打印清楚并退出 3 ——
    不降级、不硬闯。
  * 想验"打断正在跑的回合"，必须显式 `--allow-running`，而且要在终端里
    手打 `YES`；非交互环境（管道/CI）一律拒绝。

用法
----
    # 1) 先看现在有哪些会话、谁在跑（只读）
    python tools/drill-steer.py --peers
    # 2) 在空会话上演练一次（这才是默认行为）
    python tools/drill-steer.py --shell desktop-h2kgiao
    # 3) 指定会话（仍然会被 running 检查挡住）
    python tools/drill-steer.py --shell desktop-h2kgiao --session session-xxxx
    # 4) 真的要验"打断"（需要人工确认；会打断那个会话正在跑的回合）
    python tools/drill-steer.py --shell desktop-h2kgiao --session session-xxxx --allow-running

这个脚本只调 `molt dsh`（走账本代转），不直接连 DSH，也不碰电源/搬家。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from molt import dshrelay  # noqa: E402  （只借它的 summarize_events，避免两处解析同一份形状）

MARK = "【molt steer 演练】"


def _run(argv: List[str], *, timeout: float = 60.0) -> Dict[str, Any]:
    """跑一条 `molt dsh ... --json`，把 stdout 当 JSON 读。

    不用 shell、不用管道拼接 —— 这个脚本的每一跳都应该是可复现的字面量。
    """
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    text = (proc.stdout or "").strip()
    payload: Any = None
    if text:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
    return {
        "rc": proc.returncode,
        "stdout": text,
        "stderr": (proc.stderr or "").strip(),
        "json": payload,
    }


def _sessions(base_for) -> List[Dict[str, Any]]:
    got = _run(base_for("sessions") + ["--json"])
    if got["rc"] != 0:
        raise SystemExit(f"列会话失败（rc={got['rc']}）：{got['stderr'] or got['stdout']}")
    value = got["json"] or {}
    rows = value.get("items") if isinstance(value, dict) else None
    out: List[Dict[str, Any]] = []
    for item in rows or []:
        out.append(
            {
                "id": str(item.get("sessionId") or ""),
                "running": bool(item.get("running")),
                "cwd": str(item.get("cwd") or ""),
                "updatedAt": float(item.get("updatedAt") or 0),
            }
        )
    return out


def _events(base_for, session: str, *, limit: int = 20) -> List[str]:
    got = _run(base_for("history") + ["--session", session, "--limit", str(limit), "--json"])
    if got["rc"] != 0:
        raise SystemExit(f"读历史失败（rc={got['rc']}）：{got['stderr'] or got['stdout']}")
    value = got["json"] if isinstance(got["json"], dict) else {}
    return dshrelay.summarize_events(value, limit=limit)


def _pick(sessions: List[Dict[str, Any]], *, allow_running: bool) -> Optional[Dict[str, Any]]:
    for item in sessions:
        if allow_running or not item.get("running"):
            return item
    return None


def _confirm_running(item: Dict[str, Any]) -> bool:
    """要打断一个正在跑的回合 —— 必须人在终端里敲 YES。"""
    if not sys.stdin.isatty():
        print("✗ 这个会话正在跑，而当前不是交互终端 —— 拒绝打断。")
        print("  真要验，请人在终端里跑，并加 --allow-running。")
        return False
    print(f"⚠ 这个会话正在跑：{item.get('id')}  {item.get('cwd') or ''}")
    print("  继续会打断它正在做的活。真的要验，请手打 YES：")
    try:
        return input("> ").strip() == "YES"
    except EOFError:
        return False


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="演练 --steer（默认不打断正在跑的回合）")
    ap.add_argument("--shell", default="desktop-h2kgiao", help="账本里的壳名")
    ap.add_argument("--session", default="", help="指定会话；不给就自动挑一个空着的")
    ap.add_argument("--peers", action="store_true", help="只列会话（只读），不发送任何东西")
    ap.add_argument("--plan", action="store_true", help="只打印这一轮会发什么、发给谁（不发送）")
    ap.add_argument("--allow-running", action="store_true", help="允许打断正在跑的回合（需要人工 YES）")
    ap.add_argument("--linger", type=float, default=8.0, help="发完等多久再回读事件流（秒）")
    ap.add_argument("--molt", default="", help="molt 命令；默认用当前解释器跑 -m molt.cli")
    ap.add_argument("--warden-url", default="", help="账本地址（不给就读本地 root）")
    ap.add_argument("--warden-token", default="", help="账本 token")
    args = ap.parse_args(argv)

    # 子命令必须排在 `--shell` 之前（argparse 的 choices 只认第一个位置参数）
    prefix = [sys.executable, "-m", "molt.cli", "--no-color", "dsh"] if not args.molt else [args.molt, "dsh"]
    # 账本的地址/token 属于全局开关，必须排在子命令**之前**
    extras: List[str] = []
    if args.warden_url:
        extras += ["--warden-url", args.warden_url]
    if args.warden_token:
        extras += ["--warden-token", args.warden_token]
    # 注意：这些开关同时挂在 `dsh` 与各个动作的解析器上，而**子解析器的默认值会覆盖
    # 父解析器已经设好的值**（argparse 的老毛病）—— 实测把开关放 `dsh` 后面会被抹成空串，
    # 于是偷偷退回本地账本。所以一律放到动作之后再传。
    pass

    def base_for(action: str) -> List[str]:
        return prefix + [action, "--shell", args.shell] + extras

    sessions = _sessions(base_for)
    running = [s for s in sessions if s.get("running")]
    print(f"账本里 {args.shell} 上共 {len(sessions)} 个会话，其中正在跑 {len(running)} 个。")
    for item in sessions[:10]:
        flag = "在跑" if item.get("running") else "空着"
        print(f"  · {item.get('id')}  {flag}  {item.get('cwd') or ''}")

    if args.peers:
        return 0

    if args.session:
        target = next((s for s in sessions if s.get("id") == args.session), None)
        if target is None:
            print(f"✗ 账本里没有这个会话：{args.session}")
            return 3
    else:
        target = _pick(sessions, allow_running=args.allow_running)
        if target is None:
            print("✗ 这台机器上没有空着的会话。")
            print("  要么等一个回合结束再来，要么人工确认后用 --session 指定正在跑的那个。")
            return 3

    if target.get("running"):
        if not args.allow_running:
            print("✗ 挑中的会话正在跑，而没给 --allow-running —— 拒绝打断。")
            return 4
        if not _confirm_running(target):
            print("已取消：没有发送任何东西。")
            return 5

    text = f"{MARK}这条只用来确认 steer 能插进这条链路，不用做任何事，也不用回复。({time.strftime('%H:%M:%S')})"
    if args.plan:
        print("\n（空转，不会发出任何东西）")
        print(f"  目标壳  {args.shell}")
        print(f"  会话    {target.get('id')}   {'在跑' if target.get('running') else '空着'}")
        print(f"  模式    steer")
        print(f"  内容    {text}")
        print("\n真跑（空会话上）：把 --plan 去掉")
        print("真跑（正在跑的回合，需要人工 YES）：再加 --allow-running")
        return 0
    print(f"\n发送 steer → {target.get('id')}")
    got = _run(base_for("send") + ["--session", str(target.get("id")), "--steer", text])
    print(f"  rc={got['rc']}  {got['stdout'] or got['stderr']}")
    if got["rc"] != 0:
        return got["rc"]

    print(f"等 {args.linger:.0f} 秒再回读事件流……")
    time.sleep(max(0.0, args.linger))
    messages = _events(base_for, str(target.get("id")), limit=20)
    hits = [m for m in messages if MARK in m]
    print(f"  历史里最近的 {len(messages)} 条中，含演练标记的有 {len(hits)} 条。")

    if not hits:
        print("✗ 没在历史里看到那条 steer —— 这条链路这次没成功（别当成功记着）。")
        return 6
    print("✓ 看到了。注意：这只证明 steer 被接受并落进历史；")
    print("  「打断正在跑的回合」这一次" + ("是" if target.get("running") else "没有") + "被真正演练到。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
