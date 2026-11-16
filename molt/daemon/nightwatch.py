"""守夜人：NAS 上替鱼看着灯的那个人。

为什么要有它
------------
2026-11-16 18:44，这台 NAS 收到一次系统级的「关机通知」：docker 被停、
`astrbot` 容器被 SIGKILL（exit 137）。机器没断电，18:45:10 一整组服务又被
拉回来，但容器因为被"显式停止"过，`unless-stopped` 不会自动起。

结果是：**鱼从 18:45 消失到 22:46**，中间四小时。而这段时间里：

* `homer.log` 一直在写 `[idle] 快照 #36 我已经落过了`
* `warden.log` 一直在写 `✓ 已监听 http://0.0.0.0:6186`
* `molt status` 会说「鱼在 ugreen-776b-8b7896df 上」

没有一条日志是错的，但它们合起来在说谎：**账本记的是"谁取走了快照"，
不是"谁还活着"**。心跳能回答后者，可心跳也停了 —— 停掉的恰恰是上报心跳
的那个容器。

它能做的三件事
--------------
1. **把 docker 的状态变化落盘**（不再依赖会被冲掉的 dmesg/环缓冲）。
2. **盯住 `/data/notifyshutdownflag`**：UGOS 每次发起关机流程都会写它。
3. **发现"账本说鱼在这台机器上，但容器没在跑"时报一条告警**，
   写进账本（`POST /alert`），于是 `/where` 页面和 `molt status` 都会念出来。

它**不做什么**
--------------
**绝不自动拉起容器。** 万一鱼其实已经在 Windows 上活着（公理 0），
这台机器自己把容器起来，就是两只鱼同时在线 —— 那比"停着"糟糕得多。
守夜人的职责是**喊人**，不是替人做决定。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..client import WardenClient

DEFAULT_CONTAINER = "astrbot"
DEFAULT_FLAG_PATH = "/data/notifyshutdownflag"
DEFAULT_INTERVAL = 10.0
DEFAULT_DEDUPE = 600.0
DEFAULT_LEDGER_DEDUPE = 1800.0
DEFAULT_STATE_PATH = "/mnt/dm-0/astrbot/_molt/nightwatch-state.json"
DEFAULT_LOG_PATH = "/mnt/dm-0/astrbot/_molt/nightwatch.log"
DOCKER_TIMEOUT = 15.0


# --------------------------------------------------------------------- 观测


def parse_container_state(raw: str) -> Dict[str, Any]:
    """把 `docker inspect <name>` 的 JSON 解析成我们关心的几个字段。

    读不懂就返回 `running=None`（=「我不知道」），绝不返回 False ——
    "不知道"和"没在跑"会触发完全不同的动作，混成一个就会误报。
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {"running": None, "error": "docker inspect 的输出不是 JSON"}
    if isinstance(data, list):
        if not data:
            return {"running": None, "error": "docker inspect 返回了空列表"}
        data = data[0]
    if not isinstance(data, dict):
        return {"running": None, "error": "docker inspect 的结构不对"}
    state = data.get("State") or {}
    if not isinstance(state, dict):
        return {"running": None, "error": "State 不是对象"}
    running = state.get("Running")
    if not isinstance(running, bool):
        running = None
    return {
        "running": running,
        "status": str(state.get("Status") or ""),
        "exit_code": state.get("ExitCode"),
        "oom_killed": bool(state.get("OOMKilled") or False),
        "started_at": str(state.get("StartedAt") or ""),
        "finished_at": str(state.get("FinishedAt") or ""),
        "restart_count": data.get("RestartCount"),
    }


def inspect_container(
    container: str,
    *,
    runner: Optional[Callable[..., Any]] = None,
    timeout: float = DOCKER_TIMEOUT,
) -> Dict[str, Any]:
    """问 docker「这个容器现在怎么样」。容器不存在时返回 running=False。"""
    run = runner or subprocess.run
    try:
        got = run(
            ["docker", "inspect", container],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {"running": None, "error": "这台机器上没有 docker"}
    except subprocess.TimeoutExpired:
        return {"running": None, "error": "docker inspect 超时"}
    except OSError as e:  # pragma: no cover - 平台相关
        return {"running": None, "error": f"docker inspect 起不来：{e}"}
    out = (getattr(got, "stdout", "") or "").strip()
    if getattr(got, "returncode", 0) != 0 and not out:
        err = (getattr(got, "stderr", "") or "").strip()
        if "No such object" in err or "No such container" in err:
            return {"running": False, "status": "missing", "exit_code": None}
        return {"running": None, "error": err or "docker inspect 失败"}
    return parse_container_state(out)


def read_flag(path: str) -> Dict[str, Any]:
    """看一眼 UGOS 的关机通知文件。"""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return {"exists": False}
    try:
        content = p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        content = ""
    return {"exists": True, "content": content[:80], "mtime": st.st_mtime}


# ------------------------------------------------------------------ 判断


@dataclass
class Alert:
    kind: str
    message: str
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.kind


def _fish_here(ledger: Optional[Dict[str, Any]]) -> Optional[str]:
    """账本说鱼现在在谁那儿？返回壳名；不知道返回 None。

    判据是 `latest_generation.consumed_by` —— 它回答"最后一代被谁取走了"。
    """
    if not ledger:
        return None
    latest = ledger.get("latest_generation") or {}
    if not isinstance(latest, dict):
        return None
    who = str(latest.get("consumed_by") or "").strip()
    return who or None


def decide(
    *,
    container: Dict[str, Any],
    ledger: Optional[Dict[str, Any]],
    flag: Dict[str, Any],
    previous: Dict[str, Any],
    shell: str,
    ledger_error: str = "",
) -> List[Alert]:
    """纯函数：把"看到了什么"变成"要不要喊人"。

    返回的每一条都会写进账本；去重由调用方按 `Alert.key` 处理。
    """
    alerts: List[Alert] = []
    running = container.get("running")
    who = _fish_here(ledger)
    prev_flag = previous.get("flag") or {}

    if ledger is None and ledger_error and previous.get("ledger_ok", True):
        alerts.append(
            Alert(
                "ledger-unreachable",
                f"账本连不上（{ledger_error}）—— 现在我既不知道鱼在哪，也记不了事。",
                {"error": ledger_error},
            )
        )

    if running is False:
        exit_code = container.get("exit_code")
        detail = {
            "status": container.get("status"),
            "exit_code": exit_code,
            "finished_at": container.get("finished_at"),
            "restart_count": container.get("restart_count"),
        }
        if who == shell:
            alerts.append(
                Alert(
                    "fish-down",
                    "账本说鱼在这台机器上，但 astrbot 容器没在跑 —— 它现在不在线。",
                    detail,
                )
            )
        if isinstance(exit_code, int) and exit_code != 0:
            alerts.append(
                Alert(
                    "container-died",
                    f"astrbot 容器的退出码是 {exit_code}"
                    + ("（被判 OOM）" if container.get("oom_killed") else "")
                    + f"，结束于 {container.get('finished_at') or '未知时间'}。",
                    detail,
                )
            )

    if running is True and who and who != shell:
        alerts.append(
            Alert(
                "possible-double",
                f"账本说鱼在 {who} 上，但这台机器的容器正在跑 —— "
                f"如果它也连着平台，就是两只鱼同时在线。",
                {"container": container, "ledger_says": who},
            )
        )

    if flag.get("exists"):
        same = bool(prev_flag.get("exists")) and (
            prev_flag.get("content") == flag.get("content")
            and abs(float(prev_flag.get("mtime") or 0) - float(flag.get("mtime") or 0)) < 0.001
        )
        if previous and not same:
            alerts.append(
                Alert(
                    "shutdown-notice",
                    "系统发过一次关机通知（/data/notifyshutdownflag 变了）："
                    f"{flag.get('content')!r} —— 它现在还在，说明那次关机没走完。",
                    {"flag": flag},
                )
            )

    return alerts


# ------------------------------------------------------------------ 主循环


@dataclass
class Settings:
    shell: str = ""
    container: str = DEFAULT_CONTAINER
    flag_path: str = DEFAULT_FLAG_PATH
    warden_url: str = "http://127.0.0.1:6186"
    warden_token: str = ""
    warden_token_file: str = ""
    interval_seconds: float = DEFAULT_INTERVAL
    dedupe_seconds: float = DEFAULT_DEDUPE
    state_path: str = DEFAULT_STATE_PATH
    log_path: str = DEFAULT_LOG_PATH
    execute: bool = True

    @classmethod
    def load(cls, path: Optional[str]) -> "Settings":
        data: Dict[str, Any] = {}
        if path:
            p = Path(path)
            if not p.is_file():
                raise SystemExit(f"找不到配置：{path}")
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        unknown = [k for k in data if k not in allowed]
        if unknown:
            raise SystemExit(f"不认识的配置项：{', '.join(sorted(unknown))}")
        s = cls(**{k: v for k, v in data.items() if k in allowed})
        if not s.shell:
            raise SystemExit("配置里必须写 shell（这台账本里用哪个壳名）")
        if not s.warden_token and s.warden_token_file:
            s.warden_token = Path(s.warden_token_file).read_text(encoding="utf-8").strip()
        return s


def load_state(path: str) -> Dict[str, Any]:
    p = Path(path)
    try:
        got = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def save_state(path: str, state: Dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".part")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _now_text(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts if ts is not None else time.time()))


def run_once(
    settings: Settings,
    *,
    client: Any = None,
    state: Dict[str, Any],
    sender: Optional[Callable[[str, str, str, Dict[str, Any]], bool]] = None,
    inspector: Optional[Callable[[str], Dict[str, Any]]] = None,
    quiet: bool = False,
) -> List[Alert]:
    """看一轮：读 docker、读账本、读 flag，把该喊的喊出去。"""
    warden = client if client is not None else WardenClient(
        settings.warden_url, settings.warden_token
    )
    ledger: Optional[Dict[str, Any]] = None
    ledger_error = ""
    try:
        ledger = warden.status() if hasattr(warden, "status") else warden
    except Exception as e:  # noqa: BLE001 - 账本连不上不该把守夜人弄死
        ledger_error = f"{type(e).__name__}: {e}"

    container = (inspector or inspect_container)(settings.container)
    flag = read_flag(settings.flag_path)

    alerts = decide(
        container=container,
        ledger=ledger,
        flag=flag,
        previous=state,
        shell=settings.shell,
        ledger_error=ledger_error,
    )

    sent_keys: Dict[str, float] = dict(state.get("sent") or {})
    now = time.time()
    # `delivered` 是"这一轮该喊的话"；`execute=False` 时只报不喊，
    # 这样 `--dry-run` 能看到"如果真跑会说什么"，而账本一个字不动。
    delivered: List[Alert] = []
    for alert in alerts:
        last = float(sent_keys.get(alert.key) or 0)
        if now - last < settings.dedupe_seconds:
            continue
        if not settings.execute:
            delivered.append(alert)
            continue
        ok = True
        try:
            if sender is not None:
                ok = bool(sender(alert.kind, alert.message, settings.shell, alert.detail))
            else:
                warden.alert(
                    alert.kind, alert.message, shell=settings.shell, detail=alert.detail
                )
        except Exception as e:  # noqa: BLE001 - 发不出去也要留痕
            ok = False
            if not quiet:
                print(f"! 告警没送进账本（{type(e).__name__}: {e}），只写本地日志", flush=True)
        if ok:
            # 只有真的送出去了才记时间戳：否则一次网络抖动会让这条告警
            # 在接下来的冷却窗口里彻底沉默。
            sent_keys[alert.key] = now
            delivered.append(alert)

    # 落盘：状态 + 一行流水（不依赖账本，账本死了也能回看）
    state["sent"] = sent_keys
    state["container"] = container
    state["flag"] = flag
    state["ledger_ok"] = ledger is not None
    state["checked_at"] = now
    state["checked_at_readable"] = _now_text(now)
    for alert in delivered:
        _append_log(settings.log_path, f"{_now_text(now)} [{alert.kind}] {alert.message}")
    return delivered


def _append_log(path: str, line: str) -> None:
    if not path:
        return
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(line.rstrip("\n") + "\n")
    except OSError:  # pragma: no cover - 写不了日志不该把守夜人弄死
        pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="molt-nightwatch",
        description="守夜人：看着 NAS 上的容器、关机通知，以及'账本说鱼在这，可它没在跑'。",
    )
    parser.add_argument("--config", help="配置文件（JSON）")
    parser.add_argument("--once", action="store_true", help="只看一轮就退出")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写账本、不落状态")
    parser.add_argument("--quiet", action="store_true", help="没事就别说话")
    args = parser.parse_args(argv)

    settings = Settings.load(args.config)
    if args.dry_run:
        settings.execute = False
    state = load_state(settings.state_path)

    if not args.quiet:
        print(
            f"守夜人起来了：壳 {settings.shell}，容器 {settings.container}，"
            f"每 {settings.interval_seconds:g} 秒看一眼",
            flush=True,
        )
    if args.once:
        got = run_once(settings, state=state, quiet=args.quiet)
        for alert in got:
            print(f"! [{alert.kind}] {alert.message}", flush=True)
        if not args.dry_run:
            save_state(settings.state_path, state)
        return 0

    while True:
        try:
            got = run_once(settings, state=state, quiet=args.quiet)
            for alert in got:
                print(f"! [{alert.kind}] {alert.message}", flush=True)
            if settings.execute:
                save_state(settings.state_path, state)
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001 - 守夜人自己不能死在一次读失败上
            print(f"! 这一轮出错：{type(e).__name__}: {e}", flush=True)
        time.sleep(max(1.0, float(settings.interval_seconds)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
