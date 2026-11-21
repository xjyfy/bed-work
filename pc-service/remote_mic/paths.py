"""统一路径解析：开发期走仓库目录，打包后走 `%LOCALAPPDATA%\\RemoteMic`。

打包要点（见 `打包与安装包需求_2026-11-20.md` §六）：
程序目录 `%ProgramFiles%\\RemoteMic` 只读（只有安装/升级需要写权限），
运行期的 config.json / logs / icons 缓存统一放 `%LOCALAPPDATA%\\RemoteMic`，
避免 UAC 虚拟化和多用户互相覆盖；开发期仍读 `pc-service/config.json`，
首次迁移时自动复制到新位置，旧文件原样保留。
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

APP_NAME = "RemoteMic"


def is_frozen() -> bool:
    """是否由 PyInstaller 打包运行。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """程序所在目录：打包后是 exe 所在目录，开发期是 `pc-service/`。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _bundle_dir() -> Path:
    """PyInstaller onedir 的 `_internal` 目录（随包数据落在这里）。"""
    meipass = getattr(sys, "_MEIPASS", "")
    return Path(meipass) if meipass else app_dir()


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = Path(base) / APP_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # noqa: BLE001 - 极端环境下退回程序目录
        logger.warning("create data dir failed: %s", exc)
        return app_dir()
    return path


def config_path() -> Path:
    """运行期配置路径；首次调用时把开发期的 `pc-service/config.json` 迁移过来。"""
    target = data_dir() / "config.json"
    if not target.exists():
        legacy = app_dir() / "config.json"
        if legacy.exists():
            try:
                shutil.copyfile(legacy, target)
                logger.info("config migrated: %s -> %s", legacy, target)
            except OSError as exc:  # noqa: BLE001
                logger.warning("config migration failed: %s", exc)
                return legacy
    return target


def log_dir() -> Path:
    path = data_dir() / "logs"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:  # noqa: BLE001
        return app_dir() / "logs"
    return path


def vendor_dir(name: str) -> Path:
    """随包携带的第三方资源目录（如 vb-cable）。找不到时返回候选路径（不存在）。"""
    candidates: list[Path] = []
    if is_frozen():
        candidates.append(_bundle_dir() / name)
    candidates.append(app_dir() / "vendor" / name)
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def asset_path(name: str) -> Path:
    """本 package 自带资源（应用图标等）。打包后落在 `_internal/remote_mic/assets/`。"""
    if is_frozen():
        return _bundle_dir() / "remote_mic" / "assets" / name
    return Path(__file__).resolve().parent / "assets" / name
