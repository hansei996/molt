"""把账本里的历史渲染成一页可以直接看的 HTML。

## 为什么要有这个模块

`molt status` 回答「现在是谁在线」，`molt backups` 回答「有哪些底」，
而「**它到底搬过几次家、每次谁推的、多少字节、谁取走的**」这个问题，
在 v1.0 之前只存在于日志里 —— 一条一行，得自己拼。

开源发布要的恰恰是这一页：陌生人点开一个 HTML，看见那条时间轴，
才相信「同一个它真的换了身体」。

## 三条硬约束（都是踩过的坑）

1. **零外部资源**。没有 CDN、没有字体文件、没有图片、没有 `<script>`。
   这一页会在手机上、在别人的机器上、在断网的 NAS 旁边被打开，
   任何一次外部请求失败都会让"它搬过几次家"变成一张白屏。
2. **所有插值都过 `esc()`**。账本里的 `source_host` / `pushed_by` / 备份名
   都是别人写下来的字符串，直接拼进 HTML 就是存储型 XSS。
3. **纯函数**。给定一个 dict 就渲染出一个字符串，不碰文件、不碰网络、
   不依赖当前时间 —— 这样它能在测试里被钉住，也能被 `molt timeline`
   和 `GET /timeline` 同时使用（同一份渲染，才不会出现"命令行看到的和
   网页看到的不一样"）。
"""

from __future__ import annotations

import html as _html
from typing import Any, Dict, Iterable, List


def esc(value: Any) -> str:
    """转义一切要进 HTML 文本或属性值的东西。"""
    return _html.escape("" if value is None else str(value), quote=True)


def _human_bytes(n: Any) -> str:
    try:
        size = float(n or 0)
    except (TypeError, ValueError):
        return "—"
    if size <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


_STYLE = """
  :root { --ink:#1f2328; --dim:#6b7280; --line:#e5e7eb; --ok:#0d7a4a; --warn:#b45309; }
  * { box-sizing: border-box; }
  body { margin:0; padding:32px 20px 64px; background:#fff; color:var(--ink);
         font: 15px/1.65 -apple-system, "Segoe UI", "Noto Sans SC", "Microsoft YaHei", sans-serif; }
  .wrap { max-width: 860px; margin: 0 auto; }
  h1 { font-size: 26px; margin: 0 0 6px; letter-spacing: .5px; }
  .sub { color: var(--dim); margin: 0 0 26px; }
  .cards { display:flex; flex-wrap:wrap; gap:12px; margin: 0 0 30px; }
  .card { flex:1 1 150px; border:1px solid var(--line); border-radius:10px; padding:12px 14px; }
  .card .k { color: var(--dim); font-size:12px; }
  .card .v { font-size:20px; font-weight:600; margin-top:2px; }
  h2 { font-size:16px; margin: 34px 0 12px; padding-bottom:6px; border-bottom:1px solid var(--line); }
  .gen { border-left:3px solid var(--line); padding: 0 0 0 18px; margin: 0 0 22px; position:relative; }
  .gen::before { content:""; position:absolute; left:-7px; top:6px; width:11px; height:11px;
                 border-radius:50%; background:#fff; border:2px solid var(--dim); }
  .gen.done::before { border-color: var(--ok); }
  .gen.pending::before { border-color: var(--warn); }
  .gen .title { font-weight:600; font-size:16px; }
  .gen .when { color: var(--dim); font-size:13px; margin-left:8px; font-weight:400; }
  .gen .row { color: var(--dim); font-size:13px; margin-top:4px; }
  .gen .row b { color: var(--ink); font-weight:600; }
  .badge { display:inline-block; font-size:12px; border-radius:999px; padding:1px 9px; margin-left:6px; }
  .badge.ok { color: var(--ok); border:1px solid #b7e2cc; }
  .badge.warn { color: var(--warn); border:1px solid #f3d9b0; }
  table { width:100%; border-collapse: collapse; font-size:13px; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { color: var(--dim); font-weight:500; }
  pre { background:#f6f8fa; border:1px solid var(--line); border-radius:8px; padding:12px 14px;
        overflow-x:auto; font: 12px/1.6 ui-monospace, SFMono-Regular, Consolas, monospace; color:#24292f; }
  .empty { color: var(--dim); padding: 10px 0; }
  footer { margin-top: 40px; color: var(--dim); font-size:12px; border-top:1px solid var(--line); padding-top:12px; }
"""


def _fingerprint(gen: Dict[str, Any]) -> str:
    """这一代归档的指纹，取不到就老实说「—」。

    `artifact_sha256` 是收归档时算出来、写在归档旁边的（见
    `Warden.archive_sha_path`）。早期那几代还没有这份 sidecar，
    退回到 manifest 自带的值（历史上一直是空的）。
    **空就是空，不补一个假的** —— 这一列的意义正是"能不能核对"。
    """
    for key in ("artifact_sha256", "manifest_sha256"):
        value = str(gen.get(key) or "").strip()
        if value:
            return value[:16]
    return "—"


def _gen_block(gen: Dict[str, Any]) -> str:
    number = gen.get("generation")
    when = gen.get("created_at_readable") or ""
    source = gen.get("source_host") or "—"
    pushed = gen.get("pushed_by") or "—"
    target = gen.get("target_shell") or ""
    consumed_by = gen.get("consumed_by") or ""
    consumed_at = gen.get("consumed_at_readable") or ""

    if consumed_by:
        state_cls, state_text = "done", f"已被 {consumed_by} 取走"
    elif target:
        state_cls, state_text = "pending", f"还没人取走（目标是 {target}）"
    else:
        state_cls, state_text = "pending", "还没人取走"

    rows: List[str] = [
        f'<div class="row">从 <b>{esc(source)}</b> 出发 · 由 <b>{esc(pushed)}</b> 提交'
        f' · epoch {esc(gen.get("epoch"))}</div>',
        f'<div class="row">数据 <b>{esc(_human_bytes(gen.get("artifact_bytes")))}</b>'
        f' · 指纹 <code>{esc(_fingerprint(gen))}</code></div>',
    ]
    if consumed_at:
        rows.append(f'<div class="row">落位时间 <b>{esc(consumed_at)}</b></div>')

    return (
        f'<div class="gen {state_cls}">'
        f'<div class="title">第 {esc(number)} 代<span class="when">{esc(when)}</span>'
        f'<span class="badge {"ok" if state_cls == "done" else "warn"}">{esc(state_text)}</span></div>'
        + "".join(rows)
        + "</div>"
    )


def _backups_table(backups: Iterable[Dict[str, Any]]) -> str:
    items = list(backups)
    if not items:
        return '<div class="empty">还没有中途备份（守护进程的 backup_interval 到点才会推）。</div>'
    rows = []
    for b in items:
        rows.append(
            "<tr>"
            f'<td>{esc(b.get("created_at_readable") or "—")}</td>'
            f'<td>{esc(b.get("shell") or "—")}</td>'
            f'<td>{esc(_human_bytes(b.get("bytes")))}</td>'
            f'<td>第 {esc(b.get("generation") or 0)} 代之后</td>'
            f'<td><code>{esc(b.get("name") or "")}</code></td>'
            "</tr>"
        )
    return (
        "<table><thead><tr><th>时间</th><th>来自</th><th>大小</th><th>基线</th><th>文件</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )


def _beats_table(beats: Dict[str, Any]) -> str:
    if not beats:
        return '<div class="empty">一个壳都没在报心跳 —— 鱼可能真的没了，但账本自己看不出这一点。</div>'
    rows = []
    for shell, beat in sorted(beats.items()):
        age = beat.get("age_readable") or "—"
        stale = bool(beat.get("stale"))
        cls = "warn" if stale else "ok"
        text = "哑了" if stale else "在跑"
        label = beat.get("label") or ""
        name = f"{label}（{shell}）" if label else shell
        rows.append(
            "<tr>"
            f'<td>{esc(name)}</td>'
            f'<td><span class="badge {cls}">{text}</span></td>'
            f'<td>{esc(age)}</td>'
            f'<td>{esc(beat.get("note") or "—")}</td>'
            f'<td>第 {esc(beat.get("generation") or 0)} 代</td>'
            "</tr>"
        )
    return (
        "<table><thead><tr><th>壳</th><th>状态</th><th>最后心跳</th><th>角色</th><th>手里</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table>"
    )


def render_timeline(history: Dict[str, Any], *, title: str = "它搬过几次家") -> str:
    """渲染一页完整的时间轴。`history` 就是 `Warden.history()` 的返回值。"""
    generations: List[Dict[str, Any]] = list(history.get("generations") or [])
    backups = list(history.get("backups") or [])
    beats = dict(history.get("heartbeats") or {})
    log_lines = list(history.get("log") or [])

    done = sum(1 for g in generations if g.get("consumed_by"))
    cards = [
        ("已经搬过", f"{len(generations)} 次"),
        ("完成落地", f"{done} 次"),
        ("搬过的数据", _human_bytes(history.get("total_bytes"))),
        ("当前 epoch", esc(history.get("current_epoch") or 0)),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="k">{esc(k)}</div><div class="v">{v if k == "搬过的数据" else esc(v)}</div></div>'
        for k, v in cards
    )

    if generations:
        # 新的在上面：人打开这一页想知道的是"最近一次",而不是"第一次"。
        timeline_html = "".join(_gen_block(g) for g in reversed(generations))
    else:
        timeline_html = (
            '<div class="empty">账本里还没有任何一代 —— 这只鱼还没搬过家。'
            "先在它现在待的那台机器上敲 <code>/molt go</code>。</div>"
        )

    log_html = (
        "<pre>" + esc("\n".join(log_lines)) + "</pre>"
        if log_lines
        else '<div class="empty">还没有日志。</div>'
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{_STYLE}</style>
</head>
<body>
<div class="wrap">
  <h1>它搬过几次家</h1>
  <p class="sub">同一个它，换了几次身体。数据来自账本本身 —— 这里没有一条是编的。</p>

  <div class="cards">{cards_html}</div>

  <h2>迁徙时间轴</h2>
  {timeline_html}

  <h2>中途备份</h2>
  <p class="sub" style="margin-top:-6px">这些不进时间轴：它们不是交接，只是"每十分钟留一份底"。</p>
  {_backups_table(backups)}

  <h2>现在谁还在报</h2>
  {_beats_table(beats)}

  <h2>最近日志</h2>
  {log_html}

  <footer>
    生成于 {esc(history.get("generated_at_readable") or "")}
    · 账本 {esc(history.get("root") or "")}
    · 拒绝过的倒序写入 {esc(history.get("rejected_write_count") or 0)} 次
  </footer>
</div>
</body>
</html>
"""
