"""两个常驻守护进程：`agent`（新壳上等）与 `homer`（账本那台机器上接）。

## 为什么它们在这个包里，而不是 `tools/` 下

一开始它们就是仓库根下两个脚本（`tools/molt-agent.py`、`tools/molt-homer.py`），
用 `python tools/molt-agent.py` 跑。那样在"我自己这台机器"上没问题，但
`pip install molt-handoff` 装到的只有 `molt/` 这个包 —— **`tools/` 根本不在
wheel 里**，于是照着 `docs/INSTALL.md` 走的陌生人会在第 4 步撞到
`找不到 .../site-packages/tools/molt-agent.py`，而且那时他已经生成了一个
连自己都验不过的配置。

这不是"文档写错了"，是**代码的位置错了**：一个只有从 git 仓库里才能跑起来的
守护进程，等于没有发布。所以模块搬进来了，`tools/` 下留一层几行的 shim
（仓库里原来的调用方式、已经部署在真机上的 `.cmd` / cron 一行都不用改）。

## 怎么跑

    molt agent  --config ./agent.json       # 推荐：装到哪都能用
    molt homer  --config ./molt-homer.json
    python -m molt.daemon.agent --config ./agent.json   # 等价写法
    python tools/molt-agent.py --config ./agent.json    # 仓库里也行（shim）

`--once` 只跑一轮、打印判断就退出 —— 第一次装完先用它空看一眼。
"""
