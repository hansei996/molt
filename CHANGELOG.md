# 变更记录

这个项目只记**行为变了什么**，不记内部重构。版本号是 `molt` 包 / 镜像 / PyPI 上
`molt-handoff` 的版本；`v1.3.0` 及之后按"一批可验收的功能"发一版。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

---

## [未发布] v1.3.0

> 代码在 `main` 上、CI 绿、演练脚本 20/20 通过，但**还没有打 tag、没有推 PyPI / ghcr**。
> 发不发等老大点头（见 `docs/DECISIONS.md` 的「等时机」）。

### 新增：插件配置同步

一台机器上改完插件配置，另一台机器不用再手动抄一遍。**只同步两边都装了的插件。**

- CLI
  - `molt publish-plugins --data <AstrBot data 目录> --shell <壳名>` —— 把本机的插件配置
    打包传进账本（包里有配置 + 一份清单，清单说明"这台装了哪些插件"）。
  - `molt sync-plugins --data <data 目录> --from <另一台壳名>` —— **空跑**，只打印会改什么。
    确认无误后加 `--apply` 才真的写；`--force` 才会覆盖"结构看起来不一样"的那些。
- 账本
  - `POST /plugin-configs`（上传，头带 `X-Molt-Shell`）、`GET /plugin-configs`（列表）、
    `GET /plugin-configs/<壳名>`（下载）。每个壳只保留最新一份。
- dashboard 页面：**插件 → `Molt 蜕壳 · 控制面` → 页面 `sync`**
  - 「发布这台」→ 「刷新」→ 选来源 → 「预览」→ 勾「我确认覆盖」→ 「写这 N 个」。
  - 顶部横幅给出「刚才那次成了没有、什么时候、改了哪几个」，按钮进行中会改文案并禁用；
    写完之后单独列一份**这一次写入了哪几个文件**（表格会变，这份清单不会）。
  - 后端要求 `confirm: true`，只在前端点勾是绕不过去的。

### 变更：措辞

- 「第 N 代」→ **「第 N 次搬家」/「快照 #N」**。鱼没有重生：停的是进程，续的是同一个库、
  同一份记忆、同一个人格。给鱼看的播报里不再出现编号
  （「我刚从 UGREEN-776B 那边过来 —— 中间睡了一觉。」）。
- 协议字段 `generation` / `--generation N` **没有改**（改它等于改线上格式，零收益）。

### 修复

- dashboard 同步页第一次发布时出现「**undefined 个插件的配置**」：`do_publish` 返回的
  `plugins` 是数字，页面按数组取 `.length`。现在 `plugins` 一律是名字数组，数量另给
  `plugin_count`，页面两者都认，并有测试钉着。
- 同步成功后表格被刷成「一样，不用动」、看起来像什么都没发生 —— 现在会保留一份写入清单。

### 怎么回滚（同步只动 `data/config/<插件>_config.json`）

同步**不装插件、不删插件、不碰 `plugin_data`**，回滚也只需要动配置：

```powershell
$cfg = "G:\molt-fish\data\config"

# 1) 看有哪些备份（每次覆盖都会留一份，带时间戳）
Get-ChildItem $cfg -Filter *_config.json.molt-before-sync-* |
  Sort-Object LastWriteTime -Descending | Select-Object -First 10 Name, LastWriteTime

# 2) 把某一份换回去（例：astrbot_plugin_bili_watch）
Copy-Item "$cfg\astrbot_plugin_bili_watch_config.json.molt-before-sync-20261115T193000" `
          "$cfg\astrbot_plugin_bili_watch_config.json" -Force

# 3) 让它生效：重载那些插件（不用整机重启）
python tools\reload-plugin.py --config G:\molt-fish\data\cmd_config.json `
    astrbot_plugin_bili_watch
```

- 重载只会 `Terminating plugin … → Loading plugin …`，聊天连接不重连；
  **页面上的「刷新」不会重载插件**，它只是重新读账本。

- 备份命名：`<插件名>_config.json.molt-before-sync-YYYYMMDDTHHMMSS`
- **备份不会被自动删除**；确认没问题后自己清理即可。
- 如果连备份都没了：`data/config/` 里的配置会被插件重新生成一份默认值
  （代价是那台的个性化设置丢了，但插件不会坏）。

- **搬家会带走"记忆类"的 plugin_data**（2026-11-15 决定）：
  `astrbot_plugin_private_companion` 的 `companions.json` 与 141 张表的 `companion-v2.sqlite3`
  现在真的会跟着快照走（此前 `COPY_ARTIFACTS` 是一张从没被代码读过的清单）。
  缓存目录（`meme_manager`）不搬；`--with-plugin-data 插件名` 可以追加；
  单个记忆目录超过 256 MB 会**整目录不搬**并报错，而不是搬一半。

- 搬家会把 `<插件>_config.json` 一起带走，落位时只写**这台壳也装了的**插件；
  结构差太多的跳过、机器绑定的键（如 dsh_connector 的 `http_base_url`）保留本机的，
  每次都留 `.molt-before-land-*` 备份。不想带就加 `molt land --no-plugin-configs`。

- 落位前的停机多了一条兜底：优雅停机超时后 `taskkill /T /F` 整棵进程树。
  之前只有一条路径，in-place 重启出来的子进程打不动它，agent 会每 40 秒失败一次，
  而鱼卡在「两边都没连 QQ」的半路（2026-11-15 21:30 真机）。

### 已知限制

- **插件版本漂移会被念出来**：同步包里带每个插件的版本（\metadata.yaml\ 的
  \ersion:\），预览时若两边版本不同，CLI 与面板都会说清「对面 7.1.18 / 这边 6.5.6」。
  真机背景：\strbot_plugin_private_companion\ 两台分别是 7.1.18 与 6.5.6
  （配置顶层键 1008 vs 15）。**插件代码仍然不跟着快照走**；要不要同步代码见
  \docs/DECISIONS.md\ 的 D6。

- **只同步配置，不同步插件代码**。代码属于"这台壳装了什么"，配置属于"这只鱼喜欢什么"。
- **只同步两边都装了的插件**；只在一边有的，不装、不写、不删（这是主人 2026-11-15 定的规则）。
- 顶层键几乎不重合（重合率 < 20%）的配置**默认跳过**，因为它通常意味着两边插件版本不同；
  要覆盖得显式 `--force`（页面上没有这个开关，只有 CLI 有）。
- 写完之后要让 AstrBot **重新载入那些插件**才生效：插件配置是插件载入时从磁盘读的
  （`astrbot/core/star/star_manager.py:1167` 构造 `AstrBotConfig(config_path=...)`）。
  重载插件 → 不用掉线；整机重启 → 也会生效，只是更重。

---

## [未发布] v1.2.1（并入 1.3.0，不会单独发）

### 修复

- **`wakeable` 从"被打印"变成"参与判断"**：账本说某台壳 `wakeable: false` 时，
  `molt migrate` 不再发魔术封包，并明确说「账本说 X 不可唤醒 —— 不发」；没声明过
  （`None`）仍然照发，保持老行为。加了 `molt declare --no-wakeable` —— 在这之前 CLI
  **根本表达不出 false**（只有 `store_true`），也就是说除了测试没人能配置这件事。
- 钉住"没说"和"说了不能"的区别：`shells_view()` 里没声明的字段返回 `None`，不是 `False`
  （旧写法 `bool(attrs.get("wakeable", False))` 会把"我不知道"伪装成"我确认它不能"，
  症状是封包永远不发、看起来像"叫不醒"）。
- 演练脚本 `tools/rehearse-pypi.py` 支持 `--wheel`（发布前用本地 wheel 走一遍），
  起账本的等待从 10 秒放宽到 30 秒（**会随机红的演练很快会被所有人忽略**）。

---

## [1.2.0] - 2026-11-15

### 新增：账本知道「那台机器是什么」

- `molt declare --shell <壳名> --role nas|desktop|cloud|unknown [--gpu ...] [--wakeable]
  [--cost-per-hour ...] [--label ...]`；`molt shells` 看回来。
- 账本 `POST /shell` / `GET /shells`；`/where` 页面多一列「是什么」。
- **声明不是探测**：没登记就显示"没登记"，不猜。`role` 只影响人怎么看，代码里没有一条
  `if` 因它改变行为（见 `docs/AUDIT.md` 第 1 条）。

---

## [1.1.1] - 2026-11-15

### 修复

- `molt --no-color agent …` 不认自己的命令：入口分发只看了 `argv[0]`。现在会先剥掉前导
  全局开关（`--no-color` / `--color`）再判断是不是守护进程入口。

---

## [1.1.0] - 2026-11-15

### 新增

- **守护进程进 wheel**：`tools/molt-agent.py` / `tools/molt-homer.py` 搬到 `molt/daemon/`，
  于是 `pip install molt-handoff` 之后可以直接 `molt agent --config …`、`molt homer --config …`。
  在这之前它们住在仓库 `tools/` 下、**不进 wheel**，等于"只能从 git 仓库里跑起来的守护进程"。
- `molt timeline --demo`：没迁徙记录的新用户也能先看见那一页长什么样（示例数据带横幅标记）。

### 修复

- `molt/initcfg.py` 在 `molt_pkg` 下没有 `tools/` 时也能校验（走 `import molt.daemon.<kind>`）。
- 测试加载器指向真模块（`molt/daemon/agent.py`），不再对着 `tools/` 垫片打补丁 ——
  之前 `monkeypatch` 打在影子上，`SetSuspendState` 被真调用，**测试把开发机挂起了四次**、
  最后以非正常关机收场（`docs/ARCH.md` §14.9）。
- 测试套件加一道**电源总闸**：`shutdown` / `powercfg` / `rundll32` / `SetSuspendState`
  在测试里一律拒绝，要测就走桩。

---

## [1.0.1] - 2026-11-15

### 变更

- PyPI 发行名从 `molt` 改成 **`molt-handoff`**（`molt` 早被一个 Mustache 模板项目占用）。
  导入名与命令不变，仍然 `import molt` / `molt <子命令>`。

---

## [1.0.0] - 2026-11-14

首个可发布版本：账本（Warden）+ 租约/epoch fencing + `migrate`/`land` 双向迁徙 +
中途备份 + `/where` 心跳页 + `molt timeline` 迁徙时间轴 + Docker 镜像 + CI。
