"""插件必须能在"宿主机上没装 molt"的机器上自己站起来。

## 这条测试是为哪一次事故写的

2026-11-17 09:01，`morning-home` 真的把鱼从 Windows 搬回了 NAS（账本第 38 代
`consumed_by=ugreen-776b-8b7896df`、`consumed_at 09:01:17`），但 NAS 容器里的
插件在 `__init__` 里抛了：

    ModuleNotFoundError: No module named 'molt'

于是 `self._mover = None`，所有 `/molt` 指令退化成「迁徙指令层没装成功，看日志」，
落地播报的回执也写不进账本 —— 最终 `power_decision` 拿不到回执，Windows 没被关。
**鱼搬过去了，但它在新壳上没有手。**

根因很朴素：插件把 `molt` 自带在 `_vendor/molt/`，可没有任何一处把它放进
`sys.path`。仓库里跑测试能 import，是因为仓库根本来就在路径上；容器里没有。

所以这里不复述"代码里有那四行"，而是**搭一个和线上一样的目录**（插件 + `_vendor`），
在干净的子进程里 `import command` —— 只要有人再把这四行删掉，这条测试就会红。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _build_plugin_layout(tmp_path: Path) -> Path:
    """按 `molt.bundling` 的规则摊出一份真实的插件目录。"""
    sys.path.insert(0, str(REPO))
    from molt.bundling import plugin_files, vendored_modules  # noqa: PLC0415

    target = tmp_path / "astrbot_plugin_molt"
    (target / "_vendor" / "molt").mkdir(parents=True)
    for name, src in plugin_files(REPO / "plugin").items():
        dst = target / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for name, src in vendored_modules(REPO / "molt").items():
        shutil.copy2(src, target / "_vendor" / "molt" / name)
    return target


def _probe(tmp_path: Path, module_file: Path) -> subprocess.CompletedProcess:
    """在一个"只有插件目录"的世界里加载它。"""
    code = (
        "import importlib.util as u, sys\n"
        f"spec = u.spec_from_file_location('probe', r'{module_file}')\n"
        "mod = u.module_from_spec(spec)\n"
        "sys.modules['probe'] = mod\n"
        "spec.loader.exec_module(mod)\n"
        "print('OK')\n"
    )
    env = {"PATH": "", "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows")}
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_command_layer_imports_with_only_the_plugin_directory(tmp_path: Path) -> None:
    """`command.py` 必须自己找到 `_vendor/molt`，不靠仓库根、不靠 pip 装包。"""
    target = _build_plugin_layout(tmp_path)
    got = _probe(tmp_path, target / "command.py")
    assert got.returncode == 0, got.stderr[-2000:]
    assert "OK" in got.stdout


def test_the_bootstrap_never_depends_on_cwd(tmp_path: Path) -> None:
    """路径按 `__file__` 算，不按 cwd —— 容器里的 cwd 是 `/AstrBot`。"""
    target = _build_plugin_layout(tmp_path)
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    got = _probe(elsewhere, target / "command.py")
    assert got.returncode == 0, got.stderr[-2000:]


def test_both_plugin_entry_points_carry_the_bootstrap() -> None:
    """`main.py` 与 `command.py` 都得有 —— 它们都会在 import 期碰 `molt`。"""
    for name in ("main.py", "command.py"):
        text = (REPO / "plugin" / name).read_text(encoding="utf-8")
        assert '_VENDOR_DIR = Path(__file__).resolve().parent / "_vendor"' in text, name
        assert "sys.path.insert(0, str(_VENDOR_DIR))" in text, name
