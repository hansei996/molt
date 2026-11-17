"""「哪台机器」的少打字写法：编号、前缀、片段，以及**有歧义就不猜**。

2026-11-18 00:54 主人看着 `/molt machine on desktop-h2kgiao` 说：
「这也太难写了吧」。壳名是给账本当主键的（唯一、稳定），不是给人
在手机上敲的 —— 所以人可以说 `2`、`desk`、`h2kgiao`。

这一层钉的是**解析**，不是格式：`list` 打出来的编号和 `resolve`
认的编号必须是同一个顺序，否则人会遇到"照着你写的敲，它说没有这个"。
"""
from __future__ import annotations

from molt import machines

SHELLS = {
    "desktop-h2kgiao": {"role": "desktop"},
    "ugreen-776b-8b7896df": {"role": "nas"},
    "win-box": {"role": "desktop"},
}


def test_the_order_is_stable_and_alphabetical() -> None:
    assert machines.ordered_shells(SHELLS) == [
        "desktop-h2kgiao",
        "ugreen-776b-8b7896df",
        "win-box",
    ]


def test_a_number_picks_the_same_row_that_list_printed() -> None:
    order = machines.ordered_shells(SHELLS)
    for index, name in enumerate(order, 1):
        assert machines.resolve(str(index), SHELLS) == (name, "")


def test_a_number_out_of_range_says_how_many_there_are() -> None:
    name, problem = machines.resolve("9", SHELLS)
    assert name == ""
    assert "9" in problem and "3" in problem


def test_the_full_name_still_works() -> None:
    assert machines.resolve("win-box", SHELLS) == ("win-box", "")


def test_a_prefix_is_enough_when_it_is_unique() -> None:
    assert machines.resolve("desk", SHELLS) == ("desktop-h2kgiao", "")
    assert machines.resolve("WIN", SHELLS) == ("win-box", "")


def test_a_fragment_inside_the_name_also_works() -> None:
    assert machines.resolve("h2kgiao", SHELLS) == ("desktop-h2kgiao", "")
    assert machines.resolve("776b", SHELLS) == ("ugreen-776b-8b7896df", "")


def test_an_ambiguous_prefix_is_refused_and_both_candidates_are_named() -> None:
    shells = {"win-a": {}, "win-b": {}}
    name, problem = machines.resolve("win", shells)
    assert name == ""
    assert "win-a" in problem and "win-b" in problem


def test_an_unknown_name_does_not_pick_the_closest_match() -> None:
    name, problem = machines.resolve("desktopp", SHELLS)
    assert name == ""
    assert "desktopp" in problem


def test_empty_input_and_empty_ledger_are_their_own_messages() -> None:
    assert machines.resolve("  ", SHELLS)[0] == ""
    assert machines.resolve("1", {})[0] == ""


def test_heartbeat_age_reads_like_a_person_wrote_it() -> None:
    assert machines.alive_text(3) == "在跑（3 秒前）"
    assert "分钟" in machines.alive_text(600)
    assert "小时" in machines.alive_text(7200)
    assert machines.alive_text(None) == "没上报心跳"
