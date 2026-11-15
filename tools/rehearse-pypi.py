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

退出码 0 表示十五步全过。
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


def wait_status(url: str, token: str, *, tries: int = 120) -> str:
    # 120 × 0.25s = 30 秒。原来是 10 秒 —— 在全新的 venv 里第一次 import
    # 要编译 .pyc、杀毒软件还要扫一遍，冷启动偶尔就超过 10 秒，于是演练
    # 会莫名其妙红一次。**一个会随机红的演练，很快就会被所有人忽略。**
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


def walk(version: str, *, keep: bool, wheel: str = "") -> int:
    root = pathlib.Path(tempfile.mkdtemp(prefix="molt-pypi-"))
    steps: list[tuple[str, bool, str]] = []
    venv = root / "venv"
    say(f"工作目录：{root}")
    spec = wheel or f"{PACKAGE}=={version}"
    # 传了本地 wheel 就装它：这一轮要验的是"还没发出去的这一版装得上吗"。
    # 没有这一步，新版本永远只能在发布之后才第一次被真的装一遍。
    say(f"安装目标：{spec}")

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
        #
        # 但**刚发完版就跑**会撞上另一件事：JSON API 先更新、simple 索引的
        # CDN 边缘还停在旧版本上，pip 会说 "No matching distribution"。那不是
        # 发版失败，是"你跑得太早了"。所以只在这种错上重试，别的错一次就报。
        for attempt in range(6):
            rc, out = run(
                [str(vpy), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                 "--no-cache-dir", *(["--no-deps", spec] if wheel else
                                     ["--index-url", "https://pypi.org/simple", spec])],
                cwd=root,
            )
            # 本地 wheel 不存在索引同步问题：错了就是错了，不重试。
            if rc == 0 or wheel or "No matching distribution" not in out:
                break
            say(f"  （索引还没同步到 {version}，等 20 秒再试：第 {attempt + 1} 次）")
            time.sleep(20)
        if not record(f"pip install {spec}", rc == 0, out[-1200:]):
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
        if "current_epoch" in status:
            detail = status[:400]
        else:
            # 只报 "TIMEOUT" 的话，读的人下一步不知道该看哪儿 ——
            # 起不来和起得慢是两件事，日志尾部能把它们分开。
            log.flush()
            tail = (root / "warden.log").read_text(encoding="utf-8", errors="replace")[-1200:]
            detail = f"{status}\n--- warden.log 尾部 ---\n{tail}"
        record(f"起账本 127.0.0.1:{port}", "current_epoch" in status, detail)

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

        # 1.2 起账本记得"那台机器是什么"。不填也能跑，但这两条命令装出来必须
        # 真的存在、而且真的写进了账本 —— 只验"命令不报错"验不出任何东西。
        rc, out = run([str(exe), "--no-color", "declare",
                       "--warden-url", f"http://127.0.0.1:{port}", "--warden-token", token,
                       "--shell", "my-box", "--role", "desktop", "--gpu", "4070TiS",
                       "--wakeable", "--by", "rehearse"], cwd=root)
        record("molt declare 写下壳属性", rc == 0 and "已登记" in out, out.strip()[-800:])

        rc, out = run([str(exe), "--no-color", "shells",
                       "--warden-url", f"http://127.0.0.1:{port}", "--warden-token", token], cwd=root)
        record("molt shells 读得回来（含 role 与可唤醒）",
               rc == 0 and "desktop" in out and "可唤醒" in out, out.strip()[-800:])

        # 1.2.1 补的：把"它叫不醒"写进账本。这一条必须走**装出来的** exe ——
        # 它是命令行的入口，只测源码路径证明不了装好的那份认这两个开关。
        rc, out = run([str(exe), "--no-color", "declare",
                       "--warden-url", f"http://127.0.0.1:{port}", "--warden-token", token,
                       "--shell", "my-box", "--no-wakeable", "--by", "rehearse"], cwd=root)
        record("molt declare --no-wakeable 写进账本", rc == 0 and "已登记" in out, out.strip()[-800:])

        rc, out = run([str(exe), "--no-color", "shells",
                       "--warden-url", f"http://127.0.0.1:{port}", "--warden-token", token], cwd=root)
        record("molt shells 会把「不可唤醒」印出来（不是空白）",
               rc == 0 and "不可唤醒" in out, out.strip()[-800:])

        # 全局开关排在前面也认得 —— 1.1.0 就是在这条上翻的车。
        rc, out = run([str(exe), "--no-color", "agent", "--config", "./agent.json", "--once"], cwd=root)
        record("molt --no-color agent --once 能跑", rc == 0 and "idle" in out, out.strip()[-1200:])

        # 1.3 起多了插件配置同步。这一段必须走**装出来的** exe，而且必须真的
        # 落一次盘：只验 --help 存在，验不出"发出去的包里有没有那个模块"。
        left = root / "left"
        right = root / "right"
        for where, body in ((left, '{"who": "left"}'), (right, '{"who": "right"}')):
            (where / "plugins" / "alpha").mkdir(parents=True, exist_ok=True)
            (where / "config").mkdir(parents=True, exist_ok=True)
            (where / "config" / "alpha_config.json").write_text(body, encoding="utf-8")
        # 右边还有一个只有它自己装的插件 —— 它两边的配置都不许被碰
        (right / "plugins" / "only_here").mkdir(parents=True, exist_ok=True)
        (right / "config" / "only_here_config.json").write_text('{"x": 1}', encoding="utf-8")

        rc, out = run([str(exe), "--no-color", "publish-plugins",
                       "--data", str(left), "--shell", "left-box",
                       "--warden-url", f"http://127.0.0.1:{port}", "--warden-token", token], cwd=root)
        record("molt publish-plugins 把配置收进账本", rc == 0 and "已收进账本" in out, out.strip()[-800:])

        rc, out = run([str(exe), "--no-color", "sync-plugins",
                       "--data", str(right), "--shell", "right-box", "--from", "left-box",
                       "--warden-url", f"http://127.0.0.1:{port}", "--warden-token", token], cwd=root)
        record("molt sync-plugins 空跑一个字都不写",
               rc == 0 and "一个字都没写" in out and '"right"' in (right / "config" / "alpha_config.json").read_text(encoding="utf-8"),
               out.strip()[-1200:])

        rc, out = run([str(exe), "--no-color", "sync-plugins",
                       "--data", str(right), "--shell", "right-box", "--from", "left-box", "--apply",
                       "--warden-url", f"http://127.0.0.1:{port}", "--warden-token", token], cwd=root)
        after = (right / "config" / "alpha_config.json").read_text(encoding="utf-8")
        record("molt sync-plugins --apply 只写两边都装了的那个",
               rc == 0 and '"left"' in after and (right / "config" / "only_here_config.json").is_file(),
               out.strip()[-1200:] + f" | alpha={after!r}")

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
    # 这里曾经写成 (n, d) 却在下面按三元组解包 —— 于是"全部通过"时它从没被执行过，
    # 第一次真有失败就直接 ValueError。演练脚本自己也要有失败路径的测试。
    bad = [(name, detail) for name, ok, detail in steps if not ok]
    say("")
    for name, detail in bad:
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
    parser.add_argument("--wheel", default="", help="装这个本地 wheel，而不是从 PyPI 装（用来验还没发布的版本）")
    args = parser.parse_args(argv)
    wheel = args.wheel.strip()
    if wheel:
        path = pathlib.Path(wheel).expanduser().resolve()
        if not path.is_file():
            say(f"没有这个 wheel：{path}")
            return 2
        wheel = str(path)
    version = args.version.strip() or ("" if wheel else latest_version())
    if not version and wheel:
        # 从文件名里读版本，免得人再抄一遍（抄错的话最后一条断言会莫名其妙地红）
        stem = pathlib.Path(wheel).name
        version = stem.split("-")[1] if stem.count("-") >= 2 else ""
    if not version:
        say("读不出版本号，用 --version 指定一个")
        return 2
    return walk(version, keep=args.keep, wheel=wheel)


if __name__ == "__main__":
    raise SystemExit(main())
