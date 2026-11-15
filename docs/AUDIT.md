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
