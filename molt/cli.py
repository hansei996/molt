"""molt 命令行。

## 为什么先做 CLI 而不是 API

因为 v0.1 的目标是**证明这条路能走通**，不是"提供服务"。
CLI 是验证成本最低的形态：

  - 不用起服务、不用配端口、不用担心防火墙
  - 每一步的输出都能被人直接看见（这比日志好看得多）
  - 出问题时人可以手动重跑那一条命令

API 在 v0.2 做（Warden 需要一个 HTTP 面给远端壳调用）。

## 命令设计的核心原则

**所有权衡都显式化。** 这个项目里所有"看起来可以默认处理"的地方，
都刻意做成需要明确输入：

  - `checkout` 不给目标就报错，不会"猜一个"
  - `preflight` 发现 arm 丢失时不会自动修，而是明确告诉你该跑什么
  - `push` 校验不过就拒绝，不提供 `--force`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__, initcfg, landcfg, machines, pluginsync
from .backend import LocalBackend, RemoteBackend, open_backend
from .language import move_count, move_count_after, snapshot_tag
from .epoch import DEFAULT_LEASE_TTL_SECONDS
from .errors import (
    EpochAhead,
    EpochStale,
    Fatal,
    LeaseExpired,
    LeaseHeld,
    MoltError,
    NeedsHuman,
    ProtocolMismatch,
    SelfHealable,
)
from .shell import (
    Capabilities,
    ShellIdentity,
    preflight,
    probe_capabilities,
    probe_identity,
)
from .soul import (
    Artifact,
    Manifest,
    collect_memory_dirs,
    collect_runtime_pip,
    copy_memory_dir,
    copy_plugin_configs,
    hot_backup,
    sha256_of,
    sqlite_check,
)
from .transfer import extract_archive, make_archive, restore_into
from . import render
from .render import render_timeline
from .overlay import (
    DEFAULT_DASHBOARD_PORT,
    apply_overlay,
    load_config,
    overlay_for_windows,
    save_config,
)
from .warden import DEFAULT_ROOT, Warden
from .server import DEFAULT_PORT
from .version import PROTOCOL_VERSION
from . import wake as W
from .control import AstrBotControl, CONTROL_PREFIX


# --------------------------------------------------------------------------
# 输出小工具
# --------------------------------------------------------------------------


class C:
    """ANSI 颜色 + 一组在编码受限的控制台上会自动降级的符号。

    ## 为什么要做降级（这是开源项目的必修课）

    Windows 中文版的默认控制台代码页是 **GBK (cp936)**。`print("✓")`
    在这种终端上直接抛 `UnicodeEncodeError: 'gbk' codec can't encode
    character '\\u2713'`。

    也就是说：一个中文用户在中文 Windows 上双击第一次运行 molt，
    看到的是一串 traceback。这不能接受。

    解法分两层：
      1. 先把 stdout/stderr 切到 UTF-8（Python 3.7+ 的 reconfigure）；
         现代 Windows Terminal 与 conhost 都支持，切完就正常了。
      2. 切不动（老终端、被重定向到文件、CI 的奇怪环境）就把符号
         全部换成 ASCII 等价物。**能看懂比好看重要。**
    """

    R = "\033[0m"
    B = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GRN = "\033[32m"
    YEL = "\033[33m"
    BLU = "\033[34m"
    MAG = "\033[35m"
    CYA = "\033[36m"

    # 符号（可能被降级成 ASCII）
    S_OK = "✓"
    S_BAD = "✗"
    S_WARN = "!"
    S_INFO = "·"
    S_LINE = "─"
    S_BULLET = "·"

    @classmethod
    def off(cls) -> None:
        for k in ("R", "B", "DIM", "RED", "GRN", "YEL", "BLU", "MAG", "CYA"):
            setattr(cls, k, "")

    @classmethod
    def ascii_mode(cls) -> None:
        """把符号换成 ASCII 等价物。"""
        cls.S_OK = "[OK]"
        cls.S_BAD = "[X]"
        cls.S_WARN = "[!]"
        cls.S_INFO = "-"
        cls.S_LINE = "-"
        cls.S_BULLET = "*"


def _setup_console() -> None:
    """把控制台切到 UTF-8；切不动就降级成 ASCII 符号。

    这一步必须在任何输出之前调用。它决定这个程序在
    "中文 Windows + cmd.exe" 这种最常见的首次使用场景下
    是正常显示还是一屏 traceback。
    """
    encodable = False

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    # 切完之后实测一下到底能不能编码这几个符号——
    # 不要靠猜平台，要靠实际编码能力。
    probe = C.S_OK + C.S_BAD + C.S_LINE + C.S_BULLET
    try:
        target = sys.stdout.encoding or "ascii"
        probe.encode(target)
        encodable = True
    except (UnicodeEncodeError, LookupError):
        encodable = False

    if not encodable:
        C.ascii_mode()


def _no_color() -> bool:
    return bool(
        sys.platform == "win32" and not sys.stdout.isatty()
    ) or not sys.stdout.isatty()


def ok(msg: str) -> None:
    print(f"{C.GRN}{C.S_OK}{C.R} {msg}")


def warn(msg: str) -> None:
    print(f"{C.YEL}{C.S_WARN}{C.R} {msg}")


def bad(msg: str) -> None:
    print(f"{C.RED}{C.S_BAD}{C.R} {msg}")


def info(msg: str) -> None:
    print(f"{C.BLU}{C.S_INFO}{C.R} {msg}")


def dim(msg: str) -> None:
    print(f"{C.DIM}{msg}{C.R}")


def head(title: str) -> None:
    print()
    print(f"{C.B}{title}{C.R}")
    print(f"{C.DIM}{C.S_LINE * min(len(title) * 2, 60)}{C.R}")


def kv(k: str, v: Any, width: int = 22) -> None:
    print(f"  {k:<{width}} {v}")


# --------------------------------------------------------------------------
# 各命令
# --------------------------------------------------------------------------


def cmd_shell(args: argparse.Namespace) -> int:
    """自述这台壳是谁、能做什么、能不能开始迁徙。"""
    src = Path(args.astrbot_src).expanduser() if args.astrbot_src else None
    data = Path(args.data).expanduser() if args.data else None
    check_wol = Path(args.check_wol).expanduser() if args.check_wol else None

    ident = probe_identity(src, data_path=data)
    caps = probe_capabilities(ident, data_path=data, check_wol_script=check_wol)
    pf = preflight(ident, caps)

    if args.json:
        print(
            json.dumps(
                {
                    "identity": ident.to_dict(),
                    "capabilities": caps.to_dict(),
                    "preflight": pf.to_dict(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if pf.ok else 1

    head("这台壳")
    kv("shell_id", f"{C.CYA}{ident.shell_id}{C.R}")
    kv("hostname", ident.hostname)
    kv("platform", ident.platform)
    kv("python", f"{ident.python_version}  {C.DIM}{ident.python_executable}{C.R}")
    kv("容器内", "是" if ident.is_container else "否")
    kv("ASTRBOT_ROOT", ident.astrbot_root or f"{C.YEL}未设置{C.R}")
    kv("AstrBot 版本", ident.astrbot_version or f"{C.YEL}未探测到{C.R}")

    head("能做什么")
    for name, flag in (
        ("soul.read（热备库）", caps.soul_read),
        ("soul.write（收快照）", caps.soul_write),
        ("shell.identity（自述身份）", caps.shell_identity),
    ):
        (ok if flag else bad)(name)

    print()
    optional = [
        ("发魔术封包", caps.can_send_magic_packet),
        ("能被 WOL 叫醒", caps.can_be_woken),
        ("网卡已 arm", caps.wake_armed),
        ("有交互式桌面", caps.has_interactive_desktop),
        ("有 GPU", caps.has_gpu),
    ]
    for name, flag in optional:
        if flag is None:
            dim(f"  ?  {name}：未知（探测不到，不猜）")
        elif flag:
            ok(f"  {name}")
        else:
            warn(f"  {name}")

    if caps.notes:
        head("备注")
        for n in caps.notes:
            dim(f"  {C.S_BULLET} {n}")

    head("迁徙前判决")
    color = {"proceed": C.GRN, "self_heal": C.YEL, "block": C.RED}[pf.action]
    label = {
        "proceed": "可以开始",
        "self_heal": "需要先自愈（可以自动修）",
        "block": "阻塞（需要人介入）",
    }[pf.action]
    print(f"  {color}{C.B}{pf.action.upper()}{C.R} —— {label}")
    for r in pf.reasons:
        print(f"    {C.DIM}{C.S_BULLET}{C.R} {r}")
    if pf.blocking:
        print()
        for b in pf.blocking:
            print(f"    {C.RED}{C.S_BAD}{C.R} {b}")

    return 0 if pf.ok else 1


def _backend(args: argparse.Namespace):
    """按命令行参数打开本地或远端 Warden。"""
    return open_backend(
        root=getattr(args, "root", None),
        url=getattr(args, "warden_url", None),
        token=getattr(args, "warden_token", None),
        staging=getattr(args, "staging", None),
    )


def cmd_status(args: argparse.Namespace) -> int:
    """现在是谁在线、搬过几次、心跳多久前。"""
    try:
        be = _backend(args)
    except ValueError as e:
        bad(str(e))
        return 2

    try:
        s = be.status()
    except Fatal as e:
        bad(str(e))
        return 2

    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0

    head("Warden 状态")
    kv("位置", be.where())
    kv("根目录", s["root"])
    kv("当前 epoch", f"{C.B}{s['current_epoch']}{C.R}")
    kv("最后接受的 epoch", s["last_accepted_epoch"])
    kv("被拒绝的写入次数", s["rejected_write_count"])

    lease = s["lease"]
    state = lease["state"]
    if state == "no-lease":
        print()
        info("没有租约 —— 现在没有任何壳在线，可以开始迁徙")
    elif state == "active":
        print()
        ok(f"租约持有者：{C.B}{lease['holder']}{C.R}")
        kv("  epoch", lease["epoch"])
        kv("  剩余", f"{lease['remaining_seconds']} 秒 / TTL {lease['ttl_seconds']} 秒")
        dim("  （公理 0：任意时刻只能有一个存在在线）")
    else:
        print()
        warn(f"租约由 {lease['holder']} 持有，但已过期 {lease['expired_seconds_ago']} 秒")
        dim("  意味着那个壳可能已经死了。新的壳可以接管。")

    head("谁还在")
    beats = s.get("heartbeats") or {}
    if not beats:
        dim("  还没有任何壳上报过心跳 —— 三个守护进程都还没跑起来（或都是旧版）")
        dim("  这意味着下面这些数字只能说明「账本记到了哪」，不能说明「谁还活着」。")
    else:
        for shell_id, beat in sorted(
            beats.items(), key=lambda kv: kv[1].get("age_seconds", 0)
        ):
            age = beat.get("age_readable") or "?"
            if beat.get("stale"):
                warn(f"  {C.B}{shell_id}{C.R} 已 {age} 没上报 —— 判断它已经没了")
            else:
                ok(f"  {C.B}{shell_id}{C.R} 最后心跳 {age}")
            note = beat.get("note") or ""
            if note:
                dim(f"      它的角色：{note}")
            if beat.get("generation"):
                dim(f"      它手里是{snapshot_tag(beat['generation'])}")
        stale = [k for k, v in beats.items() if v.get("stale")]
        live = len(beats) - len(stale)
        print()
        if live == 0:
            warn("  一个在报的都没有 —— 鱼可能真的没了，但账本自己看不出来这一点")
            dim("  心跳只能证明某个壳在报；它不证明鱼在说话（那要看控制面 /status 的 speaking）")
        else:
            info(f"  {live} 个壳在报。心跳证明它还在跑，不证明它在说话。")

    head("迁徙历史")
    if s["generation_count"] == 0:
        dim("  还没有任何一次迁徙")
    else:
        kv("已完成迁徙次数", s["generation_count"])
        latest = s["latest_generation"]
        kv("最新 generation", latest["generation"])
        kv("  时间", latest["created_at_readable"])
        kv("  来源", latest["source_host"])
        kv("  提交者", latest["pushed_by"])
        if latest.get("target_shell"):
            kv("  目标壳", latest["target_shell"])
        # `target_shell` 是"打算交给谁"，只有 `consumed_by` 是"真的取走了"。
        # 账本没记消费时如实说"还没人取走"，不拿 target_shell 冒充事实——
        # 目标壳可能一直没开机，猜错会让人以为灵魂已经搬家了。
        if latest.get("consumed_by"):
            kv("  消费方", latest["consumed_by"])
            kv("  消费时间", latest.get("consumed_at_readable") or "（账本缺时间）")
        else:
            kv("  消费方", "还没人取走")
        kv("  数据量", f"{latest['artifact_bytes'] / 1024 / 1024:.1f} MB")

    return 0


def cmd_checkout(args: argparse.Namespace) -> int:
    """从源端 data/ 做一次热备，产出一个 generation 目录。

    这是去程的第一步。它**只读**源库，不需要停 AstrBot。
    """
    if not args.data:
        bad("必须用 --data 指定源端 data 目录（不猜）")
        return 2

    src = Path(args.data).expanduser().resolve()
    if not src.is_dir():
        bad(f"源目录不存在：{src}")
        return 2

    root = Path(args.root).expanduser() if args.root else DEFAULT_ROOT
    try:
        be = _backend(args)
    except ValueError as e:
        bad(str(e))
        return 2

    try:
        epoch = be.acquire(args.shell)
    except LeaseHeld as e:
        bad(str(e))
        return 3
    except MoltError as e:
        bad(f"{type(e).__name__}: {e}")
        return 3

    head(f"热备 {src}")
    info(f"shell_id={args.shell}  epoch={epoch}  裁决者={be.where()}")
    print()

    gen = be.next_generation()
    dest_dir = be.prepare_generation_dir(gen)

    # 【2026-11-15】这里原来是**第二份**热备实现（和 `_snapshot_payload` 各写一遍）。
    # 两份的差别在记忆目录上直接变成 bug：`molt migrate` 走的是新版、
    # `molt checkout` 走的是旧版，于是"同一台机器、两条命令、搬走的东西不一样"。
    # 现在只留一条实现。
    rc, artifacts, _m = _snapshot_payload(
        src=src,
        dest_dir=dest_dir,
        astrbot_src=getattr(args, "astrbot_src", None),
        target=getattr(args, "target", "") or "",
        generation=gen,
        with_plugin_data=tuple(getattr(args, "with_plugin_data", ()) or ()),
    )
    if rc != 0:
        be.release(args.shell, epoch)
        return rc

    total = sum(a.bytes for a in artifacts)
    runtime_pip = _m.runtime_pip if _m is not None else []

    head("结论")
    kv("generation", gen)
    kv("产出目录", dest_dir)
    kv("总字节", f"{total / 1024 / 1024:.1f} MB")
    kv("工件数", len(artifacts))
    if runtime_pip:
        kv("需在新壳重装", f"{len(runtime_pip)} 个运行时包")
        dim(f"  {', '.join(runtime_pip[:6])}" + (" …" if len(runtime_pip) > 6 else ""))
    print()
    ok(f"热备完成。租约仍由 {args.shell} 持有（epoch={epoch}）")
    dim("  下一步：把整个 generation 目录传给目标壳，然后 push")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """验证一个 generation 目录是否完整可用。"""
    if not args.dir:
        bad("必须用 --dir 指定 generation 目录")
        return 2

    gdir = Path(args.dir).expanduser().resolve()
    mpath = gdir / "manifest.json"
    if not mpath.exists():
        bad(f"没有 manifest.json：{gdir}")
        return 2

    m = Manifest.load(mpath)

    head(f"验证 generation {m.generation}")
    kv("来源", f"{m.source_host}  ({m.platform})")
    kv("产出时间", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.created_at)))
    kv("AstrBot", m.astrbot_version)
    kv("协议版本", m.protocol_version)
    if m.protocol_version != PROTOCOL_VERSION:
        bad(f"协议版本不匹配：快照 {m.protocol_version}，本机 {PROTOCOL_VERSION}")
        return 5

    print()
    problems = m.verify_against(gdir)
    for a in m.artifacts:
        if a.kind == "sqlite":
            print(f"  {C.GRN}{C.S_OK}{C.R} {a.relpath}")
            print(
                f"      {a.bytes / 1024 / 1024:.1f} MB  {a.table_count} 张表  "
                f"{C.DIM}{a.sha256[:16]}…{C.R}"
            )
        else:
            print(f"  {C.GRN}{C.S_OK}{C.R} {a.relpath}  {a.bytes} B")

    print()
    if problems:
        bad(f"{len(problems)} 个问题：")
        for p in problems:
            print(f"    {C.RED}{C.S_BULLET}{C.R} {p}")
        return 6

    ok("全部通过。这份快照可以交给目标壳。")
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    """把一份已验证的快照登记到 Warden（= 正式把灵魂交给新壳）。

    这是整个流程里唯一会改变"谁持有灵魂"的地方。
    校验不过就拒绝，没有 --force。
    """
    if not args.dir:
        bad("必须用 --dir 指定 generation 目录")
        return 2
    if not args.shell:
        bad("必须用 --shell 指定要交给谁")
        return 2

    gdir = Path(args.dir).expanduser().resolve()
    mpath = gdir / "manifest.json"
    if not mpath.exists():
        bad(f"没有 manifest.json：{gdir}")
        return 2

    m = Manifest.load(mpath)

    head(f"登记 generation {m.generation} → {args.shell}")

    # 先本地验一遍，省得把坏东西推上去再被拒
    problems = m.verify_against(gdir)
    if problems:
        bad("快照本身有问题，拒绝登记：")
        for p in problems:
            print(f"    {C.RED}{C.S_BULLET}{C.R} {p}")
        return 6
    ok("快照完整性校验通过")

    if m.protocol_version != PROTOCOL_VERSION:
        bad(
            f"协议版本不匹配：快照 {m.protocol_version}，本机 {PROTOCOL_VERSION}"
        )
        return 5

    try:
        be = _backend(args)
    except ValueError as e:
        bad(str(e))
        return 2

    try:
        record = be.push(args.shell, args.epoch, m.to_dict())
    except EpochStale as e:
        bad(f"epoch 已过期：{e}")
        dim("  说明你手里的租约被回收了——可能另一个壳已经接管。")
        return 7
    except EpochAhead as e:
        bad(f"epoch 超前：{e}")
        dim("  说明这个 epoch 根本不属于你。")
        return 7
    except LeaseExpired as e:
        bad(f"租约已过期：{e}")
        dim("  TTL 到了。重新 acquire 拿一个新 epoch。")
        return 7
    except NeedsHuman as e:
        bad(f"被拒绝：{e}")
        return 7
    except ProtocolMismatch as e:
        bad(f"协议不匹配：{e}")
        return 5
    except MoltError as e:
        bad(f"{type(e).__name__}: {e}")
        return 7

    head("已登记")
    kv("generation", record["generation"])
    kv("提交者", record["pushed_by"])
    if record.get("target_shell"):
        kv("目标壳", record["target_shell"])
    kv("epoch", record["epoch"])
    kv("数据量", f"{record['artifact_bytes'] / 1024 / 1024:.1f} MB")
    kv("来源", record["source_host"])
    kv("裁决者", be.where())
    print()
    ok("这份快照已经登记，目标壳现在可以取走它了。")
    dim("  注意：登记 ≠ 交接完成。提交者（现在是它）必须先终止，")
    dim("  新壳才能 acquire 拿到更大的 epoch 并接管。这就是「先死后生」。")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    """交还租约（正常的收尾动作）。"""
    try:
        be = _backend(args)
    except ValueError as e:
        bad(str(e))
        return 2
    try:
        be.release(args.shell, args.epoch)
    except EpochStale as e:
        bad(str(e))
        return 3
    except MoltError as e:
        bad(f"{type(e).__name__}: {e}")
        return 3
    ok(f"{args.shell} 已交还租约（{be.where()}）")
    return 0


def _control_client(args: argparse.Namespace) -> Optional[AstrBotControl]:
    """按命令行参数建一个控制面客户端；没给 `--control-url` 就返回 None。

    ## 为什么地址必须由人显式给，而不是从配置里推

    AstrBot 的 dashboard 可能监听 `127.0.0.1`、`0.0.0.0`、或者容器映射出来的
    某个端口上。猜错的后果不是"连不上"，而是**连上了另一台机器上的 AstrBot**
    —— 那会让我们把别人的鱼按住。所以这里不猜。
    """
    url = (getattr(args, "control_url", "") or "").strip()
    if not url:
        return None

    cfg = (getattr(args, "control_config", "") or "").strip()
    if cfg:
        # 推荐这条路：从它自己的 cmd_config.json 里取 username + jwt_secret
        # 现签一个短命 token。人不需要另外保存一份密码。
        return AstrBotControl.from_config(url, cfg)

    token = (getattr(args, "control_token", "") or "").strip()
    if not token:
        raise NeedsHuman(
            "给了 --control-url 但没给凭据。二选一：\n"
            "  --control-config <那台壳的 cmd_config.json>（推荐，会自己签 token）\n"
            "  --control-token <直接给一个 JWT>"
        )
    return AstrBotControl(url, token)


#: 目标壳"还活着"的门槛。心跳每 10 秒一次；60 秒等于容了 6 拍不报，
#: 而 2026-11-15 23:38 那次翻车正是这样发生的：机器 23:37:30 关机，
#: 到 23:38:29 才过了 59 秒，快照照样被推了过去。现在收紧到 25 秒，
#: 并且**光看年龄不够** —— 还要求在这段时间里真的又跳了一拍（见下面）。
TARGET_ALIVE_SECONDS = 25.0


def _target_heartbeat_age(be: Any, shell: str) -> Optional[float]:
    """目标壳上次报心跳是多久前。读不到就返回 None（"不知道"，不是"活着"）。"""
    try:
        status = be.status() or {}
    except Exception:
        return None
    beat = ((status.get("heartbeats") or {}).get(shell)) or {}
    try:
        return float(beat.get("age_seconds"))
    except (TypeError, ValueError):
        return None


def _heartbeat_marker(be: Any, shell: str) -> Optional[str]:
    """取这一次心跳的"指纹"（账本给的是可读时间串）。

    用它而不是 `age_seconds`：年龄是算出来的、每次请求都在变，
    而 `last_seen_readable` 只在**对方真的又报了一拍**之后才变。
    """
    try:
        status = be.status() or {}
    except MoltError:
        return None
    beat = (status.get("heartbeats") or {}).get(shell) or {}
    marker = beat.get("last_seen_readable")
    return str(marker) if marker else None


def _target_is_alive_now(
    be: Any, shell: str, *, guard: float = 15.0, interval: float = 3.0
) -> bool:
    """"它还活着吗" —— 要看到**新的一拍**，而不是"最后一拍还不算久"。

    2026-11-15 23:38:29 那次：Windows 在 23:37:30 关机，59 秒后
    `age_seconds` 仍在阈值内，闸门于是说"它还在报心跳，可以直接搬"，
    把鱼交给了一台正在关机的机器。判据换成"观察窗口内又跳了一拍"，
    关机后的第一秒就不成立了。
    """
    first = _heartbeat_marker(be, shell)
    age = _target_heartbeat_age(be, shell)
    if first is None or age is None or age > TARGET_ALIVE_SECONDS:
        return False
    deadline = time.monotonic() + max(guard, interval)
    while time.monotonic() < deadline:
        time.sleep(interval)
        marker = _heartbeat_marker(be, shell)
        if marker is not None and marker != first:
            return True
    return False


def _wait_for_target_heartbeat(
    be: Any, shell: str, *, timeout: float, interval: float = 5.0
) -> Optional[float]:
    """等目标壳自己来账本报到。成功返回它这次的心跳年龄，超时返回 None。

    ## 为什么判据是"心跳"而不是 ICMP / TCP

    搬家需要的是**那台机器上的守护进程活着**，不是"网卡能回 ping"。
    一台刚开机、还没登录的机器 ping 得通，但没人会接住快照 —— 于是源壳
    已经把鱼交出去了，目标壳却什么也没做。心跳是唯一"它能干活"的证据。
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        age = _target_heartbeat_age(be, shell)
        if age is not None and age <= TARGET_ALIVE_SECONDS:
            return age
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


#: 交出去之后等目标壳"真的接走"的时限（秒）。
#:
#: 这一条是 2026-11-15 23:30 那次的补丁：源壳 drain 了、快照上传了、也登记了，
#: 然后**什么都不管地退出 0** —— 目标壳的 agent 卡在"停不下来"，
#: 于是两台壳都不在线。判据只能是账本上的 `consumed_by`，
#: 因为那是唯一"另一边真的把它取走了"的证据。
TARGET_CONFIRM_SECONDS = 180.0


def _wait_for_consumption(
    be: Any, generation: int, shell: str, *, timeout: float, interval: float = 5.0
) -> Optional[float]:
    """等账本记下 `consumed_by == shell`。成功返回等了多久，超时返回 None。"""
    started = time.monotonic()
    deadline = started + max(0.0, timeout)
    while True:
        try:
            status = be.status() or {}
        except MoltError:
            status = {}
        latest = status.get("latest_generation") or {}
        if str(latest.get("consumed_by") or "") == shell:
            return time.monotonic() - started
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def cmd_migrate(args: argparse.Namespace) -> int:
    """一条命令走完整个迁徙：热备 → 打包 → 登记。

    ## 为什么要有这个命令

    前面那四个命令（`checkout` / 打包 / 传 / `push`）各自都是对的，
    但它们把**一次不可中断的动作**拆成了四段人肉步骤。手工做的后果
    在 v0.1 实测里已经看到了：中间任何一步忘了顺序，就会得到
    "快照有了但没登记"或"登记了但目标机根本没拿到字节"这两种
    都需要人工判断的中间态。

    更麻烦的是**租约的 TTL 是 90 秒**。热备很快（0.27s + 0.29s），
    但人手工打包再传再 push，中间去泡杯茶就过期了，于是 push 被
    拒——而人看到的现象是"我明明备份成功了却推不上去"。

    ## 它的边界

    `--transport` 现在有两种：

      * `local`  —— 归档留在源端的 staging 里，人自己传（默认）
      * `warden` —— 把归档 PUT 给 Warden 的中转区，目标壳 `molt land`
                   会自己 GET 下来。**两边都只跟已经认证过的 Warden
                    说话，不需要第三套凭据（SMB 账号/SSH 密钥）。**

    之所以保留 `local`：同机（Warden 就跑在这台机器上）时多一次 HTTP
    往返是没有意义的，而且某些环境（Warden 只监听 127.0.0.1）根本走不通。
    默认值按后端类型自动选——远端后端用 `warden`，本地后端用 `local`。
    """
    if not args.data:
        bad("必须用 --data 指定源端 data 目录（不猜）")
        return 2

    src = Path(args.data).expanduser().resolve()
    if not src.is_dir():
        bad(f"源目录不存在：{src}")
        return 2

    try:
        be = _backend(args)
    except ValueError as e:
        bad(str(e))
        return 2

    # transport 的默认值取决于后端：远端后端默认把归档交上去，
    # 本地后端默认留在本地（多一次无意义的 HTTP 往返还不如直接拷）。
    transport = getattr(args, "transport", None) or (
        "warden" if be.kind == "remote" else "local"
    )
    if transport not in ("local", "warden"):
        bad(f"--transport 只支持 local 或 warden，收到 {transport!r}")
        return 2
    if transport == "warden" and be.kind == "local":
        bad("--transport warden 只对远端后端有意义（本地就是同一台机器）")
        dim("  去掉 --transport，或者改用 --warden-url 指向 NAS。")
        return 2

    head(f"迁徙：{args.shell}")
    kv("源目录", src)
    kv("裁决者", be.where())
    kv("传输", transport)

    # --- 第 -1 步：如果给了 --wake-mac，先把目标叫醒 ---
    #
    # **这一步必须在 acquire 之前。** 顺序不是随便定的：
    # 租约 TTL 只有 90 秒，而叫醒一台 S5 的机器实测要 23.1 秒（网络恢复）
    # 到几十秒（服务能监听）。如果在拿到租约之后才去叫醒，那这 90 秒
    # 有一部分花在等 BIOS POST 上，热备+打包+上传的余量就被吃掉了。
    # 更要命的是：叫醒失败（网卡没 arm）会让租约白占 90 秒。
    #
    # 另外这条顺序也符合语义：**你不可能把一个还没开机的壳"交接"出去**。
    # 账本里对目标壳的声明。**只在能读到的时候用**：读不到账本不该让整个
    # 迁徙失败 —— 那会把"账本抖了一下"变成"它搬不了家"。
    #
    # `wakeable` 的语义严格三分：True=我说过能叫醒 / False=我说过叫不醒 /
    # None=我什么都没说（老行为：给了 --wake-mac 就发）。
    # 把 None 当成 False 会让一台没登记的机器**永远收不到封包**，而症状
    # 看起来是"叫不醒" —— 这正是"默认值伪装成事实"最贵的那个后果。
    target_wakeable: Optional[bool] = None
    target_mac = ""
    target_name = str(getattr(args, "target", "") or "")
    if target_name:
        try:
            declared = ((be.status() or {}).get("shells") or {}).get(target_name) or {}
            if "wakeable" in declared:
                target_wakeable = declared.get("wakeable")
            target_mac = str(declared.get("wake_mac") or "")
        except Exception:
            target_wakeable = None
            target_mac = ""

    # --- 第 -1.5 步：目标壳现在到底在不在？ ---
    #
    # ## 为什么这一步必须在 drain 之前
    #
    # 2026-11-15 23:04 的真事：NAS→Windows 的迁徙没有 MAC 可发（账本里没有），
    # 于是它直接在 NAS 上 drain 掉、把快照推出去 —— 而 Windows 是关着的。
    # 结果：**两边都没有鱼**，持续四分钟，直到机器被开机。
    #
    # 判据是**心跳**，不是 ping：搬家需要的是那台机器上的守护进程活着。
    # 一台刚开机还没人登录的机器 ping 得通，但没人接住快照。
    #
    # 顺序：先看 → 不在就叫 → 等它自己来报到 → **报到了才继续**。
    # 任何一步不成立就不搬，源壳一个字都不动（鱼还在）。
    cross_machine = bool(target_name) and target_name != str(getattr(args, "shell", "") or "")
    target_ready = True
    #: 只有**真的读到过新鲜心跳**才算"确认活着"。跳过检查时它是 False ——
    #: 那不等于"它活着"，只是"这次没查"。
    target_confirmed_alive = False
    target_age: Optional[float] = None
    if cross_machine and not getattr(args, "skip_target_check", False):
        age = _target_heartbeat_age(be, target_name)
        target_age = age
        if age is not None and age <= TARGET_ALIVE_SECONDS and _target_is_alive_now(
            be, target_name
        ):
            ok(f"目标壳 {target_name} 还在报心跳（{age:.0f} 秒前），刚刚又跳了一拍 —— 可以直接搬")
            target_confirmed_alive = True
        else:
            if age is None:
                warn(f"账本里没有 {target_name} 的心跳 —— 不知道它开没开机。")
            else:
                warn(f"目标壳 {target_name} 已经 {age:.0f} 秒没报心跳 —— 它多半是关着的。")
            target_ready = False

    wake_mac = getattr(args, "wake_mac", None)
    if wake_mac and target_wakeable is False:
        # 账本说这台机器叫不醒。发封包不是"也没坏处"——它会让人以为叫过了，
        # 于是去查 BIOS 而不是去按电源键。
        print()
        warn(f"账本说 {target_name} 不可唤醒（wakeable: false）——不发魔术封包。")
        dim("  这台机器要人手动开。包照发只会让人去查网卡，而问题在电源键上。")
        dim("  如果这个声明写错了：molt declare --shell <壳名> --wakeable")
        wake_mac = None
    elif not wake_mac and target_wakeable is not False and target_mac:
        # 账本里记着这台机器的 MAC —— 那是迁移方唯一能读到它的地方
        # （配置只在本机，而叫醒别人的恰恰是另一台机器）。
        wake_mac = target_mac
        print()
        info(f"账本里记着 {target_name} 的 MAC（{target_mac}）——用它发魔术封包")
    elif not wake_mac and target_wakeable is True:
        print()
        warn(f"账本说 {target_name} 可以被叫醒（wakeable: true），但账本里没有它的 MAC。")
        dim("  在那台机器上登记一次，之后每次搬家都会自动叫醒：")
        dim(f"    molt declare --shell {target_name} --wakeable --wake-mac AA-BB-CC-DD-EE-FF")

    # 只有"刚刚还在跳"才免掉封包。2026-11-15 23:38:29 的根因就在这里：
    # 当时判据是"心跳年龄 ≤ 60 秒"，机器 23:37:30 关机、23:38:29 时年龄 59 秒
    # —— 被判成"还活着"，于是**封包一个都没发**，鱼被推给了一台正在关机的机器。
    # 现在留 10 秒的余量：超过它即使"看起来活着"也照发一封，封包是幂等的，
    # 多发一封没有代价，少发一封就是四分钟的静默。
    FRESH_ENOUGH_TO_SKIP_WAKE = 10.0
    if wake_mac and target_confirmed_alive and (target_age is None or target_age <= FRESH_ENOUGH_TO_SKIP_WAKE):
        print()
        ok(f"{target_name} 已经活着 —— 不用发魔术封包")
        wake_mac = None

    if wake_mac:
        wake_host = getattr(args, "wake_host", "") or ""
        print()
        info(f"先叫醒目标 {W.format_mac(wake_mac)}（{wake_host or '全网广播'}）")
        # **封包必须从宿主发。** 2026-11-16 00:11 那次迁移是在容器里跑的：
        # 容器用 bridge 网络，往 255.255.255.255 的广播出不了 docker0 ——
        # 包生成得出来、日志也"发出去了"，但它到不了那台机器的网卡。
        # 而 2026-11-14 12:40 那次真的叫醒了，因为发包的是宿主上的脚本。
        # 所以优先让账本那台机器发（它一定在宿主网络上），本机发是退路。
        wr: Optional[Dict[str, Any]] = None
        remote_sent: List[str] = []
        remote_failed: List[str] = []
        if be.kind == "remote":
            try:
                got = be.wake(
                    wake_mac,
                    host=wake_host,
                    prefix_len=getattr(args, "wake_prefix", 24),
                )
                remote_sent = list(got.get("sent") or [])
                remote_failed = list(got.get("failed") or [])
            except Exception as e:  # 账本发不出去时退回本机，别让一次故障变成"搬不了家"
                warn(f"让账本发包失败（{e}）—— 改用本机发。")
        if remote_sent:
            kv("发包", f"由账本 {be.where()} 发出，路径 {' / '.join(remote_sent)}")
            if remote_failed:
                for line in remote_failed:
                    dim(f"    {line}")
        else:
            if remote_failed:
                for line in remote_failed:
                    dim(f"    账本那边：{line}")
            try:
                wr = W.wake(
                    wake_mac,
                    host=wake_host,
                    prefix_len=getattr(args, "wake_prefix", 24),
                    count=getattr(args, "wake_count", W.DEFAULT_COUNT),
                    interval=getattr(args, "wake_interval", W.DEFAULT_INTERVAL),
                    quiet=True,
                )
            except Fatal as e:
                bad(str(e))
                return 4
            if not wr["any_sent"]:
                bad("封包一个都没发出去——是本机网络的问题，不是目标机。")
                for r in wr["rounds"][0]["results"]:
                    dim(f"    {r['target']}: {r['error']}")
                return 4
            kv("发包", f"{wr['rounds_ok']}/{wr['rounds_sent']} 轮，路径 {' / '.join(wr['targets'])}")

        if wake_host and not getattr(args, "no_wait_wake", False):
            wait_port = getattr(args, "wake_port_check", 0) or None
            # 没有 ping 又没指定 TCP 端口时**不等**，而不是假装等。
            # 「探测不了」不等于「叫不醒」——真机上（容器里没有 ping）
            # 这两件事长得一模一样，而给出后者会让人去查 BIOS。
            if wait_port is None and not W.can_probe_icmp():
                warn("这台机器上没有 ping，ICMP 判据用不了——不等待了。")
                dim("  包已经发出去了；目标机真醒了就会自己来账本取快照。")
                dim("  想确认它起来了，给 --wake-port-check <端口>（走 TCP 判据）。")
            else:
                try:
                    r = W.wait_online(
                        wake_host,
                        port=wait_port,
                        timeout=getattr(args, "wake_timeout", 180.0),
                        interval=2.0,
                    )
                    ok(f"目标起来了：{r['seconds']} 秒（{r['criterion']}）")
                except NeedsHuman as e:
                    # 叫不醒就不该继续：接下来的 checkout 会把灵魂搬给一台
                    # 永远不会开机的机器，而账本里会留下一条"已交接"的记录。
                    bad(str(e))
                    dim("  不继续了：账本里不该出现一条指向一台没开机的机器的记录。")
                    return 5
        else:
            dim("  没等它起来（不判断是否真的开机了）")
        print()

    # --- 第 -1.4 步：目标没在跑就不搬（鱼不能死在这儿） ---
    #
    # 走到这里只有两种可能：目标本来就活着；或者上面刚发过魔术封包。
    # 前者直接过，后者必须**等到它自己来报到** —— 只看"包发出去了"是不够的：
    # 包出去和机器起来之间，隔着 BIOS、快速启动、网卡 arm、登录。
    if cross_machine and not target_ready and not getattr(args, "skip_target_check", False):
        if not wake_mac:
            bad(f"目标壳 {target_name} 没在跑，账本里也没有它的 MAC —— 叫不醒它。")
            dim("  不搬。源壳还在说话，鱼没死。")
            dim("  要么先把那台机器打开，要么在它上面登记一次：")
            dim(f"    molt declare --shell {target_name} --wakeable --wake-mac AA-BB-CC-DD-EE-FF")
            return 5
        wait = float(getattr(args, "wake_timeout", 180.0) or 180.0)
        info(f"等 {target_name} 来账本报到（最多 {wait:.0f} 秒）…")
        age = _wait_for_target_heartbeat(be, target_name, timeout=wait)
        if age is None:
            bad(f"发包之后 {wait:.0f} 秒，{target_name} 还是没有来账本报到。")
            dim("  不搬。源壳还在说话，鱼没死 —— 这次迁徙没有发生。")
            dim("  去那台机器看一眼：卡在登录界面？网卡没 arm？还是包没出去？")
            return 5
        ok(f"目标壳起来了（{age:.0f} 秒前的心跳）—— 继续搬")
    print()

    # --- 第 0 步：先看能不能开始，别做完备份才发现进不去 ---
    #
    # 顺序很重要：acquire 会占住租约，如果备份做了一半失败，租约要还回去。
    # 所以先 try/except 包住整个"拿了就得还"的区间。
    try:
        epoch = be.acquire(args.shell)
    except LeaseHeld as e:
        bad(str(e))
        dim("  公理 0：现在有另一个壳在线。等它下线，或者先让它 release。")
        return 3
    except MoltError as e:
        bad(f"{type(e).__name__}: {e}")
        return 3

    info(f"拿到租约 epoch={epoch}")
    print()

    # --- 第 0.5 步：按住源壳，让它先闭嘴 ---
    #
    # ## 为什么必须在 acquire 之后、checkout 之前
    #
    # **之后**：如果先按嘴再拿租约，而租约被别人占着，那只鱼就白白安静了一场
    # —— 它不会自己醒过来，用户会以为"我跑了个命令，机器人就哑了"。
    # 拿到租约之后才按，前提就成立："我现在是唯一有资格说话的那个，
    # 而我要把自己关掉，把资格交出去。"
    #
    # **之前**：热备是即时一致的，但按嘴之后到热备之间不会再有新消息进来
    # —— 快照因此是"它说过的最后一句话"为止的完整状态，而不是"还差几条"。
    #
    # 没有 --control-url 时这一步整个跳过：插件可能还没装（v0.1 第一次
    # 迁移就是这种情况），那就只能靠人自己先停掉源壳。但我们必须说出来。
    drained = False
    control = None
    try:
        control = _control_client(args)
    except (Fatal, NeedsHuman) as e:
        bad(str(e))
        try:
            be.release(args.shell, epoch)
        except MoltError:
            pass
        return 2

    if control is None:
        warn("没有给 --control-url：这次迁徙不会让源壳停下来。")
        dim("  源壳会继续跟 QQ 说话。目标壳一起来就是两条鱼同时在线（公理 0）。")
        dim("  要么现在人工停掉源壳，要么下次加上：")
        dim("    --control-url http://127.0.0.1:6185 --control-config <源壳的 cmd_config.json>")
        print()
    else:
        info(f"按住源壳 {control.where()} …")
        try:
            dr = control.drain_and_verify()
        except (Fatal, NeedsHuman, MoltError) as e:
            # 按不住就不能继续。继续的后果是目标壳起来时源壳还在说话，
            # 而那个场面在日志里看不出来——只有用户会发现机器人回了两遍。
            bad(f"按住源壳失败：{e}")
            dim("  不继续了。现在启动目标壳就会是两条鱼同时在线（公理 0）。")
            dim("  源壳没有被动过（drain 没成功就等于没 drain）。")
            try:
                be.release(args.shell, epoch)
                dim("  已交还租约。")
            except MoltError:
                dim("  交还租约也失败了，90 秒后会自动过期。")
            return 8
        drained = True
        kv("已关闭的平台", ", ".join(dr.get("drained") or []) or "（本来就没连着）")
        kv("仍在对外说话", "否（已复核）" if dr.get("verified_silent") else "**是**")
        print()

    # 从这里开始，任何失败都必须把租约还回去——否则那 90 秒里
    # 谁都不能接管，而原因只是一次失败的备份。

    def _resume_source(reason: str) -> None:
        """迁徙没成，把源壳放回"能说话"的状态。

        ## 为什么失败时要主动 resume

        因为**哑着比坏了更不容易被发现**。一只不再回消息的机器人，用户会以为
        是网络问题、QQ 掉线、或者自己在忙；而一只明确报错的机器人，用户立刻
        会去看日志。drain 是我们主动造成的静默，所以失败时也该由我们撤销它。

        但撤销也失败时不假装成功：那是"我们把它弄哑了却没本事弄回来"，
        必须说成人话。
        """
        if not drained or control is None:
            return
        print()
        warn(f"迁徙没完成（{reason}）。把源壳放回来说话的状态 …")
        try:
            r = control.resume()
        except (Fatal, NeedsHuman, MoltError) as e:
            bad(f"恢复源壳失败：{e}")
            dim("  ⚠️ **源壳现在是哑的**。在它恢复之前不要再启动任何别的壳。")
            dim("  它的 plugin 还在跑，重启那个 AstrBot 进程也会让它重新连上。")
            return
        if r.get("resumed"):
            ok(f"源壳已恢复：{', '.join(r['resumed'])}")
        else:
            warn("resume 没有恢复任何平台——它现在仍然是哑的。")
            dim("  原因见上面：要么配置里 enable=false（那是护栏，不是故障），")
            dim("  要么它本来就没连过任何东西。")
        print()

    try:
        rc, gen, gen_dir, archive_info = _do_checkout(
            src=src,
            be=be,
            shell=args.shell,
            astrbot_src=getattr(args, "astrbot_src", None),
            target=args.target,
            want_archive=not args.no_archive,
            with_plugin_data=tuple(getattr(args, "with_plugin_data", ()) or ()),
        )
        if rc != 0:
            _resume_source("热备或打包失败")
            be.release(args.shell, epoch)
            warn("已交还租约（这次迁徙没有完成）")
            return rc

        # --- 上传（可选） ---
        #
        # 上传放在 push 之前：先让字节到位，再让账本承认这件事。
        # 反过来的话，一旦上传失败，账本里就有了一条"登记了但取不到字节"
        # 的记录——那正是最难处理的中间态。
        if archive_info and transport == "warden":
            up = be.upload_archive(
                gen, Path(archive_info["path"]),
                expected_sha256=archive_info["sha256"],
            )
            info(
                f"已上传到中转区：gen-{gen:06d} "
                f"{up['bytes'] / 1024 / 1024:.1f} MB"
            )

        # --- 登记 ---
        m = Manifest.load(gen_dir / "manifest.json")
        record = be.push(args.shell, epoch, m.to_dict())

        head("迁徙完成")
        kv("generation", record["generation"])
        kv("快照目录", gen_dir)
        if archive_info:
            kv("归档", archive_info["path"])
            kv("归档大小", f"{archive_info['bytes'] / 1024 / 1024:.1f} MB")
            kv("归档 sha256", archive_info["sha256"])
            if transport == "warden":
                kv("中转区", "已上传，目标壳可直接取")
        kv("数据量", f"{record['artifact_bytes'] / 1024 / 1024:.1f} MB")
        print()
        ok("已登记。快照现在归新壳管了。")

    except Fatal as e:
        bad(str(e))
        _resume_source("迁徙中止")
        try:
            be.release(args.shell, epoch)
            warn("已交还租约")
        except MoltError:
            warn("交还租约也失败了——它会在 90 秒后自动过期")
        return 4
    except MoltError as e:
        bad(f"{type(e).__name__}: {e}")
        _resume_source("迁徙被拒")
        try:
            be.release(args.shell, epoch)
        except MoltError:
            pass
        return 7

    # --- 下一步提示 ---
    print()
    if drained:
        # 这句话必须显眼：用户跑完一条命令之后，机器人就不回消息了。
        # 不说清楚，他会以为是自己搞坏了什么。
        dim("  源壳现在是哑的（这是我们按的，不是它坏了）。")
        dim("  目标壳起来接管之前，它不会回任何人的消息。")
    if archive_info and not args.keep_lease:
        dim("  下一步：")
        if transport == "warden":
            dim("    1. 在目标壳：molt land --generation "
                f"{gen} --data <目标 data>")
            dim("       （归档已经在 Warden 的中转区，它会自己取）")
            dim(f"    2. 目标壳接管：molt migrate --shell <新壳 id> ...")
        else:
            dim(f"    1. 把 {Path(archive_info['path']).name} 传到目标壳")
            dim("    2. 在那边：molt land --archive <文件> --data <目标 data>")
        dim("  （传输这一环现在也不用人手了，但租约的生死顺序还得你自己掌握。）")
    else:
        dim("  租约还握在你手里。目标壳必须先等你 release 才能接管。")

    if not args.keep_lease:
        # 默认走"先死后生"的生死边界：这次迁徙做完就把租约放掉，
        # 让目标壳能接管。`--keep-lease` 留给"我还要再拍一份"的场景。
        try:
            be.release(args.shell, epoch)
            ok("已交还租约——目标壳现在可以接管了")
        except MoltError as e:
            warn(f"交还租约失败：{e}")
            dim("  90 秒后会自动过期，不用慌。")

    # ---- 交出控制权之后，必须确认它真的被接住了 ----------------------
    #
    # 只有"真的把鱼交出去了"（drained）才需要这一步：没 drain 的迁徙
    # 源壳一直在说话，鱼从来没离开过，没什么可回滚的。
    if drained and not getattr(args, "no_wait_confirm", False):
        wait = float(getattr(args, "confirm_timeout", TARGET_CONFIRM_SECONDS) or 0)
        print()
        info(f"等 {args.target} 真的接走这一代（最多 {wait:.0f} 秒）…")
        took = _wait_for_consumption(be, gen, args.target, timeout=wait)
        if took is not None:
            ok(f"{args.target} 已经接走了（{took:.0f} 秒）—— 这次交接完成")
            return 0
        # 没接走：把鱼拉回源壳。这是"鱼不能死"的最后一道闸。
        warn(f"{args.target} 在 {wait:.0f} 秒内没有接走这一代。")
        return _rollback_to_source(
            be, args, generation=gen, reason="目标壳没有接管",
            control=control, drained=drained,
        )

    return 0


def _rollback_to_source(
    be: Any,
    args: argparse.Namespace,
    *,
    generation: int,
    reason: str,
    control: Any,
    drained: bool,
) -> int:
    """把鱼拉回源壳：先抢回租约（fencing），再让平台适配器重新上线。

    顺序不能反：先恢复平台、再抢租约，中间那几秒里目标壳可能也起来 ——
    那就是两只鱼同时在线。抢租约在前，目标壳的 agent 看到 holder 非空
    就不会落位（`daemon/agent.py::_lease_is_free`）。
    """
    print()
    warn(f"交接失败（{reason}）—— 现在把鱼拉回源壳。")
    try:
        epoch2 = be.acquire(args.shell)
        ok(f"已抢回租约：epoch {epoch2}（目标壳看到有人持租约就不会落位）")
    except MoltError as e:
        bad(f"抢回租约失败：{e}")
        dim("  鱼现在只在账本里 —— 请人工确认哪台壳该起来。")
        return 7

    if drained:
        if control is None:
            warn("这次迁徙没有控制面凭据 —— 我没法把源壳叫回来。")
            dim("  下一步：molt resume，或重启源壳的 AstrBot。")
            return 6
        try:
            r = control.resume()
            resumed = r.get("resumed") or []
            if resumed:
                ok(f"源壳已恢复：{', '.join(resumed)}")
                ok("鱼回到源壳了 —— 这次迁徙没有发生，鱼没死。")
                return 6
            warn("resume 没有恢复任何平台 —— 它现在仍然是哑的。")
            dim("  下一步：molt resume（或重启源壳的 AstrBot）。")
            return 6
        except MoltError as e:
            bad(f"恢复源壳失败：{e}")
            dim("  下一步：molt resume（或重启源壳的 AstrBot）。")
            return 7
    dim("  源壳没被 drain，鱼一直在说话。")
    return 6



def _add_memory_dirs(
    *,
    src: Path,
    dest_dir: Path,
    artifacts: List[Artifact],
    extra: "Sequence[str]" = (),
    quiet: bool = False,
) -> int:
    """把"记忆类" plugin_data 目录也搬进这份快照。返回 0 或 4。

    这一条以前**根本不存在**：`COPY_ARTIFACTS` 里写着 plugin_data，
    但没有任何代码读它，于是 private_companion 的记忆每次都留在原地。
    见 `soul.MEMORY_PLUGIN_DATA` 上面的更正说明。
    """
    rels = collect_memory_dirs(src, extra=extra)
    if not rels:
        return 0
    for rel in rels:
        if not quiet:
            info(f"搬记忆目录 {rel} …")
        try:
            got = copy_memory_dir(src, rel, dest_dir)
        except NeedsHuman as e:
            bad(str(e))
            return 4
        except Fatal as e:
            bad(str(e))
            return 4
        artifacts.extend(got)
        if not quiet:
            total = sum(a.bytes for a in got)
            sqlite_count = sum(1 for a in got if a.kind == "sqlite")
            print(
                f"    {C.GRN}{C.S_OK}{C.R} {len(got)} 个文件  "
                f"{total / 1024 / 1024:.1f} MB  "
                f"{C.DIM}其中 {sqlite_count} 个 sqlite 走 backup(){C.R}"
            )
    return 0


def _snapshot_payload(
    *,
    src: Path,
    dest_dir: Path,
    astrbot_src: Optional[str],
    target: str,
    generation: int,
    quiet: bool = False,
    with_plugin_data: "Sequence[str]" = (),
) -> "tuple[int, List[Artifact], Optional[Manifest]]":
    """把"灵魂"热备进 `dest_dir` 并写好 manifest。

    抽出来是为了让 `migrate`（要分配新一代、要 push 账本）和
    `backup`（不占代、只上传字节）**走同一条热备逻辑**。两处各写一遍的话，
    迟早出现"迁徙时抓的是这两个库、备份时抓的是另外两个"这种没法解释的差异。
    """
    artifacts: List[Artifact] = []

    for rel in (
        "data_v4.db",
        "plugin_data/astrbot_plugin_memory_companion/memory_companion.db",
    ):
        src_db = src / rel
        if not src_db.exists():
            warn(f"源端没有 {rel}（这个部署可能没装对应插件）")
            continue

        dst_db = dest_dir / rel
        try:
            if not quiet:
                info(f"热备 {rel} …")
            payload = hot_backup(src_db, dst_db)
        except Fatal as e:
            bad(str(e))
            return 4, artifacts, None

        if not quiet:
            print(
                f"    {C.GRN}{C.S_OK}{C.R} {payload['bytes'] / 1024 / 1024:.1f} MB  "
                f"{payload['backup_seconds']:.3f}s  "
                f"{payload['table_count']} 张表  "
                f"quick_check={payload['quick_check']}  "
                f"{C.DIM}{payload['sha256'][:16]}…{C.R}"
            )

        artifacts.append(
            Artifact(
                relpath=rel,
                kind="sqlite",
                bytes=payload["bytes"],
                sha256=payload["sha256"],
                quick_check=payload["quick_check"],
                table_count=payload["table_count"],
                backup_seconds=payload["backup_seconds"],
            )
        )

    if not artifacts:
        bad("一个库都没备份到，这次操作没有意义")
        return 4, artifacts, None

    cfg = src / "cmd_config.json"
    if cfg.exists():
        if not quiet:
            info("复制 cmd_config.json …")
        dst_cfg = dest_dir / "cmd_config.json"
        dst_cfg.parent.mkdir(parents=True, exist_ok=True)
        dst_cfg.write_bytes(cfg.read_bytes())
        artifacts.append(
            Artifact(
                relpath="cmd_config.json",
                kind="copy",
                bytes=dst_cfg.stat().st_size,
                sha256=sha256_of(dst_cfg),
            )
        )
        if not quiet:
            print(f"    {C.GRN}{C.S_OK}{C.R} {dst_cfg.stat().st_size} B")

    rc = _add_memory_dirs(
        src=src,
        dest_dir=dest_dir,
        artifacts=artifacts,
        extra=with_plugin_data,
        quiet=quiet,
    )
    if rc != 0:
        return rc, artifacts, None

    # 插件配置：搬来的这一份是"源壳上当时正在生效的那份"。
    # 落位时只写目标壳也装了的插件（见 molt.landcfg）。
    try:
        cfg_arts = copy_plugin_configs(src, dest_dir)
    except (Fatal, NeedsHuman) as e:
        bad(str(e))
        return 4, artifacts, None
    if cfg_arts and not quiet:
        info(f"带上 {len(cfg_arts)} 份插件配置（落位时只写这边也装了的插件）")
        for art in cfg_arts[:6]:
            print(f"    {C.GRN}{C.S_OK}{C.R} {art.relpath}  {art.bytes} B")
        if len(cfg_arts) > 6:
            dim(f"    … 共 {len(cfg_arts)} 份")
    artifacts.extend(cfg_arts)

    ident = probe_identity(
        Path(astrbot_src) if astrbot_src else None,
        data_path=src,
    )
    runtime_pip = collect_runtime_pip(src / "site-packages")

    m = Manifest(
        generation=generation,
        created_at=time.time(),
        source_host=ident.hostname,
        source_root=str(src),
        protocol_version=PROTOCOL_VERSION,
        astrbot_version=ident.astrbot_version or "unknown",
        platform=ident.platform,
        python_version=ident.python_version,
        artifacts=artifacts,
        runtime_pip=runtime_pip,
        target_shell=target or "",
    )
    m.save(dest_dir / "manifest.json")
    return 0, artifacts, m


def _do_checkout(
    *,
    src: Path,
    be,
    shell: str,
    astrbot_src: Optional[str],
    target: str,
    want_archive: bool,
    with_plugin_data: "Sequence[str]" = (),
) -> "tuple[int, int, Path, Optional[Dict[str, Any]]]":
    """热备 + 打包。拆出来是为了让 `migrate` 的租约管理保持扁平。

    返回 `(rc, generation, gen_dir, archive_info)`；`rc != 0` 表示失败。
    """
    gen = be.next_generation()
    dest_dir = be.prepare_generation_dir(gen)

    rc, _artifacts, _manifest = _snapshot_payload(
        src=src,
        dest_dir=dest_dir,
        astrbot_src=astrbot_src,
        target=target,
        generation=gen,
        with_plugin_data=with_plugin_data,
    )
    if rc != 0:
        return rc, gen, dest_dir, None

    archive_info: Optional[Dict[str, Any]] = None
    if want_archive:
        archive_path = dest_dir.parent / f"gen-{gen:06d}.tar.gz"
        info("打包 …")
        archive_info = make_archive(dest_dir, archive_path)
        print(
            f"    {C.GRN}{C.S_OK}{C.R} {archive_info['bytes'] / 1024 / 1024:.1f} MB  "
            f"{archive_info['file_count']} 个文件  "
            f"{C.DIM}{archive_info['sha256'][:16]}…{C.R}"
        )

    return 0, gen, dest_dir, archive_info


def _work_root(data_dir: Path) -> Path:
    """临时工作目录放在"数据那台磁盘"上，而不是 /tmp。

    2026-11-16 在 NAS 上炸过一次：这台机器的 /tmp 是 3.8 GB 的 tmpfs，
    `cmd_backup` 用 mkdtemp 把数据目录复制进去、又从来没删过，
    结果 72 个 `molt-backup-*` 目录占掉 2.6 GB、66 个 tar.gz 占掉 665 MB，
    /tmp 直接 100% 满 —— 之后 `docker exec` 报 `no space left on device`，
    SQLite 报 `database or disk is full`，整台机器的服务开始写不进临时文件。

    所以临时目录必须跟数据在同一块盘上（`<data 的父目录>/.molt-work`），
    并且**用完就删**。`MOLT_WORK_DIR` 可以覆盖。
    """
    override = os.environ.get("MOLT_WORK_DIR", "").strip()
    if override:
        base = Path(override)
    else:
        try:
            base = Path(data_dir).resolve().parent / ".molt-work"
        except OSError:
            base = Path(tempfile.gettempdir()) / "molt-work"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = Path(tempfile.gettempdir()) / "molt-work"
        base.mkdir(parents=True, exist_ok=True)
    return base


def cmd_backup(args: argparse.Namespace) -> int:
    """把当前状态热备一份、推给 Warden 的 `backups/`。

    ## 它和 `migrate` 的区别只有一句话

    `migrate` 是**交接**：占一代、要租约、有目标壳、有人会来取。
    `backup` 不是：它没有目标壳，也没人会来取，账本里一个字都不写。
    恢复只能由人显式发起（`molt recover`）——正因为如此，它可以在鱼
    正常说话的时候十分钟跑一次，不会和任何状态机打架。
    """
    data_dir = Path(args.data).expanduser().resolve()
    if not data_dir.is_dir():
        bad(f"data 目录不存在：{data_dir}")
        return 2

    head("中途备份")
    kv("源 data", data_dir)

    try:
        be = _backend(args)
    except (NeedsHuman, Fatal, ValueError) as e:
        bad(str(e))
        return 2

    shell = args.shell or _default_shell_id()

    try:
        status = be.status()
    except (NeedsHuman, Fatal, MoltError) as e:
        bad(f"读不到账本：{e}")
        return 3

    latest = (status.get("latest_generation") or {}).get("generation") or 0
    try:
        latest = int(latest)
    except (TypeError, ValueError):
        latest = 0

    gen_dir = Path(tempfile.mkdtemp(prefix="molt-backup-", dir=_work_root(Path(data_dir))))
    try:
        return _run_backup(args, be, data_dir, shell, latest, gen_dir)
    finally:
        shutil.rmtree(gen_dir, ignore_errors=True)


def _run_backup(
    args: argparse.Namespace,
    be: Any,
    data_dir: str,
    shell: str,
    latest: int,
    gen_dir: Path,
) -> int:
    rc, _artifacts, manifest = _snapshot_payload(
        src=data_dir,
        dest_dir=gen_dir,
        astrbot_src=args.astrbot_src,
        target="",
        generation=latest,
        with_plugin_data=tuple(getattr(args, "with_plugin_data", ()) or ()),
    )
    if rc != 0 or manifest is None:
        return rc or 4

    archive_path = _work_root(Path(data_dir)) / f"backup-{int(time.time())}.tar.gz"
    info("打包 …")
    archive = make_archive(gen_dir, archive_path)
    print(
        f"    {C.GRN}{C.S_OK}{C.R} {archive['bytes'] / 1024 / 1024:.1f} MB  "
        f"{archive['file_count']} 个文件  "
        f"{C.DIM}{archive['sha256'][:16]}…{C.R}"
    )

    if getattr(args, "dry_run", False):
        warn("--dry-run：打包完成，但没有上传")
        dim(f"  归档留在这里：{archive_path}")
        return 0

    info("上传给 Warden …")
    try:
        meta = be.upload_backup(
            Path(archive["path"]),
            shell=shell,
            generation=latest,
            expected_sha256=archive["sha256"],
        )
    except (NeedsHuman, Fatal, MoltError) as e:
        bad(f"上传失败：{e}")
        dim(f"  归档还在本地：{archive_path}")
        return 4

    ok(f"已收进 backups/：{meta.get('name', '?')}")
    archive_path.unlink(missing_ok=True)
    kv("字节", meta.get("bytes", 0))
    kv("第几次搬家之后", f"{move_count_after(latest)}（这一份是它之后的全部增量）")
    dim("  它不会出现在 molt status 的迁徙历史里 —— 备份不是交接。")
    return 0


def cmd_backups(args: argparse.Namespace) -> int:
    """列出 Warden 上现有的中途备份。"""
    try:
        be = _backend(args)
    except (NeedsHuman, Fatal, ValueError) as e:
        bad(str(e))
        return 2
    try:
        items = be.backups()
    except (NeedsHuman, Fatal, MoltError) as e:
        bad(f"读不到备份列表：{e}")
        return 3

    head("中途备份")
    if not items:
        info("一份都没有。")
        dim("  要让壳定时回推，在 agent 的配置里设 backup_interval（秒）。")
        return 0

    total = 0
    for item in items:
        size = int(item.get("bytes") or 0)
        total += size
        print(
            f"  {C.GRN}{C.S_OK}{C.R} {item.get('name')}  "
            f"{size / 1024 / 1024:.1f} MB  "
            f"来自 {item.get('shell') or '未知壳'}  "
            f"{item.get('created_at_readable') or ''}"
        )
    print()
    kv("一共", f"{len(items)} 份 / {total / 1024 / 1024:.1f} MB")
    dim(f"  恢复：molt recover --backup <名字> --data <目标 data 目录>")
    return 0


def cmd_publish_plugins(args: argparse.Namespace) -> int:
    """把这台壳的「插件配置」打包推给账本。

    ## 为什么只装配置，不装代码

    代码属于"这台机器装了什么"，配置属于"这只鱼喜欢什么"。
    同步代码会让一台机器的插件版本变成另一台的决定 —— 而两边
    本来就允许装不同的东西（比如 dsh_connector 只在 Windows 上有）。

    所以这个包里只有 `config/<插件>_config.json`，一个插件一行，
    别的一概不带：`plugin_data` 是运行时数据，也不会进这个包。
    """
    data_dir = Path(args.data).expanduser().resolve()
    if not data_dir.is_dir():
        bad(f"data 目录不存在：{data_dir}")
        return 2

    shell = args.shell or _default_shell_id()
    plugins = pluginsync.list_plugins(data_dir)

    head("发布插件配置")
    kv("壳", shell)
    kv("本机插件", len(plugins))

    payload, manifest = pluginsync.make_bundle(data_dir, plugins)
    configs = manifest.get("configs") or {}
    if not configs:
        warn("这台机器上还没有任何可同步的插件配置。")
        dim("  只认 data/config/<插件>_config.json；装了插件但没配置过，就不在里面。")
        dim("  也不用担心：这不会让另一台机器上已有的配置消失 —— 什么都不做就是不做事。")
        return 0

    kv("含配置的插件", len(configs))
    kv("包", f"{len(payload) / 1024:.0f} KB")

    try:
        be = _backend(args)
    except (NeedsHuman, Fatal, ValueError) as e:
        bad(str(e))
        return 2

    digest = hashlib.sha256(payload).hexdigest()
    try:
        meta = be.publish_plugin_bundle(
            payload, shell=shell, sha256=digest, plugins=sorted(configs)
        )
    except (NeedsHuman, Fatal, MoltError) as e:
        bad(f"上传失败：{e}")
        return 4

    ok(f"已收进账本：{shell} 的插件配置包")
    kv("字节", meta.get("bytes", len(payload)))
    kv("含", f"{len(configs)} 个插件的配置")
    dim("  另一边跑 molt sync-plugins 时，只会拿「两边都装了」的那几个。")
    dim("  这个包不装插件代码、不装 plugin_data，也不会动另一边的独有插件。")
    return 0


def cmd_sync_plugins(args: argparse.Namespace) -> int:
    """把另一台壳的插件配置同步到这台机器。

    ## 主人定的规则（2026-11-15）

    **只同步两边都装了的插件**。只在一边有的插件，两边都不动 ——
    不装代码、不写配置、也不删配置。

    三条理由：

      - 给一个不存在的插件送配置不会报错，它只会安静躺着；等哪天
        真装上了，它会带着**另一台机器**的配置启动
      - "同步"一旦被理解成"让两边一样"，销毁就成了其中一步
      - 有些值天然绑定机器（比如 dsh_connector 的 `http_base_url`），
        带过去只会让人去查网卡

    结构明显不一样的也默认跳过（`--force` 才写）：同一个插件在两台
    机器上版本不同时，它的配置键会完全不同（真实例子：private_companion
    一边 15 个键、一边 1008 个键），覆盖等于把对方不认识的东西塞进去。
    """
    data_dir = Path(args.data).expanduser().resolve()
    if not data_dir.is_dir():
        bad(f"data 目录不存在：{data_dir}")
        return 2

    try:
        be = _backend(args)
    except (NeedsHuman, Fatal, ValueError) as e:
        bad(str(e))
        return 2

    try:
        bundles = be.plugin_bundles()
    except (NeedsHuman, Fatal, MoltError) as e:
        bad(f"读不到账本上的插件配置包：{e}")
        return 3

    me = args.shell or _default_shell_id()
    source = str(getattr(args, "source", "") or "auto")

    head("同步插件配置")
    kv("这台壳", me)

    if source == "auto":
        others = [b for b in bundles if str(b.get("shell") or "") != me]
        if not others:
            warn("账本里没有别的壳发布过插件配置。")
            dim("  先在另一台机器上跑：molt publish-plugins --data <那边的 data 目录>")
            return 0
        others.sort(key=lambda b: int(b.get("created_at") or 0))
        source = str(others[-1].get("shell") or "")
        dim(f"  没给 --from，取最近的那一份：{source}")

    kv("来自", source)

    try:
        payload = be.plugin_bundle_bytes(source)
    except (NeedsHuman, Fatal, MoltError) as e:
        bad(f"取不下来：{e}")
        return 4

    try:
        remote_configs = pluginsync.parse_bundle(payload)
    except (NeedsHuman, Fatal, MoltError) as e:
        bad(f"这个包读不了：{e}")
        return 4

    local_plugins = pluginsync.list_plugins(data_dir)
    local_configs = pluginsync.read_plugin_configs(data_dir, local_plugins)
    # 清单里才有"那边装了哪些插件"；只用配置文件列表的话，"装了但没配置过"
    # 会被误报成"那边没装"——那是两件完全不同的事。
    manifest = pluginsync.read_bundle_manifest(payload)
    manifest_plugins = [str(n) for n in (manifest.get("plugins") or [])]
    remote_plugins = manifest_plugins or sorted(remote_configs)
    remote_versions = {
        str(k): str(v) for k, v in (manifest.get("versions") or {}).items()
    }
    local_versions = pluginsync.read_plugin_versions(data_dir, local_plugins)
    plan = pluginsync.plan_sync(
        remote_plugins=remote_plugins,
        local_plugins=local_plugins,
        remote_configs=remote_configs,
        local_configs=local_configs,
        remote_versions=remote_versions,
        local_versions=local_versions,
    )

    print()
    kv("两边都装了的插件", len(plan.common))
    if not manifest_plugins:
        dim("  （这个包里没有清单，只能按「哪些插件有配置」来判断）")
    for name in sorted(plan.sync):
        print(f"  {C.GRN}{C.S_OK}{C.R} {name}  {C.DIM}会写{C.R}")
    for name in sorted(plan.unchanged):
        print(f"  {C.DIM}·{C.R} {name}  {C.DIM}一样，不用动{C.R}")
    for name in sorted(plan.suspect):
        print(
            f"  {C.YEL}{C.S_WARN}{C.R} {name}  "
            f"{C.YEL}结构看起来不一样（可能两边插件版本不同）{C.R}"
        )
    for name in sorted(plan.no_remote_config):
        print(f"  {C.DIM}·{C.R} {name}  {C.DIM}那边装了但没有配置 —— 保留这边的{C.R}")
    for name in sorted(plan.only_remote):
        print(f"  {C.DIM}·{C.R} {name}  {C.DIM}那边有、这边没装 —— 不装也不写{C.R}")
    for name in sorted(plan.only_local):
        print(f"  {C.DIM}·{C.R} {name}  {C.DIM}这边独有 —— 不动{C.R}")

    if plan.version_mismatch:
        print()
        warn(f"{len(plan.version_mismatch)} 个插件两边版本不一样：")
        for name in sorted(plan.version_mismatch):
            pair = plan.version_mismatch[name]
            dim(f"  {name}：对面 {pair.get('remote') or '?'} / 这边 {pair.get('local') or '?'}")
        dim("  插件代码不跟着快照走 —— 同一个插件在两台上可以是两个大版本。")
        dim("  版本不同时，配置结构不同往往只是表象；先决定升级哪一台，再同步。")

    if plan.suspect and not args.force:
        print()
        warn(f"{len(plan.suspect)} 个插件的配置结构差太多，默认跳过。")
        dim("  这通常意味着两边的插件版本不同 —— 覆盖过去等于把对方不认识的键塞进去。")
        dim("  确认真要覆盖就加 --force（原文件仍会备份成 *.molt-before-sync-*）。")
    if plan.only_remote:
        dim("  只在那边有的插件不会在这台机器上被安装、也不会写它的配置。")

    if not args.apply:
        print()
        warn("空跑：一个字都没写。确认无误后加 --apply。")
        return 0

    try:
        report = pluginsync.apply_plan(
            data_dir, plan, dry_run=False, force=bool(args.force)
        )
    except (NeedsHuman, Fatal, MoltError) as e:
        bad(f"写入失败：{e}")
        return 4

    print()
    ok(f"写好了 {len(report.updated)} 个")
    if report.backups:
        kv("旧文件备份", f"{len(report.backups)} 个（*.molt-before-sync-*）")
    kv("没动", f"{len(report.unchanged)} 个相同、{len(report.only_remote)} 个那边独有、{len(report.only_local)} 个这边独有")
    dim("  AstrBot 读的是启动时载入的配置 —— 要让它生效还得重启那边（或等它下次重启）。")
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    """从一份中途备份恢复。

    ## 为什么它只是 `land` 的一层皮

    恢复要动的文件和落位要动的是同一批，护栏也必须完全相同（活库检查、
    原子替换、旧数据先备份、机器覆写层）。把那些逻辑再写一遍，等于给
    "最危险的一次操作"开第二条实现路径——两条路径迟早只有一条是对的。

    所以这里只做三件事：把备份取下来、核对指纹、把参数整理成
    `land` 认的形状，然后调它。
    """
    try:
        be = _backend(args)
    except (NeedsHuman, Fatal, ValueError) as e:
        bad(str(e))
        return 2

    head("恢复")
    kv("备份", args.backup)

    staging = Path(tempfile.mkdtemp(prefix="molt-recover-"))
    dest = staging / "recovered.tar.gz"
    try:
        got = be.download_backup(args.backup, dest)
    except (NeedsHuman, Fatal, MoltError) as e:
        bad(f"取不下来：{e}")
        return 4

    ok(f"已取下：{got['bytes'] / 1024 / 1024:.1f} MB")
    kv("sha256", str(got["sha256"])[:24] + "…")

    ns = argparse.Namespace(**vars(args))
    ns.archive = str(dest)
    ns.generation = None
    ns.sha256 = str(got["sha256"])
    return cmd_land(ns)


def _default_shell_id() -> str:
    """没显式给壳名时用的默认值：小写主机名。

    为什么小写：账本里的壳名一直是小写的（`desktop-h2kgiao`、
    `ugreen-776b-8b7896df`），而 `socket.gethostname()` 在 Windows 上
    是全大写。混着来会让"同一个壳"在 `molt status` 里显示成两个。
    """
    try:
        name = socket.gethostname().strip()
    except OSError:
        name = ""
    return (name or "unknown").lower()


def _live_data_dir_reason(data_dir: Path) -> Optional[str]:
    """这个 data 目录看起来正被一个活着的 AstrBot 用着吗？

    判据是 SQLite 的 `-shm` 文件：WAL 模式下的连接会把它 mmap 成共享内存索引，
    **只有活着的连接才会建它**，干净停机后会被删掉（实测：停机前 32,768 B，停机后消失）。

    局限：进程崩溃也会留下 `-shm`。所以这个函数只负责"提醒"，不负责"定罪"——
    调用方给 `--force` 就放行。猜测不该是不可绕过的。
    """
    if not data_dir.is_dir():
        return None
    for side in sorted(data_dir.glob("*-shm")):
        try:
            size = side.stat().st_size
        except OSError:
            size = 0
        return (
            f"{side.name} 还在（{size} 字节）—— WAL 的共享内存索引，"
            "只有活着的连接才会建它"
        )
    return None


def cmd_land(args: argparse.Namespace) -> int:
    """在目标壳上，把收到的归档落进 data 目录。

    ## 它为什么必须独立成一条命令

    因为 `land` 跑在**另一台机器**上，而 `migrate` 跑在源端。它们之间
    隔着一次传输。把两者合成一条命令需要先解决"谁连谁、用什么凭据"，
    那是 Warden 中转区解决的事——现在支持了，见下面的 `--generation`。

    现在这样安排的好处是：每一步都能单独验证，出了问题知道是哪一半。

    ## 两种取归档的方式

      * `--archive <本地文件>` —— 人已经把它传过来了（scp / 共享盘 / U 盘）
      * `--generation N`       —— 从 Warden 的中转区自己取（需要
                                  `--warden-url` / `--warden-token`）

    后者是"全自动"的最后一环：源端 `migrate --transport warden` 把归档
    PUT 上去，目标端 `land --generation N` 把它 GET 下来。两端都只跟
    已经互相认证过的 Warden 说话，中间不需要第三套凭据。
    """
    data_dir = Path(args.data).expanduser().resolve()
    if not data_dir.is_dir():
        # 新壳上 data/ 本来就不存在——AstrBot 第一次启动才会创建它。
        # 但"不存在"有两种：路径打错了，还是这台机器真的还没跑过 AstrBot。
        # 分不清就一律拒绝，会把最常见的场景（迁到一台全新的 Windows 上）
        # 变成必须先手工 mkdir 的谜题；一律放行，则 `--data G:\molt` 这种
        # 手滑会安静地在盘根上建出一个 data 目录。
        # 判据用父目录：父目录在 → 是"这台机器还没跑过"；父目录也不在 → 打错了。
        if not data_dir.parent.is_dir():
            bad(f"目标 data 目录的上层目录不存在：{data_dir.parent}")
            dim("  这通常意味着路径打错了。请给一个已经存在的位置。")
            return 2
        info(f"目标 data 目录不存在，新建它：{data_dir}")
        dim("  这是全新壳上正常的第一次——AstrBot 还没在它上面跑过。")
        try:
            data_dir.mkdir(parents=True, exist_ok=False)
        except OSError as e:
            bad(f"建不出目标 data 目录：{e}")
            return 2

    generation = getattr(args, "generation", None)
    archive_arg = getattr(args, "archive", None)
    if bool(generation) == bool(archive_arg):
        bad("必须且只能给 --archive 或 --generation 之一")
        dim("  --archive 用本地文件；--generation 从 Warden 中转区自己取。")
        return 2

    be = None
    if generation:
        try:
            be = _backend(args)
        except ValueError as e:
            bad(str(e))
            return 2
        if be.kind != "remote" and not getattr(args, "root", None):
            # 默认拒绝：不给 --warden-url 也不给 --root 时，`open_backend` 会
            # 落到"本机默认 Warden 根"，那是个几乎空的目录 —— 于是 `land`
            # 会报「这一代不存在」，而人会以为自己搞错了代次。
            #
            # 但显式给了 `--root` 就是另一回事：那是"Warden 的根就在这台机器
            # 上这个位置"，同机测试和 NAS 本机上都是真实场景。
            bad("--generation 需要 --warden-url / --warden-token 指向 NAS 上的 Warden")
            dim("  中转区在 Warden 那一侧，不是本地目录。")
            dim("  如果你确实要在这台机器上从某个 Warden 根目录取，加 --root <那个目录>。")
            return 2

    # 归档落到哪里：跟解包目录分开，因为"下载下来的 tar"和"解开之后的
    # 目录"是两个生命周期的东西。下载失败时我们希望看到的是
    # 「下载失败」，而不是「解包目录里只有一半的文件」。
    if args.to:
        work_dir = Path(args.to).expanduser().resolve()
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="molt-land-"))

    if generation:
        archive = work_dir / f"gen-{int(generation):06d}.tar.gz"
        head("从 Warden 中转区取归档")
        kv("generation", generation)
        kv("裁决者", be.where())
        kv("存到", archive)
        try:
            got_arc = be.download_archive(
                int(generation), archive, expected_sha256=args.sha256 or ""
            )
        except Fatal as e:
            bad(str(e))
            return 4
        except MoltError as e:
            bad(f"{type(e).__name__}: {e}")
            return 4
        ok(
            f"已取到 {got_arc['bytes'] / 1024 / 1024:.1f} MB "
            f"sha256 {got_arc['sha256'][:16]}…"
        )
        # 服务端已经算过并回报了哈希；把它当成后续解包的期望值，
        # 这样"下载 → 解包"这条链上 sha256 只在前一步被信任一次。
        if not args.sha256:
            args.sha256 = got_arc["sha256"]
    else:
        archive = Path(args.archive).expanduser().resolve()
        if not archive.is_file():
            bad(f"归档不存在：{archive}")
            return 2

    # 每次落位都必须是一个**全新的空目录**。
    #
    # 这里原本用 `landed-<时间戳>` 建在共享的系统临时目录下，时间戳精确到秒。
    # 实测抓到的后果：连着两次运行落在同一秒时，第二次会读到第一次留下的
    # `manifest.json` 和数据文件——于是 `land` 拿着**上一份** manifest 去
    # "校验通过"，把上一次的文件当成这一次的落进目标 data 目录。
    # 它不会报错，它只是默默地用了错的东西。
    #
    # 所以默认路径改成 mkdtemp：由内核保证唯一，不存在"同一秒撞车"这种可能。
    # 只有用户显式给了 --to 时才按时间戳命名——那种情况下"目录已存在"
    # 是个需要人看一眼的信号，直接拒绝而不是覆盖。
    if args.to:
        candidate = work_dir / f"landed-{time.strftime('%Y%m%dT%H%M%S')}"
        if candidate.exists():
            bad(f"解包目录已存在：{candidate}")
            dim("  拒绝复用它——里面的旧文件会被误认为这份归档的内容。")
            dim("  删掉它，或者用别的 --to 目录，或者等一秒再跑。")
            return 2
        gen_dir = candidate
        gen_dir.mkdir(parents=True)
    else:
        gen_dir = Path(tempfile.mkdtemp(prefix="molt-land-unpack-"))

    head("落位")
    kv("归档", archive)
    kv("目标 data", data_dir)
    kv("解包到", gen_dir)
    if args.sha256:
        kv("期望 sha256", args.sha256[:24] + "…")

    sha = args.sha256 or ""
    if not sha:
        warn("没有给 --sha256，跳过归档指纹校验")
        dim("  这意味着截断的归档也会被解包。跨机传过来的应该总是带上它。")

    try:
        extracted = extract_archive(archive, gen_dir, expected_sha256=sha or None)
    except Fatal as e:
        bad(str(e))
        return 4
    except NeedsHuman as e:
        bad(str(e))
        return 2

    ok(f"解包完成：{len(extracted['files'])} 个文件 / {extracted['bytes'] / 1024 / 1024:.1f} MB")

    mpath = gen_dir / "manifest.json"
    if not mpath.exists():
        # 走到这里说明归档里真的没有 manifest。
        #
        # 这个判断**必须**建在"全新的空目录"之上才成立。之前 `land` 用
        # 共享时间戳目录，同一秒内第二次运行会继承上一次的 manifest.json，
        # 于是这条检查永远通过，而 `land` 会拿旧 manifest 去校验新归档。
        # 真正的保险是上面那个"目录已存在就拒绝"，这一条是它的补充：
        # 万一将来有人改成允许复用目录，至少这里还有一道。
        bad("归档里没有 manifest.json——这不是一个 molt 快照")
        dim(f"  解包目录 {gen_dir} 里没有 manifest.json。")
        return 6

    try:
        m = Manifest.load(mpath)
    except NeedsHuman as e:
        bad(str(e))
        return 6
    except Fatal as e:
        # manifest 本身不完整 / 不是合法 JSON —— 这是"这份快照有问题"，
        # 不是"molt 出 bug 了"，所以按拒收处理（退出码 6，与其它校验失败一致）。
        bad(str(e))
        return 6
    if m.protocol_version != PROTOCOL_VERSION:
        bad(f"协议版本不匹配：快照 {m.protocol_version}，本机 {PROTOCOL_VERSION}")
        return 5

    # 交叉校验：manifest 说的代次必须和账本里那一代对得上。
    #
    # 为什么这条值得单独拦：`--generation N` 的意思是"取下账本里第 N 代"。
    # 如果归档里的 manifest 说它是第 7 代，那要么账本被换过、要么这个归档
    # 不是那一代的。两种情况下落位都会让本地库变成"账本说第 N 代，实际是
    # 第 7 代的内容"——之后每一次 `decide()` 都基于一个错的代次做判断。
    if generation and int(generation) != m.generation:
        bad(f"manifest 说的是{snapshot_tag(m.generation)}，但你要取的是{snapshot_tag(generation)}")
        dim("  这两者对不上，说明归档和账本里的那一条不是同一个东西。")
        dim("  用 --generation N 时，归档必须来自账本里同一份快照；换一份，或者查账本。")
        return 6

    print()
    info(f"来源 {m.source_host} ({m.platform}) / AstrBot {m.astrbot_version} / {snapshot_tag(m.generation)}")

    problems = m.verify_against(gen_dir)
    if problems:
        bad("快照校验失败，不落位：")
        for p in problems:
            print(f"    {C.RED}{C.S_BULLET}{C.R} {p}")
        return 6
    ok("快照完整性校验通过")

    # 目标 data 目录是不是还活着？——在问"要不要落位"之前先问这个。
    live = _live_data_dir_reason(data_dir)
    if live:
        bad("目标 data 目录看起来正在被一个活着的 AstrBot 使用：")
        print(f"    {C.RED}{C.S_BULLET}{C.R} {live}")
        dim("  它现在持有那个库的写连接。直接换掉文件，它会继续往被移走的旧 inode 上写，")
        dim("  而你以为新数据已经生效了 —— 两边各说各话，比崩溃难查得多。")
        dim("  先让它停下来：")
        dim("    容器：docker kill -s INT <容器名>   （不是 docker stop，那个不走优雅停机）")
        dim("    裸进程：在那个窗口里按 Ctrl+C")
        if not getattr(args, "force", False):
            dim("  确认它真的停了，再加 --force 跳过这一条。")
            return 2
        warn("--force：跳过这项检查，继续落位。")

    if not args.yes:
        # 落位会移动目标 data 里现有的文件（虽然会备份）。这一步不可自动。
        print()
        warn("落位会把目标 data 目录里的同名文件移进备份目录")
        dim(f"  备份到 {data_dir}/.molt-pre-restore-<时间戳>/")
        dim("  确认无误后加 --yes 执行。")
        return 0

    try:
        got = restore_into(gen_dir, data_dir, backup=True)
    except NeedsHuman as e:
        bad(str(e))
        return 2
    except Fatal as e:
        bad(str(e))
        return 4

    head("落位完成")
    for rel in got["restored"]:
        print(f"    {C.GRN}{C.S_OK}{C.R} {rel}")
    if got["backup_dir"]:
        kv("原有文件已备份到", got["backup_dir"])
    if got.get("sidecars_cleaned"):
        kv("清掉了落位产生的 sidecar", ", ".join(got["sidecars_cleaned"]))
    print()

    # --- 记账：告诉 Warden 这份快照已经被这台壳消费 ---
    #
    # 只在这份快照是从 Warden 取的（--generation）、且知道自己是哪只壳
    # （--shell）时登记。`--archive` 那条路是人的手工传文件，Warden
    # 根本不知道那次落位，拿一个本地 shell_id 去认领一代它没发过的
    # 快照只会制造假记录。
    #
    # 为什么这次标记失败**绝不能**让 land 失败：
    # 走到这里时 `restore_into` 已经返回，字节真的进了 data/。也就是说
    # "灵魂已经搬完"这个事实已经发生，账本只是它的一份索引。索引落后
    # 是**可恢复**的（账本会一直显示"还没人取走"，人可以稍后补记，或者
    # 至少知道这次没被记上）；而把一次成功的落位报成失败是**不可恢复**的
    # ——人会重跑 land，那份快照会再把刚恢复好的 data/ 覆盖一遍。
    # 两种错误的不对称性决定了这里只能 warn，不能 raise/return 非零。
    if generation and args.shell:
        try:
            consumed = be.mark_consumed(int(generation), args.shell)
        except MoltError as e:
            warn(
                f"字节已经落位，但账本没记住这次消费：{type(e).__name__}: {e}"
            )
            dim("  这不是落位失败：data/ 已经是这份快照的状态，可以正常启动。")
            dim("  账本落后于现实是可恢复的（status 会显示还没人取走），")
            dim("  可以确认 Warden 可用后补记这一代的消费。")
        else:
            ok(
                f"账本已记下：generation {consumed['generation']} 由 "
                f"{consumed['consumed_by']} 消费"
            )

    # --- 机器覆写层 ---
    #
    # 走到这一步时，目标 data/ 里是**从别处搬来的** cmd_config.json。
    # 它带着原来那台机器的机器绑定设置：dashboard 监听地址、jwt_secret、
    # 以及指向原机器本机服务的 provider 地址。直接起 AstrBot 的后果实测过：
    # 它在 Windows 上监听 0.0.0.0:6185，并对局域网开放，用的还是 NAS 的密钥。
    #
    # 还有一条比上面几条都重要：`platform[].enable` 也是原样搬来的 `True`，
    # 一起 AstrBot 就会去连那个 QQ 机器人。2026-11-14 14:41 真的发生过
    # （见 overlay._disable_platform_adapters 的注释）——那次只差几秒就是
    # 两条鱼同时在线。所以**平台适配器无论在哪个平台都必须关掉**，
    # 而 dashboard 那几条只在 Windows 上做（非 Windows 不猜端口）。
    cfg_path = data_dir / "cmd_config.json"
    if cfg_path.exists():
        print()
        try:
            base_cfg = load_config(cfg_path)
            if platform.system() == "Windows":
                patched, notes = overlay_for_windows(
                    base_cfg,
                    shell_id=args.shell or "windows",
                    port=args.dashboard_port or DEFAULT_DASHBOARD_PORT,
                    keep_provider_sources=getattr(args, "keep_provider_source", None) or (),
                )
                save_config(patched, cfg_path)
                head("机器覆写层（Windows）")
                for ch in notes["changes"]:
                    print(f"    {C.GRN}{C.S_OK}{C.R} {ch}")
                ok("配置已改成这台壳该有的样子。")
                dim("  jwt_secret 已重新生成——绝不跨机复用（那是安全问题）。")
            else:
                # 非 Windows：dashboard 端口不猜（猜错会让它监听在别人能访问的
                # 地址上），但平台适配器这一条与平台无关，必须关。
                # dashboard 是**壳自己的事**：Windows 上该是 127.0.0.1（不对外），
                # 容器里该是 0.0.0.0（容器网络本身隔离，且外面还要端口映射进来）。
                # 2026-11-15 01:02 真机踩到：从 Windows 回家的快照带着 127.0.0.1:6199，
                # 落在 NAS 容器里之后 WebUI 谁都连不上 —— 容器里监听 loopback，
                # 映射出去的端口后面什么都没有。所以这一条必须由目标壳自己说。
                patched, notes = apply_overlay(
                    base_cfg,
                    shell_id=args.shell or platform.system().lower(),
                    platform=platform.system().lower(),
                    drop_provider_sources=[],
                    dashboard_host=args.dashboard_host or None,
                    dashboard_port=args.dashboard_port,
                    rotate_jwt=False,
                    disable_platforms=True,
                )
                save_config(patched, cfg_path)
                head("机器覆写层（非 Windows：只做与平台无关的那几条）")
                for ch in notes["changes"]:
                    print(f"    {C.GRN}{C.S_OK}{C.R} {ch}")
                if args.dashboard_host:
                    ok(f"dashboard 已按这台壳的要求改成 {args.dashboard_host}"
                       f"{':' + str(args.dashboard_port) if args.dashboard_port else ''}")
                else:
                    warn("这是一台非 Windows 壳，dashboard 地址没有自动改")
                    dim("  请确认 dashboard.host/port 与本机 provider 地址是否符合预期。")
                    dim("  （不猜是刻意的：猜错会让它监听在别人能访问的地址上。）")
        except (Fatal, NeedsHuman) as e:
            bad(str(e))
            dim("  落位本身已完成，但配置没能自动调整。请手动检查 dashboard 段。")
            dim("  ⚠️ 尤其要确认 platform[].enable 是 false——它连着 QQ 账号。")
            return 0

    print()

    # --- 搬来的插件配置：只写这台壳也装了的插件 ---
    #
    # 位置刻意放在机器覆写层之后：覆写层只碰 cmd_config.json（壳自己的事），
    # 这一层只碰 data/config/<插件>_config.json（鱼喜欢什么）。两者互不覆盖，
    # 但顺序错了会让人以为是同一件事。
    if not getattr(args, "no_plugin_configs", False):
        try:
            carried = landcfg.apply_carried_configs(gen_dir, data_dir)
        except (Fatal, NeedsHuman) as e:
            bad(f"插件配置没能写回：{e}")
            dim("  落位本身已完成；这不会挡住启动，只是这台壳还用着旧的那份配置。")
        else:
            if carried["applied"] or carried["schema"] or carried["not_installed"]:
                head("搬来的插件配置")
                for plugin in carried["applied"]:
                    bak = carried["backups"].get(plugin) or ""
                    extra = f"（旧文件留在 {Path(bak).name}）" if bak else ""
                    print(f"    {C.GRN}{C.S_OK}{C.R} {plugin} 已换成搬来的那份{extra}")
                for plugin in carried["same"]:
                    print(f"    {C.DIM}{C.S_OK} {plugin} 本来一样，没动{C.R}")
                for plugin in carried["schema"]:
                    warn(f"{plugin} 两边结构差太多（多半插件版本不同），保留这边的")
                for plugin in carried["not_installed"]:
                    dim(f"  {plugin} 这边没装 —— 不写（装了才会有配置）")
                if carried["applied"]:
                    dim("  这些配置要下一次重载插件才生效：tools/reload-plugin.py")

    ok("现在这台壳的 data/ 就是那个存在的状态了。")
    if cfg_path.exists():
        dim("  ⚠️ 平台适配器已关闭：这只鱼现在还没连上 QQ。")
        dim("     确认账本里只有你在持租约之后，再手动把 platform[].enable 打开。")
    dim("  下一步：起 AstrBot（记得 --data 指向这里），让它用这份状态跑起来。")
    if m.runtime_pip:
        warn(f"还需要重装 {len(m.runtime_pip)} 个运行时包（manifest 里有清单）：")
        for pkg in m.runtime_pip[:8]:
            print(f"    {C.DIM}{pkg}{C.R}")
        if len(m.runtime_pip) > 8:
            dim(f"    … 共 {len(m.runtime_pip)} 个")
    return 0


def cmd_wake(args: argparse.Namespace) -> int:
    """把睡着的目标壳叫醒。

    这个命令存在的原因：迁徙的第一步不是"备份"，而是"目标机得是开着的"。
    在此之前那一步一直是人手做的（人按电源键，或者人跑 `wol-test.py`），
    而**人手做的第一步会让整条链子看起来是自动的、其实不是**。

    ## 为什么 `--wait` 是独立的开关

    "发出去了"和"醒了"是两件事，中间隔着 BIOS POST + 引导 + 网络栈起来。
    真机实测：S5 关机 → 发包 → 网络恢复 **23.1 秒**，但 AstrBot 监听 6199
    还要再等几十秒。所以"等不等""等哪个判据"必须由调用者决定：
    `--wait` 用 ICMP（网络栈起来就算醒），`--wait-port` 用 TCP（服务能干活才算醒）。

    ## 为什么叫不醒要报错退出（而不是重试到成功）

    `wait_online()` 超时抛 `NeedsHuman`。叫不醒只有三种原因（网卡没 arm /
    上电后没进到能联网的状态 / 换了网卡或驱动把设置重置了），**每一种都需要
    人看一眼**。静默重试会让人以为系统在工作，而实际上它永远叫不醒。
    """
    mac = args.mac
    host = args.host or ""

    head("唤醒目标")
    # 从 format_mac 开始就要在 try 里：它会对坏 MAC 抛 Fatal，
    # 而"参数错"（exit 4）和"目标叫不醒"（exit 5）必须区分开 ——
    # 这两种故障要人做的事完全不同，混成一个退出码等于让人自己猜。
    try:
        kv("MAC", W.format_mac(mac))
        kv("目标", host or "（只知道 MAC，走全网广播）")

        targets = W.wake_targets(host, args.prefix)
        kv("发包路径", " / ".join(targets))
        if len(targets) == 1:
            dim("  只发广播：机器关机后 ARP 表项会过期，单播出不去，广播才是主力。")
        print()

        result = W.wake(
            mac,
            host=host,
            prefix_len=args.prefix,
            port=args.port,
            count=args.count,
            interval=args.interval,
            quiet=args.quiet,
        )
    except Fatal as e:
        bad(str(e))
        return 4

    kv("轮次", f"{result['rounds_ok']}/{result['rounds_sent']} 轮至少一个路径成功")
    kv("封包", f"{result['packet_bytes']} 字节 × {result['packets']} 个")
    print()

    if not result["any_sent"]:
        # 一个都发不出去 = 环境问题，不是"机器没醒"。这两件事必须分开报，
        # 否则人会去检查机器，而真正的问题是 NAS 自己的网络。
        bad("一个封包都没发出去。")
        dim("  这不是目标机的问题，是本机发不出去：检查网卡、防火墙、广播权限。")
        for r in result["rounds"][0]["results"]:
            dim(f"    {r['target']}: {r['error']}")
        return 4

    ok("封包已发出。")

    if not args.wait and not args.wait_port:
        print()
        dim("  没等它醒（--wait 才等）。魔术封包只能「请求」开机，不保证成功。")
        return 0

    print()
    timeout = args.wait_timeout
    criterion = f"tcp/{args.wait_port}" if args.wait_port else "icmp"
    info(f"等它起来（判据 {criterion}，最多 {timeout:.0f} 秒）…")

    def tick(n: int, secs: float) -> None:
        # 每 5 次打一行，避免刷屏。真机上 23 秒醒来，所以大约 2-3 行。
        if n % 5 == 0:
            dim(f"  第 {n} 次探测，已等 {secs:.1f} 秒")

    if not (args.wait_port or None) and not W.can_probe_icmp():
        raise NeedsHuman(
            "这台机器上没有 ping，ICMP 判据用不了。"
            "\n  这不是「目标机没醒」——是我没法判断它醒没醒。"
            "\n  给一个 TCP 判据试试：--wait-port <端口>"
        )
    try:
        r = W.wait_online(
            host,
            port=args.wait_port or None,
            timeout=timeout,
            interval=args.wait_interval,
            on_tick=tick,
        )
    except NeedsHuman as e:
        print()
        bad(str(e))
        return 5

    print()
    ok(f"它起来了：{r['seconds']} 秒（{r['criterion']}，探测 {r['attempts']} 次）")
    return 0


def _human_size(n: int) -> str:
    """把字节数变成人能读的大小。中转区里的东西从 KB 到 GB 都可能。"""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def cmd_machine(args: argparse.Namespace) -> int:
    """看 / 叫醒 / 关掉账本里的任意一台壳 —— 从**鱼待着的那台机器**发出去。

    这是「鱼常驻 NAS，Windows 只是工具」这条产品设定的操作面：
    NAS 上的鱼不需要把自己搬过去，就能知道那台机器在不在、能不能叫醒、
    要不要关掉。三件事本来就在（心跳、`/wake`、关机票），缺的是把它们
    放在一条命令里，并且**说清楚每一下的代价**。
    """
    be = _backend(args)
    action = str(getattr(args, "machine_action", "") or "list").strip().lower()
    shell = str(getattr(args, "shell", "") or "").strip()
    shells = be.shells()
    status = be.status()
    latest = status.get("latest_generation") or {}
    here = str(latest.get("consumed_by") or "").strip()
    beats = status.get("heartbeats") or {}
    tickets = status.get("power") or {}

    def age_of(shell_id: str) -> Optional[float]:
        beat = beats.get(shell_id) or {}
        try:
            value = beat.get("age_seconds")
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def dsh_of(attrs: Dict[str, object]) -> str:
        """那台机器上的 DeepSeek Harness 还通不通。

        只探 TCP 3082，不判"是不是 DSH" —— 这一步的目的是回答
        「那台机器醒着吗、它的工具还连着吗」，而不是鉴权。
        0.4 秒就放弃：这条命令是给人看的，不能让人等。
        """
        address = str(attrs.get("address") or "").strip()
        if not address:
            return ""
        try:
            with socket.create_connection((address, 3082), timeout=0.4):
                return "通"
        except OSError:
            return "不通"

    if action == "list":
        if not shells:
            warn("账本里还没有任何壳的属性。")
            dim("  先让守护进程声明自己：molt declare --shell <壳名> --role nas")
            return 0
        head("账本里的机器")
        for index, shell_id in enumerate(machines.ordered_shells(shells), 1):
            attrs = shells.get(shell_id) or {}
            age = age_of(shell_id)
            bits = [f"{C.CYA}{shell_id}{C.R}", str(attrs.get("role") or "（没登记）")]
            if shell_id == here:
                bits.append(f"{C.GRN}鱼在这里{C.R}")
            alive = age is not None and age <= 25
            if age is None:
                bits.append(f"{C.YEL}没上报心跳{C.R}")
            elif alive:
                bits.append(f"{C.GRN}在跑（{age:.0f} 秒前）{C.R}")
            else:
                bits.append(f"{C.YEL}{age / 60:.0f} 分钟没心跳{C.R}")
            if attrs.get("address"):
                status_text = dsh_of(attrs)
                bits.append(f"地址 {attrs['address']}" + (f"（DSH {status_text}）" if status_text else ""))
            bits.append(
                machines.power_text(
                    alive=alive,
                    wakeable=attrs.get("wakeable"),
                    has_mac=bool(str(attrs.get("wake_mac") or "").strip()),
                )
            )
            if shell_id in tickets:
                bits.append(f"{C.YEL}有一张关机票{C.R}")
            print(f"  {index}. " + " · ".join(bits))
        if here:
            print()
            kv("鱼现在在", here)
        dim("  叫醒一台：molt machine on <序号或名字>　关掉：molt machine off <序号或名字>")
        return 0

    if not shell:
        bad("要指明哪台机器：molt machine on <序号或名字> / molt machine off <序号或名字>")
        return 2
    shell, problem = machines.resolve(shell, shells)
    if problem:
        bad(problem)
        return 4

    attrs = shells.get(shell) or {}

    if action == "on":
        mac = str(attrs.get("wake_mac") or "").strip()
        if not mac:
            bad(f"{shell} 没有登记 MAC —— 叫不醒它。")
            dim(f"  先：molt declare --shell {shell} --wake-mac AA-BB-CC-DD-EE-FF")
            return 4
        try:
            got = be.wake(mac)
        except NeedsHuman as exc:
            bad(str(exc))
            return 4
        sent = got.get("sent") or []
        head("已发魔术封包")
        kv("目标", shell)
        kv("MAC", mac)
        kv("发往", "、".join(str(x) for x in sent) or "（没有）")
        dim("  封包只负责「按下电源键」，能不能起来要看它自己；起来之后 agent 会自己上报心跳。")
        return 0 if sent else 4

    if action == "off":
        if shell == here:
            bad(f"鱼就在 {shell} 上 —— 关掉它等于把鱼一起关掉。")
            dim("  先把鱼搬走（/molt go）或者换一台机器。")
            return 4
        try:
            ticket = be.request_power(shell, "shutdown", by="molt machine")
        except NeedsHuman as exc:
            bad(str(exc))
            return 4
        head("已投一张关机票")
        kv("目标", shell)
        kv("动作", "shutdown")
        kv("有效期到", str(ticket.get("expires_at_readable") or "？"))
        dim(f"  {shell} 的 agent 下一轮心跳就会看到它；它在跑才会执行。")
        dim("  反悔：molt machine off 之前先投一张 keep —— 或者等它自己过期。")
        return 0

    bad(f"machine 只认 list / on / off，收到 {action!r}")
    return 2


def cmd_shells(args: argparse.Namespace) -> int:
    """账本里登记的「每台壳是什么」。

    和 `molt shell` 的区别：`molt shell` 是**在本地探测这台机器**（进程、
    路径、依赖能不能跑），不联网；这一条是**从账本读回来的声明**（这台
    机器自称是什么）。两者会不一致 —— 而那种不一致本身就是要看的东西。
    """
    be = _backend(args)
    shells = be.shells()
    if args.json:
        print(json.dumps(shells, ensure_ascii=False, indent=2))
        return 0
    if not shells:
        warn("账本里还没有任何壳的属性。")
        dim("  守护进程启动时会自己声明（molt init 已经把 role 写进配置）；")
        dim("  也可以手动登记：molt declare --role nas --shell <壳名> --warden-url … --token …")
        return 0
    head("账本里的壳")
    for shell_id, attrs in sorted(shells.items()):
        role = attrs.get("role") or "（没登记）"
        bits = [f"{C.CYA}{shell_id}{C.R}", role]
        if attrs.get("gpu"):
            bits.append(f"gpu={attrs['gpu']}")
        # True / False / 没声明 是三件事，不能压成"说"和"不说"：
        # 把 False 印成空白，等于把"我确认它叫不醒"藏起来。
        if attrs.get("wakeable") is True:
            bits.append("可唤醒")
        elif attrs.get("wakeable") is False:
            bits.append("不可唤醒")
        cost = attrs.get("cost_per_hour")
        if cost:
            bits.append(f"{cost}/小时")
        print("  " + "  ".join(bits))
        if attrs.get("updated_at_readable"):
            dim(f"      {attrs['updated_at_readable']} 由 {attrs.get('updated_by') or '未署名'} 登记")
    return 0


def _next_due_text(policy, *, now: float) -> str:
    """下一次会在什么时候触发。算不出来就明说，不要假装。"""
    import time as _time

    from .schedule import schedule_due, schedule_spec

    if not schedule_spec(policy.get("schedule")):
        return "定时规则读不懂 —— 它永远不会自己搬"
    for step in range(0, 24 * 60, 5):
        probe = now + step * 60
        if schedule_due(policy.get("schedule"), probe):
            delta = max(int(probe - now), 0)
            hh, mm = divmod(delta // 60, 60)
            stamp = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(probe))
            return f"{stamp}（还有 {hh} 小时 {mm} 分）"
    return "24 小时内不会再触发（或已经过了今天的窗口）"


def _policy_status(be, args) -> int:
    """把「它明早会不会真的自己搬」拆成一条条能当场验的检查。"""
    import time as _time

    st = be.status() or {}
    policy = st.get("policy") or {}
    if not policy:
        warn("现在没有自动搬家策略 —— 它不会自己搬。")
        dim("  设一条：molt policy set --id night-home --source <壳> --target <壳> --at 23:30")
        return 0

    head("自动搬家：它会不会自己动")
    kv("id", str(policy.get("id")))
    kv("从哪到哪", f"{policy.get('source')} → {policy.get('target')}")
    kv("下次触发", _next_due_text(policy, now=_time.time()))

    attempts = st.get("attempts") or []
    today = _time.strftime("%Y-%m-%d")
    used = len([a for a in attempts if str(a.get("at_readable", "")).startswith(today)])
    limit = int(policy.get("daily_limit") or 0)
    kv("今天已经自动试过", f"{used} / {limit} 次" if limit else f"{used} 次（不限）")

    shell = str(getattr(args, "shell", "") or "").strip()
    source = str(policy.get("source") or "")
    target = str(policy.get("target") or "")
    beat = (st.get("heartbeats") or {}).get(target) or {}
    age = beat.get("age_seconds")
    lease = st.get("lease") or {}
    attrs = (st.get("shells") or {}).get(target) or {}

    checks = [
        ("策略开在源壳上", shell == source, f"我是 {shell or '？（没给 --shell）'}，策略在 {source}"),
        (
            "目标壳最近在报心跳",
            isinstance(age, (int, float)) and age <= 25,
            f"最后心跳 {age:.0f} 秒前" if isinstance(age, (int, float)) else "账本里没有它的心跳",
        ),
        ("现在没有租约", lease.get("state") == "no-lease", f"租约 {lease.get('state')}"),
        (
            "目标壳叫得醒（活着，或账本里有它的 MAC）",
            (isinstance(age, (int, float)) and age <= 25)
            or bool(attrs.get("wake_mac"))
            or attrs.get("wakeable") is not False,
            f"心跳 {age:.0f} 秒前 / wake_mac={attrs.get('wake_mac') or '（空）'}"
            if isinstance(age, (int, float))
            else f"没有心跳 / wake_mac={attrs.get('wake_mac') or '（空）'}",
        ),
        (
            "搬完关旧壳的判据齐了",
            not policy.get("power_off_source") or policy.get("power_action") in ("shutdown", "sleep"),
            f"power_off_source={policy.get('power_off_source')} / action={policy.get('power_action')}",
        ),
    ]

    print()
    head("到点那一刻还要过这些（现在逐条验）")
    passed = 0
    for name, good, detail in checks:
        if good:
            passed += 1
            ok(f"{name}  ·  {detail}")
        else:
            bad(f"{name}  ·  {detail}")
    print()
    if passed == len(checks):
        ok(f"{passed}/{len(checks)} 条都成立 —— 到点它会自己动。")
    else:
        warn(f"{passed}/{len(checks)} 条成立；不成立的那几条会让它到点按兵不动（鱼不会死，只是不走）。")
    return 0


def cmd_dsh(args: argparse.Namespace) -> int:
    """跨机跟另一台机器上的 DSH 说话 —— 由账本代转，只放三个方法。

    为什么不让调用方直接连：DSH 的 host 服务按回环地址做 Host 校验，
    从 NAS 连 `http://<win>:3082` 在协议层就不成立（见 `molt/dshrelay.py`）。
    """
    from . import dshrelay

    be = _backend(args)
    action = getattr(args, "dsh_action", "") or "sessions"
    shell = str(getattr(args, "shell", "") or "").strip()
    if not shell:
        bad("要指定是哪台机器：--shell <壳名>（先跑的 molt machine list 看看）")
        return 2

    def call(method: str, payload: Any) -> Any:
        return (be.dsh(shell, method, payload) or {}).get("dsh") or {}

    if action == "sessions":
        value = call("session/list", {"_request": {}})
        items = value.get("items") or []
        head(f"{shell} 上的会话")
        if not items:
            dim("  一个都没有。")
            return 0
        import time as _t

        for item in items[: int(getattr(args, "limit", 10) or 10)]:
            sid = str(item.get("sessionId") or "")
            stamp = float(item.get("updatedAt") or 0) / 1000.0
            when = _t.strftime("%m-%d %H:%M", _t.localtime(stamp)) if stamp else "?"
            running = "在跑" if item.get("running") else "闲着"
            kv(sid[:20], f"{when} · {running} · {item.get('cwd') or ''}")
        dim("  读历史：molt dsh history --shell <壳> --session <id>")
        dim("  发一句：molt dsh send --shell <壳> --session <id> \"你在吗\"")
        return 0

    if action == "history":
        sid = str(getattr(args, "session", "") or "").strip()
        if not sid:
            bad("要指定会话：--session <id>（用 molt dsh sessions 列）")
            return 2
        payload = {
            "request": {
                "address": {"kind": "session", "sessionId": sid},
                "maxMessages": int(getattr(args, "limit", 20) or 20),
            }
        }
        value = call("session/events", payload)
        if getattr(args, "json", False):
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        lines = dshrelay.summarize_events(value, limit=int(getattr(args, "limit", 20) or 20))
        head(f"{sid[:20]} 的最近 {len(lines)} 条")
        for line in lines:
            print(f"  {line}")
        if not lines:
            dim("  （这一页里没有文字消息）")
        return 0

    if action == "send":
        sid = str(getattr(args, "session", "") or "").strip()
        text = " ".join(getattr(args, "text", []) or []).strip()
        if not sid:
            bad("要指定会话：--session <id>")
            return 2
        if not text:
            bad("要发点什么：molt dsh send --shell <壳> --session <id> \"…\"")
            return 2
        mode = "steer" if getattr(args, "steer", False) else "queue"
        if getattr(args, "dry_run", False):
            dim("空转：这一句不会发出去。")
            kv("目标", f"{shell} / {sid[:20]}")
            kv("模式", mode)
            kv("内容", text)
            return 0
        value = call(
            "session/prompt",
            {
                "request": {
                    "requestId": __import__("uuid").uuid4().hex,
                    "sessionId": sid,
                    "mode": mode,
                    "content": [{"type": "text", "text": text}],
                }
            },
        )
        ok(f"发出去了（{mode}）：{str(value)[:120]}")
        return 0

    # call：通用逃生口，但方法仍受白名单限制
    method = str(getattr(args, "method", "") or "").strip()
    raw = str(getattr(args, "args", "") or "").strip() or "{}"
    if not method:
        bad("要指定方法：--method session/list")
        return 2
    if not dshrelay.is_allowed(method):
        bad(f"{method} 不在代转白名单里：{dshrelay.describe_scope()}")
        return 2
    try:
        payload = json.loads(raw)
    except ValueError as e:
        bad(f"--args 不是合法 JSON：{e}")
        return 2
    value = call(method, payload)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


def cmd_policy(args: argparse.Namespace) -> int:
    """自动搬家策略（M2）：看、设、撤。

    为什么策略要存在**账本**里、而不是守护进程的配置文件里：
    ① 撤销要能立刻生效 —— 改本地文件得等那台机器下一轮读到；
    ② 谁授权的要留得下来 —— 配置文件是"这台机器的脾气"，
       策略是"人允许它做什么"；
    ③ 自动搬家跑的那条命令本来就要读账本，多读一个字段不多一步。
    """
    be = _backend(args)
    action = getattr(args, "policy_action", "") or "show"
    try:
        return _run_policy(be, args, action)
    except Fatal as e:
        bad(str(e))
        return 2
    except NeedsHuman as e:
        bad(str(e))
        return 2


def _run_policy(be, args: argparse.Namespace, action: str) -> int:
    """`molt policy` 的真正执行体 —— 把异常留给调用方统一翻译成退出码。"""

    if action == "show":
        got = be.policy()
        policy = got.get("policy") or {}
        attempts = got.get("attempts") or []
        if not policy:
            warn("现在没有自动搬家策略 —— 只有你 /molt confirm 它才会动。")
            dim("  设一条：molt policy set --id night-home --source <壳> --target <壳> --at 23:30")
            return 0
        head("自动搬家策略")
        kv("id", policy.get("id", ""))
        kv("开关", "开着" if policy.get("enabled") else "关着")
        kv("从哪到哪", f"{policy.get('source')} → {policy.get('target')}")
        kv("什么时候", json.dumps(policy.get("schedule"), ensure_ascii=False))
        kv("每天最多", f"{policy.get('daily_limit')} 次")
        expires = float(policy.get("expires_at") or 0)
        kv("过期", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires)) if expires else "不过期")
        if policy.get("created_by"):
            kv("谁设的", str(policy["created_by"]))
        if policy.get("power_off_source"):
            kv(
                "搬完关旧壳",
                f'是（{policy.get("power_action")}；等播报回执 '
                f'{policy.get("arrival_timeout_seconds")} 秒，等不到就不关）',
            )
        else:
            kv("搬完关旧壳", "不关")
        if policy.get("release_when_away"):
            kv("鱼已经在家时", f'也把源壳关掉（{policy.get("power_action")}）')
        if attempts:
            head("最近几次自动尝试")
            for item in attempts[-5:]:
                mark = {"ok": "✓", "failed": "✗", "skipped": "·"}.get(item.get("result"), "?")
                print(f"  {mark} {item.get('at_readable', '')}  {item.get('reason', '')[:80]}")
        return 0

    if action == "status":
        return _policy_status(be, args)

    if action == "off":
        got = be.clear_policy()
        if got.get("cleared"):
            ok("撤销了 —— 从现在起它不会自己搬任何东西。")
        else:
            info("本来就没有自动搬家策略。")
        return 0

    # set
    from .autopilot import DEFAULT_DAILY_LIMIT, policy_problems

    policy = {
        "id": args.id,
        "enabled": True,
        "source": args.source,
        "target": args.target,
        "schedule": args.at,
        "window_minutes": int(args.window_minutes or 0),
        "daily_limit": int(args.daily_limit or DEFAULT_DAILY_LIMIT),
        "expires_at": float(args.expires_at or 0.0),
        "created_by": args.by or "",
        "power_off_source": bool(getattr(args, "power_off", False)),
        "power_action": str(getattr(args, "power_action", "") or "shutdown"),
        "release_when_away": bool(getattr(args, "release_when_away", False)),
        "arrival_timeout_seconds": float(getattr(args, "arrival_timeout", 0) or 120.0),
    }
    problems = policy_problems(policy)
    if problems:
        bad("这条策略还不能存：" + "；".join(problems))
        return 2
    got = be.set_policy(policy, by=args.by or "")
    # 本地后端直接回策略本身，远端后端回 {"ok":…, "policy": {…}} —— 两种形状
    saved = got.get("policy") if isinstance(got, dict) and "policy" in got else got
    saved = saved or {}
    ok(f"存好了：{saved.get('id')}（{saved.get('source')} → {saved.get('target')}）")
    dim("  它只在源壳上生效，而且到点之后还要过：鱼在这台机器上 / 没有租约 / 今天没超次数 / 不在冷却里。")
    dim("  撤销：molt policy off")
    return 0


def cmd_declare(args: argparse.Namespace) -> int:
    """登记「这台机器是什么」。

    `role` 收窄成四个值（nas / desktop / cloud / unknown）是刻意的：自由文本
    最后一定会变成 `role in ("nas", "NAS", "nas-box")` 这种东西，而它只在
    出事的那天被发现有漏洞。`gpu` 则是自由文本 —— 因为"什么算能打游戏"
    只有人知道，程序不该替人下这个定义。
    """
    shell_id = str(getattr(args, "shell", "") or "").strip()
    if not shell_id:
        bad("没有 --shell —— 账本里给哪台机器登记，这件事没有默认值")
        return 2

    attrs: Dict[str, object] = {}
    if args.role is not None:
        attrs["role"] = args.role
    if args.gpu is not None:
        attrs["gpu"] = args.gpu
    if args.wakeable is not None:
        attrs["wakeable"] = bool(args.wakeable)
    if args.cost_per_hour is not None:
        attrs["cost_per_hour"] = args.cost_per_hour
    if args.label is not None:
        attrs["label"] = args.label
    if getattr(args, "wake_mac", None) is not None:
        attrs["wake_mac"] = args.wake_mac
    if getattr(args, "address", None) is not None:
        attrs["address"] = args.address
    if getattr(args, "dsh_relay", None) is not None:
        attrs["dsh_relay"] = args.dsh_relay
    if not attrs:
        bad("什么都没给 —— 至少要 --role / --gpu / --wakeable / --cost-per-hour / --wake-mac 之一")
        return 2

    be = _backend(args)
    try:
        got = be.set_shell(shell_id, by=args.by or "", **attrs)
    except NeedsHuman as exc:
        bad(str(exc))
        return 4

    head("已登记")
    kv("壳", got.get("shell", shell_id) or shell_id)
    for key in ("role", "gpu", "wakeable", "cost_per_hour", "label", "wake_mac", "address"):
        if key in attrs:
            kv(key, got.get(key))
    dim("  这是**声明**，不是探测：账本只记你说它是什么。")
    dim("  看全部：molt shells --warden-url … --token …")
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    """把「它搬过几次家」渲染成一页 HTML（或打出 JSON）。

    ## 为什么这一页必须能离线打开

    这一页是准备发出去给人看的：陌生人点开它、看见那条时间轴，才相信
    「同一个它真的换了身体」。所以它**不引用任何外部资源**（CDN、字体、
    图片、脚本都没有），断网的机器上、手机上、别人的电脑上打开都一样。

    ## 为什么不直接 print 到终端

    时间轴是二维的（代次 × 时间），终端里只能一行一行往下滚，反而比
    日志更难读。终端里给一句摘要和文件路径，看图去浏览器里看。

    ## 退出码

    0 = 成功（`--json` 也算成功）
    2 = 参数不合法
    4 = 连不上 Warden / Warden 报错
    """
    demo = bool(getattr(args, "demo", False))
    if demo:
        # 不需要账本、不需要服务器、不联网 —— 刚装完的人账本是空的，
        # 而"看见迁移日志"这件事恰恰是他最想看的那一眼。
        history = render.demo_history()
    else:
        be = _backend(args)
        try:
            history = be.history()
        except MoltError as e:
            bad(f"读不到账本：{e}")
            return 4

    if getattr(args, "json", False):
        print(json.dumps(history, ensure_ascii=False, indent=2))
        return 0

    page = render_timeline(
        history,
        title="它搬过几次家 · 示例" if demo else "它搬过几次家",
        note=(
            "这是示例数据 —— 不是你机器上的迁移记录。\n"
            "真的那一页，等你第一次迁徙之后用 molt timeline 生成。"
            if demo
            else ""
        ),
    )
    out = Path(args.out).expanduser() if args.out else Path("molt-timeline.html")
    if out.parent and str(out.parent) not in ("", "."):
        out.parent.mkdir(parents=True, exist_ok=True)
    try:
        out.write_text(page, encoding="utf-8")
    except OSError as e:
        bad(f"写不进去：{out}（{e}）")
        return 2

    generations = history.get("generations") or []
    done = sum(1 for g in generations if g.get("consumed_by"))
    if demo:
        warn("这是示例数据 —— 不是你机器上的记录。真的那一页等你迁徙一次之后再生成。")
    ok(f"已经搬过 {len(generations)} 次，完成落地 {done} 次。")
    if generations:
        latest = generations[-1]
        kv("最近一次搬家", move_count(latest.get("generation")))
        kv("从哪来", str(latest.get("source_host") or "—"))
        kv("谁取走的", str(latest.get("consumed_by") or "还没人取走"))
    kv("搬到", str(out.resolve()))
    print()
    dim("  把这一页发出去，或者自己打开看一眼。它不联网，断网也能看。")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """清理 Warden 中转区里"账本不认"的归档。

    ## 为什么需要一个命令而不是自动清

    因为自动清理会删掉人正在看的东西。中转区里的归档有四种来历：
    ①账本里记着、目标壳还没取（**绝不能删**）②上传成功但 push 失败
    （那次迁徙废了，可以删）③来自更早的、已经清理过的账本（可删）
    ④人手工扔进来的（不归我们管，不删）。判据只有第一条是硬边界，
    其余都要靠 `keep_recent` 这种"留几份缓冲"的直觉来兜。

    把判断交给命令 + 默认 `dry_run`，人就能先看一眼"它会删哪些"。

    ## 退出码

    0 = 成功（dry-run 也算成功，它完成了它承诺的事）
    2 = 参数不合法
    4 = 连不上 Warden / Warden 报错
    """
    try:
        be = _backend(args)
    except ValueError as e:
        bad(str(e))
        return 2

    if args.keep_recent < 0:
        bad(f"--keep-recent 不能是负数：{args.keep_recent}")
        return 2

    try:
        result = be.prune(keep_recent=args.keep_recent, dry_run=not args.yes)
    except Fatal as e:
        bad(str(e))
        return 4
    except MoltError as e:
        bad(str(e))
        return 4

    head("中转区")
    kv("位置", result.get("archive_dir", be.where()))
    kv("保留最近", f"{args.keep_recent} 份")

    removed = result.get("removed") or []
    kept = result.get("kept") or []
    freed = int(result.get("freed_bytes") or 0)
    dry = bool(result.get("dry_run", True))

    if not removed:
        ok("没有可清理的归档 —— 每一份都在账本里，或者都是别人放的")
    else:
        print()
        if dry:
            warn(f"将要删除 {len(removed)} 份（共 {_human_size(freed)}）—— 这次没动手")
        else:
            ok(f"已删除 {len(removed)} 份，腾出 {_human_size(freed)}")
        for item in removed:
            name = item.get("name", "?")
            size = _human_size(int(item.get("bytes") or 0))
            reason = item.get("reason", "")
            marker = "?" if dry else "-"
            print(f"  {marker} {name}  {size}  {C.DIM}{reason}{C.R}")

    if kept:
        print()
        dim(f"  保留 {len(kept)} 份：")
        for item in kept[:12]:
            name = item.get("name", "?")
            reason = item.get("reason", "")
            dim(f"    {name}  {reason}")
        if len(kept) > 12:
            dim(f"    …… 还有 {len(kept) - 12} 份")

    print()
    if dry:
        dim("  想真的删：加上 --yes")
    else:
        dim("  这些文件已经不在盘上了。账本里记着的永远不删。")
    return 0


def _wait_for_webui(base: str, *, timeout: float = 90.0, interval: float = 2.0) -> bool:
    """等这只壳的 WebUI **装配完**，而不只是端口通了。

    判据用 `GET /api/auth/setup-status`：进程起来时端口可能已经在听，
    但 dashboard 的路由还没挂上，这时候 POST 出去只会拿到 404。
    4xx 也算"在应答" —— 那说明路由在，只是我们没带凭据。
    """
    deadline = time.monotonic() + max(0.0, timeout)
    url = f"{base}/api/auth/setup-status"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3.0) as resp:
                if resp.status < 500:
                    return True
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return True
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(interval)
    return False


def cmd_observe(args: argparse.Namespace) -> int:
    """让这只壳自己做一轮「我观察到自己搬过来了」。

    为什么要有这条命令：插件里有同样的逻辑，但它依赖一个后台任务；2026-11-15
    实测 AstrBot 给插件初始化的那个事件循环并不总是之后在跑的服务循环，
    后台任务会静默地永不执行。**请求处理器一定跑在服务循环上** —— 所以让
    落地的守护进程在壳起来之后敲一下这个端点，比指望后台任务可靠。

    退出码：0 = 问到了（无论记没记住），2 = 参数/配置问题。
    "没记住"不是失败 —— 交接本身早就完成了。
    """
    from .control import CONTROL_PREFIX, read_dashboard_credentials, sign_dashboard_jwt

    config_path = Path(args.config)
    try:
        secret, username = read_dashboard_credentials(config_path)
    except (Fatal, NeedsHuman) as e:
        bad(str(e))
        return 2

    base = args.base.rstrip("/")
    ok(f"等 {base} 的 WebUI 能应答（最多 {args.wait:.0f} 秒）…")
    if not _wait_for_webui(base, timeout=args.wait):
        warn("WebUI 一直没应答 —— 这次先不记了。")
        dim("  交接本身不受影响：这是「没记住」，不是「搬失败」。")
        return 0

    token = sign_dashboard_jwt(secret, username)
    url = f"{base}{CONTROL_PREFIX}/observe"
    req = urllib.request.Request(
        url, data=b"{}", headers={"Authorization": f"Bearer {token}"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        warn(f"这只壳拒绝了（HTTP {e.code}）：{body}")
        return 0
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        warn(f"连不上这只壳的控制面：{e}")
        return 0

    action = str(payload.get("action") or "")
    if payload.get("status") == "error" or (not action and not payload.get("success")):
        warn(f"这只壳说：{payload.get('message') or payload.get('error') or '没听懂'}")
    if action == "recorded":
        ok(f"记下了：{move_count(payload.get('generation'))}（走 {payload.get('via')}）")
    elif action == "skip":
        dim(f"不用记：{payload.get('reason') or ''}")
    else:
        warn(f"没记成：{payload.get('reason') or action or '未知原因'}")
        dim("  这不影响交接 —— 下一轮还会再试。")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """把 Warden 跑成一个 HTTP 服务，等远端的壳来敲门。

    这个命令是"权威住在库旁边"这条原则的落地形态：它必须跑在
    **放着 state.json 和 data/ 的那台机器上**（也就是 NAS），
    而不是跑在壳上。

    前台阻塞运行（不 daemon 化）。理由：v0.1 阶段人在旁边看着它，
    能直接看到每个请求；真要做成服务，交给 systemd / 计划任务，
    而不是让 python 自己去 fork。
    """
    from .server import WardenServer

    root = Path(args.root).expanduser() if args.root else DEFAULT_ROOT
    srv = WardenServer(
        root=root,
        token=args.token,
        host=args.host,
        port=args.port,
    )

    head("Warden HTTP 服务")
    kv("根目录", root)
    kv("绑定", f"{args.host}:{args.port}")
    print()

    if srv.generated_token:
        # 只在生成时打印一次，且明确说"只显示这一次"——
        # token 会出现在终端回滚里，人应该把它当密码对待。
        warn("没有指定 --token，已随机生成一个。它只显示这一次：")
        print()
        print(f"    {C.B}{srv.token}{C.R}")
        print()
        dim("  把它记到壳那边：molt status --warden-url http://<这台机器>:%d --warden-token <上面那串>" % args.port)
    else:
        info("使用命令行提供的 token")

    print()
    dim("  Ctrl+C 停止。")
    print()

    try:
        host, port = srv.start()
        ok(f"已监听 http://{host}:{port}")
        # 主线程只等待。所有实际工作都在 HTTP 处理线程里。
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print()
        info("收到 Ctrl+C，正在停止 …")
    except OSError as e:
        bad(f"起不来：{e}")
        dim("  端口被占用了？换一个 --port 试试。")
        return 3
    finally:
        srv.stop()

    ok("已停止")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """把「第一次装」这件事从 README 搬进代码。

    它只做三件事：写一份**能被真正的加载器读通**的配置、生成一个不会被
    shell 咬到的 token、把「下一步该敲什么」打印出来。它不猜壳名，也不
    覆盖已有配置 —— 一份调通的配置比一份「更新过的」配置值钱。
    """
    role = str(getattr(args, "role", "") or "").strip()
    if role not in ("agent", "homer", "warden"):
        bad(f"不认识的角色：{role or '（空）'}")
        dim("  只有 agent（Windows / 裸机壳）、homer（NAS / 容器壳）、warden（账本服务）")
        return 2

    head(f"初始化：{role}")

    if role == "warden":
        root = Path(getattr(args, "root", "") or DEFAULT_ROOT).expanduser()
        token = str(getattr(args, "token", "") or "").strip() or initcfg.new_token()
        root.mkdir(parents=True, exist_ok=True)
        token_path = root / "warden.token"
        token_path.write_text(token + "\n", encoding="utf-8")
        try:
            token_path.chmod(0o600)
        except OSError:
            pass
        kv("根目录", root)
        kv("token 文件", token_path)
        kv("token", token)
        info("下一步：")
        # 这里原来写的是 `python3 -m molt.cli serve ...`。两个问题：
        # Windows 上没有 `python3` 这个名字（实测 exit 9009），而且 `molt`
        # 命令既然已经装了就该用它 —— 别人照抄的那一行，必须在他自己的
        # 机器上真能跑。
        dim(f"  molt serve --root {root} --host 0.0.0.0 --port 6186 --token {token}")
        dim(
            f"  （没装命令行入口时等价写法：{sys.executable} -m molt.cli serve "
            f"--root {root} --port 6186 --token {token}）"
        )
        dim("  然后交给一个守护进程（NAS 上用 tools/molt-supervise.py，它只补不杀）")
        dim("  另外两台壳的配置里，warden_token 都填这个值")
        return 0

    shell = str(getattr(args, "shell", "") or "").strip()
    if not shell:
        bad("没有壳名，我不猜")
        dim("  --shell 是账本里别人会写下的那个字符串（比如 desktop-h2kgiao）")
        dim(f"  如果确认这台机器就叫这个名字，加 --shell {initcfg.hostname_shell_id()}")
        return 2

    data_dir = str(getattr(args, "data", "") or "").strip()
    if not data_dir:
        bad("没有 --data —— 落位目标是哪，这件事没有默认值")
        return 2

    warden_url = str(getattr(args, "warden_url", "") or "").strip()
    if not warden_url:
        bad("没有 --warden-url —— 账本在哪台机器上，这件事没有默认值")
        return 2

    warden_token = str(getattr(args, "token", "") or "").strip()
    if not warden_token:
        bad("没有 --token")
        dim("  账本那台机器上的 <root>/warden.token 就是它；没有就先去那边跑 molt init --role warden")
        return 2

    molt_pkg = str(getattr(args, "molt_pkg", "") or "").strip()
    if not molt_pkg:
        # 默认就是这份代码自己所在的包根 —— 人在本仓库里跑 init 时不用再写一遍。
        molt_pkg = str(Path(__file__).resolve().parent.parent)
    python = str(getattr(args, "python", "") or "").strip() or sys.executable

    default_out = "molt-agent.json" if role == "agent" else "molt-homer.json"
    out = Path(str(getattr(args, "out", "") or default_out)).expanduser()

    try:
        payload = initcfg.build(
            role=role,
            out=out,
            shell=shell,
            data_dir=data_dir,
            molt_pkg=molt_pkg,
            python=python,
            warden_url=warden_url,
            warden_token=warden_token,
            astrbot_python=str(getattr(args, "astrbot_python", "") or "").strip(),
            boot_script=str(getattr(args, "boot_script", "") or "").strip(),
            log_path=str(getattr(args, "log_path", "") or "").strip(),
            platform_switch=str(getattr(args, "platform_switch", "") or "").strip(),
            port=int(getattr(args, "port", DEFAULT_DASHBOARD_PORT) or DEFAULT_DASHBOARD_PORT),
            container=str(getattr(args, "container", "") or "astrbot").strip(),
            dashboard_host=str(getattr(args, "dashboard_host", "") or "").strip(),
            dashboard_port=int(getattr(args, "dashboard_port", 0) or 0),
        )
    except NeedsHuman as e:
        bad(str(e))
        return 2

    # 先写进旁边一个临时文件，用**守护进程自己的加载器**读一遍。
    # 生成器自己认为对不算数 —— 这是 v0.4 那条「看起来像成功的中间态」的同一课。
    probe = out.with_name(out.name + ".validate")
    problems: List[str] = []
    try:
        initcfg.write_json_atomic(probe, payload, force=True)
        problems = initcfg.validate_config(probe, kind=role, molt_pkg=molt_pkg)
    except (NeedsHuman, Fatal) as e:
        bad(f"验不过：{e}")
        return 2
    finally:
        if probe.exists():
            try:
                probe.unlink()
            except OSError:
                pass

    if problems:
        bad("这份配置连它自己的加载器都过不了，不写盘：")
        for p in problems:
            dim(f"  - {p}")
        return 2

    try:
        initcfg.write_json_atomic(out, payload, force=bool(getattr(args, "force", False)))
    except NeedsHuman as e:
        bad(str(e))
        return 2

    ok(f"已写：{out}")
    kv("壳名", shell)
    kv("data 目录", data_dir)
    kv("账本", warden_url)
    kv("python", python)
    kv("execute", "false（先只看不动；确认壳名后再改成 true）")

    head("下一步")
    for line in initcfg.next_steps(role, out=out, python=python):
        dim(f"  {line}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    head("环境")
    ident = probe_identity()
    kv("molt 版本", __version__)
    kv("协议版本", PROTOCOL_VERSION)
    kv("shell_id", ident.shell_id)
    kv("platform", ident.platform)
    kv("python", ident.python_version)

    head("Warden")
    root = Path(args.root).expanduser() if args.root else DEFAULT_ROOT
    w = Warden(root=root)
    kv("根目录", root)
    kv("存在", "是" if root.exists() else "否（首次运行会创建）")
    try:
        s = w.status()
        kv("state.json", "可读")
        kv("当前 epoch", s["current_epoch"])
        kv("租约", s["lease"]["state"])
    except Fatal as e:
        bad(f"state.json 有问题：{e}")
    except Exception as e:  # noqa: BLE001 - 诊断命令的契约是"说清楚"，不是"崩给你看"
        # 这一条吃到的第一例：默认根目录指向一个别人机器上不存在的路径，
        # 于是 doctor 在最该帮上忙的时候抛异常退出（CI 陌生人演练，2026-11-15）。
        bad(f"读不了那边的账本：{e}")
        dim("  这通常说明根目录指向了别的机器上的路径。要么建好它，要么用 --root 指个本地目录。")

    head("SQLite")
    import sqlite3

    kv("版本", sqlite3.sqlite_version)
    kv("backup()", "可用" if hasattr(sqlite3.Connection, "backup") else "不可用")

    head("依赖")
    try:
        import pytest  # noqa: F401

        ok("pytest（可以跑测试）")
    except ImportError:
        dim(f"  {C.S_BULLET} pytest 未装（只影响跑测试）")

    config = str(getattr(args, "config", "") or "").strip()
    if config:
        return _doctor_config(
            Path(config).expanduser(),
            role=str(getattr(args, "role", "") or "").strip(),
            molt_pkg=str(getattr(args, "molt_pkg", "") or "").strip(),
        )

    return 0


def _doctor_config(path: Path, *, role: str, molt_pkg: str) -> int:
    """检查一份**真实的**壳配置 —— 这是"陌生人装不起来"最常卡住的地方。

    三件事分开报，因为它们的修法完全不同：

      1. 这份配置自己读得过吗（用守护进程自己的加载器）
      2. 它写的账本地址能连上吗、token 对吗（401 与连不上是两回事）
      3. 它指向的目录真的存在吗
    """
    from . import initcfg

    head("配置")
    kv("文件", path)
    if not path.is_file():
        bad("不存在。先跑：molt init --role agent --out <这个路径> …")
        return 2

    if not role:
        name = path.name.lower()
        role = "homer" if "homer" in name else "agent"
        dim(f"  {C.S_BULLET} 没给 --role，按文件名猜成 {role}（猜错就用 --role 指定）")
    kv("角色", role)

    pkg = molt_pkg or str(Path(__file__).resolve().parent.parent)
    problems = initcfg.validate_config(path, kind=role, molt_pkg=pkg)
    if problems:
        bad("它自己的加载器读不过：")
        for p in problems:
            dim(f"  - {p}")
        return 2
    ok("守护进程的加载器读得过")

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        bad(f"读不出 JSON：{e}")
        return 2
    if not isinstance(raw, dict):
        bad("顶层不是一个 JSON 对象")
        return 2

    data_dir = str(raw.get("data_dir") or "").strip()
    if data_dir:
        exists = Path(data_dir).exists()
        (ok if exists else warn)(f"data 目录{'存在' if exists else '不存在'}：{data_dir}")
        if not exists:
            dim("  AstrBot 第一次启动才会创建它；落位之前它必须存在（或至少父目录存在）")

    url = str(raw.get("warden_url") or "").strip()
    token = str(raw.get("warden_token") or "").strip()
    if not url:
        bad("配置里没有 warden_url")
        return 2

    head("账本")
    kv("地址", url)
    reachable = _probe_warden(url, token)
    if reachable is None:
        bad("连不上（服务没起来 / 端口不对 / 被防火墙挡了）")
        dim(f"  自己验一下：curl -sS -o /dev/null -w '%{{http_code}}' {url}/status")
        return 2
    if reachable == 401:
        bad("连上了，但 token 不对（HTTP 401）")
        dim("  token 在账本那台机器的 <root>/warden.token 里；把它抄进配置的 warden_token")
        return 2
    if reachable != 200:
        bad(f"账本回了一个意料之外的 HTTP {reachable}")
        return 2
    ok("连得上，token 也对")

    head("下一步")
    dim("  这份配置可以用了。真正让它动手之前，先把里面的 execute 改成 true。")
    return 0


def _probe_warden(base: str, token: str, *, timeout: float = 5.0) -> Optional[int]:
    """问一次 `/status`，返回 HTTP 状态码；连不上返回 None。

    为什么单独写一个而不是用 `WardenClient`：这一条要能在**什么都还没装好**
    的机器上跑（doctor 的整个意义就是这个），所以只用标准库、且把 401 与
    "连不上"分开报 —— 这两种失败的修法完全不同。
    """
    req = urllib.request.Request(base.rstrip("/") + "/status", method="GET")
    if token:
        req.add_header("X-Molt-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200))
    except urllib.error.HTTPError as e:
        return int(e.code)
    except (urllib.error.URLError, OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    def _warden_opts(parser: argparse.ArgumentParser, *, parent: bool = False) -> None:
        """每个 `policy` 子命令都要能指向远端账本。

        `--root` 写本地、`--warden-url/--warden-token` 写远端 —— 只给前者，
        这条命令就只能在账本那台机器上敲，而那台通常是 NAS 的容器外。
        `parent=True` 时只补 `--root`：父解析器从 `parents=[common]` 已经带了
        那两个远端开关，再加一次会 `conflicting option string`。
        """
        parser.add_argument("--root", help="Warden 根目录（本地模式）")
        if not parent:
            parser.add_argument("--warden-url", help="Warden 地址（远端模式）")
            parser.add_argument("--warden-token", help="Warden token")

    p = argparse.ArgumentParser(
        prog="molt",
        description="让一个 AI 存在在多台机器之间迁徙，而不失去连续性。",
        epilog="核心公理：任意时刻只能有一个存在在线。",
    )
    p.add_argument("--version", action="version", version=f"molt {__version__}")
    p.add_argument("--no-color", action="store_true", help="关闭彩色输出")

    # --no-color 也要能被放在子命令后面（molt doctor --no-color），
    # 因为人就是这么写的。argparse 默认只认全局位置，所以用 parent 再挂一份。
    #
    # --warden-url / --warden-token 同样挂在这里：**每个**需要跟 Warden
    # 说话的命令都要能指定远端，否则只有一半命令支持远程是没用的。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS)
    common.add_argument(
        "--warden-url",
        default=argparse.SUPPRESS,
        help="远端 Warden 地址，如 http://192.168.1.10:6186（给了就走 HTTP）",
    )
    common.add_argument(
        "--warden-token",
        default=argparse.SUPPRESS,
        help="远端 Warden 的 token",
    )
    common.add_argument(
        "--staging",
        default=argparse.SUPPRESS,
        help="远端模式下快照落在本地哪个目录（默认系统临时目录下的 molt-staging）",
    )

    sub = p.add_subparsers(dest="command", metavar="<命令>")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, parents=[common])

    # shell
    s = add("shell", "自述这台壳是谁、能做什么")
    s.add_argument("--json", action="store_true", help="输出 JSON")
    s.add_argument("--astrbot-src", help="AstrBot 源码目录（用于探测版本）")
    s.add_argument("--data", help="data 目录（覆盖 ASTRBOT_ROOT 推断）")
    s.add_argument("--check-wol", help="tools/check-wol.ps1 的路径")
    s.set_defaults(func=cmd_shell)

    # status
    s = add("status", "现在是谁在线、搬过几次")
    s.add_argument("--json", action="store_true")
    s.add_argument("--root", help="Warden 根目录")
    s.set_defaults(func=cmd_status)

    # checkout
    s = add("checkout", "从源端 data/ 热备出一份快照")
    s.add_argument("--data", required=True, help="源端 data 目录")
    s.add_argument("--shell", required=True, help="自己的 shell_id")
    s.add_argument("--root", help="Warden 根目录")
    s.add_argument("--astrbot-src", help="AstrBot 源码目录")
    s.add_argument("--target", help="打算交给哪个壳（可留空，反正还不知道它开机没有）")
    s.add_argument(
        "--with-plugin-data",
        action="append",
        metavar="插件名",
        help="除了默认的记忆目录，再带走这个插件的 plugin_data（可重复）",
    )
    s.set_defaults(func=cmd_checkout)

    # verify
    s = add("verify", "验证一个 generation 目录")
    s.add_argument("--dir", required=True, help="generation 目录")
    s.set_defaults(func=cmd_verify)

    # push
    s = add("push", "把快照交给新壳（唯一改变灵魂归属的操作）")
    s.add_argument("--dir", required=True, help="generation 目录")
    s.add_argument("--shell", required=True, help="目标 shell_id")
    s.add_argument("--epoch", required=True, type=int, help="自己的 epoch")
    s.add_argument("--root", help="Warden 根目录")
    s.set_defaults(func=cmd_push)

    # release
    s = add("release", "交还租约")
    s.add_argument("--shell", required=True)
    s.add_argument("--epoch", required=True, type=int)
    s.add_argument("--root", help="Warden 根目录")
    s.set_defaults(func=cmd_release)

    # migrate
    s = add("migrate", "一条命令走完：热备 → 打包 → （上传）→ 登记")
    s.add_argument("--data", required=True, help="源端 data 目录")
    s.add_argument("--shell", required=True, help="自己的 shell_id")
    s.add_argument("--root", help="Warden 根目录")
    s.add_argument("--astrbot-src", help="AstrBot 源码目录（用于探测版本）")
    s.add_argument("--target", default="", help="打算交给哪个壳（写进 manifest）")
    s.add_argument(
        "--transport",
        choices=("local", "warden"),
        default=None,
        help="归档怎么走：local=留在本地由人传（默认同机）；"
             "warden=PUT 到 Warden 中转区，目标壳自己取（默认跨机）",
    )
    s.add_argument(
        "--no-archive",
        action="store_true",
        help="只做热备不打包（用于本地调试）",
    )
    s.add_argument(
        "--confirm-timeout",
        type=float,
        default=TARGET_CONFIRM_SECONDS,
        help=f"交出去之后等目标壳接走的时限（秒，默认 {TARGET_CONFIRM_SECONDS:.0f}）",
    )
    s.add_argument(
        "--no-wait-confirm",
        action="store_true",
        dest="no_wait_confirm",
        help="不等目标壳接管（脚本/测试用；真实使用不要加）",
    )
    s.add_argument(
        "--keep-lease",
        action="store_true",
        help="做完不自释放租约（还要再拍一份时用）",
    )
    s.add_argument(
        "--wake-mac",
        default=None,
        help="先发魔术封包叫醒目标网卡（跨机迁徙的第一步，也可单独用 molt wake）",
    )
    s.add_argument("--wake-host", default="", help="目标 IPv4（配合 --wake-mac 判断是否真的起来）")
    s.add_argument("--wake-prefix", type=int, default=24, help="子网掩码长度（默认 24）")
    s.add_argument("--wake-count", type=int, default=W.DEFAULT_COUNT, help=f"发几轮（默认 {W.DEFAULT_COUNT}）")
    s.add_argument("--wake-interval", type=float, default=W.DEFAULT_INTERVAL, help=f"轮次间隔秒（默认 {W.DEFAULT_INTERVAL}）")
    s.add_argument(
        "--wake-timeout",
        type=float,
        default=180.0,
        help="等目标起来的最长秒数（默认 180）",
    )
    s.add_argument(
        "--wake-port-check",
        type=int,
        default=0,
        help="等这个 TCP 端口能连上才算起来（不给就用 ICMP）",
    )
    s.add_argument(
        "--skip-target-check",
        action="store_true",
        help="不做「目标壳在不在」的闸门（默认会先确认心跳，叫不醒就不搬）",
    )
    s.add_argument(
        "--no-wait-wake",
        action="store_true",
        help="发完封包就继续，不确认目标是否真的开机了（不推荐）",
    )
    s.add_argument(
        "--control-url",
        default="",
        help="源壳的控制面地址（如 http://127.0.0.1:6185）。给了就在热备前先让它停止对外说话",
    )
    s.add_argument(
        "--control-config",
        default="",
        help="源壳的 cmd_config.json（用它自己的 jwt_secret 现签一个短命 token）",
    )
    s.add_argument(
        "--control-token",
        default="",
        help="直接给一个 dashboard JWT（不给 --control-config 时才用）",
    )
    s.add_argument(
        "--with-plugin-data",
        action="append",
        metavar="插件名",
        help="除了默认的记忆目录，再带走这个插件的 plugin_data（可重复）",
    )
    s.set_defaults(func=cmd_migrate)

    # land
    s = add("land", "在目标壳上把归档落进 data 目录（本地文件或从 Warden 取）")
    s.add_argument("--archive", help="本地 .tar.gz（与 --generation 二选一）")
    s.add_argument(
        "--root",
        help="Warden 根目录（本地模式；用 --generation 且 Warden 就在本机时给它）",
    )
    s.add_argument(
        "--generation",
        type=int,
        help="从 Warden 中转区取第几代（与 --archive 二选一，需要 --warden-url）",
    )
    s.add_argument(
        "--data",
        required=True,
        help="目标 data 目录（不存在就新建——全新壳上它本来就不存在，但上层目录必须在）",
    )
    s.add_argument("--sha256", default="", help="归档的 sha256（强烈建议给）")
    s.add_argument("--to", help="解包到哪个目录（默认用 mkdtemp 建一个全新的）")
    s.add_argument("--shell", default="", help="这台壳的 shell_id（写进覆写日志）")
    s.add_argument(
        "--dashboard-host",
        default="",
        help="目标壳自己的 dashboard 监听地址（Windows 用 127.0.0.1、NAS 容器用 0.0.0.0）。"
             "不给就保持快照里原样 —— 但那份配置是从**另一台机器**搬来的。",
    )
    s.add_argument(
        "--dashboard-port",
        type=int,
        default=None,
        help=f"目标壳自己的 dashboard 端口（Windows 默认 {DEFAULT_DASHBOARD_PORT}）。"
             "不给就保持快照里原样。",
    )
    s.add_argument(
        "--yes",
        action="store_true",
        help="真的落位（不给只做检查并说明会移动哪些文件）",
    )
    s.add_argument(
        "--force",
        action="store_true",
        help="跳过「目标 data 目录看起来还活着」的检查（确认那个 AstrBot 真的停了再用）",
    )
    s.add_argument(
        "--no-plugin-configs",
        dest="no_plugin_configs",
        action="store_true",
        help="不把搬来的插件配置写回这台壳（默认会写，只写这边也装了的插件）",
    )
    s.add_argument(
        "--keep-provider-source",
        dest="keep_provider_source",
        action="append",
        metavar="来源 id",
        help="这台机器上确实跑着这个 provider 服务时，别在覆写层里把它删掉（可重复）。"
             "默认会删掉指向回环地址的 workbuddy —— 除非你在这里点名。",
    )
    s.set_defaults(func=cmd_land)

    # backup
    s = add("backup", "把当前状态热备一份推给 Warden（不占代、不是交接）")
    s.add_argument("--data", required=True, help="源端 data 目录")
    s.add_argument("--shell", default="", help="这台壳的名字（默认用小写主机名）")
    s.add_argument("--astrbot-src", default="", help="AstrBot 源码目录（用来读出它的版本）")
    s.add_argument("--dry-run", action="store_true", help="只打包不上传")
    s.add_argument("--root", help="Warden 根目录（本地模式）")
    s.add_argument(
        "--with-plugin-data",
        action="append",
        metavar="插件名",
        help="除了默认的记忆目录，再带走这个插件的 plugin_data（可重复）",
    )
    s.set_defaults(func=cmd_backup)

    # backups
    s = add("backups", "列出 Warden 上现有的中途备份")
    s.add_argument("--root", help="Warden 根目录（本地模式）")
    s.set_defaults(func=cmd_backups)

    # recover
    s = add("recover", "从一份中途备份恢复（护栏与 land 完全相同）")
    s.add_argument("--backup", required=True, help="备份名（molt backups 里看到的那个）")
    s.add_argument("--data", required=True, help="目标 data 目录")
    s.add_argument("--sha256", default="", help="（忽略：指纹从下载结果里取）")
    s.add_argument("--to", help="解包到哪个目录（默认用 mkdtemp 建一个全新的）")
    s.add_argument("--shell", default="", help="这台壳的 shell_id（写进覆写日志）")
    s.add_argument("--dashboard-host", default="", help="目标壳的 dashboard 监听地址")
    s.add_argument("--dashboard-port", type=int, default=None, help="目标壳的 dashboard 端口")
    s.add_argument("--yes", action="store_true", help="真的落位（不给只做检查）")
    s.add_argument("--force", action="store_true", help="跳过「目标 data 目录还活着」的检查")
    s.add_argument("--root", help="Warden 根目录（本地模式）")
    s.set_defaults(func=cmd_recover)

    # publish-plugins
    s = add(
        "publish-plugins",
        "把这台机器上「插件的配置」打包推给 Warden（只装配置，不装代码）",
    )
    s.add_argument("--data", required=True, help="AstrBot 的 data 目录")
    s.add_argument("--shell", default="", help="这台壳的名字（默认用小写主机名）")
    s.add_argument("--root", help="Warden 根目录（本地模式）")
    s.set_defaults(func=cmd_publish_plugins)

    # sync-plugins
    s = add(
        "sync-plugins",
        "把另一台壳的插件配置同步过来（只同步两边都装了的插件；默认空跑）",
    )
    s.add_argument("--data", required=True, help="AstrBot 的 data 目录")
    s.add_argument(
        "--shell",
        default="",
        help="这台壳的名字（只用来判断「哪一份不是自己的」，默认用小写主机名）",
    )
    s.add_argument(
        "--from",
        dest="source",
        default="auto",
        help="从哪个壳同步（默认 auto：取账本里最近一份不是自己的包）",
    )
    s.add_argument("--apply", action="store_true", help="真的写盘（不给只报告）")
    s.add_argument(
        "--force",
        action="store_true",
        help="连「结构看起来不一样」的也写（默认跳过，因为那通常意味着两边插件版本不同）",
    )
    s.add_argument("--root", help="Warden 根目录（本地模式）")
    s.set_defaults(func=cmd_sync_plugins)

    # init
    s = add("init", "生成一份能被它自己的加载器读通的配置")
    s.add_argument(
        "--role",
        required=True,
        choices=["agent", "homer", "warden"],
        help="agent = Windows/裸机壳；homer = NAS/容器壳；warden = 账本服务",
    )
    s.add_argument("--out", help="配置文件写到哪（默认当前目录的 molt-agent.json / molt-homer.json）")
    s.add_argument("--shell", help="这台壳在账本里的名字（不猜，必须给）")
    s.add_argument("--data", help="AstrBot 的 data 目录")
    s.add_argument("--root", help="warden 角色：账本根目录")
    s.add_argument("--token", help="warden 要用的 token（warden 角色不给就自动生成一个）")
    s.add_argument("--molt-pkg", dest="molt_pkg", help="源码根目录（默认就是这份代码自己）")
    s.add_argument("--python", help="守护进程跑 land 用的 python")
    s.add_argument("--astrbot-python", dest="astrbot_python", help="跑 AstrBot 的 python（venv）")
    s.add_argument("--boot-script", dest="boot_script", help="tools/boot-keepalive.py 的路径")
    s.add_argument("--log-path", dest="log_path", help="AstrBot 的日志文件")
    s.add_argument("--platform-switch", dest="platform_switch", help="tools/platform-switch.py 的路径")
    s.add_argument("--port", type=int, help="AstrBot dashboard 端口（默认 6199）")
    s.add_argument("--container", help="homer 角色：容器名（默认 astrbot）")
    s.add_argument("--dashboard-host", dest="dashboard_host", help="落位后 dashboard 监听地址")
    s.add_argument("--dashboard-port", dest="dashboard_port", type=int, help="落位后 dashboard 端口")
    s.add_argument("--force", action="store_true", help="覆盖已存在的配置（旧文件备份成 *.bak）")
    s.set_defaults(func=cmd_init)

    # doctor
    s = add("doctor", "体检：把能查的都查一遍")
    s.add_argument("--root", help="Warden 根目录")
    s.add_argument("--config", help="检查一份真实的壳配置（守护进程 json）")
    s.add_argument(
        "--role", choices=["agent", "homer"], help="配合 --config：这份配置是给谁的"
    )
    s.add_argument("--molt-pkg", dest="molt_pkg", help="源码根目录（默认就是这份代码自己）")
    s.set_defaults(func=cmd_doctor)

    # wake
    s = add("wake", "发魔术封包把目标壳叫醒（可选等它真的起来）")
    s.add_argument("--mac", required=True, help="目标网卡 MAC（四种写法都认）")
    s.add_argument("--host", default="", help="目标 IPv4（给得越准，单播路径就能用上）")
    s.add_argument(
        "--prefix",
        type=int,
        default=24,
        help="子网掩码长度，用来算定向广播（默认 24）",
    )
    s.add_argument("--port", type=int, default=W.DEFAULT_PORT, help=f"UDP 端口（默认 {W.DEFAULT_PORT}）")
    s.add_argument("--count", type=int, default=W.DEFAULT_COUNT, help=f"发几轮（默认 {W.DEFAULT_COUNT}）")
    s.add_argument(
        "--interval",
        type=float,
        default=W.DEFAULT_INTERVAL,
        help=f"轮次间隔秒（默认 {W.DEFAULT_INTERVAL}）",
    )
    s.add_argument("--wait", action="store_true", help="发完等它起来（ICMP 判据）")
    s.add_argument(
        "--wait-port",
        type=int,
        default=0,
        help="等这个 TCP 端口能连上才算起来（比 --wait 严格，也更接近「能干活」）",
    )
    s.add_argument("--wait-timeout", type=float, default=180.0, help="最长等多少秒（默认 180）")
    s.add_argument("--wait-interval", type=float, default=2.0, help="探测间隔秒（默认 2）")
    s.add_argument("--quiet", action="store_true", help="不逐轮打印")
    s.set_defaults(func=cmd_wake)

    # prune
    s = add("prune", "清理 Warden 中转区里账本不认的归档（默认只看不删）")
    s.add_argument("--root", help="Warden 根目录（本地模式）")
    s.add_argument(
        "--keep-recent",
        type=int,
        default=2,
        help="账本不认的归档里，保留最新几份做缓冲（默认 2）",
    )
    s.add_argument(
        "--yes",
        action="store_true",
        help="真的删。不给这个开关只报告不删",
    )
    s.set_defaults(func=cmd_prune)

    # shells
    s = add("shells", "列出账本里登记的壳属性（每台机器自称是什么）")
    s.add_argument("--root", help="Warden 根目录（本地模式）")
    s.add_argument("--json", action="store_true", help="打成 JSON（给脚本用）")
    s.set_defaults(func=cmd_shells)

    # declare
    s = add("machine", "看 / 叫醒 / 关掉账本里的任意一台机器（鱼不用搬过去）")
    _warden_opts(s, parent=True)
    s.add_argument("--json", action="store_true", help="机器可读输出（暂只用于 list）")
    ms = s.add_subparsers(dest="machine_action", required=True)
    ml = ms.add_parser("list", help="列出所有机器：心跳、地址、能不能叫醒")
    _warden_opts(ml)
    ml.add_argument("--json", action="store_true")
    mo = ms.add_parser("on", help="发魔术封包叫醒一台机器")
    mo.add_argument("shell")
    _warden_opts(mo)
    mf = ms.add_parser("off", help="给一台机器投一张关机票（会过期）")
    mf.add_argument("shell")
    _warden_opts(mf)

    s.set_defaults(func=cmd_machine)

    s = add("declare", "登记「这台机器是什么」：role / gpu / 能不能被叫醒 / 每小时多少钱")
    _warden_opts(s, parent=True)
    s.add_argument("--shell", required=True, help="哪台壳（与账本里的 target_shell 一致）")
    s.add_argument("--role", choices=("nas", "desktop", "cloud", "unknown"), help="这台机器的角色")
    s.add_argument(
        "--gpu",
        help="显卡（自由文本，比如 4070TiS；给空串表示「确认过，没有显卡」）",
    )
    s.add_argument(
        "--wakeable",
        dest="wakeable",
        action="store_const",
        const=True,
        default=None,
        help="这台机器能被魔术封包叫醒（发 WOL 之前要知道这件事）",
    )
    s.add_argument(
        "--address",
        help="这台机器的局域网地址（比如 192.168.50.63）—— 看它的 DSH 通不通要用",
    )
    s.add_argument(
        "--no-wakeable",
        dest="wakeable",
        action="store_const",
        const=False,
        help="明确声明它叫不醒（老主板 / 网卡不支持 PCI-E 唤醒）；molt migrate 就不会白等",
    )
    s.add_argument(
        "--wake-mac",
        default=None,
        help="这台机器的网卡地址（存在账本里，别的机器搬家时用它发魔术封包）",
    )
    s.add_argument(
        "--cost-per-hour",
        dest="cost_per_hour",
        type=float,
        help="醒着一小时多少钱（云主机/电费；用来排序「哪台更该醒着」）",
    )
    s.add_argument("--dsh-relay", help="这台机器上的代转小口端口（有 DSH 的机器才需要，比如 6191）")
    s.add_argument("--label", help="给人看的名字（不填就用壳名）")
    s.add_argument("--by", help="署名（写进账本的日志，方便事后追）")
    s.set_defaults(func=cmd_declare)

    s = add("policy", "自动搬家策略（M2）：看、设、撤")

    _warden_opts(s, parent=True)
    psub = s.add_subparsers(dest="policy_action", required=True)
    p_show = psub.add_parser("show", help="看现在的策略与最近几次尝试")
    _warden_opts(p_show)
    p_show.set_defaults(func=cmd_policy)
    p_status = psub.add_parser("status", help="它到点会不会真的自己动：逐条预检")
    _warden_opts(p_status)
    p_status.add_argument("--shell", help="以哪台壳的身份检查（默认取本机名）")
    p_status.set_defaults(func=cmd_policy)
    p_off = psub.add_parser("off", help="撤销策略（立刻生效）")
    _warden_opts(p_off)
    p_off.set_defaults(func=cmd_policy)
    p_set = psub.add_parser("set", help="设一条定时自动搬家策略")
    _warden_opts(p_set)
    p_set.add_argument("--id", required=True, help="给它起个名字，比如 night-home")
    p_set.add_argument("--source", required=True, help="在哪台壳上执行（通常是鱼现在所在的那台）")
    p_set.add_argument("--target", required=True, help="自动搬去哪台壳")
    p_set.add_argument("--at", required=True, help='时刻，比如 "23:30" 或 {"at": "23:30", "window_minutes": 45}')
    p_set.add_argument("--window-minutes", type=int, default=0, help="窗口多长（默认用定时规则里的）")
    p_set.add_argument("--daily-limit", type=int, default=0, help="每天最多自动搬几次（默认 2）")
    p_set.add_argument("--expires-at", type=float, default=0.0, help="到什么时候自动失效（epoch 秒，0 = 不过期）")
    p_set.add_argument("--by", default="", help="谁设的（留个名字）")
    p_set.add_argument(
        "--power-off",
        action="store_true",
        help="搬完之后关掉旧壳（走账本里那张关机票；没接走或播报没送到就不关）",
    )
    p_set.add_argument(
        "--power-action",
        default="shutdown",
        choices=("shutdown", "sleep", "keep"),
        help="关旧壳时用什么动作（默认 shutdown）",
    )
    p_set.add_argument(
        "--release-when-away",
        action="store_true",
        help="鱼已经在目标壳上时，也把这台源壳关掉（早上 9 点只关 Windows 用这条）",
    )
    p_set.add_argument(
        "--arrival-timeout",
        type=float,
        default=120.0,
        help="等新壳确认落地播报送达最多等多久（秒，默认 120）",
    )
    p_set.set_defaults(func=cmd_policy)

    s = add("dsh", "跟另一台机器上的 DSH 说话（由账本代转，只放三个方法）")
    _warden_opts(s, parent=True)
    dsub = s.add_subparsers(dest="dsh_action", required=True)
    d_sessions = dsub.add_parser("sessions", help="那台机器上有哪些会话")
    _warden_opts(d_sessions)
    d_sessions.add_argument("--shell", required=True, help="哪台机器（molt machine list 里看）")
    d_sessions.add_argument("--limit", type=int, default=10, help="最多列几个（默认 10）")
    d_sessions.set_defaults(func=cmd_dsh)
    d_history = dsub.add_parser("history", help="读某个会话的最近消息")
    _warden_opts(d_history)
    d_history.add_argument("--shell", required=True)
    d_history.add_argument("--session", required=True, help="会话 id（molt dsh sessions 里看）")
    d_history.add_argument("--limit", type=int, default=20, help="最多几条消息（默认 20）")
    d_history.add_argument("--json", action="store_true", help="直接打原始 JSON")
    d_history.set_defaults(func=cmd_dsh)
    d_send = dsub.add_parser("send", help="往那个会话发一句话（会在那台机器上消耗模型）")
    _warden_opts(d_send)
    d_send.add_argument("--shell", required=True)
    d_send.add_argument("--session", required=True)
    d_send.add_argument("--steer", action="store_true", help="插进去改方向（默认排队，不打断）")
    d_send.add_argument("--dry-run", action="store_true", help="只打印要发什么，不发")
    d_send.add_argument("text", nargs="+", help="要说的话")
    d_send.set_defaults(func=cmd_dsh)
    d_call = dsub.add_parser("call", help="通用逃生口（方法仍在白名单内）")
    _warden_opts(d_call)
    d_call.add_argument("--shell", required=True)
    d_call.add_argument("--method", required=True, help="session/list、session/events、session/prompt")
    d_call.add_argument("--args", default="{}", help="JSON 参数（默认 {}）")
    d_call.set_defaults(func=cmd_dsh)

    # timeline
    s = add(
        "timeline",
        "把「它搬过几次家」渲染成一页 HTML（零外部资源，断网也能看）",
    )
    s.add_argument("--root", help="Warden 根目录（本地模式）")
    s.add_argument(
        "--out",
        default="molt-timeline.html",
        help="写到哪个文件（默认 ./molt-timeline.html）",
    )
    s.add_argument(
        "--json",
        action="store_true",
        help="不打摘要，直接把时间轴的原始数据打成 JSON（给脚本用）",
    )
    s.add_argument(
        "--demo",
        action="store_true",
        help="用内置示例数据渲染，不连账本（第一次装完想看看它长什么样）",
    )
    s.set_defaults(func=cmd_timeline)

    # observe
    s = add(
        "observe",
        "让这只壳自己看一眼账本，把「我搬到了这里」记成它自己的观察",
    )
    s.add_argument("--config", required=True, help="这只壳的 cmd_config.json（拿 dashboard 凭据）")
    s.add_argument("--base", default="http://127.0.0.1:6199", help="这只壳的 dashboard 地址")
    s.add_argument(
        "--wait",
        type=float,
        default=90.0,
        help="等 WebUI 能应答的最长秒数（默认 90；壳刚起来时 WebUI 比进程慢）",
    )
    s.add_argument("--timeout", type=float, default=30.0, help="单次 HTTP 超时秒数")
    s.set_defaults(func=cmd_observe)

    # serve
    s = add("serve", "把 Warden 跑成一个 HTTP 服务（给远端壳用）")
    s.add_argument("--root", help="Warden 根目录")
    s.add_argument("--host", default="0.0.0.0", help="绑定地址（默认 0.0.0.0）")
    s.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口（默认 {DEFAULT_PORT}）")
    s.add_argument("--token", help="认证 token（不给就随机生成并打印一次）")
    s.set_defaults(func=cmd_serve)

    return p


#: 两个常驻守护进程的名字。它们**不是**普通子命令：一个是长期运行的循环、
#: 有自己的 argparse 和退出码语义，所以不走 `build_parser()`，在入口处直接
#: 转交。这样 `molt agent --config x --once` 与
#: `python -m molt.daemon.agent --config x --once` 逐字等价。
#:
#: 为什么不挂成 argparse 子命令：试过了，`nargs=REMAINDER` + `parents=[common]`
#: 的子解析器**不接管未知开关** —— `molt agent --config x` 会被顶层 parser
#: 判成 `unrecognized arguments: --config`。与其和 argparse 较劲，不如在入口
#: 认这两个词：它们本来就是进程入口，不是子命令。
# nightwatch 也在这里：它不是"操作账本的命令"，而是和 agent/homer 一样的常驻进程。
_DAEMON_COMMANDS = ("agent", "homer", "nightwatch")


def main(argv: Optional[List[str]] = None) -> int:
    # 必须在任何输出之前——见 _setup_console 的文档。
    # 漏了这一步的后果实测过：中文 Windows 上第一行输出就 traceback。
    _setup_console()

    raw = list(sys.argv[1:] if argv is None else argv)

    # 全局开关可以排在守护进程名字前面：`molt --no-color agent --config x`。
    # 不剥掉它们，这一行会掉进 argparse 并报 `invalid choice: 'agent'` ——
    # 一个"只差一个参数位置就全盘不认"的失败，而写它的人不会觉得自己写错了。
    head = list(raw)
    while head and head[0] in ("--no-color", "--color"):
        head.pop(0)

    if head and head[0] in _DAEMON_COMMANDS:
        # 装成 wheel 之后没有 tools/，所以这两条命令必须由包自己提供 ——
        # 否则 `pip install` 的人拿到一份配置，却没有一条能跑它的命令。
        import importlib

        module = importlib.import_module(f"molt.daemon.{head[0]}")
        try:
            return int(module.main(head[1:]))
        except MoltError as e:
            bad(f"{type(e).__name__}: {e}")
            return 1
        except KeyboardInterrupt:
            print()
            warn("被中断")
            return 130

    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "no_color", False) or _no_color():
        C.off()

    if not getattr(args, "command", None):
        parser.print_help()
        print()
        dim("  第一次用？先跑：molt doctor && molt shell")
        dim("  装好之后：molt init --role warden|agent|homer；常驻用 molt agent --config <json>")
        return 0

    try:
        return int(args.func(args))
    except MoltError as e:
        bad(f"{type(e).__name__}: {e}")
        return 1
    except KeyboardInterrupt:
        print()
        warn("被中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
