"""搬家会不会把插件配置一起带走、落位时又写不写回来。

这里钉住的是三条边界（见 `molt/landcfg.py` 的模块 docstring）：
只写这边也装了的插件、结构差太多的不写、机器绑定的值保留本机的。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from molt import cli, landcfg, pluginsync, soul


def _make_data(root: Path, *, plugins: list[str], configs: dict[str, dict]) -> Path:
    data = root / "data"
    for name in plugins:
        (data / "plugins" / name).mkdir(parents=True, exist_ok=True)
        (data / "plugins" / name / "main.py").write_text("# stub\n", encoding="utf-8")
    for name, payload in configs.items():
        path = data / "config" / f"{name}_config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    # 快照逻辑要看到一个能热备的库，否则 _snapshot_payload 会报"一个库都没备份到"
    db = data / "data_v4.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS conversations (id INTEGER PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO conversations (title) VALUES ('hi')")
    conn.commit()
    conn.close()
    return data


def test_the_snapshot_carries_plugin_configs(tmp_path: Path):
    src = _make_data(tmp_path, plugins=["alpha"], configs={"alpha": {"k": 1}})
    gen = tmp_path / "staging" / "gen-000001"
    gen.mkdir(parents=True)

    rc, arts, manifest = cli._snapshot_payload(
        src=src, dest_dir=gen, astrbot_src=None, target="", generation=1, quiet=True
    )

    assert rc == 0
    assert manifest is not None
    carried = gen / "config" / "alpha_config.json"
    assert carried.is_file()
    assert json.loads(carried.read_text(encoding="utf-8")) == {"k": 1}
    assert any(a.relpath == "config/alpha_config.json" for a in arts)


def test_backups_of_configs_are_not_carried(tmp_path: Path):
    src = _make_data(tmp_path, plugins=["alpha"], configs={"alpha": {"k": 1}})
    cfg_dir = src / "config"
    (cfg_dir / "alpha_config.json.bak-before-port-3082").write_text("{}", encoding="utf-8")
    (cfg_dir / "alpha_config.json.molt-before-sync-20260101T000000").write_text(
        "{}", encoding="utf-8"
    )

    assert soul.collect_plugin_configs(src) == ["alpha_config.json"]


def test_an_applied_config_lands_and_keeps_a_backup(tmp_path: Path):
    data = _make_data(tmp_path, plugins=["alpha"], configs={"alpha": {"k": "old"}})
    gen = tmp_path / "gen"
    (gen / "config").mkdir(parents=True)
    (gen / "config" / "alpha_config.json").write_text('{"k": "new"}', encoding="utf-8")

    got = landcfg.apply_carried_configs(gen, data, stamp="20260101T000000")

    assert got["applied"] == ["alpha"]
    assert json.loads((data / "config" / "alpha_config.json").read_text(encoding="utf-8")) == {
        "k": "new"
    }
    backup = data / "config" / "alpha_config.json.molt-before-land-20260101T000000"
    assert backup.is_file()
    assert json.loads(backup.read_text(encoding="utf-8")) == {"k": "old"}


def test_a_plugin_that_is_not_installed_here_is_not_written(tmp_path: Path):
    data = _make_data(tmp_path, plugins=["alpha"], configs={"alpha": {"k": 1}})
    gen = tmp_path / "gen"
    (gen / "config").mkdir(parents=True)
    (gen / "config" / "beta_config.json").write_text('{"k": 2}', encoding="utf-8")

    got = landcfg.apply_carried_configs(gen, data, stamp="20260101T000000")

    assert got["not_installed"] == ["beta"]
    assert not (data / "config" / "beta_config.json").exists()


def test_identical_configs_are_left_alone(tmp_path: Path):
    data = _make_data(tmp_path, plugins=["alpha"], configs={"alpha": {"k": 1}})
    before = (data / "config" / "alpha_config.json").stat().st_mtime_ns
    gen = tmp_path / "gen"
    (gen / "config").mkdir(parents=True)
    (gen / "config" / "alpha_config.json").write_text(
        json.dumps({"k": 1}, ensure_ascii=False), encoding="utf-8"
    )

    got = landcfg.apply_carried_configs(gen, data, stamp="20260101T000000")

    assert got["same"] == ["alpha"]
    assert (data / "config" / "alpha_config.json").stat().st_mtime_ns == before


def test_a_different_schema_is_not_overwritten(tmp_path: Path):
    data = _make_data(
        tmp_path, plugins=["alpha"], configs={"alpha": {f"old_{i}": i for i in range(10)}}
    )
    gen = tmp_path / "gen"
    (gen / "config").mkdir(parents=True)
    (gen / "config" / "alpha_config.json").write_text(
        json.dumps({f"new_{i}": i for i in range(10)}), encoding="utf-8"
    )

    got = landcfg.apply_carried_configs(gen, data, stamp="20260101T000000")

    assert got["schema"] == ["alpha"]
    original = json.loads((data / "config" / "alpha_config.json").read_text(encoding="utf-8"))
    assert "old_0" in original


def test_machine_bound_values_keep_the_local_one(tmp_path: Path):
    data = _make_data(
        tmp_path,
        plugins=["astrbot_plugin_dsh_connector"],
        configs={"astrbot_plugin_dsh_connector": {"http_base_url": "http://127.0.0.1:4000"}},
    )
    gen = tmp_path / "gen"
    (gen / "config").mkdir(parents=True)
    (gen / "config" / "astrbot_plugin_dsh_connector_config.json").write_text(
        json.dumps({"http_base_url": "http://10.0.0.9:3082", "mode": "http"}),
        encoding="utf-8",
    )

    got = landcfg.apply_carried_configs(gen, data, stamp="20260101T000000")

    assert got["applied"] == ["astrbot_plugin_dsh_connector"]
    merged = json.loads(
        (data / "config" / "astrbot_plugin_dsh_connector_config.json").read_text(encoding="utf-8")
    )
    assert merged["http_base_url"] == "http://127.0.0.1:4000"      # 本机的
    assert merged["mode"] == "http"                                  # 搬来的


def test_a_dry_run_writes_nothing(tmp_path: Path):
    data = _make_data(tmp_path, plugins=["alpha"], configs={"alpha": {"k": "old"}})
    gen = tmp_path / "gen"
    (gen / "config").mkdir(parents=True)
    (gen / "config" / "alpha_config.json").write_text('{"k": "new"}', encoding="utf-8")

    got = landcfg.apply_carried_configs(gen, data, stamp="20260101T000000", dry_run=True)

    assert got["applied"] == ["alpha"]
    assert json.loads((data / "config" / "alpha_config.json").read_text(encoding="utf-8")) == {
        "k": "old"
    }
