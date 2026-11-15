"""`molt init`：把"第一次装"这件事从 README 里搬进代码。

为什么值得单独一个模块
----------------------

这个项目最难的部分不是迁徙，是**第一次配置**：三台机器、三份 JSON、两个
token、一个壳名，任何一处写错，症状都是"它不动"，而且不会报错。

所以这里的原则是：

1. **生成的东西要能被真正的加载器读一遍再落盘** —— 写进临时文件、
   用守护进程自己的 `Settings.load()` 验一次、通过了才改名。生成器自己
   认为对不算数（这是 v0.4 那条"看起来像成功的中间态"的同一课）。
2. **默认不覆盖** —— 一份已经调通的配置比一份"更新过的"配置值钱。
   要覆盖必须显式 `--force`。
3. **不猜身份** —— `shell` 必须由人给（或显式 `--from-hostname`），
   因为账本里的 `target_shell` 是**别人写下的字符串**，猜错就会让
   这台机器永远等不到自己的那一代。
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import secrets
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .errors import Fatal, NeedsHuman


# --------------------------------------------------------------------------
# 模板
# --------------------------------------------------------------------------


def hostname_shell_id() -> str:
    """把主机名变成账本用的壳名。

    必须小写：账本里的壳名一直是 `desktop-h2kgiao` 这种形状，而 Windows 的
    `gethostname()` 全大写 —— 混着来，同一个壳在 `molt status` 里会显示成两个。
    """
    name = (socket.gethostname() or "").strip().lower()
    return name or "unknown"


def agent_template(
    *,
    shell: str,
    data_dir: str,
    molt_pkg: str,
    python: str,
    warden_url: str,
    warden_token: str,
    astrbot_python: str = "",
    boot_script: str = "",
    log_path: str = "",
    platform_switch: str = "",
    port: int = 6199,
) -> Dict[str, Any]:
    """Windows / 裸机壳的配置。

    `execute` 刻意是 `False`：第一次装上时它只观察、不动手。人要先把
    `molt status` 里的壳名对一遍，再改成 `true` —— 打开它就意味着这台机器
    会自己关机、自己落位、自己起进程。
    """
    return {
        "execute": False,
        "shell": shell,
        "data_dir": data_dir,
        "molt_pkg": molt_pkg,
        "python": python,
        "astrbot_python": astrbot_python,
        "boot_script": boot_script,
        "log_path": log_path,
        "platform_switch": platform_switch,
        "warden_url": warden_url,
        "warden_token": warden_token,
        "port": int(port),
        "interval": 10.0,
        "backup_interval": 600.0,
        "shutdown_after_leave": True,
        "power_action": "shutdown",
        "dashboard_host": "127.0.0.1",
        "dashboard_port": int(port),
        "boot_extra": [],
    }


def homer_template(
    *,
    shell: str,
    data_dir: str,
    molt_pkg: str,
    warden_url: str,
    warden_token: str,
    python: str = "python3",
    container: str = "astrbot",
    platform_switch: str = "",
    dashboard_host: str = "0.0.0.0",
    dashboard_port: int = 6185,
) -> Dict[str, Any]:
    """NAS / 容器壳的配置。

    `dashboard_host` 默认 `0.0.0.0`：**容器里监听回环等于没有监听** ——
    映射出去的端口后面什么都没有，人只会看到"鱼在 QQ 上说话，但管理页面打不开"。
    """
    return {
        "execute": False,
        "shell": shell,
        "container": container,
        "data_dir": data_dir,
        "molt_pkg": molt_pkg,
        "python": python,
        "platform_switch": platform_switch,
        "warden_url": warden_url,
        "warden_token": warden_token,
        "interval": 10.0,
        "backup_interval": 600.0,
        "dashboard_host": dashboard_host,
        "dashboard_port": int(dashboard_port),
    }


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------


def write_json_atomic(path: Path, payload: Dict[str, Any], *, force: bool = False) -> None:
    if path.exists() and not force:
        raise NeedsHuman(
            f"{path} 已经存在。\n"
            "  一份调通的配置比一份「更新过的」配置值钱，所以不覆盖。\n"
            "  确实要重写就加 --force（旧文件会先备份成 *.bak-<时间>）。"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(body, encoding="utf-8")
        if path.exists():
            backup = path.with_name(path.name + ".bak")
            try:
                backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def load_builder(path: Path, module_name: str) -> Any:
    """按文件路径加载守护进程脚本，拿它自己的 `Settings`。

    这条路走的是**仓库模式**（`tools/` 就在旁边）。装成 wheel 之后没有
    `tools/`，参见 `builder_module()`。

    为什么要用"它自己的"校验器：生成器自己认为对不算数。
    """
    if not path.is_file():
        raise Fatal(f"找不到 {path} —— --molt-pkg 指向的目录里应该有 tools/")
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise Fatal(f"加载不了 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(module_name, module)
    spec.loader.exec_module(module)
    return module


#: 加载器会把「还没打开开关」也算成 problem —— 那是对的，因为对守护进程
#: 来说「不能动手」和「配置错了」都该死。但对 `molt init` 来说，
#: `execute: false` 是**刻意的默认**（第一次装上只观察不动手），不是缺陷。
#: 所以这里只过滤掉这一类"待办提醒"，其余一律当阻塞问题。
_ADVISORY_PREFIXES = ("execute 是 false",)


def builder_module(kind: str, molt_pkg: str) -> Any:
    """拿到守护进程模块本身，两条路依次试。

    1. **仓库模式**：`<molt_pkg>/tools/molt-agent.py`（或 `molt-homer.py`）。
    2. **安装模式**：直接 `import molt.daemon.agent`。

    第二条是 v0.7 补上的。在那之前只有第一条，而 `pip install` 装到的包里
    没有 `tools/` —— 于是陌生人照 `docs/INSTALL.md` 走到第 2 步就拿到一句
    「找不到 .../site-packages/tools/molt-agent.py」，连配置都写不出来。
    """
    script = "molt-agent.py" if kind == "agent" else "molt-homer.py"
    path = Path(molt_pkg) / "tools" / script
    if path.is_file():
        return load_builder(path, f"_molt_init_{kind}")
    module_name = f"molt.daemon.{kind}"
    try:
        return importlib.import_module(module_name)
    except ImportError as e:
        raise Fatal(
            f"找不到 {path}，也 import 不到 {module_name}（{e}）—— "
            f"这份配置验证不了，不写盘。用 pip 装的请确认 `molt --version` 能跑。"
        )


def validate_config(
    path: Path,
    *,
    kind: str,
    molt_pkg: str,
) -> List[str]:
    """用守护进程自己的加载器读一遍，返回它认为的**阻塞性**问题（空 = 通过）。"""
    try:
        module = builder_module(kind, molt_pkg)
    except Fatal as e:
        # 加载器本身找不到，也是"这份配置现在用不了"的一种 —— 归到问题列表里
        # 返回，调用方就能统一按"不写盘"处理，而不是两种失败两种文案。
        return [str(e)]
    try:
        settings = module.Settings.load(path)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - 任何读不了都是"配置不对"
        return [f"加载失败：{exc}"]
    problems: List[str] = [
        p for p in settings.problems() if not str(p).startswith(_ADVISORY_PREFIXES)
    ]
    return problems


def build(
    *,
    role: str,
    out: Path,
    shell: str,
    data_dir: str,
    molt_pkg: str,
    python: str,
    warden_url: str,
    warden_token: str,
    astrbot_python: str = "",
    boot_script: str = "",
    log_path: str = "",
    platform_switch: str = "",
    port: int = 6199,
    container: str = "astrbot",
    dashboard_host: str = "",
    dashboard_port: int = 0,
) -> Dict[str, Any]:
    if role == "agent":
        return agent_template(
            shell=shell,
            data_dir=data_dir,
            molt_pkg=molt_pkg,
            python=python,
            warden_url=warden_url,
            warden_token=warden_token,
            astrbot_python=astrbot_python,
            boot_script=boot_script,
            log_path=log_path,
            platform_switch=platform_switch,
            port=port,
        )
    if role == "homer":
        return homer_template(
            shell=shell,
            data_dir=data_dir,
            molt_pkg=molt_pkg,
            warden_url=warden_url,
            warden_token=warden_token,
            python=python,
            container=container,
            platform_switch=platform_switch,
            dashboard_host=dashboard_host or "0.0.0.0",
            dashboard_port=dashboard_port or 6185,
        )
    raise NeedsHuman(f"不认识的角色：{role}（只有 agent / homer）")


def new_token(nbytes: int = 24) -> str:
    """给新装的 Warden 生成一个 token。

    `token_urlsafe` 出来的串没有 `=`/`+`/`/`，直接塞进 JSON、URL、shell 变量
    都不会被转义咬到 —— 这一点在真机上值钱：本项目就因为 URL 里带特殊字符
    浪费过一整轮排查。
    """
    return secrets.token_urlsafe(nbytes)


def daemon_command(kind: str, *, config: Path | str) -> str:
    """跑守护进程的**可移植**写法。

    `molt agent` 是 v0.7 加的入口：装成 wheel 之后没有 `tools/`，而
    `python tools/molt-agent.py` 这种写法在别人的机器上根本不存在。
    命令行和文档都改用这一句。
    """
    return f"molt {kind} --config {config}"


def next_steps(role: str, *, out: Path, python: str) -> List[str]:
    if role == "agent":
        return [
            f"先看一眼（不执行任何动作）：{daemon_command('agent', config=out)} --once",
            f"确认壳名对得上之后，把 {out} 里的 \"execute\" 改成 true",
            "然后让它常驻：把这条命令放进启动项（或计划任务）",
            f"（在仓库里没装包也能跑：{python} tools/molt-agent.py --config {out} --once）",
        ]
    if role == "homer":
        return [
            f"先看一眼：{daemon_command('homer', config=out)} --once",
            f"确认容器名对得上之后，把 {out} 里的 \"execute\" 改成 true",
            "常驻：交给 tools/molt-supervise.py（只补不杀，可进 cron）",
            f"（在仓库里没装包也能跑：{python} tools/molt-homer.py --config {out} --once）",
        ]
    return []
