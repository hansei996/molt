# 参与 molt

先说一件事：**这个项目的核心不是代码量，是那几条不能破的约束。**
改之前请至少读完 `docs/ARCH.md` 的 §2.2 和 §5。

## 绝对不要做的事

1. **让两只鱼同时在线。** 这是公理 0，不是性能取舍。
   任何"反正只是并发读"的改动，先假设它会变成两条鱼同时说话。
2. **改 AstrBot 核心。** 只允许走它公开的接缝：
   `initialize()` / `terminate()`、`context.register_web_api()`、
   `context.platform_manager`、`core_lifecycle.restart()`。
   别的都算私有 API，上游一个 patch 版本就会让你崩。
3. **在落位时打开 `platform[].enable`。**
   `appid` 可以跟着灵魂走，"要不要开始说话"不能。
4. **给 `molt` 加一条会关机的命令。** 交接到账本接受快照就结束了，
   关机是另一件事，而且它可以被拒绝。

## 改代码的规矩

- **测试必须能被打穿。** 这个项目用变异测试验收：改坏一行，看有没有测试变红。
  `tools/_mutate_*.py` 是现成的样板。写不出会失败的测试，等于没写测试。
- **判据是"有没有测试失败"，不是"失败够不够多"。** 不要设 `min_fail` 阈值 ——
  那只是让脚本变绿。
- **报错要能区分"我没做成"和"我没法判断"。**
  容器里没有 `ping` 时不能说"叫不醒"（bug #14）。
- **凡是 `sqlite3` 可能抛的地方都要包。** 否则租约永远收不回来（bug #4）。
- 运行时**零依赖**。`server.py` / `wake.py` / `molt-homer.py` 都要能在
  什么都没装的 NAS 宿主（python 3.10.7）上跑。

## 提交前

```sh
python -m pytest -q      # 应该全绿，目前 568 个
```

CI 会在 Linux 和 Windows 上各跑一遍 py3.10 / py3.13 ——
**Windows 上跑通不是可选项**，这个项目一半的 bug 只在那上面出现
（GBK 控制台、`AttachConsole`、WSL 没有的 `-shm` 语义）。

## 报 issue 时

请带上：

- `molt doctor` 的输出
- `molt status` 的输出（**记得把 token 去掉**）
- 你那个壳的 `platform[].enable` 当前是什么值
- 如果涉及迁徙：两个壳的 `shell` 名、账本里的 `epoch` 和 `generation`

## 文档

- `docs/ARCH.md` —— 为什么这么做，以及所有踩过的坑（15 个 bug 表在 §13.6）
- `docs/SELFUSE.md` —— 怎么用，一页纸
- `docs/PLAN.md` —— 开源规划、竞品调研
- `docs/architecture.html` —— 给人看的总览图（单文件，直接打开）
