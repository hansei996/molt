"""两份"演练"脚本的安全形状：能被重跑，而且**不许碰不该碰的东西**。

为什么值得一条测试
------------------
这两个脚本会在真机上杀进程、发 HTTP。它们的价值全在"可以重跑"，
所以最容易被将来某次改动改坏的地方不是逻辑，而是**边界**：
有人为了让演练"更完整"，往里加一句关机、加一条防火墙规则、或者把
`--yes` 闸拆掉 —— 那就不再是演练了。

这里钉三件事：

1. `--plan` 必须能跑（默认不动手，rc=0，并且真的打印了步骤）；
2. `drill-dsh-chain.py --help` 必须能跑（参数被改坏时立刻红）；
3. 两份源码里**不许出现**关机/电源/防火墙写操作的痕迹 —— 它们只读、只在
   本机动手，且动手前必须 `--yes`。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEATH = ROOT / "tools" / "drill-agent-death.py"
CHAIN = ROOT / "tools" / "drill-dsh-chain.py"

# 这些词出现在脚本里就意味着它越界了。注意 `shutdown` 例外说明：
# 我们连"字符串"都不想让它出现 —— 因为下一个改它的人会顺着字符串去接线。
FORBIDDEN = (
    "SetSuspendState",
    "shutdown /s",
    "shutdown.exe",
    "New-NetFirewallRule",
    "Remove-NetFirewallRule",
    "powercfg",
)


def _run(args: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(ROOT),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def test_the_death_drill_prints_its_plan_and_touches_nothing() -> None:
    got = _run([str(DEATH), "--plan"])
    assert got.returncode == 0, got.stderr
    out = got.stdout + got.stderr
    assert "杀掉本机 agent" in out
    assert "--yes" in out, "plan 必须告诉人怎么真跑"


def test_the_chain_drill_has_a_usable_cli() -> None:
    got = _run([str(CHAIN), "--help"])
    assert got.returncode == 0, got.stderr
    out = got.stdout + got.stderr
    assert "--warden-url" in out and "--warden-token" in out


def test_neither_drill_reaches_for_power_or_firewall() -> None:
    for path in (DEATH, CHAIN):
        source = path.read_text(encoding="utf-8")
        for word in FORBIDDEN:
            assert word not in source, f"{path.name} 里出现了 {word!r} —— 演练不许碰电源/防火墙"


def test_the_death_drill_requires_an_explicit_yes() -> None:
    source = DEATH.read_text(encoding="utf-8")
    # 默认走 plan；只有 args.yes 才进 run()
    assert "if not args.yes:" in source
    # 而且它拉起的必须是 Startup 里的 vbs（和真人开机时同一条路），不是另一套拉起逻辑
    assert "molt-agent.vbs" in source
