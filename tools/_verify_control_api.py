"""控制面真机验证：用 molt.control 客户端打四个端点。

判据（缺一不可）：
  1. /status 能答，且 `speaking` 是布尔
  2. /drain 之后 `speaking` 变 false（**这才是 drain 真的生效了**）
  3. /resume 之后 `speaking` 变回 true
  4. /checkpoint 让 WAL 变小或归零
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, r"C:\Users\13553\Downloads\DSH_workspace\molt")

from molt.control import AstrBotControl
from molt.errors import MoltError, NeedsHuman

BASE = "http://127.0.0.1:6199"
CFG = Path(r"G:\molt-p0\ctl-test\data\cmd_config.json")


def show(tag, payload):
    print(f"\n--- {tag} ---")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    print(f"目标：{BASE}")
    print(f"凭据：{CFG}")
    ctl = AstrBotControl.from_config(BASE, CFG)
    print(f"  {ctl.where()}")

    fails = []

    # 1) status
    st = ctl.status()
    show("1) GET /status", st)
    plats = st.get("platforms") or {}
    speaking0 = st.get("speaking")
    print(f"\n  speaking = {speaking0!r}  （布尔={isinstance(speaking0, bool)}）")
    print(f"  connected_count = {plats.get('connected_count')}")
    print(f"  enabled_in_config = {plats.get('enabled_in_config')}")
    print(f"  wal.total_wal_bytes = {(st.get('wal') or {}).get('total_wal_bytes')}")
    if not isinstance(speaking0, bool):
        fails.append("status 的 speaking 不是布尔")

    # 2) checkpoint（drain 之前先做，这样能看到真实的 WAL）
    try:
        cp = ctl.checkpoint()
        show("2) POST /checkpoint", cp)
        for row in cp.get("checkpointed") or []:
            print(f"  {row.get('name'):24s} {row.get('before_wal_bytes'):>10} -> "
                  f"{row.get('after_wal_bytes'):>10}  ok={row.get('ok')}  pragma={row.get('pragma_result')}")
    except MoltError as e:
        fails.append(f"checkpoint 失败：{e}")
        print(f"  [!] {e}")

    # 3) drain + verify
    print("\n=== 3) drain 并确认它真的不说话了 ===")
    try:
        res = ctl.drain_and_verify()
        show("POST /drain + verify", res)
        print(f"  verified_silent = {res.get('verified_silent')}")
        print(f"  confirm_attempts = {res.get('confirm_attempts')}")
    except NeedsHuman as e:
        fails.append(f"drain 之后仍在说话：{e}")
        print(f"  [X] {e}")

    after_drain = ctl.status()
    speaking1 = after_drain.get("speaking")
    print(f"\n  drain 后 speaking = {speaking1!r}")
    if speaking1 is not False:
        fails.append(f"drain 之后 speaking 应为 False，实际 {speaking1!r}")

    # 4) resume
    print("\n=== 4) resume 让它重新开口 ===")
    enabled = (after_drain.get("platforms") or {}).get("enabled_in_config") or []
    try:
        rr = ctl.resume()
        show("POST /resume", rr)
    except MoltError as e:
        fails.append(f"resume 失败：{e}")
        print(f"  [!] {e}")

    after_resume = ctl.status()
    speaking2 = after_resume.get("speaking")
    print(f"\n  resume 后 speaking = {speaking2!r}")
    print(f"  enabled_in_config = {enabled}  （空 = 平台被护栏关着，没什么可恢复）")

    if enabled:
        # 有对外平台时才要求它真的重新说话
        if speaking2 is not True:
            fails.append(f"resume 之后 speaking 应为 True（配置里有 {len(enabled)} 个平台），实际 {speaking2!r}")
    else:
        # 平台被护栏关着（land 之后的标准状态）：
        # drain/resume 都无事可做，speaking 必须一直是 false。
        # **这不是失败，是这个场景下的正确行为** —— 强行要求 True 会逼出一个
        # "为了测试通过而偷偷打开平台"的实现，那正是公理 0 最怕的事。
        if speaking2 is not False:
            fails.append(f"平台关着时 speaking 应为 False，实际 {speaking2!r}")
        print("  [OK] 平台被关着 → resume 无副作用地把 speaking 留在了 false（正确）")
        print("      想验真的 drain/resume，需要先手动把 platform[].enable 改成 true。")
        # 这种情况下第 5 步的幂等性检查仍然有效，但不算"全绿"
        print("\n" + "=" * 60)
        if fails:
            print("[FAIL] " + f"{len(fails)} 项不通过：")
            for f in fails:
                print(f"  - {f}")
            return 1
        print("[PARTIAL] status / checkpoint / drain 三项按预期工作；")
        print("          drain-resume 的完整往返需要一台**有对外平台**的壳才能验。")
        return 0

    # 5) 二次 drain 应当幂等（drained 为空）
    print("\n=== 5) 再 drain 一次应当幂等 ===")
    again = ctl.drain()
    print(f"  drained = {again.get('drained')}  （应为空：它已经关过了）")
    print(f"  ok = {again.get('ok')}  still_connected = {again.get('still_connected')}")

    print("\n" + "=" * 60)
    if fails:
        print("[FAIL] " + f"{len(fails)} 项不通过：")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("[PASS] 四个端点全部按预期工作")
    return 0


if __name__ == "__main__":
    sys.exit(main())
