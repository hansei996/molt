"""drain/resume 往返测试：用假 platform_manager 驱动，不起真 AstrBot。

为什么必须这么测：真机上唯一能做的"对外平台"是 QQ，打开它就会和生产容器双活
（公理 0 的反面）。而要把 drain/resume 的逻辑验干净，需要的是"有个能被关掉、
又能被重新加载的实例"——这跟 QQ 是不是那个实例无关。

所以这里用**假的 platform_manager**，它精确模仿 `/AstrBot/astrbot/core/platform/manager.py`
的真实行为：
  - `initialize()` 里硬编码 append 一个 webchat 实例（几乎不可能关掉且永不可重建）
  - 配置驱动的实例进 `_inst_map`，同时 append 进 `platform_insts`
  - `terminate_platform(pid)` 只对 `_inst_map` 里的生效
  - `load_platform(cfg)` 重建并放回 `_inst_map`
这个模仿本身也要测（见 test_fake_matches_real_contract），否则"用假对象测过"
很可能只是测了我对 AstrBot 的想象。
"""
import asyncio
import sys
from pathlib import Path

import pytest

MOLT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = MOLT_ROOT / "plugin"
# sys.path 与 astrbot 桩都由 conftest.py 装好（见那里的说明：
# 为什么不装真的 AstrBot、以及那个假 PlatformManager 的契约为什么也要测）。


# --------------------------------------------------------------------------- 假对象


class FakeMetadata:
    def __init__(self, name, id_):
        self.name = name
        self.id = id_


class FakeInstance:
    """一个最小可用的 Platform 替身。"""

    _counter = 0

    def __init__(self, name, id_, config=None):
        FakeInstance._counter += 1
        self.client_self_id = f"cid-{FakeInstance._counter}"
        self._name = name
        self._id = id_
        self.config = config or {}
        self.terminated = False

    def meta(self):
        return FakeMetadata(self._name, self._id)

    async def terminate(self):
        self.terminated = True

    def __repr__(self):
        return f"<Fake {self._name} id={self._id}>"


class FakePlatformManager:
    """精确模仿 AstrBot `PlatformManager` 的关键行为。"""

    def __init__(self, platform_configs):
        self.platform_insts = []
        self._inst_map = {}
        self.platforms_config = platform_configs

    def initialize(self):
        """对应 manager.py:105-117：先按配置加载，再硬编码 append webchat。"""
        for cfg in self.platforms_config:
            if cfg.get("enable"):
                self._load(cfg)
        webchat = FakeInstance("webchat", "webchat")
        self.platform_insts.append(webchat)  # 不进 _inst_map

    def _load(self, cfg):
        inst = FakeInstance(cfg.get("type", "unknown"), cfg.get("id"), config=cfg)
        self._inst_map[str(cfg.get("id"))] = {"inst": inst, "client_id": inst.client_self_id}
        self.platform_insts.append(inst)
        return inst

    async def load_platform(self, platform_config):
        if not platform_config.get("enable"):
            return
        pid = str(platform_config.get("id"))
        if pid in self._inst_map:
            return
        self._load(platform_config)

    async def terminate_platform(self, platform_id):
        info = self._inst_map.pop(str(platform_id), None)
        if info is None:
            return  # 真实实现同样静默返回
        inst = info["inst"]
        try:
            self.platform_insts = [
                i for i in self.platform_insts if i.client_self_id != info["client_id"]
            ]
        except Exception:
            pass
        await inst.terminate()

    def get_insts(self):
        return self.platform_insts


class FakeConfig(dict):
    """AstrBotConfig 是 dict 的子类，这里只需要 .get()。"""


class FakeContext:
    def __init__(self, config, platform_manager):
        self._config = FakeConfig(config)
        self.platform_manager = platform_manager
        self.registered = []

    def register_web_api(self, route, view_handler, methods, desc):
        self.registered.append({"route": route, "methods": methods, "desc": desc})


# --------------------------------------------------------------------------- 夹具


QQ = {"id": "default_1234567890", "type": "qq_official", "enable": True}
TG = {"id": "tg-1", "type": "telegram", "enable": True}
OFF = {"id": "wecom-1", "type": "weixin_oc", "enable": False}


def _plugin(platforms, enable_platforms=None):
    """造一个插件实例。`enable_platforms` 用来模拟护栏关掉 enable 的情况。"""
    from astrbot_plugin_molt.main import MoltControlPlugin

    plats = [dict(p) for p in platforms]
    if enable_platforms is False:
        for p in plats:
            p["enable"] = False
    config = {"platform": plats, "dashboard": {"port": 6199}}
    pm = FakePlatformManager(plats)
    pm.initialize()
    ctx = FakeContext(config, pm)
    plugin = MoltControlPlugin.__new__(MoltControlPlugin)
    plugin.context = ctx
    plugin._drained = []
    plugin._has_drained = False
    plugin._api = None
    return plugin, pm, ctx


# --------------------------------------------------------------------------- 测试


def test_fake_matches_real_contract():
    """假对象必须复现真实 manager 的三个关键性质，否则后面的测试没有意义。"""
    plugin, pm, _ = _plugin([QQ])
    # 性质 1：webchat 存在于 platform_insts，但不在 _inst_map
    assert any(type(i).__name__ == "FakeInstance" and i._id == "webchat" for i in pm.platform_insts)
    assert "webchat" not in pm._inst_map
    # 性质 2：配置驱动的在 _inst_map 里
    assert "default_1234567890" in pm._inst_map
    # 性质 3：terminate 一个不在 _inst_map 的 id 是静默无操作的
    before = len(pm.platform_insts)
    asyncio.run(pm.terminate_platform("webchat"))
    assert len(pm.platform_insts) == before


def test_snapshot_separates_outward_from_local_only():
    plugin, _, _ = _plugin([QQ, TG])
    snap = plugin.platform_snapshot()
    assert snap["connected"] == 2
    assert snap["connected_ids"] == ["default_1234567890", "tg-1"]
    assert snap["local_only"] == ["webchat"]
    assert {c["id"] for c in snap["enabled_in_config"]} == {"default_1234567890", "tg-1"}


def test_snapshot_when_all_platforms_disabled():
    """护栏关掉后的标准状态：对外 0 个，webchat 仍在但被标成 local_only。"""
    plugin, _, _ = _plugin([QQ], enable_platforms=False)
    snap = plugin.platform_snapshot()
    assert snap["connected"] == 0
    assert snap["connected_ids"] == []
    assert snap["local_only"] == ["webchat"]
    assert snap["enabled_in_config"] == []


def test_drain_actually_terminates_outward_platforms():
    plugin, pm, _ = _plugin([QQ, TG])
    result = asyncio.run(plugin.drain())
    assert result["ok"] is True
    assert sorted(result["drained"]) == ["default_1234567890", "tg-1"]
    assert result["still_connected"] == 0
    # 真的调用了实例的 terminate
    assert all(i.terminated for i in pm.platform_insts if i._id in {"default_1234567890", "tg-1"})
    # webchat 没被碰
    webchat = [i for i in pm.platform_insts if i._id == "webchat"]
    assert len(webchat) == 1 and webchat[0].terminated is False


def test_drain_is_idempotent():
    plugin, _, _ = _plugin([QQ])
    first = asyncio.run(plugin.drain())
    second = asyncio.run(plugin.drain())
    assert first["drained"] == ["default_1234567890"]
    assert second["drained"] == []          # 已经关掉的不重复报告
    assert second["still_connected"] == 0


def test_drain_on_nothing_connected_is_ok():
    """本来就没有对外平台时，drain 必须成功 —— 不是"没什么可关所以失败"。"""
    plugin, _, _ = _plugin([QQ], enable_platforms=False)
    result = asyncio.run(plugin.drain())
    assert result["ok"] is True
    assert result["drained"] == []
    assert result["still_connected"] == 0
    assert result["local_only"] == ["webchat"]


def test_drain_reports_still_connected_when_terminate_is_ignored():
    """最危险的情形：terminate 调了但实例还在。

    模拟方式是让 terminate_platform 变成空操作 —— 对应真实世界里适配器的
    `terminate()` 被吞掉异常（`manager.py` 的 `except Exception: logger.warning`）。
    这时 ok 必须是 False，否则 Warden 会以为可以安全启动目标壳。
    """
    plugin, pm, _ = _plugin([QQ])

    async def noop(pid):
        return None

    pm.terminate_platform = noop
    result = asyncio.run(plugin.drain())
    assert result["ok"] is False
    assert result["still_connected"] == 1
    assert result["still_connected_ids"] == ["default_1234567890"]


def test_resume_before_drain_is_refused():
    plugin, _, _ = _plugin([QQ])
    result = asyncio.run(plugin.resume())
    assert result["ok"] is False
    assert "没有 drain 过" in result.get("reason", "")


def test_resume_restores_outward_platforms():
    plugin, pm, _ = _plugin([QQ, TG])
    asyncio.run(plugin.drain())
    assert plugin.platform_snapshot()["connected"] == 0

    result = asyncio.run(plugin.resume())
    assert result["ok"] is True
    assert sorted(result["resumed"]) == ["default_1234567890", "tg-1"]
    snap = plugin.platform_snapshot()
    assert snap["connected"] == 2
    assert snap["drained"] == []


def test_resume_does_not_open_platforms_that_are_disabled_in_config():
    """drain 之后有人把 enable 改成 false（护栏做了这件事），resume 不许悄悄打开它。

    "让这只鱼重新对外说话"必须是人的决定，不是 resume 的副作用。
    """
    plugin, pm, ctx = _plugin([QQ])
    asyncio.run(plugin.drain())
    # 模拟：配置里把它关掉
    ctx._config["platform"][0]["enable"] = False
    result = asyncio.run(plugin.resume())
    assert result["resumed"] == []
    assert result["skipped_disabled"] == ["default_1234567890"]
    assert plugin.platform_snapshot()["connected"] == 0


def test_platform_instance_without_meta_is_handled():
    """真实 Platform 基类没有统一的 platform_id；meta() 也可能抛。"""

    class NoMeta:
        config = {"id": "cfg-id-1"}
        client_self_id = "x"

        def meta(self):
            raise RuntimeError("boom")

    plugin, pm, _ = _plugin([QQ])
    pm.platform_insts.append(NoMeta())
    snap = plugin.platform_snapshot()
    # 用 config["id"] 兜底，且因为不在 _inst_map 里 → local_only
    assert "cfg-id-1" in snap["local_only"]


def test_snapshot_without_platform_manager():
    from astrbot_plugin_molt.main import MoltControlPlugin

    plugin = MoltControlPlugin.__new__(MoltControlPlugin)
    plugin._drained = []
    plugin._has_drained = False
    plugin.context = object()  # 没有 platform_manager
    snap = plugin.platform_snapshot()
    assert snap["connected"] == 0
    assert snap["connected_ids"] == []
    result = asyncio.run(plugin.drain())
    assert result["ok"] is False
    assert "没有 platform_manager" in result["reason"]


def test_api_registers_five_endpoints():
    """`_register_api_if_available` 必须真的注册五条，不多不少。

    第五条 `/observe` 是 2026-11-15 加的：AstrBot 交给插件的那个事件循环不一定是
    在跑的那个，所以"每 30 秒自己看一眼账本"的后台任务在真机上从来没跑起来
    （详见 `docs/ARCH.md` §13.15）。改成由外部敲一下 —— **请求处理器一定跑在
    服务循环上**，这条路才靠得住。
    """
    from astrbot_plugin_molt.main import MoltControlPlugin

    plugin, _, ctx = _plugin([QQ])
    MoltControlPlugin._register_api_if_available(plugin)
    routes = [r["route"] for r in ctx.registered]
    assert sorted(routes) == sorted([
        "/astrbot_plugin_molt/status",
        "/astrbot_plugin_molt/drain",
        "/astrbot_plugin_molt/resume",
        "/astrbot_plugin_molt/checkpoint",
        "/astrbot_plugin_molt/observe",
    ])
    # 方法必须对：只有 status 是 GET，/resume 是唯一能让它重新开口的
    by_route = {r["route"].rsplit("/", 1)[-1]: r for r in ctx.registered}
    assert by_route["status"]["methods"] == ["GET"]
    for name in ("drain", "resume", "checkpoint", "observe"):
        assert by_route[name]["methods"] == ["POST"]

def test_platform_configs_includes_disabled_entries():
    """`_platform_configs()` 必须返回**全部**条目，包括 enable=false 的。

    这条测试盯的正是真机上撞到的那个 bug：护栏把 `enable` 关掉之后，
    `drain()` 靠"只认 enable=true 的配置列表"决定关谁 → 拿到空列表 →
    什么都没关，却报告"仍有 1 个连接"（HTTP 409）。

    `_platform_configs()` 的用途是回答"哪些 id 是配置驱动的适配器"，
    `enable` 只影响"启动时加载了没"，不影响这个判断。
    """
    plugin, _, _ = _plugin([QQ, OFF])
    ids = {c.get("id") for c in plugin._platform_configs()}
    assert ids == {"default_1234567890", "wecom-1"}
    # 而 resume 用的那个只认 enable=true
    assert {c.get("id") for c in plugin._enabled_configs()} == {"default_1234567890"}


def test_drain_works_when_platforms_were_disabled_by_guardrail():
    """护栏关掉 enable 之后仍要能正确分类（真机 bug 的直接回归测试）。

    场景：配置里 enable=false，但**运行实例其实还在**（比如进程启动后才被改的，
    或者像真机上那样——配置是搬运来的，运行态与配置不一致）。
    这时 `_inst_map` 里有它 = 它真的连着外面 → 必须算进 outward，drain 要关掉它。
    """
    plugin, pm, ctx = _plugin([QQ])
    # 模拟：运行态还在，但配置里已经被改成 enable=false
    ctx._config["platform"][0]["enable"] = False

    snap = plugin.platform_snapshot()
    # _inst_map 是主判据 → 仍然认得出它是对外连接
    assert snap["connected"] == 1
    assert snap["connected_ids"] == ["default_1234567890"]

    result = asyncio.run(plugin.drain())
    assert result["ok"] is True
    assert result["drained"] == ["default_1234567890"]
    assert all(
        i.terminated for i in pm.platform_insts if i._id == "default_1234567890"
    )


def test_status_speaking_reflects_only_outward_connections():
    """`/status` 的 `speaking` 只看对外连接，webchat 不算。

    为什么这条测试必须存在：webchat 在每个 AstrBot 实例里都存在。
    如果 `speaking` 把它算进去，那条字段将**恒为 true**，
    Warden 再也没法用它判断"源壳闭嘴了没有" —— 而这正是它存在的全部理由。
    """
    from astrbot_plugin_molt.api import MoltControlApi

    # 有对外平台 → speaking=True
    plugin, _, _ = _plugin([QQ])
    api = MoltControlApi(plugin)
    resp = asyncio.run(api.status())
    body = resp.json
    assert body["speaking"] is True

    # drain 之后 → speaking=False，但 local_only 里仍有 webchat
    asyncio.run(plugin.drain())
    resp2 = asyncio.run(api.status())
    body2 = resp2.json
    assert body2["speaking"] is False
    assert body2["platforms"]["local_only"] == ["webchat"]
    assert body2["platforms"]["connected"] == 0


def test_status_speaking_false_when_nothing_connected():
    """一个 webchat 都不算的情形：speaking 必须是 False，不是 True。

    这是上一条的反面：如果实现把 local_only 也算进 speaking，
    这个实例（只有 webchat）会报 speaking=True，而它其实没有任何对外连接。
    """
    from astrbot_plugin_molt.api import MoltControlApi

    plugin, _, _ = _plugin([QQ], enable_platforms=False)
    api = MoltControlApi(plugin)
    body = asyncio.run(api.status()).json
    assert body["platforms"]["local_only"] == ["webchat"]
    assert body["platforms"]["connected"] == 0
    assert body["speaking"] is False


def test_host_label_prefers_env_override(monkeypatch):
    """容器里 hostname 是容器 ID，所以必须能显式给一个名字。

    实测：生产容器报 `container-id`，而 `molt status` 要显示的是
    「灵魂在哪台机器上」。一串十六进制对要做关机决定的人毫无帮助。
    """
    from astrbot_plugin_molt.main import MoltControlPlugin

    monkeypatch.setenv("MOLT_HOST_LABEL", "UGREEN-776B")
    assert MoltControlPlugin.host_label(object()) == "UGREEN-776B"


def test_host_label_falls_back_to_hostname(monkeypatch):
    import socket

    from astrbot_plugin_molt.main import MoltControlPlugin

    monkeypatch.delenv("MOLT_HOST_LABEL", raising=False)
    monkeypatch.setattr(socket, "gethostname", lambda: "DESKTOP-1A2B3C4D")
    assert MoltControlPlugin.host_label(object()) == "DESKTOP-1A2B3C4D"


def test_host_label_ignores_blank_env(monkeypatch):
    """空白的环境变量不算"给了名字" —— 否则会得到一个空字符串的壳名。"""
    import socket

    from astrbot_plugin_molt.main import MoltControlPlugin

    monkeypatch.setenv("MOLT_HOST_LABEL", "   ")
    monkeypatch.setattr(socket, "gethostname", lambda: "realhost")
    assert MoltControlPlugin.host_label(object()) == "realhost"


def test_host_label_reads_sidecar_file(monkeypatch, tmp_path):
    """已经跑着的容器不该为了改一个显示名字被重建 —— 所以要有文件这一层。

    实测动因（2026-11-14 22:55）：人在 QQ 上问"你现在已经转移到 windows 了"，
    它回「脚下还是 Linux，主机名 `container-id`」。判断是对的，但读不了。
    """
    import socket

    from astrbot_plugin_molt import main as plugin_main

    monkeypatch.delenv("MOLT_HOST_LABEL", raising=False)
    monkeypatch.setattr(plugin_main, "__file__", str(tmp_path / "main.py"))
    monkeypatch.setattr(socket, "gethostname", lambda: "container-id")
    (tmp_path / "host_label.txt").write_text("UGREEN-776B\n", encoding="utf-8")

    assert plugin_main.MoltControlPlugin.host_label(object()) == "UGREEN-776B"


def test_host_label_env_beats_sidecar_file(monkeypatch, tmp_path):
    """环境变量是更"硬"的部署声明，压过文件。"""
    from astrbot_plugin_molt import main as plugin_main

    monkeypatch.setenv("MOLT_HOST_LABEL", "from-env")
    monkeypatch.setattr(plugin_main, "__file__", str(tmp_path / "main.py"))
    (tmp_path / "host_label.txt").write_text("from-file\n", encoding="utf-8")

    assert plugin_main.MoltControlPlugin.host_label(object()) == "from-env"


def test_host_label_ignores_blank_sidecar_file(monkeypatch, tmp_path):
    """文件在但是空的（或只有空白）→ 等同于没配，继续往下退。"""
    import socket

    from astrbot_plugin_molt import main as plugin_main

    monkeypatch.delenv("MOLT_HOST_LABEL", raising=False)
    monkeypatch.setattr(plugin_main, "__file__", str(tmp_path / "main.py"))
    monkeypatch.setattr(socket, "gethostname", lambda: "fallback-host")
    (tmp_path / "host_label.txt").write_text("   \n\n", encoding="utf-8")

    assert plugin_main.MoltControlPlugin.host_label(object()) == "fallback-host"


def test_host_label_strips_bom_and_takes_first_line(monkeypatch, tmp_path):
    """Windows 上随手用记事本存的文件会带 BOM、可能还有尾巴 —— 都要能吃下。"""
    from astrbot_plugin_molt import main as plugin_main

    monkeypatch.delenv("MOLT_HOST_LABEL", raising=False)
    monkeypatch.setattr(plugin_main, "__file__", str(tmp_path / "main.py"))
    (tmp_path / "host_label.txt").write_bytes(
        "\ufeffDESKTOP-1A2B3C4D\r\n第二行不该被用\r\n".encode("utf-8")
    )

    assert plugin_main.MoltControlPlugin.host_label(object()) == "DESKTOP-1A2B3C4D"
