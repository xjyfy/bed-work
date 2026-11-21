package com.willen.remotemic.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.util.Log
import android.widget.Toast
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
        // start() 会因权限被拒 / 麦克风被占用而失败（返回 false 或抛异常）。
        // 失败就必须把前台服务停掉：否则通知栏一直写着"正在录音"，实际一个字节都没采到。
        val started = runCatching { recorder?.start() == true }.getOrDefault(false)
        if (!started) {
            Log.e(TAG, "recorder start failed, stop service")
            runCatching { recorder?.stop() }
            recorder = null
            Toast.makeText(this, "麦克风启动失败：请检查录音权限，或是否被其他应用占用", Toast.LENGTH_LONG).show()
            // 通知界面复位语音按钮/波形，不然会一直显示"说话中"
            sendBroadcast(Intent(ACTION_START_FAILED).setPackage(packageName))
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
        }
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
        /** 麦克风起不来时发给界面（只在本应用内），让语音按钮/波形立刻复位。 */
        const val ACTION_START_FAILED = "com.willen.remotemic.RECORD_START_FAILED"
        private const val TAG = "RecorderService"
        private const val CHANNEL_ID = "recorder_channel"
        private const val NOTIF_ID = 1001
    }
}
