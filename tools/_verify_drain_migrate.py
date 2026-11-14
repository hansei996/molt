"""真机验证：`molt migrate --control-url` 会不会真的先把源壳按住。

## 为什么这个验证必须做在真机上

`test_migrate_drain.py` 里的 `FakeControl` 是我自己写的，它"证明"了顺序正确
——但那只能证明**我的代码按我以为的顺序调了我的假货**。真机要回答的是另外
几个问题：

  1. 那个 URL 真的能连上吗（端口、JWT、中间件）
  2. `drain_and_verify` 的轮询在真实 AstrBot 上会不会一直转
  3. 按完之后，AstrBot 的**日志**里有没有 `terminate platform adapter`
  4. 热备出来的库，是不是"它说完最后一句话之后"的状态

## 用哪台壳

用 `G:\\molt-p0\\ctl-test\\`（装了 melt 控制面插件 + 环回适配器，
`platform[].enable` 里 QQ 那条是 false）。**绝不碰 NAS 生产容器。**
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

MOLT = Path(r"C:\Users\13553\Downloads\DSH_workspace\molt")
DATA = Path(r"G:\molt-p0\ctl-test\data")
ROOT = Path(r"G:\molt-p0\drain-e2e\warden")
CONFIG = DATA / "cmd_config.json"
BASE = "http://127.0.0.1:6199"
PY = sys.executable

sys.path.insert(0, str(MOLT))

from molt.control import AstrBotControl  # noqa: E402


def step(title: str) -> None:
    print()
    print(f"=== {title} ===")


def main() -> int:
    step("0) 先把源壳叫回来说话（上一轮验证结束时它是哑的）")
    ctl = AstrBotControl.from_config(BASE, str(CONFIG))
    try:
        st = ctl.status()
        print(f"  drain 之前 speaking = {st.get('speaking')}")
        if not st.get("speaking"):
            r = ctl.resume()
            print(f"  resume -> resumed={r.get('resumed')} connected={r.get('connected')}")
        st = ctl.status()
        print(f"  现在 speaking = {st.get('speaking')}  connected={st.get('platforms', {}).get('connected_ids')}")
        if not st.get("speaking"):
            print("  ✗ 叫不回来，后面的验证没有意义")
            return 1
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ 控制面连不上：{type(e).__name__}: {e}")
        return 1

    step("1) 跑 molt migrate --control-url（这就是用户会敲的那一条）")
    cmd = [
        PY,
        "-m",
        "molt.cli",
        "migrate",
        "--data",
        str(DATA),
        "--shell",
        "win-loopback",
        "--root",
        str(ROOT),
        "--target",
        "nas-fish",
        "--control-url",
        BASE,
        "--control-config",
        str(CONFIG),
        "--no-color",
    ]
    print("  " + " ".join(cmd))
    p = subprocess.run(cmd, cwd=str(MOLT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print(p.stdout)
    if p.stderr.strip():
        print("  [stderr] " + p.stderr)
    print(f"  退出码 = {p.returncode}")

    step("2) 迁徙之后：它还在说话吗")
    try:
        st = ctl.status()
        print(f"  speaking = {st.get('speaking')}")
        print(f"  connected_ids = {st.get('platforms', {}).get('connected_ids')}")
        print(f"  local_only = {st.get('platforms', {}).get('local_only')}")
        print(f"  has_drained = {st.get('platforms', {}).get('has_drained')}")
    except Exception as e:  # noqa: BLE001
        print(f"  问不出来：{type(e).__name__}: {e}")

    step("3) 账本")
    state = json.loads((ROOT / "state.json").read_text(encoding="utf-8"))
    print(f"  current_epoch = {state['fenced_state']['current_epoch']}")
    print(f"  lease = {state['fenced_state']['lease']}")
    for rec in state["generations"]:
        print(f"  generation {rec['generation']}: pushed_by={rec['pushed_by']} "
              f"target={rec.get('target_shell')} epoch={rec['epoch']} "
              f"bytes={rec['artifact_bytes']}")

    step("4) 归档内容")
    for g in sorted((ROOT / "staging").glob("gen-*.tar.gz")):
        print(f"  {g.name}  {g.stat().st_size:,} B")

    step("5) AstrBot 日志里的平台生命周期")
    log = MOLT / "tools" / "ctl-test-boot4.log"
    if log.is_file():
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        hits = [ln for ln in lines if "terminate platform" in ln or "Loading IM platform" in ln]
        for ln in hits[-10:]:
            print("  " + ln)
    else:
        print(f"  找不到 {log}")

    return 0 if p.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
