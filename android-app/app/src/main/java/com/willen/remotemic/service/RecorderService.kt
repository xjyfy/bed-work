package com.willen.remotemic.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import com.willen.remotemic.R
import com.willen.remotemic.audio.AudioBus
import com.willen.remotemic.audio.AudioRecorder

/**
 * 录音前台服务：仅在语音开启期间运行并显示通知（Android 录音强制要求）。
 * 不做常驻强保活；App 被杀后下次启动重新连接即可。
 */
class RecorderService : Service() {

    private var recorder: AudioRecorder? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> startRecording()
            ACTION_STOP -> stopRecording()
        }
        return START_NOT_STICKY
    }

    private fun startRecording() {
        ensureChannel()
        val notification: Notification = Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("手机遥控麦")
            .setContentText("正在录音…")
            .setSmallIcon(R.drawable.ic_mic)
            .build()
        startForeground(NOTIF_ID, notification)

        if (recorder == null) {
            recorder = AudioRecorder { pcm -> AudioBus.onPcm?.invoke(pcm) }
        }
        recorder?.start()
    }

    private fun stopRecording() {
        recorder?.stop()
        recorder = null
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        recorder?.stop()
        recorder = null
        super.onDestroy()
    }

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val mgr = getSystemService(NotificationManager::class.java)
            if (mgr.getNotificationChannel(CHANNEL_ID) == null) {
                val channel = NotificationChannel(
                    CHANNEL_ID, "录音服务", NotificationManager.IMPORTANCE_LOW
                )
                mgr.createNotificationChannel(channel)
            }
        }
    }

    companion object {
        const val ACTION_START = "com.willen.remotemic.START_RECORD"
        const val ACTION_STOP = "com.willen.remotemic.STOP_RECORD"
        private const val CHANNEL_ID = "recorder_channel"
        private const val NOTIF_ID = 1001
    }
}
