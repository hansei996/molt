"""v0.5 T4：陌生人流程演练（可重复跑）。

在一个**全新目录**里，按 docs/INSTALL.md 的步骤从零走一遍：
  1. molt init --role warden
  2. 起一个本地账本
  3. molt init --role agent
  4. molt doctor --config
  5. 守护进程 --once（它真的能读到账本吗）

任何一步不对，这里就会说出来 —— 而不是等别人踩。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
FAIL = []


def step(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def ok(label: str) -> None:
    print(f"  [ok] {label}")


def bad(label: str) -> None:
    FAIL.append(label)
    print(f"  [FAIL] {label}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_status(base: str, token: str, *, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(base + "/status", method="GET")
            req.add_header("X-Molt-Token", token)
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="molt-stranger-"))
    print(f"演练目录：{work}")
    try:
        # ---- 1) 立账本 ----
        step("第 1 步：molt init --role warden")
        r = run([PY, "-m", "molt.cli", "init", "--role", "warden", "--root", str(work / "warden"), "--no-color"], cwd=REPO)
        print(r.stdout.strip()[:800])
        if r.returncode != 0:
            bad(f"init warden rc={r.returncode} {r.stderr[:300]}")
            return 1
        token_file = work / "warden" / "warden.token"
        if not token_file.is_file():
            bad("没有生成 warden.token")
            return 1
        token = token_file.read_text(encoding="utf-8").strip()
        ok(f"token 已生成（{len(token)} 字符）")
        if token not in r.stdout:
            bad("打印的 serve 命令里没有这个 token —— 人照抄会 401")
        else:
            ok("打印的 serve 命令带着这个 token")

        # ---- 2) 起账本 ----
        step("第 2 步：起账本（本地随机端口，模拟甲机器）")
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        log = (work / "warden.log").open("w", encoding="utf-8")
        proc = subprocess.Popen(
            [PY, "-m", "molt.cli", "serve", "--root", str(work / "warden"),
             "--host", "127.0.0.1", "--port", str(port), "--token", token, "--no-color"],
            cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            if not wait_status(base, token):
                bad("账本 20 秒内没有应答 /status")
                print((work / "warden.log").read_text(encoding="utf-8", errors="replace")[-1500:])
                return 1
            ok(f"账本在线：{base}")

            # ---- 3) 生成 agent 配置 ----
            step("第 3 步：molt init --role agent")
            cfg = work / "agent.json"
            r = run([PY, "-m", "molt.cli", "init", "--role", "agent", "--out", str(cfg),
                     "--shell", "rehearsal-box", "--data", str(work / "data"),
                     "--warden-url", base, "--token", token, "--no-color"], cwd=REPO)
            print(r.stdout.strip()[:900])
            if r.returncode != 0:
                bad(f"init agent rc={r.returncode}")
                return 1
            data = json.loads(cfg.read_text(encoding="utf-8-sig"))
            if data.get("execute") is not False:
                bad("execute 默认不是 false —— 装上就会动手")
            else:
                ok("execute 默认 false（只观察）")

            # ---- 4) 自检 ----
            step("第 4 步：molt doctor --config")
            r = run([PY, "-m", "molt.cli", "doctor", "--config", str(cfg), "--role", "agent",
                     "--molt-pkg", str(REPO), "--no-color"], cwd=REPO)
            tail = "\n".join(r.stdout.strip().splitlines()[-8:])
            print(tail)
            if r.returncode != 0:
                bad(f"doctor 对一份刚生成的正确配置返回了 {r.returncode}")
            elif "连得上，token 也对" not in r.stdout:
                bad("doctor 没有确认账本连得上")
            else:
                ok("doctor：加载器读得过 + 账本连得上")

            # ---- 5) 守护进程空跑 ----
            step("第 5 步：守护进程 --once（execute=false 只观察）")
            r = run([PY, str(REPO / "tools" / "molt-agent.py"), "--config", str(cfg), "--once"], cwd=REPO)
            out = (r.stdout or "") + (r.stderr or "")
            print(out.strip()[:800])
            if "HTTP 401" in out or "连不上" in out:
                bad("守护进程读不到账本（凭据或地址问题）")
            elif r.returncode != 0:
                bad(f"守护进程 --once rc={r.returncode}")
            else:
                ok("守护进程真的读到了账本")
            if "Traceback" in out:
                bad("守护进程抛了 Traceback")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()

        # ---- 汇总 ----
        step("演练结论")
        if FAIL:
            print(f"卡住 {len(FAIL)} 处：")
            for f in FAIL:
                print(f"  - {f}")
            return 1
        print("按 INSTALL.md 从零走完，没有卡住 —— 陌生人流程成立。")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
