"""变异测试：`tools/molt-homer.py` 的判据有没有牙。

判据是「有没有测试失败」，不设 min_fail 阈值——
猜一个数字让脚本变绿，正是这个项目反复踩过的坑。
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(r"C:\Users\13553\Downloads\DSH_workspace\molt")
TARGET = ROOT / "tools" / "molt-homer.py"
ORIG = TARGET.read_text(encoding="utf-8")
PY = sys.executable

MUTANTS = [
    ("不留活口：容器没停下来也照样落位",
     'if not self.do_stop():\n            self.log("  ! 容器没停下来，这一轮不落位")\n            return {"ok": False, "reason": "容器没停下来"}',
     'if not self.do_stop():\n            pass'),

    ("欠账不压过更新的一代",
     '    if state.get("needs_start"):',
     '    if False:'),

    ("不看租约持有者",
     '    if holder:\n        return Decision("wait", generation=gen,',
     '    if False:\n        return Decision("wait", generation=gen,'),

    ("不看 target_shell",
     '    if target and target != shell:',
     '    if False:'),

    ("落位失败也照样往下走",
     '        if not landed:\n            self.log("  ! 落位失败 —— 但先把它拉起来，别让它两边都不在")\n            if self.do_start():\n                state["needs_start"] = False\n                self.save_state(state)\n            return {"ok": False, "reason": "落位失败"}',
     '        if False:\n            pass'),

    ("先落位再记账（中途炸了就没欠账）",
     '        state["needs_start"] = True\n        self.save_state(state)\n\n        # 3) 落位。\n        landed = self.do_land(generation)',
     '        landed = self.do_land(generation)\n        state["needs_start"] = True\n        self.save_state(state)'),

    ("platform-switch 路径用 Path 拼（平台相关）",
     '        cfg = self.settings.data_dir.rstrip("/") + "/cmd_config.json"',
     '        cfg = str(Path(self.settings.data_dir) / "cmd_config.json")'),
]


def run_tests():
    cp = subprocess.run([PY, "-m", "pytest", "molt/tests/test_molt_homer.py", "-q"],
                        cwd=str(ROOT), capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    tail = (cp.stdout or "").strip().splitlines()
    last = tail[-1] if tail else ""
    failed = any(("failed" in ln or "error" in ln) for ln in tail[-3:])
    return failed, last


rows = []
for title, old, new in MUTANTS:
    if old not in ORIG:
        rows.append((title, "ANCHOR-MISSING", ""))
        continue
    TARGET.write_text(ORIG.replace(old, new, 1), encoding="utf-8")
    try:
        failed, last = run_tests()
    finally:
        TARGET.write_text(ORIG, encoding="utf-8")
    rows.append((title, "CAUGHT" if failed else "MISSED", last))

TARGET.write_text(ORIG, encoding="utf-8")
print("=" * 70)
for title, verdict, last in rows:
    print(f"[{verdict:12s}] {title}")
    if verdict != "CAUGHT":
        print(f"               {last}")
print("=" * 70)
caught = sum(1 for _, v, _ in rows if v == "CAUGHT")
print(f"{caught}/{len(rows)} CAUGHT")
