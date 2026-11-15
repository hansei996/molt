#!/usr/bin/env python3
"""molt reload：让 AstrBot 重新载入几个插件 —— 配置改完之后的那一步。

## 为什么需要它

插件配置是**插件载入时**从磁盘读的：`astrbot/core/star/star_manager.py:1167`
构造 `AstrBotConfig(config_path=.../config/<插件>_config.json)`。所以
`molt sync-plugins --apply` 写完文件之后，正在跑的那个插件手里还是旧配置 ——
文件变了、行为没变，而界面上看不出任何异常。

重载插件就够了（`Terminating plugin … → Loading plugin …`），不必整机重启：
整机重启会掉线重连，重载不会。

## 鉴权

用 `cmd_config.json` 里的 `dashboard.jwt_secret` 自己签一个短时 JWT
（`molt.control.sign_dashboard_jwt`，标准库 HMAC）。**跑这个脚本的人必须
已经能读那份配置** —— 也就是说它不增加任何新的信任边界：谁拿得到配置，
谁本来就能控制那只壳。token 默认只签 300 秒。

## 用法

    python tools/reload-plugin.py --config G:\\molt-fish\\data\\cmd_config.json astrbot_plugin_bili_watch
    python tools/reload-plugin.py --config <cfg> --dry-run a b c   # 只说要做什么

`cmd_config.json` 带 UTF-8 BOM，读的时候用 `utf-8-sig`（`read_dashboard_credentials` 里已经处理）。

退出码：0 全部成功；2 参数/配置问题；4 有插件重载失败。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from molt.control import read_dashboard_credentials, sign_dashboard_jwt  # noqa: E402

try:  # 复用 CLI 的符号表：控制台打不出 ✓ 时它会自动降级成 ASCII
    from molt.cli import C, _setup_console as _cli_setup_console
except Exception:  # pragma: no cover - 只有仓库被拆散时才会发生
    C = None  # type: ignore[assignment]
    _cli_setup_console = None  # type: ignore[assignment]

OK_MARK = C.S_OK if C is not None else "OK"
BAD_MARK = C.S_BAD if C is not None else "X"

DEFAULT_CONFIG = Path(r"G:\molt-fish\data\cmd_config.json")
DEFAULT_BASE = "http://127.0.0.1:6199"
RELOAD_ENDPOINT = "/api/plugin/reload"


def reload_one(base: str, token: str, name: str, *, timeout: float) -> tuple[bool, str]:
    """重载一个插件。返回 (成功?, 说明)。"""
    body = json.dumps({"name": name}).encode("utf-8")
    request = urllib.request.Request(
        base.rstrip("/") + RELOAD_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status == 200, f"HTTP {response.status} {raw[:200]}"
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也要把正文读出来
        raw = exc.read().decode("utf-8", "replace")
        return False, f"HTTP {exc.code} {raw[:200]}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"连不上 {base}：{exc}"


def main(argv: list[str] | None = None) -> int:
    if _cli_setup_console is not None:
        _cli_setup_console()
    parser = argparse.ArgumentParser(
        prog="reload-plugin",
        description="让 AstrBot 重新载入指定的插件（配置改完之后的那一步）",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help=f"AstrBot 的 cmd_config.json（默认 {DEFAULT_CONFIG}）",
    )
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"dashboard 地址（默认 {DEFAULT_BASE}）")
    parser.add_argument("--timeout", type=float, default=30.0, help="单个请求超时秒数")
    parser.add_argument("--dry-run", action="store_true", help="只说明会做什么，不发请求")
    parser.add_argument("plugins", nargs="+", help="插件目录名，例如 astrbot_plugin_bili_watch")
    args = parser.parse_args(argv)

    config_path = Path(args.config).expanduser()
    if not config_path.is_file():
        print(f"{BAD_MARK} 找不到配置：{config_path}", file=sys.stderr)
        return 2
    try:
        # 注意返回顺序是 (jwt_secret, username) —— 不是 (username, secret)
        secret, username = read_dashboard_credentials(config_path)
    except Exception as exc:  # 配置格式问题，给出人能看懂的说法
        print(f"{BAD_MARK} 读不出 dashboard 凭据（需要 dashboard.jwt_secret / username）：{exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"（空跑）会用 {username} 的身份重载 {len(args.plugins)} 个插件：")
        for name in args.plugins:
            print(f"  · {name}")
        return 0

    token = sign_dashboard_jwt(secret, username)
    failed: list[str] = []
    for name in args.plugins:
        ok, detail = reload_one(args.base, token, name, timeout=args.timeout)
        print(f"{OK_MARK if ok else BAD_MARK} {name}：{detail}")
        if not ok:
            failed.append(name)

    if failed:
        print(f"{BAD_MARK} {len(failed)} 个没重载成功：{', '.join(failed)}", file=sys.stderr)
        return 4
    print(f"{OK_MARK} {len(args.plugins)} 个插件已重新载入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
