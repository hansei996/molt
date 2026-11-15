"""机器覆写层：让同一个 soul 在不同壳上跑出各自的正确行为。

## 为什么必须有这一层

`soul` 是跨机器搬的，但 `cmd_config.json` 里有两类完全不同的东西混在一起：

| 类型 | 例子 | 能跨机器吗 |
|---|---|---|
| 可携带 | persona、模型名、平台账号、插件行为参数 | ✅ 应该搬 |
| 机器绑定 | `provider_sources[].api_base = http://127.0.0.1:17863/v1` | ❌ 走到 Windows 就指向空气 |
| 机器绑定 | `dashboard.host/port` | ❌ NAS 用 0.0.0.0:6185，Windows 该用 127.0.0.1:6199 |
| 机密 | `dashboard.jwt_secret` | ❌ **绝不能搬** |

最后一条最要紧，而且是个安全问题：`jwt_secret` 用来签 dashboard 的会话令牌。
把它从 NAS 同步到 Windows，等于一台机器被攻破时两台一起被攻破。它必须
**每台壳独立生成**。

第一次用落位数据启动 AstrBot 时就撞上了这个问题：它照着 NAS 的配置监听
`0.0.0.0:6185`，在一个 Windows 游戏机上把 dashboard 暴露到局域网，
而且用的是 NAS 的 JWT 密钥。

## 设计

不做"两份 JSON 合成"（那会在磁盘上留一个中间文件，中间文件本身就是
新的真相来源，很快会有人手改它然后困惑）。改成**纯函数 + 明确的覆写表**：

    base = json.loads(cmd_config.json)
    patched = apply_overlay(base, shell=..., ...)

覆写表是代码，不是数据文件——这样"这台机器该改什么"跟着代码走、能被
测试固定住，而不是散落在某台机器的磁盘上。

## 一个容易做错的地方

`cmd_config.json` 带 **UTF-8 BOM**（前 3 字节 `ef bb bf`）。AstrBot 自己用
`utf-8-sig` 读所以没事，但 Linux 上普通 `json.load()` 打开会直接报
`JSONDecodeError: Unexpected UTF-8 BOM`。读写都必须用 `utf-8-sig`。
"""

from __future__ import annotations

import copy
import json
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .errors import Fatal, NeedsHuman

#: 读写的编码。必须带 `-sig`，见模块 docstring 最后一段。
CONFIG_ENCODING = "utf-8-sig"

#: dashboard 的默认端口。AstrBot 自己的默认是 6185，但那是给容器用的
#: （容器里 0.0.0.0 是隔离的）。在 Windows 上我们要的是只本机可访问。
DEFAULT_DASHBOARD_PORT = 6199


def load_config(path: Path) -> Dict[str, Any]:
    """读 cmd_config.json，正确处理 BOM。"""
    path = Path(path)
    if not path.exists():
        raise NeedsHuman(f"配置文件不存在：{path}")
    try:
        raw = path.read_text(encoding=CONFIG_ENCODING)
    except UnicodeDecodeError as e:
        raise Fatal(
            f"配置文件编码不对：{path}（{e}）\n"
            "  它应该是 UTF-8（可以带 BOM）。"
        ) from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise Fatal(
            f"配置文件不是合法 JSON：{path}\n  第 {e.lineno} 行第 {e.colno} 列：{e.msg}"
        ) from e
    if not isinstance(data, dict):
        raise Fatal(f"配置文件顶层应该是一个 JSON 对象，实际是 {type(data).__name__}")
    return data


def save_config(data: Dict[str, Any], path: Path) -> None:
    """写回 cmd_config.json，保留 BOM 与缩进风格。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding=CONFIG_ENCODING)


def _drop_provider_sources(config: Dict[str, Any], drop: List[str]) -> List[str]:
    """删掉指向别处本机服务的 provider source，返回真正删掉的 id。

    为什么是"删"而不是"改地址"：`workbuddy` 指向 `http://127.0.0.1:17863/v1`，
    那是 NAS 上的一个中继进程。Windows 上没有它，也没有类似物。
    改地址只能改成一个同样不存在的地址；留着它只会让 AstrBot 在启动时
    打印一条 provider 不可用的警告，然后退到 fallback——那个警告会让人
    每次都怀疑是不是自己配错了。

    删掉之后 AstrBot 会安静地用剩下的 provider，这才是正确行为。
    但**对应的 provider 条目也要一起删**，否则 `provider.manager` 会因为
    找不到 provider_source 而抛 `KeyError: 'type'`（实测过）。
    """
    sources = config.get("provider_sources")
    dropped: List[str] = []
    if isinstance(sources, list):
        keep = []
        for s in sources:
            if isinstance(s, dict) and s.get("id") in drop:
                dropped.append(str(s.get("id")))
            else:
                keep.append(s)
        config["provider_sources"] = keep
    return dropped


def _drop_providers_referencing(config: Dict[str, Any], dropped_ids: List[str]) -> List[str]:
    """删掉引用了已删 provider source 的 provider 条目。"""
    if not dropped_ids:
        return []
    providers = config.get("provider")
    removed: List[str] = []
    if isinstance(providers, list):
        keep = []
        for p in providers:
            if isinstance(p, dict) and p.get("provider_source_id") in dropped_ids:
                removed.append(str(p.get("id") or p.get("model") or "?"))
            else:
                keep.append(p)
        config["provider"] = keep
    return removed


def _repoint_agent_runner(
    config: Dict[str, Any], dropped_provider_ids: List[str]
) -> Optional[str]:
    """把 `agent_runner` 的主模型从已删的 provider 挪到它的 fallback 上。

    这是**第三处**引用。前两处（`provider_sources`、`provider`）删掉之后
    启动日志里还剩一条：

        WARN Configured Agent Runner model provider ID
        `workbuddy/deepseek-v4.1-flash` does not match an enabled provider.
        AstrBot will use `deepseek/deepseek-flash` as the fallback.

    它不崩（`fallback_provider_ids` 救了它），这正是最容易漏掉的那类问题：
    **功能上没事，但每次启动都留一条"配置好像有问题"的警告。** 那种警告
    的长期效果是让人对日志脱敏，真正出事时没人看。

    所以顺手把它改到 fallback 上。只在"主 provider 确实被我们删了"时才动，
    否则一台上本来配得好好的机器会被莫名其妙改掉主模型。
    """
    if not dropped_provider_ids:
        return None

    runner = config.get("agent_runner")
    if not isinstance(runner, dict):
        return None
    model = runner.get("config", {}).get("model") if isinstance(runner.get("config"), dict) else None
    if not isinstance(model, dict):
        return None

    current = model.get("provider_id")
    if current not in dropped_provider_ids:
        return None

    fallbacks = model.get("fallback_provider_ids")
    if not isinstance(fallbacks, list) or not fallbacks:
        # 没有 fallback 可挪。不改——把主模型设成空会让它彻底没有模型可用，
        # 那比留一条警告糟糕得多。让 AstrBot 自己去报错，人来做决定。
        return None

    replacement = str(fallbacks[0])
    model["provider_id"] = replacement
    return f"agent_runner 主模型：{current} → {replacement}（原 provider 已删，用它自己的 fallback）"


def _disable_platform_adapters(config: Dict[str, Any]) -> List[str]:
    """把 `platform[].enable` 全部置为 False，返回被关掉的平台 id。

    ## 这条是公理 0 的落地，不是"可选的省电设置"

    `platform` 里配的是 QQ 官方机器人的 appid/secret。两个壳同时 enable，
    就是**两个进程用同一个 appid 连同一个 QQ 机器人**——也就是两条鱼同时
    在线，正好是架构里 §2.2 那条公理禁止的事。

    2026-11-14 14:41 真的发生过一次：Windows 上落位后的数据里
    `platform[0].enable` 还是 `True`（从 NAS 原样搬来的），AstrBot 一启动
    就打印 `[QQOfficial] Websocket session starting.`。那次因为进程 3 秒后
    就被测试脚本杀了、没来得及完成会话，所以没有造成用户可见的事故。
    但**它差一点就成立了**，而"差一点"不是理由——护栏必须在没人看着的时候
    也成立。

    ## 为什么不在这里删掉 platform 条目

    删了就丢掉了 appid/secret，而"这只鱼从这边醒过来"时还需要它们。
    关掉开关是可逆的；删掉配置不是。区别在于前者只表达"这个壳现在不该连"，
    后者表达"这个壳永远连不了"，而后者是错的。

    ## 那它什么时候被重新打开

    不该由这里做。打开平台的时机是交接协议里"确认自己是唯一在线的那个"之后
    （见 ARCH §5.2 先死后生）。v0.1 不实现自动打开——那时人显式地改一次配置，
    比一个自动打开但没验证租约的开关安全得多。
    """
    platforms = config.get("platform")
    disabled: List[str] = []
    if not isinstance(platforms, list):
        return disabled
    for p in platforms:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or p.get("type") or "?")
        if p.get("enable") is not False:
            p["enable"] = False
            disabled.append(pid)
    return disabled


def apply_overlay(
    config: Dict[str, Any],
    *,
    shell_id: str,
    platform: str,
    drop_provider_sources: Optional[List[str]] = None,
    dashboard_host: Optional[str] = None,
    dashboard_port: Optional[int] = None,
    rotate_jwt: bool = True,
    disable_platforms: bool = True,
) -> Dict[str, Any]:
    """返回覆写后的配置副本（不修改入参）。

    参数都是显式的而不是从环境猜——"猜这台机器该用哪个端口"正是
    分布式系统出事的典型起点。
    """
    cfg = copy.deepcopy(config)
    notes: Dict[str, Any] = {"shell_id": shell_id, "platform": platform, "changes": []}

    # --- 0. 关掉所有平台适配器（公理 0 的护栏，必须排在最前面）---
    #
    # 放在第一步而不是最后，是因为这一条一旦漏掉，后面的每一条都失去意义：
    # 配置写得再对，只要两个壳同时连上同一个 QQ 机器人，就已经是两条鱼在线了。
    #
    # `platforms_disabled` 不管开关是否生效都写进 notes——调用方不该为了读
    # 一个字段先判断它在不在。缺字段和"没有平台要关"在代码里长得太像了。
    notes["platforms_disabled"] = []
    if disable_platforms:
        disabled = _disable_platform_adapters(cfg)
        notes["platforms_disabled"] = disabled
        if disabled:
            notes["changes"].append(
                f"关闭平台适配器：{', '.join(disabled)}"
                "（这个壳刚接过 soul，还不能确定自己是唯一在线的那个；"
                "确认后再手动打开）"
            )

    # --- 1. 删掉本机不存在的外部依赖 ---
    drop = drop_provider_sources or []
    dropped_sources = _drop_provider_sources(cfg, drop)
    dropped_providers = _drop_providers_referencing(cfg, dropped_sources)
    if dropped_sources:
        notes["changes"].append(
            f"移除 provider_sources：{', '.join(dropped_sources)}（本机没有这些服务）"
        )
    if dropped_providers:
        notes["changes"].append(
            f"移除引用它们的 provider：{', '.join(dropped_providers)}"
            "（留着会抛 KeyError: 'type'）"
        )

    # --- 1b. agent_runner 里还有第三处引用 ---
    #
    # 前两处删完之后启动日志里还剩一条 WARN，指向同一个已删 provider。
    # 它不崩（fallback 救了它），但每次启动都留一条"配置好像有问题"的警告。
    repointed = _repoint_agent_runner(cfg, dropped_providers)
    if repointed:
        notes["changes"].append(repointed)

    # --- 2. dashboard 绑定地址 ---
    dash = cfg.get("dashboard")
    if not isinstance(dash, dict):
        dash = {}
        cfg["dashboard"] = dash
    if dashboard_host is not None:
        old = dash.get("host")
        dash["host"] = dashboard_host
        if old != dashboard_host:
            notes["changes"].append(f"dashboard.host：{old} → {dashboard_host}")
    if dashboard_port is not None:
        old = dash.get("port")
        dash["port"] = dashboard_port
        if old != dashboard_port:
            notes["changes"].append(f"dashboard.port：{old} → {dashboard_port}")

    # --- 3. jwt_secret 必须每壳独立 ---
    #
    # 这一条是安全问题，不是洁癖：这个密钥签的是 dashboard 的会话令牌。
    # 从 NAS 同步过来的那个密钥意味着"攻破 Windows 等于攻破 NAS"。
    if rotate_jwt:
        old = dash.get("jwt_secret")
        dash["jwt_secret"] = secrets.token_urlsafe(32)
        notes["changes"].append(
            "dashboard.jwt_secret：已重新生成"
            + ("（原值是随 soul 一起搬来的，绝不能跨机复用）" if old else "")
        )

    return cfg, notes


def overlay_for_windows(
    config: Dict[str, Any],
    *,
    shell_id: str = "windows",
    port: int = DEFAULT_DASHBOARD_PORT,
    drop_provider_sources: Optional[List[str]] = None,
    keep_provider_sources: Optional[Sequence[str]] = None,
) -> tuple:
    """Windows 壳的覆写预设。

    只监听 127.0.0.1：这台机器是游戏机/直播机，dashboard 没有理由对局域网
    开放。NAS 上用 0.0.0.0 是因为它在容器里，而容器网络本身就是隔离的；
    在裸 Windows 上 `0.0.0.0` 等于真的对全家开放。

    `disable_platforms` 默认 `True`，而且**不给调用方关闭它的便利**：
    这条护栏一旦允许按机器关掉，第一台关掉它的机器就会变成事故现场。
    要打开平台是"交接完成后的一次显式人工动作"，不是覆写层的事。

    ## `keep_provider_sources`：那台机器上真的跑着这个服务

    默认会删掉 `workbuddy`（指向 `http://127.0.0.1:17863/v1`）——理由是
    "Windows 上没有那个服务"。但这条默认在 2026-11-15 被真机推翻了一次：
    WorkBuddy 是**插件自己**在 `initialize()` 里起的本地网关（`astrbot_plugin_workbuddy`
    读 `plugin_data` 里的凭据 + 环境变量 `WORKBUDDY_MASTER_KEY`），两台机器都装了
    这个插件，所以那条回环地址在 Windows 上**也是有东西在听的**。
    删掉它，等于把一条真的能用的 provider 丢掉。

    所以留了一个显式开口：调用方知道"这台机器上确实有那个服务"时，把它列进
    `keep_provider_sources`。**默认仍然是删** —— 猜错方向不对称：多留一条死
    provider 会让 AstrBot 启动日志里多一条 WARN，少留一条活的会让人少一个模型。
    """
    drop = ["workbuddy"]
    if keep_provider_sources:
        keep = {str(x).strip() for x in keep_provider_sources if str(x).strip()}
        drop = [x for x in drop if x not in keep]
    return apply_overlay(
        config,
        shell_id=shell_id,
        platform="windows",
        drop_provider_sources=(
            drop_provider_sources
            if drop_provider_sources is not None
            else drop
        ),
        dashboard_host="127.0.0.1",
        dashboard_port=port,
        rotate_jwt=True,
        disable_platforms=True,
    )


def overlay_for_posix(
    config: Dict[str, Any],
    *,
    shell_id: str = "posix",
    host: str = "0.0.0.0",
    port: int = 6185,
) -> tuple:
    """POSIX/NAS 壳的覆写预设。

    不删 provider source：`workbuddy` 指向的 `http://127.0.0.1:17863/v1`
    在 NAS 上真的存在。

    ## 但平台适配器一样要关

    直觉上"NAS 本来就是它的家，落地就该连上 QQ"——这是**错的**，而且错的
    方式很隐蔽：`land` 不持有租约（租约是源端 `migrate` 拿的，push 完就还了）。
    一个不持有租约、却已经连上 QQ 的壳，如果源端也还活着，就是两条鱼同时在线。

    所以"重新连上 QQ"必须是 `land` **之后**的一次显式动作，而且应该在
    确认租约握在自己手里之后（见 ARCH §5.2 先死后生）。v0.1 把这一步留给人：
    比一个自动打开、但没验证租约的开关安全得多。
    """
    return apply_overlay(
        config,
        shell_id=shell_id,
        platform="posix",
        drop_provider_sources=[],
        dashboard_host=host,
        dashboard_port=port,
        rotate_jwt=True,
        disable_platforms=True,
    )
