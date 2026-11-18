"""`docs/VERIFY.md` 的三档计数必须和实际条目数一致。

为什么值得一条测试：这份文档的全部价值就是"哪条真验过、哪条没有"。
一旦有人加了行却没改数字，读者看到的比例就是错的，而**错的数字不会自己红** ——
它会安静地被引用到汇报里（这个项目已经吃过一次「常量放在那里，不等于它被使用」）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / "docs" / "VERIFY.md"


def _sections() -> list[str]:
    text = VERIFY.read_text(encoding="utf-8")
    parts = re.split(r"^### ", text, flags=re.M)
    return parts[1:]


def _rows(section: str) -> list[str]:
    """数表格里以数字或单个大写字母开头的数据行（表头与分隔行不算）。"""
    return [
        line
        for line in section.splitlines()
        if re.match(r"^\|\s*(\d+|[A-Z])\s*\|", line)
    ]


def _section(prefix: str) -> str:
    for section in _sections():
        if section.startswith(prefix):
            return section
    raise AssertionError(f"VERIFY.md 里找不到以 {prefix!r} 开头的小节")


def _live_count() -> int:
    return sum(len(_rows(section)) for section in _sections() if section.startswith("真机验过"))


def _pending_count() -> int:
    """「仍未验」小节里只数 G/H/I 这种字母编号（D19 的 A/B/C 在后面的小节里，不混进来）。"""
    section = _section("仍未验")
    # 只取到下一个 "## " 为止，避免把后面的 D19 选项表算进来
    section = section.split("\n## ", 1)[0]
    return len([line for line in _rows(section) if re.match(r"^\|\s*[A-Z]\s*\|", line)])


def test_the_document_lists_the_numbers_it_claims() -> None:
    text = VERIFY.read_text(encoding="utf-8")
    match = re.search(r"真机 \*\*(\d+)\*\* 条", text)
    assert match, "VERIFY.md 顶部应该有「真机 **N** 条」这一行"
    claimed_live = int(match.group(1))
    assert claimed_live == _live_count(), f"文档说真机 {claimed_live} 条，实际 {_live_count()} 条"

    match = re.search(r"未验 \*\*(\d+)\*\* 条", text)
    assert match, "VERIFY.md 顶部应该有「未验 **N** 条」这一行"
    claimed_pending = int(match.group(1))
    assert claimed_pending == _pending_count(), f"文档说未验 {claimed_pending} 条，实际 {_pending_count()} 条"


def test_every_live_row_says_what_was_seen() -> None:
    """真机行必须写出"看到了什么"，不能只有一句"验过"。"""
    for section in _sections():
        if not section.startswith("真机验过"):
            continue
        for line in _rows(section):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            assert len(cells) >= 3, f"真机行太短：{line}"
            assert any(len(cell) > 8 for cell in cells[1:]), f"这一行没写结果：{line}"

def test_the_baseline_line_carries_a_number_and_a_date() -> None:
    """基线那一行必须能被读成人话：数量 + 耗时 + 日期。

    为什么值得一条测试：这一行的作用是让后来的人知道"上次跑是什么时候、多少条"。
    少了日期，两年前的 `1319 passed` 与今天的看起来一模一样；少了耗时，
    就看不出测试是不是从 90 秒长到了 155 秒。
    """
    text = VERIFY.read_text(encoding="utf-8")
    match = re.search(r"\*\*最近一次全量回归\*\*：(.+)", text)
    assert match, "VERIFY.md 里应该有一行「**最近一次全量回归**：…」"
    line = match.group(1)
    assert re.search(r"\d+ passed in [\d.]+s", line), f"这一行没写数量与耗时：{line}"
    assert re.search(r"20\d\d-\d\d-\d\d", line), f"这一行没写日期：{line}"
