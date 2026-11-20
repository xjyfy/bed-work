"""系统托盘：连接状态、控制权切换、发送文件、开机自启、设置页入口、退出。

（"刷新声卡设备"已移到设置页的「音频输出」卡片里，托盘只留日常入口。）
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem

from . import autostart

logger = logging.getLogger(__name__)


def _make_icon_image(color: str) -> Image.Image:
    img = Image.new("RGB", (64, 64), "white")
    draw = ImageDraw.Draw(img)
    draw.ellipse((12, 12, 52, 52), fill=color)
    return img


class Tray:
    def __init__(
        self,
        on_quit: Callable[[], None],
        on_open_settings: Optional[Callable[[], None]] = None,
        on_switch_remote: Optional[Callable[[str], None]] = None,
        on_send_files: Optional[Callable[[], None]] = None,
    ):
        self._on_quit = on_quit
        self._on_open_settings = on_open_settings
        self._on_switch_remote = on_switch_remote
        self._on_send_files = on_send_files
        self._icon: Optional[Icon] = None
        self._status = "未连接"
        self._remotes: list[tuple[str, str, bool]] = []  # (id, name, active)

    # ------------------------------------------------------------------ 状态

    def set_status(self, status: str) -> None:
        self._status = status
        if self._icon is not None:
            self._icon.title = f"手机遥控麦 - {status}"
            green = "#4CAF50" if self._remotes else "#9E9E9E"
            if self._remotes and not any(r[2] for r in self._remotes):
                green = "#FFA000"
            self._icon.icon = _make_icon_image(green)
            self._refresh_menu()

    def set_remotes(self, remotes: list[tuple[str, str, bool]]) -> None:        # 同一 remote_id 只保留一条（防御服务端重复连接残留）
        seen: dict[str, tuple[str, str, bool]] = {}
        for rid, name, active in remotes:
            if rid in seen:
                seen[rid] = (rid, name, seen[rid][2] or active)
            else:
                seen[rid] = (rid, name, active)
        self._remotes = list(seen.values())
        self._refresh_menu()

    def notify(self, message: str, title: str = "手机遥控麦") -> None:
        """托盘气泡提示（远程粘贴写剪贴板失败时用）。"""
        if self._icon is None:
            logger.warning("tray not ready, drop notification: %s", message)
            return
        try:
            self._icon.notify(message, title)
        except Exception:  # noqa: BLE001 - 通知失败不能拖垮服务线程
            logger.exception("tray notify failed")

    def _refresh_menu(self) -> None:
        if self._icon is not None:
            self._icon.menu = self._menu()
            self._icon.update_menu()

    # ------------------------------------------------------------------ 菜单

    def _switch_menu(self) -> Menu:
        items = []
        for rid, name, active in self._remotes:
            label = ("● " if active else "○ ") + name
            items.append(MenuItem(label, lambda _, r=rid: self._switch(r)))
        if not items:
            items.append(MenuItem("（无已连接遥控器）", None, enabled=False))
        return Menu(*items)

    def _switch(self, remote_id: str) -> None:
        if self._on_switch_remote is not None:
            self._on_switch_remote(remote_id)

    def _menu(self):
        return Menu(
            MenuItem(lambda _: f"状态：{self._status}", None, enabled=False),
            MenuItem("切换控制遥控器", self._switch_menu()),
            MenuItem("发送文件到手机…", lambda _: self._send_files()),
            Menu.SEPARATOR,
            MenuItem("开机自动启动", lambda _: self._toggle_autostart(), checked=self._autostart_checked),
            MenuItem("设置…", lambda _: self._open_settings()),
            MenuItem("退出", lambda _: self._quit()),
        )

    def _autostart_checked(self, _item=None) -> bool:
        return autostart.is_enabled()

    def _toggle_autostart(self) -> None:
        enabled = autostart.toggle()
        self.notify("已开启开机自动启动" if enabled else "已关闭开机自动启动")
        self._refresh_menu()

    def _send_files(self) -> None:
        # 选文件 + 传输都在后台线程做，别把托盘菜单线程卡住
        if self._on_send_files is None:
            return
        threading.Thread(target=self._send_files_worker, daemon=True, name="tray-send-files").start()

    def _send_files_worker(self) -> None:
        try:
            self._on_send_files()
        except Exception:  # noqa: BLE001 - 托盘回调异常会被 pystray 静默吞掉
            logger.exception("tray send files failed")

    def _open_settings(self) -> None:
        if self._on_open_settings is not None:
            try:
                self._on_open_settings()
            except Exception:  # noqa: BLE001 - 托盘回调异常否则会被 pystray 静默吞掉
                logger.exception("open settings failed")

    def _quit(self) -> None:
        if self._icon is not None:
            self._icon.stop()
        self._on_quit()

    # ------------------------------------------------------------------ 运行

    def run(self) -> None:
        self._icon = Icon("remote-mic", _make_icon_image("#9E9E9E"), "手机遥控麦", menu=self._menu())
        self._icon.title = "手机遥控麦 - 未连接"
        self._icon.run()

    def run_detached(self) -> None:
        """在后台线程跑托盘，把主线程让给 pywebview 的设置窗口。"""
        self._icon = Icon("remote-mic", _make_icon_image("#9E9E9E"), "手机遥控麦", menu=self._menu())
        self._icon.title = "手机遥控麦 - 未连接"
        self._icon.run_detached()

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()
