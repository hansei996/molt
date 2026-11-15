"""插件配置的多端同步。

## 这条规则是主人定的，不是我们猜的

> 如果有相同插件才同步，如果某个插件只有某一段独有，就不能同步。

这句话把一个很容易做错的功能钉住了。最省事的实现是"把源壳的
`config/` 整个搬过去"，而它会造成三件坏事：

1. **给不存在的插件送配置。** 目标壳上没有那个插件，却多出一份
   `xxx_config.json`。它不会报错，它会安静地躺着；等哪天有人把那个
   插件装上，它会带着**另一台机器**的配置启动。
2. **删掉目标壳独有的插件的配置。** "同步"一旦被理解成"让两边一样"，
   销毁就成了其中一步。
3. **把机器绑定的值带过去。** 比如 DSH 连接器的 `http_base_url`：
   Windows 上是 3082，NAS 上根本没有这个插件。带过去只会让人查网卡。

所以这里只做一件事：**两边都有的插件，配置跟着鱼走；只有一边有的，
两边都不动。** 没有第二种模式。

## 为什么只同步配置，不同步插件代码

代码属于"这个壳装了什么"，配置属于"这只鱼喜欢什么"。混在一起同步，
会让一次配置同步变成一次软件部署 —— 而部署失败与配置同步失败，
需要的是完全不同的两种应对。

## 为什么默认是空跑

`molt sync-plugins` 不带 `--apply` 时只打印"会改哪些文件"。这个功能
第一次跑的时候，人不可能已经知道结果对不对；先让人看见，再让它动手。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .errors import NeedsHuman

#: `data/` 下三个与之相关的目录名。
PLUGINS_DIRNAME = "plugins"
CONFIG_DIRNAME = "config"
PLUGIN_DATA_DIRNAME = "plugin_data"

#: AstrBot 给插件生成的配置文件命名：`<插件目录名>_config.json`。
CONFIG_SUFFIX = "_config.json"

#: 本机自己的备份后缀 —— 这类文件永远不参与同步（它是本机的历史，不是配置）。
LOCAL_BACKUP_MARKERS = (".bak", ".molt-before-sync", ".molt-part")
# 清单本身也进包：它记着"那边装了哪些插件"，而不只是"那边哪些插件有配置"。
# 没有它，接收方只能看到少一个配置，分不清是没装还是装了没配。
MANIFEST_NAME = "manifest.json"
#: 结构差太多时默认不动手。理由见 `_looks_like_a_different_schema`。
SCHEMA_OVERLAP_FLOOR = 0.2


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def list_plugins(data_dir: Path) -> List[str]:
    """列出这个壳上装了哪些插件（`data/plugins/` 下的目录名）。

    只认目录，且跳过隐藏目录与 `__pycache__` —— 后者是编译产物，
    它出现在这里只会让"共有插件"这个集合莫名其妙地变大。
    """
    d = Path(data_dir) / PLUGINS_DIRNAME
    if not d.is_dir():
        return []
    out: List[str] = []
    for p in sorted(d.iterdir()):
        if not p.is_dir():
            continue
        name = p.name
        if name.startswith(".") or name == "__pycache__":
            continue
        out.append(name)
    return out


def is_local_noise(filename: str) -> bool:
    """本机产生的文件（备份、半成品）不参与同步。"""
    return any(marker in filename for marker in LOCAL_BACKUP_MARKERS)


def read_plugin_configs(data_dir: Path, plugins: List[str]) -> Dict[str, bytes]:
    """读出这些插件的配置**原始字节**。

    读字节而不是读 JSON：写回去的时候要一字不差地还原（包括 BOM 和缩进）。
    我们这个工具没有资格顺手重新格式化别人的配置。
    """
    d = Path(data_dir) / CONFIG_DIRNAME
    out: Dict[str, bytes] = {}
    for plugin in plugins:
        p = d / f"{plugin}{CONFIG_SUFFIX}"
        if not p.is_file():
            continue
        try:
            out[plugin] = p.read_bytes()
        except OSError:
            continue
    return out


def _top_level_keys(raw: bytes) -> Optional[set]:
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    return set(value.keys())


def _looks_like_a_different_schema(remote: bytes, local: bytes) -> bool:
    """两份配置看起来是不是同一个插件/同一个版本写出来的。

    这不是安全检查，是**停下来让人看一眼**的机制。真实世界里见过一次：
    同一个 `private_companion`，NAS 上 15 个顶层键（嵌套老结构），
    Windows 上 1008 个（扁平新结构）。当时如果无条件覆盖，就是把
    另一台机器上插件根本不认识的配置塞给它。

    判据只用"顶层键的重合比例"，因为它在"结构变了"时下降得最明显，
    而对"只改了几个值"完全不敏感 —— 后者恰恰是我们想同步的。
    读不出 JSON 的一律当作可疑（我们看不懂的东西不覆盖）。
    """
    a, b = _top_level_keys(remote), _top_level_keys(local)
    if a is None or b is None:
        return True
    if not a or not b:
        return False
    bigger = max(len(a), len(b))
    if bigger < 5:
        # 键太少时比例没有意义（两个只有 2 个键的配置很容易完全不同却同构）。
        return False
    return len(a & b) / bigger < SCHEMA_OVERLAP_FLOOR


@dataclass
class SyncPlan:
    """"哪些同步、哪些不动"的完整答案。**它不执行任何写操作。**"""

    #: 两边都有、且值得写过去的插件 -> 远端那份配置的字节
    sync: Dict[str, bytes] = field(default_factory=dict)
    #: 两边都有、但内容已经一样
    unchanged: List[str] = field(default_factory=list)
    #: 两边都有、但结构差太多 —— 默认不动，除非 `--force`
    suspect: List[str] = field(default_factory=list)
    #: 上面那些可疑项的远端字节（只在 `--force` 时用得上）
    suspect_payloads: Dict[str, bytes] = field(default_factory=dict)
    #: 只在远端有：不装、不写配置
    only_remote: List[str] = field(default_factory=list)
    #: 只在本地有：不删、不覆盖
    only_local: List[str] = field(default_factory=list)
    #: 两边都有但远端没有配置文件（没什么可同步的）
    no_remote_config: List[str] = field(default_factory=list)

    @property
    def common(self) -> List[str]:
        return sorted(set(self.sync) | set(self.unchanged) | set(self.suspect) | set(self.no_remote_config))


def plan_sync(
    *,
    remote_plugins: List[str],
    local_plugins: List[str],
    remote_configs: Dict[str, bytes],
    local_configs: Dict[str, bytes],
) -> SyncPlan:
    """算出"该怎么同步"，**不碰磁盘**。

    这是整个功能里唯一需要被反复测试的部分：它决定了什么东西会
    离开自己的机器、什么东西会覆盖别人的文件。所有 I/O 都在
    `apply_plan()` 里，且都以这个结果为准。
    """
    remote = set(remote_plugins)
    local = set(local_plugins)
    plan = SyncPlan()

    # 主人定的那条规则，在这里只有一行：
    plan.only_remote = sorted(remote - local)
    plan.only_local = sorted(local - remote)

    for plugin in sorted(remote & local):
        incoming = remote_configs.get(plugin)
        if incoming is None:
            plan.no_remote_config.append(plugin)
            continue
        current = local_configs.get(plugin)
        if current is not None and current == incoming:
            plan.unchanged.append(plugin)
            continue
        if current is not None and _looks_like_a_different_schema(incoming, current):
            plan.suspect.append(plugin)
            plan.suspect_payloads[plugin] = incoming
            continue
        plan.sync[plugin] = incoming

    return plan


@dataclass
class SyncReport:
    """一次同步的结果。

    `updated` 与 `would_update` 是两个字段，不是一个字段的两种读法 ——
    空跑时 `updated` **必须是空的**：一个叫 updated 的列表在什么都没写
    的时候有内容，就是又一个"看起来像成功"的东西。
    """

    updated: List[str] = field(default_factory=list)
    would_update: List[str] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    suspect: List[str] = field(default_factory=list)
    only_remote: List[str] = field(default_factory=list)
    only_local: List[str] = field(default_factory=list)
    no_remote_config: List[str] = field(default_factory=list)
    backups: Dict[str, str] = field(default_factory=dict)
    dry_run: bool = True

    @property
    def changed(self) -> bool:
        return bool(self.updated)


def apply_plan(
    data_dir: Path,
    plan: SyncPlan,
    *,
    dry_run: bool = True,
    force: bool = False,
) -> SyncReport:
    """按 `plan_sync()` 的结论写文件。

    - 只写 `plan.sync` 里那些（`suspect` 只有 `force=True` 才写）
    - 写之前把原文件改名成 `<name>.molt-before-sync-<时间戳>`，**不删**
    - 先写 `.molt-part` 再 `os.replace`：中途断电只会留下一个明显的半成品
    """
    data_dir = Path(data_dir)
    cfg_dir = data_dir / CONFIG_DIRNAME
    report = SyncReport(
        unchanged=list(plan.unchanged),
        suspect=list(plan.suspect),
        only_remote=list(plan.only_remote),
        only_local=list(plan.only_local),
        no_remote_config=list(plan.no_remote_config),
        dry_run=dry_run,
    )

    items: Dict[str, bytes] = dict(plan.sync)
    if force:
        # 强制模式下才写"结构看起来不对"的那些 —— 而且它们仍然会先备份。
        for plugin in plan.suspect:
            payload = plan.suspect_payloads.get(plugin)
            if payload is not None:
                items[plugin] = payload

    for plugin, payload in sorted(items.items()):
        dst = cfg_dir / f"{plugin}{CONFIG_SUFFIX}"
        if dry_run:
            report.would_update.append(plugin)
            continue

        cfg_dir.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            stamp = time.strftime("%Y%m%dT%H%M%S")
            bak = dst.with_name(dst.name + f".molt-before-sync-{stamp}")
            try:
                os.replace(dst, bak)
                report.backups[plugin] = str(bak)
            except OSError:  # pragma: no cover - 备份失败就不要覆盖
                raise NeedsHuman(
                    f"旧配置改名失败，已中止，没有覆盖 {dst.name}。\n"
                    "  同步不该以「删掉一份还能用的配置」为前提。"
                )
        part = dst.with_name(dst.name + ".molt-part")
        try:
            part.write_bytes(payload)
            os.replace(part, dst)
            report.updated.append(plugin)
        finally:
            if part.exists():  # pragma: no cover
                try:
                    part.unlink()
                except OSError:
                    pass

    return report


# ---------------------------------------------------------------------------
# 打包（上传给账本的那一份）
# ---------------------------------------------------------------------------


def make_bundle(data_dir: Path, plugins: List[str]) -> Tuple[bytes, Dict[str, Any]]:
    """把插件的配置打成一个内存里的 tar.gz，并返回 manifest。

    只装 `config/<插件>_config.json` 这一种文件。**不装插件代码、
    不装 `plugin_data/`、不装本机备份**：这个包的语义是"这只鱼的偏好"，
    不是"这个壳的软件状态"。
    """
    configs = read_plugin_configs(data_dir, plugins)
    manifest_configs: Dict[str, Dict[str, Any]] = {}
    for plugin, payload in sorted(configs.items()):
        manifest_configs[plugin] = {"sha256": _sha256(payload), "bytes": len(payload)}

    manifest = {
        "plugins": sorted(plugins),
        "configs": manifest_configs,
        "bundled_at": time.time(),
        "bundled_at_readable": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 清单本身也进包：接收方据此区分「那边没装这个插件」和
    # 「那边装了但没配置过」—— 这两件事对应完全不同的处理，而只看
    # 配置文件列表是分不出来的（都会表现为"少一个配置"）。
    body = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tf:
        for plugin, payload in sorted(configs.items()):
            arcname = f"{CONFIG_DIRNAME}/{plugin}{CONFIG_SUFFIX}"
            info = tarfile.TarInfo(arcname)
            info.size = len(payload)
            info.mtime = int(time.time())
            tf.addfile(info, io.BytesIO(payload))
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(body)
        info.mtime = int(time.time())
        tf.addfile(info, io.BytesIO(body))
    return buf.getvalue(), manifest


def read_bundle_manifest(payload: bytes, *, max_bytes: int = 32 << 20) -> Dict[str, Any]:
    """读包里的 `manifest.json`；没有或读不动就返回 `{}`。

    返回 `{}` 而不是报错，是因为**旧版本打的包没有这个成员** ——
    那不叫损坏，只叫更少的信息。调用方按"不知道那边装了哪些插件"
    来处理（退化成只比配置文件），而不是让整次同步失败。
    """
    if len(payload) > max_bytes:
        return {}
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tf:
            for member in tf.getmembers():
                if member.name != MANIFEST_NAME or not member.isfile():
                    continue
                fh = tf.extractfile(member)
                if fh is None:  # pragma: no cover
                    return {}
                data = json.loads(fh.read().decode("utf-8"))
                return data if isinstance(data, dict) else {}
    except (tarfile.TarError, OSError, ValueError, UnicodeDecodeError):
        return {}
    return {}


def parse_bundle(payload: bytes, *, max_bytes: int = 32 << 20) -> Dict[str, bytes]:
    """把收到的 tar.gz 解开，返回 `{插件名: 配置字节}`。

    防的是"对方给了什么就信什么"：只接受 `config/<名字>_config.json`
    这一种成员，其它一律忽略（包括 `../`、绝对路径、符号链接 —— 那条
    路 `transfer.py` 已经踩过，这里不重复实现，直接按白名单取名字）。
    """
    if len(payload) > max_bytes:
        raise NeedsHuman(f"插件配置包太大：{len(payload)} 字节（上限 {max_bytes}）")
    out: Dict[str, bytes] = {}
    pattern = re.compile(r"^" + CONFIG_DIRNAME + r"/([A-Za-z0-9._-]+)" + re.escape(CONFIG_SUFFIX) + r"$")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            m = pattern.match(member.name)
            if m is None:
                continue
            plugin = m.group(1)
            if plugin.startswith(".") or is_local_noise(plugin):
                continue
            fh = tf.extractfile(member)
            if fh is None:  # pragma: no cover
                continue
            out[plugin] = fh.read()
    return out
