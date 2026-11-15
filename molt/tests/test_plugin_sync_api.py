"""dashboard 同步页面的后端（`plugin/sync_api.py`）。

## 这一组测试盯的是什么

页面本身是 HTML，测不了也不该测；**能被测错的是它背后那四件事**：

1. 发布时只打包"两边都可能有"的东西 —— 配置，不含代码、不含 plugin_data；
2. 预览是空跑（一个字都不写），而且分类和 CLI 一致；
3. 真的写必须带 `confirm=true` —— 这个参数是让"误触"变成"必须再点一次"；
4. `only` 能限定只写哪几个插件，写错类型要 400 而不是静默忽略。

后端和 CLI 共用 `molt.pluginsync`，所以这里刻意**只测它自己的接线**：
如果哪天有人在这份代码里重新实现一遍"怎么算同步"，测试会立刻红。
"""

from __future__ import annotations

import asyncio
import json
import tarfile
import types
from pathlib import Path

import pytest

from molt import pluginsync

sync_api = pytest.importorskip("astrbot_plugin_molt.sync_api")
web = pytest.importorskip("astrbot.api.web")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class FakeSettings:
    def __init__(self, data_dir: Path, shell: str = "desk"):
        self.data_dir = str(data_dir)
        self.shell = shell
        self.warden_url = "http://ledger"
        self.warden_token = "token"


class FakeClient:
    """只实现 sync_api 用到的那几个方法。"""

    def __init__(self, bundles=None):
        self._bundles = list(bundles or [])  # [(shell, payload, plugins)]
        self.published = []
        self.downloaded = []

    def plugin_bundles(self):
        out = []
        for shell, payload, plugins in self._bundles:
            out.append(
                {
                    "shell": shell,
                    "bytes": len(payload),
                    "plugins": list(plugins),
                    "created_at_readable": f"{shell} 刚刚",
                }
            )
        return out

    def download_plugin_bundle(self, shell):
        self.downloaded.append(shell)
        for name, payload, _ in self._bundles:
            if name == shell:
                return payload
        raise AssertionError(f"测试里没有 {shell} 的包")

    def publish_plugin_bundle(self, payload, *, shell, sha256="", plugins=None):
        self.published.append(
            {"shell": shell, "payload": payload, "sha256": sha256, "plugins": list(plugins or [])}
        )
        return {"shell": shell, "bytes": len(payload)}


def _write_plugin(root: Path, name: str, config: dict | None) -> None:
    (root / "plugins" / name).mkdir(parents=True, exist_ok=True)
    (root / "plugins" / name / "main.py").write_text("# stub\n", encoding="utf-8")
    if config is not None:
        (root / "config").mkdir(exist_ok=True)
        (root / f"config/{name}_config.json").write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8"
        )


def _bundle_for(plugins: dict[str, dict | None]) -> bytes:
    """用 pluginsync 自己打一个包（走真实打包路径，不是手搓 tar）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name, config in plugins.items():
            _write_plugin(root, name, config)
        payload, _ = pluginsync.make_bundle(root, pluginsync.list_plugins(root))
        return payload


def _local_dir(tmp_path: Path) -> Path:
    root = tmp_path / "local"
    _write_plugin(root, "alpha", {"n": 1})
    _write_plugin(root, "beta", {"same": True})
    _write_plugin(root, "only_here", {"x": 1})
    return root


class FakePlugin:
    def __init__(self, settings):
        self._mover = types.SimpleNamespace(settings=lambda: settings)
        self.context = types.SimpleNamespace(register_web_api=lambda *a, **k: None)
        self.API_PREFIX = "/astrbot_plugin_molt"

    def register(self):
        self._calls = []

        def register(route, handler, methods, desc):
            self._calls.append((route, methods, desc))

        self.context = types.SimpleNamespace(register_web_api=register)
        return self._calls


# ---------------------------------------------------------------------------
# 发布
# ---------------------------------------------------------------------------


def test_publish_sends_configs_and_their_names(tmp_path):
    root = _local_dir(tmp_path)
    client = FakeClient()
    result = sync_api.do_publish(FakeSettings(root), client)

    assert result["plugins"] == 3
    assert result["installed"] == 3
    assert len(client.published) == 1
    sent = client.published[0]
    assert sent["shell"] == "desk"
    assert len(sent["sha256"]) == 64
    # 包里只有配置 + 清单
    names = set(pluginsync.parse_bundle(sent["payload"]))
    assert names == {"alpha", "beta", "only_here"}
    manifest = pluginsync.read_bundle_manifest(sent["payload"])
    assert set(manifest["plugins"]) == {"alpha", "beta", "only_here"}


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


def test_state_excludes_this_shell_from_peers(tmp_path):
    root = _local_dir(tmp_path)
    payload = _bundle_for({"alpha": {"n": 2}})
    client = FakeClient([("desk", payload, ["alpha"]), ("nas", payload, ["alpha"])])

    state = sync_api.collect_state(FakeSettings(root, shell="desk"), client)

    assert state["shell"] == "desk"
    assert [p["shell"] for p in state["peers"]] == ["nas"]


# ---------------------------------------------------------------------------
# 预览
# ---------------------------------------------------------------------------


def test_preview_classifies_and_writes_nothing(tmp_path):
    root = _local_dir(tmp_path)
    before = {p.name: p.read_bytes() for p in (root / "config").glob("*.json")}
    payload = _bundle_for({"alpha": {"n": 999}, "only_there": {"y": 2}})
    client = FakeClient([("nas", payload, ["alpha", "only_there"])])

    result = sync_api.do_preview(FakeSettings(root), client, source="nas")

    rows = {row["plugin"]: row["action"] for row in result["rows"]}
    assert rows["alpha"] == "write"
    assert rows["only_there"] == "only_remote"
    assert rows["only_here"] == "only_local"
    # 一个字都没写
    after = {p.name: p.read_bytes() for p in (root / "config").glob("*.json")}
    assert after == before


def test_preview_says_when_the_bundle_has_no_manifest(tmp_path):
    root = _local_dir(tmp_path)
    payload = _tar_without_manifest({"alpha": {"n": 5}})
    client = FakeClient([("nas", payload, ["alpha"])])

    result = sync_api.do_preview(FakeSettings(root), client, source="nas")

    assert result["has_manifest"] is False
    rows = {row["plugin"]: row["action"] for row in result["rows"]}
    assert rows["alpha"] == "write"


def _tar_without_manifest(configs: dict[str, dict]) -> bytes:
    """造一个"老版本的包"：没有清单成员。"""
    import io

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        for name, payload in configs.items():
            body = json.dumps(payload).encode("utf-8")
            info = tarfile.TarInfo(f"config/{name}_config.json")
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 应用（含 confirm 闸）
# ---------------------------------------------------------------------------


def _api(tmp_path: Path, client: FakeClient, shell: str = "desk"):
    settings = FakeSettings(tmp_path / "local", shell=shell)
    plugin = FakePlugin(settings)
    # `client_factory` 是类自己的注入口 —— 不去改模块全局，
    # 所以一条用例失败了也不会把下一条带歪。
    return sync_api.MoltSyncApi(plugin, client_factory=lambda _settings: client)


def test_apply_without_confirm_is_refused_and_writes_nothing(tmp_path):
    root = _local_dir(tmp_path)
    client = FakeClient([("nas", _bundle_for({"alpha": {"n": 999}}), ["alpha"])])
    api = _api(tmp_path, client)
    api.plugin._mover = types.SimpleNamespace(settings=lambda: FakeSettings(root))

    web.request._json = {"from": "nas"}
    response = asyncio.run(api.apply())

    assert response.status_code == 400
    assert "confirm" in response.json["message"]
    assert json.loads((root / "config/alpha_config.json").read_text("utf-8")) == {"n": 1}


def test_apply_with_confirm_writes_and_keeps_a_backup(tmp_path):
    root = _local_dir(tmp_path)
    client = FakeClient([("nas", _bundle_for({"alpha": {"n": 999}}), ["alpha"])])
    api = _api(tmp_path, client)
    api.plugin._mover = types.SimpleNamespace(settings=lambda: FakeSettings(root))

    web.request._json = {"from": "nas", "confirm": True}
    response = asyncio.run(api.apply())

    assert response.status_code == 200
    assert response.json["updated"] == ["alpha"]
    assert response.json["backups"] == 1
    assert json.loads((root / "config/alpha_config.json").read_text("utf-8")) == {"n": 999}
    backups = list((root / "config").glob("alpha_config.json.molt-before-sync-*"))
    assert len(backups) == 1


def test_apply_can_restrict_to_named_plugins(tmp_path):
    root = _local_dir(tmp_path)
    client = FakeClient([("nas", _bundle_for({"alpha": {"n": 7}, "beta": {"same": False}}), ["alpha", "beta"])])
    api = _api(tmp_path, client)
    api.plugin._mover = types.SimpleNamespace(settings=lambda: FakeSettings(root))

    web.request._json = {"from": "nas", "confirm": True, "only": ["alpha"]}
    response = asyncio.run(api.apply())

    assert response.json["updated"] == ["alpha"]
    assert json.loads((root / "config/beta_config.json").read_text("utf-8")) == {"same": True}


def test_apply_rejects_a_bad_only(tmp_path):
    root = _local_dir(tmp_path)
    client = FakeClient([("nas", _bundle_for({"alpha": {"n": 7}}), ["alpha"])])
    api = _api(tmp_path, client)
    api.plugin._mover = types.SimpleNamespace(settings=lambda: FakeSettings(root))

    web.request._json = {"from": "nas", "confirm": True, "only": "alpha"}
    response = asyncio.run(api.apply())

    assert response.status_code == 400
    assert "only" in response.json["message"]


def test_apply_needs_a_source(tmp_path):
    client = FakeClient([])
    api = _api(tmp_path, client)

    web.request._json = {"confirm": True}
    response = asyncio.run(api.apply())

    assert response.status_code == 400
    assert "from" in response.json["message"]


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------


def test_routes_are_registered_under_the_plugin_prefix(tmp_path):
    plugin = FakePlugin(FakeSettings(tmp_path))
    calls = plugin.register()
    api = sync_api.MoltSyncApi(plugin)
    api.register_routes()

    routes = {route: methods for route, methods, _ in calls}
    assert routes["/astrbot_plugin_molt/sync/state"] == ["GET"]
    assert routes["/astrbot_plugin_molt/sync/publish"] == ["POST"]
    assert routes["/astrbot_plugin_molt/sync/preview"] == ["GET"]
    assert routes["/astrbot_plugin_molt/sync/apply"] == ["POST"]
