"""电脑端截屏：整屏抓图 → JPEG（给手机端"截图"按钮用）。

不落盘：全程在内存里转成 JPEG 字节，随后 base64 走 WebSocket 回传；
手机端也只是内存里的 Bitmap，退出即销毁。
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

# 长边缩到 1600：1080p/2K 屏在手机上看已经足够清楚，体积能压到 200~400 KB；
# 不缩的话 4K 多屏会到好几 MB，base64 之后更大，手机解码也慢。
MAX_LONG_EDGE = 1600
JPEG_QUALITY = 72
# 编码后大小上限（字节），超了就降质重编码一次，再超就直接报错
MAX_BYTES = 3 * 1024 * 1024


def capture_jpeg(max_long_edge: int = MAX_LONG_EDGE, quality: int = JPEG_QUALITY) -> tuple[bytes, int, int]:
    """抓全屏（含多显示器）并编码成 JPEG，返回 (字节, 编码后宽, 编码后高)。"""
    from PIL import ImageGrab

    img = ImageGrab.grab(all_screens=True)
    width, height = img.size

    long_edge = max(width, height)
    if long_edge > max_long_edge:
        scale = max_long_edge / long_edge
        img = img.resize((max(1, int(width * scale)), max(1, int(height * scale))), 2)  # 2 = BILINEAR，快且够用

    if img.mode != "RGB":
        img = img.convert("RGB")

    data = _encode(img, quality)
    if len(data) > MAX_BYTES:
        logger.warning("screenshot %d bytes too big, re-encode at quality 50", len(data))
        data = _encode(img, 50)
    if len(data) > MAX_BYTES:
        raise RuntimeError(f"screenshot too large: {len(data)} bytes")

    return data, img.width, img.height


def _encode(img, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()
