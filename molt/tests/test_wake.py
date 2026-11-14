"""
test_wake.py -- WOL 模块的测试。

这组测试的两个重点，都不是"发出去没报错"：

1. **封包字节必须逐字节对**。写错成 15 段或 17 段 MAC 时，某些网卡照样能醒，
   所以"能唤醒"不是格式正确的证据。真机上核对过 102 字节、前 6 字节全 ff、
   后面 16 段、末尾 6 字节等于 MAC —— 测试把这几条固定住。

2. **`wait_online` 超时抛 `NeedsHuman` 而不是返回 False**。这条是有意的设计：
   叫不醒是一个需要人看一眼的情况。如果它返回 False，调用方几乎一定会写
   `while not ok: retry()`，于是系统看起来在工作，实际上永远叫不醒。
"""

import socket
import time

import pytest

from molt.errors import Fatal, NeedsHuman
from molt import wake as W


# ── MAC 解析 ──────────────────────────────────────────────────────────


def test_parse_mac_accepts_four_spellings():
    """同一个 MAC 会从四个地方来：ip link / Get-NetAdapter / 路由器 / 人手敲。"""
    expected = bytes.fromhex("aabbccddeeff")
    for spelling in [
        "aa:bb:cc:dd:ee:ff",
        "AA-BB-CC-DD-EE-FF",
        "aabb.ccdd.eeff",
        "aabbccddeeff",
        "aa bb cc dd ee ff",
    ]:
        assert W.parse_mac(spelling) == expected, spelling


def test_parse_mac_rejects_bad_input():
    for bad in ["", "aa:bb:cc", "aa:bb:cc:dd:ee:zz", "aa:bb:cc:dd:ee:ff:00"]:
        with pytest.raises(Fatal):
            W.parse_mac(bad)


def test_parse_mac_rejects_non_string():
    with pytest.raises(Fatal):
        W.parse_mac(None)  # type: ignore[arg-type]


def test_format_mac_is_canonical_lowercase():
    assert W.format_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"


# ── 魔术封包格式（核心）────────────────────────────────────────────────


def test_magic_packet_is_102_bytes():
    assert len(W.magic_packet("aa:bb:cc:dd:ee:ff")) == 102


def test_magic_packet_structure():
    pkt = W.magic_packet("aa:bb:cc:dd:ee:ff")
    mac = bytes.fromhex("aabbccddeeff")
    assert pkt[:6] == b"\xff" * 6, "前缀必须是 6 个 0xFF"
    body = pkt[6:]
    assert len(body) == 96
    for i in range(16):
        assert body[i * 6:(i + 1) * 6] == mac, f"第 {i + 1} 段 MAC 不对"
    assert pkt[-6:] == mac, "末尾 6 字节必须等于 MAC"


def test_magic_packet_differs_by_mac():
    a = W.magic_packet("aa:bb:cc:dd:ee:ff")
    b = W.magic_packet("aa:bb:cc:dd:ee:f0")
    assert a != b
    assert a[:6] == b[:6] == b"\xff" * 6


# ── 三路齐发 ──────────────────────────────────────────────────────────


def test_wake_targets_three_paths():
    t = W.wake_targets("192.168.1.20", 24)
    assert t == ["192.168.1.20", "192.168.1.255", "255.255.255.255"]


def test_wake_targets_dedupes_when_host_is_broadcast():
    """直接给了广播地址时，不要重复发同一个目标。"""
    t = W.wake_targets("192.168.1.255", 24)
    assert t.count("192.168.1.255") == 1
    assert t[-1] == "255.255.255.255"


def test_wake_targets_hostname_falls_back_to_global_broadcast():
    """只知道 MAC、不知道 IP（或者给了主机名）时，只剩全网广播可用。"""
    t = W.wake_targets("win-shell", 24)
    assert t == ["win-shell", "255.255.255.255"]


def test_wake_targets_without_host_uses_broadcast_only():
    assert W.wake_targets("", 24) == ["255.255.255.255"]


@pytest.mark.parametrize(
    "ip,prefix,expected",
    [
        ("192.168.1.20", 24, "192.168.1.255"),
        ("192.168.1.20", 16, "192.168.255.255"),
        ("10.0.0.5", 8, "10.255.255.255"),
        ("172.16.5.9", 12, "172.31.255.255"),
        ("192.168.1.20", 32, "192.168.1.20"),
        ("192.168.1.20", 0, "255.255.255.255"),
        ("192.168.1.20", 25, "192.168.1.127"),
        ("192.168.1.130", 25, "192.168.1.255"),
    ],
)
def test_directed_broadcast(ip, prefix, expected):
    assert W.directed_broadcast(ip, prefix) == expected


def test_directed_broadcast_rejects_bad_input():
    for ip, prefix in [("192.168.1", 24), ("192.168.1.256", 24), ("a.b.c.d", 24)]:
        with pytest.raises(Fatal):
            W.directed_broadcast(ip, prefix)
    with pytest.raises(Fatal):
        W.directed_broadcast("192.168.1.20", 33)


# ── 发包（真 socket，不需要目标存在）────────────────────────────────


def test_wake_sends_three_packets_per_round():
    """
    真的发到 127.0.0.1 上：本机一定有 UDP 栈，`sendto` 一定成功。
    这证明的是"发包这条路是通的"，不是"有机器被唤醒"。
    """
    r = W.wake("aa:bb:cc:dd:ee:ff", host="127.0.0.1", count=1, interval=0, quiet=True)
    assert r["any_sent"] is True
    assert r["rounds_sent"] == 1
    assert r["rounds_ok"] == 1
    assert r["packet_bytes"] == 102
    assert r["mac"] == "aa:bb:cc:dd:ee:ff"
    # 127.0.0.1 的定向广播是 127.0.0.1 自己（去重后）+ 全网广播
    assert "255.255.255.255" in r["targets"]


def test_wake_multiple_rounds():
    r = W.wake("aa:bb:cc:dd:ee:ff", host="127.0.0.1", count=3, interval=0, quiet=True)
    assert r["rounds_sent"] == 3
    assert [x["round"] for x in r["rounds"]] == [1, 2, 3]


def test_wake_records_failures_per_target():
    """
    一个发不出去的目标不能拖垮其他目标 —— 这正是本项目最开始的 bug 形态：
    共用 socket 时，单播的 `No route to host` 会让后面的广播也失败。
    """
    r = W.wake("aa:bb:cc:dd:ee:ff", host="203.0.113.9", count=1, interval=0, quiet=True)
    # 203.0.113.0/24 是 TEST-NET-3，不可路由；至少广播那一路应当能发出去
    assert r["rounds_sent"] == 1
    assert len(r["rounds"][0]["results"]) == len(r["targets"])
    # 无论成败，每个目标都要有独立的记录
    for item in r["rounds"][0]["results"]:
        assert set(item) == {"target", "ok", "error"}


def test_wake_rejects_bad_count_and_interval():
    with pytest.raises(Fatal):
        W.wake("aa:bb:cc:dd:ee:ff", count=0)
    with pytest.raises(Fatal):
        W.wake("aa:bb:cc:dd:ee:ff", interval=-1)


def test_wake_rejects_bad_mac_before_sending():
    """MAC 错必须在发包之前失败 —— 否则人只会看到"发了但没醒"。"""
    with pytest.raises(Fatal):
        W.wake("not-a-mac", host="127.0.0.1", count=1, quiet=True)


# ── wait_online ───────────────────────────────────────────────────────


def test_wait_online_returns_immediately_when_already_up(tmp_path):
    """本机一定在线。判据用 ICMP 太慢也不可靠，所以用一个必然在听的端口。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        r = W.wait_online("127.0.0.1", port=port, timeout=5, interval=0.1)
        assert r["online"] is True
        assert r["criterion"] == f"tcp/{port}"
        assert r["seconds"] >= 0
    finally:
        srv.close()


def test_wait_online_times_out_with_needs_human():
    """
    关键断言：超时抛 `NeedsHuman`，不是返回 False。

    返回 False 会让调用方写出 `while not ok: retry()`，于是系统看起来在工作，
    实际上永远叫不醒。异常强制人看一眼。
    """
    with pytest.raises(NeedsHuman) as ei:
        # 端口 1 在本机不会有服务监听；timeout 很短以便测试快
        W.wait_online("127.0.0.1", port=1, timeout=0.6, interval=0.2)
    msg = str(ei.value)
    assert "没能连上" in msg
    # 消息里必须给出"人该去看什么"，否则人只能干瞪眼
    assert "唤醒白名单" in msg
    assert "check-wol.ps1" in msg


def test_wait_online_calls_on_tick():
    """
    `on_tick` 每次探测之后都会被调用，参数是 `(第几次, 已耗时)`。

    这里显式给 `probe_timeout=0.1`：本机连 `127.0.0.1:1` 会被 Windows 防火墙
    静默丢弃（不是立刻 ECONNREFUSED），所以单次探测会吃掉整个预算。
    默认 `probe_timeout=2.0` 时，`timeout=0.5` 只够探测一次 —— 那是正确行为，
    不是 bug（"最多等 0.5 秒"就该只探一次）。
    """
    ticks = []
    with pytest.raises(NeedsHuman):
        W.wait_online(
            "127.0.0.1", port=1, timeout=0.6, interval=0.2, probe_timeout=0.1,
            on_tick=lambda n, secs: ticks.append((n, round(secs, 1))),
        )
    assert len(ticks) >= 2, f"应当至少探测两次，实际 {ticks}"
    assert ticks[0][0] == 1


def test_wait_online_does_not_overshoot_its_timeout():
    """
    一个承诺"最多等 N 秒"的函数，不能因为单次探测慢就跑出 N 秒。

    这个 bug 是实测抓到的：第一版单次探测固定 `timeout=2.0`，
    于是 `timeout=0.6` 的函数实际跑了 **2.006 秒**。
    在真机上 180 秒变成 182 秒不显眼，但它是承诺与行为不符，
    而且会让"探测间隔"完全失真（tick 只发生了一次）。
    """
    t0 = time.monotonic()
    with pytest.raises(NeedsHuman):
        W.wait_online("127.0.0.1", port=1, timeout=0.6, interval=0.2)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.6, f"timeout=0.6 却跑了 {elapsed:.2f} 秒，单次探测没受预算约束"


def test_wait_online_budget_shrinks_probe_timeout():
    """
    预算剩下的比 `probe_timeout` 更少时，应当用剩下的那个。
    用 `_tcp_alive` 打桩来观察实际传进去的超时值。
    """
    seen = []
    real = W._tcp_alive

    def spy(host, port, timeout):
        seen.append(round(timeout, 2))
        return real(host, port, timeout)

    orig = W._tcp_alive
    W._tcp_alive = spy
    try:
        with pytest.raises(NeedsHuman):
            W.wait_online("127.0.0.1", port=1, timeout=0.9, interval=0.3,
                          probe_timeout=2.0)
    finally:
        W._tcp_alive = orig
    assert seen, "应当至少探测一次"
    assert all(t <= 0.9 for t in seen), f"探测超时超出了总预算：{seen}"
    assert seen[0] == 0.9, f"首次探测应当用满剩余预算，实际 {seen[0]}"


def test_wait_online_rejects_nonpositive_timeout():
    with pytest.raises(Fatal):
        W.wait_online("127.0.0.1", port=1, timeout=0)
    with pytest.raises(Fatal):
        W.wait_online("127.0.0.1", port=1, timeout=-5)
    with pytest.raises(Fatal):
        W.wait_online("127.0.0.1", port=1, timeout=5, probe_timeout=0)


def test_icmp_alive_on_localhost_or_graceful():
    """
    ICMP 判据只验证"不会崩"。本机 ping 在某些环境被禁，所以不断言 True。
    """
    assert isinstance(W._icmp_alive("127.0.0.1", timeout=0.5), bool)


# --- bug #14：没有 ping 时不许「假装等 180 秒然后报叫不醒」 -------------------


def test_can_probe_icmp_follows_shutil_which(monkeypatch):
    """它只回答一个问题：这台机器上有没有 ping 这个命令。"""
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ping" if name == "ping" else None)
    assert W.can_probe_icmp() is True

    monkeypatch.setattr("shutil.which", lambda name: None)
    assert W.can_probe_icmp() is False


def test_wait_online_refuses_immediately_when_icmp_unavailable(monkeypatch):
    """
    **关键在"立刻"**：带 `--wake-host` 的迁徙曾经在容器里白等 180 秒，
    然后报「叫不醒」——而机器开着、人正坐在前面。等 3 分钟是次要损失，
    报错原因说反了才是主要损失：它会让人去查 BIOS。
    """
    monkeypatch.setattr(W, "can_probe_icmp", lambda: False)

    started = time.monotonic()
    with pytest.raises(NeedsHuman) as excinfo:
        W.wait_online("192.168.1.20", timeout=180.0)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"应该立刻失败，实际等了 {elapsed:.1f} 秒"
    message = str(excinfo.value)
    # 说清"是探测不了"，而不是"目标机没醒"
    assert "没法判断" in message
    assert "没醒" in message
    # 并给出两条可执行的出路
    assert "--wake-port-check" in message


def test_wait_online_tcp_path_never_needs_ping(monkeypatch):
    """给了端口就走 TCP —— 这条路上没有 ping 也照样能判断。"""
    monkeypatch.setattr(W, "can_probe_icmp", lambda: False)
    monkeypatch.setattr(W, "_tcp_alive", lambda host, port, timeout: True)

    result = W.wait_online("192.168.1.20", port=6199, timeout=5.0)
    assert result["online"] is True
    assert result["criterion"] == "tcp/6199"

