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
import json
import os
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
DEFAULT_INTERVAL = 10.0
DEFAULT_TIMEOUT = 15.0
DEFAULT_PORT = 6199


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
    timeout: float = DEFAULT_TIMEOUT

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
    port_probe: Optional[Callable[[], bool]] = None
    spawner: Optional[Callable[[list, str], int]] = None
    log: Callable[[str], None] = field(default=_log_line)

    # -- 状态 ------------------------------------------------------
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
        ]

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

    def do_land(self, generation: int) -> StepResult:
        return self._run(self.land_argv(generation), timeout=600)

    def do_switch_on(self) -> StepResult:
        return self._run(self.switch_argv(), timeout=120)

    def do_boot(self) -> StepResult:
        """起 AstrBot。**不起第二个**——本机已经在跑就什么都不做。"""
        if self.port_probe is not None and self.port_probe():
            return StepResult("boot", True, "本机已经在跑（端口在听），不重复起")
        if self.spawner is None:
            return StepResult("boot", False, "没有 spawner")
        try:
            pid = self.spawner(self.boot_argv(), self.settings.log_path)
        except OSError as exc:
            return StepResult("boot", False, f"起不来：{exc}")
        return StepResult("boot", True, f"已拉起 pid={pid}")

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

        if decision.kind == "land":
            if not self.settings.execute:
                self.log(f"  (dry-run) 我会落位 gen-{decision.generation}")
                return decision, steps
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
            if not boot.ok:
                self.log(f"  ✗ 启动失败：{boot.detail}")
                return decision, steps
            state["needs_start"] = False
            state["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.save_state(state)
            self.log(f"  ✓ {boot.detail}")

        return decision, steps

    def run_forever(self, *, once: bool = False) -> int:
        while True:
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


def _default_spawn(argv: list, log_path: str) -> int:
    """脱离本进程起 AstrBot。agent 退出不该带走它。"""
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "ab")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        proc = subprocess.Popen(
            argv, stdout=handle, stderr=handle, stdin=subprocess.DEVNULL,
            creationflags=flags, close_fds=True,
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
        port_probe=lambda: _port_open("127.0.0.1", settings.port),
        spawner=_default_spawn,
    )
    return agent.run_forever(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
