#!/usr/bin/env python3
"""确保 NAS 上的三个守护进程在跑：Warden / molt-homer / molt-bounce。

为什么需要它
------------
这台 NAS 是 OpenWrt 式的 procd，没有 systemd，`/etc/rc.local` 是空的。
现在三个进程都是手敲 `python3 tools/start-*.py` 起的 —— **NAS 一重启，
整条迁徙链路就断了**，而且断得很安静：鱼还在 QQ 上说话，但账本没了，
谁也没法搬。

设计上只有两条规矩
-----------------
1. **只补不杀**。在跑就跳过，绝不重启。
   `start-warden.py` 那个脚本是"先杀掉旧的再起新的"，拿它当看门狗用会
   每五分钟把 Warden 打断一次 —— 恰好赶上一轮迁徙就是数据事故。
2. **光看 pid 存在不算数**。
   机器重启之后 pid 从低位重新分配，旧的 pid 文件很可能指到一个完全
   无关的进程上。那时候"看 pid 存活"会说"已经在跑"，于是永远不起。
   所以要用 `/proc/<pid>/cmdline` 确认它真的是那一个。

还有两条是 2026-11-15 真机重启之后补的
-------------------------------------
3. **开机那一刻起的东西会立刻死掉，而日志里写着"已启动"。**
   实测：重启后 `supervise.log` 里有 `warden: 已启动 pid=8869` 三行，
   两分钟后 `--status` 却说三个都没在跑 —— 那时网络还没就绪，
   它们起来就连不上自己，随即退出。**"我拉起了一个进程"不等于
   "那个进程还活着"**，所以启动之后要回头看它一眼。
4. **要等真正的就绪信号，不是等几秒。**
   就绪的判据是"有默认路由"（`/proc/net/route` 里出现 00000000），
   不是"磁盘挂上了"、也不是"过了 30 秒"。

用法
----
    python3 molt-supervise.py              # 补齐缺的，打印做了什么
    python3 molt-supervise.py --status     # 只看，不动
    python3 molt-supervise.py --quiet      # 由 cron 调用时用它，没事就闭嘴
    python3 molt-supervise.py --wait 60    # 先等最多 60 秒就绪（开机/rc.local 用）
"""

import os
import subprocess
import sys
import time
from pathlib import Path

PY = "/usr/bin/python3"
MOLT = Path("/mnt/dm-0/astrbot/_molt")
PKG = MOLT / "pkg"
ROOT = MOLT / "warden-root"
TOKEN_FILE = MOLT / "warden.token"
TOKEN = "molt-dev-token-20261114"

# 起来之后回头看它一眼，等这么久再判生死
START_GRACE = 1.5


class Spec:
    def __init__(self, name, pidfile, log, argv, marker, cwd, env=None):
        self.name = name
        self.pidfile = Path(pidfile)
        self.log = Path(log)
        self.argv = argv
        self.marker = marker
        self.cwd = str(cwd)
        self.env = env or {}


def specs():
    env_molt = {"PYTHONPATH": str(PKG)}
    return [
        Spec(
            "warden",
            MOLT / "warden.pid",
            MOLT / "warden.log",
            [PY, "-u", "-m", "molt.cli", "serve",
             "--root", str(ROOT), "--host", "0.0.0.0",
             "--port", "6186", "--token", TOKEN],
            "molt.cli",
            PKG,
            env_molt,
        ),
        Spec(
            "homer",
            MOLT / "homer.pid",
            MOLT / "homer.log",
            [PY, "-u", str(PKG / "tools" / "molt-homer.py"),
             "--config", str(PKG / "tools" / "molt-homer.json")],
            "molt-homer.py",
            PKG,
        ),
        Spec(
            "bounce",
            MOLT / "bounce.pid",
            MOLT / "bounce.log",
            [PY, "-u", str(PKG / "tools" / "molt-bounce.py"),
             "--config", str(PKG / "tools" / "molt-bounce.json")],
            "molt-bounce.py",
            PKG,
        ),
        # 守夜人：看着容器与 UGOS 的关机通知，出事就往账本写一条告警。
        # 它只喊人，不自动拉起容器 —— 自动拉起会破坏公理 0。
        Spec(
            "nightwatch",
            MOLT / "nightwatch.pid",
            MOLT / "nightwatch.log",
            [PY, "-u", str(PKG / "tools" / "molt-nightwatch.py"),
             "--config", str(PKG / "tools" / "molt-nightwatch.json"), "--quiet"],
            "molt-nightwatch.py",
            PKG,
        ),
    ]


def read_pid(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def cmdline_of(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


def running(spec: Spec) -> int:
    """返回真正在跑的那个 pid，没有就 0。

    `os.kill(pid, 0)` 只回答"这个号有人用"，不回答"是我要的那个人"。
    重启之后这两件事经常不是一回事。
    """
    pid = read_pid(spec.pidfile)
    if pid <= 0:
        return 0
    try:
        os.kill(pid, 0)
    except OSError:
        return 0
    line = cmdline_of(pid)
    if spec.marker not in line:
        return 0
    return pid


def files_ready() -> bool:
    """`/mnt/dm-0` 挂上了没有。磁盘没挂时，下面所有路径都不存在。

    判据必须挂在**稳定的东西**上。原来这里看的是 `molt-bounce.py` —— 2026-11-15
    把 bounce 改名禁用之后，这个函数就永远返回 False，于是整套看护静默失效：
    进程死了也不会被拉起，cron 每两分钟跑一次、每次都安静地什么都不做。
    """
    return (PKG / "molt" / "cli.py").is_file()


def spec_enabled(spec) -> bool:
    """这个守护进程在这台机器上装了没有。

    bounce 被改名成 `.disabled` 之后仍然留在 `specs()` 里；如果不去掉，
    每一轮都会尝试启动一个不存在的文件、然后记一条失败。
    """
    for arg in spec.argv:
        text = str(arg)
        if text.endswith(".py"):
            return Path(text).is_file()
    return True


def network_ready() -> bool:
    """有没有默认路由。

    开机早期 `lo` 也可能是通的，而外网/局域网还没配好 —— 那时 Warden 绑得上，
    但 homer / bounce 连 `127.0.0.1:6186` 会拿到 `Network is unreachable`。
    默认路由是"网络真的起来了"的最小判据。
    """
    try:
        text = Path("/proc/net/route").read_text()
    except OSError:
        return False
    for line in text.splitlines()[1:]:
        fields = line.split()
        # Iface Destination Gateway Flags RefCnt Use Metric Mask ...
        if len(fields) > 1 and fields[1] == "00000000":
            return True
    return False


def wait_ready(timeout: float) -> tuple:
    """等到磁盘和网络都就绪，返回 (ok, 说明)。"""
    deadline = time.monotonic() + timeout
    reason = "磁盘未挂载"
    while True:
        if files_ready():
            reason = "网络未就绪"
            if network_ready():
                return True, "就绪"
        if time.monotonic() >= deadline:
            return False, reason
        time.sleep(1.0)


def ensure_token() -> None:
    """Warden 要读 token 文件；它是账本的唯一钥匙，权限必须是 600。"""
    try:
        if not TOKEN_FILE.exists() or TOKEN_FILE.read_text().strip() != TOKEN:
            TOKEN_FILE.write_text(TOKEN + "\n")
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass


def tail_of(path: Path, lines: int = 3) -> str:
    """日志尾巴。起失败时不说清为什么，等于没说。"""
    try:
        data = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return " / ".join(data[-lines:])


def start(spec: Spec) -> str:
    spec.log.parent.mkdir(parents=True, exist_ok=True)
    handle = open(spec.log, "ab")
    env = dict(os.environ)
    env.update(spec.env)
    try:
        proc = subprocess.Popen(
            spec.argv,
            cwd=spec.cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            start_new_session=True,   # 脱离调用者的会话，SSH 断了也不死
            close_fds=True,
        )
    finally:
        handle.close()
    spec.pidfile.write_text(str(proc.pid))

    # 回头看它一眼：拉起一个进程不等于它活着
    time.sleep(START_GRACE)
    if running(spec):
        return f"{spec.name}: 已启动 pid={proc.pid}"
    return (f"{spec.name}: 启动了 pid={proc.pid}，但它立刻退出了"
            f"（日志：{tail_of(spec.log) or '空'}）")


def main(argv) -> int:
    status_only = "--status" in argv
    quiet = "--quiet" in argv
    wait_for = 0.0
    if "--wait" in argv:
        idx = argv.index("--wait")
        try:
            wait_for = float(argv[idx + 1])
        except (IndexError, ValueError):
            print("--wait 需要一个秒数", flush=True)
            return 2

    if wait_for > 0:
        ok, why = wait_ready(wait_for)
        if not ok:
            # 不是失败：cron 那条每 2 分钟的还会再来。但要说清为什么没动手。
            print(f"未就绪（{why}），这一轮什么都不做", flush=True)
            return 0

    if not status_only and not files_ready():
        if not quiet:
            print("磁盘没挂上，这一轮什么都不做", flush=True)
        return 0

    if not status_only:
        ensure_token()

    out = []
    failed = 0
    for spec in specs():
        if not spec_enabled(spec):
            if status_only:
                out.append(f"{spec.name}: 没装（跳过）")
            continue
        pid = running(spec)
        if pid:
            if status_only:
                out.append(f"{spec.name}: 在跑 pid={pid}")
            continue
        if status_only:
            out.append(f"{spec.name}: 没在跑")
            continue
        try:
            line = start(spec)
        except OSError as exc:
            line = f"{spec.name}: 起不来（{exc}）"
        if "立刻退出" in line or "起不来" in line:
            failed += 1
        out.append(line)

    if out:
        for line in out:
            print(line, flush=True)
    elif not quiet:
        print("三个都在跑，没事可做", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
