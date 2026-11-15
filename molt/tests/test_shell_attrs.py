"""壳属性：账本要知道「那台机器是什么」，不只是「它叫什么」。

这一层的存在理由，用一句话说：**账本原来只知道壳的名字**。
而"回 NAS"、"去打会儿游戏"、"要不要先发魔术封包"这些判断，全都需要
"那台机器是什么"这个知识 —— 没有它，所有判断只能靠壳名猜，猜错的
时候不会报错，只会安静地搬去一台没有显卡的机器。

这些用例钉住的四件事：
1. 存得住、读得回来（重启之后还在）
2. 局部更新（声明 role 不会把手工填的 gpu 抹掉）
3. 校验会拒绝（不认识的 role 不会被存进去）
4. 没登记就不出现（**不编默认值** —— "我不知道"不能被伪装成"我知道"）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from molt.errors import NeedsHuman
from molt.warden import SHELL_ROLES, Warden


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "warden"


def test_a_shell_can_be_declared_and_read_back(root: Path) -> None:
    w = Warden(root=root)
    attrs = w.set_shell("nas-1", role="nas", wakeable=True, by="lead")
    assert attrs["role"] == "nas"
    assert attrs["wakeable"] is True
    assert attrs["declared"] is True

    # 新开一个 Warden（模拟进程重启）：它必须还在。
    again = Warden(root=root).shells_view()
    assert again["nas-1"]["role"] == "nas"
    assert again["nas-1"]["wakeable"] is True


def test_role_is_normalised_to_lowercase(root: Path) -> None:
    w = Warden(root=root)
    assert w.set_shell("box", role="  NAS  ")["role"] == "nas"


def test_gpu_empty_string_means_no_gpu(root: Path) -> None:
    """空串与"没填"必须不同。

    `gpu: ""` 是"确认过，没有显卡"，可以据此拒绝"去打游戏"；
    而"没填"（键不存在）什么都不该推定。两者都写成 ""，语义就丢了。
    """
    w = Warden(root=root)
    w.set_shell("box", gpu="")
    view = w.shells_view()["box"]
    assert view["gpu"] == ""
    assert "gpu" in (json.loads((root / "state.json").read_text("utf-8"))["shells"]["box"])


def test_partial_update_keeps_the_fields_you_did_not_mention(root: Path) -> None:
    """这是整层最重要的性质：**声明不覆盖手工填的**。

    守护进程每次启动都会声明 role/wakeable；如果那是整份覆盖，
    人手工填的 gpu/cost_per_hour 会在下一次重启后无声消失 ——
    而那种 bug 的表现是"我明明填过"，不是报错。
    """
    w = Warden(root=root)
    w.set_shell("box", gpu="4070TiS", cost_per_hour=0.5)
    w.set_shell("box", role="desktop", wakeable=True)
    view = w.shells_view()["box"]
    assert view["gpu"] == "4070TiS"
    assert view["cost_per_hour"] == 0.5
    assert view["role"] == "desktop"


@pytest.mark.parametrize("bad", ["laptop", "NAS机", ""])
def test_an_unknown_role_is_refused(root: Path, bad) -> None:
    w = Warden(root=root)
    with pytest.raises(NeedsHuman):
        w.set_shell("box", role=bad)
    assert w.shells_view() == {}


def test_none_means_do_not_touch_this_field(root: Path) -> None:
    """`None` 是「这次不提这个字段」，不是「把它清空成垃圾值」。

    这条与"空串是确认没有显卡"配对：参数缺席 = 保持原样，
    只有显式给了值才写进去。
    """
    w = Warden(root=root)
    w.set_shell("box", role="nas")
    after = w.set_shell("box", role=None, gpu=None)
    assert after["role"] == "nas"


def test_every_role_in_the_list_is_accepted(root: Path) -> None:
    w = Warden(root=root)
    for index, role in enumerate(SHELL_ROLES):
        assert w.set_shell(f"box-{index}", role=role)["role"] == role


def test_wakeable_must_be_a_bool(root: Path) -> None:
    w = Warden(root=root)
    with pytest.raises(NeedsHuman):
        w.set_shell("box", wakeable="yes")


@pytest.mark.parametrize("bad", [-1, 20000, "免费"])
def test_cost_per_hour_must_look_like_money(root: Path, bad) -> None:
    w = Warden(root=root)
    with pytest.raises(NeedsHuman):
        w.set_shell("box", cost_per_hour=bad)


def test_an_empty_shell_id_is_refused(root: Path) -> None:
    w = Warden(root=root)
    with pytest.raises(NeedsHuman):
        w.set_shell("  ", role="nas")


def test_an_undeclared_shell_does_not_appear(root: Path) -> None:
    """没登记就不出现 —— 缺席必须能被看见，而不是被默认值填满。"""
    w = Warden(root=root)
    w.set_shell("known", role="nas")
    assert "unknown-box" not in w.shells_view()


def test_declaring_is_logged(root: Path) -> None:
    w = Warden(root=root)
    w.set_shell("box", role="cloud", by="lead")
    log = w.log_tail(limit=5)
    assert any("壳属性" in line and "box" in line for line in log)


def test_status_exposes_the_shells(root: Path) -> None:
    w = Warden(root=root)
    w.set_shell("box", role="nas")
    assert w.status()["shells"]["box"]["role"] == "nas"


def test_the_view_does_not_leak_internal_keys(root: Path) -> None:
    """给外面看的东西要固定，否则每加一个内部字段都会泄漏进 API。"""
    w = Warden(root=root)
    w.set_shell("box", role="nas")
    assert set(w.shells_view()["box"]) == {
        "role",
        "gpu",
        "wakeable",
        "cost_per_hour",
        "label",
        "updated_at",
        "updated_at_readable",
        "updated_by",
        "declared",
    }
