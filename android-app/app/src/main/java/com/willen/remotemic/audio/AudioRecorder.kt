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
        val bufferSize = maxOf(frameBytes * 4, minBuf)

        val recorder = AudioRecord(
            MediaRecorder.AudioSource.MIC,
            sampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            bufferSize
        )
        if (recorder.state != AudioRecord.STATE_INITIALIZED) {
            Log.e(TAG, "AudioRecord init failed")
            recorder.release()
            return false
        }

        record = recorder
        recorder.startRecording()
        running.set(true)
        worker = Thread {
            val buf = ByteArray(frameBytes)
            while (running.get()) {
                val read = recorder.read(buf, 0, buf.size)
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
