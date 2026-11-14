#!/usr/bin/env python3
"""NAS 侧的"接住它"——把回家的四步变成一次轮询。

## 它和 `tools/molt-agent.py` 的区别

那个在 Windows 上，等账本出现"给它的"新快照就落位、开平台、起 AstrBot。
这个在 NAS 上，做的是**同一件事的镜像**，只有一处不同：

    NAS 侧落位之前，必须先把生产容器停掉。

因为 NAS 上的 AstrBot 是个容器，`/molt confirm` 只让它**闭嘴**（drain），
进程和数据库连接都还在。这时候 `land` 会把文件换掉，而那个活着的进程手里的 fd
指向**被移走的旧 inode** —— 它继续往旧文件写，账本却以为新一代已经落地了。
**这不是崩溃，是两边各说各话**，比崩溃难查得多。（`land` 从 v0.1 起会自己查
`-shm` 并拒绝，但拒绝之后还是得有人去把容器停掉 —— 这就是它。）

## 它为什么不做"关机"

回家的判据是**账本接受了快照**，不是那台 Windows 关机了。
关机是另一件事，而且它可以被拒绝（有人正在打游戏 / 直播 / 写没保存的文档）。
详见 `docs/ARCH.md` §5.5。

## 它为什么是纯标准库

NAS 宿主上是 python 3.10.7，什么都没装。和 `server.py` / `wake.py` /
`platform-switch.py` 同一条约束。

## 默认只报告，不动手

`execute` 默认 `false`（和 Windows 侧 agent 一致）。要它真的动，得在配置里写 `true`。
理由：一个会在你没盯着的时候把生产容器停掉、换掉数据库、再拉起来的程序，
默认必须是"只说不做"。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ENCODING = "utf-8-sig"
CONFIG_FILENAME = "molt-homer.json"
STATE_FILENAME = "molt-homer-state.json"
DEFAULT_INTERVAL = 10.0
DEFAULT_STOP_TIMEOUT = 90.0
DEFAULT_TIMEOUT = 60.0


# ---------------------------------------------------------------- 控制台

def _setup_console() -> None:
    """把 stdout/stderr 尽量弄成能打中文和符号的。拿不到就算了，绝不为它崩。

    Windows 上是 GBK 控制台（本项目 bug #1 / #15）；POSIX 上多半是 UTF-8，
    但 `LANG=C` 的时候就不是了 —— 而这个脚本会给人看中文，
    打不出来就降级成符号替换，不许抛。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            try:
                stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
            except Exception:
                pass


def _log_line(msg: str) -> None:
    """打一行日志。**打不出来也绝不抛。**

    真机教训（bug #15）：一个"落位已经成功、但还没启动"的中间态，
    被一行 `print()` 的 `UnicodeEncodeError` 打断了 —— 鱼两边都不在。
    日志是给人看的，它没有资格决定这件事的成败。
    """
    try:
        print(msg, flush=True)
        return
    except UnicodeEncodeError:
        pass
    except Exception:
        return
    try:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        safe = msg.encode(enc, "replace").decode(enc, "replace")
        sys.stdout.write(safe + "\n")
        sys.stdout.flush()
    except Exception:
        pass


# ---------------------------------------------------------------- 配置

class ConfigError(Exception):
    """配置读不了 / 不合法。"""


@dataclass
class Settings:
    execute: bool = False
    shell: str = ""
    container: str = "astrbot"
    data_dir: str = "/mnt/dm-0/astrbot/data"
    molt_pkg: str = "/mnt/dm-0/astrbot/_molt/pkg"
    python: str = "python3"
    platform_switch: str = ""
    warden_url: str = "http://127.0.0.1:6186"
    warden_token: str = ""
    interval: float = DEFAULT_INTERVAL
    #: 每隔多少秒把当前状态热备一份推给 Warden（0 = 关掉）。
    #: 备份不进 generations、不占租约、没人来取，所以趁鱼正常说话时跑。
    backup_interval: float = 600.0
    timeout: float = DEFAULT_TIMEOUT
    stop_timeout: float = DEFAULT_STOP_TIMEOUT
    #: 这台壳自己的 dashboard 监听地址/端口。留空 = 保持快照里原样。
    #: 容器里必须是 0.0.0.0（容器内监听 loopback 的话，映射出去的端口后面什么都没有）。
    dashboard_host: str = ""
    dashboard_port: int = 0

    @classmethod
    def load(cls, path: Path) -> "Settings":
        if not path.is_file():
            return cls()
        try:
            raw = path.read_text(encoding=ENCODING)
        except UnicodeDecodeError as exc:
            raise ConfigError(f"{path} 不是 UTF-8：{exc}") from exc
        except OSError as exc:
            raise ConfigError(f"{path} 读不了：{exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{path} 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"
            ) from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{path} 顶层应该是一个对象，实际是 {type(data).__name__}")
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(**kwargs)

    def problems(self) -> List[str]:
        """列出现在还不能干活的原因。空列表 = 可以。"""
        out: List[str] = []
        if not self.execute:
            out.append("execute 是 false —— 现在只报告，不动手")
        if not self.shell:
            out.append("没配 shell（这台机器的壳名）")
        if not self.warden_url:
            out.append("没配 warden_url")
        if not self.warden_token:
            out.append("没配 warden_token")
        if not self.container:
            out.append("没配 container")
        if self.interval <= 0:
            out.append(f"interval 应该是正数，现在是 {self.interval}")
        if self.execute:
            # 只在真要动手时才查这些：只报告模式下路径不对也不该拦住看账本
            if not Path(self.data_dir).is_dir():
                out.append(f"data_dir 不存在：{self.data_dir}")
            if not Path(self.molt_pkg).is_dir():
                out.append(f"molt_pkg 不存在：{self.molt_pkg}")
            if self.platform_switch and not Path(self.platform_switch).is_file():
                out.append(f"platform_switch 不存在：{self.platform_switch}")
        return out


# ---------------------------------------------------------------- 账本

class Warden:
    def __init__(self, base: str, token: str, *, timeout: float = 15.0) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def heartbeat(
        self, shell_id: str, *, generation: int = 0, note: str = "", label: str = ""
    ) -> bool:
        """告诉账本「我还在」。

        为什么需要：账本其它字段都只记「谁取走了第几代」，没有一处回答
        「现在谁还在跑」。于是鱼被拔电之后，`molt status` 看起来一切正常 ——
        它分不清「鱼死了」和「鱼闲着」。
        失败只返回 False，**绝不抛**：一次心跳丢了不影响任何交接。
        """
        if not shell_id:
            return False
        body = json.dumps(
            {
                "shell_id": shell_id,
                "generation": int(generation),
                "note": note,
                "label": label or socket.gethostname(),
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/heartbeat",
            data=body,
            headers={"X-Molt-Token": self.token, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return 200 <= resp.status < 300
        except Exception:  # noqa: BLE001 - 心跳永远不该把守护进程弄死
            return False

    def status(self) -> Dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base}/status",
            headers={"X-Molt-Token": self.token},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
        status = payload.get("status")
        if not isinstance(status, dict):
            raise RuntimeError(f"账本返回的形状不对：{payload!r}")
        return status


# ---------------------------------------------------------------- 判决

@dataclass
class Decision:
    kind: str          # idle | wait | land | error
    generation: int = 0
    reason: str = ""


def decide(status: Dict[str, Any], *, shell: str, state: Dict[str, Any]) -> Decision:
    """看一眼账本，决定这一轮要做什么。**纯函数，好测。**

    顺序是有讲究的：

    1. **欠账优先**（`needs_start`）—— 上一轮停下了却没起来，先把鱼救活再说别的。
       宁可晚一代，也不能让它在世界上消失。
    2. 没有快照 → 什么都不做。
    3. 快照不是给这台机器的 → 什么都不做。
    4. 这一代已经被我消费过 → 什么都不做（幂等）。
    5. 别人正在消费 → 等（不抢）。
    6. 有人还攥着租约 → 等（公理 0）。
    7. 剩下的才是真的该落位。
    """
    if not shell:
        return Decision("error", reason="没配 shell —— 我连自己是谁都不知道")

    if state.get("needs_start"):
        return Decision("land", generation=int(state.get("landed_generation") or 0),
                        reason="上一轮停下了但没起来（补上）")

    latest = status.get("latest_generation")
    if not isinstance(latest, dict):
        return Decision("idle", reason="账本里还没有任何一代快照")

    gen = latest.get("generation")
    if not isinstance(gen, int) or gen <= 0:
        return Decision("error", reason=f"账本里的 generation 不是正整数：{gen!r}")

    target = str(latest.get("target_shell") or "")
    if target and target != shell:
        return Decision("idle", reason=f"第 {gen} 代是给 {target} 的，不是我")

    consumed = str(latest.get("consumed_by") or "")
    if consumed == shell:
        return Decision("idle", reason=f"第 {gen} 代我已经落过了")

    if consumed and consumed != shell:
        return Decision("wait", generation=gen,
                        reason=f"第 {gen} 代已经被 {consumed} 消费，等下一轮")

    lease = status.get("lease") or {}
    holder = str((lease or {}).get("holder") or "")
    if holder:
        return Decision("wait", generation=gen,
                        reason=f"{holder} 还攥着租约（公理 0：不抢）")

    if int(state.get("landed_generation") or 0) == gen:
        return Decision("idle", generation=gen,
                        reason=f"第 {gen} 代我落过了，账本可能还没刷新")

    return Decision("land", generation=gen, reason=f"第 {gen} 代是给我的，该落位了")


# ---------------------------------------------------------------- 干活

def _run(argv: List[str], *, cwd: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT,
         env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def checkpoint_databases(data_dir: str) -> List[Tuple[str, int, int]]:
    """把 data 目录下每个 `.db` 的 WAL 合回主库，返回 [(相对路径, 合并前字节, 合并后字节)]。

    为什么需要它：第三方插件（比如 memory_companion）用自己的 SQLite 连接，
    它不会跟着 AstrBot 的优雅停机一起 checkpoint。于是 `-wal` 里留着已提交的事务，
    而 `molt land` 看到非空 `-wal` 必然拒绝落位 —— 那个护栏是对的，它分不清
    「有进程正在写」和「只是没合并」。

    容器停干净之后这里没有写者，`wal_checkpoint(TRUNCATE)` 是安全的，
    而且正是让快照能落地的前提。**只允许在容器确实停了之后调用。**

    2026-11-15 01:05 真机：memory_companion.db-wal 留着 20,632 字节，
    容器明明已经优雅停机，`land` 却因此拒绝落位，鱼回家卡在这里。
    """
    out: List[Tuple[str, int, int]] = []
    root = Path(data_dir)
    if not root.is_dir():
        return out
    for db in sorted(root.rglob("*.db")):
        if not db.is_file():
            continue
        side = db.with_name(db.name + "-wal")
        try:
            before = side.stat().st_size if side.exists() else 0
        except OSError:
            before = 0
        if before == 0:
            continue
        try:
            conn = sqlite3.connect(str(db), timeout=10.0)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        try:
            after = side.stat().st_size if side.exists() else 0
        except OSError:
            after = 0
        out.append((str(db.relative_to(root)), before, after))
    return out


@dataclass
class Homer:
    settings: Settings
    state_path: Path
    runner: Callable[..., subprocess.CompletedProcess] = _run
    status_fetcher: Optional[Callable[[], Dict[str, Any]]] = None
    log: Callable[[str], None] = _log_line

    # -- 路径与 argv

    def land_argv(self, generation: int) -> List[str]:
        """`land --generation N` 的参数。

        **必须带上 `--warden-url` / `--warden-token`**：归档不在本地磁盘上，
        它在 Warden 的中转区里。第一次真机跑（2026-11-15 00:59）就是漏了这两个，
        `land` 直接退出码 2 ——「✗ --generation 需要 --warden-url / --warden-token」，
        于是容器被停下来、落位失败、又被拉起来，鱼回到了家里却是个哑的。
        """
        argv = [
            self.settings.python, "-m", "molt.cli", "land",
            "--generation", str(generation),
            "--data", self.settings.data_dir,
            "--yes", "--no-color",
        ]
        # **必须带 `--shell`**：`land` 只在同时给了 `--generation` 和 `--shell`
        # 时才向账本登记 `consumed_by`。漏了它，字节明明已经落地、鱼已经在说话，
        # 账本却永远显示「还没人取走」——而 NAS 侧的 molt-bounce 正是靠
        # `consumed_by == 我` 来判断"鱼到家了、可以重新推出去"，
        # 于是它会一直 wait（2026-11-15 01:45 真机抓到）。
        if self.settings.shell:
            argv += ["--shell", self.settings.shell]
        if self.settings.warden_url:
            argv += ["--warden-url", self.settings.warden_url]
        if self.settings.warden_token:
            argv += ["--warden-token", self.settings.warden_token]
        # dashboard 是壳自己的事：从 Windows 回来的快照带着 127.0.0.1:6199，
        # 落进容器之后 WebUI 谁都连不上（2026-11-15 01:02 真机踩过）。
        if self.settings.dashboard_host:
            argv += ["--dashboard-host", self.settings.dashboard_host]
        if self.settings.dashboard_port:
            argv += ["--dashboard-port", str(self.settings.dashboard_port)]
        return argv

    def switch_argv(self) -> List[str]:
        # 这里**不能**用 `Path(...) / "cmd_config.json"`：这个脚本跑在 NAS（POSIX）上，
        # 但测试跑在 Windows 上，Path 会按测试机的分隔符拼出 `\data\cmd_config.json`。
        # 目标路径的写法是固定的，就直接拼，别让"跑测试的那台机器"影响它。
        cfg = self.settings.data_dir.rstrip("/") + "/cmd_config.json"
        return [
            self.settings.python, self.settings.platform_switch, "on",
            cfg, "--yes",
        ]

    def _docker(self, *args: str) -> List[str]:
        return ["docker", *args]

    # -- 状态

    def load_state(self) -> Dict[str, Any]:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_state(self, state: Dict[str, Any]) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            tmp.replace(self.state_path)
        except Exception as exc:
            self.log(f"  ! 状态写不下去：{exc}（下一轮会重来，但会更慢）")

    # -- 容器

    def should_stand_down(self, status: dict) -> bool:
        """鱼已经被对面接走了吗？接走了我就该把自己关掉。

        2026-11-15 01:5x 真机抓到一次公理 0 事故：gen-10 落在我这儿、我起了
        容器并打开了平台；随后 gen-11 被 Windows 接走，我却还开着 —— 两只鱼
        同时在线，任何一条消息都会被回两遍。

        判据必须同时满足三条，缺一条都不动：
          · 最新一代的 target 不是我，而且非空（是明确写给别人的）
          · 那一代的 consumed_by == target（对方真的接走了，不是还没来取）
          · 我的容器确实在跑

        只在 `consumed_by` 落地之后才动手，是为了不抢在对方前面死掉 ——
        先死后生，但"后生"这一半得真的发生过。
        """
        g = status.get("latest_generation") or {}
        target = str(g.get("target_shell") or "")
        consumed = str(g.get("consumed_by") or "")
        if not target or target == self.settings.shell:
            return False
        return consumed == target

    def container_running(self) -> Optional[bool]:
        """`True` 在跑 / `False` 停了 / `None` 问不出来。</summary>"""
        try:
            cp = self.runner(self._docker("inspect", self.settings.container,
                                          "--format", "{{.State.Running}}"),
                             timeout=self.settings.timeout)
        except Exception:
            return None
        if cp.returncode != 0:
            return None
        return cp.stdout.strip().lower() == "true"

    def do_stop(self) -> bool:
        running = self.container_running()
        if running is False:
            self.log(f"  容器 {self.settings.container} 已经停了")
            return True
        if running is None:
            self.log(f"  ! 问不出容器 {self.settings.container} 的状态，不敢往下走")
            return False
        try:
            cp = self.runner(self._docker("kill", "-s", "INT", self.settings.container),
                             timeout=self.settings.timeout)
        except Exception as exc:
            self.log(f"  ! docker kill 起不来：{exc}")
            return False
        if cp.returncode != 0:
            self.log(f"  ! docker kill 失败：{cp.stderr.strip() or cp.stdout.strip()}")
            return False

        deadline = time.monotonic() + self.settings.stop_timeout
        while time.monotonic() < deadline:
            if self.container_running() is False:
                self.log("  ✓ 容器已优雅停机")
                return True
            time.sleep(1.0)
        self.log(f"  ! {self.settings.stop_timeout:.0f} 秒内没有停干净")
        return False

    def do_start(self) -> bool:
        if self.container_running() is True:
            self.log(f"  容器 {self.settings.container} 已经在跑")
            return True
        try:
            cp = self.runner(self._docker("start", self.settings.container),
                             timeout=self.settings.timeout)
        except Exception as exc:
            self.log(f"  ! docker start 起不来：{exc}")
            return False
        if cp.returncode != 0:
            self.log(f"  ! docker start 失败：{cp.stderr.strip() or cp.stdout.strip()}")
            return False
        self.log(f"  ✓ 容器已启动：{cp.stdout.strip()}")
        return True

    # -- 三步

    def do_checkpoint(self) -> bool:
        """容器停干净之后，把残留的 WAL 合并回主库。

        不做这一步，"优雅停机"之后的 `land` 仍然会被自己的护栏挡住 ——
        因为第三方插件的 WAL 不会跟着 AstrBot 一起 checkpoint。
        """
        report = checkpoint_databases(self.settings.data_dir)
        if not report:
            self.log("  目标库里没有待合并的 WAL")
            return True
        stuck: List[str] = []
        for name, before, after in report:
            if after:
                stuck.append(f"{name} 还剩 {after} 字节")
            else:
                self.log(f"  ✓ 合并 WAL：{name}（{before} → 0）")
        if stuck:
            self.log("  ! 这些库的 WAL 合不干净：" + "、".join(stuck))
            return False
        return True

    def do_land(self, generation: int) -> bool:
        argv = self.land_argv(generation)
        env = dict(os.environ)
        env["PYTHONPATH"] = self.settings.molt_pkg
        env.setdefault("TZ", "Asia/Shanghai")
        try:
            cp = self.runner(argv, cwd=self.settings.molt_pkg, env=env,
                             timeout=self.settings.timeout * 10)
        except Exception as exc:
            self.log(f"  ! land 起不来：{exc}")
            return False
        if cp.returncode != 0:
            tail = (cp.stdout or "") + (cp.stderr or "")
            self.log(f"  ! land 失败（退出码 {cp.returncode}）：{tail.strip()[-500:]}")
            return False
        self.log(f"  ✓ 落位完成：第 {generation} 代")
        return True

    def do_switch_on(self) -> bool:
        if not self.settings.platform_switch:
            self.log("  ! 没配 platform_switch —— 平台会一直是关的，鱼不会说话")
            return False
        argv = self.switch_argv()
        try:
            cp = self.runner(argv, timeout=self.settings.timeout)
        except Exception as exc:
            self.log(f"  ! platform-switch 起不来：{exc}")
            return False
        if cp.returncode != 0:
            tail = (cp.stdout or "") + (cp.stderr or "")
            self.log(f"  ! 打开平台失败（退出码 {cp.returncode}）：{tail.strip()[-500:]}")
            return False
        self.log("  ✓ 平台已打开")
        return True

    def observe_argv(self) -> List[str]:
        """让容器里的那只壳自己把"我搬到了哪儿"记进它自己的记忆。

        `--wait` 给到 120 秒：`docker start` 之后容器里要跑二十多秒
        dashboard 才装配完，而 `observe` 的第一步就是等 WebUI 应答
        （端口通了不算，`/api/auth/setup-status` 应答了才算）。
        """
        return [
            self.settings.python, "-m", "molt.cli", "observe",
            "--config", self.settings.data_dir.rstrip("/") + "/cmd_config.json",
            "--base", f"http://127.0.0.1:{self.settings.dashboard_port or 6185}",
            "--wait", "120",
            "--no-color",
        ]

    def do_observe(self) -> bool:
        env = dict(os.environ)
        env["PYTHONPATH"] = self.settings.molt_pkg
        try:
            cp = self.runner(self.observe_argv(), timeout=240, env=env)
        except Exception as exc:  # noqa: BLE001 - 记不住一行档案不该影响交接
            self.log(f"  ! 让它记账时出错：{exc}")
            return False
        if cp.returncode != 0:
            tail = ((cp.stdout or "") + (cp.stderr or "")).strip().splitlines()
            self.log(f"  ! 没记住这一代（退出码 {cp.returncode}）：{tail[-1] if tail else ''}")
            return False
        self.log("  ✓ 它自己记住了这一代")
        return True

    # -- 一轮

    def backup_argv(self) -> List[str]:
        """把当前状态热备一份推给 Warden 的 `backups/`（不占代、不是交接）。"""
        return [
            self.settings.python, "-m", "molt.cli", "backup",
            "--data", self.settings.data_dir,
            "--shell", self.settings.shell,
            "--no-color",
        ]

    def do_backup(self) -> bool:
        env = dict(os.environ)
        env["PYTHONPATH"] = self.settings.molt_pkg
        try:
            cp = self.runner(self.backup_argv(), timeout=300, env=env)
        except Exception as exc:  # noqa: BLE001 - 备份失败不该影响任何交接
            self.log(f"  ! 备份时出错：{exc}")
            return False
        if cp.returncode != 0:
            tail = ((cp.stdout or "") + (cp.stderr or "")).strip().splitlines()
            self.log(f"  ! 备份没推上去（退出码 {cp.returncode}）：{tail[-1] if tail else ''}")
            return False
        self.log("  ✓ 已推一份中途备份（备份不是交接）")
        return True

    def maybe_backup(self, state: Dict[str, Any], status: Dict[str, Any]) -> None:
        """到点了就推一份备份。三个前提缺一不可。
        （和 `molt-agent.py` 里那份是同一条理由：不是给我这一代的、鱼没在
        说话的、没到点的，都不该备份。）
        """
        interval = float(getattr(self.settings, "backup_interval", 0) or 0)
        if interval <= 0 or not self.settings.execute:
            return
        gen = status.get("latest_generation") or {}
        if str(gen.get("consumed_by") or "").strip() != self.settings.shell:
            return
        if not self.container_running():
            return
        now = time.time()
        if now - float(state.get("last_backup_at") or 0) < interval:
            return
        if self.do_backup():
            state["last_backup_at"] = now
            self.save_state(state)

    def run_once(self) -> Dict[str, Any]:
        state = self.load_state()
        try:
            if self.status_fetcher is not None:
                status = self.status_fetcher()
            else:
                status = Warden(self.settings.warden_url, self.settings.warden_token).status()
        except Exception as exc:
            # 账本读不到就什么都不做。宁可晚，不可错。
            self.log(f"  ! 读不到账本：{exc}")
            return {"ok": False, "reason": f"读不到账本：{exc}"}

        decision = decide(status, shell=self.settings.shell, state=state)

        if decision.kind == "error":
            self.log(f"  ! {decision.reason}")
            return {"ok": False, "reason": decision.reason, "kind": "error"}
        if decision.kind == "idle":
            self.log(f"  [{decision.kind}] {decision.reason}")
            # 这一代不是给我的、而且已经被别人接走了 → 我不该还在说话。
            if self.should_stand_down(status):
                running = self.container_running()
                if running:
                    if not self.settings.execute:
                        self.log("  (dry-run) 我会把容器停掉 —— 鱼已经不在我这儿了")
                        return {"ok": True, "kind": "idle", "dry_run": True}
                    self.log("  鱼已经被对面接走了 —— 把我自己关掉，别让两只鱼同时在线")
                    if not self.do_stop():
                        self.log("  ! 没能把自己关掉，下一轮再试")
                        return {"ok": False, "kind": "idle", "reason": "没能停机"}
                    return {"ok": True, "kind": "idle", "stood_down": True}
            self.maybe_backup(state, status)
            return {"ok": True, "kind": "idle"}
        if decision.kind == "wait":
            self.log(f"  [{decision.kind}] {decision.reason}")
            return {"ok": True, "kind": "wait"}

        generation = decision.generation
        self.log(f"  [{decision.kind}] {decision.reason}")

        if not self.settings.execute:
            self.log("  (dry-run) 我会：停容器 → 落位 → 打开平台 → 起容器")
            return {"ok": True, "kind": "land", "dry_run": True, "generation": generation}

        # 1) 停容器。停不掉就不落位 —— 落在一个活着的数据目录上，
        #    那个进程会继续往被移走的旧 inode 写。
        if not self.do_stop():
            self.log("  ! 容器没停下来，这一轮不落位")
            return {"ok": False, "reason": "容器没停下来"}

        # 1.5) 停干净了，把残留的 WAL 合回主库。第三方插件的连接不会跟着
        #      AstrBot 的优雅停机一起 checkpoint，不合并的话 `land` 会拒绝落位。
        if not self.do_checkpoint():
            self.log("  ! WAL 合不干净，这一轮不落位 —— 先把它拉起来")
            if self.do_start():
                pass
            return {"ok": False, "reason": "WAL 合不干净"}

        # 2) **先记账，再冒险**：把"我要起来"写进文件，然后才去动数据。
        #    2026-11-14 真机上就是这一步救了一次 —— agent 在"已经落位、还没启动"
        #    的中间态被一行日志打死，活下来靠的是欠账比动作先落盘。
        state["needs_start"] = True
        self.save_state(state)

        # 3) 落位。
        landed = self.do_land(generation)
        state["landed_generation"] = generation if landed else state.get("landed_generation")
        self.save_state(state)
        if not landed:
            self.log("  ! 落位失败 —— 但先把它拉起来，别让它两边都不在")
            if self.do_start():
                state["needs_start"] = False
                self.save_state(state)
            return {"ok": False, "reason": "落位失败"}

        # 3) 打开平台。它失败不影响"先把容器拉起来"。
        switched = self.do_switch_on()

        # 4) 起容器。
        started = self.do_start()
        if started:
            state["needs_start"] = False
            self.save_state(state)
            # 4.5) 让它自己记一笔"我是第几代到这儿来的"。
            #      它失败**不影响交接结论** —— 鱼已经在说话了，少的只是一行档案。
            self.do_observe()
            self.maybe_backup(state, status)
        return {"ok": bool(landed and switched and started),
                "kind": "land", "generation": generation,
                "switched": switched, "started": started}

    def run_forever(self, *, once: bool = False) -> None:
        while True:
            try:
                Warden(self.settings.warden_url, self.settings.warden_token).heartbeat(
                    self.settings.shell, note="homer"
                )
            except Exception:  # noqa: BLE001 - 心跳失败不该拦住这一轮
                pass
            try:
                self.run_once()
            except Exception as exc:      # noqa: BLE001 - 守护进程不能因为一轮出错就死
                self.log(f"  ! 这一轮炸了：{exc}")
            if once:
                return
            time.sleep(self.settings.interval)


# ---------------------------------------------------------------- 入口

def main(argv: Optional[List[str]] = None) -> int:
    _setup_console()
    ap = argparse.ArgumentParser(description="NAS 侧接住回家的鱼（停容器 → 落位 → 开平台 → 起容器）")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent / CONFIG_FILENAME),
                    help=f"配置文件（默认同目录 {CONFIG_FILENAME}）")
    ap.add_argument("--once", action="store_true", help="只看一轮就退出")
    ap.add_argument("--dry-run", action="store_true", help="强制只报告，不动手")
    args = ap.parse_args(argv)

    cfg_path = Path(args.config)
    try:
        settings = Settings.load(cfg_path)
    except ConfigError as exc:
        _log_line(f"✗ {exc}")
        return 2
    if args.dry_run:
        settings.execute = False

    problems = settings.problems()
    if problems:
        _log_line(f"· 配置：{cfg_path}")
        for p in problems:
            _log_line(f"  - {p}")
        if not settings.warden_url or not settings.warden_token:
            return 1

    state_path = cfg_path.parent / STATE_FILENAME
    homer = Homer(settings=settings, state_path=state_path)
    homer.run_forever(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
