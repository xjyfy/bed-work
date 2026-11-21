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
# transfer_id 会拼进 `.part` 文件名，必须和文件名一样清洗：
# 带 `..` / 分隔符的 tid 能让落盘路径逃出接收目录（覆盖甚至删除任意文件）。
_TID_OK = re.compile(r"[0-9A-Za-z_-]{1,64}")
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
    # Windows 把「首个点之前的整段」当设备名，nul.a.b 照样是 NUL 设备，
    # 所以不能只看 splitext 的 stem。
    if base.split(".", 1)[0].lower() in _RESERVED:
        base = "_" + base
    stem, ext = os.path.splitext(base)
    if len(stem) > 120:
        stem = stem[:120]
    if len(ext) > 20:
        ext = ext[:20]
    return stem + ext


def sanitize_transfer_id(tid: str) -> str:
    """客户端给的 transfer_id 只允许字母数字下划线连字符；不合格就服务端自生成。

    正常的手机端 tid 是 `u<hex>`、电脑端是 uuid hex，都落在白名单里，
    所以这条只拦手改包/异常客户端，不影响正常传输。
    """
    text = str(tid or "")
    return text if _TID_OK.fullmatch(text) else uuid.uuid4().hex[:12]


def place_unique(part: Path, folder: Path, name: str) -> Path:
    """把 `.part` 改名成正式名，同名自动加 ` (1)`，**绝不覆盖已有文件**。

    不能「先 exists() 挑名、再 replace()」：两步之间有竞态，并发同名传输会
    互相覆盖；而且 Windows 上 `Path.replace` 本身就会覆盖目标。
    `os.rename` 在目标已存在时抛 FileExistsError，据此逐个试候选名，
    每次尝试自身是原子的。（本项目只跑 Windows；POSIX 的 rename 会静默覆盖。）
    """
    stem, ext = os.path.splitext(name)
    candidates = [name]
    candidates.extend(f"{stem} ({i}){ext}" for i in range(1, 1000))
    candidates.append(f"{stem} ({uuid.uuid4().hex[:6]}){ext}")
    for cand in candidates:
        target = folder / cand
        try:
            os.rename(part, target)
            return target
        except FileExistsError:
            continue
    raise ValueError(f"接收目录里同名文件过多，无法保存 {name}")


class Receiver:
    """一次手机→电脑的接收会话：边写边统计，收完 rename，失败就删。"""

    def __init__(self, folder: Path, name: str, size: int, transfer_id: str = ""):
        self.transfer_id = sanitize_transfer_id(transfer_id)
        self.folder = folder
        self.size = int(size or 0)
        self.name = sanitize_name(name)
        self.received = 0
        self.started = time.monotonic()
        self.final_path: Path | None = None
        self.part_path = folder / f".part_{self.transfer_id}_{self.name}"
        self._fh = open(self.part_path, "wb")

    def write(self, data: bytes) -> None:
        nxt = self.received + len(data)
        if nxt > MAX_FILE_BYTES:
            raise ValueError("文件超过 2GB 上限")
        # 声明了大小就不许超：否则谎报一个小 size 也能一直写到 2GB 才失败
        if self.size and nxt > self.size:
            raise ValueError(f"收到的数据超过声明大小（{nxt} > {self.size} 字节）")
        self._fh.write(data)
        self.received = nxt

    def finish(self) -> Path:
        self._fh.close()
        if self.size and self.received != self.size:
            self.abort()
            raise ValueError(f"大小不符：收到 {self.received} / 应为 {self.size}")
        final = place_unique(self.part_path, self.folder, self.name)
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
