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
import base64
import json
import logging
import os
import queue
import threading
import uuid
from typing import Optional

from websockets.asyncio.server import ServerConnection, serve

from . import screencap
from . import applist
from . import files as filex
from . import driver
from . import filedialog
from . import paths
from .actioner import Actioner
from .audio_out import AudioOut
from .store import (
    CONFIG_PATH,
    add_remote,
    computer_name,
    default_config,
    ensure_basics,
    find_remote_by_token,
    load_config,
    remove_remote,
    save_config,
    touch_remote,
)
from .tray import Tray

logger = logging.getLogger("remote_mic")

# 远程粘贴文本上限（字符数）。手机端先拦一次，服务端再兜一次。
CLIP_MAX_CHARS = 100_000


class _Client:
    __slots__ = (
        "remote_id", "name", "ws", "ip", "seq",
        "allow_files", "rx", "rx_saved", "accept_events", "tx_lock",
    )

    def __init__(self, remote_id: str, name: str, ws: ServerConnection, ip: str, seq: int,
                 allow_files: bool = True):
        self.remote_id = remote_id
        self.name = name
        self.ws = ws
        self.ip = ip
        self.seq = seq
        self.allow_files = allow_files
        self.rx: Optional[filex.Receiver] = None      # 手机→电脑的当前接收会话
        self.rx_saved: list[str] = []                  # 本轮已落盘的文件名
        self.accept_events: dict[str, asyncio.Event] = {}  # 电脑→手机：手机接受传输的信号
        self.tx_lock = asyncio.Lock()                  # 同一时刻只推一个文件，别把通道挤爆


class RemoteMicService:
    def __init__(self, config: dict):
        self._config = ensure_basics(config)
        self._audio = AudioOut(self._config)
        self._actioner = Actioner(
            self._config,
            on_voice_state_change=self._audio.set_active,
            on_notice=self._notify,
        )
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
        for key in ("apps", "send_hotkey", "slot_send_hotkey", "computer_name", "voice_hotkey", "remotes",
                    "download_dir"):
            if key in fresh:
                self._config[key] = fresh[key]
        logger.info("config reloaded from disk")
        # 文件接收开关可能被改了：同步到在线客户端并通知手机端按钮状态
        for c in self._clients:
            remote = next((r for r in self._config.get("remotes", []) if r.get("id") == c.remote_id), None)
            if remote is None:
                continue
            allow = bool(remote.get("receive_files", True))
            if allow != c.allow_files:
                c.allow_files = allow
                if self._loop is not None and self._loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._send_json(c, {"type": "files_allowed", "allow": allow}), self._loop
                    )
                logger.info("file receive switch for %s -> %s", c.name, allow)

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

    async def _close_quietly(self, ws: ServerConnection, timeout: float = 1.5) -> None:
        """带超时的关闭：死连接握手可能永远等不到回包，绝不能无限期阻塞。"""
        try:
            await asyncio.wait_for(ws.close(code=1000, reason="replaced"), timeout=timeout)
        except Exception:  # noqa: BLE001
            pass

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
            # 不能 await：对已失联的 socket，close() 要等完整的关闭握手（默认 10s），
            # 一旦卡在认证路径上就把整个事件循环堵住 —— 表现为 ping 不回、手机反复重连。
            asyncio.create_task(self._close_quietly(c.ws))

        self._seq += 1
        client = _Client(
            remote_id=remote_id,
            name=device or str(remote.get("name") or "遥控器"),
            ws=ws,
            ip=(ws.remote_address[0] if ws.remote_address else ""),
            seq=self._seq,
            allow_files=bool(remote.get("receive_files", True)),
        )
        client.rx_saved = []
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
                    # 手机端据此决定「传文件」按钮是否可用
                    "files_allowed": client.allow_files,
                },
                ensure_ascii=False,
            )
        )
        logger.info("remote bound: %s (%s) active=%s", client.name, client.ip, active)
        await self._broadcast_status()
        # 认证完就把四个快捷栏位的名称/图标发过去，手机端按钮不用等用户操作才有图标
        await self._send_json(client, self._apps_frame())
        self._refresh_icons_async()
        return client

    def _unregister(self, client: Optional[_Client]) -> None:
        if client is None:
            return
        if client.rx is not None:
            client.rx.abort()
            client.rx = None
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
            elif mtype == "clip":
                self._handle_clip(msg)
            elif mtype == "screenshot":
                await self._handle_screenshot(client)
            elif mtype == "mic":
                self._audio.set_active(msg.get("action") == "start")
            elif mtype == "file_begin":
                await self._handle_file_begin(client, msg)
            elif mtype == "file_end":
                await self._handle_file_end(client, msg)
            elif mtype == "file_abort":
                self._abort_receive(client, str(msg.get("reason") or "手机取消"))
            elif mtype == "file_accept":
                self._handle_accept(client, msg)
            elif mtype == "switch_active":
                self.switch_active(str(msg.get("id", "")))
            else:
                logger.warning("unknown message type: %s", mtype)
        except Exception as exc:  # noqa: BLE001
            logger.error("dispatch %s failed: %s", mtype, exc)

    # ------------------------------------------------------------------ 远程粘贴

    def _handle_clip(self, msg: dict) -> None:
        """手机剪贴板 → 电脑剪贴板。

        手机端已经做过一次类型/长度校验，这里再校验一遍（旧版 App、手改包、
        其他客户端都可能绕过），失败只在本机提示，不回给手机。
        """
        text = msg.get("text")
        if not isinstance(text, str) or not text:
            logger.warning("clip rejected: empty or non-string payload")
            return
        if len(text) > CLIP_MAX_CHARS:
            logger.warning("clip rejected: %d chars over limit %d", len(text), CLIP_MAX_CHARS)
            self._notify("远程粘贴失败", f"文本超长：{len(text)} 字符（上限 {CLIP_MAX_CHARS}）")
            return
        if not self._actioner.handle_clip(text):
            self._notify("远程粘贴失败", "写入电脑剪贴板失败，剪贴板可能被其他程序占用")

    def _notify(self, title: str, message: str) -> None:
        if self._tray is not None:
            self._tray.notify(message, title)

    # ------------------------------------------------------------------ 快捷应用

    def _apps_frame(self) -> dict:
        """四个快捷栏位的名称 + 图标（data URL），推给手机端给按钮上图标。

        图标只读本地缓存 PNG（`icon_data_url` 不触发导出），导出动作在服务启动
        的预热线程里做过；缓存缺失时 icon 为空，手机端就继续显示文字。
        """
        cfg = self._config
        apps = cfg.get("apps") or {}
        if not applist.peek_entries():
            # 缓存还没预热好：现在只能退化成 exe 文件名，先把枚举放到后台跑，
            # 预热完成后 _warm_app_icons 会再推一帧带正确名称/图标的
            applist.warm_cache_async()
        slots = []
        for slot in ("1", "2", "3", "4"):
            path = str(apps.get(slot) or "")
            name = ""
            icon = ""
            if path:
                info = applist.describe_app(path, allow_scan=False)
                name = info.get("name") or ""
                icon = applist.icon_data_url(path)
            slots.append({"slot": slot, "name": name, "icon": icon})
        return {"type": "apps", "slots": slots}

    def _slot_apps(self) -> list[str]:
        return [str(p) for p in (self._config.get("apps") or {}).values() if p]

    def _refresh_icons_async(self) -> None:
        """栏位里有应用还没有图标缓存时，后台导一遍再重推一帧。

        设置页刚换成一个从没导出过图标的应用时，第一次推的帧里 icon 是空的，
        手机就只剩文字；不补的话得重启服务才会好。
        """
        paths = self._slot_apps()
        if not paths or not applist.missing_icons(paths):
            return

        def run() -> None:
            try:
                applist.ensure_icons(paths)
                logger.info("slot icons exported, re-pushing apps frame")
                self.push_apps()
            except Exception as exc:  # noqa: BLE001
                logger.warning("export slot icons failed: %s", exc)

        import threading

        threading.Thread(target=run, daemon=True, name="app-slot-icons").start()

    def push_apps(self) -> None:
        """设置页改完栏位后调用：把新的名称/图标广播给已连的手机。"""
        if self._loop is None or not self._loop.is_running():
            return
        frame = self._apps_frame()
        for client in list(self._clients):
            asyncio.run_coroutine_threadsafe(self._send_json(client, frame), self._loop)
        logger.info("apps frame pushed to %d client(s)", len(self._clients))
        self._refresh_icons_async()

    def _warm_app_icons(self) -> None:
        """后台把配置里四个栏位的图标导出一遍（首次会调一次 PowerShell）。

        顺手把整个应用表缓存也预热掉并重推一次 apps 帧：手机如果在这之前就
        连上了，收到的名称会是 exe 文件名（Weixin / chrome），预热完成后再补一帧
        就变成「微信 / 豆包 / Google Chrome」。
        """
        try:
            paths = [str(p) for p in (self._config.get("apps") or {}).values() if p]
            applist.ensure_icons(paths)
            logger.info("app slot icons ready: %d", len(paths))
        except Exception as exc:  # noqa: BLE001
            logger.warning("warm app slot icons failed: %s", exc)

        try:
            applist.cached_entries()
            self.push_apps()
        except Exception as exc:  # noqa: BLE001
            logger.warning("push apps after warm failed: %s", exc)

    # ------------------------------------------------------------------ 截屏

    async def _handle_screenshot(self, client: _Client) -> None:
        """抓屏并把 JPEG（base64）回给手机端。

        抓图是阻塞 IO（几十到上百毫秒），放线程里跑，别卡住事件循环；
        图片只在内存中流转，不落盘。
        """
        try:
            data, width, height = await asyncio.to_thread(screencap.capture_jpeg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("screenshot failed: %s", exc)
            await self._send_json(client, {"type": "screenshot", "ok": False, "reason": str(exc)})
            return

        payload = base64.b64encode(data).decode("ascii")
        logger.info("screenshot sent: %dx%d, %d KB", width, height, len(data) // 1024)
        await self._send_json(
            client,
            {
                "type": "screenshot",
                "ok": True,
                "w": width,
                "h": height,
                "bytes": len(data),
                "image": payload,
            },
        )

    async def _send_json(self, client: _Client, payload: dict) -> None:
        try:
            await client.ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            logger.info("send failed (%s): %s", payload.get("type"), exc)

    # ------------------------------------------------------------------ 文件互传

    async def _handle_file_begin(self, client: _Client, msg: dict) -> None:
        """手机要传一个文件过来：先校验开关/数量/大小，再准备 `.part` 接收。"""
        if not client.allow_files:
            await self._send_json(client, {
                "type": "file_reject",
                "transfer_id": str(msg.get("transfer_id") or ""),
                "reason": "这台电脑已关闭该遥控器的文件接收",
            })
            logger.info("file rejected (disabled) from %s", client.name)
            return

        index = int(msg.get("index") or 1)
        count = int(msg.get("count") or 1)
        if count > filex.MAX_FILES_PER_BATCH:
            await self._send_json(client, {
                "type": "file_reject",
                "transfer_id": str(msg.get("transfer_id") or ""),
                "reason": f"一次最多传 {filex.MAX_FILES_PER_BATCH} 个文件",
            })
            return
        if index > filex.MAX_FILES_PER_BATCH:
            await self._send_json(client, {
                "type": "file_reject",
                "transfer_id": str(msg.get("transfer_id") or ""),
                "reason": f"超过单次 {filex.MAX_FILES_PER_BATCH} 个文件的上限",
            })
            return
        size = int(msg.get("size") or 0)
        if size > filex.MAX_FILE_BYTES:
            await self._send_json(client, {
                "type": "file_reject",
                "transfer_id": str(msg.get("transfer_id") or ""),
                "reason": f"单个文件上限 {filex.describe_size(filex.MAX_FILE_BYTES)}",
            })
            return

        self._abort_receive(client, "上一个传输未完成", notify=False)
        folder = filex.target_dir(self._config)
        try:
            client.rx = filex.Receiver(folder, str(msg.get("name") or ""), size,
                                       str(msg.get("transfer_id") or ""))
        except Exception as exc:  # noqa: BLE001
            logger.error("open receive file failed: %s", exc)
            await self._send_json(client, {
                "type": "file_reject",
                "transfer_id": str(msg.get("transfer_id") or ""),
                "reason": f"电脑端无法创建文件：{exc}",
            })
            return

        if index == 1:
            client.rx_saved = []
        logger.info(
            "file begin: %s (%s) %d/%d from %s",
            client.rx.name, filex.describe_size(size), index, count, client.name,
        )
        await self._send_json(client, {
            "type": "file_ack",
            "transfer_id": client.rx.transfer_id,
            "name": client.rx.name,
        })

    def _feed_file_chunk(self, client: _Client, data: bytes) -> None:
        """二进制块落到当前接收会话；写盘异常直接中止并告知手机。"""
        rx = client.rx
        if rx is None:
            return
        try:
            rx.write(data)
        except Exception as exc:  # noqa: BLE001
            logger.error("write file chunk failed: %s", exc)
            self._abort_receive(client, str(exc))
            self._notify("文件接收失败", str(exc))

    async def _handle_file_end(self, client: _Client, msg: dict) -> None:
        rx = client.rx
        if rx is None:
            return
        client.rx = None
        try:
            saved = rx.finish()
        except Exception as exc:  # noqa: BLE001
            logger.warning("file end failed: %s", exc)
            rx.abort()
            self._notify("文件接收失败", str(exc))
            await self._send_json(client, {
                "type": "file_result", "transfer_id": str(msg.get("transfer_id") or ""),
                "ok": False, "reason": str(exc),
            })
            return
        client.rx_saved.append(saved.name)
        self._notify("收到文件", f"{saved.name} → {saved.parent}")
        await self._send_json(client, {
            "type": "file_result",
            "transfer_id": str(msg.get("transfer_id") or ""),
            "ok": True,
            "name": saved.name,
            "dir": str(saved.parent),
            "count": len(client.rx_saved),
        })

    def _abort_receive(self, client: _Client, reason: str = "取消", notify: bool = True) -> None:
        rx = client.rx
        if rx is None:
            return
        client.rx = None
        name = rx.name
        rx.abort()
        logger.info("file aborted: %s (%s)", name, reason)
        if notify:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._send_json(client, {"type": "file_result", "transfer_id": rx.transfer_id,
                                             "ok": False, "reason": reason}),
                    self._loop,
                )
            except Exception:  # noqa: BLE001
                pass

    def _handle_accept(self, client: _Client, msg: dict) -> None:
        tid = str(msg.get("transfer_id") or "")
        ev = client.accept_events.get(tid)
        if ev is not None and not ev.is_set():
            ev.set()

    # 电脑→手机 --------------------------------------------------------

    def send_files_to_remote(self, paths: list[str], remote_id: str = "") -> dict:
        """设置页/托盘调用：把本地若干个文件推给手机（默认给控制中的那台）。"""
        paths = [str(p) for p in paths if p and os.path.exists(p)]
        if not paths:
            return {"ok": False, "reason": "没有可发送的文件"}
        if len(paths) > filex.MAX_FILES_PER_BATCH:
            return {"ok": False, "reason": f"一次最多 {filex.MAX_FILES_PER_BATCH} 个文件"}
        for p in paths:
            if os.path.getsize(p) > filex.MAX_FILE_BYTES:
                return {"ok": False, "reason": f"{os.path.basename(p)} 超过 2GB 上限"}
        if self._loop is None or not self._loop.is_running():
            return {"ok": False, "reason": "服务未就绪"}

        target = self._find_client(remote_id) if remote_id else self._find_client(self._active_id or "")
        if target is None:
            return {"ok": False, "reason": "没有在线的遥控器"}

        asyncio.run_coroutine_threadsafe(self._push_files(target, paths), self._loop)
        logger.info("push %d file(s) to %s: %s", len(paths), target.name, [os.path.basename(p) for p in paths])
        return {"ok": True, "count": len(paths), "remote": target.name}

    def send_files_from_tray(self) -> None:
        """托盘右键「发送文件到手机…」：原生多选，然后推给控制中的那台遥控器。"""
        picked = filedialog.pick_files(max_files=filex.MAX_FILES_PER_BATCH)
        if not picked:
            return
        result = self.send_files_to_remote(picked)
        if self._tray is None:
            return
        if result.get("ok"):
            self._tray.notify(f"开始发送 {result.get('count', 0)} 个文件到 {result.get('remote', '手机')}")
        else:
            self._tray.notify(f"发送失败：{result.get('reason', '未知原因')}")

    async def _push_files(self, client: _Client, paths: list[str]) -> None:
        total = len(paths)
        for idx, path in enumerate(paths, start=1):
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            tid = uuid.uuid4().hex[:12]
            ev = asyncio.Event()
            client.accept_events[tid] = ev
            await self._send_json(client, {
                "type": "file_offer",
                "transfer_id": tid,
                "name": os.path.basename(path),
                "size": size,
                "mime": filex.guess_mime(path),
                "index": idx,
                "count": total,
            })
            try:
                await asyncio.wait_for(ev.wait(), timeout=20)
            except asyncio.TimeoutError:
                logger.info("file offer timeout: %s", os.path.basename(path))
                client.accept_events.pop(tid, None)
                continue
            client.accept_events.pop(tid, None)
            async with client.tx_lock:
                try:
                    with open(path, "rb") as fh:
                        while True:
                            chunk = await asyncio.to_thread(fh.read, filex.CHUNK_SIZE)
                            if not chunk:
                                break
                            await client.ws.send(filex.MAGIC + chunk)
                    await self._send_json(client, {
                        "type": "file_end", "transfer_id": tid, "ok": True,
                        "name": os.path.basename(path),
                    })
                    logger.info("file pushed: %s (%.1f MB) -> %s", os.path.basename(path),
                                size / 1048576, client.name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("push file failed: %s", exc)
                    try:
                        await self._send_json(client, {"type": "file_end", "transfer_id": tid,
                                                       "ok": False, "reason": str(exc)})
                    except Exception:  # noqa: BLE001
                        pass

    # ------------------------------------------------------------------ 连接

    async def _handler(self, websocket: ServerConnection) -> None:
        client: Optional[_Client] = None
        logger.info("connection open: %s", websocket.remote_address)
        try:
            try:
                async for raw in websocket:
                    if isinstance(raw, bytes):
                        if raw[:4] == filex.MAGIC:
                            # 文件块，前缀 4 字节 MAGIC，剩余是数据（要和语音 PCM 区分）
                            if client is not None:
                                self._feed_file_chunk(client, raw[4:])
                            continue
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

    def refresh_audio(self) -> dict:
        """设置页「重新探测声卡」调用：重开输出流并返回当前状态。

        会重新查一遍设备清单（`sounddevice` 每次 start 都现查），所以插了新的
        虚拟声卡、改过 `audio_device_keyword` 之后点一下就生效，不用重启服务。
        注意：没有匹配到设备时语音会整体禁用，**不会回落到物理扬声器**。
        """
        self._audio.stop()
        self._audio.start()
        logger.info("audio re-probed: ready=%s device=%s", self._audio.ready, self._audio.matched_device)
        return self.audio_status()

    def audio_status(self) -> dict:
        st = driver.status()
        return {
            "ready": bool(self._audio.ready),
            "device": self._audio.matched_device,
            "keyword": str(self._config.get("audio_device_keyword", "CABLE Input")),
            # 虚拟声卡驱动状态：设置页据此决定是否显示「安装虚拟声卡」
            "driver_installed": st["installed"],
            "driver_can_install": st["can_install"],
        }

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
        # 清理上次异常退出留下的半截文件（.part_*），保持下载目录干净
        try:
            filex.cleanup_old_parts(filex.target_dir(self._config))
        except Exception as exc:  # noqa: BLE001
            logger.warning("cleanup old .part failed: %s", exc)
        # 后台预热应用列表缓存：设置页打开时要立刻渲染，不能现场枚举（会白屏 1~2 秒）
        applist.warm_cache_async()
        threading.Thread(target=self._warm_app_icons, daemon=True, name="slot-icons").start()
        self._settings_queue = queue.Queue()
        self._stopping = threading.Event()
        self._tray = Tray(
            on_quit=self.stop,
            on_open_settings=self._request_settings,
            on_switch_remote=self.switch_active,
            on_send_files=self.send_files_from_tray,
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
    """日志写文件（打包后没有控制台，不能依赖重定向）。

    路径统一走 `%LOCALAPPDATA%\\RemoteMic\\logs`，程序目录只读。
    """
    import sys

    log_dir = paths.log_dir()
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(str(log_dir), "service.log")

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


def _load_or_init_config() -> dict:
    """首次运行时自动生成 config.json；已存在则原样读取，**绝不覆盖**。

    之前缺 config.json 会直接抛 FileNotFoundError，双击启动只闪一下就退出，
    用户完全看不到原因，因此改为自愈。
    """
    if CONFIG_PATH.exists():
        return load_config()

    example = CONFIG_PATH.with_name("config.example.json")
    try:
        if example.exists():
            import shutil

            shutil.copyfile(example, CONFIG_PATH)
            logger.warning("config.json missing; created from config.example.json")
        else:
            save_config(default_config(), CONFIG_PATH)
            logger.warning("config.json missing; created with defaults")
    except Exception as exc:  # noqa: BLE001
        logger.error("create config.json failed: %s", exc)
        raise
    return load_config()


def main() -> None:
    _setup_logging()
    config = _load_or_init_config()
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
