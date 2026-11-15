"""`molt/render.py` 的测试。

这一页要能发给别人看，所以它有两个不能破的性质：

1. **不引用任何外部资源** —— 断网、手机、别人的电脑上打开都得是同一张图。
2. **所有来自账本的字符串都过转义** —— `source_host` / `pushed_by` / 备份名
   都是"别人写下来的字符串"，而账本是一台机器上的一个 JSON 文件，能改它的人
   同样能改这些字段。直接拼进 HTML 就是存储型 XSS。

这两个性质都不会"报错"，只会悄悄地失效 —— 所以必须有测试钉住。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MOLT_ROOT = Path(__file__).resolve().parents[2]
if str(MOLT_ROOT) not in sys.path:
    sys.path.insert(0, str(MOLT_ROOT))

from molt.render import _human_bytes, esc, render_timeline  # noqa: E402


def _history(**over) -> dict:
    base = {
        "root": "/var/lib/molt",
        "generated_at": 1794713454.0,
        "generated_at_readable": "2026-11-15 11:30:54",
        "current_epoch": 3,
        "last_accepted_epoch": 3,
        "rejected_write_count": 0,
        "generation_count": 0,
        "total_bytes": 0,
        "generations": [],
        "backups": [],
        "heartbeats": {},
        "log": [],
    }
    base.update(over)
    return base


def _gen(**over) -> dict:
    g = {
        "generation": 1,
        "created_at": 1794713000.0,
        "created_at_readable": "2026-11-15 11:23:20",
        "source_host": "UGREEN-776B",
        "pushed_by": "nas-shell-1a2b3c4d",
        "target_shell": "desktop-abcd1234",
        "epoch": 1,
        "artifact_bytes": 8_345_862,
        "manifest_sha256": "0da947d069cb5e692ba806e79887823563f82f821021b0d1893027d6aea4bd3a",
        "consumed_by": "desktop-abcd1234",
        "consumed_at": 1794713100.0,
        "consumed_at_readable": "2026-11-15 11:25:00",
    }
    g.update(over)
    return g


# -- 转义 ---------------------------------------------------------------


def test_esc_handles_none_and_angles() -> None:
    assert esc(None) == ""
    assert esc("<b>") == "&lt;b&gt;"
    # 引号也要转，否则 "&quot; onmouseover=..." 能逃出去
    assert esc('"') == "&quot;"
    assert esc("'") == "&#x27;"


def test_hostname_is_escaped_in_the_page() -> None:
    page = render_timeline(_history(generations=[_gen(source_host="<script>alert(1)</script>")]))
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_shell_names_are_escaped_too() -> None:
    page = render_timeline(_history(generations=[_gen(pushed_by="<img src=x onerror=1>")]))
    assert "<img src=x" not in page
    assert "&lt;img src=x onerror=1&gt;" in page


# -- 零外部资源 ---------------------------------------------------------


def test_page_has_no_external_references() -> None:
    page = render_timeline(
        _history(
            generations=[_gen()],
            backups=[{"name": "backup-1.tar.gz", "bytes": 1024, "shell": "a", "generation": 1}],
            heartbeats={"desktop-abcd1234": {"age_readable": "3 秒前", "stale": False}},
            log=["2026-11-15 11:23:20  [INFO ] 收下快照"],
        )
    )
    assert "http://" not in page
    assert "https://" not in page
    assert "<script" not in page
    assert "<link" not in page
    assert "src=" not in page
    assert page.lstrip().lower().startswith("<!doctype html>")


# -- 时间轴顺序 ---------------------------------------------------------


def test_timeline_is_newest_first() -> None:
    page = render_timeline(
        _history(generations=[_gen(generation=1), _gen(generation=2), _gen(generation=3)])
    )
    first = page.index("第 3 次搬家")
    second = page.index("第 2 次搬家")
    third = page.index("第 1 次搬家")
    assert first < second < third, "时间轴必须新的在上面 —— 人想知道的是最近一次"


def test_generation_without_a_taker_says_so() -> None:
    page = render_timeline(
        _history(generations=[_gen(consumed_by="", consumed_at=0, consumed_at_readable="")])
    )
    assert "还没人取走" in page
    assert "已被" not in page.replace("已被 X 取走", "")


def test_generation_with_a_target_but_no_taker_names_the_target() -> None:
    page = render_timeline(
        _history(
            generations=[
                _gen(consumed_by="", consumed_at=0, consumed_at_readable="", target_shell="desk-1")
            ]
        )
    )
    assert "还没人取走（目标是 desk-1）" in page


# -- 空账本 -------------------------------------------------------------


def test_empty_ledger_does_not_crash_and_explains_itself() -> None:
    page = render_timeline(_history())
    assert "已经搬过" in page
    assert ">0 次<" in page
    assert "/molt go" in page
    assert "一个壳都没在报心跳" in page


def test_empty_backups_says_when_they_appear() -> None:
    page = render_timeline(_history())
    assert "还没有中途备份" in page
    assert "backup_interval" in page


# -- 中途备份 -----------------------------------------------------------


def test_backups_are_listed_but_marked_as_not_handoffs() -> None:
    page = render_timeline(
        _history(
            backups=[
                {
                    "name": "backup-20261115T043012-desktop-abcd1234.tar.gz",
                    "bytes": 7_195_227,
                    "shell": "desktop-abcd1234",
                    "generation": 3,
                    "created_at_readable": "2026-11-15 04:30:12",
                }
            ]
        )
    )
    assert "backup-20261115T043012-desktop-abcd1234.tar.gz" in page
    assert "这些不进时间轴" in page


# -- 心跳 ---------------------------------------------------------------


def test_heartbeats_show_label_and_shell_id() -> None:
    page = render_timeline(
        _history(
            heartbeats={
                "desktop-abcd1234": {
                    "age_readable": "8 秒前",
                    "stale": False,
                    "note": "agent",
                    "label": "DESKTOP-ABCD1234",
                    "generation": 3,
                }
            }
        )
    )
    assert "DESKTOP-ABCD1234（desktop-abcd1234）" in page
    assert "在跑" in page


def test_stale_heartbeat_is_marked_dead() -> None:
    page = render_timeline(
        _history(
            heartbeats={
                "desktop-abcd1234": {"age_readable": "42 分钟前", "stale": True, "label": "X"}
            }
        )
    )
    assert "哑了" in page


# -- 日志与统计 ---------------------------------------------------------


def test_log_lines_are_escaped_and_shown() -> None:
    page = render_timeline(_history(log=["<b>boom</b>"]))
    assert "&lt;b&gt;boom&lt;/b&gt;" in page
    assert "最近日志" in page


def test_rejected_writes_are_reported() -> None:
    page = render_timeline(_history(rejected_write_count=7))
    assert "7" in page
    assert "倒序" in page or "拒绝" in page


# -- 小工具 -------------------------------------------------------------


def test_human_bytes() -> None:
    assert _human_bytes(0) == "—"
    assert _human_bytes(None) == "—"
    assert _human_bytes(999) == "999 B"
    assert _human_bytes(8_345_862) == "8.0 MB"
    assert _human_bytes(2 * 1024 ** 3) == "2.0 GB"


def test_page_title_can_be_overridden() -> None:
    page = render_timeline(_history(), title="它<搬>过几次")
    assert "它&lt;搬&gt;过几次" in page
    assert re.search(r"<title>.*</title>", page) is not None
