#!/usr/bin/env python3
"""维护窗口的保险丝：如果这个窗口没人收尾，就把生产容器拉起来。

## 为什么需要它

一次真正的迁徙窗口里，NAS 上那只鱼会先停下来（`drain` 或 `docker kill -s INT`），
然后 Windows 上的新壳接过去。这中间有一段**生产容器是关着的**时间。

那段时间里，唯一能保证"鱼会回来"的是**人的手**——而人的手会累、会分心、
会因为 SSH 掉线而消失。如果人正好在那一刻消失，鱼就一直哑着，
而且**哑着的鱼比坏掉的鱼更不容易被发现**：没人报错，只是没人回消息。

所以这个窗口必须有一个**不依赖任何一方还活着的保险丝**：
它跑在权威那一侧（NAS），它不需要网络、不需要客户端配合，
时间一到就把容器拉起来。

## 它不是什么

它**不是** fencing，**不能**用来防"两条鱼同时在线"。它只做一件很笨的事：
超时了就把生产容器 `docker start`。真正的互斥仍然由 Warden 的租约
与 `molt` 的"先死后生"负责。

## 用法

    python3 tools/window-guard.py --minutes 15 --cancel-file /tmp/window-done

窗口正常结束时，人（或脚本）**创建 cancel 文件**，guard 看到就退出、什么都不做。
没创建，到点执行 `docker start <container>` 并写日志。

判据故意选"超时就恢复"而不是"超时就什么都不做"：
漏恢复 = 鱼一直哑着（静默故障），误恢复 = 容器多起一次（一条日志）。
这两种错误的代价不对称，所以默认动作必须是"恢复"。
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

CHECK_INTERVAL = 2.0


def _container_running(name: str) -> bool:
    try:
        out = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def _start(name: str) -> tuple:
    try:
        out = subprocess.run(
            ["docker", "start", name], capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 1, f"docker start 失败：{e}"
    return out.returncode, (out.stdout + out.stderr).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="维护窗口保险丝")
    ap.add_argument("--container", default="astrbot")
    ap.add_argument("--minutes", type=float, default=15.0)
    ap.add_argument("--cancel-file", required=True)
    ap.add_argument("--log", default="")
    ap.add_argument("--already-stopped", action="store_true",
                    help="启动时容器已经是停的（正常情况下应该已经停了）")
    args = ap.parse_args()

    cancel = Path(args.cancel_file)
    log_path = Path(args.log) if args.log else None

    def log(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        if log_path is not None:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    deadline = time.monotonic() + args.minutes * 60.0
    log(f"--- 窗口开始 --- 容器={args.container} 上限={args.minutes:g} 分钟 "
        f"取消文件={cancel}")

    if _container_running(args.container):
        log(f"[注意] 容器 {args.container} 现在是**在跑**的。"
            f"这个 guard 依然按超时工作，但你可能还没开始窗口。")

    while True:
        if cancel.exists():
            log("--- 窗口正常结束（看到取消文件）--- 不动容器")
            return 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(CHECK_INTERVAL, max(0.1, remaining)))

    log(f"--- 超时 {args.minutes:g} 分钟，没有看到取消文件 ---")
    if _container_running(args.container):
        log("容器已经在跑，不需要恢复。")
        return 0
    rc, out = _start(args.container)
    log(f"docker start {args.container} → 返回码 {rc}：{out}")
    if rc != 0:
        log("[FAIL] 拉不起来。鱼现在是哑的，需要人来看。")
        return 1
    for _ in range(45):
        if _container_running(args.container):
            log("✓ 生产容器已恢复运行")
            return 0
        time.sleep(2.0)
    log("[FAIL] docker start 返回 0 但容器 90 秒内没进入 running。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
