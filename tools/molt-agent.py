"""仓库里的入口垫片 —— 真代码在 `molt/daemon/agent.py`。

## 为什么只剩这几行

守护进程原来就在这里。但它只有从 git 仓库里才跑得起来：`pip install
molt-handoff` 装到的只有 `molt/` 这个包，`tools/` 不在 wheel 里，于是照
`docs/INSTALL.md` 走的陌生人第 4 步就会撞到
`找不到 ...\\site-packages\\tools\\molt-agent.py`。

代码搬进包里之后，这里留一层垫片，好处是**已经部署的调用方式一行都不用改**：
真机上 `molt-agent.cmd` 里那行 `python ...\\tools\\molt-agent.py --config ...`、
NAS 上的 cron、两个仓库的文档全都继续有效。

## 两种写法都对

    python tools/molt-agent.py --config ./agent.json     # 仓库里（本文件）
    molt agent --config ./agent.json                     # 装到哪都能用

`globals().update(...)` 是给测试用的：`molt/tests/test_molt_agent.py` 按文件
路径加载这个脚本、然后直接拿 `mod.Agent` / `mod.Settings`。真正的模块在
`molt/daemon/agent.py`，这里把它的命名空间摊平出来，两边看到的是同一批对象。
"""

from __future__ import annotations

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    # `python tools/molt-agent.py` 时 sys.path[0] 是 tools/，仓库根不在里面。
    sys.path.insert(0, str(_ROOT))

from molt.daemon import agent as _impl  # noqa: E402  (必须在改 sys.path 之后)

globals().update({name: value for name, value in vars(_impl).items() if not name.startswith("__")})

if __name__ == "__main__":
    raise SystemExit(_impl.main())
