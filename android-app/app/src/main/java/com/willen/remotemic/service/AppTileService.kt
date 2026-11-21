package com.willen.remotemic.service

import android.app.PendingIntent
import android.content.Intent
import android.os.Build
import android.service.quicksettings.TileService
import com.willen.remotemic.MainActivity

/**
 * 下拉快捷开关（控制中心磁贴）：点一下直接把本应用拉到前台。
 *
 * 磁贴不承载业务状态（连接与否看应用内顶栏），所以不做开关态切换、不常驻；
 * 点完就收起面板并启动 Activity。
 */
class AppTileService : TileService() {

    override fun onClick() {
        super.onClick()

        val intent = Intent(this, MainActivity::class.java).apply {
            // NEW_TASK：应用在后台时复用已有任务栈，不新建实例
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            // Android 14 起禁止用 Intent 直接启动，必须走 PendingIntent
            val pi = PendingIntent.getActivity(
                this, 0, intent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            startActivityAndCollapse(pi)
        } else {
            @Suppress("DEPRECATION")
            startActivityAndCollapse(intent)
        }
    }
}
