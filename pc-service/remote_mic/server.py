"""WebSocket 服务端：多遥控器 + token 认证 + 单控制权。

控制权规则：
- 先连上的遥控器取得控制权；后续连接为待命（可连上但按键不生效）。
- 托盘菜单可切换控制中的遥控器；被设置页删除的遥控器立即断开且 token 失效。
- 控制权遥控器断线后，自动顺延给最早连上的待命遥控器。

认证：
- 首帧必须是 {"type":"auth","token":...,"device":...}；token 不在 config.remotes 里即拒绝。
- 认证成功回 {"type":"auth_ok","active":bool,"computer":名称,"remotes":[...]}。
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from typing import Optional

from websockets.asyncio.server import ServerConnection, serve

from .actioner import Actioner
from .audio_out import AudioOut
from .store import (
    CONFIG_PATH,
    add_remote,
    computer_name,
    ensure_basics,
    find_remote_by_token,
    load_config,
    remove_remote,
    save_config,
    touch_remote,
)
from .tray import Tray

logger = logging.getLogger("remote_mic")


class _Client:
    __slots__ = ("remote_id", "name", "ws", "ip", "seq")

    def __init__(self, remote_id: str, name: str, ws: ServerConnection, ip: str, seq: int):
        self.remote_id = remote_id
        self.name = name
        self.ws = ws
        self.ip = ip
        self.seq = seq


class RemoteMicService:
    def __init__(self, config: dict):
        self._config = ensure_basics(config)
        self._audio = AudioOut(self._config)
        self._actioner = Actioner(self._config, on_voice_state_change=self._audio.set_active)
        self._tray: Optional[Tray] = None

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._stop_event: Optional[asyncio.Event] = None
        self._settings_queue: Optional[queue.Queue] = None
        self._stopping: Optional[threading.Event] = None

        self._clients: list[_Client] = []
        self._active_id: Optional[str] = None
        self._seq = 0
        self._audio_frames = 0
        self._audio_bytes = 0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 配置

    def _save(self) -> None:
        save_config(self._config, CONFIG_PATH)

    @property
    def config(self) -> dict:
        return self._config

    def reload_config(self) -> None:
        """设置页保存后调用：只重载与应用/快捷键/绑定相关的字段，端口等运行期参数不动。"""
        try:
            fresh = load_config(CONFIG_PATH)
        except Exception as exc:  # noqa: BLE001
            logger.error("reload config failed: %s", exc)
            return
        for key in ("apps", "send_hotkey", "slot_send_hotkey", "computer_name", "voice_hotkey", "remotes"):
            if key in fresh:
                self._config[key] = fresh[key]
        logger.info("config reloaded from disk")

    # ------------------------------------------------------------------ 状态

    def _status_frame(self) -> dict:
        clients = [
            {
                "id": c.remote_id,
                "name": c.name,
                "ip": c.ip,
                "active": c.remote_id == self._active_id,
            }
            for c in sorted(self._clients, key=lambda x: x.seq)
        ]
        return {
            "type": "status",
            "computer": self._config.get("computer_name") or computer_name(),
            "active_id": self._active_id or "",
            "clients": clients,
            "voice": self._actioner.voice_on,
        }

    async def _broadcast_status(self) -> None:
        base = self._status_frame()
        for c in list(self._clients):
            frame = dict(base)
            frame["active"] = c.remote_id == self._active_id
            try:
                await c.ws.send(json.dumps(frame, ensure_ascii=False))
            except Exception:  # noqa: BLE001
                pass
        self._sync_tray()

    def _sync_tray(self) -> None:
        if self._tray is not None:
            self._tray.set_remotes(
                [(c.remote_id, c.name, c.remote_id == self._active_id) for c in self._clients]
            )
            if not self._clients:
                self._tray.set_status("未连接")
            else:
                active = next((c for c in self._clients if c.remote_id == self._active_id), None)
                self._tray.set_status(f"已连接 · {active.name}" if active else "已连接（待选）")

    def _find_client(self, remote_id: str) -> Optional[_Client]:
        return next((c for c in self._clients if c.remote_id == remote_id), None)

    # ------------------------------------------------------------------ 认证

    async def _authenticate(self, ws: ServerConnection, msg: dict) -> Optional[_Client]:
        token = str(msg.get("token") or "")
        device = str(msg.get("device") or "")
        remote = find_remote_by_token(self._config, token)
        if remote is None:
            await ws.send(json.dumps({"type": "auth_fail", "reason": "token_invalid"}, ensure_ascii=False))
            logger.warning("auth rejected from %s", ws.remote_address)
            return None

        remote_id = str(remote.get("id"))

        # 同一台遥控器重复连接：踢掉旧连接，只保留最新一条（否则托盘会出现多个同名设备）
        stale: list[_Client] = []
        with self._lock:
            stale = [c for c in self._clients if c.remote_id == remote_id]
            for c in stale:
                if c in self._clients:
                    self._clients.remove(c)
        for c in stale:
            logger.info("replace stale connection of %s (%s)", c.name, c.ip)
            try:
                await c.ws.close(code=1000, reason="replaced")
            except Exception:  # noqa: BLE001
                pass

        self._seq += 1
        client = _Client(
            remote_id=remote_id,
            name=device or str(remote.get("name") or "遥控器"),
            ws=ws,
            ip=(ws.remote_address[0] if ws.remote_address else ""),
            seq=self._seq,
        )
        with self._lock:
            self._clients.append(client)
            touch_remote(remote, device_name=client.name, ip=client.ip)
            if self._active_id is None or not any(c.remote_id == self._active_id for c in self._clients):
                self._active_id = client.remote_id
        self._save()

        active = client.remote_id == self._active_id
        await ws.send(
            json.dumps(
                {
                    "type": "auth_ok",
                    "active": active,
                    "computer": self._config.get("computer_name") or computer_name(),
                    "status": self._status_frame(),
                },
                ensure_ascii=False,
            )
        )
        logger.info("remote bound: %s (%s) active=%s", client.name, client.ip, active)
        await self._broadcast_status()
        return client

    def _unregister(self, client: Optional[_Client]) -> None:
        if client is None:
            return
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
            if self._active_id == client.remote_id:
                # 控制权顺延给最早连上的待命遥控器
                self._active_id = min(self._clients, key=lambda c: c.seq).remote_id if self._clients else None
        self._actioner.reset_modifiers()
        self._audio.set_active(False)
        logger.info("remote disconnected: %s", client.name)
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast_status(), self._loop)

    # ------------------------------------------------------------------ 控制权

    def switch_active(self, remote_id: str) -> bool:
        """托盘菜单调用：切换控制权。"""
        with self._lock:
            if not self._find_client(remote_id):
                return False
            self._active_id = remote_id
        self._actioner.reset_modifiers()
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast_status(), self._loop)
        logger.info("active remote switched to %s", remote_id)
        return True

    def _is_active(self, client: _Client) -> bool:
        return client.remote_id == self._active_id

    # ------------------------------------------------------------------ 绑定管理（设置页调用）

    def create_binding(self, name: str = "新遥控器") -> dict:
        remote = add_remote(self._config, name)
        self._save()
        return remote

    def delete_binding(self, remote_id: str) -> bool:
        """删除绑定：先断开该遥控器连接（如果有），再从配置移除。"""
        client = self._find_client(remote_id)
        if client is not None and self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(client.ws.close(), self._loop)
        removed = remove_remote(self._config, remote_id)
        if removed is None:
            return False
        with self._lock:
            if self._active_id == remote_id:
                self._active_id = None
        self._save()
        logger.info("binding removed: %s", removed.get("name"))
        return True

    def active_remote_names(self) -> list[tuple[str, str, bool]]:
        return [(c.remote_id, c.name, c.remote_id == self._active_id) for c in self._clients]

    # ------------------------------------------------------------------ 分发

    async def _on_control(self, client: _Client, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype in ("ping",):
            return
        if mtype == "hello":
            logger.info("client hello: %s", msg.get("device"))
            return
        if not self._is_active(client):
            logger.info("ignored %s from standby remote %s", mtype, client.name)
            return
        try:
            if mtype == "key":
                self._actioner.handle_key(msg.get("code", ""), msg.get("action", "down"))
            elif mtype == "send":
                self._actioner.handle_send()
            elif mtype == "voice":
                self._actioner.handle_voice(msg.get("action", ""))
            elif mtype == "voice_toggle":
                self._actioner.handle_voice_toggle(msg.get("action", "toggle"))
            elif mtype == "launch":
                self._actioner.handle_launch(str(msg.get("slot", "")))
            elif mtype == "switcher":
                self._actioner.handle_switcher(msg.get("action", ""))
            elif mtype == "select":
                self._actioner.handle_select(msg.get("action", ""))
            elif mtype == "mic":
                self._audio.set_active(msg.get("action") == "start")
            elif mtype == "switch_active":
                self.switch_active(str(msg.get("id", "")))
            else:
                logger.warning("unknown message type: %s", mtype)
        except Exception as exc:  # noqa: BLE001
            logger.error("dispatch %s failed: %s", mtype, exc)

    # ------------------------------------------------------------------ 连接

    async def _handler(self, websocket: ServerConnection) -> None:
        client: Optional[_Client] = None
        logger.info("connection open: %s", websocket.remote_address)
        try:
            try:
                async for raw in websocket:
                    if isinstance(raw, bytes):
                        if client is not None and self._is_active(client):
                            self._audio_frames += 1
                            self._audio_bytes += len(raw)
                            if self._audio_frames % 50 == 1:
                                logger.info("audio frames=%d bytes=%d", self._audio_frames, self._audio_bytes)
                            self._audio.feed(raw)
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning("invalid json frame")
                        continue
                    if client is None:
                        if msg.get("type") == "auth":
                            client = await self._authenticate(websocket, msg)
                            if client is None:
                                break
                        continue
                    if msg.get("type") == "ping":
                        await websocket.send(json.dumps({"type": "pong", "ts": msg.get("ts")}))
                        continue
                    await self._on_control(client, msg)
            except Exception as exc:  # noqa: BLE001 - 客户端异常断开是常态，别刷堆栈
                logger.info("connection ended: %s", type(exc).__name__)
        finally:
            self._unregister(client)

    # ------------------------------------------------------------------ 生命周期

    def _refresh_audio(self) -> None:
        self._audio.stop()
        self._audio.start()

    def _request_settings(self) -> None:
        """托盘线程里只投递请求；真正的窗口在主线程打开（pywebview 要求）。"""
        logger.info("settings requested from tray")
        if self._settings_queue is not None:
            self._settings_queue.put(True)

    def _open_settings_blocking(self) -> None:
        logger.info("opening settings window on main thread")
        try:
            from .settings_web import open_settings_window

            open_settings_window(self)
            logger.info("settings window closed")
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("webview settings unavailable, falling back to tkinter: %s", exc, exc_info=True)
        from .settings_ui import open_settings_window as tk_open

        worker = threading.Thread(target=tk_open, args=(self,), daemon=True)
        worker.start()
        worker.join()

    def _run_server(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        host = self._config.get("host", "0.0.0.0")
        port = int(self._config.get("port", 8765))

        async def main():
            self._stop_event = asyncio.Event()
            async with serve(self._handler, host, port):
                logger.info("WebSocket listening on %s:%s", host, port)
                await self._stop_event.wait()

        self._loop.run_until_complete(main())
        self._loop.close()

    def start(self) -> None:
        self._audio.start()
        self._settings_queue = queue.Queue()
        self._stopping = threading.Event()
        self._tray = Tray(
            on_quit=self.stop,
            on_refresh_audio=self._refresh_audio,
            on_open_settings=self._request_settings,
            on_switch_remote=self.switch_active,
        )
        # 服务与托盘都在后台线程，主线程留给设置窗口（pywebview 只能跑主线程）
        threading.Thread(target=self._run_server, daemon=True).start()
        self._tray.run_detached()
        logger.info("service started; waiting for settings requests on main thread")
        while not self._stopping.is_set():
            try:
                self._settings_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._open_settings_blocking()
        logger.info("main loop exited")

    def stop(self) -> None:
        if self._stopping is not None:
            self._stopping.set()
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        self._actioner.reset_modifiers()
        self._audio.stop()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._tray is not None:
            self._tray.stop()
        logger.info("service stopped")


def _setup_logging() -> None:
    """日志写文件（pythonw 启动时没有控制台，不能依赖重定向）。"""
    import os
    import sys

    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "service.log")

    handlers: list[logging.Handler] = []
    try:
        from logging.handlers import RotatingFileHandler

        handlers.append(
            RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        )
    except Exception:  # noqa: BLE001
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def _port_in_use(port: int) -> bool:
    import socket

    sock = socket.socket()
    sock.settimeout(0.5)
    try:
        return sock.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        sock.close()


def _notify_already_running() -> None:
    """pythonw 没有控制台，用系统弹窗告知（避免重复启动堆进程）。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            0,
            "手机遥控麦服务已经在运行，无需重复启动。\n可从任务栏托盘图标右键打开设置。",
            "手机遥控麦",
            0x40,
        )
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    _setup_logging()
    config = load_config()
    ensure_basics(config)
    if _port_in_use(int(config.get("port", 8765))):
        logger.warning("port %s already in use, another instance is running", config.get("port", 8765))
        _notify_already_running()
        return
    service = RemoteMicService(config)
    try:
        service.start()
    except KeyboardInterrupt:
        service.stop()


if __name__ == "__main__":
    main()
