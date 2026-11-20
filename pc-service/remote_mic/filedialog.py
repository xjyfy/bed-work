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


class _OPENFILENAMEW(ctypes.Structure):    _fields_ = [
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


_comdlg32 = ctypes.WinDLL("comdlg32", use_last_error=True)
_comdlg32.GetOpenFileNameW.argtypes = [ctypes.POINTER(_OPENFILENAMEW)]
_comdlg32.GetOpenFileNameW.restype = ctypes.c_int


def pick_files(title: str = "选择要发送到手机的文件", max_files: int = 50) -> List[str]:
    """弹多选文件框，返回绝对路径列表；取消返回 []。"""
    buf = ctypes.create_unicode_buffer(65536)
    ofn = _OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
    # c_wchar_p 字段不能直接吃 c_wchar_Array（新版 ctypes 会报
    # "incompatible types, c_wchar_Array_65536 instance instead of c_wchar_p"），必须 cast
    ofn.lpstrFile = ctypes.cast(buf, ctypes.c_wchar_p)
    ofn.nMaxFile = len(buf)
    ofn.lpstrTitle = title
    ofn.lpstrFilter = "所有文件\0*\0\0"
    ofn.Flags = (
        OFN_EXPLORER | OFN_ALLOWMULTISELECT | OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST
        | OFN_NOCHANGEDIR | OFN_HIDEREADONLY
    )
    try:
        ok = _comdlg32.GetOpenFileNameW(ctypes.byref(ofn))
    except Exception as exc:  # noqa: BLE001 - 极端环境下别把托盘线程带崩
        logger.warning("file dialog failed: %s", exc)
        return []
    if not ok:
        return []

    # 多选时缓冲区是 "目录\0文件1\0文件2\0\0"，c_wchar_Array 切片会在第一个 NUL 处截断，
    # 必须按长度整段读出，再自己 split
    raw = ctypes.wstring_at(ctypes.addressof(buf), len(buf))
    if not raw:
        return []
    parts = [p for p in raw.split("\0") if p]
    if not parts:
        return []
    if len(parts) == 1:
        return [parts[0]]
    folder = parts[0]
    paths = []
    for name in parts[1:]:
        paths.append(str(__import__("os").path.join(folder, name)))
        if len(paths) >= max_files:
            break
    return paths
