"""仓库里的入口垫片 —— 真代码在 `molt/daemon/homer.py`。

理由与 `tools/molt-agent.py` 完全相同（见那个文件的说明）：`tools/` 不在
wheel 里，所以守护进程必须能被 `import`，否则 `pip install` 的人根本起不来。
NAS 上已经部署的 `python3 tools/molt-homer.py --config ...` 继续有效。

    python tools/molt-homer.py --config ./molt-homer.json   # 仓库里 / 老部署
    molt homer --config ./molt-homer.json                  # 推荐
"""

from __future__ import annotations

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from molt.daemon import homer as _impl  # noqa: E402  (必须在改 sys.path 之后)

globals().update({name: value for name, value in vars(_impl).items() if not name.startswith("__")})

if __name__ == "__main__":
    raise SystemExit(_impl.main())
