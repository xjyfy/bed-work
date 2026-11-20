"""配置读写与绑定数据：remotes / 应用栏位 / 每栏位发送快捷键。

配置默认落在 `%LOCALAPPDATA%\\RemoteMic\\config.json`（打包后程序目录只读），
开发期首次运行会把仓库里的 `pc-service/config.json` 自动迁移过去。
所有写入都走原子替换（先写临时文件再 os.replace），
避免托盘、设置页、服务端三方并发时写出半个文件。
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import paths

CONFIG_PATH = paths.config_path()


# ------------------------------------------------------------------ 读写

def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.loads(f.read())


def save_config(config: dict, path: Path = CONFIG_PATH) -> None:
    text = json.dumps(config, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def default_config() -> dict:
    return {
        "host": "0.0.0.0",
        "port": 8765,
        "computer_name": computer_name(),
        "sample_rate": 16000,
        "channels": 1,
        "sample_width_bytes": 2,
        "frame_ms": 20,
        "audio_device_keyword": "CABLE Input",
        "voice_hotkey": ["ctrl", "win"],
        "send_hotkey": {"default": ["enter"]},
        "slot_send_hotkey": {},
        "remotes": [],
        "apps": {},
        # 手机传来的文件默认存这里；留空 = 系统「下载」目录
        "download_dir": "",
    }


# ------------------------------------------------------------------ 基础信息

def computer_name() -> str:
    return socket.gethostname()


def lan_ip() -> str:
    """取本机在局域网中的 IPv4；取不到时回落到 127.0.0.1。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


# ------------------------------------------------------------------ 遥控器绑定

def ensure_basics(config: dict) -> dict:
    config.setdefault("computer_name", computer_name())
    config.setdefault("remotes", [])
    config.setdefault("slot_send_hotkey", {})
    config.setdefault("send_hotkey", {"default": ["enter"]})
    config.setdefault("download_dir", "")
    # 老配置里没有这个字段的遥控器，默认允许接收文件
    for remote in config.get("remotes", []):
        remote.setdefault("receive_files", True)
    return config


def new_remote(name: str = "新遥控器") -> dict:
    return {
        "id": "r_" + secrets.token_hex(4),
        "name": name,
        "token": secrets.token_urlsafe(18),
        "added": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "last_ip": "",
        "last_seen": "",
        # 是否允许这台遥控器往电脑传文件（默认开，可在设置页设备列表里单独关）
        "receive_files": True,
    }


def add_remote(config: dict, name: str = "新遥控器") -> dict:
    remote = new_remote(name)
    config.setdefault("remotes", []).append(remote)
    return remote


def find_remote_by_token(config: dict, token: str) -> Optional[dict]:
    if not token:
        return None
    for r in config.get("remotes", []):
        if r.get("token") == token:
            return r
    return None


def remove_remote(config: dict, remote_id: str) -> Optional[dict]:
    remotes = config.get("remotes", [])
    for i, r in enumerate(remotes):
        if r.get("id") == remote_id:
            return remotes.pop(i)
    return None


def touch_remote(remote: dict, device_name: str = "", ip: str = "") -> None:
    remote["last_seen"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    if device_name:
        remote["name"] = device_name
    if ip:
        remote["last_ip"] = ip


# ------------------------------------------------------------------ 二维码

def qr_payload(config: dict, token: str) -> str:
    """扫码内容：绑定所需的全部信息，手机端解析后直接建连接。"""
    return json.dumps(
        {
            "t": "remote-mic",
            "name": config.get("computer_name", computer_name()),
            "host": lan_ip(),
            "port": int(config.get("port", 8765)),
            "token": token,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def make_qr_image(text: str) -> Any:
    """返回 PIL Image；调用方负责显示。qrcode 未安装时抛出 ImportError。"""
    import qrcode  # noqa: WPS433 - 延迟导入，服务不依赖它也能跑

    qr = qrcode.QRCode(border=2, box_size=8)
    qr.add_data(text)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")
