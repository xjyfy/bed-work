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
        # 是否真正打开了虚拟声卡输出流；False 时语音应整体禁用（不得回落到物理输出设备）
        self._ready = False
        self._matched_name = ""
        self._matched_hostapi = ""

        # 环形缓冲：约 1 秒容量的 int16 样本
        capacity = self._sample_rate
        self._buf = np.zeros(capacity, dtype=np.int16)
        self._write = 0
        self._read = 0
        self._filled = 0

        # 观测计数（B2）：status 里能看到"卡不卡"，不必翻日志
        self._frames_in = 0          # 从网络收进来的样本数
        self._frames_out = 0         # 送往声卡的样本数
        self._overflows = 0          # 缓冲溢出次数（丢的是最旧的帧）
        self._underruns = 0          # 欠载次数（说话时缓冲见底，补了静音）
        self._dropped_samples = 0    # 因溢出被丢掉的样本数

    # ------------------------------------------------------------------ 设备

    @property
    def ready(self) -> bool:
        """虚拟声卡是否就绪（供 status 帧 / 托盘显示用）。"""
        return self._ready

    @property
    def matched_device(self) -> str:
        return f"{self._matched_name} [{self._matched_hostapi}]" if self._matched_name else ""

    def stats(self) -> dict:
        """缓冲水位与丢帧计数，供 status 帧与设置页显示（B2）。"""
        with self._lock:
            capacity = len(self._buf)
            level_ms = int(self._filled * 1000 / self._sample_rate) if capacity else 0
            return {
                "ready": self._ready,
                "device": self._matched_name,
                "hostapi": self._matched_hostapi,
                "level_ms": level_ms,          # 当前缓冲水位（毫秒）
                "overflows": self._overflows,
                "underruns": self._underruns,
                "dropped_samples": self._dropped_samples,
                "frames_in": self._frames_in,
                "frames_out": self._frames_out,
            }

    def reset_stats(self) -> None:
        with self._lock:
            self._frames_in = 0
            self._frames_out = 0
            self._overflows = 0
            self._underruns = 0
            self._dropped_samples = 0

    @staticmethod
    def _hostapi_name(dev: dict) -> str:
        try:
            return str(sd.query_hostapis(int(dev["hostapi"]))["name"])
        except Exception:  # noqa: BLE001
            return ""

    def _output_candidates(self) -> list[tuple[int, dict, str]]:
        """按关键字列出候选播放端，WASAPI 优先。

        注意：WASAPI 走共享模式的混音格式，**不做重采样**，VB-CABLE 默认 48k 时打不开
        16k 流（实测 `Invalid sample rate / PaErrorCode -9997`）；MME / DirectSound 会
        重采样，所以候选必须按顺序逐个试开，不能只取第一个。
        """
        try:
            matching = [
                (idx, dict(dev), self._hostapi_name(dev))
                for idx, dev in enumerate(sd.query_devices())
                if dev["max_output_channels"] > 0
                and self._device_keyword.lower() in str(dev["name"]).lower()
            ]
        except Exception as exc:  # noqa: BLE001
            logger.error("query audio devices failed: %s", exc)
            return []

        if not matching:
            logger.error(
                "no output device matched keyword '%s'; voice is disabled "
                "(install VB-CABLE or fix audio_device_keyword)",
                self._device_keyword,
            )
            return []

        def rank(item: tuple[int, dict, str]) -> int:
            api = item[2].lower()
            if "wasapi" in api:
                return 0  # 延迟最低，但可能不支持 16k
            if "directsound" in api:
                return 1  # 会重采样，兼容性最好
            return 2

        return sorted(matching, key=rank)

    def start(self) -> None:
        with self._lock:
            if self._stream is not None or self._ready:
                return
            candidates = self._output_candidates()
            if not candidates:
                self._ready = False
                return
            blocksize = int(self._sample_rate * self._frame_ms / 1000)
            for idx, dev, api in candidates:
                try:
                    stream = sd.OutputStream(
                        samplerate=self._sample_rate,
                        blocksize=blocksize,
                        channels=self._channels,
                        dtype="int16",
                        device=idx,
                        callback=self._callback,
                    )
                    stream.start()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("device %s (%s, idx=%s) unusable: %s", dev["name"], api, idx, exc)
                    continue
                self._stream = stream
                self._ready = True
                self._matched_name = str(dev["name"])
                self._matched_hostapi = api
                logger.info(
                    "audio output started (sr=%s, block=%s) -> %s (%s, idx=%s)",
                    self._sample_rate, blocksize, dev["name"], api, idx,
                )
                return
            self._ready = False
            logger.error(
                "all %d candidate device(s) refused %s Hz; voice is disabled",
                len(candidates), self._sample_rate,
            )

    def stop(self) -> None:
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("close stream failed: %s", exc)
                self._stream = None
            self._ready = False
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
        # 声卡未就绪时直接丢帧：宁可没有声音，也不能把语音从物理扬声器放出来
        if not self._active or not self._ready or not pcm:
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
                self._dropped_samples += drop
                self._overflows += 1
            first = min(n, cap - self._write)
            self._buf[self._write:self._write + first] = samples[:first]
            rest = n - first
            if rest > 0:
                self._buf[:rest] = samples[first:]
            self._write = (self._write + n) % cap
            self._filled += n
            self._frames_in += n

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
            elif self._active:
                # 说话中缓冲见底：这帧会补静音，记一次欠载（空闲时不算）
                self._underruns += 1
            outdata[:, 0] = chunk
            self._frames_out += frames
