"""陌生人冷启动复核钉住的三件事（v0.7）。

这三条测试的存在理由都一样：**"装得起来"是一个会退化的性质**，而它退化的
时候不会有任何红字 —— 只有别人在他自己的机器上装到一半撞墙。

1. 守护进程必须是包的一部分（`molt.daemon`）。它在 `tools/` 下住过很久，
   而 `tools/` 不进 wheel —— 于是 `pip install` 的人 `molt init --role agent`
   直接报「找不到 .../site-packages/tools/molt-agent.py」，连配置都写不出来。
2. `molt init` 必须在**没有 `tools/` 目录**的情况下也能验配置（安装模式）。
3. 时间轴必须能被一个还没有任何迁徙记录的人看见（`molt timeline --demo`），
   并且那一页上必须**写着**它是示例 —— 这一页会被截图转发。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from molt import initcfg, render
from molt.cli import main

REPO = Path(__file__).resolve().parent.parent.parent


def test_the_two_daemons_are_importable_packages():
    """守护进程得能被 `import` —— 这是"pip 装上就能跑"的最小前提。"""
    for kind in ("agent", "homer"):
        module = importlib.import_module(f"molt.daemon.{kind}")
        assert callable(module.main), kind
        assert hasattr(module, "Settings"), kind


def test_the_wheel_is_told_to_ship_the_daemons():
    """`pyproject.toml` 的 packages 必须列 `molt.daemon`。

    这条测试是给"有人为了干净又把 packages 改回 ['molt']"准备的：那样
    `.py` 文件还在仓库里、测试全绿、`pip install -e .` 也照样能跑，只有
    真正的 wheel 里少一个目录 —— 而发现它的方式，是别人装不上。
    """
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert '"molt.daemon"' in text or "'molt.daemon'" in text


def test_init_validates_without_a_tools_directory(tmp_path):
    """安装模式：`<molt_pkg>/tools` 不存在时，退到 `import molt.daemon.*`。

    这里把 `molt_pkg` 指到一个空目录，模拟 wheel 装出来的环境 —— 仓库里
    恰好有 `tools/`，所以仓库模式反而是"幸运路径"，不能只测它。
    """
    empty = tmp_path / "no-tools-here"
    empty.mkdir()
    module = initcfg.builder_module("agent", str(empty))
    assert callable(module.main)
    assert hasattr(module, "Settings")


def test_init_still_prefers_the_repo_copy_when_it_exists(tmp_path):
    """有 `tools/` 时优先用仓库里那份 —— 真机上的部署路径依赖这一点。"""
    pkg = tmp_path / "repo"
    (pkg / "tools").mkdir(parents=True)
    (pkg / "tools" / "molt-agent.py").write_text(
        "class Settings:  # 只用来证明用的是文件这份\n    pass\n"
        "def main(argv=None):\n    return 0\n",
        encoding="utf-8",
    )
    module = initcfg.builder_module("agent", str(pkg))
    assert module.__name__.startswith("_molt_init_agent")


def test_the_daemon_command_is_portable():
    """`next_steps()` 里给的那条命令必须能直接复制执行。

    它以前是 `python tools/molt-agent.py --config ...`：在作者机器上能跑，
    在 pip 装出来的环境里既没有 `tools/`、脚本也不在 PATH 里。
    """
    steps = initcfg.next_steps("agent", out=Path("./agent.json"), python="python")
    assert steps
    assert "molt agent --config agent.json --once" in steps[0]
    # 第一步是"照这个敲"，里面不能再出现只有仓库里才有的路径
    assert "molt-agent.py" not in steps[0]


def test_demo_timeline_renders_a_marked_example(tmp_path):
    """示例页必须自己说明它是示例。"""
    out = tmp_path / "demo.html"
    assert main(["--no-color", "timeline", "--demo", "--out", str(out)]) == 0
    page = out.read_text(encoding="utf-8")
    assert "示例数据" in page
    assert "它搬过几次家 · 示例" in page
    # 三条代次 + 备份 + 心跳都在，说明用的是同一套模板而不是另画一张图
    assert "第 3 代" in page and "中途备份" in page and "现在谁还在报" in page


def test_demo_data_does_not_leak_this_machines_real_shell_names():
    """示例用的是假壳名。

    真实的壳名（`ugreen-776b-8b7896df` / `desktop-h2kgiao`）落到截图里
    就是信息泄漏 —— 而这一页天生就是给人截图看的。
    """
    history = render.demo_history()
    shells = {g["source_host"] for g in history["generations"]}
    shells |= {g["target_shell"] for g in history["generations"]}
    shells |= set(history["heartbeats"])
    assert shells == {"nas-box", "my-laptop"}, shells


def test_the_note_is_escaped_not_injected():
    """`note` 走的是同一个 `esc()` —— 它迟早会被塞进别的东西。"""
    page = render.render_timeline({}, note="<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_real_timeline_has_no_example_banner(tmp_path):
    """真实页面不许带示例提示 —— 反过来也要钉住。"""
    page = render.render_timeline({"generations": [], "backups": [], "heartbeats": {}})
    assert "示例数据" not in page
    assert "这里没有一条是编的" in page


def test_the_repo_shims_still_expose_the_real_objects():
    """`tools/*.py` 垫片必须还活着（真机上的部署路径依赖它）。

    它靠 `globals().update(...)` 把真模块的名字搬过来；这条测试钉住的是
    "搬过来的就是同一个对象"，而不是一份看起来一样的副本。

    顺带说清它为什么不能当测试的加载目标：**垫片上的 monkeypatch 改不到
    真正执行的那份代码**（2026-11-15 那次"测试把机器睡过去"就是这么来的）。
    """
    import importlib.util

    from molt.daemon import agent as agent_impl, homer as homer_impl

    for script, impl in (("molt-agent.py", agent_impl), ("molt-homer.py", homer_impl)):
        path = REPO / "tools" / script
        name = f"_shim_{script.replace('.', '_')}"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
        assert getattr(module, "main") is impl.main
        # 两类都各有一个"主体"，垫片把它们搬过来就得是同一对象
        assert getattr(module, "Agent", None) is getattr(impl, "Agent", None)
        assert getattr(module, "Homer", None) is getattr(impl, "Homer", None)
