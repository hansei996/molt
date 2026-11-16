"""仓库里的入口垫片 —— 真代码在 `molt/daemon/nightwatch.py`。

和 `tools/molt-homer.py` 同一个理由：`tools/` 不进 wheel，守护进程必须能被
`import`，否则 `pip install` 的人起不来。

    python tools/molt-nightwatch.py --config ./molt-nightwatch.json --once
    molt nightwatch --config ./molt-nightwatch.json          # 推荐
"""

from __future__ import annotations

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from molt.daemon import nightwatch as _impl  # noqa: E402  (必须在改 sys.path 之后)

globals().update({name: value for name, value in vars(_impl).items() if not name.startswith("__")})

if __name__ == "__main__":
    raise SystemExit(_impl.main())
