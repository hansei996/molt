#!/usr/bin/env python3
"""让鱼"回家待一会儿"之后自己再走出去 —— NAS 侧的往返触发器。

为什么需要它
------------
`/molt go` 让鱼从 NAS 走到 Windows 是自动的；回家也是自动的（homer 接住）。
但"回家之后再出门"这一段，此前只能靠人在手机上发一条消息 ——
因为那会儿 Windows 已经睡了/关了，会话不存在，没有任何进程能发起这件事。

这个脚本补的就是这一格：它跑在 NAS 上（权威侧、永远醒着），
看见鱼回家落地了，等一小会儿，再把它推出去，并顺手发魔术封包。
Windows 那边的 agent 接住之后会自己把 AstrBot 和 DSH 拉起来。

它不是魔法，也不是新的特权：它做的正是人在 QQ 里敲的那两条指令，
走的是同一个 `molt.cli migrate`、同一套租约、同一条"先死后生"。

安全边界
--------
· 只在**账本明确说这一代归我、且我已经消费过**的时候才动（其余一律等）
· 同一代只推一次（记在状态文件里），避免一来一回无限弹跳
· 有租约被别人持着 → 等
· `--dry-run` 只报告不动手

用法
----
    python3 tools/molt-bounce.py --config tools/molt-bounce.json
    python3 tools/molt-bounce.py --config tools/molt-bounce.json --once --dry-run

配置（JSON，UTF-8，可带 BOM）
-----------------------------
    {
      "shell": "nas-shell",
      "data_dir": "/mnt/dm-0/astrbot/data",
      "molt_pkg": "/mnt/dm-0/astrbot/_molt/pkg",
      "python": "python3",
      "warden_url": "http://127.0.0.1:6186",
      "warden_token": "……",
      "target": "win-shell",
      "wake_mac": "AA-BB-CC-DD-EE-FF",
      "wake_host": "192.168.1.20",
      "after_seconds": 90,
      "interval": 10,
      "state_file": "/mnt/dm-0/astrbot/_molt/bounce-state.json"
    }
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ENCODING = "utf-8-sig"
DEFAULT_INTERVAL = 10.0
DEFAULT_AFTER = 90.0
DEFAULT_TIMEOUT = 900.0


# --------------------------------------------------------------------------
# 控制台：宁可显示得难看，也不能因为一个对勾把整轮打死
# （这是 bug #1 / #15 的同一种病，两个入口都栽过）
# --------------------------------------------------------------------------
def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            try:
                stream.reconfigure(errors="replace")                 # type: ignore[attr-defined]
            except Exception:
                pass


def _log_line(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            print(msg.encode(enc, "replace").decode(enc, "replace"), flush=True)
        except Exception:
            pass


class ConfigError(Exception):
    """配置读不懂。不猜，直接说哪里不对。"""


def _as_float(cfg: Dict[str, Any], key: str, default: float) -> float:
    raw = cfg.get(key, default)
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            return float(raw)
        except ValueError:
            raise ConfigError(f"配置里的 {key} 不是数字：{raw!r}")
    return default


@dataclass
class Settings:
    shell: str = ""
    data_dir: str = ""
    molt_pkg: str = ""
    python: str = "python3"
    warden_url: str = "http://127.0.0.1:6186"
    warden_token: str = ""
    target: str = ""
    wake_mac: str = ""
    wake_host: str = ""
    # 推出之前要先让本机的壳闭嘴 —— 否则它一边被搬走、一边继续回消息，
    # 而目标壳一开口就是两条鱼同时在线（公理 0）。
    control_url: str = ""
    control_config: str = ""
    after_seconds: float = DEFAULT_AFTER
    interval: float = DEFAULT_INTERVAL
    state_file: str = ""

    @classmethod
    def load(cls, path: str) -> "Settings":
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"找不到配置文件：{path}")
        try:
            raw = json.loads(p.read_text(encoding=ENCODING))
        except UnicodeDecodeError as exc:
            raise ConfigError(f"{path} 不是 UTF-8：{exc}")
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} 第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}")
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} 的顶层必须是对象")
        cfg = cls(
            shell=str(raw.get("shell", "") or ""),
            data_dir=str(raw.get("data_dir", "") or ""),
            molt_pkg=str(raw.get("molt_pkg", "") or ""),
            python=str(raw.get("python", "python3") or "python3"),
            warden_url=str(raw.get("warden_url", cls.warden_url) or ""),
            warden_token=str(raw.get("warden_token", "") or ""),
            target=str(raw.get("target", "") or ""),
            wake_mac=str(raw.get("wake_mac", "") or ""),
            wake_host=str(raw.get("wake_host", "") or ""),
            control_url=str(raw.get("control_url", "") or ""),
            control_config=str(raw.get("control_config", "") or ""),
            state_file=str(raw.get("state_file", "") or ""),
        )
        cfg.after_seconds = _as_float(raw, "after_seconds", DEFAULT_AFTER)
        cfg.interval = _as_float(raw, "interval", DEFAULT_INTERVAL)
        return cfg

    def problems(self) -> List[str]:
        out: List[str] = []
        for name in ("shell", "data_dir", "molt_pkg", "warden_token", "target"):
            if not getattr(self, name):
                out.append(f"缺 {name}")
        for name in ("data_dir", "molt_pkg"):
            v = getattr(self, name)
            if v and not Path(v).exists():
                out.append(f"{name} 不存在：{v}")
        if self.after_seconds < 0:
            out.append("after_seconds 不能是负数")
        if self.interval <= 0:
            out.append("interval 必须大于 0")
        return out


# --------------------------------------------------------------------------
# 账本
# --------------------------------------------------------------------------
class Warden:
    def __init__(self, base: str, token: str, timeout: float = 15.0) -> None:
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
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"读不到账本 {self.base}/status：{exc}")
        payload = body.get("status") if isinstance(body, dict) else None
        if not isinstance(payload, dict):
            raise RuntimeError(f"账本返回的形状不对：{body!r}")
        return payload


# --------------------------------------------------------------------------
# 判决（纯函数 —— 唯一需要仔细测的东西）
# --------------------------------------------------------------------------
@dataclass
class Decision:
    kind: str            # idle / wait / bounce / error
    reason: str
    generation: Optional[int] = None
    detail: Dict[str, Any] = field(default_factory=dict)


def decide(status: Dict[str, Any], *, shell: str, state: Dict[str, Any]) -> Decision:
    """要不要把鱼推出去。

    规则顺序是有意的：先看"这一代是不是我的、我消费没消费"，
    再看租约 —— 因为"不是给我的"和"给我但还没轮到"结果完全不同。
    """
    if not shell:
        return Decision("error", "配置里没有 shell，我不知道自己是哪只")

    latest = status.get("latest_generation")
    if not isinstance(latest, dict) or not latest:
        return Decision("idle", "账本里还没有任何一代快照")

    raw_gen = latest.get("generation")
    try:
        gen = int(raw_gen)
    except (TypeError, ValueError):
        return Decision("error", f"账本里的 generation 不是整数：{raw_gen!r}")

    target = str(latest.get("target_shell") or "")
    consumed = str(latest.get("consumed_by") or "")

    if target and target != shell:
        return Decision("idle", f"第 {gen} 代是给 {target} 的，不是我", gen)
    if consumed and consumed != shell:
        return Decision("idle", f"第 {gen} 代已经被 {consumed} 消费过了", gen)
    if consumed != shell:
        return Decision("wait", f"第 {gen} 代是给我的，但我还没落地", gen)

    if int(state.get("bounced_generation", 0) or 0) == gen:
        return Decision("idle", f"第 {gen} 代我已经推出去过一次了", gen)

    lease = status.get("lease")
    if isinstance(lease, dict) and lease.get("holder"):
        return Decision("wait", f"租约被 {lease.get('holder')} 持着（公理 0）", gen)

    return Decision("bounce", f"第 {gen} 代已经在我这儿落地了，该把它推回去", gen)


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------
@dataclass
class Bounce:
    settings: Settings
    state_path: Path
    runner: Callable[..., Any] = subprocess.run
    status_fetcher: Optional[Callable[[], Dict[str, Any]]] = None
    sleep: Callable[[float], None] = time.sleep
    log: Callable[[str], None] = _log_line

    # ---- 状态 ----
    def load_state(self) -> Dict[str, Any]:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_state(self, state: Dict[str, Any]) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as exc:                       # pragma: no cover
            self.log(f"  ! 状态存不下来（{exc}）—— 这一轮的结果会丢")

    # ---- argv ----
    def migrate_argv(self) -> List[str]:
        s = self.settings
        argv = [s.python, "-m", "molt.cli", "migrate",
                "--data", s.data_dir,
                "--shell", s.shell,
                "--target", s.target,
                "--warden-url", s.warden_url,
                "--warden-token", s.warden_token,
                "--no-color"]
        # 不给 --control-url 的话，migrate 会在"源壳还在说话"的状态下拍快照；
        # 而它拍完就走人，源壳没人按住 —— 2026-11-15 真机上因此出现过两只都在说话。
        if s.control_url:
            argv += ["--control-url", s.control_url]
        if s.control_config:
            argv += ["--control-config", s.control_config]
        if s.wake_mac:
            argv += ["--wake-mac", s.wake_mac]
        if s.wake_host:
            argv += ["--wake-host", s.wake_host]
        return argv

    # ---- 动作 ----
    def do_bounce(self, generation: int) -> Tuple[bool, str]:
        argv = self.migrate_argv()
        env = dict(os.environ)
        env["PYTHONPATH"] = self.settings.molt_pkg
        self.log(f"  → {' '.join(argv)}")
        try:
            proc = self.runner(argv, cwd=self.settings.molt_pkg, env=env,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=DEFAULT_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"起不来：{exc}"
        code = getattr(proc, "returncode", 0)
        out = (getattr(proc, "stdout", "") or "").strip()
        if code != 0:
            tail = "\n".join(out.splitlines()[-6:])
            return False, f"退出码 {code}\n{tail}"
        return True, f"第 {generation} 代已经推出去"

    def run_once(self, *, dry_run: bool = False) -> Tuple[Decision, List[str]]:
        notes: List[str] = []
        state = self.load_state()
        try:
            status = (self.status_fetcher or Warden(
                self.settings.warden_url, self.settings.warden_token).status)()
        except RuntimeError as exc:
            notes.append(f"读不到账本：{exc}")
            return Decision("wait", "账本问不出来"), notes

        decision = decide(status, shell=self.settings.shell, state=state)
        notes.append(f"[{decision.kind}] {decision.reason}")

        if decision.kind != "bounce":
            return decision, notes

        gen = decision.generation or 0
        if dry_run:
            notes.append(f"(dry-run) 等 {self.settings.after_seconds:g} 秒后我会把它推回 "
                         f"{self.settings.target}")
            return decision, notes

        self.log(f"  等待 {self.settings.after_seconds:g} 秒（给那边收拾的时间）")
        self.sleep(self.settings.after_seconds)

        ok, detail = self.do_bounce(gen)
        notes.append(("✓ " if ok else "✗ ") + detail)
        # 失败也记账：否则每 10 秒重试一次，会把对面的机器反复吵醒
        state["bounced_generation"] = gen
        state["last_ok"] = bool(ok)
        state["last_detail"] = detail
        state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.save_state(state)
        return decision, notes

    def run_forever(self, *, once: bool = False, dry_run: bool = False) -> None:
        while True:
            try:
                Warden(self.settings.warden_url, self.settings.warden_token).heartbeat(
                    self.settings.shell, note="bounce"
                )
            except Exception:  # noqa: BLE001 - 心跳失败不该拦住这一轮
                pass
            try:
                _d, notes = self.run_once(dry_run=dry_run)
                for line in notes:
                    self.log(line)
            except Exception as exc:                 # pragma: no cover - 兜底不退出
                self.log(f"! 这一轮出错了：{exc}")
            if once:
                return
            time.sleep(self.settings.interval)


def main(argv: Optional[List[str]] = None) -> int:
    _setup_console()
    ap = argparse.ArgumentParser(description="鱼回家待一会儿之后，自己再走出去（NAS 侧）")
    ap.add_argument("--config", required=True)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    try:
        cfg = Settings.load(args.config)
    except ConfigError as exc:
        _log_line(f"✗ {exc}")
        return 2

    problems = cfg.problems()
    if problems:
        _log_line("✗ 配置还不完整：")
        for p in problems:
            _log_line(f"    {p}")
        return 2

    state_path = Path(cfg.state_file) if cfg.state_file else Path("/tmp/molt-bounce-state.json")
    _log_line(f"往返触发器：{cfg.shell} → {cfg.target}（等 {cfg.after_seconds:g} 秒）")
    Bounce(settings=cfg, state_path=state_path).run_forever(once=args.once, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
