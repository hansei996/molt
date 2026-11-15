"""多端插件配置同步：**只有两边都装了的插件才同步**。

主人 2026-11-15 定的规则：

    如果有相同插件才同步，如果某个插件只有某一段独有，就不能同步。

这条规则看起来像"少做一点"，其实是这个功能唯一安全的形状：

  - 给一个不存在的插件送配置不会报错，它只会安静躺着；等哪天真装上了，
    它会带着**另一台机器**的配置启动
  - "同步"一旦被理解成"让两边一样"，销毁就成了其中一步
  - 有些值天然绑定机器（`http_base_url` 就是真机上咬过人的那个）

所以这里的用例一半在测"同步了什么"，一半在测"**没动什么**"。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict

import pytest

from molt import pluginsync
from molt.server import WardenServer


def _make_data(
    where: Path, plugins: list, configs: Dict[str, object]
) -> Path:
    """造一个最小的 data 目录：plugins/<名字>/ + config/<名字>_config.json。"""
    (where / "plugins").mkdir(parents=True, exist_ok=True)
    (where / "config").mkdir(parents=True, exist_ok=True)
    for name in plugins:
        (where / "plugins" / name).mkdir(exist_ok=True)
    for name, body in configs.items():
        (where / "config" / f"{name}_config.json").write_text(
            json.dumps(body, ensure_ascii=False), encoding="utf-8"
        )
    return where


# --------------------------------------------------------------------------
# 纯函数：计划
# --------------------------------------------------------------------------


def test_only_the_plugins_both_sides_have_are_planned() -> None:
    """交集才同步；只在一边有的，两边都不动。"""
    plan = pluginsync.plan_sync(
        remote_plugins=["alpha", "beta", "only_remote"],
        local_plugins=["alpha", "beta", "only_local"],
        remote_configs={
            "alpha": b'{"k": 1}',
            "beta": b'{"k": 2}',
            "only_remote": b'{"x": 9}',
        },
        local_configs={"alpha": b'{"k": 111}', "beta": b'{"k": 2}'},
    )
    assert sorted(plan.sync) == ["alpha"]
    assert plan.unchanged == ["beta"]
    assert plan.only_remote == ["only_remote"]
    assert plan.only_local == ["only_local"]
    # 关键：独有的插件**不能**出现在要写的那一侧
    assert "only_remote" not in plan.sync
    assert "only_local" not in plan.sync


def test_a_plugin_that_has_no_config_on_the_remote_is_not_written() -> None:
    """那边装了但没配置过 —— 不能拿"空"去覆盖这边的配置。"""
    plan = pluginsync.plan_sync(
        remote_plugins=["alpha"],
        local_plugins=["alpha"],
        remote_configs={},
        local_configs={"alpha": b'{"k": 1}'},
    )
    assert plan.sync == {}
    assert plan.no_remote_config == ["alpha"]


def test_identical_bytes_are_not_rewritten() -> None:
    """一样就不写 —— 写一遍会改 mtime，还会让备份目录里堆满同样的文件。"""
    payload = b'{"k": 1}'
    plan = pluginsync.plan_sync(
        remote_plugins=["alpha"],
        local_plugins=["alpha"],
        remote_configs={"alpha": payload},
        local_configs={"alpha": payload},
    )
    assert plan.sync == {}
    assert plan.unchanged == ["alpha"]


def test_a_completely_different_schema_is_suspect_not_silently_written() -> None:
    """真实例子：private_companion 一边 15 个键、一边 1008 个键。

    那通常意味着两边的插件版本不同。覆盖过去等于把对方不认识的键塞进去，
    症状是"同步完了，插件开始报 KeyError"。
    """
    remote = json.dumps({f"remote_{i}": i for i in range(20)}).encode()
    local = json.dumps({f"local_{i}": i for i in range(20)}).encode()
    plan = pluginsync.plan_sync(
        remote_plugins=["alpha"],
        local_plugins=["alpha"],
        remote_configs={"alpha": remote},
        local_configs={"alpha": local},
    )
    assert plan.sync == {}
    assert plan.suspect == ["alpha"]
    assert "alpha" in plan.suspect_payloads


def test_a_config_that_is_not_json_is_suspect() -> None:
    plan = pluginsync.plan_sync(
        remote_plugins=["alpha"],
        local_plugins=["alpha"],
        remote_configs={"alpha": b"not json at all"},
        local_configs={"alpha": b'{"k": 1}'},
    )
    assert plan.suspect == ["alpha"]


def test_a_plan_never_mentions_a_plugin_outside_the_intersection() -> None:
    """把这条写成一条独立断言，因为它是主人那条规则的直接翻译。"""
    plan = pluginsync.plan_sync(
        remote_plugins=["a", "r"],
        local_plugins=["a", "l"],
        remote_configs={"a": b'{"k": 1}', "r": b'{"k": 2}'},
        local_configs={"a": b'{"k": 9}', "l": b'{"k": 3}'},
    )
    everybody = set(plan.sync) | set(plan.unchanged) | set(plan.suspect)
    assert everybody <= {"a"}


# --------------------------------------------------------------------------
# 落盘：空跑不写、写就一字不差、旧文件备份
# --------------------------------------------------------------------------


def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    data = _make_data(tmp_path / "local", ["alpha"], {"alpha": {"k": 111}})
    before = {p.name: p.read_bytes() for p in (data / "config").iterdir()}
    plan = pluginsync.plan_sync(
        remote_plugins=["alpha"],
        local_plugins=["alpha"],
        remote_configs={"alpha": b'{"k": 1}'},
        local_configs=pluginsync.read_plugin_configs(data, ["alpha"]),
    )
    report = pluginsync.apply_plan(data, plan, dry_run=True)
    after = {p.name: p.read_bytes() for p in (data / "config").iterdir()}
    assert after == before
    assert report.updated == []
    assert report.dry_run is True


def test_writing_restores_the_remote_bytes_exactly(tmp_path: Path) -> None:
    """字节级还原：带 BOM、带换行、键顺序都必须是那边的样子。

    用 JSON 反序列化再序列化会悄悄改掉这些 —— 而那一份是**另一台机器的**
    文件，我们没有任何理由让它变样。
    """
    data = _make_data(tmp_path / "local", ["alpha"], {"alpha": {"k": 111}})
    remote_bytes = b'\xef\xbb\xbf{"b": 2, "a": 1}\n'
    plan = pluginsync.plan_sync(
        remote_plugins=["alpha"],
        local_plugins=["alpha"],
        remote_configs={"alpha": remote_bytes},
        local_configs=pluginsync.read_plugin_configs(data, ["alpha"]),
    )
    pluginsync.apply_plan(data, plan, dry_run=False)
    got = (data / "config" / "alpha_config.json").read_bytes()
    assert got == remote_bytes


def test_the_old_config_is_kept_before_being_replaced(tmp_path: Path) -> None:
    data = _make_data(tmp_path / "local", ["alpha"], {"alpha": {"k": 111}})
    plan = pluginsync.plan_sync(
        remote_plugins=["alpha"],
        local_plugins=["alpha"],
        remote_configs={"alpha": b'{"k": 1}'},
        local_configs=pluginsync.read_plugin_configs(data, ["alpha"]),
    )
    report = pluginsync.apply_plan(data, plan, dry_run=False)
    assert len(report.backups) == 1
    old = Path(next(iter(report.backups.values())))
    assert old.is_file()
    assert b"111" in old.read_bytes()
    # 备份名要能让人一眼看出是什么时候、为什么
    assert ".molt-before-sync-" in old.name


def test_force_writes_the_suspect_ones(tmp_path: Path) -> None:
    data = _make_data(
        tmp_path / "local", ["alpha"], {"alpha": {f"local_{i}": i for i in range(20)}}
    )
    remote = json.dumps({f"remote_{i}": i for i in range(20)}).encode()
    plan = pluginsync.plan_sync(
        remote_plugins=["alpha"],
        local_plugins=["alpha"],
        remote_configs={"alpha": remote},
        local_configs=pluginsync.read_plugin_configs(data, ["alpha"]),
    )
    assert plan.suspect == ["alpha"]
    without = pluginsync.apply_plan(data, plan, dry_run=False, force=False)
    assert without.updated == []
    with_force = pluginsync.apply_plan(data, plan, dry_run=False, force=True)
    assert with_force.updated == ["alpha"]


# --------------------------------------------------------------------------
# 包：只装配置、路径不能逃出去
# --------------------------------------------------------------------------


def test_the_bundle_contains_configs_and_nothing_else(tmp_path: Path) -> None:
    data = _make_data(
        tmp_path / "local",
        ["alpha", "beta"],
        {"alpha": {"k": 1}, "beta": {"k": 2}},
    )
    # 放一些"绝不该进包"的东西
    (data / "plugin_data" / "alpha").mkdir(parents=True, exist_ok=True)
    (data / "plugin_data" / "alpha" / "state.json").write_text("{}", encoding="utf-8")
    (data / "plugins" / "alpha" / "main.py").write_text("# code", encoding="utf-8")

    payload, manifest = pluginsync.make_bundle(data, ["alpha", "beta", "ghost"])
    got = pluginsync.parse_bundle(payload)

    assert sorted(got) == ["alpha", "beta"]
    assert sorted(manifest["configs"]) == ["alpha", "beta"]
    assert b"main.py" not in payload
    assert b"state.json" not in payload


def test_a_bundle_round_trips_byte_for_byte(tmp_path: Path) -> None:
    data = _make_data(tmp_path / "local", ["alpha"], {"alpha": {"k": 1}})
    original = (data / "config" / "alpha_config.json").read_bytes()
    payload, _ = pluginsync.make_bundle(data, ["alpha"])
    assert pluginsync.parse_bundle(payload)["alpha"] == original


def test_a_path_that_escapes_the_config_directory_is_not_accepted() -> None:
    """白名单正则是唯一的入口 —— 不是"解包之后再检查"。

    这些成员**被忽略**而不是报错：报错会让一个存心写坏的包变成
    一次拒服务；而忽略它，结果和"这份包里没有它"完全一样。
    """
    for bad in (
        "../evil_config.json",
        "config/../../evil_config.json",
        "config/.._config.json",
        "/etc/passwd",
    ):
        got = pluginsync.parse_bundle(_tar_with((bad, b"x")))
        assert got == {}, bad
    assert pluginsync.parse_bundle(_tar_with(("config/ok_config.json", b"{}"))) == {
        "ok": b"{}"
    }


def _tar_with(pair) -> bytes:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(pair[0])
        info.size = len(pair[1])
        tf.addfile(info, io.BytesIO(pair[1]))
    return buf.getvalue()


def test_local_noise_files_are_ignored(tmp_path: Path) -> None:
    data = _make_data(tmp_path / "local", ["alpha"], {"alpha": {"k": 1}})
    (data / "config" / "alpha_config.json.bak-before-port").write_text(
        "{}", encoding="utf-8"
    )
    configs = pluginsync.read_plugin_configs(data, ["alpha"])
    assert list(configs) == ["alpha"]
    assert b"bak-before-port" not in configs["alpha"]


def test_list_plugins_ignores_loose_files_and_pycache(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "plugins" / "alpha").mkdir(parents=True)
    (data / "plugins" / "__pycache__").mkdir()
    (data / "plugins" / ".hidden").mkdir()
    (data / "plugins" / "README.md").write_text("hi", encoding="utf-8")
    assert pluginsync.list_plugins(data) == ["alpha"]


# --------------------------------------------------------------------------
# 帐本端点
# --------------------------------------------------------------------------


@pytest.fixture()
def srv(tmp_path: Path) -> WardenServer:
    return WardenServer(root=tmp_path / "warden", token="t0ken", port=0)


def test_the_ledger_lists_and_serves_a_bundle(srv: WardenServer) -> None:
    payload = _tar_with(("config/alpha_config.json", b'{"k": 1}'))
    r = srv.accept_plugin_bundle(_tmp_file(payload), "", "desk", ["alpha"])
    assert r.status == 200
    assert r.payload["plugin_bundle"]["shell"] == "desk"

    listing = srv.dispatch("GET", "/plugin-configs", None)
    assert listing.status == 200
    assert [b["shell"] for b in listing.payload["plugin_bundles"]] == ["desk"]

    got = srv.stream_plugin_bundle("desk")
    assert got.status == 200
    assert got.stream_path is not None
    assert got.stream_path.read_bytes() == payload


def test_an_unknown_shell_is_a_404(srv: WardenServer) -> None:
    r = srv.stream_plugin_bundle("ghost")
    assert r.status == 404
    assert r.payload["kind"] == "not_found"


def test_a_bundle_without_a_shell_name_is_refused(srv: WardenServer) -> None:
    r = srv.accept_plugin_bundle(_tmp_file(b"x"), "", "", ["alpha"])
    assert r.status == 400
    assert "壳名" in r.payload["message"]


def test_a_tampered_bundle_is_refused(srv: WardenServer) -> None:
    """指纹对不上的包一律丢弃 —— 它会让另一台机器照着改配置。"""
    r = srv.accept_plugin_bundle(_tmp_file(b"payload"), "0" * 64, "desk", [])
    assert r.status == 400
    assert r.payload["kind"] == "checksum_mismatch"
    assert srv.dispatch("GET", "/plugin-configs", None).payload["plugin_bundles"] == []


def _tmp_file(data: bytes) -> Path:
    import tempfile

    fd, name = tempfile.mkstemp(prefix="bundle-")
    os.close(fd)
    p = Path(name)
    p.write_bytes(data)
    return p


# --------------------------------------------------------------------------
# CLI 端到端：发布 → 空跑 → 应用
# --------------------------------------------------------------------------


def test_the_cli_publishes_then_applies_only_the_shared_plugins(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from molt import cli

    root = tmp_path / "warden"
    a = _make_data(
        tmp_path / "a",
        ["alpha", "beta", "only_a"],
        {"alpha": {"k": 1}, "beta": {"k": 2}, "only_a": {"x": 9}},
    )
    b = _make_data(
        tmp_path / "b",
        ["alpha", "beta", "only_b"],
        {"alpha": {"k": 111}, "beta": {"k": 2}, "only_b": {"y": 8}},
    )

    assert (
        cli.main(
            [
                "--no-color",
                "publish-plugins",
                "--data",
                str(a),
                "--shell",
                "shell-a",
                "--root",
                str(root),
            ]
        )
        == 0
    )

    # 空跑：一个字都不许写
    assert (
        cli.main(
            [
                "--no-color",
                "sync-plugins",
                "--data",
                str(b),
                "--shell",
                "shell-b",
                "--from",
                "shell-a",
                "--root",
                str(root),
            ]
        )
        == 0
    )
    assert json.loads(
        (b / "config" / "alpha_config.json").read_text(encoding="utf-8")
    ) == {"k": 111}
    assert "一个字都没写" in capsys.readouterr().out

    # 真的写
    assert (
        cli.main(
            [
                "--no-color",
                "sync-plugins",
                "--data",
                str(b),
                "--shell",
                "shell-b",
                "--from",
                "shell-a",
                "--root",
                str(root),
                "--apply",
            ]
        )
        == 0
    )
    assert json.loads(
        (b / "config" / "alpha_config.json").read_text(encoding="utf-8")
    ) == {"k": 1}

    # 独有插件的东西，一个都不许出现
    assert (b / "config" / "only_b_config.json").is_file()
    assert not (b / "plugins" / "only_a").exists()
    assert not (b / "config" / "only_a_config.json").exists()
    # 而且那边也没被动过
    assert (a / "config" / "only_a_config.json").is_file()
    assert not (a / "plugins" / "only_b").exists()
    assert json.loads(
        (a / "config" / "alpha_config.json").read_text(encoding="utf-8")
    ) == {"k": 1}


def test_syncing_from_a_shell_that_never_published_fails_loudly(
    tmp_path: Path,
) -> None:
    from molt import cli

    b = _make_data(tmp_path / "b", ["alpha"], {"alpha": {"k": 1}})
    rc = cli.main(
        [
            "--no-color",
            "sync-plugins",
            "--data",
            str(b),
            "--shell",
            "shell-b",
            "--from",
            "ghost",
            "--root",
            str(tmp_path / "warden"),
        ]
    )
    assert rc == 4


def test_sync_with_no_other_bundle_says_so_and_writes_nothing(tmp_path: Path) -> None:
    from molt import cli

    b = _make_data(tmp_path / "b", ["alpha"], {"alpha": {"k": 1}})
    rc = cli.main(
        [
            "--no-color",
            "sync-plugins",
            "--data",
            str(b),
            "--shell",
            "shell-b",
            "--root",
            str(tmp_path / "warden"),
        ]
    )
    assert rc == 0
    assert json.loads(
        (b / "config" / "alpha_config.json").read_text(encoding="utf-8")
    ) == {"k": 1}


def test_get_slash_plugin_configs_lists_instead_of_405(tmp_path: Path) -> None:
    """真起服务的一条：`GET /plugin-configs` 必须**落到 dispatch 拿列表**。

    第一版把 GET 也拦在 handler 里回 405「请用 POST」，而 `dispatch()`
    里的 GET 分支看起来完全正常 —— 两边各自都对，合起来永远 405。
    只在 `dispatch()` 层测是抓不到这种错的。
    """
    import urllib.request

    srv = WardenServer(root=tmp_path / "warden", token="t0ken", port=0)
    host, port = srv.start()
    try:
        url = f"http://127.0.0.1:{port}/plugin-configs"
        req = urllib.request.Request(
            url, headers={"X-Molt-Token": "t0ken"}, method="GET"
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read().decode("utf-8"))
        assert body["ok"] is True
        assert body["plugin_bundles"] == []
    finally:
        srv.stop()


# ---------------------------------------------------------------------------
# 清单进包：分得清「那边没装」和「装了但没配置」
# ---------------------------------------------------------------------------


def test_the_bundle_carries_a_manifest_of_every_installed_plugin(tmp_path: Path) -> None:
    """清单记的是"装了哪些插件"，不是"哪些插件有配置"。

    少了它，接收方看到 `beta` 没有配置，只能猜 —— 而"没装"和"装了没配"
    要做的处理完全不同（前者不能装，后者保留本机那份）。
    """
    data = _make_data(tmp_path, ["alpha", "beta"], {"alpha": {"a": 1}})
    payload, manifest = pluginsync.make_bundle(data, ["alpha", "beta"])

    assert manifest["plugins"] == ["alpha", "beta"]
    assert list(manifest["configs"]) == ["alpha"]

    got = pluginsync.read_bundle_manifest(payload)
    assert got["plugins"] == ["alpha", "beta"]
    # 配置仍然只含有配置的那一个
    assert list(pluginsync.parse_bundle(payload)) == ["alpha"]


def test_a_bundle_without_a_manifest_still_parses(tmp_path: Path) -> None:
    """旧版本打的包没有 manifest.json —— 那不叫损坏，只叫更少的信息。"""
    data = _make_data(tmp_path, ["alpha"], {"alpha": {"a": 1}})
    payload, _ = pluginsync.make_bundle(data, ["alpha"])
    stripped = _strip_manifest(payload)

    assert pluginsync.read_bundle_manifest(stripped) == {}
    assert list(pluginsync.parse_bundle(stripped)) == ["alpha"]


def test_a_plugin_the_remote_has_but_never_configured_is_not_only_local(
    tmp_path: Path,
) -> None:
    """真实数据里踩到的那个误报：那边装了 molt 插件、只是没配置。

    `plan_sync` 的 `remote_plugins` 如果只传"有配置的那些"，这个插件就会
    被说成"这边独有"——而它明明两边都有。
    """
    remote = _make_data(tmp_path / "remote", ["alpha", "beta"], {"alpha": {"a": 1}})
    payload, _ = pluginsync.make_bundle(remote, ["alpha", "beta"])
    local = _make_data(
        tmp_path / "local", ["alpha", "beta"], {"alpha": {"a": 9}, "beta": {"b": 2}}
    )

    manifest = pluginsync.read_bundle_manifest(payload)
    plan = pluginsync.plan_sync(
        remote_plugins=manifest["plugins"],
        local_plugins=pluginsync.list_plugins(local),
        remote_configs=pluginsync.parse_bundle(payload),
        local_configs=pluginsync.read_plugin_configs(local, ["alpha", "beta"]),
    )

    assert plan.only_local == []
    assert plan.no_remote_config == ["beta"]
    assert "alpha" in plan.sync


def test_syncing_from_a_bundle_without_a_manifest_falls_back_to_configs(
    tmp_path: Path,
) -> None:
    """没有清单时退回按配置列表判断 —— 会误报，但不会凭空声称"它装了"。"""
    remote = _make_data(tmp_path, ["alpha"], {"alpha": {"a": 1}})
    payload, _ = pluginsync.make_bundle(remote, ["alpha"])
    stripped = _strip_manifest(payload)

    manifest = pluginsync.read_bundle_manifest(stripped)
    configs = pluginsync.parse_bundle(stripped)
    plan = pluginsync.plan_sync(
        remote_plugins=(manifest.get("plugins") or sorted(configs)),
        local_plugins=["alpha"],
        remote_configs=configs,
        local_configs={"alpha": b'{"a": 9}'},
    )
    assert plan.sync == {"alpha": b'{"a": 1}'}


def test_publishing_twice_keeps_only_the_newest_bundle(srv: WardenServer) -> None:
    """同一个壳发布两次：账本里只留最新一份。

    `docs/ARCH.md` §14.12 与 `docs/SELFUSE.md` 都写着"每个壳只保留最新一份（配置不是历史）"。
    2026-11-15 的断言审计发现这条**没有任何测试钉着** —— 它靠的是 `os.replace` 恰好同名覆盖，
    哪天有人把目标文件名加上时间戳（像备份那样），文档会立刻变成谎话，而不会有任何东西变红。
    """
    first = _tar_with(("config/alpha_config.json", b'{"k": 1}'))
    second = _tar_with(("config/alpha_config.json", b'{"k": 2}'))

    assert srv.accept_plugin_bundle(_tmp_file(first), "", "desk", ["alpha"]).status == 200
    assert srv.accept_plugin_bundle(_tmp_file(second), "", "desk", ["alpha"]).status == 200

    listing = srv.dispatch("GET", "/plugin-configs", None)
    assert listing.status == 200
    bundles = listing.payload["plugin_bundles"]
    assert len(bundles) == 1, f"同一个壳应该只留一份，实际 {len(bundles)} 份"
    assert bundles[0]["shell"] == "desk"
    # 留下的是**新的那一份**，不是先到的那一份
    assert bundles[0]["bytes"] == len(second)

    got = srv.stream_plugin_bundle("desk")
    assert got.status == 200
    payload = got.stream_path.read_bytes()
    assert pluginsync.parse_bundle(payload)["alpha"] == b'{"k": 2}'


def _strip_manifest(payload: bytes) -> bytes:
    """把包里的 manifest.json 摘掉，模拟旧版本打的包。"""
    import io
    import tarfile

    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as src:
        with tarfile.open(fileobj=out, mode="w:gz") as dst:
            for member in src.getmembers():
                if member.name == pluginsync.MANIFEST_NAME:
                    continue
                fh = src.extractfile(member)
                dst.addfile(member, fh)
    return out.getvalue()
