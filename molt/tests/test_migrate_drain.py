"""`migrate --control-url` 的测试：先按住源壳，再搬灵魂。

## 这个文件在测什么

测的不是"能不能 drain"——那是 `test_plugin_control.py` 的活。这里测的是
**`cmd_migrate` 里那个顺序和那些失败分支**，因为它们是公理 0 的最后一道闸：

  1. 没有 `--control-url` 时必须**明说**源壳不会停（沉默的放行最危险）
  2. 有 `--control-url` 时，drain 必须在热备**之前**（快照才是"最后一句话"）
  3. drain 失败 → 不许继续，且租约要还回去（继续就是两条鱼）
  4. drain 成功但后续失败 → 必须 **resume** 把源壳放回来说话
  5. 给了地址不给凭据 → 报错退出，不是猜一个

第 4 条是这里最容易被写错的一条：drain 之后我们主动造出了一个"哑掉的鱼"，
如果迁徙半路失败还不管它，用户看到的现象是"我跑了个命令，机器人就不回我了"
——而日志里一句错都没有。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from molt.cli import main
from molt.errors import EpochStale, MoltError, NeedsHuman
from molt.warden import Warden


# --------------------------------------------------------------------------
# 夹具
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


def _read_state(root: Path) -> dict:
    return json.loads((root / "state.json").read_text(encoding="utf-8"))


class FakeControl:
    """记下每一次调用，并按剧本决定成功还是失败。

    `calls` 里存的是 `(动作名, 当时 gen-000001 目录是否已存在)`——
    第二个字段就是为了断言"drain 在热备之前"。
    """

    def __init__(
        self,
        *,
        root: Path,
        drain_error: Exception | None = None,
        resume_error: Exception | None = None,
        drained: List[str] | None = None,
    ) -> None:
        self.root = root
        self.drain_error = drain_error
        self.resume_error = resume_error
        self.drained = ["default_1234567890"] if drained is None else drained
        self.calls: List[Tuple[str, bool]] = []

    def where(self) -> str:
        return "http://fake-control"

    def drain_and_verify(self, **kwargs: Any) -> Dict[str, Any]:
        gen_dir_exists = (self.root / "staging" / "gen-000001").exists()
        self.calls.append(("drain", gen_dir_exists))
        if self.drain_error is not None:
            raise self.drain_error
        return {
            "ok": True,
            "drained": self.drained,
            "still_connected": 0,
            "verified_silent": True,
        }

    def resume(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(("resume", (self.root / "staging" / "gen-000001").exists()))
        if self.resume_error is not None:
            raise self.resume_error
        return {"ok": True, "resumed": list(self.drained)}


@pytest.fixture
def wired(monkeypatch):
    """把 `_control_client` 换成受控的假货，并返回一个"装好后取回来"的函数。"""

    def _install(control: FakeControl | None, *, error: Exception | None = None):
        def fake_client(args):
            if error is not None:
                raise error
            return control

        monkeypatch.setattr("molt.cli._control_client", fake_client)
        return control

    return _install


def _argv(data: Path, root: Path, *extra: str) -> List[str]:
    return [
        "migrate",
        # 这些用例验的不是「目标在不在」那道闸门（见 test_migrate_wakeable.py）。
        "--skip-target-check",
        # 也不验"交出去之后等目标接管"（那条见 test_migrate_confirm.py）。
        "--no-wait-confirm",
        "--data",
        str(data),
        "--shell",
        "shell-a",
        "--root",
        str(root),
        "--target",
        "shell-b",
        "--no-color",
        *extra,
    ]


# --------------------------------------------------------------------------
# 1) 没给 --control-url：明说源壳不会停
# --------------------------------------------------------------------------


def test_no_control_url_does_not_claim_shell_is_silent(tmp_path: Path, capsys):
    """没按过它，就绝不能告诉用户"它现在是哑的"。

    这条比它看起来重要：drain 之后那句"源壳现在是哑的"是**唯一**会让用户
    不去启动第二个壳的提示。反过来，如果没 drain 也说这句话，用户会白等
    一个本来就活着的机器人，然后以为整个工具坏了。
    """
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"

    rc = main(_argv(data, root))
    assert rc == 0

    out = capsys.readouterr().out
    assert "源壳现在是哑的" not in out


def test_no_control_url_warns_loudly(tmp_path: Path, capsys):
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"

    rc = main(_argv(data, root))
    assert rc == 0

    out = capsys.readouterr().out
    # 必须是**警告**，不能只是下面那几行 dim 提示。
    # dim 的每一行都以两个空格缩进，warn 的行不是——这样才区分得开
    # （变异测试第一轮就是在这个点上漏的：`"--control-url" in out`
    # 被下面那行 `dim("    --control-url ...")` 满足了）。
    assert any(
        "--control-url" in line and not line.startswith(" ") for line in out.splitlines()
    ), "缺参数这件事必须当成警告说出来，而不是缩在提示里"
    assert "公理 0" in out, "必须把后果说成公理 0，不是'可能会冲突'"
    # 没给地址就不该有任何 drain 动作
    st = _read_state(root)
    assert st["fenced_state"]["lease"] is None


# --------------------------------------------------------------------------
# 2) 给了地址：drain 必须在热备之前
# --------------------------------------------------------------------------


def test_drain_happens_before_checkout(tmp_path: Path, wired, capsys):
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    ctl = wired(FakeControl(root=root))

    rc = main(_argv(data, root, "--control-url", "http://127.0.0.1:6185"))
    assert rc == 0

    assert [c[0] for c in ctl.calls] == ["drain"], "成功路径只该 drain 一次，不该 resume"
    assert ctl.calls[0][1] is False, (
        "drain 时 gen-000001 目录必须还不存在——"
        "否则说明我们先把灵魂拍了照，然后才让它闭嘴，中间那几条消息丢了"
    )

    out = capsys.readouterr().out
    assert "按住源壳" in out
    assert "源壳现在是哑的" in out, "用户必须被告知机器人为什么突然不回话了"

    # 迁徙本身仍然完整
    st = _read_state(root)
    assert len(st["generations"]) == 1


# --------------------------------------------------------------------------
# 3) drain 失败：不许继续
# --------------------------------------------------------------------------


def test_drain_failure_aborts_and_releases_lease(tmp_path: Path, wired, capsys):
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    ctl = wired(
        FakeControl(
            root=root,
            drain_error=NeedsHuman("drain 之后它仍在对外说话。不要继续这次迁徙"),
        )
    )

    rc = main(_argv(data, root, "--control-url", "http://127.0.0.1:6185"))

    assert rc == 8, "这是'按不住源壳'的专属退出码，要和普通失败区分开"
    assert [c[0] for c in ctl.calls] == ["drain"], (
        "drain 没成功就不该 resume——它压根没被按下去过，"
        "resume 会去重建一个本来就活着的平台"
    )

    st = _read_state(root)
    assert st["fenced_state"]["lease"] is None, "按不住也要把租约还回去"
    assert st["generations"] == [], "不许留下任何 generation"

    out = capsys.readouterr().out
    assert "两条鱼同时在线" in out


# --------------------------------------------------------------------------
# 4) drain 成功但热备失败：必须 resume
# --------------------------------------------------------------------------


def test_resumes_source_when_backup_fails(tmp_path: Path, wired, capsys):
    data = _make_source_data(tmp_path)
    # 把这个库弄坏，热备会在它身上抛 Fatal
    (data / "data_v4.db").write_bytes(b"this is definitely not a sqlite database")
    root = tmp_path / "warden"
    ctl = wired(FakeControl(root=root))

    rc = main(_argv(data, root, "--control-url", "http://127.0.0.1:6185"))

    assert rc != 0
    assert [c[0] for c in ctl.calls] == ["drain", "resume"], (
        "drain 成功了就必须负责把它放回来"
    )

    st = _read_state(root)
    assert st["fenced_state"]["lease"] is None

    out = capsys.readouterr().out
    assert "源壳已恢复" in out


# --------------------------------------------------------------------------
# 5) drain 成功但 push 被拒：同样要 resume
# --------------------------------------------------------------------------


def test_resumes_source_when_push_fails(tmp_path: Path, wired, monkeypatch, capsys):
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    ctl = wired(FakeControl(root=root))

    def boom(self, shell, epoch, manifest_dict):
        raise EpochStale("这个 epoch 已经过期了")

    monkeypatch.setattr("molt.cli.LocalBackend.push", boom)

    rc = main(_argv(data, root, "--control-url", "http://127.0.0.1:6185"))

    # `EpochStale` 继承 `Fatal`（见 errors.py），所以走的是 4 号分支。
    assert rc == 4
    assert [c[0] for c in ctl.calls] == ["drain", "resume"]
    assert _read_state(root)["fenced_state"]["lease"] is None


def test_resumes_source_when_push_raises_generic_molt_error(
    tmp_path: Path, wired, monkeypatch, capsys
):
    """非 `Fatal` 的 `MoltError` 走 7 号分支——两条路都要 resume。"""
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"
    ctl = wired(FakeControl(root=root))

    def boom(self, shell, epoch, manifest_dict):
        raise MoltError("账本状态说不清")

    monkeypatch.setattr("molt.cli.LocalBackend.push", boom)

    rc = main(_argv(data, root, "--control-url", "http://127.0.0.1:6185"))

    assert rc == 7
    assert [c[0] for c in ctl.calls] == ["drain", "resume"]


# --------------------------------------------------------------------------
# 6) resume 也失败：不能假装成功
# --------------------------------------------------------------------------


def test_says_so_when_resume_also_fails(tmp_path: Path, wired, capsys):
    data = _make_source_data(tmp_path)
    (data / "data_v4.db").write_bytes(b"still not a database")
    root = tmp_path / "warden"
    wired(
        FakeControl(root=root, resume_error=NeedsHuman("控制面没响应"))
    )

    rc = main(_argv(data, root, "--control-url", "http://127.0.0.1:6185"))
    assert rc != 0

    out = capsys.readouterr().out
    assert "源壳现在是哑的" in out, (
        "恢复失败时必须明说'它现在不会说话'——"
        "这是唯一能让用户不去启动另一个壳的提示"
    )


# --------------------------------------------------------------------------
# 7) 给了地址不给凭据：报错，不猜
# --------------------------------------------------------------------------


def test_control_url_without_credentials_is_refused(tmp_path: Path, monkeypatch, capsys):
    data = _make_source_data(tmp_path)
    root = tmp_path / "warden"

    # 用真的 _control_client（不给 patch），只给 url 不给 config/token
    rc = main(_argv(data, root, "--control-url", "http://127.0.0.1:6185"))

    assert rc == 2
    out = capsys.readouterr().out
    assert "--control-config" in out

    st = _read_state(root)
    assert st["fenced_state"]["lease"] is None, "参数错误不该占住租约"


def test_control_client_returns_none_without_url():
    import argparse

    from molt.cli import _control_client

    assert _control_client(argparse.Namespace()) is None
    assert _control_client(argparse.Namespace(control_url="")) is None


def test_control_client_accepts_token(tmp_path: Path):
    import argparse

    from molt.cli import _control_client

    c = _control_client(
        argparse.Namespace(control_url="http://x:1", control_token="a.b.c")
    )
    assert c is not None
    assert "http://x:1" in c.where()
