"""一个不碰网络的平台适配器，只为验证 drain / resume 的完整往返。

## 为什么需要它

`drain` / `resume` 的真实往返需要一个"能被关掉、又能被重新加载"的对外平台。
在家用环境里唯一现成的对外平台是 QQ —— 而**打开它会和 NAS 上的生产容器
双活**，直接违反公理 0。所以真机验证一直卡在这一步：
`drain` 验过了（`speaking` 变 false），`resume` 只能验"无副作用"。

这个适配器是解法：它用 AstrBot **公开的** `register_platform_adapter` 装饰器
注册成一个真正的平台类型（`type: molt_loopback`），走的是**和 QQ 完全相同的
代码路径** —— `platform_manager.load_platform()` 创建它、`_inst_map` 记住它、
`terminate_platform()` 关掉它、再 `load_platform()` 重建它。

差别只有一处：它的 `run()` 不连任何东西，安安静静地等一个事件。

## 它凭什么安全

1. **不建立任何网络连接**（没有 socket、没有 HTTP、没有 websocket）
2. **不读凭据**（配置里没有 token / appid / secret）
3. **只在测试数据目录里存在**（`G:\molt-p0\ctl-test\data\plugins\`），
   永远不会被装到 NAS 的生产容器里
4. `run()` 里有一个 `stop_event` 检查，`terminate()` 会把它置位，
   所以它能像真适配器一样"优雅退出"，而不是被 `task.cancel()` 硬杀
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from astrbot.api.star import Context, Star
from astrbot.core.platform import Platform, PlatformMetadata
from astrbot.core.platform.register import register_platform_adapter

logger = logging.getLogger("Molt.Loopback")

LOOPBACK_TYPE = "molt_loopback"


class MoltLoopbackAdapter(Platform):
    """一个只存在于内存里的"对外平台"。"""

    def __init__(self, platform_config: dict, platform_settings: dict, event_queue: asyncio.Queue) -> None:
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings
        self._stop_event = asyncio.Event()
        self._platform_id = str(platform_config.get("id") or "loopback")
        #: 记录自己是否真的被 terminate 过。测试断言它。
        self.terminated = False
        #: 已经跑了多久（run() 被调用后开始计时）。用来证明它真的"活着"过。
        self.started = False

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name=LOOPBACK_TYPE,
            description="Molt 的环回平台：不连任何外部服务，只用来验证 drain / resume。",
            id=self._platform_id,
            support_proactive_message=False,
        )

    async def run(self) -> None:
        """不连任何东西，等 terminate。"""
        self.started = True
        logger.info("[Molt.Loopback] %s 已启动（不连接任何外部服务）", self._platform_id)
        try:
            await self._stop_event.wait()
        except asyncio.CancelledError:
            logger.info("[Molt.Loopback] %s 被取消", self._platform_id)
            raise
        logger.info("[Molt.Loopback] %s 已停止", self._platform_id)

    async def terminate(self) -> None:
        self.terminated = True
        self._stop_event.set()
        logger.info("[Molt.Loopback] %s 收到 terminate", self._platform_id)

    async def send_by_session(self, session, message_chain):  # pragma: no cover
        """它不发消息。有人在用就说明配置错了。"""
        logger.warning("[Molt.Loopback] 收到了 send_by_session，但它是环回平台，没有出口")

    def __repr__(self) -> str:
        return f"<MoltLoopback id={self._platform_id} started={self.started} terminated={self.terminated}>"


register_platform_adapter(
    adapter_name=LOOPBACK_TYPE,
    desc="Molt 环回平台（不连任何外部服务，供 drain/resume 验证使用）",
    adapter_display_name="Molt 环回",
    default_config_tmpl={
        "id": "molt-loopback",
        "type": LOOPBACK_TYPE,
        "enable": False,
    },
)(MoltLoopbackAdapter)


class MoltLoopbackPlugin(Star):
    """只为让 AstrBot 的插件加载器把上面的适配器注册进去。

    AstrBot 加载插件时会 import 插件的 `main.py`，注册发生在 import 时。
    这个类本身什么都不做。
    """

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.context = context
        logger.info(
            "[Molt.Loopback] 插件已加载，平台类型 %r 已注册（enable 默认为 false）",
            LOOPBACK_TYPE,
        )
