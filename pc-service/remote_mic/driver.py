"""VB-CABLE 虚拟声卡：检测是否已装、静默安装/卸载、提权重试。

命令行入口（打包后的 exe 同样可用）：
    RemoteMicService.exe --install-driver
    RemoteMicService.exe --uninstall-driver
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

from . import paths

logger = logging.getLogger(__name__)

# pnputil 输出里能出现的关键字（大小写不敏感，按小写匹配）。
# 2026-11-20 修正：实测本机 INF 原始名是 `vbmmecable64_win10.inf`（mme 与 cable 之间**没有**下划线），
# 旧列表写成 "vbmme_cable64" 永远匹配不上，导致已装驱动被判定为"未装"，
# 安装包在 ssPostInstall 又跑一遍安装器，最后弹出"VB-CABLE 虚拟声卡未装好"的误报。
# 现在同时匹配 INF 名与 Provider Name（"VB-Audio Software"），任一条命中即视为已装。
DRIVER_KEYWORDS = ("vbmmecable", "vbaudiocable", "vbcable", "vb-audio", "vb_audio")
INSTALLER_NAMES = ("VBCABLE_Setup_x64.exe", "VBCABLE_Setup.exe")
CREATE_NO_WINDOW = 0x08000000

# PowerShell 绝对路径优先：隐藏启动（计划任务/安装器子进程）时 PATH 可能只指向受限运行时
_PS_CANDIDATES = (
    "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
    "powershell.exe",
)

_cache: Optional[tuple[float, bool]] = None
_CACHE_TTL = 5.0


def _run(cmd: list[str], timeout: float = 12.0) -> str:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            errors="ignore",
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # noqa: BLE001 - 检测失败按"未装"处理即可
        logger.warning("driver probe failed (%s): %s", cmd[0], exc)
        return ""


def detect_installed(force: bool = False) -> bool:
    """驱动是否存在：pnputil（INF 名 / Provider 名）→ 设备管理器兜底。

    打包后也要走兜底：安装器的 OpenProcess 子进程里 pnputil 偶发拿不到完整输出，
    只靠 INF 名会把已装驱动判成未装（2026-11-20 真机踩坑）。
    """
    global _cache
    if not force and _cache is not None and time.time() - _cache[0] < _CACHE_TTL:
        return _cache[1]

    out = _run(["pnputil", "/enum-drivers"]).lower()
    found = any(k in out for k in DRIVER_KEYWORDS)

    if not found:
        # 设备管理器兜底（打包后同样可用，只是慢一点）。用绝对路径 powershell，
        # 避免隐藏启动时 PATH 指向受限运行时导致静默降级。
        for ps in _PS_CANDIDATES:
            count = _run([
                ps, "-NoProfile", "-NonInteractive", "-Command",
                "@(Get-PnpDevice -ErrorAction SilentlyContinue | "
                "Where-Object { $_.FriendlyName -match 'VB-Audio|CABLE' -and $_.Status -eq 'OK' }).Count",
            ], timeout=20).strip()
            if count.isdigit():
                found = int(count) > 0
                break

    _cache = (time.time(), found)
    return found


def installer_path() -> Optional[Path]:
    folder = paths.vendor_dir("vb-cable")
    for name in INSTALLER_NAMES:
        path = folder / name
        if path.exists():
            return path
    return None


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _run_installer(exe: Path, flag: str) -> int:
    """`-i`/`-u` + `-h`（隐藏界面）是 VB-Audio 官方支持的静默参数，实测退出码 0。"""
    args = f"{flag} -h"
    if is_admin():
        proc = subprocess.run([str(exe), flag, "-h"], timeout=180,
                              creationflags=CREATE_NO_WINDOW)
        return int(proc.returncode)
    # 未提权（开发期直跑 python）时弹一次 UAC，用户点"是"才继续
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(exe), args, None, 1)
    return 0 if rc > 32 else -1


def install(wait_seconds: float = 20.0) -> dict:
    """安装驱动；已装则直接返回。返回 {ok, already, reason}。"""
    if detect_installed(force=True):
        return {"ok": True, "already": True, "reason": "", "installed": True}
    exe = installer_path()
    if exe is None:
        return {
            "ok": False, "already": False, "installed": False,
            "reason": "安装包里没有 VB-CABLE（应位于 vb-cable\\VBCABLE_Setup_x64.exe）",
        }
    logger.info("installing VB-CABLE: %s", exe)
    rc = _run_installer(exe, "-i")
    if rc < 0:
        return {"ok": False, "already": False, "installed": False,
                "reason": "需要管理员权限（已取消 UAC 授权）"}
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if detect_installed(force=True):
            logger.info("VB-CABLE installed, device node found")
            return {"ok": True, "already": False, "installed": True, "reason": ""}
        time.sleep(1.0)
    # 安装器退出码正常但节点还没出现：让用户重启后再点一次，不谎报成功
    return {"ok": False, "already": False, "installed": False,
            "reason": f"安装器退出码 {rc}，但驱动节点尚未出现（可能需要重启后重试）"}


def uninstall() -> dict:
    exe = installer_path()
    if exe is None:
        return {"ok": False, "reason": "找不到 VB-CABLE 卸载程序"}
    rc = _run_installer(exe, "-u")
    return {"ok": rc >= 0, "reason": "" if rc >= 0 else "需要管理员权限（已取消 UAC 授权）"}


def status() -> dict:
    """给设置页/托盘用的状态快照。"""
    return {
        "installed": detect_installed(),
        "installer": str(installer_path() or ""),
        "can_install": installer_path() is not None,
        "admin": is_admin(),
    }


def main(action: str) -> int:
    """命令行入口（--install-driver / --uninstall-driver / --check-driver）。"""
    import logging as _logging

    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    if action == "check":
        # 只返回退出码，不弹窗：安装包的 [Run] 段用它判断要不要静默装驱动
        return 0 if detect_installed(force=True) else 1

    result = install() if action == "install" else uninstall()
    message = "已安装" if result.get("ok") else f"失败：{result.get('reason')}"
    logger.info("driver %s: %s", action, message)
    try:
        ctypes.windll.user32.MessageBoxW(0, message, "手机遥控麦 - 虚拟声卡", 0x40)
    except Exception:  # noqa: BLE001
        pass
    return 0 if result.get("ok") else 1
