"""`molt.notify` 的测试 —— 让"这次搬家"进它记忆的那条通道。

重点测两类东西：

1. **文案不说谎**：不知道的就不写。`generation` / `seconds` / `size_bytes`
   缺了必须整句消失，不能出现 `None` 或者 `大约`。
2. **它不会把一次成功的迁徙搞失败**：所有异常都必须收进返回值里。
   这条比"能发出去"重要 —— 通知是锦上添花，交接是命。
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from molt import notify


# --------------------------------------------------------------- 小假件


class _FakeResponse:
    def __init__(self, body: bytes = b"", status: int = 200):
        self._lines = body.splitlines(keepends=True) if body else []
        self.status = status

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return b"".join(self._lines)


def _sse(*events: dict) -> bytes:
    return b"".join(
        ("data: " + json.dumps(e, ensure_ascii=False) + "\n\n").encode("utf-8") for e in events
    )


# --------------------------------------------------------------- 文案


def test_announcement_names_both_machines():
    text = notify.build_announcement(src="nas-shell", dst="win-shell")
    assert "nas-shell" in text
    assert "win-shell" in text
    assert "迁徙完成" in text


def test_announcement_says_it_is_not_from_the_owner():
    """这句不能少：它确实不是主人打的字，不说就会被当成主人说的话去理解。"""
    text = notify.build_announcement(src="a", dst="b")
    assert "不是你主人打的字" in text


def test_announcement_omits_facts_it_does_not_have():
    text = notify.build_announcement(src="a", dst="b")
    assert "None" not in text
    assert "代" not in text
    assert "快照" not in text
    assert "秒" not in text


def test_announcement_includes_generation_and_timings():
    text = notify.build_announcement(
        src="a", dst="b", generation=13, seconds=272, size_bytes=28624617
    )
    assert "第 13 代" in text
    assert "4 分 32 秒" in text
    assert "27.3 MB" in text


def test_announcement_extra_line_is_optional():
    assert "注意" not in notify.build_announcement(src="a", dst="b")
    assert "注意" in notify.build_announcement(src="a", dst="b", extra="注意：这次是主人提的")


def test_announcement_falls_back_when_names_are_blank():
    text = notify.build_announcement(src="", dst="  ")
    assert "某台机器" in text and "另一台机器" in text


# --------------------------------------------------------------- 小工具


@pytest.mark.parametrize(
    "n,expect",
    [(0, "0 B"), (999, "999 B"), (1024, "1.0 KB"), (28624617, "27.3 MB"), (10**12, "931.3 GB")],
)
def test_human_size(n, expect):
    assert notify._human_size(n) == expect


def test_human_size_none_is_empty_not_a_guess():
    assert notify._human_size(None) == ""


@pytest.mark.parametrize(
    "s,expect",
    [(0, "0 秒"), (59, "59 秒"), (60, "1 分 0 秒"), (272, "4 分 32 秒"), (3661, "1 小时 1 分")],
)
def test_human_span(s, expect):
    assert notify._human_span(s) == expect


def test_human_span_none_is_empty():
    assert notify._human_span(None) == ""


# --------------------------------------------------------------- 协议


def test_payload_pins_webchat():
    """必须显式给 webchat：刚搬完家那一刻最不能依赖 QQ。"""
    body = json.loads(notify.build_payload("hi", "s1"))
    assert body == {"message": "hi", "session_id": "s1", "platform_id": "webchat"}


def test_parse_sse_line_accepts_only_data_lines():
    assert notify.parse_sse_line('data: {"type":"plain"}') == {"type": "plain"}
    assert notify.parse_sse_line("") is None
    assert notify.parse_sse_line("event: ping") is None
    assert notify.parse_sse_line("data: not-json") is None
    assert notify.parse_sse_line("data: [DONE]") is None
    assert notify.parse_sse_line('data: ["a"]') is None


def test_text_of_drops_reasoning():
    """思考过程不进正文 —— 只有说出口的那部分会变成对话记忆。"""
    assert notify.text_of({"chain_type": "reasoning", "data": "嗯…"}) == ""
    assert notify.text_of({"data": "你好"}) == "你好"
    assert notify.text_of({"data": {"text": "在"}}) == "在"
    assert notify.text_of({"data": {"content": "在"}}) == "在"
    assert notify.text_of({"data": {"message": "在"}}) == "在"
    assert notify.text_of({"data": {"nope": 1}}) == ""


def test_collect_reply_joins_plain_and_skips_lifecycle():
    body = _sse(
        {"type": "session_id", "data": "x"},
        {"type": "run_started"},
        {"type": "plain", "chain_type": "reasoning", "data": "让我想想"},
        {"type": "plain", "data": "我"},
        {"type": "plain", "data": "到了。"},
        {"type": "done"},
    )
    assert notify.collect_reply(io.BytesIO(body)) == "我到了。"


def test_collect_reply_tolerates_garbage_lines():
    assert notify.collect_reply([b"data: {", b"not sse", b"data: {\"type\":\"plain\",\"data\":\"ok\"}"]) == "ok"


# --------------------------------------------------------------- 等起来


def test_wait_for_dashboard_true_on_200():
    def opener(url, timeout=None):
        assert url.endswith("/api/auth/setup-status")
        return _FakeResponse(b"{}", status=200)

    assert notify.wait_for_dashboard("http://h:1", timeout=1, opener=opener) is True


def test_wait_for_dashboard_true_on_4xx():
    """4xx 也说明服务在应答了 —— 我们问的是"你起来了吗"，不是"你同意吗"。"""
    def opener(url, timeout=None):
        raise urllib.error.HTTPError(url, 404, "nope", {}, None)

    assert notify.wait_for_dashboard("http://h:1", timeout=1, opener=opener) is True


def test_wait_for_dashboard_retries_then_gives_up():
    calls = []
    sleeps = []
    clock = {"t": 0.0}

    def opener(url, timeout=None):
        calls.append(url)
        raise urllib.error.URLError("refused")

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    import time as real_time

    real_monotonic = real_time.monotonic
    try:
        real_time.monotonic = lambda: clock["t"]
        assert (
            notify.wait_for_dashboard(
                "http://h:1", timeout=5, interval=2, opener=opener, sleep=fake_sleep
            )
            is False
        )
    finally:
        real_time.monotonic = real_monotonic
    assert len(calls) >= 2
    assert sleeps and sleeps[0] == 2


def test_wait_for_dashboard_swallows_unexpected_errors():
    def opener(url, timeout=None):
        raise ValueError("怪东西")

    import time as real_time

    clock = {"t": 0.0}
    real_monotonic = real_time.monotonic
    try:
        real_time.monotonic = lambda: clock["t"]
        assert (
            notify.wait_for_dashboard(
                "http://h:1",
                timeout=1,
                interval=1,
                opener=opener,
                sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
            )
            is False
        )
    finally:
        real_time.monotonic = real_monotonic


# --------------------------------------------------------------- 投递


def test_announce_returns_reply_on_success():
    captured = {}

    def opener(request, timeout=None):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(_sse({"type": "plain", "data": "嗯，我记住这次搬家了。"}))

    result = notify.announce("http://h:1", "tok", "hello", opener=opener)
    assert result["ok"] is True
    assert result["reply"] == "嗯，我记住这次搬家了。"
    assert captured["url"].endswith("/api/v1/chat")
    assert captured["auth"] == "Bearer tok"
    assert captured["body"]["platform_id"] == "webchat"


def test_announce_reports_http_error_without_raising():
    def opener(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, io.BytesIO(b"bad token"))

    result = notify.announce("http://h:1", "tok", "hi", opener=opener)
    assert result["ok"] is False
    assert result["status"] == 401
    assert "401" in result["error"]


def test_announce_reports_unreachable_without_raising():
    def opener(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    result = notify.announce("http://127.0.0.1:6199", "tok", "hi", opener=opener)
    assert result["ok"] is False
    assert result["status"] == 0
    assert "连不上" in result["error"]


def test_announce_never_raises_on_weird_errors():
    """通知是锦上添花，交接是命。这条路径不许把进程带崩。"""
    def opener(request, timeout=None):
        raise RuntimeError("boom")

    result = notify.announce("http://h:1", "tok", "hi", opener=opener)
    assert result["ok"] is False
    assert "RuntimeError" in result["error"]


def test_announce_tolerates_str_lines():
    class _StrResponse(_FakeResponse):
        def __iter__(self):
            return iter(['data: {"type":"plain","data":"好"}\n'])

    result = notify.announce("http://h:1", "tok", "hi", opener=lambda r, timeout=None: _StrResponse())
    assert result["ok"] is True
    assert result["reply"] == "好"


# --------------------------------------------------------------- CLI


def test_cli_announce_dry_run_does_not_touch_anything(capsys):
    from molt import cli

    rc = cli.main(
        [
            "announce",
            "--config",
            "no-such-file.json",
            "--src",
            "nas-shell",
            "--dst",
            "win-shell",
            "--generation",
            "13",
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "nas-shell" in out and "win-shell" in out
    assert "第 13 代" in out
    assert "只拼文案" in out


def test_cli_announce_reports_missing_credentials(capsys, tmp_path):
    """配置读不到 → 2（参数/配置问题），不能假装成功。"""
    from molt import cli

    rc = cli.main(
        [
            "announce",
            "--config",
            str(tmp_path / "missing.json"),
            "--src",
            "a",
            "--dst",
            "b",
            "--wait",
            "0",
        ]
    )
    assert rc == 2


def test_collect_reply_does_not_print_the_same_sentence_twice():
    """真机第一次跑就撞到的：AstrBot 后面会重发一遍"到目前为止的全文"。

    直接拼接会把同一句印两遍（2026-11-15 02:4x 实测）。
    """
    body = _sse(
        {"type": "plain", "data": "收到，第 13 代了。"},
        {"type": "plain", "data": "收到，第 13 代了。"},
    )
    assert notify.collect_reply(io.BytesIO(body)) == "收到，第 13 代了。"


def test_collect_reply_handles_growing_full_text():
    """越重发越长的形状（全量覆盖）也只能留最后那一份。"""
    body = _sse(
        {"type": "plain", "data": "我"},
        {"type": "plain", "data": "我刚到家"},
        {"type": "plain", "data": "我刚到家，东西都在。"},
    )
    assert notify.collect_reply(io.BytesIO(body)) == "我刚到家，东西都在。"


def test_collect_reply_still_concatenates_true_deltas():
    """真增量（每条都不以已有的开头）必须接起来，不能只留最后一段。"""
    body = _sse(
        {"type": "plain", "data": "我"},
        {"type": "plain", "data": "到了"},
        {"type": "plain", "data": "。"},
    )
    assert notify.collect_reply(io.BytesIO(body)) == "我到了。"