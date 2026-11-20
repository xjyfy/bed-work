"""音频输出：把手机推来的 PCM 帧写入虚拟声卡（CABLE Input）。

- 固定格式：16 kHz / 16-bit / 单声道 int16（与手机端、config.json 对齐）。
- 环形缓冲隔离网络抖动；溢出丢旧帧，欠载补静音。
- 语音关闭时清空缓冲并输出静音，避免上一段音频残留。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


class AudioOut:
    def __init__(self, config: dict):
        self._sample_rate = int(config.get("sample_rate", 16000))
        self._channels = int(config.get("channels", 1))
        self._sample_width = int(config.get("sample_width_bytes", 2))
        self._frame_ms = int(config.get("frame_ms", 20))
        self._device_keyword = config.get("audio_device_keyword", "CABLE Input")

        self._lock = threading.RLock()
        self._active = False
        self._stream: Optional[sd.OutputStream] = None

        # 环形缓冲：约 1 秒容量的 int16 样本
        capacity = self._sample_rate
        self._buf = np.zeros(capacity, dtype=np.int16)
        self._write = 0
        self._read = 0
        self._filled = 0

    # ------------------------------------------------------------------ 设备

    def find_output_device(self) -> Optional[int]:
        try:
            devices = sd.query_devices()
            for idx, dev in enumerate(devices):
                if dev["max_output_channels"] > 0 and self._device_keyword.lower() in dev["name"].lower():
                    logger.info("matched audio device: %s (idx=%s)", dev["name"], idx)
                    return idx
            logger.warning("no output device matched keyword '%s'; will use default", self._device_keyword)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error("query audio devices failed: %s", exc)
            return None

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            device = self.find_output_device()
            blocksize = int(self._sample_rate * self._frame_ms / 1000)
            try:
                self._stream = sd.OutputStream(
                    samplerate=self._sample_rate,
                    blocksize=blocksize,
                    channels=self._channels,
                    dtype="int16",
                    device=device,
                    callback=self._callback,
                )
                self._stream.start()
                logger.info("audio output started (sr=%s, block=%s)", self._sample_rate, blocksize)
            except Exception as exc:  # noqa: BLE001
                self._stream = None
                logger.error("open audio stream failed: %s", exc)

    def stop(self) -> None:
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("close stream failed: %s", exc)
                self._stream = None
            self._clear()

    # ------------------------------------------------------------------ 缓冲

    def _clear(self) -> None:
        self._buf.fill(0)
        self._write = 0
        self._read = 0
        self._filled = 0

    def set_active(self, active: bool) -> None:
        """语音 on/off 回调：关闭时清空残留缓冲。"""
        with self._lock:
            self._active = active
            if not active:
                self._clear()

    def feed(self, pcm: bytes) -> None:
        if not self._active or not pcm:
            return
        samples = np.frombuffer(pcm, dtype=np.int16)
        with self._lock:
            cap = len(self._buf)
            n = len(samples)
            if n >= cap:
                # 整段过长，只保留尾部
                samples = samples[-(cap - 1):]
                n = len(samples)
            if self._filled + n > cap:
                # 溢出丢旧帧：前移读指针
                drop = self._filled + n - cap
                self._read = (self._read + drop) % cap
                self._filled -= drop
            first = min(n, cap - self._write)
            self._buf[self._write:self._write + first] = samples[:first]
            rest = n - first
            if rest > 0:
                self._buf[:rest] = samples[first:]
            self._write = (self._write + n) % cap
            self._filled += n

    def _callback(self, outdata, frames, _time, _status) -> None:
        with self._lock:
            cap = len(self._buf)
            n = min(frames, self._filled)
            chunk = np.zeros(frames, dtype=np.int16)
            if n > 0:
                first = min(n, cap - self._read)
                chunk[:first] = self._buf[self._read:self._read + first]
                rest = n - first
                if rest > 0:
                    chunk[first:first + rest] = self._buf[:rest]
                self._read = (self._read + n) % cap
                self._filled -= n
            outdata[:, 0] = chunk
