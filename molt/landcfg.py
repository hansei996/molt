"""落位时把搬来的插件配置写回这台壳（只写"这边也装了的插件"）。

为什么这件事要做成"落位的一部分"，而不是每次让人去点同步按钮：

    搬家时源壳上的配置，就是**当时正在生效的那份**。鱼换了身体，
    它喜欢的设置应该跟着走 —— 否则新壳会用一份更旧的配置去扮演同一个它，
    而那种不一致没有任何提示（主人 2026-11-15 在 NAS 上就撞到了这一条）。

三条边界（每一条都是从"更坏的那一侧"反推出来的）：

1. **只写这边真的装了的插件**。给一个没装的插件写配置不会报错，
   只会安静地躺着 —— 等哪天装上，它带着另一台机器的旧设置启动。
2. **结构差太多的不写**（复用 `pluginsync` 的判据）。同一个插件的两个大版本，
   顶层键可能完全不重合；覆盖过去等于把对方不认识的键塞进去。
3. **机器绑定的值保留本机的**。`astrbot_plugin_dsh_connector` 的
   `http_base_url` 指向"这台机器上的 DSH"，跟着鱼搬会指到一个不存在的地方。
   表写在代码里而不是配置里：一份会被复制的配置文件，不能决定"哪些键不该被复制"。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

from . import pluginsync
from .soul import PLUGIN_CONFIG_DIR

#: 插件名 -> 那些"属于这台机器、不属于这只鱼"的顶层键。
MACHINE_BOUND_KEYS: Dict[str, tuple] = {
    "astrbot_plugin_dsh_connector": ("http_base_url",),
}

#: 落位时留的备份后缀（与 pluginsync 的 `.molt-before-sync-*` 刻意区分：
#: 一个能看出它是"搬家带过来的"还是"人手动同步的"）。
BACKUP_MARKER = ".molt-before-land-"


def _plugin_of(filename: str) -> str:
    """`astrbot_plugin_xxx_config.json` -> `astrbot_plugin_xxx`。"""
    return filename[: -len(pluginsync.CONFIG_SUFFIX)]


def _merge_machine_bound(plugin: str, carried: bytes, local: bytes) -> bytes:
    """把本机那台机器绑定的值塞回搬来的配置里（读不出就原样返回）。"""
    keys = MACHINE_BOUND_KEYS.get(plugin)
    if not keys:
        return carried
    try:
        new = json.loads(carried.decode("utf-8-sig"))
        old = json.loads(local.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return carried
    if not isinstance(new, dict) or not isinstance(old, dict):
        return carried
    changed = False
    for key in keys:
        if key in old and new.get(key) != old[key]:
            new[key] = old[key]
            changed = True
    if not changed:
        return carried
    return json.dumps(new, ensure_ascii=False, indent=2).encode("utf-8")


def apply_carried_configs(
    gen_dir: Path,
    data_dir: Path,
    *,
    stamp: str = "",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """把 `gen_dir/config/*_config.json` 应用到 `data_dir`。

    返回 `{"applied": [...], "same": [...], "schema": [...],
    "not_installed": [...], "backups": {名字: 备份路径或 ""}}`。
    每一类都如实列出来 —— "什么都没做"和"做了但看不出来"必须能区分。
    """
    carried_dir = Path(gen_dir) / PLUGIN_CONFIG_DIR
    result: Dict[str, Any] = {
        "applied": [],
        "same": [],
        "schema": [],
        "not_installed": [],
        "backups": {},
    }
    if not carried_dir.is_dir():
        return result

    stamp = stamp or time.strftime("%Y%m%dT%H%M%S")
    target_dir = Path(data_dir) / PLUGIN_CONFIG_DIR

    for src in sorted(carried_dir.iterdir()):
        if not src.is_file() or not src.name.endswith(pluginsync.CONFIG_SUFFIX):
            continue
        plugin = _plugin_of(src.name)
        installed = (Path(data_dir) / "plugins" / plugin).is_dir()
        if not installed:
            result["not_installed"].append(plugin)
            continue

        carried = src.read_bytes()
        target = target_dir / src.name
        if target.exists():
            local = target.read_bytes()
            if local == carried:
                result["same"].append(plugin)
                continue
            if pluginsync._looks_like_a_different_schema(carried, local):
                result["schema"].append(plugin)
                continue
            carried = _merge_machine_bound(plugin, carried, local)

        if dry_run:
            result["applied"].append(plugin)
            result["backups"][plugin] = ""
            continue

        target_dir.mkdir(parents=True, exist_ok=True)
        backup = ""
        if target.exists():
            bak = target.with_name(target.name + BACKUP_MARKER + stamp)
            bak.write_bytes(target.read_bytes())
            backup = str(bak)
        target.write_bytes(carried)
        result["applied"].append(plugin)
        result["backups"][plugin] = backup

    return result
