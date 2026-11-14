"""针对 shell.py 的测试。

关注点是**判断逻辑**，不是"能不能跑"：

  - arm 丢了 → self_heal（唯一可自愈的分支）
  - ready_to_wake=false 但网卡已 armed → block，且明确说"不要瞎修"
  - 必需能力缺失 → block，且优先于 WOL 判断
  - 探测不到的东西记为未知，绝不能默认成成功
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from molt.errors import CapabilityMissing
from molt.shell import (
    CAPABILITY_DOCS,
    Capabilities,
    Preflight,
    ShellIdentity,
    _norm_hostname,
    detect_container,
    detect_platform,
    machine_fingerprint,
    preflight,
    probe_capabilities,
    probe_identity,
    read_astrbot_version,
)


def make_identity(**over) -> ShellIdentity:
    base = dict(
        shell_id="win-shell-1a2b3c4d",
        hostname="DESKTOP-1A2B3C4D",
        platform="windows",
        python_version="3.13.5",
        python_executable=r"G:\molt-p0\venv313\Scripts\python.exe",
        is_container=False,
        astrbot_root=r"G:\molt-p0\root",
        data_path=r"G:\molt-p0\root\data",
        astrbot_version="4.29.4",
    )
    base.update(over)
    return ShellIdentity(**base)


def make_caps(**over) -> Capabilities:
    base = dict(
        soul_read=True,
        soul_write=True,
        shell_identity=True,
        can_send_magic_packet=True,
        can_be_woken=True,
        wake_armed=True,
        has_interactive_desktop=True,
        has_gpu=True,
        astrbot_installed=True,
    )
    base.update(over)
    return Capabilities(**base)


# --------------------------------------------------------------------------
# shell_id
# --------------------------------------------------------------------------


def test_norm_hostname_strips_illegal_chars():
    assert _norm_hostname("DESKTOP-1A2B3C4D") == "desktop-1a2b3c4d"
    # 下划线映射为 '-'，空格直接丢弃（不是替换），所以是 my-hostname 而非 my-host-name。
    # 这不影响可用性：shell_id 只需要稳定、可读、可比较，
    # 不需要"每个分隔符都保留成一个分隔符"。
    assert _norm_hostname("my_host name") == "my-hostname"
    assert _norm_hostname("a..b") == "a-b"


def test_norm_hostname_collapses_repeated_separators():
    assert _norm_hostname("a___b") == "a-b"
    assert _norm_hostname("---x---") == "x"


def test_norm_hostname_handles_empty_and_truncates():
    assert _norm_hostname("") == "unknown"
    assert _norm_hostname("!!!") == "unknown"
    long = "a" * 200
    assert len(_norm_hostname(long)) == 40


def test_machine_fingerprint_is_stable_and_short():
    a = machine_fingerprint()
    b = machine_fingerprint()
    assert a == b
    assert len(a) == 8
    assert all(c in "0123456789abcdef" for c in a)


def test_shell_id_differs_between_nas_and_windows():
    """两台机器的 shell_id 必须不同——否则 fencing 的 shell_id 校验形同虚设。"""
    nas = make_identity(
        shell_id="ugreen-776b-deadbeef", hostname="UGREEN-776B", platform="linux"
    )
    win = make_identity()
    assert nas.shell_id != win.shell_id


def test_probe_identity_produces_usable_id():
    ident = probe_identity()
    assert ident.shell_id
    assert "-" in ident.shell_id
    assert ident.platform in ("linux", "windows", "darwin")
    assert ident.python_version.count(".") == 2


# --------------------------------------------------------------------------
# 平台 / 容器 / AstrBot 版本
# --------------------------------------------------------------------------


def test_detect_platform_maps_current_platform():
    either = detect_platform()
    if sys.platform == "win32":
        assert either == "windows"
    elif sys.platform == "darwin":
        assert either == "darwin"
    else:
        assert either == "linux"


def test_detect_container_returns_bool():
    assert isinstance(detect_container(), bool)


def test_read_astrbot_version_from_fake_source(tmp_path: Path):
    pkg = tmp_path / "astrbot"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        '# comment\n__version__ = "4.29.4"\n', encoding="utf-8"
    )
    assert read_astrbot_version(tmp_path) == "4.29.4"


def test_read_astrbot_version_single_quotes(tmp_path: Path):
    pkg = tmp_path / "astrbot"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("__version__='1.2.3'\n", encoding="utf-8")
    assert read_astrbot_version(tmp_path) == "1.2.3"


def test_read_astrbot_version_missing_returns_none(tmp_path: Path):
    assert read_astrbot_version(tmp_path) is None
    assert read_astrbot_version(None) is None


# --------------------------------------------------------------------------
# 必需能力
# --------------------------------------------------------------------------


def test_missing_required_empty_when_all_present():
    assert make_caps().missing_required() == []


def test_missing_required_lists_each_gap():
    caps = make_caps(soul_read=False, soul_write=False, shell_identity=False)
    assert caps.missing_required() == ["soul.read", "soul.write", "shell.identity"]


def test_assert_required_raises_with_human_readable_reason():
    caps = make_caps(soul_read=False)
    with pytest.raises(CapabilityMissing) as ei:
        caps.assert_required()
    msg = str(ei.value)
    assert "soul.read" in msg
    # 必须带上人类可读的解释，而不只是能力代号
    assert CAPABILITY_DOCS["soul.read"] in msg


def test_assert_required_passes_silently():
    make_caps().assert_required()      # 不应抛


# --------------------------------------------------------------------------
# preflight 契约表（这是本模块最重要的行为）
# --------------------------------------------------------------------------


def test_preflight_proceed_when_everything_ready():
    pf = preflight(make_identity(), make_caps())
    assert pf.action == "proceed"
    assert pf.ok
    assert pf.blocking == []
    assert "WOL 就绪" in pf.explain()


def test_preflight_self_heal_when_arm_lost():
    """arm 丢了 → self_heal，这是唯一允许自动修的分支（ARCH §14 契约表）。"""
    caps = make_caps(can_be_woken=False, wake_armed=False)
    pf = preflight(make_identity(), caps)

    assert pf.action == "self_heal"
    assert not pf.ok
    assert any("唤醒白名单" in b for b in pf.blocking)
    # 必须给出具体的自愈手段，而不是只说"有问题"
    assert "wol-guard-worker.ps1" in pf.explain()


def test_preflight_blocks_and_warns_against_blind_fix():
    """ready=false 但网卡已 armed —— 卡在别处，不能瞎修。

    这个分支存在的意义：如果这里也返回 self_heal，Warden 会去跑
    deviceenablewake，然后发现没修好，再跑一遍，无限循环。
    更糟的是人会被"我修了但它还是不行"误导。
    """
    caps = make_caps(can_be_woken=False, wake_armed=True)
    pf = preflight(make_identity(), caps)

    assert pf.action == "block"
    assert any("不要跑 deviceenablewake" in r for r in pf.reasons)


def test_preflight_unknown_wol_state_is_not_success():
    """探测不到 WOL 状态时不能当成就绪。

    can_be_woken=None 与 wake_armed=None 表示"不知道"。
    不知道就该 proceed 吗？在 Windows 上不该——但也不能 block，
    否则缺一个脚本就再也迁不动。当前设计：proceed + 明确记一条未知。
    """
    caps = make_caps(can_be_woken=None, wake_armed=None)
    pf = preflight(make_identity(), caps)

    assert pf.action == "proceed"
    assert any("WOL 状态未知" in r for r in pf.reasons)


def test_preflight_required_gap_wins_over_wol():
    """必需能力缺失优先于 WOL 判断：连灵魂都搬不了，谈什么唤醒。"""
    caps = make_caps(
        soul_read=False,
        can_be_woken=False,
        wake_armed=False,
    )
    pf = preflight(make_identity(), caps)

    assert pf.action == "block"
    assert any("soul.read" in b for b in pf.blocking)
    # 不能走 self_heal 分支
    assert pf.action != "self_heal"


def test_preflight_flags_missing_desktop_but_still_proceeds():
    """无桌面不是硬阻塞：AstrBot 做服务能跑，只是不能开直播。"""
    caps = make_caps(has_interactive_desktop=False)
    pf = preflight(make_identity(), caps)

    assert pf.action == "proceed"
    assert any("直播" in r for r in pf.reasons)


def test_preflight_skips_wol_on_nas():
    """NAS 自己不需要被叫醒，WOL 判断不该出现在它的结论里。"""
    nas = make_identity(
        shell_id="ugreen-776b-deadbeef",
        hostname="UGREEN-776B",
        platform="linux",
        astrbot_root="/AstrBot",
        data_path="/AstrBot/data",
    )
    caps = make_caps(
        can_be_woken=None,
        wake_armed=None,
        has_interactive_desktop=False,
        has_gpu=False,
    )
    pf = preflight(nas, caps)

    assert pf.action == "proceed"
    assert not any("WOL" in r for r in pf.reasons)
    assert not any("直播" in r for r in pf.reasons)


def test_preflight_explain_is_human_readable():
    pf = preflight(make_identity(), make_caps(can_be_woken=False, wake_armed=False))
    text = pf.explain()
    assert "SELF_HEAL" in text or "self_heal" in text
    assert "阻塞项" in text
    assert "·" in text


# --------------------------------------------------------------------------
# probe_capabilities：真实探测（在本机跑）
# --------------------------------------------------------------------------


def test_probe_capabilities_on_this_machine():
    ident = probe_identity()
    caps = probe_capabilities(ident, data_path=Path(".") / "__probe_tmp__")

    # soul.read 取决于 sqlite3 版本，本机 3.49.1 一定有 backup
    assert caps.soul_read is True
    # 工作目录一定可写
    assert caps.soul_write is True
    assert caps.shell_identity is True
    assert isinstance(caps.notes, list)

    cleanup = Path(".") / "__probe_tmp__"
    if cleanup.exists():
        import shutil

        shutil.rmtree(cleanup, ignore_errors=True)


def test_probe_capabilities_reports_unknown_wol_without_script(tmp_path: Path):
    """没给 check-wol.ps1 时，WOL 状态必须是 None + 一条说明。

    绝不能用默认值假装"已就绪"——那正是 ARCH §7.4.3 记下的教训。
    """
    ident = make_identity()
    target = tmp_path / "data"
    caps = probe_capabilities(ident, data_path=target, check_wol_script=None)

    if ident.platform == "windows":
        assert caps.can_be_woken is None
        assert caps.wake_armed is None
        assert any("WOL 状态未知" in n for n in caps.notes)
    else:
        assert caps.can_be_woken is None


def test_probe_capabilities_soul_write_false_when_no_target():
    ident = make_identity(astrbot_root=None, data_path=None)
    caps = probe_capabilities(ident, data_path=None, check_wol_script=None)
    assert caps.soul_write is False
    assert any("无从校验可写性" in n for n in caps.notes)


def test_probe_capabilities_serializes_to_json_safe_dict():
    import json

    ident = probe_identity()
    caps = probe_capabilities(ident, data_path=Path(".") / "__probe_tmp2__")
    payload = {
        "identity": ident.to_dict(),
        "capabilities": caps.to_dict(),
        "preflight": preflight(ident, caps).to_dict(),
    }
    text = json.dumps(payload, ensure_ascii=False)
    assert "shell_id" in text
    assert "missing_required" in text

    import shutil

    shutil.rmtree(Path(".") / "__probe_tmp2__", ignore_errors=True)

# --------------------------------------------------------------------------
# read_astrbot_version
# --------------------------------------------------------------------------


def test_read_version_from_source_tree(tmp_path: Path):
    src = tmp_path / "src"
    (src / "astrbot").mkdir(parents=True)
    (src / "astrbot" / "__init__.py").write_text(
        '__version__ = "4.29.4"\n', encoding="utf-8"
    )
    assert read_astrbot_version(src) == "4.29.4"


def test_read_version_falls_back_to_cmd_config(tmp_path: Path):
    """NAS 上源码在容器里，宿主机只有 data/。必须能从 cmd_config.json 读到。"""
    src = tmp_path / "src"
    src.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    (data / "cmd_config.json").write_text(
        "\ufeff" + json.dumps({"config_version": 2}), encoding="utf-8"
    )
    assert read_astrbot_version(src, data) == "config-2"


def test_read_version_handles_bom(tmp_path: Path):
    """cmd_config.json 带 UTF-8 BOM，用 utf-8 打开会 JSONDecodeError。

    这是实测到的真事故：Linux 上 json.load() 直接
    `Unexpected UTF-8 BOM (decode using utf-8-sig)`。
    """
    data = tmp_path / "data"
    data.mkdir()
    raw = json.dumps({"config_version": 2}).encode("utf-8")
    (data / "cmd_config.json").write_bytes(b"\xef\xbb\xbf" + raw)

    assert read_astrbot_version(None, data) == "config-2"


def test_read_version_source_beats_config(tmp_path: Path):
    """两条路都能读到时，源码更权威。"""
    src = tmp_path / "src"
    (src / "astrbot").mkdir(parents=True)
    (src / "astrbot" / "__init__.py").write_text(
        '__version__ = "4.29.4"\n', encoding="utf-8"
    )
    data = tmp_path / "data"
    data.mkdir()
    (data / "cmd_config.json").write_text(
        json.dumps({"config_version": 2}), encoding="utf-8"
    )

    assert read_astrbot_version(src, data) == "4.29.4"


def test_read_version_returns_none_when_nothing_found(tmp_path: Path):
    assert read_astrbot_version(tmp_path / "nope", tmp_path / "nada") is None
