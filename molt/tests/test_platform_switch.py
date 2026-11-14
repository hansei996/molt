"""`tools/platform-switch.py` 的测试。

## 为什么一个"工具脚本"值得单独测

因为它能**让你的一只鱼开始对外说话**。

`molt land` 落位后一定把 `platform[].enable` 关掉（`overlay.py` 覆写表第 0 条），
而把它重新打开的唯一途径就是这个脚本（或人手改 JSON）。换句话说：

- 这个脚本写错了 → 你以为它开了，其实没开 → 鱼不说话（能查出来，烦）
- 这个脚本**多改了东西** → `appid` / `jwt_secret` / 别的配置被顺手改掉 →
  **一只带着错凭据的鱼连上 QQ**，或者 dashboard 的签名换了导致控制面全部 401（难查得多）

所以这里的测试不测"能不能开"，重点测三件事：
1. **dry-run 真的不写**（默认值必须安全）
2. **只动 `platform[].enable` 这一个字段**（逐键对比，不是"看起来对"）
3. **BOM 进 BOM 出**（少了它 Linux 上直接 JSONDecodeError）

`tools/` 不是包（没有 `__init__.py`，也不该有——它是仓库根的一次性脚本区），
所以用 importlib 按文件路径加载。这也顺带证明了**这个脚本不依赖包结构**，
可以单独拷一台机器上跑（NAS 上就是这么用的）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "platform-switch.py"


def _load_tool():
    assert TOOL.is_file(), f"找不到 {TOOL}"
    spec = importlib.util.spec_from_file_location("platform_switch_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


psw = _load_tool()


# --------------------------------------------------------------------------- 夹具


def _config(platforms=None):
    return {
        "config_version": 5,
        "platform": platforms
        if platforms is not None
        else [
            {
                "id": "default_1234567890",
                "type": "qq_official",
                "enable": True,
                "appid": "1234567890",
                "secret": "should-not-be-touched",
            }
        ],
        "dashboard": {"host": "0.0.0.0", "port": 6185, "jwt_secret": "x" * 43},
        "provider": [{"id": "deepseek", "type": "openai_chat_completion"}],
    }


def _write(path: Path, data, *, bom: bool = True) -> bytes:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    raw = text.encode("utf-8")
    if bom:
        raw = b"\xef\xbb\xbf" + raw
    path.write_bytes(raw)
    return raw


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def cfg(tmp_path):
    p = tmp_path / "cmd_config.json"
    _write(p, _config())
    return p


# --------------------------------------------------------------------------- 安全默认


def test_show_does_not_write(cfg):
    before = _sha(cfg)
    assert psw.main(["show", str(cfg)]) == 0
    assert _sha(cfg) == before


def test_off_without_yes_does_not_write(cfg):
    """默认必须安全：不写这一条错了，就等于"跑一次 `off` 看会改什么"真的把它关掉了。"""
    before = _sha(cfg)
    assert psw.main(["off", str(cfg)]) == 0
    assert _sha(cfg) == before
    assert json.loads(cfg.read_text(encoding="utf-8-sig"))["platform"][0]["enable"] is True


def test_on_without_yes_does_not_write(cfg):
    _write(cfg, _config([{"id": "q", "type": "qq_official", "enable": False}]))
    before = _sha(cfg)
    assert psw.main(["on", str(cfg)]) == 0
    assert _sha(cfg) == before


# --------------------------------------------------------------------------- 真的写


def test_off_with_yes_flips_enable(cfg):
    assert psw.main(["off", str(cfg), "--yes"]) == 0
    data = json.loads(cfg.read_text(encoding="utf-8-sig"))
    assert data["platform"][0]["enable"] is False


def test_on_with_yes_flips_enable(cfg):
    _write(cfg, _config([{"id": "q", "type": "qq_official", "enable": False}]))
    assert psw.main(["on", str(cfg), "--yes"]) == 0
    data = json.loads(cfg.read_text(encoding="utf-8-sig"))
    assert data["platform"][0]["enable"] is True


def test_only_platform_key_changes(cfg):
    """逐键对比。这一条就是这个脚本存在理由的全部。"""
    before = json.loads(cfg.read_text(encoding="utf-8-sig"))
    assert psw.main(["off", str(cfg), "--yes"]) == 0
    after = json.loads(cfg.read_text(encoding="utf-8-sig"))

    differing = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert differing == {"platform"}


def test_credentials_and_secret_survive(cfg):
    assert psw.main(["off", str(cfg), "--yes"]) == 0
    data = json.loads(cfg.read_text(encoding="utf-8-sig"))

    plat = data["platform"][0]
    assert plat["appid"] == "1234567890"
    assert plat["secret"] == "should-not-be-touched"
    assert plat["type"] == "qq_official"
    assert data["dashboard"]["jwt_secret"] == "x" * 43
    assert data["config_version"] == 5


def test_bom_is_preserved(cfg):
    """少了这一条，Linux 上 `json.load(open(p))` 直接 Unexpected UTF-8 BOM。"""
    assert cfg.read_bytes().startswith(b"\xef\xbb\xbf")
    assert psw.main(["off", str(cfg), "--yes"]) == 0
    assert cfg.read_bytes().startswith(b"\xef\xbb\xbf")


def test_reads_bomless_file(tmp_path):
    """反方向也要成立：不带 BOM 的文件不能被写坏成带 BOM。"""
    p = tmp_path / "cmd_config.json"
    _write(p, _config(), bom=False)
    assert not p.read_bytes().startswith(b"\xef\xbb\xbf")
    assert psw.main(["off", str(p), "--yes"]) == 0
    assert not p.read_bytes().startswith(b"\xef\xbb\xbf")


def test_idempotent(cfg):
    assert psw.main(["off", str(cfg), "--yes"]) == 0
    after_first = _sha(cfg)
    assert psw.main(["off", str(cfg), "--yes"]) == 0
    assert _sha(cfg) == after_first, "已经是目标状态时不该再重写一遍文件"


def test_round_trip_restores_original_semantics(cfg):
    before = json.loads(cfg.read_text(encoding="utf-8-sig"))
    assert psw.main(["off", str(cfg), "--yes"]) == 0
    assert psw.main(["on", str(cfg), "--yes"]) == 0
    assert json.loads(cfg.read_text(encoding="utf-8-sig")) == before


# --------------------------------------------------------------------------- 多个平台


def test_all_platforms_are_switched(cfg):
    _write(
        cfg,
        _config(
            [
                {"id": "qq", "type": "qq_official", "enable": True},
                {"id": "tg", "type": "telegram", "enable": True},
                {"id": "off", "type": "weixin_oc", "enable": False},
            ]
        ),
    )
    assert psw.main(["off", str(cfg), "--yes"]) == 0
    data = json.loads(cfg.read_text(encoding="utf-8-sig"))
    assert [p["enable"] for p in data["platform"]] == [False, False, False]


# --------------------------------------------------------------------------- 坏输入


def test_missing_file_is_exit_2(tmp_path):
    assert psw.main(["show", str(tmp_path / "nope.json")]) == 2


def test_bad_json_is_exit_2(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    assert psw.main(["show", str(p)]) == 2


def test_non_object_json_is_exit_2(tmp_path):
    p = tmp_path / "arr.json"
    p.write_text("[1,2,3]", encoding="utf-8")
    assert psw.main(["show", str(p)]) == 2


def test_no_platform_section_is_exit_1(tmp_path):
    """配置里没有 platform[] 时**不能**假装成功——那会让人以为"开关过了"。"""
    p = tmp_path / "cmd_config.json"
    _write(p, {"config_version": 5})
    assert psw.main(["on", str(p), "--yes"]) == 1


def test_platform_entries_that_are_not_objects_are_skipped(tmp_path):
    p = tmp_path / "cmd_config.json"
    _write(p, {"platform": ["garbage", {"id": "q", "type": "qq_official", "enable": True}]})
    assert psw.main(["off", str(p), "--yes"]) == 0
    data = json.loads(p.read_text(encoding="utf-8-sig"))
    assert data["platform"][1]["enable"] is False
    assert data["platform"][0] == "garbage", "不是对象的条目不该被动手"


# --------------------------------------------------------------------------- 输出


def test_console_setup_never_raises_on_narrow_encoding(monkeypatch):
    """`✓` 不在 GBK 里。这一步要是抛，就会出现"文件改成了但打印时崩掉"。"""

    class Narrow:
        encoding = "gbk"

        def reconfigure(self, **kw):
            raise UnicodeError("no")

        def write(self, s):
            return len(s)

        def flush(self):
            pass

    monkeypatch.setattr(psw.sys, "stdout", Narrow())
    monkeypatch.setattr(psw.sys, "stderr", Narrow())
    psw._setup_console()  # 不该抛
    # 降级之后这几个符号必须是 GBK 能编码的
    (psw.OK + psw.BAD + psw.DOT).encode("gbk")
