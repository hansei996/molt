"""`molt init` 的行为测试。

## 这个文件在守什么

`molt init` 是"陌生人装得起来"这件事的第一道门，它有三种骗人的方式：

  1. **写出一份看起来对、但连它自己的加载器都读不过的配置** —— 所以要
     真把写出来的文件交给守护进程的 `Settings.load()` 读一遍。
  2. **悄悄覆盖掉一份已经调通的配置** —— 一份调通的配置比一份"更新过的"
     值钱，覆盖必须是显式的 `--force`，而且旧的要留 `.bak`。
  3. **猜壳名** —— 账本里的 `target_shell` 是别人写下的字符串，猜错会让
     这台机器永远等不到自己的那一代。所以没有 `--shell` 就必须报错。

## 为什么用 `main([...])` 而不是直接调 `cmd_init`

`args` 的形状由 parser 定义，手搓 Namespace 会和真实调用漂移（漏一个
`default=SUPPRESS` 的参数就会在真机上炸）。走 `main()` 还能顺带测到子
命令注册本身。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from molt import initcfg
from molt.cli import main

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(*argv: str) -> int:
    return main(["--no-color", "init", *argv])


def _agent_args(tmp_path: Path, **over: str) -> list:
    base = {
        "--role": "agent",
        "--out": str(tmp_path / "agent.json"),
        "--shell": "desktop-test",
        "--data": str(tmp_path / "data"),
        "--warden-url": "http://127.0.0.1:6186",
        "--token": "tok-abc",
        "--python": sys.executable,
        "--molt-pkg": str(REPO_ROOT),
    }
    base.update(over)
    out: list = []
    for k, v in base.items():
        out.extend([k, v])
    return out


# --------------------------------------------------------------------------
# 模板与加载器
# --------------------------------------------------------------------------


def test_agent_config_passes_the_real_loader(tmp_path: Path) -> None:
    """写出来的东西必须能被守护进程自己的加载器读通。

    这是这个文件的中心：**生成器自己认为对不算数**。
    """
    assert _run(*_agent_args(tmp_path)) == 0
    path = tmp_path / "agent.json"
    assert path.is_file()
    problems = initcfg.validate_config(path, kind="agent", molt_pkg=str(REPO_ROOT))
    assert problems == []


def test_homer_config_passes_the_real_loader(tmp_path: Path) -> None:
    rc = _run(
        "--role",
        "homer",
        "--out",
        str(tmp_path / "homer.json"),
        "--shell",
        "nas-test",
        "--data",
        str(tmp_path / "data"),
        "--warden-url",
        "http://127.0.0.1:6186",
        "--token",
        "tok-abc",
        "--molt-pkg",
        str(REPO_ROOT),
    )
    assert rc == 0
    problems = initcfg.validate_config(
        tmp_path / "homer.json", kind="homer", molt_pkg=str(REPO_ROOT)
    )
    assert problems == []


def test_homer_listens_on_all_interfaces_by_default(tmp_path: Path) -> None:
    """容器里监听回环等于没有监听 —— 默认值必须是 0.0.0.0。"""
    assert (
        _run(
            "--role",
            "homer",
            "--out",
            str(tmp_path / "homer.json"),
            "--shell",
            "nas-test",
            "--data",
            str(tmp_path / "data"),
            "--warden-url",
            "http://127.0.0.1:6186",
            "--token",
            "tok",
            "--molt-pkg",
            str(REPO_ROOT),
        )
        == 0
    )
    data = json.loads((tmp_path / "homer.json").read_text(encoding="utf-8"))
    assert data["dashboard_host"] == "0.0.0.0"
    assert data["dashboard_port"] == 6185


def test_agent_starts_in_observe_only_mode(tmp_path: Path) -> None:
    """第一次装上只观察不动手：打开 execute 意味着这台机器会自己关机。"""
    assert _run(*_agent_args(tmp_path)) == 0
    data = json.loads((tmp_path / "agent.json").read_text(encoding="utf-8"))
    assert data["execute"] is False
    assert data["shell"] == "desktop-test"
    assert data["warden_token"] == "tok-abc"


def test_execute_false_is_not_treated_as_a_blocking_problem(tmp_path: Path) -> None:
    """加载器把「还没打开开关」也算 problem，但那不是配置错误。"""
    assert _run(*_agent_args(tmp_path)) == 0
    assert initcfg.validate_config(
        tmp_path / "agent.json", kind="agent", molt_pkg=str(REPO_ROOT)
    ) == []


# --------------------------------------------------------------------------
# 拒绝猜测
# --------------------------------------------------------------------------


def test_init_refuses_to_guess_the_shell_name(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    rc = _run(*_agent_args(tmp_path, **{"--shell": ""}))
    assert rc == 2
    out = capsys.readouterr().out
    assert "我不猜" in out
    # 顺便告诉人他这台机器默认会叫什么 —— 但只是提示，不是替他决定
    assert initcfg.hostname_shell_id() in out
    assert not (tmp_path / "agent.json").exists()


def test_init_refuses_without_data_dir(tmp_path: Path) -> None:
    assert _run(*_agent_args(tmp_path, **{"--data": ""})) == 2
    assert not (tmp_path / "agent.json").exists()


def test_init_refuses_without_warden_url(tmp_path: Path) -> None:
    assert _run(*_agent_args(tmp_path, **{"--warden-url": ""})) == 2
    assert not (tmp_path / "agent.json").exists()


def test_init_refuses_without_token(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert _run(*_agent_args(tmp_path, **{"--token": ""})) == 2
    assert "warden.token" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 不覆盖
# --------------------------------------------------------------------------


def test_init_does_not_overwrite_an_existing_config(tmp_path: Path) -> None:
    path = tmp_path / "agent.json"
    path.write_text('{"keep": "me"}\n', encoding="utf-8")
    assert _run(*_agent_args(tmp_path)) == 2
    assert json.loads(path.read_text(encoding="utf-8")) == {"keep": "me"}


def test_force_keeps_the_old_file_as_bak(tmp_path: Path) -> None:
    path = tmp_path / "agent.json"
    path.write_text('{"keep": "me"}\n', encoding="utf-8")
    assert _run(*_agent_args(tmp_path), "--force") == 0
    bak = path.with_name(path.name + ".bak")
    assert bak.is_file()
    assert json.loads(bak.read_text(encoding="utf-8")) == {"keep": "me"}
    assert json.loads(path.read_text(encoding="utf-8"))["shell"] == "desktop-test"


# --------------------------------------------------------------------------
# 验证失败就不落盘
# --------------------------------------------------------------------------


def test_init_writes_nothing_when_the_loader_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """生成器验不过就不许写盘 —— 半份配置比没有配置更难查。"""
    monkeypatch.setattr(initcfg, "validate_config", lambda *a, **k: ["molt_pkg 不存在：/nope"])
    assert _run(*_agent_args(tmp_path)) == 2
    assert not (tmp_path / "agent.json").exists()


def test_validate_config_accepts_a_pkg_without_tools_when_the_package_is_installed(
    tmp_path: Path,
) -> None:
    """`molt_pkg` 下没有 `tools/` 不再是错误 —— 那是 pip 装出来的正常形状。

    v1.0 之前守护进程只住在仓库 `tools/` 下，所以"找不到脚本"是一件必须
    拦下来的事。v1.1 把它们搬进了 `molt/daemon/`，安装模式下 `molt_pkg`
    甚至可以是空的；此时该走的是 `import molt.daemon.agent`。
    """
    assert _run(*_agent_args(tmp_path)) == 0
    problems = initcfg.validate_config(
        tmp_path / "agent.json", kind="agent", molt_pkg=str(tmp_path / "nowhere")
    )
    assert problems == [], problems


def test_validate_config_still_reports_a_package_that_cannot_be_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真找不到加载器时仍要报 —— 这才是这条检查原本要拦的东西。"""
    assert _run(*_agent_args(tmp_path)) == 0

    def _boom(name: str):
        raise ImportError(f"{name}: 故意失败")

    monkeypatch.setattr(initcfg.importlib, "import_module", _boom)
    problems = initcfg.validate_config(
        tmp_path / "agent.json", kind="agent", molt_pkg=str(tmp_path / "nowhere")
    )
    assert any("找不到" in p or "加载失败" in p for p in problems), problems


# --------------------------------------------------------------------------
# warden 角色
# --------------------------------------------------------------------------


def test_warden_role_writes_a_token_file(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = tmp_path / "warden-root"
    assert _run("--role", "warden", "--root", str(root)) == 0
    token = (root / "warden.token").read_text(encoding="utf-8").strip()
    assert len(token) >= 20
    out = capsys.readouterr().out
    # 打印的 serve 命令里必须真的带这个 token，否则人复制过去会 401
    assert token in out
    assert "--port 6186" in out


def test_warden_role_honours_a_given_token(tmp_path: Path) -> None:
    root = tmp_path / "warden-root"
    assert _run("--role", "warden", "--root", str(root), "--token", "given-token") == 0
    assert (root / "warden.token").read_text(encoding="utf-8").strip() == "given-token"


def test_two_wardens_get_different_tokens(tmp_path: Path) -> None:
    assert _run("--role", "warden", "--root", str(tmp_path / "a")) == 0
    assert _run("--role", "warden", "--root", str(tmp_path / "b")) == 0
    a = (tmp_path / "a" / "warden.token").read_text(encoding="utf-8")
    b = (tmp_path / "b" / "warden.token").read_text(encoding="utf-8")
    assert a != b


# --------------------------------------------------------------------------
# token 与壳名
# --------------------------------------------------------------------------


def test_new_token_has_no_shell_hostile_characters() -> None:
    """token 会进 JSON、URL、shell 变量 —— 特殊字符会被转义咬到。"""
    for _ in range(50):
        token = initcfg.new_token()
        assert token
        for ch in "=+/'\"$` \n\t":
            assert ch not in token, (ch, token)


def test_hostname_shell_id_is_lowercase() -> None:
    """账本里的壳名一直小写；大写会让同一个壳显示成两个。"""
    name = initcfg.hostname_shell_id()
    assert name == name.lower()
    assert name


def test_unknown_role_is_refused(tmp_path: Path) -> None:
    # argparse 会先拦住非法 choices（退出码 2），这里验的是它真拦得住
    with pytest.raises(SystemExit) as ei:
        _run("--role", "butler", "--out", str(tmp_path / "x.json"))
    assert ei.value.code == 2
