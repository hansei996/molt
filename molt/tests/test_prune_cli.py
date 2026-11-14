
"""
test_prune_cli.py -- `molt prune` 命令与 `POST /prune` 端点。

## 为什么要单独测这两个

`Warden.prune_archives()` 已经有 22 个测试（见 test_prune.py）。但那只是
**业务规则**。真正会出事的地方在接线上：

1. **CLI 的默认值。** 一个人敲 `molt prune` 时，它必须只报告不删。
   如果哪天有人"顺手"把 `dry_run=not args.yes` 写成 `dry_run=False`，
   测试得挡住它——那是这个项目里唯一会真删用户数据的路径。
2. **HTTP 端点的默认值。** 同理。而且端点要能在 JSON body 里
   不带任何字段就工作（`{}` 是最自然的调用形状）。
3. **`--root` + `--warden-url` 同时给。** 这是本项目一贯的拒绝项，
   prune 也不该例外——含糊地猜"该跟谁说话"是分布式系统出事的典型起点。
"""

import json
from pathlib import Path

import pytest

from molt.cli import main
from molt.server import WardenServer
from molt.warden import Warden


def _touch(w, gen, size=1000):
    d = w.archive_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"gen-{gen:06d}.tar.gz"
    p.write_bytes(b"x" * size)
    return p


# ── CLI ──────────────────────────────────────────────────────────────


def test_prune_cli_defaults_to_report_only(tmp_path, capsys):
    """敲 `molt prune` 不加 --yes，**盘上的文件必须一个不少**。"""
    root = tmp_path / "warden"
    w = Warden(root=root)
    for g in (1, 2, 3):
        _touch(w, g)

    rc = main(["prune", "--root", str(root), "--no-color"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "1" in out  # 将要删 1 份（3 份保留 2 份）
    assert "没动手" in out or "将要删除" in out
    assert "想真的删" in out or "--yes" in out
    for g in (1, 2, 3):
        assert (w.archive_dir() / f"gen-{g:06d}.tar.gz").exists(), (
            "dry-run 竟然删了文件 —— 这是这个项目里最不能出的错"
        )


def test_prune_cli_yes_actually_deletes(tmp_path, capsys):
    root = tmp_path / "warden"
    w = Warden(root=root)
    for g in (1, 2, 3, 4):
        _touch(w, g)

    rc = main(["prune", "--root", str(root), "--keep-recent", "1", "--yes", "--no-color"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "已删除" in out
    names = sorted(p.name for p in w.archive_dir().iterdir())
    assert names == ["gen-000004.tar.gz"]


def test_prune_cli_keep_recent_zero(tmp_path, capsys):
    root = tmp_path / "warden"
    w = Warden(root=root)
    for g in (1, 2):
        _touch(w, g)

    rc = main(["prune", "--root", str(root), "--keep-recent", "0", "--yes", "--no-color"])
    assert rc == 0
    assert list(w.archive_dir().iterdir()) == []


def test_prune_cli_rejects_negative_keep_recent(tmp_path, capsys):
    root = tmp_path / "warden"
    Warden(root=root)
    rc = main(["prune", "--root", str(root), "--keep-recent", "-1", "--no-color"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "不能是负数" in out


def test_prune_cli_rejects_ambiguous_backend(tmp_path, capsys):
    """`--root` 与 `--warden-url` 同时给 → 拒绝，不猜。"""
    rc = main(
        [
            "prune",
            "--root",
            str(tmp_path),
            "--warden-url",
            "http://127.0.0.1:6186",
            "--warden-token",
            "t",
            "--no-color",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 2
    assert "无法判断" in out or "同时" in out


def test_prune_cli_empty_message_is_honest(tmp_path, capsys):
    """一份都没有时说"没有可清理的"，不要假装做了什么。"""
    root = tmp_path / "warden"
    rc = main(["prune", "--root", str(root), "--no-color"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "没有可清理的归档" in out


# ── HTTP 端点 ─────────────────────────────────────────────────────────


def test_prune_endpoint_defaults_to_dry_run(tmp_path):
    """`POST /prune` 空 body → dry_run。**端点默认值也不能删。**"""
    w = Warden(root=tmp_path / "warden")
    for g in (1, 2, 3):
        _touch(w, g)
    srv = WardenServer(root=tmp_path / "warden", token="tok", port=0)

    resp = srv.dispatch("POST", "/prune", {})
    assert resp.status == 200
    assert resp.payload["dry_run"] is True
    assert len(resp.payload["removed"]) == 1
    for g in (1, 2, 3):
        assert (w.archive_dir() / f"gen-{g:06d}.tar.gz").exists()


def test_prune_endpoint_can_delete(tmp_path):
    w = Warden(root=tmp_path / "warden")
    for g in (1, 2, 3):
        _touch(w, g)
    srv = WardenServer(root=tmp_path / "warden", token="tok", port=0)

    resp = srv.dispatch("POST", "/prune", {"keep_recent": 1, "dry_run": False})
    assert resp.status == 200
    assert resp.payload["dry_run"] is False
    assert [x["name"] for x in resp.payload["removed"]] == [
        "gen-000001.tar.gz",
        "gen-000002.tar.gz",
    ]
    assert sorted(p.name for p in w.archive_dir().iterdir()) == ["gen-000003.tar.gz"]


def test_prune_endpoint_rejects_bad_keep_recent(tmp_path):
    srv = WardenServer(root=tmp_path / "warden", token="tok", port=0)
    resp = srv.dispatch("POST", "/prune", {"keep_recent": "abc"})
    assert resp.status == 400
    assert resp.payload["kind"] == "bad_request"


def test_prune_endpoint_wrong_method_is_404(tmp_path):
    """`GET /prune` 不存在 —— 清理是动作，不是资源。"""
    srv = WardenServer(root=tmp_path / "warden", token="tok", port=0)
    resp = srv.dispatch("GET", "/prune", None)
    assert resp.status == 404


def test_list_archives_endpoint_marks_referenced(tmp_path):
    """
    `GET /archives` 必须带 `referenced` 标记。

    这就是"复用 Warden.list_archives() 而不是自己扫目录"的理由：
    如果服务端只回报文件名和大小，`molt prune` 就没法知道哪些能删。
    """
    w = Warden(root=tmp_path / "warden")
    _touch(w, 1)
    _touch(w, 2)
    srv = WardenServer(root=tmp_path / "warden", token="tok", port=0)

    resp = srv.dispatch("GET", "/archives", None)
    assert resp.status == 200
    items = {x["name"]: x for x in resp.payload["archives"]}
    assert "referenced" in items["gen-000001.tar.gz"]
    assert "deletable" in items["gen-000001.tar.gz"]
    assert items["gen-000001.tar.gz"]["generation"] == 1
