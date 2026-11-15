"""T3：manifest 校验加固。

## 这一条在防什么

manifest 是快照的出生证明：它说"这份东西是第几代、从哪台机器来、带了什么"。
`land` 靠它决定要不要落位、落了之后算第几代。

三种坏数据都长得像正常：

  - **缺字段**：`json.dumps` 掉一个 key，JSON 照样合法、照样能 `json.loads`
  - **类型错**：`generation` 变成字符串 `"7"`，`int()` 还能转过去，于是没人发现
  - **代次对不上**：归档里的 manifest 说第 7 代，而账本让你取第 9 代 ——
    这是最坏的一种，因为**落位会成功**，只是本地库从此变成"账本说第 9 代、
    实际是第 7 代的内容"。之后每一次 `decide()` 都基于一个错的代次做判断。

所以这里的断言重点不是"能不能拒"，而是**拒绝的理由像不像人话**：
报 `KeyError: 'source_host'` 会被人当成 molt 的 bug；报"缺字段 `source_host`
（从哪台机器来的）"才会被当成"这份快照有问题"。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from molt.cli import main
from molt.errors import Fatal
from molt.soul import Manifest, manifest_problems
from molt.transfer import extract_archive, make_archive


# --------------------------------------------------------------------------
# 夹具：造一份真实可用的快照，然后按需篡改它的 manifest
# --------------------------------------------------------------------------


def _make_sqlite(path: Path, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, title TEXT)")
        conn.executemany(
            "INSERT INTO conversations (title) VALUES (?)",
            [(f"第 {i} 条",) for i in range(rows)],
        )
        conn.commit()
    finally:
        conn.close()


def _make_source_data(base: Path) -> Path:
    data = base / "source" / "data"
    _make_sqlite(data / "data_v4.db", rows=3)
    _make_sqlite(
        data / "plugin_data" / "astrbot_plugin_memory_companion" / "memory_companion.db",
        rows=2,
    )
    (data / "cmd_config.json").write_text('{"config_version": 5}', encoding="utf-8-sig")
    return data


def _fresh_archive(tmp_path: Path) -> tuple[Path, Path]:
    """跑一次真 migrate，拿到一个**完全合法**的 gen-000001 归档。"""
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    rc = main(
        [
            "migrate",
            # 这些用例验的不是「目标在不在」那道闸门（见 test_migrate_wakeable.py）。
            "--skip-target-check",
            "--data",
            str(data),
            "--shell",
            "shell-a",
            "--root",
            str(root),
            "--target",
            "shell-b",
            "--no-color",
        ]
    )
    assert rc == 0, "夹具本身要求 migrate 成功"
    # `--transport local` 只把归档留在 staging 里（真走 Warden 时由
    # `upload_archive` 搬进归档区）。而 `land --generation N` 读的是**归档区**，
    # 所以要篡改的必须是归档区那一份 —— 否则改完 land 拿到的是原件，
    # 测试会"通过"但什么都没验到。
    archive = root / "archives" / "gen-000001.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "staging" / "gen-000001.tar.gz", archive)
    assert archive.is_file()
    return root, archive


def _mutate_manifest(archive: Path, mutate) -> None:
    """解开归档、改里面的 manifest.json、重新打包回原路径。

    `mutate` 收到 `(payload_dir, manifest_dict)`，返回值被忽略——
    它直接改 `manifest_dict` 或删文件都行。
    """
    with tempfile.TemporaryDirectory() as tmp:
        payload = Path(tmp) / "payload"
        extract_archive(archive, payload)
        mpath = payload / "manifest.json"
        payload_dict = json.loads(mpath.read_text(encoding="utf-8"))
        mutate(payload, payload_dict)
        mpath.write_text(
            json.dumps(payload_dict, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        make_archive(payload, archive)


def _land(root: Path, target: Path, generation: int = 1) -> int:
    # `land` 的判据是"父目录必须已经存在"（那是为了区分"路径打错了"和
    # "这台机器还没跑过 AstrBot"）。这里给父目录，目标是全新的 data/。
    target.parent.mkdir(parents=True, exist_ok=True)
    return main(
        [
            "land",
            "--generation",
            str(generation),
            "--data",
            str(target),
            "--root",
            str(root),
            "--shell",
            "shell-b",
            "--yes",
            "--no-color",
        ]
    )


# --------------------------------------------------------------------------
# manifest_problems：纯函数，逐条错误都点名
# --------------------------------------------------------------------------


def _good_manifest() -> dict:
    return {
        "generation": 3,
        "created_at": 1794679431.5,
        "source_host": "ugreen-776b",
        "source_root": "/mnt/dm-0/astrbot/data",
        "protocol_version": 1,
        "artifacts": [],
    }


def test_a_complete_manifest_has_no_problems():
    assert manifest_problems(_good_manifest()) == []


def test_a_missing_field_is_named_with_its_meaning():
    bad = _good_manifest()
    del bad["source_host"]
    problems = manifest_problems(bad)
    assert any("source_host" in p and "从哪台机器来的" in p for p in problems), problems


def test_a_wrong_type_says_what_it_expected_and_what_it_got():
    bad = _good_manifest()
    bad["generation"] = "7"
    problems = manifest_problems(bad)
    assert any("generation" in p and "str" in p and "int" in p for p in problems), problems


def test_a_boolean_generation_is_rejected_even_though_bool_is_an_int():
    """`isinstance(True, int)` 是 True —— 但把 `True` 当成"第 1 代"是胡说。"""
    bad = _good_manifest()
    bad["generation"] = True
    assert any("generation" in p for p in manifest_problems(bad))


def test_an_empty_source_host_counts_as_missing():
    bad = _good_manifest()
    bad["source_host"] = "   "
    assert any("source_host" in p for p in manifest_problems(bad))


def test_a_top_level_that_is_not_an_object_is_rejected():
    assert manifest_problems([1, 2, 3]) == ["顶层不是一个 JSON 对象，而是 list"]


def test_a_future_schema_version_is_refused():
    bad = _good_manifest()
    bad["schema_version"] = 99
    assert any("schema_version" in p for p in manifest_problems(bad))


# --------------------------------------------------------------------------
# Manifest.from_dict / load：把问题翻译成人话再抛出
# --------------------------------------------------------------------------


def test_from_dict_raises_an_error_that_reads_like_a_rejection():
    bad = _good_manifest()
    del bad["source_root"]
    with pytest.raises(Fatal) as ei:
        Manifest.from_dict(bad)
    text = str(ei.value)
    assert "source_root" in text
    # 不是 KeyError 的裸消息，而是"这份快照有问题"的口吻
    assert "出生证明" in text


def test_load_reports_the_line_and_column_of_broken_json(tmp_path: Path):
    path = tmp_path / "manifest.json"
    path.write_text('{"generation": 3,', encoding="utf-8")
    with pytest.raises(Fatal) as ei:
        Manifest.load(path)
    assert "不是合法 JSON" in str(ei.value)
    assert "断电" in str(ei.value)


# --------------------------------------------------------------------------
# cmd_land：代次交叉校验（T3 的核心）
# --------------------------------------------------------------------------


def test_land_refuses_when_the_manifest_names_a_different_generation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    root, archive = _fresh_archive(tmp_path)

    def to_generation_two(payload: Path, d: dict) -> None:
        d["generation"] = 2

    _mutate_manifest(archive, to_generation_two)

    target = tmp_path / "target" / "data"
    rc = _land(root, target, generation=1)
    out = capsys.readouterr().out

    assert rc == 6, "代次对不上必须拒收"
    assert "快照 #2" in out and "快照 #1" in out
    assert not (target / "data_v4.db").exists(), "拒收之后不许留下任何文件"


def test_land_refuses_a_manifest_missing_a_required_field(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    root, archive = _fresh_archive(tmp_path)

    def drop_host(payload: Path, d: dict) -> None:
        del d["source_host"]

    _mutate_manifest(archive, drop_host)

    target = tmp_path / "target" / "data"
    rc = _land(root, target, generation=1)
    out = capsys.readouterr().out

    assert rc == 6
    assert "source_host" in out
    assert not (target / "data_v4.db").exists()


def test_land_refuses_a_manifest_that_is_not_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    root, archive = _fresh_archive(tmp_path)

    def truncate(payload: Path, d: dict) -> None:
        (payload / "manifest.json").write_text('{"generation": 1,', encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        payload = Path(tmp) / "payload"
        extract_archive(archive, payload)
        truncate(payload, {})
        make_archive(payload, archive)

    target = tmp_path / "target" / "data"
    rc = _land(root, target, generation=1)
    out = capsys.readouterr().out

    assert rc == 6
    assert "不是合法 JSON" in out
    assert not (target / "data_v4.db").exists()


def test_land_still_accepts_a_snapshot_whose_manifest_is_honest(tmp_path: Path):
    """反例：不改 manifest 时必须真的落位成功 —— 否则上面三条测的是"永远拒"。"""
    root, _archive = _fresh_archive(tmp_path)
    target = tmp_path / "target" / "data"
    assert _land(root, target, generation=1) == 0
    assert (target / "data_v4.db").is_file()
