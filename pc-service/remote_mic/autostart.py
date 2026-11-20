"""开机自动启动（HKCU\\...\\Run）读写。

口径（见 `打包与安装包需求_2026-11-20.md` §七）：
- 默认不勾；托盘右键里勾选后才写入注册表；
- 写 HKCU 的 Run 键，不写系统级、不做 SYSTEM 会话 0 的计划任务
  （会话 0 里没法做键鼠注入和窗口置前，会废掉一半功能）；
- 打包后指向 `RemoteMicService.exe`，开发期指向 `pythonw.exe run.py`。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import paths

logger = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "RemoteMic"


def _command() -> str:
    if paths.is_frozen():
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = exe
    return f'"{pythonw}" "{paths.app_dir() / "run.py"}"'


def is_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(str(value).strip())
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("read autostart failed: %s", exc)
        return False


def enable() -> bool:
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _command())
        logger.info("autostart enabled: %s", _command())
        return True
    except OSError as exc:  # noqa: BLE001
        logger.error("enable autostart failed: %s", exc)
        return False


def disable() -> bool:
    try:
        import winreg

        # 默认 OpenKey 是只读句柄，删值必须显式要 KEY_SET_VALUE（否则 WinError 5 拒绝访问）
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        logger.info("autostart disabled")
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:  # noqa: BLE001
        logger.error("disable autostart failed: %s", exc)
        return False


def toggle() -> bool:
    if is_enabled():
        disable()
    else:
        enable()
    return is_enabled()
