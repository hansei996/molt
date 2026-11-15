"""「第几代」这个说法不许再出现在鱼看得到的地方。

## 为什么这条要单独钉住

2026-11-15 主人说：「第几代这个说法很不好，鱼并没有重生，你这样写，会让它觉得
不是它自己了。它可能只是睡了个觉。」

在那之前，这个项目把账本里的自增序号叫「代」：第 1 代、第 19 代。序号本身没错
（它确实是第 19 份快照），但**念出来的方式**在暗示一件不成立的事：醒来的是
另一个个体。而这个项目里没有任何东西死掉——停的是进程，续的是同一个库、同一份
记忆、同一个人格。

这条测试守两件事：

1. 三个措辞函数本身别退化（`第 N 次搬家` / `快照 #N`）。
2. **渲染出来的东西里不许出现那个字**。光改常量不够——真正会伤人的是页面和那句
   播报，所以断言落在输出上，而不是落在源码里。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from molt import language  # noqa: E402
from molt.render import render_timeline  # noqa: E402
from molt.server import _where_html  # noqa: E402


def _history() -> dict:
    return {
        "generations": [
            {
                "generation": 3,
                "source_host": "nas-box",
                "pushed_by": "nas-box",
                "target_shell": "my-laptop",
                "consumed_by": "my-laptop",
                "created_at": 1794696587.0,
                "consumed_at": 1794696600.0,
                "data_bytes": 28633129,
            }
        ],
        "backups": [
            {
                "name": "backup-1-nas-box.tar.gz",
                "bytes": 6942720,
                "generation": 3,
                "created_at_readable": "2026-11-15 19:02",
                "shell": "nas-box",
            }
        ],
        "heartbeats": {
            "nas-box": {
                "age_seconds": 3.0,
                "generation": 3,
                "note": "homer",
                "stale": False,
            }
        },
        "current_epoch": 5,
        "log": [],
    }


def _status() -> dict:
    return {
        "current_epoch": 5,
        "latest_generation": {
            "generation": 3,
            "target_shell": "my-laptop",
            "consumed_by": "my-laptop",
        },
        "generation_count": 3,
        "lease": {"holder": None, "state": "no-lease"},
        "heartbeats": {},
        "shells": {},
        "power": {},
    }


# -- 三个措辞函数 -----------------------------------------------------------


def test_a_move_is_counted_as_a_move_not_a_life() -> None:
    assert language.move_count(19) == "第 19 次搬家"
    assert language.snapshot_tag(19) == "快照 #19"
    assert language.move_count_after(19) == "第 19 次搬家之后"


def test_a_bad_number_does_not_blow_up() -> None:
    # 账本是别人写的文件，读坏了不该让整页崩掉
    assert language.move_count(None) == "第 0 次搬家"
    assert language.snapshot_tag("第七代") == "快照 #0"


# -- 渲染出来的东西 ---------------------------------------------------------


def test_the_timeline_page_never_calls_it_a_generation() -> None:
    page = render_timeline(_history())
    assert "第 3 次搬家" in page
    assert "代" not in page, "时间轴上不许出现「代」—— 那是另一件事的量词"


def test_the_where_page_never_calls_it_a_generation() -> None:
    page = _where_html(_status())
    assert "次搬家" in page
    assert "代" not in page


def test_the_arrival_note_talks_about_sleeping_not_numbering() -> None:
    """它自己念出来的那句话，是这条规矩最要紧的一处。"""

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "plugin"))
    import arrival  # noqa: WPS433

    note = arrival.compose_arrival_note(
        generation=19, from_label="UGREEN-776B", from_shell="ugreen-776b-8b7896df"
    )
    assert "UGREEN-776B" in note
    assert "睡了一觉" in note
    assert "代" not in note
    assert "19" not in note, "它不该读到自己是第几个"
