"""动作执行器：把手机端语义码翻译成键盘 / 媒体键 / 滚轮 / 应用启动 / 各类会话状态机。

按键模型（二期收敛后）：
- 方向键统一映射上下左右；确认统一映射回车；取消统一映射 Esc。
- 切换浮层不再有独立的方向 / 确认 / 取消：进入后用同一组方向键遍历，
  下一次确认释放 Alt 落地，取消则先 Esc 关浮层再释放 Alt。
- 文本选择模式（select_mode）：方向键变为 Shift+方向，确认键复制。
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
    def __init__(self, config: dict, on_voice_state_change: Optional[Callable[[bool], None]] = None):
        self._config = config
        self._keyboard = KeyController()
        self._mouse = MouseController()
        self._lock = threading.RLock()

        self._select_mode = False
        self._alt_tab_active = False
        self._voice_on = False
        self._tab_index = 0
        self._held: set[Key] = set()

        self._on_voice_state_change = on_voice_state_change

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
            self._select_mode = False
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
                if self._select_mode:
                    # 先确保修饰键抬起，再 Ctrl+C，给剪贴板一点落盘时间
                    logger.info("select mode: Ctrl+C copy")
                    self._combo(["ctrl"], KeyCode.from_char("c"))
                    time.sleep(0.06)
                    self._select_mode = False
                    # 复制后移动光标取消文字选中高亮（编辑框 / 可编辑控件生效）
                    self._tap(Key.right)
                else:
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
                if self._select_mode:
                    self._select_mode = False
                    self._tap(Key.right)
                    logger.info("select mode exited by ESC")
                    return
                self._tap(Key.esc)
                return

            if code == "paste":
                self._combo(["ctrl"], KeyCode.from_char("v"))
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
                if self._select_mode:
                    # 选择模式：Shift + 方向扩展选区
                    self._combo(["shift"], _KEY_MAP[code])
                else:
                    # 切换浮层里同样用这四个方向键遍历窗口（Alt 保持按下）
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

    def _focus_window_by_exe(self, exe_name: str) -> bool:
        """查找同名可执行文件的主可见窗口并前置；找到返回 True。

        过滤离屏无标题的辅助窗口（部分应用会挂 -9999 位置的隐藏子窗），
        前置时保留窗口原本的最大化 / 普通状态，不强制还原缩小。
        """
        candidates: list[dict] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum_cb(hwnd: wintypes.HWND, _lparam: wintypes.LPARAM) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == 0:
                return True
            hproc = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
            if not hproc:
                return True
            try:
                buf = ctypes.create_unicode_buffer(260)
                size = wintypes.DWORD(260)
                if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                    if os.path.basename(buf.value).lower() == exe_name.lower():
                        title_buf = ctypes.create_unicode_buffer(128)
                        user32.GetWindowTextW(hwnd, title_buf, 128)
                        title = title_buf.value
                        placement = _WindowPlacement()
                        placement.length = ctypes.sizeof(_WindowPlacement)
                        user32.GetWindowPlacement(hwnd, ctypes.byref(placement))
                        on_screen = placement.rcNormalPosition.left > -9000
                        candidates.append({
                            "hwnd": int(hwnd),
                            "title": title,
                            "show_cmd": placement.showCmd,
                            "has_title": bool(title),
                            "on_screen": on_screen,
                            "minimized": placement.showCmd == 2,
                        })
            finally:
                kernel32.CloseHandle(hproc)
            return True

        user32.EnumWindows(_enum_cb, 0)
        # 排除离屏无标题的辅助窗口；有标题、在屏、非最小化的主窗口优先
        main = [c for c in candidates if c["has_title"] or c["on_screen"]]
        if not main:
            return False
        main.sort(key=lambda c: (c["has_title"], c["on_screen"], not c["minimized"]), reverse=True)
        win = main[0]

        hwnd = win["hwnd"]
        # 最大化保持最大化；其余（普通 / 最小化）还原，避免把全屏窗口缩小
        show_cmd = 3 if win["show_cmd"] == 3 else 9  # SW_MAXIMIZE / SW_RESTORE
        return self._activate_window(hwnd, show_cmd)

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

    def _activate_window(self, hwnd: int, show_cmd: int) -> bool:
        """前置目标窗口；成功判据是前台读回属于目标，而非 SetForegroundWindow 的 BOOL。

        流程：解锁前台锁 → AttachThreadInput 附加前台线程（失败不阻断）→
        ShowWindow/BringToTop/SetForegroundWindow → 读回复核；仍失败则最小化当前
        前台窗口，再还原并置前目标。不注入 Alt 等修饰键，避免卡在按下态导致全局键盘异常。
        """
        LSF_UNLOCKLOCK = 1
        SW_MINIMIZE = 6
        try:
            user32.LockSetForegroundWindow(LSF_UNLOCKLOCK)
        except Exception:  # noqa: BLE001
            pass

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

        if self._readback(hwnd):
            return True

        # 兜底：当前前台仍占用且拒绝附加时，先把它最小化，再还原并置前目标。
        cur = user32.GetForegroundWindow()
        if cur and cur != hwnd:
            user32.ShowWindow(cur, SW_MINIMIZE)
            time.sleep(0.1)
        user32.ShowWindow(hwnd, show_cmd)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        return self._readback(hwnd)

    def _extract_exe_name(self, path: str) -> str:
        """从纯路径或带引号/参数的命令行里提取可执行文件名（小写 basename）。"""
        import re
        m = re.search(r'"([^"]+\.exe)"', path, re.IGNORECASE) or re.search(r"(\S+\.exe)", path, re.IGNORECASE)
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
            # 已在运行则前置已有窗口，避免重复新开一个实例 / 窗口
            if exe_name and self._focus_window_by_exe(exe_name):
                logger.info("focused existing slot %s: %s", slot, exe_name)
                self._tab_index = 0
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

    # ------------------------------------------------------------------ 选区模式

    def handle_select(self, action: str) -> None:
        with self._lock:
            if action == "enter":
                self._select_mode = True
                logger.info("select mode entered")
            elif action == "exit":
                self._select_mode = False
                # 退出选区：轻触方向键取消文字选中高亮（光标移到选区一端）
                self._tap(Key.right)
                logger.info("select mode exited")

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
