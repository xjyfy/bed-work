package com.willen.remotemic.audio

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 麦克风采集：16 kHz / 16-bit / 单声道，20ms 一帧（320 采样 = 640 字节）。
 * 与电脑端 config.json、协议音频帧格式严格对齐。
 */
class AudioRecorder(private val onPcm: (ByteArray) -> Unit) {

    private val running = AtomicBoolean(false)
    private var record: AudioRecord? = null
    private var worker: Thread? = null

    fun start(): Boolean {
        if (running.get()) return true
        val sampleRate = SAMPLE_RATE
        val frameBytes = FRAME_SAMPLES * 2
        val minBuf = AudioRecord.getMinBufferSize(
            sampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )
        // 设备不支持这组参数时返回 ERROR/ERROR_BAD_VALUE，拿它当缓冲大小会构造失败
        if (minBuf <= 0) {
            Log.e(TAG, "getMinBufferSize failed: $minBuf")
            return false
        }
        val bufferSize = maxOf(frameBytes * 4, minBuf)

        // 权限被拒 / 麦克风被别的应用独占时，构造与 startRecording 都会直接抛异常，
        // 不接住就会把录音前台服务（乃至整个进程）崩掉。
        val recorder = try {
            AudioRecord(
                MediaRecorder.AudioSource.MIC,
                sampleRate,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                bufferSize
            )
        } catch (t: Throwable) {
            Log.e(TAG, "AudioRecord create failed", t)
            return false
        }
        if (recorder.state != AudioRecord.STATE_INITIALIZED) {
            Log.e(TAG, "AudioRecord init failed")
            runCatching { recorder.release() }
            return false
        }

        record = recorder
        try {
            recorder.startRecording()
        } catch (t: Throwable) {
            Log.e(TAG, "startRecording failed", t)
            record = null
            runCatching { recorder.release() }
            return false
        }
        if (recorder.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
            Log.e(TAG, "not recording after startRecording: ${recorder.recordingState}")
            record = null
            runCatching { recorder.release() }
            return false
        }
        running.set(true)
        worker = Thread {
            val buf = ByteArray(frameBytes)
            while (running.get()) {
                // 麦克风被抢走 / 已 release 时 read 会抛或返回负值，退出线程而不是崩
                val read = runCatching { recorder.read(buf, 0, buf.size) }.getOrDefault(-1)
                if (read < 0) {
                    Log.w(TAG, "read failed: $read, stop worker")
                    break
                }
                if (read > 0) {
                    onPcm(if (read == buf.size) buf else buf.copyOf(read))
                }
            }
        }.also { it.start() }
        Log.i(TAG, "recorder started")
        return true
    }

    fun stop() {
        running.set(false)
        worker?.join(300)
        worker = null
        record?.let {
            try { it.stop() } catch (_: Exception) {}
            it.release()
        }
        record = null
        Log.i(TAG, "recorder stopped")
    }

    companion object {
        private const val TAG = "AudioRecorder"
        const val SAMPLE_RATE = 16000
        const val FRAME_SAMPLES = 320 // 20ms
    }
}
