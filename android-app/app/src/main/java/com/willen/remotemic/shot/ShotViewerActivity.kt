package com.willen.remotemic.shot

import android.content.pm.ActivityInfo
import android.content.res.Configuration
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.willen.remotemic.R
import kotlin.math.abs

/**
 * 电脑端截图的全屏查看页。
 *
 * 交互：
 *  - 非图片区域半透明压暗，点空白处**不退出**（只有返回键/系统返回退出）
 *  - 双指缩放、单指拖动、双击复位
 *  - 手机横过来就整页转成横屏满屏显示：用加速度计自己判方向并
 *    setRequestedOrientation，**不依赖系统"自动旋转"开关**
 *  - 图片只在内存里（ShotHolder），退出即 recycle，本地不落任何文件
 */
class ShotViewerActivity : AppCompatActivity(), SensorEventListener {

    private lateinit var zoomView: ZoomImageView
    private var sensorManager: SensorManager? = null
    private var appliedOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
    private var lastChangeAt = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_shot)

        zoomView = findViewById(R.id.zoomView)
        val bitmap = ShotHolder.get()
        if (bitmap == null || bitmap.isRecycled) {
            Toast.makeText(this, "截图已释放，请重新截屏", Toast.LENGTH_SHORT).show()
            finish()
            return
        }
        zoomView.setBitmap(bitmap)

        sensorManager = getSystemService(SensorManager::class.java)
    }

    // ---------------------------------------------------------- 方向（自判横屏）

    override fun onResume() {
        super.onResume()
        val sensor = sensorManager?.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
        if (sensor != null) {
            sensorManager?.registerListener(this, sensor, SensorManager.SENSOR_DELAY_UI)
        }
    }

    override fun onPause() {
        sensorManager?.unregisterListener(this)
        super.onPause()
    }

    override fun onSensorChanged(event: SensorEvent) {
        val x = event.values[0]      // 屏幕右侧朝下时为正
        val y = event.values[1]      // 竖直握持时为正
        // 手机平放 / 轻微晃动时不动，避免来回翻
        if (abs(x) < 5f && abs(y) < 5f) return

        val target = when {
            abs(x) > abs(y) * 1.35f ->
                if (x > 0) ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE
                else ActivityInfo.SCREEN_ORIENTATION_REVERSE_LANDSCAPE
            abs(y) > abs(x) * 1.35f ->
                if (y > 0) ActivityInfo.SCREEN_ORIENTATION_PORTRAIT
                else ActivityInfo.SCREEN_ORIENTATION_REVERSE_PORTRAIT
            else -> return
        }
        // 抖动去重：500ms 内不连续切换
        val now = System.currentTimeMillis()
        if (target == appliedOrientation || now - lastChangeAt < 500) return
        lastChangeAt = now
        appliedOrientation = target
        requestedOrientation = target
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) = Unit

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        // manifest 里声明了 configChanges，转屏不重建；这里只需要让图片重新铺满
        zoomView.post { zoomView.resetZoom() }
    }

    // ---------------------------------------------------------- 销毁

    override fun onDestroy() {
        sensorManager?.unregisterListener(this)
        if (isFinishing) {
            // 退出即销毁图片：本地不留存（本来也没写过文件）
            zoomView.setBitmap(null)
            ShotHolder.clear()
        }
        super.onDestroy()
    }
}
