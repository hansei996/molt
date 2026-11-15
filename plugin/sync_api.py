"""插件配置同步的 dashboard 页面后端。

## 它为什么在插件里，而不是新加一个服务

这个页面只有一件事要做：**改这台机器上 `data/config/*.json` 的几份文件。**
那台机器上唯一的常驻进程就是 AstrBot 自己，页面也长在它的 dashboard 里。
再起一个服务，就多一个要开机自启、要单独升级、要单独鉴权的东西 —— 而它
处理的东西（插件配置）本来就归 AstrBot 管。

## 三个动作，和 CLI 是一一对应的

    GET  sync/state    账本里有哪些壳发布过配置包（外加这台壳的身份）
    POST sync/publish  把这台的配置打包传进账本
    GET  sync/preview  空跑：如果现在同步，会改哪几个（不写盘）
    POST sync/apply    真的写（必须带 confirm=true）

## 这条路径的规矩，和 CLI 完全一样

- **只同步两边都装了的插件**：只在一边有的，不装代码、不写配置、不删配置。
- **结构差太多默认跳过**（顶层键交集 < 20%），要显式 force。
- **旧文件留备份**（`*.molt-before-sync-<时间戳>`），不删。
- **`apply` 必须带 `confirm: true`**：这个参数不是礼貌，是让"误触一下"变成
  "必须再点一次"。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_VENDOR = Path(__file__).resolve().parent / "_vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    # 容器里插件目录是唯一的落脚点：`/mnt/dm-0/astrbot/_molt/pkg` 不在
    # 容器的 bind mount 里，插件要是 import 不到自己旁边那份 molt，就
    # 只能退化成"调一个不存在的命令"。
    sys.path.insert(0, str(_VENDOR))

from astrbot.api.web import error_response, json_response, request  # noqa: E402

from molt import pluginsync  # noqa: E402
from molt.client import WardenClient  # noqa: E402

logger = logging.getLogger("Molt.SyncApi")

# 页面上能看到的动作名，和 `pluginsync.SyncPlan` 的分类一一对应。
ACTION_WRITE = "write"
ACTION_SAME = "same"
ACTION_SKIP_SCHEMA = "skip_schema"
ACTION_KEEP_LOCAL = "keep_local"
ACTION_ONLY_REMOTE = "only_remote"
ACTION_ONLY_LOCAL = "only_local"


def _client(settings: Any) -> WardenClient:
    return WardenClient(settings.warden_url, settings.warden_token)


def collect_state(settings: Any, client: Any) -> Dict[str, Any]:
    """这台壳是谁、数据在哪、对面哪些壳发布过配置包。"""
    data_dir = Path(settings.data_dir)
    bundles = client.plugin_bundles() or []
    peers: List[Dict[str, Any]] = []
    for item in bundles:
        shell = str(item.get("shell") or "")
        if not shell or shell == str(settings.shell or ""):
            continue
        peers.append(
            {
                "shell": shell,
                "created_at_readable": item.get("created_at_readable") or "",
                "bytes": item.get("bytes") or 0,
                "plugins": list(item.get("plugins") or []),
            }
        )
    return {
        "shell": str(settings.shell or ""),
        "data_dir": str(data_dir),
        "data_dir_exists": data_dir.is_dir(),
        "local_plugins": pluginsync.list_plugins(data_dir),
        "local_configs": sorted(pluginsync.read_plugin_configs(data_dir, pluginsync.list_plugins(data_dir))),
        "peers": peers,
    }


def do_publish(settings: Any, client: Any) -> Dict[str, Any]:
    """把这台机器的插件配置打成包，传进账本。"""
    data_dir = Path(settings.data_dir)
    plugins = pluginsync.list_plugins(data_dir)
    payload, manifest = pluginsync.make_bundle(data_dir, plugins)
    import hashlib

    digest = hashlib.sha256(payload).hexdigest()
    client.publish_plugin_bundle(
        payload,
        shell=str(settings.shell or ""),
        sha256=digest,
        plugins=sorted(manifest.get("configs") or {}),
    )
    bundled = sorted(manifest.get("configs") or {})
    return {
        "shell": str(settings.shell or ""),
        "bytes": len(payload),
        # 「plugins」在整个 API 里一律是名字数组（list_plugin_bundles 里也是），
        # 数量另给一个 plugin_count —— 同一个字段名一会儿是列表一会儿是数字，
        # 页面就会把 undefined 印成「undefined 个插件的配置」（真出现过）。
        "plugins": bundled,
        "plugin_count": len(bundled),
        "installed": len(plugins),
    }


def do_preview(
    settings: Any, client: Any, *, source: str, force: bool = False
) -> Dict[str, Any]:
    """空跑：算出会改哪几个，一个字都不写。"""
    data_dir = Path(settings.data_dir)
    payload = client.download_plugin_bundle(source)
    manifest = pluginsync.read_bundle_manifest(payload)
    remote_configs = pluginsync.parse_bundle(payload)
    local_plugins = pluginsync.list_plugins(data_dir)
    local_configs = pluginsync.read_plugin_configs(data_dir, local_plugins)
    manifest_plugins = [str(n) for n in (manifest.get("plugins") or [])]
    plan = pluginsync.plan_sync(
        remote_plugins=manifest_plugins or sorted(remote_configs),
        local_plugins=local_plugins,
        remote_configs=remote_configs,
        local_configs=local_configs,
    )

    rows: List[Dict[str, Any]] = []
    for name in sorted(plan.sync):
        rows.append({"plugin": name, "action": ACTION_WRITE, "bytes": len(plan.sync[name])})
    for name in sorted(plan.unchanged):
        rows.append({"plugin": name, "action": ACTION_SAME})
    for name in sorted(plan.suspect):
        rows.append(
            {
                "plugin": name,
                "action": ACTION_SKIP_SCHEMA,
                "detail": "顶层键几乎不重合，多半是两边插件版本不同",
            }
        )
    for name in sorted(plan.no_remote_config):
        rows.append({"plugin": name, "action": ACTION_KEEP_LOCAL})
    for name in sorted(plan.only_remote):
        rows.append({"plugin": name, "action": ACTION_ONLY_REMOTE})
    for name in sorted(plan.only_local):
        rows.append({"plugin": name, "action": ACTION_ONLY_LOCAL})

    return {
        "source": source,
        "has_manifest": bool(manifest_plugins),
        "rows": rows,
        "counts": {
            "write": len(plan.sync),
            "same": len(plan.unchanged),
            "skip_schema": len(plan.suspect),
            "keep_local": len(plan.no_remote_config),
            "only_remote": len(plan.only_remote),
            "only_local": len(plan.only_local),
        },
        "writable": len(plan.sync) + (len(plan.suspect) if force else 0),
    }


def do_apply(
    settings: Any,
    client: Any,
    *,
    source: str,
    force: bool = False,
    only: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """真的写。`only` 给了就只动这几个插件。"""
    data_dir = Path(settings.data_dir)
    payload = client.download_plugin_bundle(source)
    manifest = pluginsync.read_bundle_manifest(payload)
    remote_configs = pluginsync.parse_bundle(payload)
    local_plugins = pluginsync.list_plugins(data_dir)
    local_configs = pluginsync.read_plugin_configs(data_dir, local_plugins)
    manifest_plugins = [str(n) for n in (manifest.get("plugins") or [])]
    plan = pluginsync.plan_sync(
        remote_plugins=manifest_plugins or sorted(remote_configs),
        local_plugins=local_plugins,
        remote_configs=remote_configs,
        local_configs=local_configs,
    )
    if only:
        wanted = set(str(n) for n in only)
        plan.sync = {k: v for k, v in plan.sync.items() if k in wanted}
        plan.suspect = [n for n in plan.suspect if n in wanted]
        plan.suspect_payloads = {
            k: v for k, v in plan.suspect_payloads.items() if k in wanted
        }
    report = pluginsync.apply_plan(data_dir, plan, dry_run=False, force=force)
    return {
        "updated": sorted(report.updated),
        "backups": len(report.backups),
        "unchanged": len(report.unchanged),
        "suspect": sorted(report.suspect),
        "only_remote": sorted(report.only_remote),
        "only_local": sorted(report.only_local),
        "source": source,
    }


class MoltSyncApi:
    """把上面四个函数挂到 dashboard 的插件 API 前缀下。"""

    def __init__(self, plugin: Any, *, client_factory: Any = None) -> None:
        self.plugin = plugin
        # 测试注入口：生产里永远是 None，于是走模块级的 `_client()`。
        # 不给测试留口子的话，每条用例都要 monkeypatch 模块全局，
        # 那会把"测试之间互不影响"变成一件靠运气的事。
        self._client_factory = client_factory

    def _make_client(self, settings: Any) -> Any:
        if self._client_factory is not None:
            return self._client_factory(settings)
        return _client(settings)

    # -- 路由 ---------------------------------------------------------------
    def _route_specs(self):
        return [
            ("/sync/state", self.state, ["GET"], "Molt：这台壳与账本里的配置包"),
            ("/sync/publish", self.publish, ["POST"], "Molt：把这台的配置打包传进账本"),
            ("/sync/preview", self.preview, ["GET"], "Molt：空跑，看会改哪几个"),
            ("/sync/apply", self.apply, ["POST"], "Molt：真的写（要 confirm）"),
        ]

    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        prefix = getattr(self.plugin, "API_PREFIX", "") or "/astrbot_plugin_molt"
        for route, handler, methods, desc in self._route_specs():
            register(f"{prefix}{route}", handler, methods, desc)

    # -- 共用 ---------------------------------------------------------------
    def _settings(self) -> Any:
        return self.plugin._mover.settings()

    def _fail(self, exc: Exception, *, status: int = 500):
        logger.warning("[Molt] 同步操作失败：%s", exc, exc_info=True)
        return error_response(str(exc), status_code=status)

    # -- 处理器 -------------------------------------------------------------
    async def state(self):
        try:
            settings = self._settings()
            client = self._make_client(settings)
            state = await asyncio.to_thread(collect_state, settings, client)
            return json_response({"success": True, **state})
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)

    async def publish(self):
        try:
            settings = self._settings()
            client = self._make_client(settings)
            result = await asyncio.to_thread(do_publish, settings, client)
            return json_response({"success": True, **result})
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)

    async def preview(self):
        source = request.query.get("from", "") or ""
        force = (request.query.get("force", "0") or "0") in ("1", "true", "True")
        if not source:
            return error_response("缺少 from（要从哪台壳同步）", status_code=400)
        try:
            settings = self._settings()
            client = self._make_client(settings)
            result = await asyncio.to_thread(
                do_preview, settings, client, source=source, force=force
            )
            return json_response({"success": True, **result})
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)

    async def apply(self):
        payload = await request.json(default={}) or {}
        source = str(payload.get("from") or "")
        if not source:
            return error_response("缺少 from（要从哪台壳同步）", status_code=400)
        if payload.get("confirm") is not True:
            # 这个参数不是礼貌：它让"误触一下"变成"必须再点一次"。
            return error_response(
                "要真的写，请带上 confirm=true（页面上是那个勾选框）", status_code=400
            )
        force = bool(payload.get("force"))
        only = payload.get("only")
        if only is not None and not isinstance(only, list):
            return error_response("only 要么不给，要么给插件名数组", status_code=400)
        try:
            settings = self._settings()
            client = self._make_client(settings)
            result = await asyncio.to_thread(
                do_apply, settings, client, source=source, force=force, only=only
            )
            return json_response({"success": True, **result})
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
