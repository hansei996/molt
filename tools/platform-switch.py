#!/usr/bin/env python3
"""开关一个 cmd_config.json 里所有平台的 enable —— 只改这一个字段。

## 为什么需要一个脚本

`molt land` 落位时**一定会**把 `platform[].enable` 关掉（`overlay.py` 覆写表第 0 条），
理由见 ARCH §6.6.7：`appid` 是可携带的、`enable` 不是，而这两件事长得太像。
关掉之后，这只鱼在新壳上不会连任何 IM —— **这是对的**，因为"现在能不能对外说话"
必须是人的决定，不能是落位的副作用。

于是日常用起来就有一步手工：确认自己是唯一在线的那一个之后，把它打开。
手工改 JSON 有三个坑，这个脚本就是为了绕开它们：

1. **BOM**。`cmd_config.json` 前 3 字节可能是 `ef bb bf`，普通 `json.load(open(p))`
   在 Linux 上直接抛 `JSONDecodeError: Unexpected UTF-8 BOM`。
2. **改写整个文件**。用 `json.dump` 重写会改掉缩进/键序，让 `git diff` 变成一篇
   噪音，也让人不敢确认"我只改了这一个字段吗"。
3. **关错了对象**。配置文件里可能有多个平台条目，其中一些是别人加的。

所以做法是：解析 → 只动 `enable` → 逐条报告改了什么 → 写回（保留 BOM）→
然后再读一遍校验。**它不会替你决定该开还是该关**，两个动作都必须显式给。

## 用法

    python3 tools/platform-switch.py show   /mnt/dm-0/astrbot/data/cmd_config.json
    python3 tools/platform-switch.py on     /mnt/dm-0/astrbot/data/cmd_config.json --yes
    python3 tools/platform-switch.py off    /mnt/dm-0/astrbot/data/cmd_config.json --yes

`on` / `off` 不给 `--yes` 时只报告会改什么（dry-run）。这是刻意的：
**这个脚本能让你的一只鱼开始说话，那它就不该在你手滑的时候默默生效。**

退出码：0 成功（含 dry-run），1 参数/环境问题，2 文件不存在或不是合法配置。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# 必须是 utf-8-sig：读到 BOM 时它会吃掉，写回时我们要自己补回去。
ENCODING = "utf-8-sig"

# 输出符号。先按 UTF-8 试，控制台编码不了就换成 ASCII。
# 这一条是被真机打出来的：中文 Windows 的默认控制台是 GBK，
# 而 `✓`（U+2713）不在 GBK 里 —— 于是脚本**改文件改成功了，却在打印结果时崩掉**，
# 用户看到一屏 traceback，会以为没改成。
OK = "✓"
BAD = "✗"
DOT = "·"


def _setup_console() -> None:
    """把标准输出切到 UTF-8；切不动就把符号降级成 ASCII。

    必须在任何 print 之前调用。
    """
    global OK, BAD, DOT

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                # errors="replace" 是保险：即使编码不了也只显示成 `?`，不会抛异常。
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    try:
        target = sys.stdout.encoding or "ascii"
        (OK + BAD + DOT).encode(target)
    except (UnicodeEncodeError, LookupError):
        OK, BAD, DOT = "[OK]", "[!]", "-"


def _fail(msg: str, code: int = 1) -> int:
    print(f"{BAD} {msg}", file=sys.stderr)
    return code


def load(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    raw = path.read_text(encoding=ENCODING)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")
    return data


def platforms_of(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    plats = data.get("platform")
    if not isinstance(plats, list):
        return []
    return [p for p in plats if isinstance(p, dict)]


def describe(p: Dict[str, Any]) -> str:
    return "%s（type=%s，enable=%s）" % (
        p.get("id") or "<无 id>",
        p.get("type") or "<无 type>",
        p.get("enable"),
    )


def save(path: Path, data: Dict[str, Any]) -> None:
    """写回，并按原文件的习惯决定要不要带 BOM。

    这里有个坑，是测试逼出来的：`encoding="utf-8-sig"` **永远会写 BOM**，
    哪怕原文件没有。于是"只改一个字段"在字节层面变成了"改字段 + 给文件加 BOM"。
    对 AstrBot 来说加不加都读得懂（它自己就是带 BOM 写的），
    但"多改了一个字节"这种事一旦开了头，下次就分不清是脚本干的还是别人干的。

    所以：**原来有 BOM 就保留，原来没有就不加。**
    """
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    had_bom = False
    try:
        had_bom = path.read_bytes()[:3] == b"\xef\xbb\xbf"
    except OSError:  # pragma: no cover - 文件刚被删掉这种情况不值得为它设计
        pass

    # 先写临时文件再原子替换：中途被杀掉时不会留下半截配置。
    # 一个半截的 cmd_config.json 会让 AstrBot 起不来，而"起不来"比"配置旧了"难查得多。
    tmp = path.with_name(path.name + ".molt-tmp")
    tmp.write_text(text, encoding="utf-8-sig" if had_bom else "utf-8")
    tmp.replace(path)


def main(argv: List[str]) -> int:
    _setup_console()

    ap = argparse.ArgumentParser(
        description="开关 cmd_config.json 里平台的 enable（只改这一个字段）"
    )
    ap.add_argument("action", choices=["show", "on", "off"])
    ap.add_argument("config", help="cmd_config.json 的路径")
    ap.add_argument("--yes", action="store_true", help="真的写回去（不给只报告）")
    args = ap.parse_args(argv)

    path = Path(args.config)

    try:
        data = load(path)
    except FileNotFoundError:
        return _fail(f"找不到配置文件：{path}", 2)
    except json.JSONDecodeError as e:
        return _fail(
            f"这不是合法 JSON（第 {e.lineno} 行第 {e.colno} 列：{e.msg}）：{path}", 2
        )
    except (UnicodeDecodeError, ValueError) as e:
        return _fail(f"读不出这个配置（{e}）：{path}", 2)

    plats = platforms_of(data)
    if not plats:
        return _fail("这个配置里没有 platform[] 条目，没什么可开关的", 1)

    print(f"配置文件：{path}")
    for p in plats:
        print(f"  {DOT} {describe(p)}")

    if args.action == "show":
        return 0

    want = args.action == "on"
    changed = [p for p in plats if bool(p.get("enable")) is not want]
    if not changed:
        print(f"已经是 {'开' if want else '关'} 的，不用改。")
        return 0

    print()
    if not args.yes:
        for p in changed:
            print(f"  [dry-run] 会改：{describe(p)} → enable={want}")
        print("  （没给 --yes，什么都没写）")
        return 0

    for p in changed:
        p["enable"] = want
    save(path, data)

    # 写回后再读一遍。不是为了好看：`json.dumps` 与 `json.loads` 之间
    # 只要有一处编码判断错（比如漏了 BOM），这里就会当场发现。
    again = load(path)
    now = {p.get("id"): p.get("enable") for p in platforms_of(again)}
    for p in changed:
        print(f"  {OK} 已改：{p.get('id')} → enable={want}")
    bad = [pid for pid, val in now.items() if bool(val) is not want]
    if bad:
        return _fail(f"写回后复核失败，这些还是不对：{bad}", 1)

    print()
    if want:
        print("现在它有对外平台是打开的了。")
        print("  ——如果这个壳已经起来了，重启 AstrBot 才会生效（AstrBot 只在启动时读平台配置）。")
    else:
        print("现在它不会连任何对外平台。")
        print("  ——如果这个壳已经起来了，重启 AstrBot 才会生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
