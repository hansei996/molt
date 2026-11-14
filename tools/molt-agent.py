"""molt-agent —— 目标机上的常驻看门人。

## 它解决什么问题

`/molt confirm` 之后，源壳会：闭嘴 → 打包 → 上传 → 交还租约。到这一步，
账本里有了一代新快照，**而鱼在世界上暂时不存在**——它已经离开了 NAS，
但 Windows 上还没有任何东西去把它捡起来。

没有这个 agent，"一条 QQ 消息让它搬家"只完成了一半：NAS 上它哑了，
Windows 上没起来。用户看到的现象是"鱼不见了"。

## 它做什么

循环（默认每 10 秒）问 Warden 一个问题：**最新一代快照是给我的吗？**

是，且账本上没有别人持租约，就依次做三件事：
1. `molt land --generation N` —— 把快照落进本地 data 目录
2. `tools/platform-switch.py on` —— 打开平台适配器（**落位后它默认是关的**）
3. 起 AstrBot —— 用一个脱离本进程的子进程，然后不再管它

## 三条安全约束（都是公理 0 的推论）

- **只有 `target_shell == 本机` 才动**。别人的鱼不归我养。
- **只在 `lease.state == "no-lease"` 时才落位**。有人还攥着租约，说明那次交接
  还没走完；这时候落位就是在别人还没松手时把东西抢过来。
- **本机已经在跑一只，就绝不再起第二只**。这是"绝不两条同时在同一个壳里"。

## 它不是 fencing

fencing 在 Warden 那侧（epoch 单调、存储端拒绝倒退写入）。这个 agent 只是
一个**把已经完成的交接接到"启动"上**的胶水。它读不到账本时什么都不做——
**不猜**。
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

ENCODING = "utf-8-sig"
CONFIG_FILENAME = "molt-agent.json"
STATE_FILENAME = "molt-agent-state.json"
SLEEP_TASK_NAME = "Molt-Sleep"
DEFAULT_INTERVAL = 10.0
DEFAULT_TIMEOUT = 15.0
DEFAULT_PORT = 6199
# 拉起来之后多久之内不再拉第二次。AstrBot 冷启动到 6199 在听实测 20–30 秒，
# 这里给到 180 秒：宁可多等一轮，也不要一个壳里冒出两只鱼。
BOOT_COOLDOWN_SECONDS = 180.0


# ---------------------------------------------------------------- 配置


class ConfigError(Exception):
    """配置文件有问题。说得清楚一点，比让它在循环里静默地什么都不做要好。"""


@dataclass
class Settings:
    """agent 的全部可调项。默认值刻意选成"什么都别做"。"""

    execute: bool = False           # 默认只看不做
    shell: str = ""                 # 本机壳名，必须与账本里的 target_shell 一致
    data_dir: str = ""              # AstrBot 的 data 目录（落位目标）
    molt_pkg: str = ""              # 源码目录，land 从这里跑
    python: str = ""                # 跑 land 的 python（要有 pytest 那个也行）
    boot_script: str = ""           # tools/boot-keepalive.py 的路径
    astrbot_python: str = ""        # 跑 AstrBot 的 python（venv313）
    log_path: str = ""              # AstrBot 的日志落到哪
    platform_switch: str = ""       # tools/platform-switch.py 的路径
    warden_url: str = ""            # http://192.168.1.10:6186
    warden_token: str = ""
    port: int = DEFAULT_PORT        # AstrBot dashboard 端口，用来判断"在不在跑"
    interval: float = DEFAULT_INTERVAL
    shutdown_after_leave: bool = False
    # "sleep" 只挂起（会话不销毁，回来不用登录）；"shutdown" 真关机（回来必须能自动登录）
    power_action: str = "sleep"
    shutdown_delay_seconds: int = 60
    timeout: float = DEFAULT_TIMEOUT
    #: 这台壳自己的 dashboard 监听地址/端口。留空 = 保持快照里原样。
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = DEFAULT_PORT
    #: AstrBot 起来之后顺手拉起来的东西（形如 `[{"argv": [...], "skip_if": "x.exe"}]`）。
    #: 鱼的身体是 AstrBot；这里放的是"人干活要用的东西"，比如 DSH 自己。
    boot_extra: list = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Settings":
        """读配置。文件不存在 = 全默认（execute=False，也就是什么都不做）。"""
        if not path.is_file():
            return cls()
        try:
            raw = path.read_text(encoding=ENCODING)
        except UnicodeDecodeError as exc:
            raise ConfigError(f"{path} 不是 UTF-8：{exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{path} 第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"
            ) from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{path} 的顶层应该是一个对象，实际是 {type(data).__name__}")

        known = {f for f in cls.__dataclass_fields__}  # 未知键忽略，不报错
        return cls(**{k: v for k, v in data.items() if k in known})

    def problems(self) -> list:
        """返回所有让它没资格动手的理由。空列表才算齐。"""
        out: list = []
        if not self.execute:
            out.append("execute 是 false（只观察，不动作）")
        if not self.shell:
            out.append("没写 shell（本机壳名）")
        if not self.data_dir:
            out.append("没写 data_dir（落位目标）")
        if not self.molt_pkg:
            out.append("没写 molt_pkg（源码目录）")
        elif not Path(self.molt_pkg).is_dir():
            out.append(f"molt_pkg 不存在：{self.molt_pkg}")
        if not self.warden_url:
            out.append("没写 warden_url（问不到账本就等于看不见）")
        if not self.warden_token:
            out.append("没写 warden_token")
        if not self.python:
            out.append("没写 python（谁来跑 land）")
        if self.interval <= 0:
            out.append("interval 必须是正数")
        return out


# ---------------------------------------------------------------- 账本


class Warden:
    """只读的账本客户端。`molt/client.py` 也能干，但那要 import 整个包；
    agent 跑在目标机上，越少依赖越不容易坏。"""

    def __init__(self, base_url: str, token: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        if not base_url:
            raise ConfigError("warden_url 是空的")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def heartbeat(self, shell_id: str, *, generation: int = 0, note: str = "") -> bool:
        """告诉账本「我还在」。

        为什么需要：账本其它字段都只记「谁取走了第几代」，没有一处回答
        「现在谁还在跑」。于是鱼被拔电之后，`molt status` 看起来一切正常 ——
        它分不清「鱼死了」和「鱼闲着」。
        失败只返回 False，**绝不抛**：一次心跳丢了不影响任何交接。
        """
        if not shell_id:
            return False
        body = json.dumps(
            {"shell_id": shell_id, "generation": int(generation), "note": note}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/heartbeat",
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
        """拿账本状态。拿不到就抛——**调用方不许把 None 当成"没有新快照"**。"""
        req = urllib.request.Request(
            f"{self.base_url}/status",
            headers={"X-Molt-Token": self.token},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"账本返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"连不上账本 {self.base_url}：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"账本返回的不是 JSON：{exc}") from exc
        if not isinstance(payload, dict) or payload.get("ok") is False:
            raise RuntimeError(f"账本说不行：{payload!r}")
        status = payload.get("status")
        if not isinstance(status, dict):
            raise RuntimeError(f"账本返回里没有 status：{payload!r}")
        return status


# ---------------------------------------------------------------- 判决


@dataclass
class Decision:
    """`decide()` 的判决。`kind` 只有五种，够用且穷尽。"""

    kind: str                      # idle / land / start / wait / error
    reason: str = ""
    generation: int = 0

    def __str__(self) -> str:
        if self.generation:
            return f"[{self.kind}] gen-{self.generation} {self.reason}"
        return f"[{self.kind}] {self.reason}"


def _lease_is_free(status: Dict[str, Any]) -> bool:
    lease = status.get("lease")
    if not isinstance(lease, dict):
        return True
    return not lease.get("holder")


def decide(
    status: Dict[str, Any],
    *,
    shell: str,
    state: Dict[str, Any],
) -> Decision:
    """纯函数：账本状态 + 本机状态 → 该干什么。

    这个函数是整个 agent 里唯一需要被仔细测的东西——其余部分都是"按判决执行"。
    """
    if not shell:
        return Decision("error", "配置里没有 shell，我不知道自己是谁")

    latest = status.get("latest_generation")
    if not isinstance(latest, dict):
        return Decision("idle", "账本里还没有任何一代快照")

    try:
        gen = int(latest.get("generation") or 0)
    except (TypeError, ValueError):
        return Decision("error", f"账本里的 generation 不是整数：{latest.get('generation')!r}")

    target = str(latest.get("target_shell") or "")
    consumed = str(latest.get("consumed_by") or "")

    # 上一轮落了位但没起起来 —— 先把这个债还掉，再去看新的一代。
    # 否则"落位成功、启动失败"会让 agent 永远停在这里，而账本显示一切正常。
    if state.get("needs_start") and int(state.get("landed_generation") or 0) > 0:
        return Decision(
            "start",
            "上一次已经落位但没起来（这次补上）",
            int(state["landed_generation"]),
        )

    if not target:
        return Decision("idle", f"第 {gen} 代没有 target_shell，不是给谁搬的")
    if target != shell:
        return Decision("idle", f"第 {gen} 代是给 {target} 的，不是我（{shell}）")
    if consumed == shell:
        return Decision("idle", f"第 {gen} 代我已经落过了")
    if consumed:
        return Decision("idle", f"第 {gen} 代已经被 {consumed} 落走了")

    if not _lease_is_free(status):
        holder = (status.get("lease") or {}).get("holder")
        return Decision("wait", f"第 {gen} 代是给我的，但 {holder} 还攥着租约", gen)

    return Decision("land", f"第 {gen} 代是给我的，落位", gen)


# ---------------------------------------------------------------- 执行

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""


# ---------------------------------------------------------------- 控制台编码


def _setup_console() -> None:
    """把控制台调成能打中文和 ✓ 的样子。

    Windows 的控制台默认是 GBK（cp936），`print("✓ …")` 会抛
    `UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'`。
    这正是本项目最早的那个 bug（`molt/cli.py` 里同样一段），只是 agent
    是另一个入口，得单独装一次。

    先试着换成 UTF-8；换不了就退一步只改 `errors`，让打不出来的字符变成
    问号，而不是把整个进程带走。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            continue
        except Exception:
            pass
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def _log_line(msg: str) -> None:
    """打一行日志，**永远不抛**。

    一条日志打不出来，绝不该杀死一个正在搬家的进程 —— 2026-11-14 真机上
    它刚好卡在"已经落位、还没启动"的中间态，差点让鱼两边都不在。
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
        print(msg.encode(enc, "replace").decode(enc, "replace"), flush=True)
    except Exception:
        pass


@dataclass
class Agent:
    settings: Settings
    state_path: Path
    runner: Runner = subprocess.run
    status_fetcher: Optional[Callable[[], Dict[str, Any]]] = None
    # 可选：往账本报「我还在」。不注入就什么都不做（测试与 dry-run 用）。
    heartbeat_sender: Optional[Callable[..., bool]] = None
    port_probe: Optional[Callable[..., bool]] = None
    _probe_takes_port: Optional[bool] = None
    spawner: Optional[Callable[[list, str], int]] = None
    #: 本机 AstrBot 的 pid（落位前要先让它停下来）。None = 本机没在跑。
    pid_probe: Optional[Callable[[], Optional[int]]] = None
    #: 让那个 pid 优雅退出。返回 True 表示"它真的没了"。
    stopper: Optional[Callable[[int], bool]] = None
    log: Callable[[str], None] = field(default=_log_line)

    # -- 状态 ------------------------------------------------------
    def _send_heartbeat(self) -> None:
        if self.heartbeat_sender is None:
            return
        try:
            self.heartbeat_sender(
                self.settings.shell,
                generation=int((self.load_state() or {}).get("landed_generation") or 0),
                note="agent",
            )
        except Exception:  # noqa: BLE001 - 心跳永远不该把守护进程弄死
            pass

    def load_state(self) -> Dict[str, Any]:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_state(self, state: Dict[str, Any]) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            tmp.replace(self.state_path)
        except OSError as exc:  # 记不下来不该让 agent 死掉
            self.log(f"  ! 状态写不进去：{exc}")

    # -- 三个动作 --------------------------------------------------
    def land_argv(self, generation: int) -> list:
        s = self.settings
        return [
            s.python, "-m", "molt.cli", "land",
            "--generation", str(generation),
            "--data", s.data_dir,
            "--warden-url", s.warden_url,
            "--warden-token", s.warden_token,
            "--shell", s.shell,
            "--yes",
            "--no-color",
        ] + (
            ["--dashboard-host", s.dashboard_host] if s.dashboard_host else []
        ) + (
            ["--dashboard-port", str(s.dashboard_port)] if s.dashboard_port else []
        )

    def switch_argv(self) -> list:
        s = self.settings
        return [
            s.python, s.platform_switch, "on",
            os.path.join(s.data_dir, "cmd_config.json"),
            "--yes",
        ]

    def boot_argv(self) -> list:
        s = self.settings
        return [s.astrbot_python, s.boot_script, s.data_dir, str(s.port), s.log_path]

    def _run(self, argv: list, *, timeout: float) -> StepResult:
        try:
            proc = self.runner(
                argv,
                cwd=self.settings.molt_pkg or None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return StepResult(argv[0], False, f"超时（{timeout:.0f} 秒）")
        except OSError as exc:
            return StepResult(argv[0], False, f"起不来：{exc}")
        if proc.returncode != 0:
            tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
            return StepResult(argv[0], False, f"退出码 {proc.returncode}：{tail[-1] if tail else ''}")
        return StepResult(argv[0], True)

    def do_stop(self) -> StepResult:
        """落位之前，先让本机那只 AstrBot 停下来。

        和 NAS 侧 homer 的 `do_stop()` 对称 —— 这一步谁都不能省：
        活着的进程手里握着 `-shm` 和对旧 inode 的 fd，`land` 会因此拒绝落位
        （那个护栏是对的：它分不清"有进程在写"和"只是没合并"）。
        2026-11-15 01:10 真机就卡在这儿：NAS 已经把第 9 代推上来了，
        Windows 上的壳还在跑，agent 每 10 秒试一次、每 10 秒失败一次。
        """
        if self.pid_probe is None or self.stopper is None:
            return StepResult("stop", True, "没有 pid_probe/stopper（当成没在跑）")
        try:
            pid = self.pid_probe()
        except OSError as exc:
            return StepResult("stop", False, f"问不出本机 AstrBot 的 pid：{exc}")
        if pid is None:
            return StepResult("stop", True, "本机没有在跑的 AstrBot")
        try:
            gone = self.stopper(pid)
        except OSError as exc:
            return StepResult("stop", False, f"停不下来（pid {pid}）：{exc}")
        if not gone:
            return StepResult("stop", False, f"pid {pid} 没有在超时内退出")
        return StepResult("stop", True, f"已让它优雅退出（pid {pid}）")

    def do_shutdown(self, generation) -> StepResult:
        """鱼已经搬走了 —— 这台机器可以睡了。

        只在「账本里出现了一代，目标不是我」时触发：那说明这只鱼已经在
        另一台机器上落位了，我这边既没有鱼、也不该继续占着电。
        """
        action = (self.settings.power_action or "sleep").strip().lower()
        if action not in ("sleep", "shutdown"):
            return StepResult("shutdown", False, f"power_action 只认 sleep / shutdown，收到 {action!r}")
        if action == "sleep":
            # 睡眠：会话不销毁，唤醒后一切照旧 —— 这是**不需要开机密码**的那条路。
            # 关机和睡眠的区别不是省不省电，是"回来的时候要不要有人在键盘前"。
            if _away_mode_enabled():
                return StepResult(
                    "shutdown", False,
                    "这台机器开着「离开模式」（AwayModeEnabled=1）：睡眠请求会被翻译成"
                    "「屏幕关掉、整机继续跑」，API 还返回成功 —— 看起来睡了其实没睡。"
                    "要真睡，需要一次管理员：tools/molt-sleep-task.ps1 -DisableAwayMode")
            ok, why = _suspend_now()
            if not ok:
                return StepResult("shutdown", False, why)
            return StepResult("shutdown", True,
                              f"机器睡着了（第 {generation} 代已经交给别人；唤醒靠魔术封包）")
        delay = int(self.settings.shutdown_delay_seconds)
        if delay < 0:
            delay = 0
        argv = ["shutdown", "/s", "/t", str(delay),
                "/c", "molt: 鱼已经搬走了，这台机器休息"]
        try:
            proc = self.runner(argv, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            return StepResult("shutdown", False, f"起不来：{exc}")
        code = getattr(proc, "returncode", 0)
        if code != 0:
            err = (getattr(proc, "stderr", "") or "").strip()
            return StepResult("shutdown", False, f"退出码 {code}：{err}")
        return StepResult("shutdown", True,
                          f"{delay} 秒后关机（第 {generation} 代已经交给别人了）")

    def do_checkpoint(self) -> StepResult:
        """停干净之后，把残留的 WAL 合并回主库 —— 不合并 land 会被护栏挡住。

        顺便清掉**崩溃留下的 sidecar**。判据已经由 `do_stop()` 给过了：
        没有 AstrBot 在跑。`-shm` 是 mmap 出来的共享内存索引，只有活着的连接
        才有意义；断电/强杀留下的那一份会永远待在那里，让 `land` 的
        「目标目录还活着」护栏一直拒绝落位 —— 2026-11-15 01:48 真机就卡在
        这里两分钟：机器明明已经重启过，AstrBot 一个进程都没有，
        `-shm` 还在，于是 agent 每 10 秒失败一次。
        """
        report = checkpoint_databases(self.settings.data_dir)
        cleaned = self.do_clean_sidecars()
        if not report:
            detail = "没有待合并的 WAL"
        else:
            stuck = [(n, a) for n, _b, a in report if a]
            if stuck:
                return StepResult("checkpoint", False,
                                  "这些库合不干净：" + "、".join(f"{n} 还剩 {a} 字节" for n, a in stuck))
            detail = "已合并 " + "、".join(f"{n}（{b} → 0）" for n, b, _a in report)
        if cleaned:
            detail += "；清掉崩溃残留 " + "、".join(cleaned)
        return StepResult("checkpoint", True, detail)

    def do_clean_sidecars(self) -> list:
        """删掉 `*.db-shm` 与零字节 `*.db-wal`。**只删这两种**。

        `-shm` 没有独立内容，下一个连接会重建它；非空 `-wal` 里有已提交事务，
        那种必须靠 checkpoint 合并，不能当垃圾删。
        """
        out = []
        root = Path(self.settings.data_dir)
        if not root.is_dir():
            return out
        for pattern in ("*.db-shm", "*.db-wal"):
            for p in root.rglob(pattern):
                # 历史备份目录里的是「上一次落位前的原件」，不是这次要清的东西
                if ".molt-pre-restore-" in str(p):
                    continue
                try:
                    if pattern == "*.db-wal" and p.stat().st_size > 0:
                        continue
                    p.unlink()
                    out.append(p.name)
                except OSError:
                    pass
        return out

    def do_land(self, generation: int) -> StepResult:
        return self._run(self.land_argv(generation), timeout=600)

    def do_switch_on(self) -> StepResult:
        return self._run(self.switch_argv(), timeout=120)

    def _probe_port(self) -> bool:
        """端口上有没有人在听。

        注入的探针有两种签名（`lambda: bool` 和 `lambda port: bool`），
        用签名判断该传几个参数 —— 比"先试一次、抛 TypeError 再试一次"干净：
        那种写法会把探针内部的真 TypeError 也吞成"换个签名再试"。
        """
        raw = self.port_probe
        if raw is None:
            return False
        if self._probe_takes_port is None:
            try:
                params = [
                    x for x in inspect.signature(raw).parameters.values()
                    if x.kind in (x.POSITIONAL_ONLY, x.POSITIONAL_OR_KEYWORD)
                ]
            except (TypeError, ValueError):
                params = []
            self._probe_takes_port = len(params) >= 1
        if self._probe_takes_port:
            return bool(raw(self.settings.port))
        return bool(raw())

    def do_boot(self) -> StepResult:
        """起 AstrBot。**不起第二个**——本机已经在跑就什么都不做。"""
        if self._probe_port():
            return StepResult("boot", True, "本机已经在跑（端口在听），不重复起")
        if self.spawner is None:
            return StepResult("boot", False, "没有 spawner")
        try:
            pid = self.spawner(self.boot_argv(), self.settings.log_path)
        except OSError as exc:
            return StepResult("boot", False, f"起不来：{exc}")
        return StepResult("boot", True, f"已拉起 pid={pid}")

    def do_extras(self) -> list:
        """AstrBot 已经开口之后，把这台壳上"顺手该起来"的东西拉起来。

        和 `do_boot` 的区别：那个起的是**鱼的身体**，这里的每一样起不来都
        **不影响交接结论** —— 鱼已经在说话了，桌面程序起不来不该让它被判失败。

        `skip_if` 是进程映像名：已经在跑就不重复开（DSH 这种单实例程序，
        再拉一次多半只是把已有窗口提到前面，但日志会让人以为起了两个）。
        """
        results: list = []
        for item in self.settings.boot_extra or []:
            argv = item.get("argv") if isinstance(item, dict) else None
            if not argv:
                results.append(StepResult("extra", False, "boot_extra 里有一项没有 argv"))
                continue
            name = item.get("skip_if") or ""
            if name and _process_running(name):
                results.append(StepResult("extra", True, f"{name} 已经在跑，跳过"))
                continue
            if self.spawner is None:
                results.append(StepResult("extra", False, "没有 spawner"))
                continue
            try:
                pid = self.spawner(list(argv), self.settings.log_path)
            except OSError as exc:
                results.append(StepResult("extra", False, f"{argv[0]} 起不来：{exc}"))
                continue
            results.append(StepResult("extra", True, f"{argv[0]} 已拉起 pid={pid}"))
        return results

    # -- 一轮 ------------------------------------------------------
    def run_once(self) -> Tuple[Decision, list]:
        state = self.load_state()
        if self.status_fetcher is None:
            return Decision("error", "没有 status_fetcher"), []
        try:
            status = self.status_fetcher()
        except RuntimeError as exc:
            # 读不到账本就什么都不做。**不猜**。
            return Decision("error", f"读不到账本：{exc}"), []

        decision = decide(status, shell=self.settings.shell, state=state)
        steps: list = []

        # 鱼搬走了就关机 —— 这一代的目标不是我，而且没有租约在飞。
        # decide() 对"这一代不是给我的"返回 idle（也有可能是 wait），两种都算鱼走了；
        # 真正的判据是下面那几个字段，不是这个 kind。
        if self.settings.shutdown_after_leave and decision.kind in ("wait", "idle"):
            gen = status.get("latest_generation") or {}
            target = str(gen.get("target_shell") or "").strip()
            gnum = gen.get("generation")
            lease = status.get("lease") or {}
            # 判据是「没有人持有租约」，不是「lease 这个对象是空的」。
            # 账本在没有租约时返回的是 {"holder": null, "state": "no-lease"}——
            # 一个**非空**的 dict。第一版写的 `not lease` 永远为假，于是关机钩子
            # 从来没触发过（真机 2026-11-15 01:41 抓到）。
            holder = (lease or {}).get("holder")
            if (target and target != self.settings.shell
                    and not holder and state.get("shut_down_for") != gnum):
                if not self.settings.execute:
                    self.log(f"  (dry-run) 我会关机：第 {gnum} 代的目標是 {target}")
                    return decision, steps
                sd = self.do_shutdown(gnum)
                steps.append(sd)
                if sd.ok:
                    state["shut_down_for"] = gnum
                    self.save_state(state)
                    self.log(f"  ✓ {sd.detail}")
                else:
                    self.log(f"  ✗ 关不掉：{sd.detail}")
                return decision, steps

        if decision.kind == "land":
            if not self.settings.execute:
                self.log(f"  (dry-run) 我会落位 gen-{decision.generation}")
                return decision, steps
            stop = self.do_stop()
            steps.append(stop)
            if not stop.ok:
                self.log(f"  ✗ 停不下来：{stop.detail} —— 这一轮不落位")
                return decision, steps
            self.log(f"  ✓ {stop.detail}")
            ck = self.do_checkpoint()
            steps.append(ck)
            if not ck.ok:
                self.log(f"  ✗ {ck.detail} —— 这一轮不落位")
                return decision, steps
            self.log(f"  ✓ {ck.detail}")
            result = self.do_land(decision.generation)
            steps.append(result)
            if not result.ok:
                self.log(f"  ✗ 落位失败：{result.detail}")
                return decision, steps
            state["landed_generation"] = decision.generation
            state["needs_start"] = True
            self.save_state(state)
            self.log(f"  ✓ 落位 gen-{decision.generation}")
            # 落位完就顺手把启动做完，不必等下一轮
            decision = Decision("start", "落位后立刻启动", decision.generation)

        if decision.kind == "start":
            if not self.settings.execute:
                self.log("  (dry-run) 我会打开平台并起 AstrBot")
                return decision, steps
            sw = self.do_switch_on()
            steps.append(sw)
            if not sw.ok:
                self.log(f"  ✗ 打开平台失败：{sw.detail}")
                return decision, steps
            boot = self.do_boot()
            steps.append(boot)
            state["last_boot_at"] = time.time()
            if not boot.ok:
                self.save_state(state)
                self.log(f"  ✗ 启动失败：{boot.detail}")
                return decision, steps
            state["needs_start"] = False
            state["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.save_state(state)
            self.log(f"  ✓ {boot.detail}")
            for extra in self.do_extras():
                steps.append(extra)
                self.log(f"  {'✓' if extra.ok else '!'} {extra.detail}")

        # 自愈：这一代确实是我取走的，但端口上没有人听 —— 那就是"我被断电/被杀掉了"。
        # 不看 `needs_start`：那个欠账只在**我这次落位**时置真，管不到"本来跑得好好的、
        # 被拔了电、重启之后照旧是我"这种情况（真机 2026-11-15 03:29 查出来）。
        # 判据是"现在到底有没有在听"，不是"我以为自己该不该在跑"。
        if decision.kind == "idle" and not steps:
            gen = status.get("latest_generation") or {}
            consumed = str(gen.get("consumed_by") or "").strip()
            gnum = gen.get("generation")
            listening = self._probe_port()
            # 刚拉起来的那一次还没开始听（AstrBot 从开机到 6199 在听要 20 多秒），
            # 如果不设冷却，每 10 秒就再拉一个 —— 真机 03:41 上真的拉出了两个实例。
            # **两只鱼同时在线是本项目唯一的硬红线**，所以这里的冷却必须存在，
            # 而且要比最慢的一次冷启动还长。
            since_boot = time.time() - float(state.get("last_boot_at") or 0)
            cooling = since_boot < BOOT_COOLDOWN_SECONDS
            if consumed == self.settings.shell and not listening and cooling:
                self.log(
                    f"  {int(BOOT_COOLDOWN_SECONDS - since_boot)} 秒前刚拉过 AstrBot，"
                    f"它可能还在启动。这一轮先不重复拉（免得两只鱼同时在线）"
                )
            if consumed == self.settings.shell and not listening and not cooling:
                if not self.settings.execute:
                    self.log(f"  (dry-run) 第 {gnum} 代是我取的，但端口没人听 —— 我会把它拉起来")
                    return decision, steps
                self.log(f"  第 {gnum} 代是我取的，但端口没人听 —— 我去把它拉起来")
                state["last_boot_at"] = time.time()
                self.save_state(state)
                sw = self.do_switch_on()
                steps.append(sw)
                if not sw.ok:
                    self.log(f"  ✗ 打开平台失败：{sw.detail}")
                    return decision, steps
                boot = self.do_boot()
                steps.append(boot)
                self.log(f"  {'✓' if boot.ok else '✗'} {boot.detail}")
                if boot.ok:
                    state["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    self.save_state(state)
                    for extra in self.do_extras():
                        steps.append(extra)
                        self.log(f"  {'✓' if extra.ok else '!'} {extra.detail}")
                return decision, steps

        return decision, steps

    def run_forever(self, *, once: bool = False) -> int:
        while True:
            # 每轮先报一次「我还在」。它失败也不影响这一轮做什么。
            self._send_heartbeat()
            try:
                decision, _ = self.run_once()
            except ConfigError as exc:
                self.log(f"配置有问题：{exc}")
                return 2
            self.log(f"{time.strftime('%H:%M:%S')} {decision}")
            if once:
                return 0
            time.sleep(self.settings.interval)


# ---------------------------------------------------------------- 默认实现


def checkpoint_databases(data_dir: str) -> list:
    """把 data 目录下每个 `.db` 的 WAL 合回主库，返回 [(相对路径, 合并前, 合并后)]。

    和 NAS 侧 `molt-homer.py` 里那份是同一条理由：第三方插件用自己的 SQLite
    连接，AstrBot 的优雅停机不会替它 checkpoint，`-wal` 里留着已提交事务；
    而 `land` 看到非空 `-wal` 必然拒绝落位。容器/进程确实停了之后合并是安全的，
    而且正是让快照能落地的前提。
    """
    out = []
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


def _away_mode_enabled() -> bool:
    """这台机器有没有开「离开模式」。

    开着的话 SetSuspendState 会被翻译成 away mode：屏幕关掉、整机继续跑，
    而 API 依然返回 TRUE。2026-11-15 真机上就是这样，Kernel-Power 59 写着
    「系统正在进入离开模式」——**一次都没睡过**，但每次调用都报成功。
    """
    if os.name != "nt":                       # pragma: no cover
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\Session Manager\Power") as k:
            value, _ = winreg.QueryValueEx(k, "AwayModeEnabled")
            return int(value) != 0
    except (OSError, ValueError):             # pragma: no cover - 读不到就当没开
        return False


def _suspend_now() -> tuple:
    """真的把机器挂起。返回 (成功, 说明)。

    为什么要先 AdjustTokenPrivileges：SetSuspendState 要 SeShutdownPrivilege，
    而这个特权**在令牌里但是被禁用的** —— 普通进程解不开它，调用就静默失败。
    shutdown.exe 自己会做这一步，所以它能用；我们照做。
    """
    if os.name != "nt":                       # pragma: no cover
        return False, "只有 Windows 才这么睡"
    import ctypes
    import ctypes.wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.OpenProcessToken.restype = w.BOOL
    advapi.LookupPrivilegeValueW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p]
    advapi.LookupPrivilegeValueW.restype = w.BOOL
    advapi.AdjustTokenPrivileges.argtypes = [w.HANDLE, w.BOOL, ctypes.c_void_p,
                                             w.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    advapi.AdjustTokenPrivileges.restype = w.BOOL

    class _Luid(ctypes.Structure):
        _fields_ = [("LowPart", w.DWORD), ("HighPart", w.LONG)]

    class _Laa(ctypes.Structure):
        _fields_ = [("Luid", _Luid), ("Attributes", w.DWORD)]

    class _Tp(ctypes.Structure):
        _fields_ = [("PrivilegeCount", w.DWORD), ("Privileges", _Laa * 1)]

    try:
        hp = w.HANDLE()
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0020 | 0x0008,
                                       ctypes.byref(hp)):
            return False, f"OpenProcessToken 失败（{ctypes.get_last_error()}）"
        luid = _Luid()
        if not advapi.LookupPrivilegeValueW(None, "SeShutdownPrivilege", ctypes.byref(luid)):
            return False, f"找不到 SeShutdownPrivilege（{ctypes.get_last_error()}）"
        tp = _Tp()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = 0x0002      # SE_PRIVILEGE_ENABLED
        advapi.AdjustTokenPrivileges(hp, False, ctypes.byref(tp), 0, None, None)
        if ctypes.get_last_error() not in (0,):   # 1300 = 令牌里没有这个特权
            return False, f"没能启用关机特权（{ctypes.get_last_error()}）"
        powr = ctypes.WinDLL("powrprof", use_last_error=True)
        powr.SetSuspendState.argtypes = [w.BOOLEAN, w.BOOLEAN, w.BOOLEAN]
        powr.SetSuspendState.restype = w.BOOLEAN
        if not powr.SetSuspendState(False, False, False):
            return False, f"SetSuspendState 返回 False（{ctypes.get_last_error()}）"
        return True, "已挂起"
    except (OSError, AttributeError) as exc:
        return False, f"挂起失败：{exc}"


def _pid_alive(pid: int) -> bool:
    try:
        cp = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=15.0)
    except (OSError, subprocess.SubprocessError):
        return True          # 问不出来时假设它还活着：多等一轮，别把"还在写"当成"已停"
    return str(pid) in (cp.stdout or "")


def _listening_pid(port: int) -> Optional[int]:
    """哪个进程在听这个端口。

    用 `netstat -ano` 而不是"按命令行找"：AstrBot 的 cmdline 只有 `main.py`，
    data 目录是环境变量传的，按命令行匹配根本认不出是哪一个（2026-11-15 00:32 踩过，
    差点留下一个孤儿实例）。
    """
    try:
        cp = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=20.0)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (cp.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local, state, pid_text = parts[1], parts[3], parts[4]
        if state.upper() != "LISTENING":
            continue
        if local.endswith(":" + str(port)):
            try:
                return int(pid_text)
            except ValueError:
                continue
    return None


def _send_ctrl_c(pid: int, *, timeout: float = 30.0) -> bool:
    """让它走自己的 Ctrl+C 处理，然后等它真的消失。

    三种发法实测过（2026-11-14）：`CTRL_C_EVENT` 在 Windows 上根本不跨进程组，
    等满 60 秒什么也没发生；`CTRL_BREAK_EVENT` 会被当成 SIGBREAK 直接杀掉
    （退出码 0xC000013A，日志里没有 `Shutting down`）——那是最坏的假成功，
    WAL 不会被 checkpoint。唯一有效的是 AttachConsole 之后
    `GenerateConsoleCtrlEvent(0, 0)`。
    """
    if os.name != "nt":                       # pragma: no cover - 仅 Windows 有意义
        return False
    import ctypes

    kernel32 = ctypes.windll.kernel32      # type: ignore[attr-defined]
    ATTACH_PARENT_PROCESS = -1
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(int(pid)):
        return False
    sent = False
    try:
        kernel32.SetConsoleCtrlHandler(None, True)
        sent = bool(kernel32.GenerateConsoleCtrlEvent(0, 0))
    finally:
        kernel32.FreeConsole()
        if kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            kernel32.SetConsoleCtrlHandler(None, False)
    if not sent:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.5)
    return False


def _process_running(image_name: str) -> bool:
    """这台机器上有没有这个映像名的进程。

    用 `tasklist` 而不是 psutil：agent 是纯标准库的，装不了第三方包。
    问不出来时返回 False（"当它没在跑"）—— 重复拉起的代价是多个窗口，
    而错误地当成"已经在跑"的代价是 DSH 根本没起来。
    """
    try:
        cp = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    out = (cp.stdout or "").strip()
    if not out or "No tasks" in out:
        return False
    return image_name.lower() in out.lower()


def _default_spawn(argv: list, log_path: str) -> int:
    """脱离本进程起 AstrBot。agent 退出不该带走它。"""
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "ab")
    flags = 0
    startupinfo = None
    if os.name == "nt":
        # 不能用 DETACHED_PROCESS：那样进程**没有控制台**，事后就再也没有办法
        # 把 Ctrl+C 送进去，只能硬杀 —— 而硬杀不 checkpoint，`land` 会因此拒绝落位。
        # 用一个隐藏的新控制台：既有自己的进程组，又还留着一扇能敲 Ctrl+C 的门。
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NEW_CONSOLE
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0          # SW_HIDE
    try:
        proc = subprocess.Popen(
            argv, stdout=handle, stderr=handle, stdin=subprocess.DEVNULL,
            creationflags=flags, startupinfo=startupinfo, close_fds=True,
        )
    finally:
        handle.close()
    return proc.pid


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main(argv: Optional[list] = None) -> int:
    _setup_console()
    parser = argparse.ArgumentParser(
        prog="molt-agent",
        description="看着账本，把轮到自己的一代快照捡起来并让它在本地开口。",
    )
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent / CONFIG_FILENAME))
    parser.add_argument("--once", action="store_true", help="只跑一轮就退出（第一次跑请用它）")
    parser.add_argument("--dry-run", action="store_true", help="强制只看不做")
    args = parser.parse_args(argv)

    cfg_path = Path(args.config)
    try:
        settings = Settings.load(cfg_path)
    except ConfigError as exc:
        print(f"配置有问题：{exc}")
        return 2
    if args.dry_run:
        settings.execute = False

    for problem in settings.problems():
        print(f"  · {problem}")
    if not settings.shell or not settings.warden_url:
        print("壳名和账本地址是必须的。")
        return 2

    warden = Warden(settings.warden_url, settings.warden_token, timeout=settings.timeout)
    agent = Agent(
        settings=settings,
        state_path=cfg_path.parent / STATE_FILENAME,
        status_fetcher=warden.status,
        heartbeat_sender=warden.heartbeat,
        port_probe=lambda port: _port_open("127.0.0.1", port),
        spawner=_default_spawn,
        pid_probe=lambda: _listening_pid(settings.port),
        stopper=lambda pid: _send_ctrl_c(pid, timeout=settings.timeout * 2),
    )
    return agent.run_forever(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
