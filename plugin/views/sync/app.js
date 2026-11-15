// Molt · 插件配置同步 —— dashboard 页面
//
// 这一页只做四件事，和 CLI 的 publish-plugins / sync-plugins 一一对应：
//   读状态 → 发布这台 → 预览（空跑）→ 同步（要勾确认）
//
// 所有真正读盘/写盘的动作都在后端做（`plugin/sync_api.py`），页面只负责
// 显示和收集"从哪台、要不要真的写"。这样即使页面被关掉，规则也只有一份。
//
// 反馈这件事在本文件里被当成功能的一部分，而不是装饰：
//   · 每个动作结束都往顶部横幅写一条带时间的结论（成功/失败/进行中）
//   · 同步成功后额外列一份"到底写了哪几个插件"的清单
//   · 按钮在请求进行中改文字并禁用 —— 点了没反应是最糟的反馈

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const $ = (id) => document.getElementById(id);

const ACTION_LABEL = {
  write: ["会写", "write"],
  same: ["一样，不用动", "same"],
  skip_schema: ["结构不一样，跳过", "skip"],
  keep_local: ["那边没配置，保留这边", "plain"],
  only_remote: ["那边有、这边没装 —— 不装也不写", "plain"],
  only_local: ["这边独有 —— 不动", "plain"],
};

// 横幅的四种状态：idle（还没做事）/ busy / ok / bad
const BANNER_ICON = { idle: "•", busy: "⏳", ok: "✓", bad: "✕" };

let peers = [];
let currentSource = "";
let writable = 0;
let lastPublishedAt = "";

function nowText() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/**
 * 顶部横幅：每个动作的结论都落在这里。
 * @param {"idle"|"busy"|"ok"|"bad"} kind
 * @param {string} text 一句话结论
 * @param {string} [detail] 第二行细节（可省）
 */
function banner(kind, text, detail) {
  const box = $("banner");
  box.classList.remove("idle", "busy", "ok", "bad");
  box.classList.add(kind);
  $("banner-icon").textContent = BANNER_ICON[kind] || "•";
  $("banner-text").textContent = text || "";
  $("banner-detail").textContent = detail || "";
  $("banner-time").textContent = kind === "busy" ? "" : nowText();
}

// 「同步」按钮只有一个条件：确实有东西可写，而且人勾了确认。
// 两个条件分开判断，是因为它们各自都会变（预览结果变、勾选变）。
function syncApplyButton() {
  $("apply").disabled = !(writable > 0 && $("confirm").checked);
}

function setResult(el, text, kind) {
  el.textContent = text || "";
  el.classList.remove("bad", "good");
  if (kind) el.classList.add(kind);
}

function describeError(err) {
  if (!err) return "未知错误";
  if (typeof err === "string") return err;
  if (err.message) return err.message;
  try {
    return JSON.stringify(err);
  } catch {
    return String(err);
  }
}

function kb(bytes) {
  if (!bytes) return "—";
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

async function refreshState(quiet) {
  if (!quiet) banner("busy", "正在读这台机器的状态…");
  const state = await bridge.apiGet("sync/state");

  $("shell").textContent = state.shell || "（没配置 shell）";
  $("data-dir").textContent = state.data_dir || "—";
  $("plugin-count").textContent = String((state.local_plugins || []).length);
  $("data-hint").textContent = state.data_dir_exists
    ? `其中 ${(state.local_configs || []).length} 个插件有配置文件（只有这些会被打包）。`
    : "这个目录不存在 —— 检查插件配置里的 data_dir。";

  peers = state.peers || [];
  renderPeers();

  if (!quiet) {
    banner(
      "ok",
      `这台的配置在账本里${peers.length ? `；另外 ${peers.length} 台也发布过` : "还没有别的壳发布过"}`,
      lastPublishedAt ? `本页上一次发布：${lastPublishedAt}` : ""
    );
  }
}

function renderPeers() {
  const box = $("peers");
  box.innerHTML = "";

  if (!peers.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = "账本里还没有别的壳发布过配置包。先在另一台上点「发布这台」。";
    box.appendChild(p);
    return;
  }

  for (const peer of peers) {
    const row = document.createElement("div");
    row.className = "peer";

    const left = document.createElement("div");
    const name = document.createElement("code");
    name.textContent = peer.shell;
    const meta = document.createElement("div");
    meta.className = "meta";
    const when = peer.updated_at_readable || peer.created_at_readable || "不知道什么时候";
    const count = (peer.plugins || []).length;
    meta.textContent = `${when} · ${count} 个插件 · ${kb(peer.bytes)}`;

    left.append(name, meta);

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "small";
    btn.textContent = "预览";
    btn.addEventListener("click", () => preview(peer.shell));

    row.append(left, btn);
    box.appendChild(row);
  }
}

async function publish() {
  const btn = $("publish");
  btn.disabled = true;
  btn.textContent = "正在打包上传…";
  setResult($("publish-result"), "进行中…");
  banner("busy", "正在把这台的插件配置打包上传到账本…");
  try {
    const res = await bridge.apiPost("sync/publish", {});
    lastPublishedAt = nowText();
    // 后端给 plugin_count（数字）与 plugins（名字数组）。两个都兼容，
    // 免得哪天字段又变回数字，页面把 undefined 印成「undefined 个插件」。
    const count = Number(
      res.plugin_count ||
        (Array.isArray(res.plugins) ? res.plugins.length : 0) ||
        0
    );
    const summary = `${count} 个插件的配置 · ${kb(res.bytes)}`;
    setResult($("publish-result"), `已发布 ${lastPublishedAt} · ${summary}`, "good");
    banner(
      "ok",
      `已发布，账本收下了（${summary}）`,
      `第 1 步完成 ${lastPublishedAt}。现在可以让另一台来「预览」了。`
    );
    await refreshState(true);
  } catch (err) {
    setResult($("publish-result"), "发布失败", "bad");
    banner("bad", `发布失败：${describeError(err)}`, "什么都没写进账本。");
  } finally {
    btn.disabled = false;
    btn.textContent = "发布这台";
  }
}

async function preview(source) {
  currentSource = source;
  $("preview-card").hidden = false;
  $("preview-source").textContent = source;
  $("rows").innerHTML = "";
  $("counts").textContent = "正在预览（空跑）…";
  $("confirm").checked = false;
  $("applied").hidden = true;
  writable = 0;
  $("apply").disabled = true;
  setResult($("apply-result"), "");
  banner("busy", `正在跟 ${source} 的那份比较…`);

  try {
    // 注意参数名是 from —— 它是关键字，所以这里用对象字面量的字符串键。
    const res = await bridge.apiGet("sync/preview", { from: source });
    renderRows(res);
    const drift = res.version_mismatch || {};
    const driftNames = Object.keys(drift);
    let note = `空跑，一个字都没写。来源 ${source}（${res.source_created_at || "时间未知"}）。`;
    if (driftNames.length) {
      const sample = driftNames
        .slice(0, 3)
        .map((n) => `${n} 对面 ${drift[n].remote} / 这边 ${drift[n].local}`)
        .join("；");
      note += ` 另外 ${driftNames.length} 个插件两边版本不一样：${sample}。插件代码不跟着快照走，先决定升级哪一台。`;
    }
    banner("ok", `预览完了：${res.writable || 0} 个可以写过来`, note);
  } catch (err) {
    $("counts").textContent = "";
    banner("bad", `预览失败：${describeError(err)}`, "空跑失败不会改任何文件。");
  }
}

function renderRows(res) {
  const rows = $("rows");
  rows.innerHTML = "";

  const counts = res.counts || {};
  const parts = [];
  if (counts.write) parts.push(`${counts.write} 个会写`);
  if (counts.same) parts.push(`${counts.same} 个一样`);
  if (counts.skip_schema) parts.push(`${counts.skip_schema} 个结构不一样`);
  if (counts.keep_local) parts.push(`${counts.keep_local} 个那边没配置`);
  if (counts.only_remote) parts.push(`${counts.only_remote} 个那边独有`);
  if (counts.only_local) parts.push(`${counts.only_local} 个这边独有`);
  $("counts").textContent =
    (res.has_manifest ? "" : "（这个包没有清单，只能按哪些插件有配置来判断）") +
    (parts.length ? parts.join(" · ") : "没有可比较的插件");

  const data = res.rows || [];
  if (!data.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 3;
    td.className = "empty";
    td.textContent = "没有两边都装了的插件。";
    tr.appendChild(td);
    rows.appendChild(tr);
  }

  for (const item of data) {
    const tr = document.createElement("tr");
    if (item.action === "write") tr.classList.add("row-write");

    const name = document.createElement("td");
    name.textContent = item.plugin;

    const action = document.createElement("td");
    const [label, kind] = ACTION_LABEL[item.action] || [item.action || "?", "plain"];
    const badge = document.createElement("span");
    badge.className = `badge ${kind}`;
    badge.textContent = label;
    action.appendChild(badge);

    const detail = document.createElement("td");
    detail.className = "meta";
    detail.textContent = item.detail || (item.bytes ? `${item.bytes} 字节` : "");

    tr.append(name, action, detail);
    rows.appendChild(tr);
  }

  // 只有"有东西可写"时才允许同步 —— 一个点了什么都不会发生的按钮，
  // 会让人以为是同步失败了。
  writable = Number(res.writable || 0);
  syncApplyButton();
  if (!writable) {
    setResult($("apply-result"), "没有需要写的东西。");
  } else {
    setResult($("apply-result"), `勾上确认就能写这 ${writable} 个。`);
  }
}

async function apply() {
  if (!$("confirm").checked) return;
  const btn = $("apply");
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "正在写…";
  setResult($("apply-result"), "进行中…");
  banner("busy", `正在把 ${currentSource} 的配置写到这台…`);
  const startedAt = nowText();
  try {
    const res = await bridge.apiPost("sync/apply", {
      from: currentSource,
      confirm: true,
    });
    const written = res.updated || [];
    const backups = Number(res.backups || 0);

    setResult(
      $("apply-result"),
      `写好了 ${written.length} 个 · 备份 ${backups} 个`,
      "good"
    );
    banner(
      "ok",
      `同步完成：写入 ${written.length} 个，备份 ${backups} 个（${startedAt}）`,
      written.length
        ? `写了：${written.join("、")}。要让它生效，重启 AstrBot。`
        : "没有需要改的文件。"
    );

    // 把"到底写了哪几个"留在页面上 —— 表格刷新后会变成"一样，不用动"，
    // 那时只有这份清单还能回答"刚才那次真的写了吗"。
    $("applied").hidden = false;
    $("applied-title").textContent = `刚才这次（${startedAt}）写入了 ${written.length} 个文件：`;
    const list = $("applied-list");
    list.innerHTML = "";
    for (const name of written) {
      const li = document.createElement("li");
      li.textContent = name;
      list.appendChild(li);
    }
    if (!written.length) {
      const li = document.createElement("li");
      li.className = "meta";
      li.textContent = "（这次没有文件被改动）";
      list.appendChild(li);
    }

    // 再预览一次：表格应当从「会写」变成「一样，不用动」——这是第二份证据。
    const source = currentSource;
    await preview(source);
    $("applied").hidden = false;
  } catch (err) {
    setResult($("apply-result"), "写入失败", "bad");
    banner("bad", `写入失败：${describeError(err)}`, "没有文件被改动（或者改动已中止）。");
  } finally {
    btn.disabled = false;
    btn.textContent = label;
    syncApplyButton();
  }
}

async function main() {
  if (!bridge) {
    banner("bad", "这个页面必须在 AstrBot dashboard 里打开 —— 拿不到 bridge。");
    return;
  }
  await bridge.ready();
  $("refresh").addEventListener("click", () => {
    refreshState().catch((err) => banner("bad", `读不到状态：${describeError(err)}`));
  });
  $("publish").addEventListener("click", publish);
  $("apply").addEventListener("click", apply);
  $("confirm").addEventListener("change", syncApplyButton);
  try {
    await refreshState(true);
  } catch (err) {
    banner("bad", `读不到状态：${describeError(err)}`, "检查插件配置里的 data_dir 与账本地址。");
  }
}

main();
