#!/usr/bin/env python3
"""
wol-test.py -- Molt / 蜕壳 的 WOL 实测观察器（跑在 NAS 上）

为什么需要它：发起关机的机器就是被试机器，关机后观察者跟着一起死。
所以观察器必须待在另一台机器（NAS）上，先跑起来，再等人关机。

它做三件事，全程写日志：
  1. 先确认目标机在线（否则说明人还没关机，继续等）
  2. 一旦掉线（ping 不通），开始周期发魔术封包
  3. 持续 ping，记录"第几秒被叫醒"

为什么每次发三个包（单播 + 定向广播 + 全网广播）：
  机器关机后 IP 上的主机不再响应 ARP，NAS 的邻居表项会过期，
  只发单播会 sendto 到一个无法解析的地址 —— 报文根本出不去。
  定向广播（192.168.1.255）由交换机泛洪，是 WOL 的主力。
  全网广播（255.255.255.255）留作兜底。

用法：
  python3 wol-test.py --mac aa:bb:cc:11:c3:d2 --host 192.168.1.20 --minutes 20
"""
import argparse, json, os, socket, subprocess, sys, time
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
OUT_DIR = "/mnt/dm-0/astrbot/_molt/wol-test"


def now():
    return datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S")


def parse_mac(mac):
    h = mac.replace(":", "").replace("-", "").replace(".", "")
    if len(h) != 12:
        raise ValueError("MAC 格式不对: " + mac)
    return bytes.fromhex(h)


def magic_packet(mac):
    m = parse_mac(mac)
    return b"\xff" * 6 + m * 16


def ping(host, timeout=1):
    """返回 True/False，静默。"""
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), host],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout + 2,
        )
        return r.returncode == 0
    except Exception:
        return False


def arp_has(host):
    """在邻居表里能否看到该 IP（用来判断它是不是刚连上来）。"""
    try:
        out = subprocess.run(["ip", "neigh", "show", host],
                             capture_output=True, text=True, timeout=3).stdout
        return ("REACHABLE" in out) or ("STALE" in out) or ("DELAY" in out)
    except Exception:
        return False


def send_packet(pkt, dests):
    """把封包发到一组目标上，返回实际成功的个数。"""
    ok = 0
    for d in dests:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(pkt, (d, 9))
            s.close()
            ok += 1
        except Exception:
            pass
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mac", required=True)
    ap.add_argument("--host", required=True)
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--broadcast-addr", default=None,
                    help="定向广播地址，默认由 host 推出 /24 的 .255")
    ap.add_argument("--unicast", action="store_true", default=True,
                    help="是否也发单播（默认开）")
    ap.add_argument("--packet-interval", type=float, default=5, help="掉线后多久发一次封包")
    ap.add_argument("--ping-interval", type=float, default=2)
    ap.add_argument("--force", action="store_true",
                    help="目标一开始就不通时也继续跑（默认直接退出）")
    args = ap.parse_args()

    # 推出定向广播
    baddr = args.broadcast_addr
    if baddr is None:
        parts = args.host.split(".")
        if len(parts) == 4:
            baddr = ".".join(parts[:3] + ["255"])
        else:
            baddr = "255.255.255.255"

    dests = [baddr, "255.255.255.255"]
    if args.unicast:
        dests.insert(0, args.host)

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y%m%dT%H%M%S")
    log_path = os.path.join(OUT_DIR, "result-%s.json" % stamp)

    result = {
        "started_at": now(),
        "mac": args.mac,
        "host": args.host,
        "destinations": dests,
        "events": [],
        "shutdown_detected_at": None,
        "wake_success_at": None,
        "wake_latency_seconds": None,
        "packets_sent": 0,
        "rounds": 0,
        "verdict": None,
    }

    print_ = print

    def ev(kind, msg, **kw):
        e = {"t": now(), "kind": kind, "msg": msg}
        e.update(kw)
        result["events"].append(e)
        print_("[%s] %-13s %s" % (now(), kind, msg), flush=True)

    pkt = magic_packet(args.mac)
    ev("start", "观察器启动，封包 %d 字节，目标 %s" % (len(pkt), args.host),
       destinations=dests)

    online = ping(args.host)
    ev("online_check", "目标机当前%s" % ("在线" if online else "不在线"), online=online)
    if not online and not args.force:
        ev("abort", "目标机一开始就不通，直接退出（加 --force 可强制跑）")
        result["verdict"] = "NOT_ONLINE_AT_START"
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print_("日志: %s" % log_path)
        return 1

    deadline = time.time() + args.minutes * 60
    phase = "wait_offline"
    off_since = None
    last_packet = 0.0

    try:
        while time.time() < deadline:
            alive = ping(args.host)

            if phase == "wait_offline":
                if alive:
                    time.sleep(args.ping_interval)
                    continue
                # 连续两次不通才算真掉线，防抖动
                time.sleep(1)
                if ping(args.host):
                    time.sleep(args.ping_interval)
                    continue
                phase = "sending"
                off_since = time.time()
                result["shutdown_detected_at"] = now()
                ev("offline", "检测到掉线，开始发魔术封包", arp_in_table=arp_has(args.host))
                last_packet = 0.0

            elif phase == "sending":
                t = time.time()
                if t - last_packet >= args.packet_interval:
                    n = send_packet(pkt, dests)
                    result["packets_sent"] += n
                    result["rounds"] += 1
                    ev("packet", "第 %d 轮，发出 %d 个封包（累计 %d）"
                       % (result["rounds"], n, result["packets_sent"]))
                    last_packet = t

                if alive:
                    lat = time.time() - off_since
                    result["wake_success_at"] = now()
                    result["wake_latency_seconds"] = round(lat, 1)
                    result["verdict"] = "PASS"
                    ev("wake", "机器回来了！从掉线到网络恢复 %.1f 秒" % lat,
                       latency=round(lat, 1), rounds=result["rounds"],
                       packets=result["packets_sent"])
                    break

            time.sleep(args.ping_interval)

        if result["verdict"] is None:
            if phase == "wait_offline":
                result["verdict"] = "NO_SHUTDOWN"
                ev("end", "超时：整个过程机器一直在线，没检测到关机")
            else:
                result["verdict"] = "FAIL"
                ev("end", "超时：发了 %d 个封包仍未被叫醒" % result["packets_sent"])

    except KeyboardInterrupt:
        result["verdict"] = "INTERRUPTED"
        ev("end", "被中断")

    result["ended_at"] = now()
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print_("")
    print_("=" * 52)
    print_("结论:     %s" % result["verdict"])
    print_("关机检测: %s" % result["shutdown_detected_at"])
    print_("唤醒成功: %s" % result["wake_success_at"])
    print_("用时:     %s 秒" % result["wake_latency_seconds"])
    print_("发包轮数: %d（共 %d 个）" % (result["rounds"], result["packets_sent"]))
    print_("日志:     %s" % log_path)
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
