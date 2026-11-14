"""对控制面做变异测试 —— 确认这些测试真的能抓到错误。

规矩来自本项目前面踩过的坑：**"测试通过"不等于"测试有牙"**。
把实现改成错的、看测试是否失败。MISSED 意味着那条测试只是在走过场。
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"C:\Users\13553\Downloads\DSH_workspace\molt")
PY = r"C:\Users\13553\AppData\Local\Programs\Python\Python313\python.exe"
MAIN = ROOT / "plugin" / "main.py"
API = ROOT / "plugin" / "api.py"
TEST = "molt/tests/test_plugin_control.py"

# (说明, 目标文件, 原文, 替换成)
# 判据只有一条：**有没有测试失败**。min_fail 这种阈值是猜的，
# 而"猜一个数字让脚本变绿"正是本项目反复踩过的坑。
MUTANTS = [
    (
        "把「在 _inst_map 里」的判据反过来（webchat 会被当成对外连接）",
        MAIN,
        'is_managed = id(inst) in managed if managed else pid in configured',
        'is_managed = id(inst) not in managed if managed else pid not in configured',
    ),
    (
        "drain 直接迭代 config 而不看运行实例（回到最初的 bug）",
        MAIN,
        "        outward, _local = self._classified()",
        "        outward = [(c.get('id'), None) for c in self._enabled_configs()]",
    ),
    (
        "drain 无条件报 ok=True（Warden 会以为安全了）",
        MAIN,
        '            "ok": still == 0,',
        '            "ok": True,',
    ),
    (
        "resume 不检查 has_drained（可以被随意调用）",
        MAIN,
        "        if not self._has_drained:",
        "        if False:",
    ),
    (
        "resume 忽略 enable（会偷偷打开被护栏关掉的平台）",
        MAIN,
        '        want = [c for c in self._enabled_configs() if c.get("id") in self._drained]',
        '        want = [c for c in self._platform_configs() if c.get("id") in self._drained]',
    ),
    (
        "speaking 把所有连接都算进去（含 webchat → 恒真）",
        API,
        '            speaking=snap.get("connected", 0) > 0,',
        '            speaking=(snap.get("connected", 0) + len(snap.get("local_only") or [])) > 0,',
    ),
    (
        "local_only 不暴露（把 webchat 藏起来）",
        MAIN,
        '            "local_only": sorted(pid for pid, _ in local_only),',
        '            "local_only": [],',
    ),
    (
        "drain 不真的调 terminate_platform（只是记账）",
        MAIN,
        "                await pm.terminate_platform(pid)",
        "                pass",
    ),
    (
        "resume 不把 id 从 _drained 移除（第二次 resume 会重装一遍）",
        MAIN,
        "            if pid in self._drained:\n                self._drained.remove(pid)",
        "            pass",
    ),
    (
        "_platform_configs 只认 enable=true（回到真机踩过的那个 bug）",
        MAIN,
        '        return [item for item in (raw or []) if isinstance(item, dict)]',
        '        return [item for item in (raw or []) if isinstance(item, dict) and item.get("enable")]',
    ),
]


def run_tests() -> tuple[bool, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", TEST, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    originals = {p: p.read_text(encoding="utf-8") for p in {MAIN, API}}
    baseline_ok, baseline_out = run_tests()
    print(f"基线：{'PASS' if baseline_ok else 'FAIL'}")
    if not baseline_ok:
        print(baseline_out[-2000:])
        return 1

    caught = 0
    missed = []
    not_found = []
    try:
        for idx, (desc, target, old, new) in enumerate(MUTANTS, 1):
            original = originals[target]
            if old not in original:
                print(f"[{idx}] !! 锚点没找到，这个变异无效：{desc}")
                print(f"     目标 {target.name}，期望包含：{old.strip()[:100]}")
                not_found.append(f"{desc}（锚点无效）")
                continue

            mutated = original.replace(old, new, 1)
            target.write_text(mutated, encoding="utf-8")
            ok, out = run_tests()
            target.write_text(original, encoding="utf-8")

            if ok:
                print(f"[{idx}] MISSED  {desc}")
                missed.append(desc)
            else:
                # 一个失败就够了。数出来的个数只用于观察，不参与判定 ——
                # 猜一个阈值然后让脚本变绿，正是本项目反复踩过的坑。
                failed = sum(1 for line in out.splitlines() if line.startswith("FAILED"))
                print(f"[{idx}] CAUGHT  {desc}  （{failed} 个测试失败）")
                caught += 1
    finally:
        for p, text in originals.items():
            p.write_text(text, encoding="utf-8")

    total = len(MUTANTS) - len(not_found)
    print(f"\n结果：{caught}/{total} 被抓到；锚点无效 {len(not_found)} 个")
    for d in missed:
        print(f"  MISSED: {d}")
    return 0 if (caught == total and total > 0) else 1


if __name__ == "__main__":
    sys.exit(main())
