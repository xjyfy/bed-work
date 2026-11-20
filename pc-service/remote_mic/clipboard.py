"""手机剪贴板 → 电脑剪贴板（远程粘贴）。

- 只处理纯文本：写 `CF_UNICODETEXT`，UTF-16 传给系统。
- 剪贴板被其它程序占用时 `OpenClipboard` 会失败，这里做有限重试。
- 是否顺带执行 Ctrl+V 由调用方决定，判断依据见 `foreground_has_caret()`。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import time

logger = logging.getLogger(__name__)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
OPEN_CLIPBOARD_RETRIES = 6
OPEN_CLIPBOARD_DELAY = 0.02

# ctypes 默认把返回值当 32 位 int，64 位下指针会被截断（GlobalLock 必失败），
# 所以所有涉及句柄/指针的 API 都要显式声明原型。
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_user32.OpenClipboard.argtypes = [wt.HWND]
_user32.OpenClipboard.restype = wt.BOOL
_user32.EmptyClipboard.argtypes = []
_user32.EmptyClipboard.restype = wt.BOOL
_user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
_user32.SetClipboardData.restype = wt.HANDLE
_user32.CloseClipboard.argtypes = []
_user32.CloseClipboard.restype = wt.BOOL

_kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wt.HGLOBAL
_kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
_kernel32.GlobalUnlock.restype = wt.BOOL
_kernel32.GlobalFree.argtypes = [wt.HGLOBAL]
_kernel32.GlobalFree.restype = wt.HGLOBAL


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("flags", wt.DWORD),
        ("hwndActive", wt.HWND),
        ("hwndFocus", wt.HWND),
        ("hwndCapture", wt.HWND),
        ("hwndMenuOwner", wt.HWND),
        ("hwndMoveSize", wt.HWND),
        ("hwndCaret", wt.HWND),
        ("rcCaret", wt.RECT),
    ]


def write_text(text: str) -> tuple[bool, str]:
    """把 text 写入 Windows 剪贴板；返回 (是否成功, 失败原因)。"""
    if not text:
        return False, "empty text"

    user32 = _user32
    kernel32 = _kernel32

    opened = False
    for _ in range(OPEN_CLIPBOARD_RETRIES):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(OPEN_CLIPBOARD_DELAY)
    if not opened:
        return False, "OpenClipboard failed (clipboard busy)"

    try:
        if not user32.EmptyClipboard():
            return False, "EmptyClipboard failed"

        payload = text.encode("utf-16-le") + b"\x00\x00"
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
        if not handle:
            return False, "GlobalAlloc failed"

        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            return False, "GlobalLock failed"

        ctypes.memmove(ptr, payload, len(payload))
        kernel32.GlobalUnlock(handle)

        # 成功后内存所有权归系统，不能再 GlobalFree
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False, "SetClipboardData failed"
        return True, ""
    finally:
        user32.CloseClipboard()


_user32.GetForegroundWindow.restype = wt.HWND
_user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_user32.GetWindowThreadProcessId.restype = wt.DWORD
_user32.GetGUIThreadInfo.argtypes = [wt.DWORD, ctypes.POINTER(_GUITHREADINFO)]
_user32.GetGUIThreadInfo.restype = wt.BOOL
_user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]

# 这些窗口类说明焦点落在"能接收文本"的控件里：
# - edit / richedit / scintilla：传统 Win32 编辑控件（写字板、记事本）
# - chrome_renderwidgethost：Chromium/Electron 的网页内容宿主（豆包、Chrome、微信小程序）
_EDITABLE_CLASS_HINTS = (
    "edit",
    "richedit",
    "scintilla",
    "chrome_renderwidgethost",
    "chrome_widgetwin",
    "ttedit",
)


def _class_name(hwnd: int) -> str:
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value or ""


def foreground_focus() -> dict:
    """探前台窗口的焦点情况：顶层窗口、焦点子窗类名、是否有 Win32 光标。

    Chromium / Electron 应用（豆包、Chrome）的输入框是网页内容，**没有 Win32 caret**，
    所以只看 `hwndCaret` 会把"其实焦点在输入框"误判成"没焦点"，导致只写剪贴板不粘贴。
    这里额外取 `hwndFocus` 的窗口类名，网页内容宿主（Chrome_RenderWidgetHostHWND）
    同样按"可粘贴"处理。
    """
    result = {"hwnd": 0, "cls": "", "title": "", "focus_hwnd": 0, "focus_cls": "", "caret": False}
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return result
        thread_id = _user32.GetWindowThreadProcessId(hwnd, None)
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(_GUITHREADINFO)
        if not _user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            return result
        title_buf = ctypes.create_unicode_buffer(200)
        _user32.GetWindowTextW(hwnd, title_buf, 200)
        result.update(
            hwnd=int(hwnd),
            cls=_class_name(hwnd),
            title=title_buf.value,
            focus_hwnd=int(info.hwndFocus or 0),
            focus_cls=_class_name(int(info.hwndFocus or 0)),
            caret=bool(info.hwndCaret),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("focus probe failed: %s", exc)
    return result


def accepts_text(focus: dict) -> bool:
    """正向判据：能否确定焦点落在可输入控件里（有 Win32 光标，或焦点是网页内容宿主）。"""
    if focus.get("caret"):
        return True
    focus_cls = (focus.get("focus_cls") or "").lower()
    if any(hint in focus_cls for hint in _EDITABLE_CLASS_HINTS):
        return True
    top_cls = (focus.get("cls") or "").lower()
    return any(hint in top_cls for hint in ("chrome_widgetwin", "chrome_renderwidgethost"))


# 明确"没有输入框"的场景：桌面 / 任务栏 / 资源管理器窗口 / 多任务视图
# 这些位置发 Ctrl+V 没有意义，甚至可能误粘到重命名字段。
_NON_TEXT_TOP_CLASSES = (
    "progman",
    "workerw",
    "shell_traywnd",
    "shell_secondarytraywnd",
    "notifyiconoverflowwindow",
    "tasklistthumbnailwnd",
    "multitaskingviewframe",
    "cabinetwclass",
    "explorewclass",
)


def should_paste(focus: dict, policy: str = "auto") -> tuple[bool, str]:
    """决定要不要补一个 Ctrl+V，返回 (是否粘贴, 判定依据)。

    背景：豆包 / DeepSeek Harness（Electron）、微信（Qt）的输入框**没有 Win32 caret**，
    只认 `hwndCaret` 会把这些应用全部误判成"没焦点"，表现为"剪贴板写进去了但没粘上"。
    所以默认策略改成「能确认是输入控件 → 粘；能确认不是（桌面/任务栏/资源管理器）→ 不粘；
    其余未知应用 → 也粘」，因为 Ctrl+V 打在非输入区域绝大多数情况是无操作，
    而漏粘一次用户就得手工按 Ctrl+V，体验代价更大。

    policy:
      auto（默认）— 上面的规则
      always     — 只要前台不是桌面/任务栏就粘
      caret_only — 只在检测到 Win32 光标时粘（旧行为，最保守）
    """
    policy = (policy or "auto").lower()
    top_cls = (focus.get("cls") or "").lower()
    focus_cls = (focus.get("focus_cls") or "").lower()
    is_shell = any(name in top_cls for name in _NON_TEXT_TOP_CLASSES)

    if not focus.get("hwnd"):
        return False, "no foreground window"
    if policy == "caret_only":
        return (bool(focus.get("caret")), "caret_only: win32 caret" if focus.get("caret") else "caret_only: no caret")
    if policy == "always":
        return (not is_shell, "always: shell surface" if is_shell else "always: app window")
    # auto
    if focus.get("caret"):
        return True, "auto: win32 caret"
    if any(hint in focus_cls for hint in _EDITABLE_CLASS_HINTS):
        return True, "auto: editable focus class"
    if any(hint in top_cls for hint in ("chrome_widgetwin", "chrome_renderwidgethost")):
        return True, "auto: chromium/electron window"
    if is_shell:
        return False, "auto: desktop/explorer surface"
    return True, "auto: unknown app, assume editable"


def foreground_has_caret() -> bool:
    """兼容旧接口：仅表示"传统 Win32 光标"。新代码请用 `should_paste()`。"""
    return bool(foreground_focus().get("caret"))
