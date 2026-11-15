"""`tools/reload-plugin.py` 的测试。

为什么单独测它：配置同步写完文件之后，**插件还拿着旧配置**（`AstrBotConfig` 是插件
载入时构造的），所以"重载"不是可选项，是这个功能的后半截。而它走的是一条会真的
`Terminating plugin … → Loading plugin …` 的路径 —— 参数写错时不能靠"跑一下看看"。

这里只用桩：不连真 dashboard，也不依赖这台机器上有没有 AstrBot。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools"
MOD_PATH = TOOLS / "reload-plugin.py"


def _load():
    """按文件路径加载（`tools/` 不是包）。

    必须在 `exec_module` **之前**把模块塞进 `sys.modules`：文件里有
    `from __future__ import annotations`，dataclass / 类型注解解析时会
    `sys.modules.get(cls.__module__).__dict__`，拿不到就是 `AttributeError`。
    """
    name = "molt_reload_plugin_tool"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, MOD_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reload_tool = _load()


def _write_config(tmp_path: Path, secret: str = "s3cret", user: str = "hansei", *, bom: bool = True) -> Path:
    path = tmp_path / "cmd_config.json"
    body = json.dumps({"dashboard": {"jwt_secret": secret, "username": user}}, ensure_ascii=False)
    raw = body.encode("utf-8")
    if bom:  # 真机上的 cmd_config.json 带 BOM
        raw = b"\xef\xbb\xbf" + raw
    path.write_bytes(raw)
    return path


def test_dry_run_does_nothing(monkeypatch, tmp_path, capsys):
    cfg = _write_config(tmp_path)

    def _boom(*args, **kwargs):  # pragma: no cover - 只用来证明它没被调用
        raise AssertionError("空跑不该发任何请求")

    monkeypatch.setattr(reload_tool, "reload_one", _boom)
    rc = reload_tool.main(["--config", str(cfg), "--dry-run", "a", "b"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "hansei" in out
    assert "· a" in out and "· b" in out


def test_all_plugins_reloaded(monkeypatch, tmp_path, capsys):
    cfg = _write_config(tmp_path)
    calls = []

    def _ok(base, token, name, *, timeout):
        calls.append(name)
        return True, "HTTP 200 重载成功。"

    monkeypatch.setattr(reload_tool, "reload_one", _ok)
    rc = reload_tool.main(["--config", str(cfg), "a", "b"])

    assert rc == 0
    assert calls == ["a", "b"]
    assert "2 个插件已重新载入" in capsys.readouterr().out


def test_one_failure_is_reported_and_exit_code_is_4(monkeypatch, tmp_path, capsys):
    cfg = _write_config(tmp_path)

    def _mixed(base, token, name, *, timeout):
        return (name == "ok"), ("HTTP 200 ok" if name == "ok" else "HTTP 404 not found")

    monkeypatch.setattr(reload_tool, "reload_one", _mixed)
    rc = reload_tool.main(["--config", str(cfg), "ok", "bad"])

    captured = capsys.readouterr()
    assert rc == 4
    assert "没重载成功：bad" in captured.err


def test_missing_config_is_exit_2(tmp_path, capsys):
    rc = reload_tool.main(["--config", str(tmp_path / "nope.json"), "a"])

    assert rc == 2
    assert "找不到配置" in capsys.readouterr().err


def test_reload_one_posts_to_the_right_endpoint_with_a_bearer_token(monkeypatch):
    seen = {}

    class _Resp:
        status = 200

        def read(self):
            return b'{"status": "ok"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(request, timeout=0):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = request.data
        return _Resp()

    monkeypatch.setattr(reload_tool.urllib.request, "urlopen", _urlopen)
    ok, detail = reload_tool.reload_one("http://127.0.0.1:6199", "tok", "p1", timeout=5)

    assert ok, detail
    assert seen["url"] == "http://127.0.0.1:6199/api/plugin/reload"
    assert seen["method"] == "POST"
    assert seen["auth"] == "Bearer tok"
    assert json.loads(seen["body"]) == {"name": "p1"}
