"""变异测试：`cmd_migrate` 里那道"先按住源壳"的闸，测试真的守得住吗？

## 为什么单给这一段写变异脚本

因为它守的是**公理 0**：任何时刻只能有一条鱼在线。这一段的失败模式特别恶劣——
它不会崩、不会报错，它只会让两只鱼同时回消息，而这件事在日志里**看不见**
（两边各自都正常，只是用户收到了两条回复）。

而"日志里看不见"正是变异测试存在的理由：**测试跑绿了，也可能什么都没守住。**

## 判据

每个变异都必须让**至少一个**测试失败。没有"最小失败数"这种阈值——
猜一个数字让脚本变绿，正是本项目反复踩过的坑。

## 关于"第一轮 8 个漏 2 个"

第一轮跑出 6/8，两个漏的都不是脚本写错，是**测试太松**：

  * `没给控制地址时不出声` —— 断言写的是 `"--control-url" in out`，
    而下面那行 `dim("    --control-url ...")` 也满足它。警告被删掉了测试照样绿。
  * `drain 挪到热备之后` —— 我一开始把"挪"写成了"删掉几行输出"，
    那根本不是挪。真正要表达的是"快照先拍了，然后才让它闭嘴"。

两处都改成能真正区分对错的形式之后才通过。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CLI = ROOT / "molt" / "cli.py"
PY = sys.executable

# (名字, [(原文, 变异后), ...], 说明)
MUTATIONS: List[Tuple[str, List[Tuple[str, str]], str]] = [
    (
        "快照先拍、再让它闭嘴",
        [
            (
                """            dr = control.drain_and_verify()""",
                """            be.prepare_generation_dir(be.next_generation())
            dr = control.drain_and_verify()""",
            )
        ],
        "热备目录在 drain 之前就建好了 —— 等于先把灵魂拍了照，中间那几条消息丢了。",
    ),
    (
        "声称 drain 了但其实没调",
        [
            (
                """            dr = control.drain_and_verify()""",
                """            dr = {"drained": [], "verified_silent": True}""",
            )
        ],
        "只把账面写漂亮，没真的让源壳闭嘴 —— 这是最难在日志里发现的一种。",
    ),
    (
        "热备失败时不 resume",
        [("""            _resume_source("热备或打包失败")""", """            pass""")],
        "半路失败却把源壳丢在哑掉的状态——用户看到的是'机器人突然不回话了'。",
    ),
    (
        "Fatal 分支不 resume",
        [("""        _resume_source("迁徙中止")""", """        pass""")],
        "同上，但走的是另一条 except 分支。",
    ),
    (
        "MoltError 分支不 resume",
        [("""        _resume_source("迁徙被拒")""", """        pass""")],
        "同上，第三条分支。",
    ),
    (
        "drain 失败也继续",
        [
            (
                """            dim("  交还租约也失败了，90 秒后会自动过期。")
            return 8""",
                """            dim("  交还租约也失败了，90 秒后会自动过期。")""",
            )
        ],
        "按不住源壳还往下走 —— 这是最直接的公理 0 违反。",
    ),
    (
        "没给控制地址时不出声",
        [
            (
                """        warn("没有给 --control-url：这次迁徙不会让源壳停下来。")""",
                """        pass""",
            )
        ],
        "沉默地放行：用户以为迁徙已经接管了，其实旧壳还在说话。",
    ),
    (
        "给了地址不给凭据时猜一个",
        [
            (
                """    if not token:
        raise NeedsHuman(""",
                """    if not token:
        token = "guessed-token"
        if False:
            raise NeedsHuman(""",
            )
        ],
        "凭据缺失时编一个 —— 于是失败发生在网络层，报错变得难以理解。",
    ),
    (
        "resume 失败时假装成功",
        [
            (
                """            bad(f"恢复源壳失败：{e}")
            dim("  ⚠️ **源壳现在是哑的**。在它恢复之前不要再启动任何别的壳。")
            dim("  它的 plugin 还在跑，重启那个 AstrBot 进程也会让它重新连上。")
            return""",
                """            bad(f"恢复源壳失败：{e}")
            return""",
            )
        ],
        "删掉那句'它现在是哑的'——那是唯一能让人不去启动第二个壳的提示。",
    ),
    (
        "没按过也告诉用户它哑了",
        [
            (
                """    if drained:
        # 这句话必须显眼：用户跑完一条命令之后，机器人就不回消息了。""",
                """    if True:
        # 这句话必须显眼：用户跑完一条命令之后，机器人就不回消息了。""",
            )
        ],
        "没 drain 也说'它现在是哑的' —— 用户会白等一个本来就活着的机器人。",
    ),
]


def run_tests() -> Tuple[int, str]:
    p = subprocess.run(
        [
            PY,
            "-m",
            "pytest",
            "molt/tests",
            "-q",
            "-x",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    if not CLI.is_file():
        print(f"找不到 {CLI}")
        return 2

    backup = CLI.with_suffix(".py.bak")
    shutil.copy2(CLI, backup)
    original = CLI.read_text(encoding="utf-8")

    results: List[Tuple[str, str]] = []
    try:
        for name, edits, why in MUTATIONS:
            mutated = original
            missing = False
            for old, new in edits:
                if old not in mutated:
                    missing = True
                    break
                mutated = mutated.replace(old, new, 1)
            if missing:
                results.append((name, "ANCHOR-MISSING"))
                print(f"[ANCHOR-MISSING] {name}")
                CLI.write_text(original, encoding="utf-8")
                continue

            CLI.write_text(mutated, encoding="utf-8")
            rc, _out = run_tests()
            verdict = "CAUGHT" if rc != 0 else "MISSED"
            results.append((name, verdict))
            print(f"[{verdict}] {name}" + ("" if verdict == "CAUGHT" else f"   << {why}"))
            CLI.write_text(original, encoding="utf-8")
    finally:
        CLI.write_text(original, encoding="utf-8")
        backup.unlink(missing_ok=True)

    print()
    caught = sum(1 for _, v in results if v == "CAUGHT")
    missed = [n for n, v in results if v == "MISSED"]
    anchor = [n for n, v in results if v == "ANCHOR-MISSING"]
    print(f"抓到的：{caught}/{len(results)}")
    if anchor:
        print(f"锚点没找到：{anchor}")
    if missed:
        print(f"漏掉的：{missed}")
        return 1
    if anchor:
        return 2
    print("全部抓到。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
