"""原生「选择文件」对话框（Win32 GetOpenFileNameW）。

托盘右键菜单跑在 pystray 的线程里，不能开 pywebview 窗口（只能主线程），
所以这里直接用 comdlg32，多选 + 任意线程都安全，打包后也不多带依赖。
"""

from __future__ import annotations

import ctypes
import logging
from typing import List

logger = logging.getLogger(__name__)

OFN_ALLOWMULTISELECT = 0x00000200
OFN_EXPLORER = 0x00080000
OFN_FILEMUSTEXIST = 0x00001000
OFN_PATHMUSTEXIST = 0x00000800
OFN_NOCHANGEDIR = 0x00000008
OFN_HIDEREADONLY = 0x00000004


class _OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", ctypes.c_ulong),
        ("hwndOwner", ctypes.c_void_p),
        ("hInstance", ctypes.c_void_p),
        ("lpstrFilter", ctypes.c_wchar_p),
        ("lpstrCustomFilter", ctypes.c_wchar_p),
        ("nMaxCustFilter", ctypes.c_ulong),
        ("nFilterIndex", ctypes.c_ulong),
        ("lpstrFile", ctypes.c_wchar_p),
        ("nMaxFile", ctypes.c_ulong),
        ("lpstrFileTitle", ctypes.c_wchar_p),
        ("nMaxFileTitle", ctypes.c_ulong),
        ("lpstrInitialDir", ctypes.c_wchar_p),
        ("lpstrTitle", ctypes.c_wchar_p),
        ("Flags", ctypes.c_ulong),
        ("nFileOffset", ctypes.c_ushort),
        ("nFileExtension", ctypes.c_ushort),
        ("lpstrDefExt", ctypes.c_wchar_p),
        ("lCustData", ctypes.c_void_p),
        ("lpfnHook", ctypes.c_void_p),
        ("lpTemplateName", ctypes.c_wchar_p),
        ("pvReserved", ctypes.c_void_p),
        ("dwReserved", ctypes.c_ulong),
        ("FlagsEx", ctypes.c_ulong),
    ]


def pick_files(title: str = "选择要发送到手机的文件", max_files: int = 50) -> List[str]:
    """弹多选文件框，返回绝对路径列表；取消返回 []。"""
    buf = ctypes.create_unicode_buffer(65536)
    ofn = _OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
    ofn.lpstrFile = buf
    ofn.nMaxFile = len(buf)
    ofn.lpstrTitle = title
    ofn.lpstrFilter = "所有文件\0*\0\0"
    ofn.Flags = (
        OFN_EXPLORER | OFN_ALLOWMULTISELECT | OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST
        | OFN_NOCHANGEDIR | OFN_HIDEREADONLY
    )
    try:
        ok = ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn))
    except Exception as exc:  # noqa: BLE001 - 极端环境下别把托盘线程带崩
        logger.warning("file dialog failed: %s", exc)
        return []
    if not ok:
        return []

    raw = buf[:]
    if not raw:
        return []
    # 多选时 buf = 目录 \0 文件名1 \0 文件名2 \0 \0；单选时只有一条完整路径
    parts = [p for p in raw.split("\0") if p]
    if len(parts) == 1:
        return [parts[0]]
    folder = parts[0]
    paths = []
    for name in parts[1:]:
        paths.append(str(__import__("os").path.join(folder, name)))
        if len(paths) >= max_files:
            break
    return paths
