"""v0.4 硬化：两条"不许留下中间态"的保证。

这个文件里的每条测试都在问同一个问题：

> **进程在中间被杀掉之后，磁盘上留下的东西会不会被人误认为"做完了"？**

打包和落位是这套系统里唯二会"往别人的目录里写一坨东西"的动作，
而它们原本都是直接写最终路径。于是断电之后会留下：

  - 一个截断的 `gen-000012.tar.gz`（`list_archives()` 会把它列出来）
  - 一个截断的 `data_v4.db`（文件名对、位置对、大小也大于 0）

两条都长得像成功。这里用"注入一次失败"来固定住"不许这样"。
"""

from __future__ import annotations

import tarfile
from pathlib import Path
from typing import List

import pytest

from molt.errors import Fatal, NeedsHuman
from molt.transfer import (
    RESTORE_MARKER_NAME,
    incomplete_restore,
    make_archive,
    restore_into,
)


def _gen(tmp_path: Path, *, name: str = "gen-000001") -> Path:
    gen = tmp_path / name
    (gen / "plugin_data" / "astrbot_plugin_memory_companion").mkdir(parents=True)
    (gen / "data_v4.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 4000)
    (
        gen / "plugin_data" / "astrbot_plugin_memory_companion" / "memory_companion.db"
    ).write_bytes(b"SQLite format 3\x00" + b"\x01" * 2000)
    (gen / "cmd_config.json").write_text('{"config_version": 5}', encoding="utf-8")
    (gen / "manifest.json").write_text('{"generation": 1}', encoding="utf-8")
    return gen


def _data_dir(tmp_path: Path, *, name: str = "data") -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# T1 · 打包原子化
# --------------------------------------------------------------------------


def test_failed_archive_leaves_no_archive_and_no_part(tmp_path: Path):
    """打包中途炸掉 → 目标路径上什么都别留。

    注入点是 `tarfile.TarFile.add`：让它写完第一个成员之后抛错，
    模拟"进程正好在压第二个文件时被 SIGKILL / 盘满"。
    """
    gen = _gen(tmp_path)
    out = tmp_path / "gen-000001.tar.gz"

    original_add = tarfile.TarFile.add
    calls: List[int] = []

    def explode(self, name, *a, **kw):  # noqa: ANN001 - 鸭子类型
        calls.append(1)
        if len(calls) >= 2:
            raise OSError("假装盘满了")
        return original_add(self, name, *a, **kw)

    import molt.transfer as mod

    try:
        mod.tarfile.TarFile.add = explode  # type: ignore[assignment]
        with pytest.raises(OSError):
            make_archive(gen, out)
    finally:
        mod.tarfile.TarFile.add = original_add  # type: ignore[assignment]

    assert not out.exists(), "失败后不该留下名字正确的归档"
    assert not out.with_name(out.name + ".part").exists(), "`.part` 也要清掉"
    leftovers = sorted(p.name for p in tmp_path.glob("gen-000001.tar.gz*"))
    assert leftovers == [], f"目标路径上不该有任何残留：{leftovers}"


def test_successful_archive_leaves_no_part_behind(tmp_path: Path):
    gen = _gen(tmp_path)
    out = tmp_path / "gen-000001.tar.gz"

    info = make_archive(gen, out)

    assert out.is_file()
    assert info["bytes"] == out.stat().st_size
    assert not out.with_name(out.name + ".part").exists()


def test_archive_is_only_visible_under_its_final_name(tmp_path: Path):
    """`.part` 不该被 `gen-*.tar.gz` 这类枚举误认成一份归档。

    这正是"半份归档看起来像一份归档"的直接判据：清理器、列表器、
    落位器都靠这个 glob 找文件。
    """
    gen = _gen(tmp_path)
    out = tmp_path / "gen-000012.tar.gz"
    make_archive(gen, out)

    part = out.with_name(out.name + ".part")
    part.write_bytes(b"half written")

    matched = sorted(p.name for p in tmp_path.glob("gen-*.tar.gz"))
    assert matched == ["gen-000012.tar.gz"], matched


def test_archive_verification_failure_keeps_nothing(tmp_path: Path):
    """回读校验失败（内容与源不一致）也不许留下归档。

    注入点和 `test_transfer.py::test_make_archive_verifies_roundtrip` 一样：
    让回读阶段看到一个少了一个成员的假列表。那条测试只验"会不会拒绝"，
    这条验"拒绝之后磁盘上还剩什么"。
    """
    gen = _gen(tmp_path)
    out = tmp_path / "gen-000001.tar.gz"

    import tarfile as _tar

    real_open = _tar.open

    class _FakeTar:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def getmembers(self):
            class _M:
                name = "data_v4.db"
                size = 1

                def isfile(self):
                    return True

            return [_M()]

    def fake_open(path, mode="r", *a, **kw):  # noqa: ANN001
        if mode == "r:gz":
            return _FakeTar()
        return real_open(path, mode, *a, **kw)

    import molt.transfer as mod

    original = mod.tarfile.open
    mod.tarfile.open = fake_open  # type: ignore[assignment]
    try:
        with pytest.raises(Fatal):
            make_archive(gen, out)
    finally:
        mod.tarfile.open = original  # type: ignore[assignment]

    assert not out.exists()
    assert not out.with_name(out.name + ".part").exists()


# --------------------------------------------------------------------------
# T2 · 落位原子化
# --------------------------------------------------------------------------


def test_interrupted_restore_leaves_no_truncated_db(tmp_path: Path, monkeypatch):
    """落位中途炸掉 → 目标 data/ 里不许出现"半份库"。"""
    gen = _gen(tmp_path)
    data = _data_dir(tmp_path)

    import molt.transfer as mod

    real_copy = mod.shutil.copy2
    seen: List[str] = []

    def explode_on_second(src, dst, *a, **kw):  # noqa: ANN001
        seen.append(Path(src).name)
        if len(seen) >= 2:
            raise OSError("假装落位时断电")
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(mod.shutil, "copy2", explode_on_second)
    try:
        with pytest.raises(OSError):
            restore_into(gen, data)
    finally:
        monkeypatch.setattr(mod.shutil, "copy2", real_copy)

    # 第一个文件是完整的（它当时已经 os.replace 就位），第二个不存在
    assert (data / "cmd_config.json").is_file()
    assert not (data / "data_v4.db").exists(), "半份库不许出现"

    # 半成品也必须被清掉 —— 否则下次有人会看到它以为是我留下的
    strays = sorted(p.name for p in data.rglob("*.molt-part"))
    assert strays == [], f"不该留下 .molt-part：{strays}"

    # 标记必须在：它在说"这一次没做完"
    marker = data / RESTORE_MARKER_NAME
    assert marker.is_file(), "落位中断必须留下未完成标记"
    assert incomplete_restore(data) is not None


def test_restore_clears_the_marker_when_it_finishes(tmp_path: Path):
    gen = _gen(tmp_path)
    data = _data_dir(tmp_path)

    result = restore_into(gen, data)

    assert sorted(result["restored"]) == [
        "cmd_config.json",
        "data_v4.db",
        "plugin_data/astrbot_plugin_memory_companion/memory_companion.db",
    ]
    assert not (data / RESTORE_MARKER_NAME).exists()
    assert incomplete_restore(data) is None


def test_restore_twice_after_an_interruption_finishes_the_job(tmp_path: Path, monkeypatch):
    """中断之后重跑一次，应当能正常收尾 —— 标记不是"永久报废"。"""
    gen = _gen(tmp_path)
    data = _data_dir(tmp_path)

    import molt.transfer as mod

    real_copy = mod.shutil.copy2
    seen: List[str] = []

    def explode_on_second(src, dst, *a, **kw):  # noqa: ANN001
        seen.append(Path(src).name)
        if len(seen) == 2:
            raise OSError("第一次断了")
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(mod.shutil, "copy2", explode_on_second)
    with pytest.raises(OSError):
        restore_into(gen, data)
    monkeypatch.setattr(mod.shutil, "copy2", real_copy)

    assert incomplete_restore(data) is not None

    restore_into(gen, data)

    assert incomplete_restore(data) is None
    assert (data / "data_v4.db").read_bytes().startswith(b"SQLite format 3\x00")


def test_incomplete_restore_treats_a_broken_marker_as_incomplete(tmp_path: Path):
    """标记本身写坏了，同样算"没做完"。

    那说明上一次正好是在写标记的那一瞬间断电的 —— 而那个时刻本来就在中段。
    """
    data = _data_dir(tmp_path)
    (data / RESTORE_MARKER_NAME).write_text("{ half a json", encoding="utf-8")

    got = incomplete_restore(data)

    assert got is not None
    assert got.get("broken") is True


def test_incomplete_restore_returns_none_on_a_clean_dir(tmp_path: Path):
    data = _data_dir(tmp_path)
    assert incomplete_restore(data) is None


def test_restore_still_refuses_a_nonempty_wal_before_touching_anything(tmp_path: Path):
    """护栏必须排在"写标记"之前 —— 拒绝时目录要一个字节都没动。"""
    gen = _gen(tmp_path)
    data = _data_dir(tmp_path)
    (data / "data_v4.db").write_bytes(b"old")
    (data / "data_v4.db-wal").write_bytes(b"x" * 32)

    with pytest.raises(NeedsHuman):
        restore_into(gen, data)

    assert not (data / RESTORE_MARKER_NAME).exists(), "拒绝了就不该动目录"
    assert (data / "data_v4.db").read_bytes() == b"old"
    assert (data / "data_v4.db-wal").stat().st_size == 32
