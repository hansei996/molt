"""变异测试：证明 `test_plugin_command.py` 里的护栏**有牙**。

这个脚本要回答的不是"测试能不能过"，而是"把护栏拆掉，测试会不会红"。
一条拆掉护栏还照样全绿的测试，等于没写——本项目在 `_mutate_plugin.py`
里就吃过这个亏（第一版 8/10，两个漏检都是真漏洞）。

判据是**有没有测试失败**，不是"失败够不够多"。
猜一个 `min_fail` 阈值只会让脚本更容易变绿，而那正是我们想避免的事。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
CMD = ROOT / "plugin" / "command.py"
MAIN = ROOT / "plugin" / "main.py"
TESTS = "molt/tests/test_plugin_command.py"

# (标签, 文件, 原文, 改成)
MUTATIONS = [
    (
        "关掉 enabled 闸：配置没开也照搬",
        CMD,
        '        if not cfg.enabled:\n            return Reply(\n                "迁徙入口没开，我不会搬。\\n"',
        '        if False:\n            return Reply(\n                "迁徙入口没开，我不会搬。\\n"',
    ),
    (
        "关掉配置完整性检查（go）",
        CMD,
        "        problems = cfg.problems()\n        if problems:\n            return Reply(\n                \"配置不完整，我搬不了：\\n· \"",
        "        problems = []\n        if problems:\n            return Reply(\n                \"配置不完整，我搬不了：\\n· \"",
    ),
    (
        "关掉『别人持租约就不提议』",
        CMD,
        '            if isinstance(lease, dict) and lease.get("shell_id"):',
        '            if False:',
    ),
    (
        "关掉提议时效检查",
        CMD,
        "        if self._expired():",
        "        if False:",
    ),
    (
        "确认后不清提议：同一个提议可以反复确认",
        CMD,
        "        was = self._proposal[1]\n        self._proposal = None",
        "        was = self._proposal[1]",
    ),
    (
        "确认后不排定执行（只说不动）",
        CMD,
        "        self._pending = plan",
        "        self._pending = None",
    ),
    (
        "计划里省掉 --control-url：不会先闭嘴",
        CMD,
        '        "--control-url",\n        cfg.control_url,',
        "",
    ),
    (
        "计划里省掉 --warden-token",
        CMD,
        '        "--warden-token",\n        cfg.warden_token,',
        "",
    ),
    (
        "take_pending 不清空：同一次迁徙会被反复执行",
        CMD,
        "        plan = self._pending\n        self._pending = None\n        return plan",
        "        plan = self._pending\n        return plan",
    ),
    (
        "参数归一化改成包含匹配：'我不想回家' 也会搬",
        CMD,
        "    first = token.split()[0]\n    if first in GO_WORDS:",
        "    first = token.split()[0]\n    if any(w in token for w in GO_WORDS):",
    ),
    (
        "after_message_sent 不看计划就开跑",
        MAIN,
        "        plan = self._mover.take_pending()\n        if plan is None:\n            return",
        "        plan = self._mover.take_pending()",
    ),
    (
        "command_args 不剥指令名",
        MAIN,
        "    if lowered.startswith(\"molt\"):\n        text = text[4:].strip()",
        "",
    ),
]


def run_tests() -> tuple[int, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", TESTS, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    result: list[tuple[str, bool, str]] = []
    for label, path, old, new in MUTATIONS:
        original = path.read_text(encoding="utf-8")
        if old not in original:
            result.append((label, False, "锚点没找到（变异脚本自己错了）"))
            continue
        mutated = original.replace(old, new, 1)
        path.write_text(mutated, encoding="utf-8")
        try:
            code, out = run_tests()
        finally:
            path.write_text(original, encoding="utf-8")
        caught = code != 0
        last = ""
        for line in reversed(out.strip().splitlines()):
            if "passed" in line or "failed" in line or "error" in line:
                last = line.strip()
                break
        result.append((label, caught, last))

    print()
    missed = 0
    for label, caught, note in result:
        if caught:
            print(f"  [CAUGHT] {label}   ({note})")
        else:
            missed += 1
            print(f"  [MISSED] {label}   ({note})")
    total = len(result)
    print(f"\n{total - missed}/{total} CAUGHT")
    return 0 if missed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
