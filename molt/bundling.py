"""发布物里到底该装哪些文件 —— 一个纯函数，给打包脚本和测试共用。

## 为什么要有这个模块

2026-11-15 之前，`dist/_rebuild_bundle.py` 里是**手写文件名清单**：

    for name in ("main.py", "api.py", "command.py", "metadata.yaml"):

那一天加了 `arrival.py` / `observed.py` / `sync_api.py`，清单没跟着改。
结果是"插件装好了、功能不见了"，而且**它不报错**：AstrBot 只是 import
不到那个文件，`_register_api_if_available` 把异常吞成一条 warning。

所以规则从"我记得要拷哪几个"变成"目录里有什么就拷什么"，
并且把这条规则做成**能被测试调用**的函数 —— 否则它又会慢慢变成一份注释。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

#: 插件自己 import 自己 vendored 模块时可能用的两种写法
_FROM_MOLT = re.compile(r"^\s*from\s+(molt(?:\.\w+)*)\s+import\s+(.+)$")
_IMPORT_MOLT = re.compile(r"^\s*import\s+(molt(?:\.\w+)*)")


def plugin_files(plugin_dir: Path) -> Dict[str, Path]:
    """`plugin/` 下该发布的东西：顶层 .py + metadata.yaml + views/**。"""
    plugin_dir = Path(plugin_dir)
    out: Dict[str, Path] = {}
    for src in sorted(plugin_dir.glob("*.py")):
        out[src.name] = src
    meta = plugin_dir / "metadata.yaml"
    if meta.is_file():
        out["metadata.yaml"] = meta
    views = plugin_dir / "views"
    if views.is_dir():
        for src in sorted(views.rglob("*")):
            if src.is_file() and "__pycache__" not in src.parts:
                out[str(Path("views") / src.relative_to(views))] = src
    return out


def vendored_modules(molt_dir: Path) -> Dict[str, Path]:
    """`molt/*.py` —— 插件要自带一份，因为它够不到仓库。"""
    molt_dir = Path(molt_dir)
    return {src.name: src for src in sorted(molt_dir.glob("*.py"))}


def molt_imports(plugin_dir: Path) -> List[str]:
    """插件源码里 import 到的 `molt` 子模块名（去重排序）。

    只认 `molt` 自己的包：`import asyncio` 之类不在此列。返回值用来和
    `vendored_modules()` 对照 —— 少一个就会变成运行时 `ImportError`，
    而那在真机上只表现为"这一页打不开"。
    """
    plugin_dir = Path(plugin_dir)
    names = set()
    for src in sorted(plugin_dir.glob("*.py")):
        for line in src.read_text("utf-8").splitlines():
            match = _FROM_MOLT.match(line)
            if match:
                head, tail = match.group(1), match.group(2)
                if head == "molt":
                    for part in tail.strip("()").split(","):
                        name = part.strip().split(" ")[0]
                        if name and name != "as":
                            names.add(name)
                else:
                    names.add(head.split(".")[1])
                continue
            match = _IMPORT_MOLT.match(line)
            if match and match.group(1) != "molt":
                names.add(match.group(1).split(".")[1])
    return sorted(names)
