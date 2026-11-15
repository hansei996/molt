"""插件发布物的完整性：该装的文件一个都不能少。

2026-11-15 的真实教训：`dist/_rebuild_bundle.py` 里是手写清单，
`sync_api.py` / `arrival.py` / `observed.py` 加进去之后清单没改，
于是"插件装好了、页面打不开"——**而且不报错**，AstrBot 只是 import 不到。

这一组测试把三条规则钉住：

1. `plugin/` 下的每个 `.py` 都要进发布物（没人再靠记性列举）；
2. `views/**` 都要进发布物（页面是文件，不是代码）；
3. 插件 import 到的每个 `molt` 子模块，`_vendor/molt/` 里都要有 —— 少一个
   就是一个只在真机上出现的 `ImportError`。
"""

from __future__ import annotations

from pathlib import Path

from molt import bundling

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugin"


def test_every_plugin_module_is_published():
    files = bundling.plugin_files(PLUGIN)
    on_disk = {p.name for p in PLUGIN.glob("*.py")}

    missing = sorted(on_disk - set(files))
    assert not missing, f"这些插件模块没进发布物：{missing}"


def test_metadata_is_published():
    files = bundling.plugin_files(PLUGIN)
    assert "metadata.yaml" in files


def test_views_are_published():
    files = bundling.plugin_files(PLUGIN)
    views = [name for name in files if name.startswith("views" + "\\") or name.startswith("views/")]

    assert "views/sync/index.html" in {v.replace("\\", "/") for v in views}
    app_js = {v.replace("\\", "/") for v in views}
    assert "views/sync/app.js" in app_js
    # 页面自己引的样式表也得在，否则页面能打开但没样式（看起来像"坏了"）
    assert "views/sync/style.css" in app_js


def test_every_molt_import_has_a_vendored_module():
    imported = bundling.molt_imports(PLUGIN)
    # `vendored_modules` 的键是文件名（client.py），而 import 名是 client ——
    # 这里统一去掉后缀再比，免得两边各有一套"名字"的概念。
    vendored = {Path(name).stem for name in bundling.vendored_modules(ROOT / "molt")}

    missing = sorted(set(imported) - vendored)
    assert not missing, (
        f"插件 import 了这些 molt 子模块，但 vendored 里没有：{missing}\n"
        f"（插件跑在容器 / G:\\molt-fish 里，够不到仓库源码，必须自带一份）"
    )


def test_the_sync_page_is_actually_wired():
    """光有页面文件不够：`main.py` 必须在启动时注册后端，否则页面拿 404。"""
    main_py = (PLUGIN / "main.py").read_text("utf-8")
    assert "MoltSyncApi" in main_py
    assert "register_routes" in main_py

    app_js = (PLUGIN / "views/sync/app.js").read_text("utf-8")
    for endpoint in ("sync/state", "sync/preview", "sync/publish", "sync/apply"):
        assert endpoint in app_js, f"页面没调 {endpoint}"


def test_the_page_cannot_apply_without_a_confirm_checkbox():
    """页面上"真的写"必须由一个勾选框把关，而后端还要求 confirm=true。

    只在前端拦是不够的（请求可以手工发），只在后端拦也是不够的
    （人看不到"我要覆盖什么"）。两处都要有，这条测试盯前端那半。
    """
    html = (PLUGIN / "views/sync/index.html").read_text("utf-8")
    assert 'id="confirm"' in html

    app_js = (PLUGIN / "views/sync/app.js").read_text("utf-8")
    assert 'confirm: true' in app_js
    assert 'id="apply"' not in app_js  # 按钮状态统一由 syncApplyButton 管
