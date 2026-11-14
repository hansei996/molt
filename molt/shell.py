"""Shell：可抛弃的那一半。

## Soul 与 Shell 的分界线

  Soul    = 记忆、人格、对话历史。**不可再生**。
  Shell   = Python 环境、二进制、驱动、桌面会话。**可重装**。

本项目最根本的工程判断是：

    壳的 Python 环境可再生，魂的状态不可再生。

所以迁徙时只搬 Soul；Shell 在目标机上就地重建（requirements.txt +
manifest 里记的 runtime_pip）。这条线画错的后果是灾难性的：
把 `data/site-packages/` 一起搬过去，Windows 会拿到 122 个
`linux-x86_64` 的 `.so`，然后以各种看不懂的方式炸掉。

## 为什么 Shell 要能"自述"

因为 Warden 在 NAS 上，它看不见 Windows 的显卡、看不见那边有没有
交互式桌面、看不见网卡有没有 armed。这些信息只能由壳自己上报。

而"上报"有个陷阱：**壳会撒谎**——不是恶意，而是陈旧缓存、部分失败、
以及最普通的"配置写着启用但其实没跑"。所以 Shell 的自述全部是
**当场探测**，一个字段都不抄配置。

## shell_id 的稳定性

`shell_id` 是 fencing 的一部分（写快照时必须带对），所以它必须：
  - 跨进程重启稳定（不能用 PID、不能用随机 UUID）
  - 跨机不同（NAS 与 Windows 不能撞）
  - 可人工阅读（出问题时 `molt status` 要能一眼看出"这是谁"）

做法：`<hostname 规范化>-<机器指纹前 8 位>`。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import CapabilityMissing, NeedsHuman
from .version import REQUIRED_CAPABILITIES, __version__


# 这些工具在 NAS 上用来发魔术封包 / 做健康检查。
# 注意：NAS 是 BusyBox，`sort` 没有 -h，`grep` 没有 --include。
WOL_TOOLS_POSIX = ("python3",)
WOL_TOOLS_WINDOWS = ("powershell",)

# Warden 需要的三项能力，缺一不可。
# 对应 version.REQUIRED_CAPABILITIES，但这里给出人类可读的解释。
CAPABILITY_DOCS = {
    "soul.read": "能对 AstrBot 的库做一致性热备（sqlite3.backup）",
    "soul.write": "能接收并验证一份快照（sha256 + quick_check + 表数）",
    "shell.identity": "能稳定自述身份（shell_id 不随重启变化）",
}


def _norm_hostname(raw: str) -> str:
    """把主机名规范化成 shell_id 的前缀。

    真实主机名里可能有下划线、空格、大写、以及超长 FQDN。
    shell_id 会被写进文件名和日志，所以只保留 [a-z0-9-]。
    """
    out = []
    for ch in raw.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in "-_.":
            out.append("-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return (slug or "unknown")[:40]


def machine_fingerprint() -> str:
    """给这台机器算一个尽量稳定的短指纹。

    Windows 上用 MachineGuid（重装系统才会变），POSIX 上用
    /etc/machine-id。都拿不到时退回 MAC 地址。

    **刻意不用 IP**：DHCP 租约会变，而 shell_id 必须稳定。
    """
    candidates: List[str] = []

    if sys.platform == "win32":
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            )
            with key:
                val, _ = winreg.QueryValueEx(key, "MachineGuid")
                candidates.append(str(val))
        except Exception:
            pass
    else:
        for p in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            try:
                if p.exists():
                    candidates.append(p.read_text(encoding="utf-8").strip())
                    break
            except Exception:
                continue

    try:
        import uuid

        candidates.append(str(uuid.getnode()))
    except Exception:
        pass

    if not candidates:
        return "00000000"

    import hashlib

    digest = hashlib.sha256("|".join(candidates).encode("utf-8")).hexdigest()
    return digest[:8]


@dataclass
class ShellIdentity:
    """壳的自述。全部字段都是当场探测的结果。"""

    shell_id: str
    hostname: str
    platform: str                     # "linux" | "windows" | "darwin"
    python_version: str
    python_executable: str
    is_container: bool
    astrbot_root: Optional[str]
    data_path: Optional[str]
    astrbot_version: Optional[str]
    molt_version: str = __version__
    probed_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "shell_id": self.shell_id,
            "hostname": self.hostname,
            "platform": self.platform,
            "python_version": self.python_version,
            "python_executable": self.python_executable,
            "is_container": self.is_container,
            "astrbot_root": self.astrbot_root,
            "data_path": self.data_path,
            "astrbot_version": self.astrbot_version,
            "molt_version": self.molt_version,
            "probed_at": self.probed_at,
            "probed_at_readable": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self.probed_at)
            ),
        }


@dataclass
class Capabilities:
    """这台壳**现在**能做什么。

    每一项都是当场探测，不是读配置。理由见模块文档。
    """

    # 三项必需能力
    soul_read: bool = False
    soul_write: bool = False
    shell_identity: bool = True

    # 可选能力（有则更好，没有就降级）
    can_send_magic_packet: bool = False     # 能在自己这边发 WOL
    can_be_woken: Optional[bool] = None     # 自己能被 WOL 叫醒（含 arm 状态）
    wake_armed: Optional[bool] = None       # 网卡此刻是否在 OS 唤醒白名单
    has_interactive_desktop: bool = False   # 有真人登录的桌面会话
    has_gpu: bool = False
    astrbot_installed: bool = False

    notes: List[str] = field(default_factory=list)

    def missing_required(self) -> List[str]:
        out = []
        if not self.soul_read:
            out.append("soul.read")
        if not self.soul_write:
            out.append("soul.write")
        if not self.shell_identity:
            out.append("shell.identity")
        return out

    def assert_required(self) -> None:
        missing = self.missing_required()
        if missing:
            desc = "；".join(f"{m}（{CAPABILITY_DOCS.get(m, '')}）" for m in missing)
            raise CapabilityMissing(f"这台壳缺少必需能力：{desc}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "soul_read": self.soul_read,
            "soul_write": self.soul_write,
            "shell_identity": self.shell_identity,
            "required_ok": not self.missing_required(),
            "missing_required": self.missing_required(),
            "can_send_magic_packet": self.can_send_magic_packet,
            "can_be_woken": self.can_be_woken,
            "wake_armed": self.wake_armed,
            "has_interactive_desktop": self.has_interactive_desktop,
            "has_gpu": self.has_gpu,
            "astrbot_installed": self.astrbot_installed,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------
# 探测
# --------------------------------------------------------------------------


def detect_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def detect_container() -> bool:
    """是不是跑在容器里。

    为什么在意：ARCH §7.3 判断魔术封包**必须在宿主机发**，
    因为容器默认没有 NET_RAW，且 docker0 网桥出去的单播到不了关机后的机器。
    这个探测是为了在容器里误配时给出明确警告，而不是让人对着
    "发不出去"的现象猜。
    """
    if Path("/.dockerenv").exists():
        return True
    try:
        if Path("/proc/1/cgroup").exists():
            text = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore")
            if "docker" in text or "kubepods" in text or "containerd" in text:
                return True
    except Exception:
        pass
    return False


def resolve_astrbot_root() -> Optional[Path]:
    """与 AstrBot 自己的规则一致：`ASTRBOT_ROOT` 优先。

    这是整个方案的地基：换壳只需要改这一个环境变量，
    AstrBot 的全部状态就跟着走（`/AstrBot/astrbot/core/utils/astrbot_path.py`
    的 `get_astrbot_root()` 优先读 `os.environ["ASTRBOT_ROOT"]`）。
    本项目已在 Windows 上实测：源码目录零污染，状态全部落在 ASTRBOT_ROOT 下。
    """
    env = os.environ.get("ASTRBOT_ROOT")
    if env:
        p = Path(env)
        return p if p.exists() else p        # 即使还没建也返回，调用方会 mkdir
    return None


def read_astrbot_version(
    src_dir: Optional[Path] = None,
    data_dir: Optional[Path] = None,
) -> Optional[str]:
    """读 AstrBot 版本，读不到就 None。

    两条路径，按可靠性排序：

    1. **源码目录** `<src>/astrbot/__init__.py` 里的 `__version__`。
       直接读文本而不是 `import astrbot`——探测环节不该触发整个包的
       导入（那会拉起一堆依赖和副作用）。

    2. **data 目录**下的 `cmd_config.json` 里的 `config_version`。
       NAS 上的真实情况是：AstrBot 跑在容器里，宿主机只有 `data/`
       被 bind mount 出来，源码在镜像内部拿不到。这时候第 1 条路必然
       失败，但 `cmd_config.json` 一定在。

    注意 `cmd_config.json` **带 UTF-8 BOM**（实测前 3 字节 ef bb bf）。
    必须用 `utf-8-sig` 打开，否则 Linux 上的 `json.load` 直接报
    `JSONDecodeError: Unexpected UTF-8 BOM`。
    """
    if src_dir is not None:
        init = Path(src_dir) / "astrbot" / "__init__.py"
        if init.exists():
            try:
                for line in init.read_text(
                    encoding="utf-8", errors="ignore"
                ).splitlines():
                    line = line.strip()
                    if line.startswith("__version__"):
                        _, _, rhs = line.partition("=")
                        ver = rhs.strip().strip("'\"")
                        if ver:
                            return ver
            except Exception:
                pass

    if data_dir is not None:
        cfg = Path(data_dir) / "cmd_config.json"
        if cfg.exists():
            try:
                with cfg.open("r", encoding="utf-8-sig", errors="ignore") as fh:
                    payload = json.load(fh)
                ver = payload.get("config_version")
                if ver:
                    return f"config-{ver}"
            except Exception:
                pass

    return None


def probe_identity(
    astrbot_src: Optional[Path] = None,
    data_path: Optional[Path] = None,
) -> ShellIdentity:
    """自述这台壳是谁。

    `data_path` 是用来**反推** ASTRBOT_ROOT 的：NAS 上 AstrBot 跑在容器里，
    宿主机没有 `ASTRBOT_ROOT` 环境变量，但 `--data /mnt/dm-0/astrbot/data`
    明确告诉了我们 data 在哪，从它 `.parent` 就能得到根目录。

    这个反推很重要：不做的后果是 `molt shell --data ...` 明明拿到了数据
    却报"AstrBot 未探测到"，而且 `soul.write` 探测会用一个凭空造出的
    目录——那是最糟的失败模式，看起来什么都对，其实什么都没验。
    """
    hostname = socket.gethostname()
    plat = detect_platform()
    root = resolve_astrbot_root()

    if root is None and data_path is not None:
        candidate = Path(data_path).expanduser()
        if candidate.name == "data" and candidate.exists():
            root = candidate.parent

    if data_path is not None:
        data = Path(data_path).expanduser()
    else:
        data = (root / "data") if root else None

    return ShellIdentity(
        shell_id=f"{_norm_hostname(hostname)}-{machine_fingerprint()}",
        hostname=hostname,
        platform=plat,
        python_version=platform.python_version(),
        python_executable=sys.executable,
        is_container=detect_container(),
        astrbot_root=str(root) if root else None,
        data_path=str(data) if data else None,
        astrbot_version=read_astrbot_version(astrbot_src, data),
    )


def _run(cmd: List[str], timeout: float = 20.0) -> tuple[int, str]:
    """跑一个探测命令，永不抛异常（探测失败也是一种信息）。"""
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"命令不存在: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"超时 {timeout}s: {' '.join(cmd)}"
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def probe_capabilities(
    identity: ShellIdentity,
    *,
    data_path: Optional[Path] = None,
    check_wol_script: Optional[Path] = None,
) -> Capabilities:
    """探测这台壳现在能做什么。

    `check_wol_script` 指向 `tools/check-wol.ps1`。给了就在 Windows 上
    实跑它并解析 JSON —— 这是唯一可信的 WOL 判据（ARCH §7.4.3 的教训：
    `PnPCapabilities` 那个位不是判据，`wake_armed` 列表本身才是）。
    """
    caps = Capabilities()
    plat = identity.platform

    # --- soul.read：能不能读 SQLite ---
    try:
        import sqlite3

        caps.soul_read = hasattr(sqlite3.Connection, "backup")
        if not caps.soul_read:
            caps.notes.append("sqlite3 没有 Connection.backup（版本过旧）")
    except Exception as e:
        caps.soul_read = False
        caps.notes.append(f"import sqlite3 失败: {e}")

    # --- soul.write：能不能落盘 + 校验 ---
    target = data_path or (Path(identity.data_path) if identity.data_path else None)
    if target is not None:
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe_file = target / ".molt-write-probe"
            probe_file.write_text("ok", encoding="utf-8")
            probe_file.unlink()
            caps.soul_write = True
        except Exception as e:
            caps.soul_write = False
            caps.notes.append(f"data 目录不可写: {e}")
    else:
        caps.soul_write = False
        caps.notes.append("没有 ASTRBOT_ROOT / data_path，无从校验可写性")

    # --- shell.identity：shell_id 稳定 ---
    caps.shell_identity = bool(identity.shell_id) and identity.shell_id != "unknown"
    if not caps.shell_identity:
        caps.notes.append("shell_id 无法稳定生成")

    # --- 发魔术封包的能力 ---
    if plat == "linux":
        caps.can_send_magic_packet = shutil.which("python3") is not None
    elif plat == "windows":
        caps.can_send_magic_packet = shutil.which("powershell") is not None
    elif plat == "darwin":
        caps.can_send_magic_packet = True        # 用 python3 发，macOS 自带

    # --- AstrBot 是否装在这台壳上 ---
    # 注意：不能只看 identity.astrbot_root。调用方可能用 --data 显式给了
    # data 目录（NAS 上 ASTRBOT_ROOT 并没有设置，源码在容器里），
    # 这时应该从 data 目录反推根目录，否则会误报"还没有 AstrBot"。
    root = Path(identity.astrbot_root) if identity.astrbot_root else None
    if root is None and target is not None and target.name == "data":
        root = target.parent
    if root and (root / "data").exists():
        caps.astrbot_installed = True
    if not caps.astrbot_installed:
        caps.notes.append("这台壳上还没有 AstrBot 的 data 目录")

    # --- Windows 专属：WOL 真实状态 / 桌面会话 / GPU ---
    if plat == "windows":
        _probe_windows_specifics(caps, check_wol_script)
    else:
        # NAS 侧：只要 Linux 有 python3 就能发包，被叫醒能力对 NAS 不适用
        caps.can_be_woken = None
        caps.notes.append("非 Windows 壳：WOL 被叫醒能力不适用")

    return caps


def _probe_windows_specifics(
    caps: Capabilities,
    check_wol_script: Optional[Path],
) -> None:
    """Windows 侧的三项探测：WOL arm 状态、交互式桌面、GPU。

    全部走"能拿到就要，拿不到就记 nil + 说明"的路线，
    绝不用默认值假装成功。
    """
    # ---- WOL：只认 check-wol.ps1 的输出 ----
    if check_wol_script and check_wol_script.exists():
        rc, out = _run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(check_wol_script),
                "-Json",
            ],
            timeout=45.0,
        )
        if rc == 0:
            try:
                # script 可能在其他位置打印非 JSON 内容，取第一个 '{' 到最后一个 '}'
                start = out.find("{")
                end = out.rfind("}")
                payload = json.loads(out[start : end + 1])
                verdict = payload.get("verdict", {})
                caps.wake_armed = bool(verdict.get("nic_in_wake_armed"))
                caps.can_be_woken = bool(verdict.get("ready_to_wake"))
                caps.notes.append(
                    f"check-wol.ps1: ready_to_wake={caps.can_be_woken}, "
                    f"nic_in_wake_armed={caps.wake_armed}"
                )
            except Exception as e:
                caps.notes.append(f"check-wol.ps1 输出无法解析: {e}")
        else:
            caps.notes.append(f"check-wol.ps1 退出码 {rc}，WOL 状态未知")
    else:
        caps.notes.append("未提供 check-wol.ps1，WOL 状态未知（不猜）")

    # ---- 交互式桌面：有真人会话才能开直播/打游戏 ----
    rc, out = _run(["query", "session"], timeout=10.0)
    if rc == 0:
        # 形如: " console              2  Active        13553"
        for line in out.splitlines():
            low = line.lower()
            if ("console" in low or "rdp-tcp#" in low) and "active" in low:
                caps.has_interactive_desktop = True
                break
        if not caps.has_interactive_desktop:
            caps.notes.append("没有 Active 的交互式会话（无人登录桌面）")
    else:
        caps.notes.append("query session 不可用，桌面会话状态未知")

    # ---- GPU ----
    rc, out = _run(["nvidia-smi", "-L"], timeout=10.0)
    if rc == 0 and "GPU" in out:
        caps.has_gpu = True
    else:
        caps.notes.append("nvidia-smi 不可用或没有 NVIDIA GPU")


# --------------------------------------------------------------------------
# preflight：迁徙前的判决
# --------------------------------------------------------------------------


@dataclass
class Preflight:
    """一次迁徙前的体检结论。

    `action` 的三种取值直接对应 ARCH §7.4 的 Warden 契约表：

      proceed    ready_to_wake=true → 可以正常迁徙
      self_heal  ready_to_wake=false 但 wake_armed=false
                 → arm 丢了（重启后常见），这是**唯一能当场自动修复**的阻塞项
      block      其他情况 → 拒绝迁徙并告警，不要瞎修

    "不要瞎修"这条很重要：如果 `wake_armed=true` 但 `ready_to_wake=false`，
    说明卡在别的地方（比如休眠没关）。这时候去跑 `deviceenablewake`
    只会制造"我修了但它还是不行"的假象。
    """

    action: str
    blocking: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    capabilities: Optional[Capabilities] = None
    identity: Optional[ShellIdentity] = None

    @property
    def ok(self) -> bool:
        return self.action == "proceed"

    def explain(self) -> str:
        lines = [f"preflight: {self.action.upper()}"]
        if self.reasons:
            for r in self.reasons:
                lines.append(f"  - {r}")
        if self.blocking:
            lines.append("  阻塞项:")
            for b in self.blocking:
                lines.append(f"    · {b}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "ok": self.ok,
            "blocking": self.blocking,
            "reasons": self.reasons,
            "capabilities": self.capabilities.to_dict() if self.capabilities else None,
            "identity": self.identity.to_dict() if self.identity else None,
        }


def preflight(
    identity: ShellIdentity,
    caps: Capabilities,
) -> Preflight:
    """把探测结果翻译成一个明确的行动指令。

    这里的判断顺序是刻意的：先看必需能力（缺了什么都白搭），
    再看 WOL（因为它是唯一可自愈的），最后才看可选项。
    """
    blocking: List[str] = []
    reasons: List[str] = []

    # 1) 必需能力
    missing = caps.missing_required()
    if missing:
        for m in missing:
            blocking.append(f"缺少必需能力 {m}：{CAPABILITY_DOCS.get(m, '')}")
        return Preflight(
            action="block",
            blocking=blocking,
            reasons=["必需能力不完整，迁徙无法开始"],
            capabilities=caps,
            identity=identity,
        )

    # 2) WOL 契约表
    #    只在 Windows 壳上判断（NAS 自己不需要被叫醒）
    if identity.platform == "windows":
        if caps.can_be_woken is True:
            reasons.append("WOL 就绪（ready_to_wake=true）")
        elif caps.wake_armed is False:
            # 唯一可自愈的分支
            blocking.append("网卡不在 OS 唤醒白名单（arm 丢了）")
            reasons.append(
                "arm 状态不跨重启保留，这是已知会复发的项；"
                "可以当场自愈：跑 tools/wol-guard-worker.ps1（需要管理员）"
            )
            return Preflight(
                action="self_heal",
                blocking=blocking,
                reasons=reasons,
                capabilities=caps,
                identity=identity,
            )
        elif caps.can_be_woken is False:
            blocking.append("ready_to_wake=false，但网卡已在白名单")
            reasons.append(
                "卡在别的地方（例如休眠/快速启动没关）。"
                "不要跑 deviceenablewake —— 那不会解决，只会制造"
                "'我修了但它还是不行'的假象。"
            )
        else:
            reasons.append("WOL 状态未知（缺 check-wol.ps1 或探测失败）")

    # 3) 交互式桌面：不是硬阻塞，但要让人知道
    if identity.platform == "windows" and not caps.has_interactive_desktop:
        reasons.append(
            "没有 Active 的交互式会话：AstrBot 能跑（做服务即可），"
            "但直播/游戏这类需要有桌面的事做不了（ARCH §9.3）"
        )

    action = "proceed" if not blocking else "block"
    return Preflight(
        action=action,
        blocking=blocking,
        reasons=reasons,
        capabilities=caps,
        identity=identity,
    )
