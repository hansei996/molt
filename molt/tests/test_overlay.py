"""机器覆写层的测试。

这层东西的价值全在"它到底改了哪些东西、没改哪些东西"上，所以测试的重点
不是"函数能跑通"，而是**逐项固定住覆写表**：每一项改动都要有一条测试
说明它为什么该改；每一个不该动的字段都要有一条测试说明它必须原样保留。

否则某天有人顺手多改了一个字段，没人会发现——直到那只鱼在 Windows 上
用着 NAS 的 jwt_secret 连上局域网。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from molt.errors import Fatal, NeedsHuman
from molt.overlay import (
    CONFIG_ENCODING,
    apply_overlay,
    load_config,
    overlay_for_posix,
    overlay_for_windows,
    save_config,
)


# --------------------------------------------------------------------------
# 夹具：一份形状接近真实的 cmd_config.json
# --------------------------------------------------------------------------


def _realistic_config() -> dict:
    """照着真实 NAS 配置的骨架造一份。

    关键点：它同时含有可携带字段（persona）与机器绑定字段
    （workbuddy 的 127.0.0.1 地址、0.0.0.0 监听、jwt_secret）。
    """
    return {
        "config_version": 5,
        "platform": [
            {
                "id": "default_1234567890",
                "type": "qq_official",
                "enable": True,
                "appid": "1234567890",
            }
        ],
        "provider_sources": [
            {
                "id": "deepseek",
                "type": "openai_chat_completion",
                "api_base": "https://api.deepseek.com/v1",
                "key": ["sk-real-key"],
            },
            {
                # 这一条指向 NAS 上的中继，Windows 没有
                "id": "workbuddy",
                "type": "openai_chat_completion",
                "api_base": "http://127.0.0.1:17863/v1",
                "key": ["wb-key"],
            },
        ],
        "provider": [
            {"id": "deepseek-flash", "provider_source_id": "deepseek", "model": "deepseek-flash"},
            {
                "id": "wb-flash",
                "provider_source_id": "workbuddy",
                "model": "deepseek-v4.1-flash",
            },
        ],
        "dashboard": {
            "enable": True,
            "username": "admin",
            "host": "0.0.0.0",
            "port": 6185,
            "jwt_secret": "ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ",
        },
        "persona": [{"id": "default", "system_prompt": "# 我是谁\n\n我叫 DeepSeek 大肥鱼"}],
    }


# --------------------------------------------------------------------------
# 读写：BOM 必须被正确处理
# --------------------------------------------------------------------------


def test_load_config_handles_bom(tmp_path: Path):
    """带 BOM 的文件必须能读。

    这个文件真实存在于 NAS 上，前 3 字节是 `ef bb bf`。Linux 上普通
    `json.load()` 打开它会直接 `JSONDecodeError: Unexpected UTF-8 BOM`，
    而 AstrBot 自己用 `utf-8-sig` 所以它没事——正因如此这个 bug 只在
    我们的代码里出现，且只在跨平台时才暴露。
    """
    p = tmp_path / "cmd_config.json"
    body = json.dumps(_realistic_config(), ensure_ascii=False, indent=2)
    p.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))

    cfg = load_config(p)
    assert cfg["config_version"] == 5
    assert cfg["provider_sources"][0]["id"] == "deepseek"


def test_save_config_writes_bom_back(tmp_path: Path):
    """写回时必须保留 BOM，否则 AstrBot 在某些路径上会读成乱码。"""
    p = tmp_path / "cmd_config.json"
    save_config(_realistic_config(), p)

    raw = p.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", "写回必须带 BOM"
    assert raw[3:4] == b"{"


def test_roundtrip_preserves_non_ascii(tmp_path: Path):
    """中文不能被转义成 \\uXXXX。

    人要看这个文件、要手改它。转义之后就没法看了。
    """
    p = tmp_path / "cmd_config.json"
    save_config(_realistic_config(), p)

    text = p.read_text(encoding=CONFIG_ENCODING)
    assert "DeepSeek 大肥鱼" in text
    assert "\\u" not in text


def test_load_config_missing_file_is_needs_human(tmp_path: Path):
    with pytest.raises(NeedsHuman):
        load_config(tmp_path / "nope.json")


def test_load_config_bad_json_is_fatal(tmp_path: Path):
    p = tmp_path / "cmd_config.json"
    p.write_text('{"a": 1,}', encoding=CONFIG_ENCODING)  # 尾逗号
    with pytest.raises(Fatal) as ei:
        load_config(p)
    assert "不是合法 JSON" in str(ei.value)
    assert "行" in str(ei.value), "错误消息要指出位置，否则人得自己找"


def test_load_config_non_object_is_fatal(tmp_path: Path):
    p = tmp_path / "cmd_config.json"
    p.write_text("[1,2,3]", encoding=CONFIG_ENCODING)
    with pytest.raises(Fatal):
        load_config(p)


# --------------------------------------------------------------------------
# 覆写：逐项固定住"该改的改了"
# --------------------------------------------------------------------------


def test_overlay_drops_workbuddy_source_and_its_providers():
    """workbuddy 必须连 provider 条目一起删。

    只删 source 会留下引用它的 provider，然后 AstrBot 在
    `provider.manager` 里抛 `KeyError: 'type'`——这是实测过的崩溃。

    而"删"而不是"改地址"的理由：改成一个同样不存在的地址，只会让
    启动日志里多一条 provider 不可用的警告，让人每次都怀疑自己配错了。
    """
    cfg, notes = overlay_for_windows(_realistic_config())

    ids = [s["id"] for s in cfg["provider_sources"]]
    assert "workbuddy" not in ids
    assert "deepseek" in ids, "能用的 provider 不该被删"

    prov_ids = [p["id"] for p in cfg["provider"]]
    assert "wb-flash" not in prov_ids, "引用已删 source 的 provider 必须一起删"
    assert "deepseek-flash" in prov_ids

    joined = " | ".join(notes["changes"])
    assert "workbuddy" in joined
    assert "KeyError" in joined, "note 要说明为什么必须一起删"


def test_overlay_repoints_agent_runner():
    """agent_runner 的主模型必须跟着挪走。

    这是第三处引用。只删前两处的话启动日志里还会留一条：
        WARN Configured Agent Runner model provider ID
        `workbuddy/deepseek-v4.1-flash` does not match an enabled provider.
    它不崩（fallback 救了它），这正是最容易漏的那类问题：**功能上没事，
    但每次启动都留一条"配置好像有问题"的警告**。那种警告的长期效果是让人
    对日志脱敏，真正出事时没人看。
    """
    cfg = _realistic_config()
    cfg["agent_runner"] = {
        "runner_type": "local",
        "config": {
            "model": {
                "provider_id": "wb-flash",
                "fallback_provider_ids": ["deepseek-flash"],
            }
        },
    }

    out, notes = overlay_for_windows(cfg)

    assert out["agent_runner"]["config"]["model"]["provider_id"] == "deepseek-flash"
    assert out["agent_runner"]["config"]["model"]["fallback_provider_ids"] == ["deepseek-flash"]
    joined = " | ".join(notes["changes"])
    assert "agent_runner 主模型" in joined


def test_overlay_does_not_repoint_when_provider_survives():
    """主 provider 没被删时不能动它。

    一台上本来配得好好的机器不该被莫名其妙改掉主模型。
    """
    cfg = _realistic_config()
    cfg["agent_runner"] = {
        "config": {"model": {"provider_id": "deepseek-flash", "fallback_provider_ids": ["x"]}}
    }

    out, notes = overlay_for_windows(cfg)

    assert out["agent_runner"]["config"]["model"]["provider_id"] == "deepseek-flash"
    assert "agent_runner" not in " | ".join(notes["changes"])


def test_overlay_does_not_repoint_without_fallback():
    """没有 fallback 可挪时宁可不改。

    把主模型设成空会让它彻底没有模型可用，那比留一条警告糟糕得多。
    让 AstrBot 自己去报错，人来做决定。
    """
    cfg = _realistic_config()
    cfg["agent_runner"] = {
        "config": {"model": {"provider_id": "wb-flash", "fallback_provider_ids": []}}
    }

    out, _ = overlay_for_windows(cfg)

    assert out["agent_runner"]["config"]["model"]["provider_id"] == "wb-flash"


def test_overlay_handles_missing_agent_runner():
    """配置里没有 agent_runner 时不该崩。"""
    cfg = _realistic_config()
    cfg.pop("agent_runner", None)

    out, _ = overlay_for_windows(cfg)
    assert out["dashboard"]["host"] == "127.0.0.1"


def test_overlay_sets_dashboard_to_localhost_on_windows():
    """Windows 上只监听本机。

    NAS 用 0.0.0.0 是因为它在容器里，容器网络本身隔离；裸 Windows 上
    0.0.0.0 等于真的对全家开放。这台机器是游戏机/直播机，dashboard
    没有理由被局域网访问到。
    """
    cfg, notes = overlay_for_windows(_realistic_config(), port=6199)

    assert cfg["dashboard"]["host"] == "127.0.0.1"
    assert cfg["dashboard"]["port"] == 6199
    joined = " | ".join(notes["changes"])
    assert "0.0.0.0 → 127.0.0.1" in joined
    assert "6185 → 6199" in joined


def test_overlay_rotates_jwt_secret():
    """jwt_secret 必须换掉，这是安全问题不是洁癖。

    它签的是 dashboard 的会话令牌。从 NAS 同步过来的那个密钥意味着
    "攻破 Windows 等于攻破 NAS"。所以哪怕把配置搬到了另一台机器，
    这个字段也绝不能跟着走。
    """
    original = _realistic_config()
    old_secret = original["dashboard"]["jwt_secret"]

    cfg, notes = overlay_for_windows(original)

    new_secret = cfg["dashboard"]["jwt_secret"]
    assert new_secret != old_secret
    assert len(new_secret) >= 40
    joined = " | ".join(notes["changes"])
    assert "jwt_secret" in joined
    assert "绝不能跨机复用" in joined


def test_overlay_jwt_is_different_every_call():
    """两次覆写必须得到不同密钥——否则"重新生成"是假的。"""
    a, _ = overlay_for_windows(_realistic_config())
    b, _ = overlay_for_windows(_realistic_config())
    assert a["dashboard"]["jwt_secret"] != b["dashboard"]["jwt_secret"]


def test_overlay_does_not_mutate_input():
    """覆写必须返回副本。

    直接改入参的话，同一份 base 配置被两次覆写就会互相污染——
    而且调用方根本看不出来，因为"看起来"它只是在读。
    """
    original = _realistic_config()
    snapshot = json.loads(json.dumps(original))

    overlay_for_windows(original)

    assert original == snapshot, "原配置不能被改动"


# --------------------------------------------------------------------------
# 覆写：逐项固定住"不该动的没动"
# --------------------------------------------------------------------------


def test_overlay_preserves_persona():
    """人格本体是 soul 的一部分，必须原样保留。

    这是整个项目的核心：记忆和人格搬过去之后，它要还是它。
    覆写层只碰机器绑定字段。
    """
    cfg, _ = overlay_for_windows(_realistic_config())
    assert cfg["persona"][0]["system_prompt"] == "# 我是谁\n\n我叫 DeepSeek 大肥鱼"


def test_overlay_preserves_platform_config():
    """平台配置（QQ appid 等）属于可携带状态，不该被覆写层碰。

    注意这条只断言 appid / type **还在**——`enable` 会被护栏关掉，
    见下面的「公理 0 护栏」那一节。关开关不删条目正是设计意图：
    删了就丢了 appid/secret，而"这只鱼从这边醒过来"时还需要它们。
    """
    cfg, _ = overlay_for_windows(_realistic_config())
    assert cfg["platform"][0]["appid"] == "1234567890"
    assert cfg["platform"][0]["type"] == "qq_official"


def test_overlay_preserves_usable_provider_api_base():
    """能用的 provider（deepseek）地址不能被动。"""
    cfg, _ = overlay_for_windows(_realistic_config())
    deepseek = [s for s in cfg["provider_sources"] if s["id"] == "deepseek"][0]
    assert deepseek["api_base"] == "https://api.deepseek.com/v1"


def test_overlay_preserves_config_version():
    """config_version 是 AstrBot 自己迁移用的，不能动。"""
    cfg, _ = overlay_for_windows(_realistic_config())
    assert cfg["config_version"] == 5


# --------------------------------------------------------------------------
# POSIX 预设
# --------------------------------------------------------------------------


def test_posix_overlay_keeps_local_providers():
    """NAS 自己那台不该删 workbuddy——它上面真的有那个中继。"""
    from molt.overlay import overlay_for_posix

    cfg, _ = overlay_for_posix(_realistic_config(), host="0.0.0.0", port=6185)

    ids = [s["id"] for s in cfg["provider_sources"]]
    assert "workbuddy" in ids, "NAS 上 workbuddy 是能用的，不该删"


def test_posix_overlay_still_rotates_jwt():
    """哪怕在 NAS 本地，重新生成 jwt 也是对的——它是"这台壳"的密钥。"""
    from molt.overlay import overlay_for_posix

    _cfg, notes = overlay_for_posix(_realistic_config())
    assert "jwt_secret" in " | ".join(notes["changes"])


# --------------------------------------------------------------------------
# 显式参数优先于默认猜测
# --------------------------------------------------------------------------


def test_apply_overlay_respects_explicit_empty_drop_list():
    """显式传 `[]` 表示"一个都别删"，不能被默认值覆盖。

    这类 bug 很典型：`drop or DEFAULT` 会把空列表当成"没传"。正确写法是
    `drop if drop is not None else DEFAULT`。
    """
    cfg, _ = apply_overlay(
        _realistic_config(),
        shell_id="test",
        platform="windows",
        drop_provider_sources=[],
        dashboard_host="127.0.0.1",
        dashboard_port=6199,
    )
    ids = [s["id"] for s in cfg["provider_sources"]]
    assert "workbuddy" in ids


def test_overlay_missing_dashboard_creates_it():
    """配置里没有 dashboard 段时不该崩，应该建出来。"""
    cfg = _realistic_config()
    del cfg["dashboard"]

    out, _ = overlay_for_windows(cfg)
    assert out["dashboard"]["host"] == "127.0.0.1"
    assert out["dashboard"]["jwt_secret"]

# --------------------------------------------------------------------------
# 公理 0 护栏：平台适配器必须被关掉
#
# 这一节的存在原因是一个真实发生过的事故（差点发生）：
# 2026-11-14 14:41，Windows 上用落位数据启动 AstrBot，`platform[0].enable`
# 是从 NAS 原样搬来的 True，它立刻打印 `[QQOfficial] Websocket session
# starting.` —— 也就是第二个进程用同一个 appid 连了同一个 QQ 机器人。
# 那次进程 3 秒后被测试脚本杀了，没造成用户可见的后果。
# 但覆写层当时完全没有处理这个字段，任何一次"在 Windows 上多留几分钟"
# 的操作都会真的变成两条鱼同时在线。
# --------------------------------------------------------------------------


def test_overlay_disables_platform_adapters():
    """两个预设都必须把 platform[].enable 关掉。

    这才是公理 0 在代码里的落地点：不管哪台机器落地，先别连 QQ。
    """
    for preset in (overlay_for_windows, overlay_for_posix):
        cfg, notes = preset(_realistic_config())
        assert cfg["platform"][0]["enable"] is False, f"{preset.__name__} 没关平台"
        assert notes["platforms_disabled"] == ["default_1234567890"]
        joined = " ".join(notes["changes"])
        assert "关闭平台适配器" in joined
        assert "default_1234567890" in joined


def test_overlay_keeps_platform_credentials():
    """关开关，但不删条目——appid / type / secret 必须还在。

    删掉就丢掉了身份凭据，而"这只鱼从这边醒过来"时还需要它们。
    关是可逆的，删不是。
    """
    cfg, _ = overlay_for_windows(_realistic_config())
    p = cfg["platform"][0]
    assert p["id"] == "default_1234567890"
    assert p["appid"] == "1234567890"
    assert p["type"] == "qq_official"


def test_overlay_platform_disable_can_be_turned_off_explicitly():
    """`apply_overlay` 层面留了开关（测试与将来的自动化需要），
    但两个预设都不允许调用方关掉它。"""
    cfg, notes = apply_overlay(
        _realistic_config(),
        shell_id="test",
        platform="windows",
        drop_provider_sources=[],
        disable_platforms=False,
    )
    assert cfg["platform"][0]["enable"] is True
    assert notes["platforms_disabled"] == []
    assert not any("关闭平台适配器" in c for c in notes["changes"])


def test_overlay_disables_multiple_platforms():
    """多平台（比如同时配了 QQ 与 aiocqhttp）要全关，不能只关第一个。"""
    cfg = _realistic_config()
    cfg["platform"].append(
        {"id": "local_aiocqhttp", "type": "aiocqhttp", "enable": True, "ws_reverse_port": 6199}
    )
    out, notes = overlay_for_windows(cfg)
    assert all(p["enable"] is False for p in out["platform"])
    assert set(notes["platforms_disabled"]) == {"default_1234567890", "local_aiocqhttp"}


def test_overlay_already_disabled_platforms_are_not_reported():
    """本来就是关的，不该在变更清单里刷一条"我关掉了它"。

    变更清单是给人看的；报告一件没做的事会让人不相信其他几条。
    """
    cfg = _realistic_config()
    cfg["platform"][0]["enable"] = False
    out, notes = overlay_for_windows(cfg)
    assert out["platform"][0]["enable"] is False
    assert notes["platforms_disabled"] == []
    assert not any("关闭平台适配器" in c for c in notes["changes"])


def test_overlay_handles_missing_or_weird_platform_field():
    """没有 platform / 它不是列表 / 元素不是 dict——都不许崩。

    配置文件是别人写的，覆写层不能因为一个字段形状不对就整份放弃。
    """
    for broken in ({}, {"platform": None}, {"platform": "nope"}, {"platform": [42, None]}):
        out, notes = apply_overlay(
            {**broken, "dashboard": {"host": "0.0.0.0", "port": 6185}},
            shell_id="test",
            platform="windows",
            drop_provider_sources=[],
        )
        assert notes["platforms_disabled"] == []
def test_apply_overlay_defaults_to_disabling_platforms():
    """`apply_overlay` 的默认值必须是"关"，不能是"开"。

    这条测试是被变异测试逼出来的：把默认值 `True` 改成 `False`，
    上面所有测试仍然全过——因为两个预设都显式传了 `disable_platforms=True`，
    默认值从头到尾没被走过。也就是说**将来任何新预设只要忘了传这个参数，
    护栏就静默消失了**，而现有测试不会响。

    默认值选"关"的理由：漏关的后果是两条鱼同时在线（用户可见的事故），
    漏开的后果只是机器人没连上（一条日志）。两者不对称，所以默认取安全的那边。
    """
    cfg, notes = apply_overlay(
        _realistic_config(),
        shell_id="test",
        platform="windows",
        drop_provider_sources=[],
    )
    assert cfg["platform"][0]["enable"] is False
    assert notes["platforms_disabled"] == ["default_1234567890"]