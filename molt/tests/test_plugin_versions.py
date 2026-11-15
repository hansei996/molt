"""版本检测的测试（2026-11-15 真机上咬人的那件事）。

真机事实：`astrbot_plugin_private_companion` 在 NAS 是 7.1.18、在 Windows 是 6.5.6，
两台配置顶层键分别是 1008 和 15。同步这件事本身没错，错的是**没告诉人原因**。
"""
from __future__ import annotations

import json
from pathlib import Path

from molt import pluginsync


def _make(where: Path, name: str, version: str, config: dict) -> None:
    (where / "plugins" / name).mkdir(parents=True, exist_ok=True)
    (where / "config").mkdir(parents=True, exist_ok=True)
    (where / "plugins" / name / "metadata.yaml").write_text(
        f"name: {name}\ndisplay_name: 测试\nauthor: someone\nversion: {version}\n",
        encoding="utf-8",
    )
    (where / "config" / f"{name}_config.json").write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )


def test_versions_ride_along_in_the_manifest(tmp_path: Path) -> None:
    _make(tmp_path, "alpha", "7.1.18", {"k": 1})
    payload, manifest = pluginsync.make_bundle(tmp_path, ["alpha"])
    assert manifest["versions"] == {"alpha": "7.1.18"}
    again = pluginsync.read_bundle_manifest(payload)
    assert again["versions"] == {"alpha": "7.1.18"}


def test_an_unknown_version_is_an_empty_string_not_a_guess(tmp_path: Path) -> None:
    (tmp_path / "plugins" / "beta").mkdir(parents=True)
    versions = pluginsync.read_plugin_versions(tmp_path, ["beta", "missing"])
    assert versions == {"beta": "", "missing": ""}


def test_a_version_mismatch_is_recorded_but_does_not_change_the_plan() -> None:
    """版本不同只是**原因**，不是新的判断条件 —— 同步与否仍只看内容与结构。"""
    plan = pluginsync.plan_sync(
        remote_plugins=["alpha"],
        local_plugins=["alpha"],
        remote_configs={"alpha": b'{"k": 1}'},
        local_configs={"alpha": b'{"k": 2}'},
        remote_versions={"alpha": "7.1.18"},
        local_versions={"alpha": "6.5.6"},
    )
    assert plan.version_mismatch == {"alpha": {"remote": "7.1.18", "local": "6.5.6"}}
    assert "alpha" in plan.sync


def test_unknown_versions_are_not_compared() -> None:
    """不知道就不比 —— 空串不能等于"版本相同"，也不能等于"版本不同"。"""
    plan = pluginsync.plan_sync(
        remote_plugins=["alpha"],
        local_plugins=["alpha"],
        remote_configs={"alpha": b'{"k": 1}'},
        local_configs={"alpha": b'{"k": 2}'},
        remote_versions={"alpha": ""},
        local_versions={"alpha": "6.5.6"},
    )
    assert plan.version_mismatch == {}


def test_identical_versions_are_quiet() -> None:
    plan = pluginsync.plan_sync(
        remote_plugins=["alpha"],
        local_plugins=["alpha"],
        remote_configs={"alpha": b'{"k": 1}'},
        local_configs={"alpha": b'{"k": 2}'},
        remote_versions={"alpha": "6.5.6"},
        local_versions={"alpha": "6.5.6"},
    )
    assert plan.version_mismatch == {}
