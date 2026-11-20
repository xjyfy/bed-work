"""手机 ↔ 电脑互传文件的落盘逻辑。

设计要点：
- 单个文件上限 2GB，一次最多 50 个（不限制会话总量）；
- 边收边写 `.part`，收完校验大小后 rename 成正式文件名，失败/取消**直接删掉** `.part`；
- 不做断点续传（按当前需求），所以 `.part` 只是半成品的临时占位，不留残余；
- 文件名统一清洗（防路径穿越、Windows 保留名），同名自动加 ` (1)`；
- 默认落到系统「下载」目录（用 KnownFolder 解析，兼容 OneDrive 重定向），可在设置页改。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import os
import re
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024   # 单文件 2GB
MAX_FILES_PER_BATCH = 50                  # 一次最多 50 个文件
CHUNK_SIZE = 256 * 1024                   # 每块 256KB
MAGIC = b"FCHN"                           # 文件块的二进制帧前缀，用来和音频帧区分

# FOLDERID_Downloads = {374DE290-123F-4565-9164-39C4925E467B}
_FOLDERID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _known_downloads() -> Path:
    """系统「下载」目录。OneDrive 会把 Downloads 重定向走，所以不能拼 %USERPROFILE%。"""
    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    try:
        guid = _GUID()
        ctypes.windll.ole32.CLSIDFromString(_FOLDERID_DOWNLOADS, ctypes.byref(guid))
        path_ptr = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(path_ptr)
        )
        if hr == 0 and path_ptr.value:
            return Path(path_ptr.value)
    except Exception as exc:  # noqa: BLE001
        logger.warning("known folder Downloads failed: %s", exc)
    return Path(os.path.expanduser("~")) / "Downloads"


def default_dir() -> Path:
    return _known_downloads()


def target_dir(config: dict) -> Path:
    """配置里的接收目录；空/无效就回落到系统下载目录（并确保存在）。"""
    raw = str((config or {}).get("download_dir") or "").strip()
    path = Path(raw) if raw else default_dir()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("download dir %s unusable (%s), fallback to default", path, exc)
        path = default_dir()
        path.mkdir(parents=True, exist_ok=True)
    return path


def sanitize_name(name: str, fallback_ext: str = "") -> str:
    """清洗文件名：去目录部分、替换非法字符、避开 Windows 保留名、限长。"""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = _BAD_CHARS.sub("_", base).strip(" .")
    if not base:
        base = "文件" + (fallback_ext if fallback_ext.startswith(".") else "")
    stem, ext = os.path.splitext(base)
    if stem.lower() in _RESERVED:
        stem = "_" + stem
    if len(stem) > 120:
        stem = stem[:120]
    if len(ext) > 20:
        ext = ext[:20]
    return stem + ext


def unique_path(folder: Path, name: str) -> Path:
    """同名不覆盖：`a.jpg` → `a (1).jpg`。"""
    candidate = folder / name
    if not candidate.exists():
        return candidate
    stem, ext = os.path.splitext(name)
    for i in range(1, 1000):
        candidate = folder / f"{stem} ({i}){ext}"
        if not candidate.exists():
            return candidate
    return folder / f"{stem} ({uuid.uuid4().hex[:6]}){ext}"


class Receiver:
    """一次手机→电脑的接收会话：边写边统计，收完 rename，失败就删。"""

    def __init__(self, folder: Path, name: str, size: int, transfer_id: str = ""):
        self.transfer_id = transfer_id or uuid.uuid4().hex[:12]
        self.folder = folder
        self.size = int(size or 0)
        self.name = sanitize_name(name)
        self.received = 0
        self.started = time.monotonic()
        self.final_path: Path | None = None
        self.part_path = folder / f".part_{self.transfer_id}_{self.name}"
        self._fh = open(self.part_path, "wb")

    def write(self, data: bytes) -> None:
        if self.received + len(data) > MAX_FILE_BYTES:
            raise ValueError("文件超过 2GB 上限")
        self._fh.write(data)
        self.received += len(data)

    def finish(self) -> Path:
        self._fh.close()
        if self.size and self.received != self.size:
            self.abort()
            raise ValueError(f"大小不符：收到 {self.received} / 应为 {self.size}")
        final = unique_path(self.folder, self.name)
        self.part_path.replace(final)
        self.final_path = final
        elapsed = max(0.001, time.monotonic() - self.started)
        logger.info(
            "file received: %s (%.1f MB, %.1f MB/s)",
            final.name, self.received / 1048576, self.received / 1048576 / elapsed,
        )
        return final

    def abort(self) -> None:
        """失败/取消：关掉句柄并把半成品删干净（用户要求不留 .part）。"""
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.part_path.exists():
                os.remove(self.part_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("remove part file failed: %s", exc)


def cleanup_old_parts(folder: Path, older_than_hours: int = 24) -> None:
    """服务启动时清一次历史残留（异常退出可能留下 .part_ 开头的文件）。"""
    limit = time.time() - older_than_hours * 3600
    try:
        for p in folder.glob(".part_*"):
            if p.is_file() and p.stat().st_mtime < limit:
                p.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cleanup old .part failed: %s", exc)


def describe_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def guess_mime(name: str) -> str:
    import mimetypes

    return mimetypes.guess_type(name)[0] or "application/octet-stream"
