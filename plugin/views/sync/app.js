// Molt · 插件配置同步 —— dashboard 页面
//
// 这一页只做四件事，和 CLI 的 publish-plugins / sync-plugins 一一对应：
//   读状态 → 发布这台 → 预览（空跑）→ 同步（要勾确认）
//
// 所有真正读盘/写盘的动作都在后端做（`plugin/sync_api.py`），页面只负责
// 显示和收集"从哪台、要不要真的写"。这样即使页面被关掉，规则也只有一份。

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

let peers = [];
let currentSource = "";
let writable = 0;

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

async function refreshState() {
  setResult($("publish-result"), "");
  const state = await bridge.apiGet("sync/state");

  $("shell").textContent = state.shell || "（没配置 shell）";
  $("data-dir").textContent = state.data_dir || "—";
  $("plugin-count").textContent = String((state.local_plugins || []).length);
  $("data-hint").textContent = state.data_dir_exists
    ? `其中 ${(state.local_configs || []).length} 个插件有配置文件（只有这些会被打包）。`
    : "这个目录不存在 —— 检查插件配置里的 data_dir。";

  peers = state.peers || [];
  renderPeers();
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
    const when = peer.created_at_readable || "（不知道什么时候）";
    const count = (peer.plugins || []).length;
    const size = peer.bytes ? `${Math.max(1, Math.round(peer.bytes / 1024))} KB` : "—";
    meta.textContent = `${when} · ${count} 个插件 · ${size}`;

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
  setResult($("publish-result"), "正在打包并上传…");
  try {
    const res = await bridge.apiPost("sync/publish", {});
    setResult(
      $("publish-result"),
      `已发布：${res.plugins} 个插件的配置，${Math.max(1, Math.round(res.bytes / 1024))} KB`,
      "good"
    );
    await refreshState();
  } catch (err) {
    setResult($("publish-result"), `发布失败：${describeError(err)}`, "bad");
  } finally {
    btn.disabled = false;
  }
}

async function preview(source) {
  currentSource = source;
  $("preview-card").hidden = false;
  $("preview-source").textContent = source;
  $("rows").innerHTML = "";
  $("counts").textContent = "正在预览…";
  $("confirm").checked = false;
  writable = 0;
  $("apply").disabled = true;
  setResult($("apply-result"), "");

  try {
    // 注意参数名是 from —— 它是关键字，所以这里用对象字面量的字符串键。
    const res = await bridge.apiGet("sync/preview", { from: source });
    renderRows(res);
  } catch (err) {
    $("counts").textContent = "";
    setResult($("apply-result"), `预览失败：${describeError(err)}`, "bad");
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
    setResult($("apply-result"), "没有需要写的东西。", "");
  }
}

async function apply() {
  if (!$("confirm").checked) return;
  const btn = $("apply");
  btn.disabled = true;
  setResult($("apply-result"), "正在写…");
  try {
    const res = await bridge.apiPost("sync/apply", {
      from: currentSource,
      confirm: true,
    });
    const written = (res.updated || []).length;
    setResult(
      $("apply-result"),
      `写好了 ${written} 个，旧文件备份 ${res.backups} 个。AstrBot 读的是启动时载入的配置 —— 要让它生效还得重启那边。`,
      "good"
    );
    await preview(currentSource);
  } catch (err) {
    setResult($("apply-result"), `写入失败：${describeError(err)}`, "bad");
  } finally {
    btn.disabled = false;
  }
}

async function main() {
  if (!bridge) {
    $("foot").textContent = "这个页面必须在 AstrBot dashboard 里打开（拿不到 bridge）。";
    return;
  }
  await bridge.ready();
  $("refresh").addEventListener("click", () => {
    refreshState().catch((err) => setResult($("publish-result"), describeError(err), "bad"));
  });
  $("publish").addEventListener("click", publish);
  $("apply").addEventListener("click", apply);
  $("confirm").addEventListener("change", syncApplyButton);
  try {
    await refreshState();
  } catch (err) {
    setResult($("publish-result"), `读不到状态：${describeError(err)}`, "bad");
  }
}

main();
