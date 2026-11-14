"""
wake -- Wake-on-LAN：把一个睡着的壳叫醒。

为什么这不是"发个魔术封包"那么简单
────────────────────────────────────
本项目在真机上踩过两个坑，都是"看起来对、实际不工作"的那种：

**坑一：单播发不出去。**
机器关机后 IP 上已经没有主机在应答 ARP，邻居表项会过期或被标记为 FAILED。
这时往 `192.168.1.20:9` 发单播，内核根本找不到下一跳，`sendto` 直接
`Network is unreachable` / `No route to host` —— **报文没有出过网卡**。
真机上实测就是这种情况：单播路径安静地失败，而人以为"我已经发过了"。

所以必须三路齐发：单播（机器还醒着、ARP 有效时能用）+ 定向广播
（`192.168.1.255`，由交换机泛洪，这是主力）+ 全网广播（`255.255.255.255`，兜底）。

**坑二：`powercfg /devicequery wake_armed` 之外的一切都是猜。**
在 Windows 上，`PnPCapabilities` 的某个位、`Get-NetAdapterPowerManagement`
的返回值、驱动的高级属性列表，**都不能**用来判断"现在能不能被叫醒"。
真机上出现过 `PnPCapabilities=8`（不是常见的 24）但 WOL 完全正常的情况。
唯一可信的判据是 `powercfg /devicequery wake_armed` 里有没有那块网卡 ——
这件事由 `shell.py` 的 `probe_capabilities()` 负责，本模块只管发。

为什么这个模块不发 ARP 请求去"唤醒"邻居表
────────────────────────────────────────────
有些实现会在发封包前先 `arping` 一下，试图让交换机记住 MAC。
对本项目没用：机器关机时 ARP 请求不会得到应答，`arping` 只会白等。
定向广播靠的是交换机的泛洪表，不需要邻居表配合。

依赖
─────
只用标准库。Warden 要能在"什么都没装"的 NAS 上跑（宿主 python 3.10.7 裸环境），
而 `wake` 是 Warden 会调用的动作（"先把目标叫醒，再搬"），所以不能引入
`wakeonlan` 之类的第三方包 —— 尽管官方推荐的第三方包都能做同样的事。
"""

from __future__ import annotations

import socket
import subprocess
import time
from typing import Dict, List, Optional, Sequence

from .errors import Fatal, NeedsHuman

#: 魔术封包的标准端口。9 是 discard，7 是 echo —— 两个都能用，
#: 但网卡固件普遍认 9，而且 9 上不会有别的服务产生噪音。
DEFAULT_PORT = 9

#: 发几轮。真机实测 4 轮（每轮 3 个包、间隔 6 秒）就够把 S5 的机器叫起来，
#: 但从"关机转换开始"到"网卡开始听魔术封包"有窗口期，多几轮更稳。
DEFAULT_COUNT = 4
DEFAULT_INTERVAL = 6.0

#: 单轮里三个包之间的间隔。太小会被某些网卡固件当成同一个包去重。
PER_PACKET_GAP = 0.15


def parse_mac(mac: str) -> bytes:
    """
    接受 `aa:bb:cc:11:c3:d2` / `AA-BB-CC-DD-EE-FF` / `d8bb.c111.c3d2` / `d8bbc111c3d2`。

    四种写法都要认，因为同一个 MAC 会从四个地方来：Linux 的 `ip link`、
    Windows 的 `Get-NetAdapter`、路由器后台、以及人直接在命令行里敲。
    """
    if not isinstance(mac, str):
        raise Fatal(f"MAC 必须是字符串，收到 {type(mac).__name__}")
    hexstr = mac.replace(":", "").replace("-", "").replace(".", "").replace(" ", "")
    if len(hexstr) != 12:
        raise Fatal(f"MAC 格式不对（去掉分隔符后应当 12 个十六进制字符）：{mac!r}")
    try:
        return bytes.fromhex(hexstr)
    except ValueError as e:
        raise Fatal(f"MAC 里有非十六进制字符：{mac!r}（{e}）") from e


def format_mac(mac: str) -> str:
    """规范成小写冒号分隔，用于日志与互相比对。"""
    raw = parse_mac(mac)
    return ":".join(f"{b:02x}" for b in raw)


def magic_packet(mac: str) -> bytes:
    """
    标准魔术封包：6 个 0xFF 前缀 + MAC 重复 16 次 = 102 字节。

    102 这个数字是判据，不是巧合：真机上核对封包时就是数"前 6 字节全 ff、
    后面正好 16 段 MAC、总长 102"。写错成 15 次或 17 次，某些网卡照样能醒，
    所以不能用"能不能唤醒"来验证格式 —— 要直接校验字节。
    """
    m = parse_mac(mac)
    return b"\xff" * 6 + m * 16


def directed_broadcast(ip: str, prefix_len: int = 24) -> str:
    """
    由 IP 与掩码长度算出定向广播地址（`192.168.1.20/24` → `192.168.1.255`）。

    为什么不直接用 `255.255.255.255` 就完事：全网广播在不少交换机/路由器上
    被丢弃（防广播风暴），而定向广播几乎总能过。主力是它，全广播只是兜底。
    """
    parts = ip.split(".")
    if len(parts) != 4:
        raise Fatal(f"不是一个 IPv4 地址：{ip!r}")
    try:
        octets = [int(p) for p in parts]
    except ValueError as e:
        raise Fatal(f"IPv4 地址里有非数字段：{ip!r}") from e
    if any(o < 0 or o > 255 for o in octets):
        raise Fatal(f"IPv4 地址段超出 0-255：{ip!r}")
    if not 0 <= prefix_len <= 32:
        raise Fatal(f"掩码长度必须在 0-32 之间：{prefix_len}")

    if prefix_len == 32:
        return ip
    if prefix_len == 0:
        return "255.255.255.255"

    # 把掩码算成一个 32 位整数，再取反
    mask = (0xFFFFFFFF << (32 - prefix_len)) & 0xFFFFFFFF
    as_int = (octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]
    bcast = as_int | (~mask & 0xFFFFFFFF)
    return ".".join(str((bcast >> s) & 0xFF) for s in (24, 16, 8, 0))


def wake_targets(host: str, prefix_len: int = 24) -> List[str]:
    """
    三路齐发的那三个地址，去重后按"优先尝试"的顺序返回。

    顺序有意义：先单播（最快，机器可能还没真的断电），再定向广播（主力），
    最后全网广播（兜底）。真机调试时这个顺序也方便定位"到底是哪一路生效了"。
    """
    if not host:
        # 只知道 MAC、不知道 IP 时，只有全网广播可用 —— 能工作，但更容易被交换机丢掉
        return ["255.255.255.255"]
    out = [host]
    try:
        bcast = directed_broadcast(host, prefix_len)
        if bcast != host:
            out.append(bcast)
    except Fatal:
        # host 不是 IPv4（比如传了个主机名）：那就只剩全网广播
        pass
    if "255.255.255.255" not in out:
        out.append("255.255.255.255")
    # 去重但保序
    seen = set()
    uniq = []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _send_udp(packet: bytes, targets: Sequence[str], port: int) -> List[Dict]:
    """
    逐个目标发，**每个目标单独一个 socket**。

    理由：一个目标发失败（`No route to host` 是 ICMP 层报回来的，会污染
    socket 的错误状态）会让后续 `sendto` 也失败。共用 socket 时，
    单播失败会把广播也拖下水 —— 那正是本项目最开始的 bug 形态。
    """
    results = []
    for target in targets:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(2.0)
            sock.sendto(packet, (target, port))
            results.append({"target": target, "ok": True, "error": ""})
        except OSError as e:
            results.append({"target": target, "ok": False, "error": str(e)})
        finally:
            try:
                sock.close()
            except OSError:
                pass
    return results


def wake(
    mac: str,
    *,
    host: str = "",
    prefix_len: int = 24,
    port: int = DEFAULT_PORT,
    count: int = DEFAULT_COUNT,
    interval: float = DEFAULT_INTERVAL,
    packet_gap: float = PER_PACKET_GAP,
    quiet: bool = False,
) -> Dict:
    """
    发 `count` 轮魔术封包，返回一份可以直接打印/记日志的摘要。

    返回值里 `sent` 是"至少有一个目标成功的轮数"，`failures` 是每个目标
    每个轮次的失败原因 —— 两者都要，因为"发出了但没人收到"和
    "根本没发出去"是两种完全不同的故障，不能合并成一个布尔。

    不在这里判断目标是否真的醒了：那是 `wait_online()` 的事。
    也不在这里做任何重试策略：重试属于调用方（Warden / CLI），
    因为"什么时候该放弃"取决于人还在不在等。
    """
    if count < 1:
        raise Fatal(f"count 必须 ≥ 1：{count}")
    if interval < 0:
        raise Fatal(f"interval 不能是负数：{interval}")

    packet = magic_packet(mac)
    targets = wake_targets(host, prefix_len)
    rounds: List[Dict] = []
    total_ok = 0

    for i in range(count):
        results = _send_udp(packet, targets, port)
        ok_count = sum(1 for r in results if r["ok"])
        if ok_count:
            total_ok += 1
        rounds.append({"round": i + 1, "ok": ok_count, "results": results})
        if not quiet:
            detail = ", ".join(
                f"{r['target']}{'✓' if r['ok'] else '✗'}" for r in results
            )
            print(f"  第 {i + 1} 轮：{ok_count}/{len(targets)} 成功（{detail}）")
        if i + 1 < count:
            time.sleep(interval)

    return {
        "mac": format_mac(mac),
        "packet_bytes": len(packet),
        "targets": targets,
        "port": port,
        "rounds_sent": len(rounds),
        "rounds_ok": total_ok,
        "packets": total_ok * len(targets),
        "rounds": rounds,
        # 一个都发不出去 = 环境有问题（防火墙/网卡/权限），不是"机器没醒"
        "any_sent": total_ok > 0,
    }


def _tcp_alive(host: str, port: int, timeout: float) -> bool:
    """TCP 三次握手能完成 = 机器起来了且那个服务在听。比 ICMP 更接近"能干活"。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def can_probe_icmp() -> bool:
    """这台机器上能不能用 ICMP 判据？

    问这个问题是因为**探测工具不存在**和**目标机没醒**是两件完全不同的事，
    而它们的表现一模一样（每一轮都返回"不通"）。2026-11-14 真机上就是这样：
    AstrBot 容器里没有 `ping`，`migrate --wake-host` 白等 180 秒，最后报
    "叫不醒"——机器其实开着，人正坐在前面。

    报错文案把这两件事混成一件，比白等 180 秒更糟：它会让人去查 BIOS。
    所以先问清楚"我有没有能力判断"，再决定要不要等。
    """
    import shutil

    return shutil.which("ping") is not None


def _icmp_alive(host: str, timeout: float = 1.0) -> bool:
    """
    ICMP ping。Windows 与 Linux 的参数不一样，所以分平台。

    为什么还留着 ICMP：机器刚上电时可能一个 TCP 服务都还没起来
    （AstrBot 要几十秒才监听端口），但网络栈已经通了 —— 这时候
    "醒了"和"能干活"确实是两个时刻，前者用 ICMP 判断更及时。
    """
    import sys

    if sys.platform == "win32":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), host]
    try:
        r = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout + 2
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def wait_online(
    host: str,
    *,
    port: Optional[int] = None,
    timeout: float = 180.0,
    interval: float = 2.0,
    probe_timeout: float = 2.0,
    on_tick=None,
) -> Dict:
    """
    等到目标机"能连上"，返回耗时。

    `port` 给了就用 TCP 连（判断"那个服务能干活了"）；
    没给就用 ICMP（判断"网络栈起来了"）。两个判据不能互换：
    真机上 WOL 唤醒后 23.1 秒网络恢复，但 AstrBot 还要再等几十秒才监听 6199。

    **单次探测的超时必须受剩余预算约束**（`probe_timeout` 会被压到不超过剩余时间）。
    这条是实测出来的：第一版里单次探测固定 `timeout=2.0`，于是 `timeout=0.6`
    的函数实际跑了 2.006 秒 —— 一个承诺"最多等 N 秒"的函数实际等了三倍多。
    这类偏差在真机上不显眼（180 秒的超时变成 182 秒），但它是**承诺与行为不符**，
    而且会让"探测间隔"完全失真。

    超时抛 `NeedsHuman` 而不是返回 False：**叫不醒是一个需要人去看一眼的
    情况**（网卡没 arm / BIOS 改了 / 换了 Wi-Fi），不是程序该静默重试的事情。
    区别很重要 —— 静默重试会让人以为系统在正常工作，而实际上它永远叫不醒。
    """
    if timeout <= 0:
        raise Fatal(f"timeout 必须大于 0：{timeout}")
    if interval < 0:
        raise Fatal(f"interval 不能是负数：{interval}")
    if probe_timeout <= 0:
        raise Fatal(f"probe_timeout 必须大于 0：{probe_timeout}")

    # 没有 ping 又没给端口时，**立刻**说清楚，而不是等满 180 秒再报「叫不醒」。
    # 这两件事在结果上一模一样（都是"没连上"），但原因相差整个方向：
    # 一个是目标机的事（去查 BIOS / 网卡 arm），一个是本机的事（装个 ping 或换个判据）。
    if port is None and not can_probe_icmp():
        raise NeedsHuman(
            f"没法判断 {host} 醒没醒：这台机器上没有 `ping`，ICMP 判据用不了。"
            "\n  注意这不是「目标机没醒」——是我探测不了。"
            "\n  两个选择："
            "\n    · 给一个 TCP 判据：--wake-port-check <端口>"
            "\n    · 或者别等：快照进账本之后，目标机醒来会自己来取"
        )

    started = time.monotonic()
    deadline = started + timeout
    attempts = 0

    while True:
        attempts += 1
        elapsed = time.monotonic() - started
        # 单次探测不许超过剩余预算，否则"最多等 N 秒"就是假的
        budget = max(0.05, min(probe_timeout, deadline - time.monotonic()))
        alive = _tcp_alive(host, port, budget) if port else _icmp_alive(host, budget)
        if alive:
            return {
                "host": host,
                "criterion": f"tcp/{port}" if port else "icmp",
                "online": True,
                "seconds": round(time.monotonic() - started, 1),
                "attempts": attempts,
            }
        if on_tick is not None:
            on_tick(attempts, elapsed)
        if time.monotonic() + interval >= deadline:
            break
        time.sleep(interval)

    criterion = f"tcp/{port}" if port else "icmp echo"
    raise NeedsHuman(
        f"{timeout:.0f} 秒内没能连上 {host}（判据：{criterion}，试了 {attempts} 次）。"
        f"\n  叫不醒通常只有三种原因，按概率排："
        f"\n    1. 网卡不在系统的唤醒白名单里 —— 跑 check-wol.ps1 看 ready_to_wake"
        f"\n    2. 上电后没有进到能联网的状态（快速启动没关 / 网线没插好）"
        f"\n    3. 换了网卡或驱动更新把 WOL 设置重置了"
        f"\n  这不是程序能自己重试解决的事，需要人看一眼。"
    )


__all__ = [
    "DEFAULT_COUNT",
    "DEFAULT_INTERVAL",
    "DEFAULT_PORT",
    "directed_broadcast",
    "format_mac",
    "magic_packet",
    "parse_mac",
    "wait_online",
    "wake",
    "wake_targets",
]
