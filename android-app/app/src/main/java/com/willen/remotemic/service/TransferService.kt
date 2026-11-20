package com.willen.remotemic.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import com.willen.remotemic.MainActivity
import com.willen.remotemic.R
import com.willen.remotemic.transfer.TransferBus
import com.willen.remotemic.transfer.TransferProgress

/**
 * 文件传输期间的**短时**前台服务：只为了在通知栏显示进度、并在切后台时不被杀。
 *
 * 和录音服务不同，这里不做常驻保活：传输一开始就起来，传完（或失败/取消）立刻 stopSelf，
 * 通知栏随之消失，符合"不做后台常驻"的一贯策略。
 */
class TransferService : Service() {

    private var listener: ((TransferProgress?) -> Unit)? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
        // 必须在 startForegroundService() 之后 5 秒内进入前台，且在 onCreate 里做最稳：
        // 若等到 onStartCommand、而传输又瞬间结束并 stopService，会直接抛
        // ForegroundServiceDidNotStartInTimeException 把整个进程干掉。
        startForeground(NOTIF_ID, buildNotification(TransferBus.current))
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIF_ID, buildNotification(TransferBus.current))
        if (listener == null) {
            val l: (TransferProgress?) -> Unit = { progress ->
                if (progress == null || progress.finished) {
                    stopSelf()
                } else {
                    notify(progress)
                }
            }
            listener = l
            TransferBus.observe(l)
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        listener?.let { TransferBus.forget(it) }
        listener = null
        super.onDestroy()
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val mgr = getSystemService(NotificationManager::class.java)
        if (mgr.getNotificationChannel(CHANNEL_ID) == null) {
            val channel = NotificationChannel(CHANNEL_ID, "文件传输", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "手机与电脑互传文件时的进度"
                setShowBadge(false)
            }
            mgr.createNotificationChannel(channel)
        }
    }

    private fun notify(progress: TransferProgress) {
        val mgr = getSystemService(NotificationManager::class.java)
        mgr.notify(NOTIF_ID, buildNotification(progress))
    }

    private fun buildNotification(progress: TransferProgress?): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP), PendingIntent.FLAG_IMMUTABLE
        )
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        val title = when {
            progress == null -> "文件传输"
            progress.sending -> "正在发送到电脑"
            else -> "正在接收电脑文件"
        }
        builder.setSmallIcon(R.drawable.ic_mic)
            .setContentTitle(title)
            .setContentIntent(open)
            .setCategory(Notification.CATEGORY_PROGRESS)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
        if (progress != null) {
            builder.setContentText("${progress.summary()} · ${progress.detail()}")
            builder.setProgress(100, progress.percent, progress.bytesTotal <= 0)
        }
        return builder.build()
    }

    companion object {
        private const val CHANNEL_ID = "remote_mic_transfer_v2"
        private const val NOTIF_ID = 4102

        fun start(ctx: Context) {
            val intent = Intent(ctx, TransferService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.startForegroundService(intent)
            } else {
                ctx.startService(intent)
            }
        }

        fun stop(ctx: Context) {
            ctx.stopService(Intent(ctx, TransferService::class.java))
        }
    }
}
