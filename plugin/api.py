"""Molt 控制面的 HTTP 层。

路由都挂在 AstrBot dashboard 的 `/api/plug/astrbot_plugin_molt` 前缀下
（`astrbot/dashboard/api/plugins.py:209` 的 `_match_registered_web_api` 负责匹配）。
因此这些端点**天然继承 dashboard 的 JWT 鉴权**，本文件不实现任何鉴权。

## 为什么端点这么少

四个端点刚好对应交接协议里需要 AstrBot 配合的四个瞬间：

    Warden: "你是谁？"          -> GET  /status
    Warden: "先别说话了"        -> POST /drain
    Warden: "把尾巴收干净"      -> POST /checkpoint
    Warden: "可以重新开口了"    -> POST /resume

多一个端点就多一个能被误调用的东西，而这四个里只有 `/resume` 会让它重新对外说话，
所以它也是唯一一个带前置条件的。

## 响应形状

全部返回 `{"success": bool, ...}`，与 AstrBot 插件页 API 的既有约定一致
（见 `astrbot_plugin_memory_companion/page_api.py:2855` 的 `_ok`）。
失败时 `success=False` 且带 `error`，同时用合适的 HTTP 状态码，
这样 Warden 侧既可以直接看 body，也可以只看状态码。
"""

from __future__ import annotations

import logging
from typing import Any

from quart import jsonify

logger = logging.getLogger("Molt.ControlApi")


class MoltControlApi:
    """把 `MoltControlPlugin` 的四个动作暴露成 HTTP 端点。"""

    def __init__(self, plugin):
        self.plugin = plugin

    # ------------------------------------------------------------------ 路由注册

    def _route_specs(self):
        from .main import API_PREFIX

        # 前缀必须和 `context.py:774 register_web_api` 拼出来的路径一致：
        # AstrBot 会把它变成 `/api/plug/<插件目录名><route>`。
        specs = [
            ("/status", self.status, ["GET"], "Molt 控制面：报告自己是谁、平台状态、WAL 大小"),
            ("/drain", self.drain, ["POST"], "Molt 控制面：断开所有平台适配器（幂等）"),
            ("/resume", self.resume, ["POST"], "Molt 控制面：重新连上 drain 过的平台"),
            ("/checkpoint", self.checkpoint, ["POST"], "Molt 控制面：把 WAL 合并进主库"),
        ]
        return [(f"{API_PREFIX}{path}", handler, methods, desc) for path, handler, methods, desc in specs]

    def register_routes(self) -> None:
        from .main import API_PREFIX

        register = self.plugin.context.register_web_api
        specs = self._route_specs()
        for route, handler, methods, desc in specs:
            register(route, handler, methods, desc)
        logger.info(
            "[Molt] 控制面已注册 %d 个端点（前缀 /api/plug%s）",
            len(specs),
            API_PREFIX,
        )

    # ------------------------------------------------------------------ 响应辅助

    @staticmethod
    def _ok(**fields: Any):
        body = {"success": True}
        body.update(fields)
        return jsonify(body)

    @staticmethod
    def _err(message: str, status: int = 500, **fields: Any):
        body = {"success": False, "error": message}
        body.update(fields)
        response = jsonify(body)
        response.status_code = status
        return response

    # ------------------------------------------------------------------ 端点

    async def status(self):
        """只读。Warden 用它确认"对面那只壳现在是不是在对外说话"。"""
        try:
            snap = self.plugin.platform_snapshot()
            wal = self.plugin.wal_snapshot()
        except Exception as exc:
            logger.warning("[Molt] status 失败：%s", exc, exc_info=True)
            return self._err(f"读取状态失败：{exc}")

        return self._ok(
            plugin="astrbot_plugin_molt",
            plugin_version=self.plugin_version(),
            host=self.plugin.host_label(),
            platforms=snap,
            wal=wal,
            # `speaking` 是给 Warden 的唯一结论字段：它不需要自己从 connected 里推。
            # 推错了会让两条鱼同时在线，而这种错误在日志里看不出来。
            #
            # `platforms.connected` 只统计**配置驱动**的适配器（能收到外面消息的那类）。
            # `platforms.local_only` 里是仅本地闭环的（webchat：只能从本机 dashboard
            # 进入，且构造在 initialize() 里、无法被 resume 重建），它不参与这个判断。
            # 理由：它每个实例都必然存在，算进来的话 `speaking` 将恒为真 ——
            # 一个永远为真的判据不能当判据用。
            speaking=snap.get("connected", 0) > 0,
        )

    async def drain(self):
        try:
            result = await self.plugin.drain()
        except Exception as exc:
            logger.warning("[Molt] drain 失败：%s", exc, exc_info=True)
            return self._err(f"drain 失败：{exc}")

        if not result.get("ok"):
            # 不是致命错误：可能本来就没连、也可能有个适配器关不掉。
            # 但调用方必须知道"我说完了"和"我真的不说话了"是两件事。
            still = result.get("still_connected")
            ids = result.get("still_connected_ids") or []
            reason = result.get("reason") or (
                f"drain 之后仍有 {still} 个对外连接：{', '.join(ids)}"
                if ids
                else f"drain 之后仍有 {still} 个对外连接"
            )
            return self._err(reason, status=409, **result)
        return self._ok(**result)

    async def resume(self):
        try:
            result = await self.plugin.resume()
        except Exception as exc:
            logger.warning("[Molt] resume 失败：%s", exc, exc_info=True)
            return self._err(f"resume 失败：{exc}")

        if not result.get("ok"):
            return self._err(result.get("reason") or "resume 没有恢复任何平台", status=409, **result)
        return self._ok(**result)

    async def checkpoint(self):
        try:
            result = await self.plugin.checkpoint()
        except Exception as exc:
            logger.warning("[Molt] checkpoint 失败：%s", exc, exc_info=True)
            return self._err(f"checkpoint 失败：{exc}")

        if not result.get("ok"):
            return self._err(result.get("reason") or "checkpoint 未完成", status=409, **result)
        return self._ok(**result)

    @staticmethod
    def plugin_version() -> str:
        from .main import PLUGIN_VERSION

        return PLUGIN_VERSION
