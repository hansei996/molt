"""文档不能在"两次确认"和"封包由账本发"这两件事上悄悄退化。

这一组不测行为，只测**话术还在不在**。理由是一次真实事故：
2026-11-16 00:57，代码里的两次确认与 `/wake` 都跑通了，但 README / INSTALL
还写着"`/molt go` 然后 `/molt confirm`"—— 一个照文档操作的人会以为一次确认
就能搬家，于是在机器关着的时候按了 confirm，得到"还没上线"的回复，
合理地以为功能坏了。

文档和代码不一致，不会让 CI 变红；所以这里用最笨的办法钉住它。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_readme_talks_about_two_confirmations() -> None:
    text = _read("README.md")
    assert "两次确认" in text
    assert "第一次确认只负责开机" in text or "第一次：只发唤醒包" in text
    assert "/molt confirm" in text


def test_readme_says_the_ledger_sends_the_packet() -> None:
    text = _read("README.md")
    assert "POST /wake" in text
    assert "docker0" in text, "必须留下'容器发出的广播出不了 docker0'这条理由"


def test_install_walks_through_two_confirms() -> None:
    text = _read("docs/INSTALL.md")
    assert "第一次：只发唤醒包" in text
    assert "第二次：它先自己再核一遍心跳" in text
    assert "molt declare --shell" in text and "--wake-mac" in text


def test_selfuse_documents_the_container_bridge_reason() -> None:
    text = _read("docs/SELFUSE.md")
    assert "两次确认" in text
    assert "docker0" in text


def test_arch_keeps_the_incident_and_the_state_machine() -> None:
    text = _read("docs/ARCH.md")
    assert "14.21 两次确认" in text
    assert "phase" in text
    assert "co_varnames" in text or "永远为真" in text, "那次'判据永远为真'的教训不能删"
