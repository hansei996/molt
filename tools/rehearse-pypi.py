"""从 PyPI 装一份 molt，照 docs/INSTALL.md 走一遍，报告哪一步真的走通了。

为什么要有这个脚本：**"装得起来"是一个会退化的性质**。

`tools/rehearse-install.py` 验的是"仓库里的代码能不能装"，它用
`pip install .` 直接装本地目录 —— 所以"发布物里少了一个目录"这类问题它永远
看不见。2026-11-15 就是这么翻的车：守护进程住在仓库 `tools/` 下而不进 wheel，
`pip install .` 一切正常，从 PyPI 装的人却连 `molt init --role agent` 都过不去。

这个脚本只碰**链路上游**：PyPI 上的那个轮子。它不读仓库里的任何代码，
所以仓库怎么改都影响不了它 —— 这正是它有用的原因。

用法：

    python tools/rehearse-pypi.py                 # 装 PyPI 上最新的
    python tools/rehearse-pypi.py --version 1.1.1 # 装指定的
    python tools/rehearse-pypi.py --keep          # 留下 venv 方便进去看

退出码 0 表示十四步全过。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PACKAGE = "molt-handoff"


def say(msg: str) -> None:
    print(msg, flush=True)


def run(argv: list[str], *, cwd: pathlib.Path, timeout: float = 600.0) -> tuple[int, str]:
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def latest_version() -> str:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{PACKAGE}/json", timeout=30) as r:
        return str(json.load(r)["info"]["version"])


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_status(url: str, token: str, *, tries: int = 40) -> str:
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"X-Molt-Token": token})
            with urllib.request.urlopen(req, timeout=1.0) as r:
                return json.dumps(json.loads(r.read().decode("utf-8")), ensure_ascii=False)
        except urllib.error.HTTPError as e:
            return f"HTTP {e.code}"
        except Exception:
            time.sleep(0.25)
    return "TIMEOUT"


def walk(version: str, *, keep: bool) -> int:
    root = pathlib.Path(tempfile.mkdtemp(prefix="molt-pypi-"))
    steps: list[tuple[str, bool, str]] = []
    venv = root / "venv"
    say(f"工作目录：{root}")
    say(f"目标版本：{PACKAGE}=={version}")

    def record(name: str, ok: bool, detail: str = "") -> bool:
        steps.append((name, ok, detail))
        say(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        return ok

    try:
        rc, out = run([sys.executable, "-m", "venv", str(venv)], cwd=root)
        if not record("建 venv", rc == 0, out[-400:]):
            return report(steps)

        vpy = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        exe = venv / ("Scripts/molt.exe" if os.name == "nt" else "bin/molt")

        # 不用本机缓存与镜像：这一轮要证明的是"陌生人能不能装到"，
        # 用一个可能停在旧索引上的缓存，测出来的是"我这台机器装得到"。
        rc, out = run(
            [str(vpy), "-m", "pip", "install", "--disable-pip-version-check", "-q",
             "--no-cache-dir", "--index-url", "https://pypi.org/simple",
             f"{PACKAGE}=={version}"],
            cwd=root,
        )
        if not record(f"pip install {PACKAGE}=={version}", rc == 0, out[-1200:]):
            return report(steps)

        rc, out = run([str(vpy), "-c",
                       "import molt, molt.daemon.agent, molt.daemon.homer; print(molt.__version__)"], cwd=root)
        record("molt.daemon.agent / homer 可导入", rc == 0 and version in out, out.strip()[-400:])

        rc, out = run([str(exe), "--version"], cwd=root)
        record(f"molt --version 打印 {version}", rc == 0 and version in out, out.strip()[-300:])

        rc, out = run([str(exe), "--no-color", "init", "--role", "warden", "--root", "./warden"], cwd=root)
        token_file = root / "warden" / "warden.token"
        if not record("molt init --role warden", rc == 0 and token_file.is_file(), out.strip()[-1200:]):
            return report(steps)
        record("提示里没有 python3（Windows 上那是应用商店占位符）", "python3" not in out,
               "\n".join(l for l in out.splitlines() if "serve" in l)[:300])
        token = token_file.read_text(encoding="utf-8").strip()

        port = free_port()
        log = open(root / "warden.log", "wb")
        serve = subprocess.Popen(
            [str(exe), "--no-color", "serve", "--root", "./warden", "--host", "127.0.0.1",
             "--port", str(port), "--token", token],
            cwd=str(root), stdout=log, stderr=subprocess.STDOUT,
        )
        status = wait_status(f"http://127.0.0.1:{port}/status", token)
        record(f"起账本 127.0.0.1:{port}", "current_epoch" in status, status[:400])

        rc, out = run([str(exe), "--no-color", "init", "--role", "agent",
                       "--out", "./agent.json", "--shell", "my-box",
                       "--data", "./astrbot-data",
                       "--warden-url", f"http://127.0.0.1:{port}", "--token", token], cwd=root)
        if not record("molt init --role agent", rc == 0 and (root / "agent.json").is_file(), out.strip()[-1200:]):
            return report(steps)
        cfg = json.loads((root / "agent.json").read_text(encoding="utf-8-sig"))
        record("生成的配置 execute 默认 false", cfg.get("execute") is False, f"execute={cfg.get('execute')}")

        rc, out = run([str(exe), "--no-color", "doctor", "--config", "./agent.json", "--role", "agent"], cwd=root)
        record("doctor 连得上账本", "连得上" in out and rc == 0, out.strip()[-1200:])

        # 全局开关排在前面也认得 —— 1.1.0 就是在这条上翻的车。
        rc, out = run([str(exe), "--no-color", "agent", "--config", "./agent.json", "--once"], cwd=root)
        record("molt --no-color agent --once 能跑", rc == 0 and "idle" in out, out.strip()[-1200:])

        rc, out = run([str(exe), "--no-color", "timeline", "--demo", "--out", "./demo.html"], cwd=root)
        demo = root / "demo.html"
        record("timeline --demo 出一页（陌生人唯一看得见的东西）",
               rc == 0 and demo.is_file() and demo.stat().st_size > 4000,
               f"{demo.stat().st_size if demo.is_file() else 0} bytes")

        rc, out = run([str(exe), "--no-color", "timeline", "--warden-url", f"http://127.0.0.1:{port}",
                       "--warden-token", token, "--out", "./real.html"], cwd=root)
        record("空账本也能出真实时间轴页", rc == 0, out.strip()[-600:])

        serve.terminate()
        try:
            serve.wait(timeout=5)
        except subprocess.TimeoutExpired:
            serve.kill()
        log.close()
        return report(steps)
    finally:
        if keep:
            say(f"（保留 {root}，进去看）")
        else:
            shutil.rmtree(root, ignore_errors=True)


def report(steps: list[tuple[str, bool, str]]) -> int:
    bad = [(n, d) for n, ok, d in steps if not ok]
    say("")
    for name, _, detail in bad:
        say(f"[FAIL] {name}")
        for line in detail.splitlines()[-6:]:
            say("       " + line)
    total = len(steps)
    say(f"{total - len(bad)}/{total} 通过")
    return 0 if not bad else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 PyPI 装一份 molt 并照 INSTALL.md 走一遍")
    parser.add_argument("--version", default="", help="要装的版本（默认：PyPI 上最新的）")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    args = parser.parse_args(argv)
    version = args.version.strip() or latest_version()
    return walk(version, keep=args.keep)


if __name__ == "__main__":
    raise SystemExit(main())
