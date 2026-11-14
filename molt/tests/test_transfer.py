"""transfer.py 的测试：打包、校验、解包、落位。

这一组测试的重点不是"功能能用"，而是**四条能悄悄毁掉迁徙的失败路径
都会被拦住**：

  1. 归档被截断 / 被改 → 指纹不符就拒绝
  2. 归档里有路径穿越（`..` / 绝对路径 / 盘符）→ 拒绝
  3. 归档里有符号链接 → 拒绝
  4. 目标库有未合并的 WAL → 拒绝落位

每一条都有对应的测试。写这些测试的时候我不看实现，先想"如果我是
攻击者/如果我这里出错，会怎么坏"，然后再去验证代码真的拦住了。
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from molt.errors import Fatal, NeedsHuman
from molt.soul import sha256_of
from molt.transfer import (
    _iter_payload_files,
    extract_archive,
    make_archive,
    restore_into,
)


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


def _make_gen_dir(base: Path, *, name: str = "gen-000001") -> Path:
    """造一个形状正确的 generation 目录。"""
    gen = base / name
    (gen / "plugin_data" / "astrbot_plugin_memory_companion").mkdir(parents=True)
    (gen / "data_v4.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 4000)
    (gen / "plugin_data" / "astrbot_plugin_memory_companion" / "memory_companion.db").write_bytes(
        b"SQLite format 3\x00" + b"\x01" * 2000
    )
    (gen / "cmd_config.json").write_text('{"config_version": 5}', encoding="utf-8")
    (gen / "manifest.json").write_text('{"generation": 1}', encoding="utf-8")
    return gen


# --------------------------------------------------------------------------
# make_archive
# --------------------------------------------------------------------------


def test_make_archive_produces_fingerprint(tmp_path: Path):
    gen = _make_gen_dir(tmp_path)
    out = tmp_path / "gen.tar.gz"

    info = make_archive(gen, out)

    assert out.is_file()
    assert info["bytes"] == out.stat().st_size
    assert info["sha256"] == sha256_of(out)
    assert len(info["sha256"]) == 64
    assert info["file_count"] == len(info["files"])
    assert "data_v4.db" in info["files"]
    assert "manifest.json" in info["files"]


def test_make_archive_excludes_caches(tmp_path: Path):
    """缓存目录不该进归档——它们会让归档变大，而且跨机无用。"""
    gen = _make_gen_dir(tmp_path)
    (gen / "__pycache__").mkdir()
    (gen / "__pycache__" / "x.cpython-313.pyc").write_bytes(b"\x00" * 100)
    (gen / "stray.pyc").write_bytes(b"\x00" * 50)

    info = make_archive(gen, tmp_path / "a.tar.gz")

    assert not any("__pycache__" in f for f in info["files"])
    assert not any(f.endswith(".pyc") for f in info["files"])


def test_make_archive_refuses_empty_dir(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(Fatal) as exc:
        make_archive(empty, tmp_path / "a.tar.gz")
    assert "空归档" in str(exc.value)


def test_make_archive_refuses_missing_dir(tmp_path: Path):
    with pytest.raises(Fatal) as exc:
        make_archive(tmp_path / "nope", tmp_path / "a.tar.gz")
    assert "不存在" in str(exc.value)


def test_make_archive_uses_posix_paths(tmp_path: Path):
    """归档里的路径必须是正斜杠，否则 Windows 打的包在 NAS 上会解出一堆怪名字。"""
    gen = _make_gen_dir(tmp_path)
    out = tmp_path / "a.tar.gz"
    make_archive(gen, out)

    with tarfile.open(out, "r:gz") as tf:
        names = tf.getnames()
    assert all("\\" not in n for n in names)
    assert any("/" in n for n in names)


def test_make_archive_verifies_roundtrip(tmp_path: Path):
    """打包完必须回读核对，而不是靠"文件够大"来猜它是对的。

    这条测试用 monkeypatch 让回读阶段看到一个假的成员列表，验证
    "内容不一致就拒绝"这条路真的会走。
    """
    gen = _make_gen_dir(tmp_path)
    out = tmp_path / "a.tar.gz"

    import tarfile as _tar

    real_open = _tar.open

    class _FakeTar:
        def __init__(self, members):
            self._members = members

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def getmembers(self):
            return self._members

    class _FakeMember:
        def __init__(self, name, size):
            self.name = name
            self.size = size

        def isfile(self):
            return True

    def fake_open(path, mode="r", *a, **kw):
        if mode == "r:gz":
            # 少报一个文件，模拟"写进去的没全读出来"
            return _FakeTar([_FakeMember("data_v4.db", 1)])
        return real_open(path, mode, *a, **kw)

    import molt.transfer as T

    original = T.tarfile.open
    T.tarfile.open = fake_open
    try:
        with pytest.raises(Fatal) as exc:
            make_archive(gen, out)
        assert "不一致" in str(exc.value)
    finally:
        T.tarfile.open = original



# --------------------------------------------------------------------------
# extract_archive：正常路径
# --------------------------------------------------------------------------


def test_roundtrip_preserves_bytes(tmp_path: Path):
    gen = _make_gen_dir(tmp_path)
    out = tmp_path / "a.tar.gz"
    info = make_archive(gen, out)

    dest = tmp_path / "out"
    got = extract_archive(out, dest, expected_sha256=info["sha256"])

    assert got["sha256"] == info["sha256"]
    for rel in info["files"]:
        assert (dest / rel).is_file(), rel
        assert (dest / rel).read_bytes() == (gen / rel).read_bytes(), rel


def test_extract_without_expected_sha_still_works(tmp_path: Path):
    """不指纹校验也允许（用于本地调试），但不校验就没有截断保护。"""
    gen = _make_gen_dir(tmp_path)
    out = tmp_path / "a.tar.gz"
    make_archive(gen, out)

    got = extract_archive(out, tmp_path / "out")
    assert "data_v4.db" in got["files"]


# --------------------------------------------------------------------------
# extract_archive：拒绝路径
# --------------------------------------------------------------------------


def test_extract_rejects_sha_mismatch(tmp_path: Path):
    """这是截断保护的唯一防线：字节不对就绝不往下走。"""
    gen = _make_gen_dir(tmp_path)
    out = tmp_path / "a.tar.gz"
    make_archive(gen, out)

    with pytest.raises(Fatal) as exc:
        extract_archive(out, tmp_path / "out", expected_sha256="0" * 64)
    msg = str(exc.value)
    assert "指纹不符" in msg
    assert "不要重试解包" in msg


def test_extract_rejects_sha_mismatch_case_insensitively(tmp_path: Path):
    """十六进制大小写不该影响判定。"""
    gen = _make_gen_dir(tmp_path)
    out = tmp_path / "a.tar.gz"
    info = make_archive(gen, out)

    got = extract_archive(out, tmp_path / "out", expected_sha256=info["sha256"].upper())
    assert got["sha256"] == info["sha256"]


def test_extract_rejects_missing_archive(tmp_path: Path):
    with pytest.raises(NeedsHuman) as exc:
        extract_archive(tmp_path / "nope.tar.gz", tmp_path / "out")
    assert "不存在" in str(exc.value)


def test_extract_rejects_corrupt_archive(tmp_path: Path):
    """不是 tar 的字节必须报"损坏"，不能抛原始 Traceback。"""
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"this is not a tarball at all" * 100)

    with pytest.raises(Fatal) as exc:
        extract_archive(bad, tmp_path / "out")
    assert "损坏" in str(exc.value) or "无法解开" in str(exc.value)


def _tar_with_member(tmp_path: Path, name: str, content: bytes = b"x") -> Path:
    """造一个带**指定成员名**的 tar.gz，用来测路径穿越。

    ## 这里踩过一个坑，值得记下来

    第一版是 `tf.add(str(payload), arcname=name)`。结果 `arcname="/tmp/x"`
    被 Python 的 tarfile 悄悄剥成了 `tmp/x`——于是那两条"拒绝绝对路径"
    的测试其实在测一个良性的相对路径，它们之所以"通过"是因为**断言没机会
    失败前就先失败在别处**（更糟的是有的直接 DID NOT RAISE）。

    换句话说：**测试自己造假数据时也不能偷懒**。直接构造 `TarInfo`
    才能保证归档里真的是那个名字。
    """
    out = tmp_path / "evil.tar.gz"
    data = content
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    info.mtime = 0
    import io

    with tarfile.open(out, "w:gz") as tf:
        tf.addfile(info, io.BytesIO(data))
    return out


def test_extract_rejects_dotdot_escape(tmp_path: Path):
    """`..` 上跳必须被拦住。

    ## 关于这条测试的断言为什么这么宽

    实测过：把代码里自己的 `".." in parts` 检查和 `resolve()/relative_to`
    兜底**都拆掉**之后，这条测试仍然会失败——因为 Python 3.13 的
    `tarfile.extractall(filter="data")` 自己也会拦（消息是
    `would be extracted to ... which is outside the destination`）。

    也就是说这里有三层拦截。所以断言不能只匹配我方措辞，否则测的是
    "我的错误消息长什么样"而不是"恶意路径进不来"。真正该断言的是
    **文件没有出现在目录之外**，那才是安全属性。
    """
    evil = _tar_with_member(tmp_path, "../escaped.txt")
    with pytest.raises(Fatal):
        extract_archive(evil, tmp_path / "out")
    assert not (tmp_path / "escaped.txt").exists()


def test_extract_rejects_nested_dotdot_escape(tmp_path: Path):
    evil = _tar_with_member(tmp_path, "sub/../../escaped2.txt")
    with pytest.raises(Fatal):
        extract_archive(evil, tmp_path / "out")
    assert not (tmp_path / "escaped2.txt").exists()


def test_extract_rejects_absolute_path(tmp_path: Path):
    evil = _tar_with_member(tmp_path, "/tmp/molt-abs-escape.txt")
    with pytest.raises(Fatal) as exc:
        extract_archive(evil, tmp_path / "out")
    assert "绝对路径" in str(exc.value)


def test_extract_rejects_drive_letter_path(tmp_path: Path):
    """Windows 盘符路径在 POSIX 上不是绝对路径，但到了 Windows 上就是。"""
    evil = _tar_with_member(tmp_path, "C:/windows/evil.txt")
    with pytest.raises(Fatal) as exc:
        extract_archive(evil, tmp_path / "out")
    assert "盘符" in str(exc.value)


def test_extract_rejects_symlink(tmp_path: Path):
    """符号链接是路径穿越的经典载体，一律拒绝。"""
    out = tmp_path / "link.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        info = tarfile.TarInfo("dangling")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)

    with pytest.raises(Fatal) as exc:
        extract_archive(out, tmp_path / "out")
    assert "链接" in str(exc.value)


def test_extract_rejects_hardlink(tmp_path: Path):
    out = tmp_path / "hard.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        info = tarfile.TarInfo("linked")
        info.type = tarfile.LNKTYPE
        info.linkname = "target"
        tf.addfile(info)

    with pytest.raises(Fatal) as exc:
        extract_archive(out, tmp_path / "out")
    assert "链接" in str(exc.value)


def test_extract_rejects_windows_shortcut(tmp_path: Path):
    """`.lnk` 在不同机器上可能指向任意目标，没有进迁徙归档的理由。"""
    evil = _tar_with_member(tmp_path, "click.lnk")
    with pytest.raises(Fatal) as exc:
        extract_archive(evil, tmp_path / "out")
    assert "快捷方式" in str(exc.value)


# --------------------------------------------------------------------------
# restore_into：落位
# --------------------------------------------------------------------------


def test_restore_copies_payload_but_not_manifest(tmp_path: Path):
    gen = _make_gen_dir(tmp_path)
    target = tmp_path / "target" / "data"
    target.mkdir(parents=True)

    got = restore_into(gen, target, backup=False)

    assert (target / "data_v4.db").is_file()
    assert (target / "cmd_config.json").is_file()
    assert (
        target
        / "plugin_data"
        / "astrbot_plugin_memory_companion"
        / "memory_companion.db"
    ).is_file()
    # manifest 是"这份快照从哪来"的元数据，不属于 data/
    assert not (target / "manifest.json").exists()
    assert "manifest.json" in got["skipped"]
    assert "data_v4.db" in got["restored"]


def test_restore_backs_up_existing_files(tmp_path: Path):
    """原有文件必须移进备份目录，不能删——落位失败时人还要能退回去。"""
    gen = _make_gen_dir(tmp_path)
    target = tmp_path / "target" / "data"
    target.mkdir(parents=True)
    (target / "data_v4.db").write_bytes(b"OLD DB CONTENT")

    got = restore_into(gen, target, backup=True)

    assert (target / "data_v4.db").read_bytes() == (gen / "data_v4.db").read_bytes()
    assert got["backup_dir"]
    bak = Path(got["backup_dir"])
    assert bak.is_dir()
    assert (bak / "data_v4.db").read_bytes() == b"OLD DB CONTENT"


def test_restore_refuses_when_target_has_nonempty_wal(tmp_path: Path):
    """这是最危险的一条：目标库的 -wal 里躺着已提交事务，覆盖就是丢数据。"""
    gen = _make_gen_dir(tmp_path)
    target = tmp_path / "target" / "data"
    target.mkdir(parents=True)
    (target / "data_v4.db").write_bytes(b"OLD")
    (target / "data_v4.db-wal").write_bytes(b"\x00" * 4096)

    with pytest.raises(NeedsHuman) as exc:
        restore_into(gen, target)
    msg = str(exc.value)
    assert "未合并" in msg
    assert "SIGINT" in msg
    # 拒绝就必须真的什么都没动
    assert (target / "data_v4.db").read_bytes() == b"OLD"


def test_restore_allows_empty_wal_and_clears_stale_shm(tmp_path: Path):
    """空的 -wal 与陈旧 -shm 是可以处理的：移进备份目录再落位。"""
    gen = _make_gen_dir(tmp_path)
    target = tmp_path / "target" / "data"
    target.mkdir(parents=True)
    (target / "data_v4.db").write_bytes(b"OLD")
    (target / "data_v4.db-wal").write_bytes(b"")
    (target / "data_v4.db-shm").write_bytes(b"\x00" * 32768)

    got = restore_into(gen, target, backup=True)

    assert (target / "data_v4.db").read_bytes() == (gen / "data_v4.db").read_bytes()
    bak = Path(got["backup_dir"])
    assert (bak / "data_v4.db-wal").exists()
    assert (bak / "data_v4.db-shm").exists()
    # 落位后目标处不该再有陈旧的 shm
    assert not (target / "data_v4.db-shm").exists()


def test_restore_creates_missing_parent_dirs(tmp_path: Path):
    """目标机上可能还没装 memory_companion，目录不存在时要能建出来。"""
    gen = _make_gen_dir(tmp_path)
    target = tmp_path / "target" / "data"
    target.mkdir(parents=True)

    restore_into(gen, target, backup=False)

    assert (target / "plugin_data").is_dir()
    assert (target / "plugin_data" / "astrbot_plugin_memory_companion").is_dir()


def test_restore_refuses_missing_target_dir(tmp_path: Path):
    """目标 data 目录必须已存在——瞎建一个会让 AstrBot 用错的目录。"""
    gen = _make_gen_dir(tmp_path)
    with pytest.raises(NeedsHuman) as exc:
        restore_into(gen, tmp_path / "not-there")
    assert "目标 data 目录不存在" in str(exc.value)


def test_restore_refuses_missing_gen_dir(tmp_path: Path):
    target = tmp_path / "data"
    target.mkdir()
    with pytest.raises(NeedsHuman) as exc:
        restore_into(tmp_path / "nope", target)
    assert "快照目录不存在" in str(exc.value)


def test_restore_twice_is_stable(tmp_path: Path):
    """连续落位两次不该互相破坏（第二次把第一次的东西备份掉）。"""
    gen = _make_gen_dir(tmp_path)
    target = tmp_path / "target" / "data"
    target.mkdir(parents=True)

    restore_into(gen, target, backup=True)
    first = (target / "data_v4.db").read_bytes()

    # 时间戳目录名精确到秒，两次连续调用可能撞名；先等一下不会更可靠，
    # 所以直接接受"备份目录可能被复用"这个事实，只断言内容没坏。
    restore_into(gen, target, backup=True)
    assert (target / "data_v4.db").read_bytes() == first


def test_iter_payload_skips_wal_sidecars(tmp_path: Path):
    """`-shm` 和空 `-wal` 绝不能进迁徙归档。

    这条规则来自一次实测：`molt verify` 用只读连接打开 gen_dir 里的库做
    quick_check，而**任何**打开 wal 模式库的连接都会建出 `-shm`/`-wal`，
    只读连接关闭时删不掉。于是 sidecar 落进 gen_dir，被当成快照内容一起
    复制到了目标 data/。

    危害不是"多两个文件"，而是把 `-shm` 送到另一台机器等于人为制造一个
    "看起来像崩溃现场"的状态。所以它们必须在打包层就被拦掉。
    """
    gen = _make_gen_dir(tmp_path)
    (gen / "data_v4.db-shm").write_bytes(b"\x00" * 32768)
    (gen / "data_v4.db-wal").write_bytes(b"")

    files = _iter_payload_files(gen)
    names = {p.name for p in files}

    assert "data_v4.db" in names
    assert "data_v4.db-shm" not in names, "共享内存索引不该被搬走"
    assert "data_v4.db-wal" not in names, "空 WAL 不该被搬走"


def test_iter_payload_refuses_nonempty_wal(tmp_path: Path):
    """非空 WAL 不是噪音——它说明快照不自包含，必须报错而不是悄悄丢掉。"""
    gen = _make_gen_dir(tmp_path)
    (gen / "data_v4.db-wal").write_bytes(b"x" * 1024)

    with pytest.raises(Fatal) as ei:
        _iter_payload_files(gen)

    msg = str(ei.value)
    assert "未合并的事务" in msg
    assert "SIGINT" in msg, "错误消息必须告诉人怎么正确处理"


def test_restore_leaves_no_sidecars_in_target(tmp_path: Path):
    """落位结束后目标 data/ 里不该有任何 sidecar。

    `verify` 的只读 quick_check 会在快照目录里生成 sidecar，如果它们混进
    payload，目标机就会多出两个自己不认识的 `-shm`/`-wal`。这个测试端到端
    固定住"落位是干净的"。
    """
    gen = _make_gen_dir(tmp_path)
    # 模拟"verify 刚跑过"的现场
    (gen / "data_v4.db-shm").write_bytes(b"\x00" * 32768)
    (gen / "data_v4.db-wal").write_bytes(b"")

    target = tmp_path / "target" / "data"
    target.mkdir(parents=True)

    info = restore_into(gen, target, backup=True)

    assert (target / "data_v4.db").is_file()
    assert not (target / "data_v4.db-shm").exists(), "目标机不该收到 -shm"
    assert not (target / "data_v4.db-wal").exists(), "目标机不该收到空 -wal"
    assert "sidecars_cleaned" in info
