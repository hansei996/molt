# 断言审计：文档说「它会」，代码里到底是哪一行？

> 起因：2026-11-15 连着三次撞到同一种事 —— **`docs/DECISIONS.md` 说某个字段"参与判断"，而
> `grep` 出来它只被打印过，没有任何一行 `if` 因它改变行为**。第一次是 `wakeable`（已修），
> 修完之后值得把同类断言系统性扫一遍：凡是写着"参与判断 / 生效 / 阻止 / 不会"的地方，
> 都要能指出真正改变行为的那一行。指不出来，就不是成立，只是愿望。

## 判据

一条断言"成立"要同时满足：

1. 代码里有一行**真的会改变行为**（`if` / `raise` / 返回值不同），不是只被 `print` 出来；
2. 那一行有测试钉着（否则它明天可能被静默改掉）；
3. 文档里的措辞与那一行的**范围**一致（Windows 上成立 ≠ 所有平台成立）。

三条里缺第三条的，记作 🟡（措辞不准确，已改文档）；缺第一、二条的，记作 ❌（不成立，要么改代码，
要么改文档，并说明为什么）。

## 结论表

| # | 文档里的断言 | 代码里的一行 | 结论 |
|---|---|---|---|
| 1 | `role` **参与判断** | —— **找不到**。全部出现处都是显示（`molt/server.py:129-133` 的 `/where`「是什么」列、`plugin/command.py:216-222` 的 `/molt where`、`molt/cli.py:1893` 的 `molt shells`） | 🟡 改成「**显示给人判断**」，并保留 DECISIONS 里那段"它参与的是人的判断"的解释 |
| 2 | `wakeable` **真的拦住魔术封包** | `molt/cli.py:798` `if "wakeable" in declared:` → `:808` `warn(不可唤醒)…; wake_mac = None` | ✅ `molt/tests/test_migrate_wakeable.py` 7 条 |
| 3 | `gpu` / `cost_per_hour` **不参与任何判断** | 只出现在存储、校验、显示（`molt/warden.py:764/772` 是校验，`molt/cli.py:1895/1903` 是打印） | ✅ 与文档一致 |
| 4 | `label` 只显示 | 同上；心跳里那份 `label` 是自报，进 `/where` 与时间轴 | ✅ |
| 5 | `schedule` 判据**已经写好了** | `plugin/command.py:192 def schedule_due(...)` —— **全仓库没有调用方**，只有测试在调 | 🟡 成立但要说全：这是纯函数、没人敲（D1a 未拍板），不是"到点会自动跑" |
| 6 | `rotate_jwt=True` 是**默认行为**（"攻破 Windows 等于攻破 NAS"） | `molt/overlay.py:226` 默认确实是 `True`；**但 `molt/cli.py:1723` 的 `cmd_land` 非 Windows 分支显式传了 `rotate_jwt=False`** | ❌ 改文档：**Windows 落位会换密钥，回 NAS 落位不会**。要不要两侧都换进了 `docs/DECISIONS.md` 的 D1.5 |
| 7 | 落位会做 **7 处自动改写** | Windows 分支：关平台 / 删 provider_sources / 删引用它们的 provider / 改 `agent_runner` / `dashboard.host` / `dashboard.port` / 换 `jwt_secret` = 7；非 Windows 分支只有 3 条 | 🟡 注明"7 条是 Windows 路径" |
| 8 | `execute: false` 就什么都不做 | `molt/daemon/agent.py:141/598/697/1035/1050/1078/1122` 七处守卫；homer 同形 | ✅ |
| 9 | 没有关机许可票就**不关** | `molt/daemon/agent.py:1022` 传 `requires_permission=` → `power_verdict()` 返回 `ask` → `run_once` 只记一行就 return | ✅ `test_power_ticket.py` / `test_molt_agent.py` |
| 10 | `backup_interval` 到点才推 | `molt/daemon/agent.py:597` `if interval <= 0 or not self.settings.execute: return`；`:601` 再判"这一代是不是我的" | ✅ |
| 11 | 插件 `enabled: false` 什么都不做 | `plugin/command.py:682/768/907` 三处；`/molt where`、`/molt status` 刻意不受它限制（`:552-609`） | ✅ |
| 12 | 心跳超 10 分钟算"哑了" | `molt/warden.py:736` `"stale": age > HEARTBEAT_STALE_SECONDS` | ✅ |
| 13 | 租约默认 90 秒 | `molt/epoch.py:53 DEFAULT_LEASE_TTL_SECONDS = 90`，`warden.acquire()` 用它 | ✅ |
| 14 | 写了不认识的触发源会**报错**而不是静默忽略 | `plugin/command.py:881-891` `move_policy_refusals()` | ✅ |
| 15 | 配置里 `shutdown_after_leave: true` 意味着"走了就关" | `molt/daemon/agent.py:1004` 还要过 `power_verdict()`；没票 → `ask` → 不关 | 🟡 文档补一句：**这个字段只管"要不要考虑关机"，关不关另有一张票** |
| 16 | `consumed` 不参与 fencing | `molt/epoch.py:164 accept_write()` 只比 `claimed_epoch` 与 `current_epoch` | ✅ |

## 第二批：v1.3.0 的插件配置同步与面板（2026-11-15 晚）

v1.3.0 加了一整块新功能（同步规则 + 账本端点 + dashboard 页面），于是又多出一批"它会……"。
同样的方法：**先 grep 到那一行，再看它有没有测试**。

| # | 文档 / 页面里的断言 | 代码里的一行 | 结论 |
|---|---|---|---|
| 17 | **只同步两边都装了的插件** | `molt/pluginsync.py:192` `for plugin in sorted(remote & local)`；交集外只进 `only_remote` / `only_local` 两个列表 | ✅ `test_pluginsync.py::test_only_the_plugins_both_sides_have_are_planned` / `::test_a_plan_never_mentions_a_plugin_outside_the_intersection` |
| 18 | **默认空跑，`--apply` 才写** | `molt/pluginsync.py:268` `if dry_run: report.would_update.append(...); continue`；CLI 只有给了 `--apply` 才传 `dry_run=False` | ✅ `::test_a_dry_run_writes_nothing` |
| 19 | **结构差太多默认跳过** | `molt/pluginsync.py:201` `_looks_like_a_different_schema(...)` → 进 `suspect`，`apply_plan` 只在 `force=True` 时并入待写 | ✅ `::test_a_completely_different_schema_is_suspect_not_silently_written` / `::test_force_writes_the_suspect_ones` |
| 20 | **旧配置留备份，不删** | `molt/pluginsync.py:275-283`：`os.replace(dst, bak)`，备份失败抛 `NeedsHuman`（"同步不该以删掉一份还能用的配置为前提"） | ✅ `::test_the_old_config_is_kept_before_being_replaced` |
| 21 | **包里只有配置与清单，不装代码、不装 `plugin_data`** | `molt/pluginsync.py::make_bundle` 只写 `config/<插件>_config.json` + `manifest.json`；`parse_bundle` 的白名单正则也只认这个形状 | ✅ `::test_the_bundle_contains_configs_and_nothing_else` |
| 22 | **每个壳只保留最新一份**（配置不是历史） | `molt/warden.py::store_plugin_bundle` 用固定文件名 `plugin-configs/<slug>.tar.gz` + `os.replace` | ✅ **本次审计补的**：`::test_publishing_twice_keeps_only_the_newest_bundle`（此前只靠"文件名恰好没加时间戳"，谁改成备份式命名就会静默变成谎话） |
| 23 | **写之前必须 `confirm: true`** | `plugin/sync_api.py::apply` 没拿到真值 → `error_response(..., 400)`；页面按钮另外由 `syncApplyButton()` 把关 | ✅ `test_plugin_sync_api.py::test_apply_without_confirm_is_refused_and_writes_nothing`（400 + 文件未变） |
| 24 | **写完只要重启 AstrBot 才生效** | 页面脚注原话。真相是 `astrbot/core/star/star_manager.py:1167` 构造 `AstrBotConfig(config_path=...)` —— **插件载入时**读一次，所以**重载插件就够**（`POST /api/plugin/reload`） | 🟡 措辞过严：改成"要重新载入那些插件"。已补 `tools/reload-plugin.py`（自签 JWT → 重载，实跑 `HTTP 200 重载成功`），页面脚注同步改 |
| 25 | **`GET /plugin-configs` 列清单** | `molt/server.py::_handle` 只拦 `POST /plugin-configs`，GET 落到 `dispatch()` | ✅ 曾经两边各自"对"、合起来永远 405 —— 已有**真起服务**的回归测试 `test_pluginsync.py::test_get_slash_plugin_configs_lists_instead_of_405`（"只在 dispatch 层测是抓不到这种错的"） |
| 26 | **页面四块功能**（发布 / 预览 / 应用 / 状态） | `plugin/views/sync/app.js` 里四个 endpoint | ✅ `test_plugin_bundle.py::test_the_sync_page_is_actually_wired`；另有人工清单 `docs/V1.3-REVIEW.md` |

### 第二批的新形态：**测试全绿，页面在骗人**

第 24 条之外，v1.3.0 还撞到一个第一次出现的形态：

> `do_publish` 返回的 `plugins` 是数字，页面写 `(res.plugins || []).length` —— 横幅打出
> 「已发布，账本收下了（**undefined** 个插件的配置 · 33 KB）」。

没有异常、没有日志、pytest 全绿。**后端对、前端对、接口形状错**。所以：

- 前端拿到的字段形状，也要有一条测试（现在 `test_plugin_bundle.py::test_the_publish_summary_survives_a_number_or_a_list` 钉着）；
- 但字段形状只是下限。**"人能看懂"这件事只能由人去看一遍** —— 那一遍写成可重跑的清单
  （`docs/V1.3-REVIEW.md`），每一格记"当时页面上真出现的字"，不记"感觉挺好"。

---

## 这类谎言的共同形态

三次（`role` 的 `unknown`、`wakeable` 的 `False`、`rotate_jwt` 的"默认"）都是**同一个句式**：

> 文档描述的是**设计意图**，代码实现的是**当前状态**，而两者之间没有一条测试连接。

它们的共同后果也一样：**症状不像"功能没做"，而像"功能做了但没生效"**。所以下一个人会去查网络、
查权限、查配置，唯独不会怀疑"这一行代码根本不存在"。

## 怎么保住这张表

- 有测试的那几条（2、8、9、10、11、12、13、14）已经把断言钉住了 —— 改坏了会红。
- 第 1、5、6、7、15 条是**这次改的文档**：它们不靠测试保，靠的是"每一句都要能指到一行"这个习惯。
  所以这份文件的读法是：**它不是一次性的报告，是下次写"它会……"之前该看的那张对照表。**
- 审计脚本留在 `dist/_audit_switch.py`（不进仓库、不进 wheel）：`python dist/_audit_switch.py <token>`
  会把「文档里的提及」和「代码里像判断的行」并排打出来，`*` 标记是可能改变行为的那些行。
