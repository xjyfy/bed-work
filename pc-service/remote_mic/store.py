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
    # 权限字段迁移：老配置只有 receive_files，没有截图权限的概念。
    # 为了升级后行为不变（原来能传文件、也能截屏），用 receive_files 作为两个子权限的默认值。
    for remote in config.get("remotes", []):
        remote.setdefault("receive_files", True)
        files_default = bool(remote.get("receive_files", True))
        remote.setdefault("perm_files", files_default)
        remote.setdefault("perm_shot", files_default)
        # 「所有」= 一键全开的总闸；勾上时两个子项置灰。单独关掉任一项后总闸自动落下来。
        remote.setdefault("perm_all", bool(remote.get("perm_files")) and bool(remote.get("perm_shot")))
    return config


def remote_perms(remote: dict) -> tuple[bool, bool]:
    """这台遥控器的有效权限 (可传文件, 可截屏)。「所有」勾上时两个都为真。"""
    if remote.get("perm_all"):
        return True, True
    return bool(remote.get("perm_files", True)), bool(remote.get("perm_shot", True))


def set_remote_perms(config: dict, remote_id: str, all_on: bool, files: bool, shot: bool) -> bool:
    """设置页勾选后落盘：总闸与两个子权限同时维护，保持 receive_files 兼容旧版。"""
    for r in config.get("remotes", []):
        if str(r.get("id")) != str(remote_id):
            continue
        r["perm_all"] = bool(all_on)
        if all_on:
            files = shot = True
        r["perm_files"] = bool(files)
        r["perm_shot"] = bool(shot)
        # 兼容：旧版电脑端/旧设置页只认 receive_files
        r["receive_files"] = bool(files) or bool(shot)
        return True
    return False


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
        # 权限三件套：总闸 + 传文件 + 截图（默认全开，与旧行为一致）
        "perm_all": True,
        "perm_files": True,
        "perm_shot": True,
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
