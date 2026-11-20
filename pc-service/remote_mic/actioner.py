"""动作执行器：把手机端语义码翻译成键盘 / 媒体键 / 滚轮 / 应用启动 / 各类会话状态机。

按键模型（二期收敛后）：
- 方向键统一映射上下左右；确认统一映射回车；取消统一映射 Esc。
- 切换浮层不再有独立的方向 / 确认 / 取消：进入后用同一组方向键遍历，
  下一次确认释放 Alt 落地，取消则先 Esc 关浮层再释放 Alt。
- 文本选择模式已下线：手机端不再有「复制」，改为「远程粘贴」（手机剪贴板 → 电脑剪贴板）。
- 视频播放暂停专走空格；多媒体播放暂停走媒体键，两者分开避免互相抵消。
- 新对话 Ctrl+N；发送按前台应用在 send_hotkey 里选快捷键。

所有修饰键在断线 / 异常 / 退出时由 reset_modifiers 兜底释放，避免卡住。
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import threading
import time
from ctypes import wintypes
from typing import Callable, Dict, Optional

from pynput.keyboard import Controller as KeyController, Key, KeyCode
from pynput.mouse import Controller as MouseController

from . import clipboard

logger = logging.getLogger(__name__)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ---- 低层 SendInput 鼠标结构（参照 SayAll：逐格真实滚轮输入）----
_WHEEL_DELTA = 120
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_WHEEL = 0x0800
_INPUT_MOUSE = 0


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("mi", _MOUSEINPUT)]


class _WindowPlacement(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("showCmd", wintypes.DWORD),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


_ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# ---- Win32 声明（缺 argtypes/restype 时 64 位句柄会被截断成 32 位 int）----
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_GWL_STYLE = -16
_GW_OWNER = 4
_WS_CAPTION = 0x00C00000
_WS_THICKFRAME = 0x00040000
_MIN_WIN_W = 120   # 真实窗口最小宽（像素），用于排除消息窗 / 辅助窗
_MIN_WIN_H = 80
_SW_SHOW = 5
_SW_RESTORE = 9
_SWP_NOZORDER = 0x0004
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79

user32.EnumWindows.argtypes = [_ENUM_WINDOWS_PROC, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(_WindowPlacement)]
user32.GetWindowPlacement.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.LockSetForegroundWindow.argtypes = [wintypes.UINT]
user32.LockSetForegroundWindow.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL

# 普通方向 / 功能键映射
_KEY_MAP = {
    "up": Key.up,
    "down": Key.down,
    "left": Key.left,
    "right": Key.right,
    "ok": Key.enter,
    "esc": Key.esc,
    "delete": Key.backspace,
    "prev_track": Key.media_previous,
    "next_track": Key.media_next,
    "play_pause": Key.media_play_pause,
    "video_play_pause": Key.space,
    "vol_up": Key.media_volume_up,
    "vol_down": Key.media_volume_down,
}

# 发送快捷键里可用作键名的功能键（单字符键另行解析）
_SEND_NAMED_KEYS = {
    "enter": Key.enter,
    "space": Key.space,
    "tab": Key.tab,
    "esc": Key.esc,
    "backspace": Key.backspace,
}

_MOD_MAP = {
    "ctrl": Key.ctrl,
    "shift": Key.shift,
    "alt": Key.alt,
    "win": Key.cmd,
}

# 滚轮每次滚动格数（抖音 / 网页对单格不敏感时调大）
SCROLL_STEP = 3


class Actioner:
    def __init__(
        self,
        config: dict,
        on_voice_state_change: Optional[Callable[[bool], None]] = None,
        on_notice: Optional[Callable[[str, str], None]] = None,
    ):
        self._config = config
        self._on_notice = on_notice
        self._keyboard = KeyController()
        self._mouse = MouseController()
        self._lock = threading.RLock()

        self._alt_tab_active = False
        self._voice_on = False
        self._tab_index = 0
        self._held: set[Key] = set()

        self._on_voice_state_change = on_voice_state_change

    def _notice(self, title: str, message: str) -> None:
        """动作失败给电脑端一个可读反馈（托盘气泡），不能静默无响应。"""
        if self._on_notice is None:
            logger.info("notice (no sink): %s | %s", title, message)
            return
        try:
            self._on_notice(title, message)
        except Exception:  # noqa: BLE001
            logger.exception("notice failed: %s", title)

    # ------------------------------------------------------------------ 基础

    def _press(self, key: Key) -> None:
        self._keyboard.press(key)
        self._held.add(key)

    def _release(self, key: Key) -> None:
        self._keyboard.release(key)
        self._held.discard(key)

    def _tap(self, key: Key) -> None:
        self._press(key)
        self._release(key)

    def _combo(self, modifiers: list[str], key: Optional[Key]) -> None:
        mod_keys = [_MOD_MAP[m] for m in modifiers if m in _MOD_MAP]
        for m in mod_keys:
            self._press(m)
        try:
            if key is not None:
                self._press(key)
                self._release(key)
        finally:
            for m in reversed(mod_keys):
                self._release(m)

    def reset_modifiers(self) -> None:
        """兜底释放所有可能按住的修饰键与会话状态。"""
        with self._lock:
            for key in list(self._held):
                self._release(key)
            self._alt_tab_active = False
            self._tab_index = 0
            if self._voice_on:
                self._set_voice(False)

    # ------------------------------------------------------------------ 普通按键

    def handle_key(self, code: str, action: str) -> None:
        with self._lock:
            if code in ("scroll_next", "scroll_prev"):
                self.handle_scroll(code)
                return

            # 切换浮层期间，除方向 / 确认 / 取消以外的动作一律先落地并释放 Alt，
            # 避免 Alt 卡在按下态导致后续键盘异常。
            if self._alt_tab_active and code not in ("up", "down", "left", "right", "ok", "esc"):
                self._release(_MOD_MAP["alt"])
                self._alt_tab_active = False
                logger.info("switcher closed by other key: %s", code)

            if code == "ok":
                if self._alt_tab_active:
                    # 统一下一次确认键：释放 Alt 锁定，完成窗口切换
                    self._release(_MOD_MAP["alt"])
                    self._alt_tab_active = False
                    logger.info("switcher confirmed by unified OK key")
                    return
                self._tap(_KEY_MAP["ok"])
                return

            if code == "esc":
                if self._alt_tab_active:
                    # 先按 Esc 关闭任务切换浮层（不切换），再松开 Alt
                    self._tap(Key.esc)
                    self._release(_MOD_MAP["alt"])
                    self._alt_tab_active = False
                    logger.info("switcher cancelled by unified ESC key")
                    return
                self._tap(Key.esc)
                return

            if code == "new_chat":
                self._combo(["ctrl"], KeyCode.from_char("n"))
                return

            if code == "send":
                self.handle_send()
                return

            if code == "tab_next":
                self.cycle_tab()
                return
            if code in ("up", "down", "left", "right"):
                # 切换浮层里用这四个方向键遍历窗口（Alt 保持按下）
                self._tap(_KEY_MAP[code])
                return

            if code in _KEY_MAP:
                self._tap(_KEY_MAP[code])
                return

            logger.warning("unknown key code: %s", code)

    # ------------------------------------------------------------------ 发送

    # ------------------------------------------------------------------ Tab 循环

    def cycle_tab(self) -> None:
        """每次调用依次发 Ctrl+1 … Ctrl+9，超过 9 回到 1。"""
        self._tab_index = (self._tab_index % 9) + 1
        self._combo(["ctrl"], KeyCode.from_char(str(self._tab_index)))
        logger.info("tab switch: ctrl+%d", self._tab_index)

    # ------------------------------------------------------------------ 发送快捷键

    def _foreground_exe(self) -> str:
        """当前前台窗口所属进程的可执行文件名（小写）；取不到返回空串。"""
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        hproc = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not hproc:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(hproc)
        return ""

    @staticmethod
    def _resolve_send_key(name: str) -> Optional[Key]:
        name = name.strip().lower()
        if name in _SEND_NAMED_KEYS:
            return _SEND_NAMED_KEYS[name]
        if len(name) == 1:
            return KeyCode.from_char(name)
        return None

    def _slot_for_exe(self, exe: str) -> str:
        """前台程序属于哪个应用栏位（按 exe 名比对 config.apps 里的路径）。"""
        if not exe:
            return ""
        import os

        for slot, path in (self._config.get("apps") or {}).items():
            if not path:
                continue
            if os.path.basename(str(path)).lower() == exe:
                return str(slot)
        return ""

    def handle_send(self) -> None:
        """发送键优先级：栏位自定义快捷键 → send_hotkey 按前台程序 → default。"""
        table = self._config.get("send_hotkey") or {}
        if not isinstance(table, dict):
            table = {}
        slot_table = self._config.get("slot_send_hotkey") or {}
        if not isinstance(slot_table, dict):
            slot_table = {}

        exe = self._foreground_exe()
        slot = self._slot_for_exe(exe)
        spec = None
        if slot:
            slot_spec = slot_table.get(slot)
            if isinstance(slot_spec, str) and slot_spec.strip():
                spec = [p for p in slot_spec.replace("+", " ").split() if p]
            elif isinstance(slot_spec, list) and slot_spec:
                spec = slot_spec
        if spec is None and exe:
            spec = table.get(exe)
        if spec is None:
            spec = table.get("default", ["enter"])
        if isinstance(spec, str):
            spec = [p for p in spec.replace("+", " ").split() if p]
        if not isinstance(spec, list) or not spec:
            spec = ["enter"]

        mods = [str(k).lower() for k in spec if str(k).lower() in _MOD_MAP]
        mains = [str(k) for k in spec if str(k).lower() not in _MOD_MAP]
        key = self._resolve_send_key(mains[0]) if mains else None
        if key is None:
            logger.warning("invalid send_hotkey for %s: %s, fallback to Enter", exe or "?", spec)
            key = Key.enter
            mods = []
        logger.info(
            "send: exe=%s slot=%s combo=%s",
            exe or "(unknown)",
            slot or "-",
            mods + [mains[0] if mains else "enter"],
        )
        self._combo(mods, key)

    # ------------------------------------------------------------------ 应用启动

    def _find_windows_by_exe(self, exe_name: str) -> list[dict]:
        """枚举属于该 exe 的「真实窗口」，**包含已最小化到托盘的隐藏窗口**。

        这是「切换应用却新起一个实例」的根因修复点：原实现只认 `IsWindowVisible`，
        而微信 / 豆包等关到托盘时主窗口是 hidden 状态，进程明明活着一窗口却看不见，
        于是被误判成"没在运行"而 `startfile` 多开了一个新实例。

        真实窗口判定：无 owner（顶层窗口）、带标题栏或可缩放边框（排除
        Qt/Chrome 的 *MessageWindow*、IME、全 0 样式的消息窗）、尺寸够大。
        """
        target = exe_name.lower()
        found: list[dict] = []

        @_ENUM_WINDOWS_PROC
        def _enum_cb(hwnd: wintypes.HWND, _lparam: wintypes.LPARAM) -> bool:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return True
            hproc = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if not hproc:
                return True
            try:
                buf = ctypes.create_unicode_buffer(260)
                size = wintypes.DWORD(260)
                if not kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                    return True
                if os.path.basename(buf.value).lower() != target:
                    return True
            finally:
                kernel32.CloseHandle(hproc)

            style = int(user32.GetWindowLongW(hwnd, _GWL_STYLE) or 0)
            if not style & (_WS_CAPTION | _WS_THICKFRAME):
                return True          # 消息窗 / IME / 无边框辅助窗
            if user32.GetWindow(hwnd, _GW_OWNER):
                return True          # 有 owner = 子级弹层，不是主窗口
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            placement = _WindowPlacement()
            placement.length = ctypes.sizeof(_WindowPlacement)
            user32.GetWindowPlacement(hwnd, ctypes.byref(placement))
            # 尺寸要用「还原后尺寸」判定：最小化 / 停到屏幕外的窗口
            # （微信就是 showCmd=2、当前 rect 停在 -21333 且只有 158x26）
            # 用当前 rect 会被尺寸过滤误杀，导致"明明开着却说没运行"。
            cur_w = rect.right - rect.left
            cur_h = rect.bottom - rect.top
            normal = placement.rcNormalPosition
            nor_w = normal.right - normal.left
            nor_h = normal.bottom - normal.top
            if max(cur_w, nor_w) < _MIN_WIN_W or max(cur_h, nor_h) < _MIN_WIN_H:
                return True

            title_buf = ctypes.create_unicode_buffer(200)
            user32.GetWindowTextW(hwnd, title_buf, 200)
            cls_buf = ctypes.create_unicode_buffer(200)
            user32.GetClassNameW(hwnd, cls_buf, 200)
            cls = cls_buf.value or ""
            # 排除 *MessageWindow / 托盘图标消息窗 这类"看起来有标题但不是主窗口"的壳
            if "messagewindow" in cls.lower() or "ime" in cls.lower():
                return True
            placement = _WindowPlacement()
            placement.length = ctypes.sizeof(_WindowPlacement)
            user32.GetWindowPlacement(hwnd, ctypes.byref(placement))
            found.append({
                "hwnd": int(hwnd),
                "pid": int(pid.value),
                "title": title_buf.value,
                "visible": bool(user32.IsWindowVisible(hwnd)),
                "has_title": bool(title_buf.value),
                "show_cmd": placement.showCmd,
                "minimized": placement.showCmd == 2,
                # 停在屏幕外（如 -21333）也算隐藏：SetForegroundWindow 对它无效，得先挪回来
                "offscreen": rect.right <= 0 or rect.bottom <= 0 or normal.left < -9000,
                "area": max(cur_w * cur_h, nor_w * nor_h),
                "rect": (rect.left, rect.top, rect.right, rect.bottom),
                "normal_rect": (normal.left, normal.top, normal.right, normal.bottom),
            })
            return True

        user32.EnumWindows(_enum_cb, 0)
        # 可见 → 有标题 → 非最小化 → 面积大，依次优先
        found.sort(
            key=lambda c: (c["visible"], c["has_title"], not c["minimized"], c["area"]),
            reverse=True,
        )
        return found

    def _process_count_by_exe(self, exe_name: str) -> int:
        """统计同名可执行文件的进程数（不看窗口，用于判断"是否已在运行"）。"""
        _TH32CS_SNAPPROCESS = 0x2
        target = exe_name.lower()
        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snapshot or int(snapshot) == -1:
            return 0
        count = 0
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                if os.path.basename(entry.szExeFile).lower() == target:
                    count += 1
                ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        except Exception:  # noqa: BLE001
            logger.exception("process snapshot failed")
        finally:
            kernel32.CloseHandle(snapshot)
        return count

    def _focus_window_by_exe(self, exe_name: str) -> bool:
        """查找同名进程的主窗口并前置；找到返回 True（含托盘隐藏窗口）。"""
        wins = self._find_windows_by_exe(exe_name)
        if not wins:
            return False
        if len(wins) > 1 or not wins[0]["visible"]:
            logger.info(
                "windows of %s: %d (hidden=%d) -> try %s",
                exe_name, len(wins), sum(1 for w in wins if not w["visible"]), wins[0]["hwnd"],
            )
        for win in wins:
            # 最大化保持最大化；隐藏（托盘）/ 停到屏幕外 / 普通 / 最小化一律 restore 拉回前台
            show_cmd = 3 if win["show_cmd"] == 3 else _SW_RESTORE
            if self._activate_window(win["hwnd"], show_cmd, hidden=(not win["visible"]) or win["offscreen"]):
                return True
        return False

    def _is_foreground(self, hwnd: int) -> bool:
        fg = user32.GetForegroundWindow()
        return fg != 0 and fg == hwnd

    def _readback(self, hwnd: int) -> bool:
        """有界复核前台读回是否属于目标窗口（Windows 可能异步完成切换）。"""
        for _ in range(3):
            if self._is_foreground(hwnd):
                return True
            time.sleep(0.1)
        return self._is_foreground(hwnd)

    def _screen_bounds(self) -> tuple[int, int, int, int]:
        x = user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
        y = user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        w = user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
        h = user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
        return x, y, x + w, y + h

    def _window_rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom

    def _on_screen(self, hwnd: int) -> bool:
        """窗口是否真的落在虚拟桌面可见区域内（不是挪到 -21333 那种"假显示"）。"""
        r = self._window_rect(hwnd)
        if not r:
            return False
        sx, sy, ex, ey = self._screen_bounds()
        iw = min(r[2], ex) - max(r[0], sx)
        ih = min(r[3], ey) - max(r[1], sy)
        return iw >= 80 and ih >= 60

    def _pull_on_screen(self, hwnd: int) -> None:
        """把停在屏幕外的窗口搬回可见区域。

        微信关到托盘 / 最小化时窗口会被挪到 (-21333, -21333)，这种状态下
        ShowWindow(SW_RESTORE) + SetForegroundWindow 会**成功返回**、前台读回也
        通过，但窗口仍在屏幕外——用户看到的就是"点了快捷应用没反应"。
        """
        if self._on_screen(hwnd):
            return
        sx, sy, ex, ey = self._screen_bounds()
        r = self._window_rect(hwnd) or (0, 0, 0, 0)
        w = r[2] - r[0]
        h = r[3] - r[1]
        if w < 200 or h < 150:
            w, h = 1200, 800
        w = min(w, ex - sx - 80)
        h = min(h, ey - sy - 80)
        x = sx + max(40, (ex - sx - w) // 2)
        y = sy + max(40, (ey - sy - h) // 2)
        user32.SetWindowPos(hwnd, 0, x, y, w, h, _SWP_NOZORDER)
        logger.info("window %s moved on-screen to (%d,%d %dx%d)", hwnd, x, y, w, h)
        time.sleep(0.05)

    def _visible_ok(self, hwnd: int) -> bool:
        return bool(user32.IsWindowVisible(hwnd)) and self._on_screen(hwnd)

    def _activate_window(self, hwnd: int, show_cmd: int, hidden: bool = False) -> bool:
        """前置目标窗口；成功判据是前台读回属于目标，而非 SetForegroundWindow 的 BOOL。

        流程：隐藏窗口先 ShowWindow(SW_SHOW) 拉回来 → 解锁前台锁 →
        AttachThreadInput 附加前台线程（失败不阻断）→ ShowWindow/BringToTop/
        SetForegroundWindow → 读回复核；仍失败则最小化当前前台窗口，再还原并置前目标。
        """
        LSF_UNLOCKLOCK = 1
        SW_MINIMIZE = 6
        try:
            user32.LockSetForegroundWindow(LSF_UNLOCKLOCK)
        except Exception:  # noqa: BLE001
            pass

        if hidden:
            # 托盘里的窗口必须先显形，否则 SetForegroundWindow 对隐藏窗口无效
            user32.ShowWindow(hwnd, _SW_SHOW)
            time.sleep(0.05)
            self._pull_on_screen(hwnd)

        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg, None)
        our_tid = kernel32.GetCurrentThreadId()
        attached = bool(fg_tid) and fg_tid != our_tid
        if attached:
            attached = bool(user32.AttachThreadInput(fg_tid, our_tid, True))
        try:
            user32.ShowWindow(hwnd, show_cmd)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(fg_tid, our_tid, False)

        # ShowWindow 之后再确认一次位置：SW_RESTORE 可能把窗口恢复到屏幕外的老位置
        self._pull_on_screen(hwnd)
        if self._readback(hwnd) and self._visible_ok(hwnd):
            return True

        # 兜底：当前前台仍占用且拒绝附加时，先把它最小化，再还原并置前目标。
        cur = user32.GetForegroundWindow()
        if cur and cur != hwnd:
            user32.ShowWindow(cur, SW_MINIMIZE)
            time.sleep(0.1)
        user32.ShowWindow(hwnd, _SW_SHOW)
        user32.ShowWindow(hwnd, show_cmd)
        self._pull_on_screen(hwnd)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        return self._readback(hwnd) and self._visible_ok(hwnd)

    def _extract_exe_name(self, path: str) -> str:
        """从纯路径或带引号/参数的命令行里提取可执行文件名（小写 basename）。

        注意 exe 名本身可能带空格（`DeepSeek Harness.exe`），不能按空格切词，
        否则会截成 `Harness.exe` 而永远匹配不到进程 → 每次点击都新开一个实例。
        """
        import re
        text = path.strip()
        m = re.search(r'"([^"]+\.exe)"', text, re.IGNORECASE)
        if m:
            return os.path.basename(m.group(1)).lower()
        # 未加引号：取到第一个 ".exe" 且其后是空白或字符串结尾为止
        m = re.search(r"^(.*?\.exe)(?=\s|$)", text, re.IGNORECASE)
        if m:
            return os.path.basename(m.group(1)).lower()
        return ""

    def handle_launch(self, slot: str) -> None:
        apps = self._config.get("apps", {})
        path = (apps.get(str(slot)) or "").strip()
        if not path:
            logger.info("launch slot %s is not configured", slot)
            return
        try:
            # 商店应用：uwp:<AUMID>，用 shell:AppsFolder 打开
            if path.lower().startswith("uwp:"):
                aumid = path[4:].strip()
                subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{aumid}"])
                logger.info("launched UWP slot %s: %s", slot, aumid)
                self._tab_index = 0
                return
            exe_name = self._extract_exe_name(path)
            if exe_name:
                # 1) 已有窗口（含关到托盘的隐藏窗口）→ 只切换，绝不新起进程
                wins = self._find_windows_by_exe(exe_name)
                if wins:
                    done = False
                    for win in wins:
                        show_cmd = 3 if win["show_cmd"] == 3 else _SW_RESTORE
                        hidden = (not win["visible"]) or win["offscreen"]
                        if self._activate_window(win["hwnd"], show_cmd, hidden=hidden):
                            logger.info(
                                "focused existing slot %s: %s (hwnd=%s hidden=%s, windows=%d)",
                                slot, exe_name, win["hwnd"], hidden, len(wins),
                            )
                            done = True
                            break
                    if not done:
                        # 关键：窗口找到了但置前失败，绝不能当成"没在运行"去 startfile，
                        # 否则就是用户看到的"切换应用却多开一个新实例"。
                        logger.warning(
                            "slot %s: %s has %d window(s) but activate failed; keep existing instance",
                            slot, exe_name, len(wins),
                        )
                        self._notice("切换应用失败", f"{exe_name} 已有窗口但无法置前，未重复启动")
                    self._tab_index = 0
                    return
                # 2) 进程活着却没有任何真实窗口（例如托盘里只剩余消息窗）→ 同样不启动
                running = self._process_count_by_exe(exe_name)
                if running:
                    logger.warning(
                        "slot %s: %s running (%d process) without a window; skip launch to avoid duplicate",
                        slot, exe_name, running,
                    )
                    self._notice(
                        "应用在后台",
                        f"{exe_name} 正在运行但没有窗口，已跳过启动（避免多开新实例）",
                    )
                    return
            if os.path.exists(path):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(path, shell=True)
            logger.info("launched slot %s: %s", slot, path)
            self._tab_index = 0
        except Exception as exc:  # noqa: BLE001
            logger.error("launch slot %s failed: %s", slot, exc)

    # ------------------------------------------------------------------ Alt-Tab 会话

    def handle_switcher(self, action: str) -> None:
        with self._lock:
            if action == "enter":
                if not self._alt_tab_active:
                    self._press(_MOD_MAP["alt"])
                    self._tap(Key.tab)
                    self._alt_tab_active = True
            elif action == "next":
                if self._alt_tab_active:
                    self._tap(Key.tab)
            elif action == "prev":
                if self._alt_tab_active:
                    self._combo(["shift"], Key.tab)
            elif action == "confirm":
                if self._alt_tab_active:
                    self._release(_MOD_MAP["alt"])
                    self._alt_tab_active = False
                    # 换了窗口，Tab 序号从 Ctrl+1 重新开始
                    self._tab_index = 0
            elif action == "cancel":
                if self._alt_tab_active:
                    # 先按 Esc 关闭任务切换浮层（不切换），再松开 Alt
                    self._tap(Key.esc)
                    self._release(_MOD_MAP["alt"])
                    self._alt_tab_active = False
                    self._tab_index = 0
            else:
                logger.warning("unknown switcher action: %s", action)

    # ------------------------------------------------------------------ 远程粘贴

    def handle_clip(self, text: str) -> bool:
        """手机剪贴板 → 电脑剪贴板。

        写入成功后：前台有文本焦点（有光标）就补一个 Ctrl+V，否则只写剪贴板、
        不发按键，避免把 Ctrl+V 打到非输入控件上。
        """
        with self._lock:
            ok, reason = clipboard.write_text(text)
            if not ok:
                logger.error("remote clip failed: %s", reason)
                return False
            focus = clipboard.foreground_focus()
            policy = str(self._config.get("clip_policy", "auto"))
            do_paste, why = clipboard.should_paste(focus, policy)
            if do_paste:
                self._combo(["ctrl"], KeyCode.from_char("v"))
                logger.info(
                    "remote clip: %d chars written, pasted [%s] (top=%s focus=%s caret=%s)",
                    len(text), why, focus["cls"] or "-", focus["focus_cls"] or "-", focus["caret"],
                )
            else:
                logger.info(
                    "remote clip: %d chars written, clipboard only [%s] (top=%s focus=%s)",
                    len(text), why, focus["cls"] or "-", focus["focus_cls"] or "-",
                )
            return True

    # ------------------------------------------------------------------ 语音状态机

    def _voice_hotkey(self) -> list[str]:
        return self._config.get("voice_hotkey", ["ctrl", "win"])

    def _set_voice(self, on: bool) -> None:
        if on == self._voice_on:
            return
        mods = [_MOD_MAP[m] for m in self._voice_hotkey() if m in _MOD_MAP]
        if on:
            for m in mods:
                self._press(m)
        else:
            for m in reversed(mods):
                self._release(m)
        self._voice_on = on
        if self._on_voice_state_change:
            try:
                self._on_voice_state_change(on)
            except Exception as exc:  # noqa: BLE001
                logger.error("voice state callback failed: %s", exc)

    def handle_voice(self, action: str) -> None:
        """push-to-talk：down 开始，up 停止。"""
        with self._lock:
            if action == "down":
                self._set_voice(True)
            elif action == "up":
                self._set_voice(False)

    def handle_voice_toggle(self, action: str) -> None:
        """常开开关：on 开始，off 停止；也支持不带值的翻转。"""
        with self._lock:
            if action == "on":
                self._set_voice(True)
            elif action == "off":
                self._set_voice(False)
            elif action == "toggle":
                self._set_voice(not self._voice_on)

    @property
    def voice_on(self) -> bool:
        return self._voice_on

    # ------------------------------------------------------------------ 滚轮

    def _foreground_center(self) -> Optional[tuple[int, int]]:
        """返回当前前台窗口客户区中心的物理坐标；取不到返回 None。"""
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if rect.right <= rect.left or rect.bottom <= rect.top:
            return None
        return ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)

    def _move_cursor(self, x: int, y: int) -> None:
        """把物理光标移到 (x, y)，三种 API 依次回退。"""
        # 1) 物理光标 API（Per-Monitor V2 下最准确）
        try:
            if user32.SetPhysicalCursorPos(x, y):
                return
        except Exception:  # noqa: BLE001
            pass
        # 2) 逻辑光标 API
        if user32.SetCursorPos(x, y):
            return
        # 3) SendInput 绝对坐标兜底（归一化到 0..65535）
        sw = user32.GetSystemMetrics(0) or 1
        sh = user32.GetSystemMetrics(1) or 1
        nx = int(x * 65535 / sw)
        ny = int(y * 65535 / sh)
        mi = _MOUSEINPUT(nx, ny, 0, _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE, 0, None)
        user32.SendInput(1, ctypes.byref(_INPUT(_INPUT_MOUSE, mi)),
                         ctypes.sizeof(_INPUT))

    def _send_wheel(self, notches: int, down: bool) -> None:
        """逐格发送真实滚轮：每格 ±120，格间留 20ms，贴近物理滚轮节奏。"""
        delta = -_WHEEL_DELTA if down else _WHEEL_DELTA
        for _ in range(max(1, notches)):
            mi = _MOUSEINPUT(0, 0, ctypes.c_ulong(delta & 0xFFFFFFFF),
                             _MOUSEEVENTF_WHEEL, 0, None)
            user32.SendInput(1, ctypes.byref(_INPUT(_INPUT_MOUSE, mi)),
                             ctypes.sizeof(_INPUT))
            time.sleep(0.02)

    def handle_scroll(self, direction: str) -> None:
        # 手机抖音习惯：上滑看下一个（滚轮向下），下滑看上一个（滚轮向上）。
        # 先把光标移到前台窗口中心，确保滚轮命中目标客户区；再逐格发送。
        center = self._foreground_center()
        if center is not None:
            self._move_cursor(*center)
            time.sleep(0.05)
        down = direction == "scroll_next"
        self._send_wheel(SCROLL_STEP, down)
