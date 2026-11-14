# 故障注入矩阵

> v0.4「硬化」的验收证据。16 个场景，全部符合声称的行为。
> 生成工具：`tools/fault-matrix.py`（`python tools/fault-matrix.py`）。

## 为什么要有这一份

这套系统里最贵的错误，全都长得像成功：

- 归档少了一半 —— 只要没人算指纹，`tarfile` 照样能解出前面几个成员；
- 落位写了一半 —— 目标 `data/` 里确实有 `data_v4.db`，`ls` 看着也对；
- 账本写坏了 —— 进程不会立刻知道，要等下一次 `load()`。

所以这里的判据不是「它报错了没有」，而是：

> **中途崩掉之后，任何人（包括下一个进程）都不许把中间态误认成终态。**

每条场景都写成「声称的行为 → 实测 → 结论」三列。`✅` 表示实测与声称一致；出现 `❌` 说明的是产品的 bug，不是文档写错了。

## 怎么跑

```bash
# 全跑，人读格式
python tools/fault-matrix.py

# 只跑几条
python tools/fault-matrix.py --only 4,5,6

# 出 markdown 表（写进文档用）
python tools/fault-matrix.py --md dist/faults.md

# 出 JSON（给 CI 用）
python tools/fault-matrix.py --json dist/faults.json
```

退出码：16 条全过 `0`，否则 `1`。

**为什么直接调函数、不走 HTTP**：故障要注入到 `shutil.copy2` / `os.replace` / `time.time` 这一层，HTTP 表面看不到这些。代价是 harness 必须自己维护一个「真实形状」的假 WAL 目录和假快照 —— 这一点在下面第 1 条里咬了人。

## 矩阵

| # | 场景 | 声称的行为 | 实测 | 结论 |
|---|---|---|---|---|
| 1 | 打包中途失败 | 无 `gen-*.tar.gz`、无 `.part` | 无归档、无 `.part` | ✅ |
| 2 | 打包改名失败 | 无归档、无 `.part` | 无归档、无 `.part` | ✅ |
| 3 | 归档被改一字节 | 解包前拒绝，不落文件 | 解包前拒绝，且没落任何文件 | ✅ |
| 4 | 上传被截断 | 400 `checksum_mismatch`，归档区干净 | 400 `checksum_mismatch`，归档区干净 | ✅ |
| 5 | 落位中途失败 | 无半份库，标记在 | 没有截断的文件，标记在（已完整落位的文件保留是设计如此） | ✅ |
| 6 | 快照带非空 WAL | 拒绝，目标不动 | 拒绝（未合并事务），目标里没有任何快照内容 | ✅ |
| 7 | 账本被截断 | `Fatal` 且不改写 | `Fatal` + 不动文件 | ✅ |
| 8 | 两壳抢租约 | 一个 ok、一个 `LeaseHeld` | 一个 ok、一个 `LeaseHeld` | ✅ |
| 9 | 旧 epoch 写入 | `EpochStale` | `EpochStale`（当前 epoch 1） | ✅ |
| 10 | 心跳停了 700 秒 | `stale=True`，不冒充在线 | `stale=True`（11 分钟前） | ✅ |
| 11 | 时钟倒退一小时 | 仍被拦，epoch 不倒退 | 仍被拦（epoch 1 → 1 未倒退） | ✅ |
| 12 | 账本写入盘满 | `Fatal`，旧账本完好 | `Fatal` + 旧账本完好可读 | ✅ |
| 13 | manifest 缺 `generation` | `Fatal`，点名字段 | `Fatal`，点名 `generation` | ✅ |
| 14 | manifest 类型错 | `Fatal`，点名字段 | `Fatal`，点名 `created_at` | ✅ |
| 15 | 未完成标记存在 | 能识别成未完成 | 标记可读，代次 9 | ✅ |
| 16 | 未完成标记损坏 | 当成未完成（`broken`） | `broken=True` | ✅ |

```
16 个场景，16 个符合声称的行为
```

## 这一轮改掉的三个真问题

### 1. 账本写盘满会冒裸 `OSError`

`molt/warden.py` 的 `_atomic_write_json` 原来只清临时文件再 `raise`。盘满时抛的是 `OSError(28)`，冒到命令行是一段 traceback —— 人会以为 molt 有 bug，而事实是「这次写入没生效、目标文件还是上一版完整内容」。

现在：

```python
    except Exception as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        if isinstance(exc, MoltError):
            raise
        raise Fatal(
            f"写 {path.name} 失败：{exc}\n"
            "  目标文件还是上一次的完整内容 —— 这次写入没有落盘。\n"
            "  账本写不进去的时候，宁可这次操作失败，也不能让它看起来成功。"
        ) from exc
```

### 2. 打包不是原子的（T1）

`make_archive` 原来直接往最终文件名写。中途被杀就留下一个**截断的 `gen-000012.tar.gz`**，而 `list_archives()` 用 `glob("gen-*.tar.gz")` 枚举 —— 它会出现在列表里；`tarfile.open()` 对截断的 gzip 往往还能读出前面几个成员。

现在写 `.part`，校验通过再 `os.replace`，`finally` 里清掉残留。`.part` 必须与目标**同目录**：`os.replace` 只在同一文件系统内原子，跨文件系统会退化成「拷贝 + 删除」。

### 3. 落位不是原子的（T2）

`restore_into` 原来逐文件 `shutil.copy2` 到最终路径，中途失败就留下一半新一半旧。现在每个文件先写 `.molt-part` 再 `os.replace`，全过程有 `.molt-restore-incomplete` 标记；两个守护进程启动前都读这个标记，读到就不启动。

标记只在**被读到**的时候才算数 —— 所以 agent 的 `do_boot()` 与 homer 的 `do_start()` 里各有一道守卫，且有一条测试把三方（`molt/transfer.py` / `tools/molt-agent.py` / `tools/molt-homer.py`）的常量名钉在一起：守护进程不能 `import molt.transfer`（它们是被 `python tools/xxx.py` 直接跑起来的脚本），所以字符串只能复制 —— **复制的东西必须有测试盯着**。

## harness 自己错过的四次（更值得记）

第一次跑出来是 `13/16`，三条 FAIL 里**没有一条是产品的 bug**，全是 harness 写错了。它们各自是同一类错误：**测的东西不是我以为的那件事**。

1. **路由写错**：归档上传是 `POST /archive`，实际是 **`PUT /archive/<generation>`**（`server.py:62 ARCHIVE_ROUTE`）。错路由落到 JSON 分支，拿到 `400 bad_request`，我还以为"截断被拒绝了，真棒"。
2. **`WardenServer` 没有 `serve_forever()`**：那是内部 `ThreadingHTTPServer` 的方法。正确用法是 `server.start()`（内部起线程并回填真实端口）+ `finally: server.stop()`。
3. **目标目录没建**：`restore_into` 第一件事就要求目标存在，否则 `NeedsHuman("目标 data 目录不存在")`。于是「落位中途失败」那条测到的是「目录不存在」——**差一点把 harness 的 bug 写成产品结论**。
4. **`_raises` 太宽**：`_raises((OSError, MoltError, Fatal), ...)` 会把任何 `MoltError` 当成功。于是每条场景都必须**额外核对错误消息的内容**（WAL 那条断言含「未合并」，盘满那条断言含「没有落盘」），否则测的是「随便抛了个错」。

## 判据本身也要精确（两条改判的理由）

第一次 `13/16` 之后，剩下的两条 `FAIL` 查下去发现是**我的期望过强**，不是产品有问题。改判据而不是改产品，理由都要能写出来：

- **#5 落位中途失败**：目标里留下了 `cmd_config.json`。查 `restore_into` —— `finally` 只清 `parts`，已**完整**落位的文件保留，这正是设计：配合 `.molt-restore-incomplete` 拦住启动，重跑 `land` 能收尾（`test_restore_twice_after_an_interruption_finishes_the_job` 钉住）。
  → 判据从「目标里一个文件都没有」改成「**没有截断的文件**（逐字节与快照比对）+ 无 `.molt-part` 残留 + 标记在」。
- **#6 快照带非空 WAL**：报错对，但目标里出现了东西 —— 那是 `.molt-restore-incomplete` 自己（标记在 `_iter_payload_files` 抛 `Fatal` 之前就写了）。
  → 判据里排除 `.molt-*` 这类 molt 自己的记账文件。

**教训**：「没有半份文件」这句话必须落成「每个已存在的文件都与快照逐字节相同」，否则它既能被实现成"什么都不留"，也能被实现成"留一份完整但没写完的" —— 两种都过，都不是本意。

## 不在这份矩阵里的（以及为什么）

| 没测 | 理由 |
|---|---|
| 网络中途断掉（WiFi 掉、NAS 重启） | 这一层由 `.part` + 指纹复核覆盖：不管断在哪，落不下来就是落不下来。真机测它要拔网线，收益不如代码路径清楚 |
| 磁盘坏道 | 那是文件系统的人质。这里只保证「写不进去时不当成功」 |
| 断电瞬间 | 与 #5/#16 同一条路径（标记损坏 → 当成未完成）。真机验证是 11-15 02:01 那次 S5 关机往返 |
| 两个 Warden 同时写同一 `root/` | 部署上不允许（Warden 只有一份）。多副本需要真正的共识层，超出本项目范围 |
