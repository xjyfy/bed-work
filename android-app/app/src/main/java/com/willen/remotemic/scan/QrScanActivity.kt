package com.willen.remotemic.scan

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.VibrationEffect
import android.os.Vibrator
import android.util.Log
import android.view.MotionEvent
import android.view.View
import android.widget.Button
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.FocusMeteringAction
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer
import com.willen.remotemic.R
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.math.min

/**
 * 竖屏扫码页：CameraX 取流 + ZXing core 解码。
 *
 * 相比 journeyapps 的 CaptureActivity：
 *  - 竖屏、与主界面方向一致（原来那个是横屏全屏，切来切去很割裂）
 *  - 直接吃相机 YUV 的 Y 平面做灰度解码，不绕 Bitmap，单帧解码几十毫秒
 *  - 只解中心 74% 的取景框区域（ROI），比全帧快一个量级
 *  - KEEP_ONLY_LATEST + 帧丢弃，永远解最新画面，不堆积
 *  - 点屏幕任意位置可对焦，右上角可开补光灯
 */
class QrScanActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_CONTENT = "qr_content"
        private const val TAG = "QrScan"
    }

    private lateinit var previewView: PreviewView
    private lateinit var maskView: ScanMaskView
    private lateinit var btnTorch: Button
    private var camera: Camera? = null
    private var cameraProvider: ProcessCameraProvider? = null
    private var analysis: ImageAnalysis? = null

    private val decodeExecutor: ExecutorService = Executors.newSingleThreadExecutor()
    private val reader = MultiFormatReader()
    private val hints: Map<DecodeHintType, Any> = mapOf(
        DecodeHintType.POSSIBLE_FORMATS to listOf(BarcodeFormat.QR_CODE),
        DecodeHintType.TRY_HARDER to true,
        DecodeHintType.CHARACTER_SET to "UTF-8"
    )

    private var torchOn = false
    private var handled = false

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) startCamera() else {
            Toast.makeText(this, "没有相机权限，无法扫码", Toast.LENGTH_LONG).show()
            finish()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_qr_scan)

        previewView = findViewById(R.id.previewView)
        maskView = findViewById(R.id.maskView)
        btnTorch = findViewById(R.id.btnTorch)

        findViewById<Button>(R.id.btnBack).setOnClickListener { finish() }

        btnTorch.setOnClickListener {
            val c = camera ?: return@setOnClickListener
            torchOn = !torchOn
            c.cameraControl.enableTorch(torchOn)
            btnTorch.text = if (torchOn) "关灯" else "补光"
        }

        // 点画面任意位置对焦一次（3 秒后自动交还连续对焦）
        previewView.setOnTouchListener { _, event ->
            if (event.action == MotionEvent.ACTION_UP) {
                focusAt(event.x, event.y)
            }
            true
        }

        maskView.start()

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            == PackageManager.PERMISSION_GRANTED
        ) {
            startCamera()
        } else {
            permissionLauncher.launch(Manifest.permission.CAMERA)
        }
    }

    private fun focusAt(x: Float, y: Float) {
        val c = camera ?: return
        val point = previewView.meteringPointFactory.createPoint(x, y)
        val action = FocusMeteringAction.Builder(
            point,
            FocusMeteringAction.FLAG_AF or FocusMeteringAction.FLAG_AE
        ).setAutoCancelDuration(3, TimeUnit.SECONDS).build()
        c.cameraControl.startFocusAndMetering(action)
    }

    private fun startCamera() {
        val future = ProcessCameraProvider.getInstance(this)
        future.addListener({
            try {
                val provider = future.get()
                cameraProvider = provider

                val preview = Preview.Builder().build().also {
                    it.surfaceProvider = previewView.surfaceProvider
                }

                val analysisCase = ImageAnalysis.Builder()
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                    .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_YUV_420_888)
                    .build()
                analysisCase.setAnalyzer(decodeExecutor) { proxy -> analyze(proxy) }
                analysis = analysisCase

                provider.unbindAll()
                camera = provider.bindToLifecycle(
                    this,
                    CameraSelector.DEFAULT_BACK_CAMERA,
                    preview,
                    analysisCase
                )
            } catch (t: Throwable) {
                Log.w(TAG, "start camera failed", t)
                Toast.makeText(this, "相机启动失败：${t.message}", Toast.LENGTH_LONG).show()
                finish()
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun analyze(proxy: ImageProxy) {
        try {
            if (!handled) {
                val text = decode(proxy)
                if (!text.isNullOrBlank()) {
                    // 二维码里含绑定 token，绝不能进日志（logcat 可被任意 adb 读走）。
                    // 只记"解出来了、多长"，足够排查扫码问题。
                    Log.i(TAG, "qr decoded, len=${text.length}")
                    runOnUiThread { onFound(text) }
                }
            }
        } catch (t: Throwable) {
            Log.w(TAG, "decode error: ${t.message}")
        } finally {
            proxy.close()
        }
    }

    /**
     * 从 YUV_420_888 的 Y 平面直接取中心正方形做灰度解码，不转 Bitmap。
     * 相机帧常见是 1280x720，取 720x720 的中心区域，像素量降到 1/3。
     */
    private fun decode(proxy: ImageProxy): String? {
        val plane = proxy.planes.getOrNull(0) ?: return null
        if (plane.pixelStride != 1) return null
        val width = proxy.width
        val height = proxy.height
        val rowStride = plane.rowStride
        val buffer = plane.buffer

        var side = min(width, height)
        val left = (width - side) / 2
        val top = (height - side) / 2

        // 有些机型最后一行的 padding 会让容量刚好不够，按需收一点
        while (side > 0 && (top + side - 1) * rowStride + left + side > buffer.capacity()) {
            side -= 8
        }
        if (side < 200) return null

        val data = ByteArray(side * side)
        for (row in 0 until side) {
            buffer.position((top + row) * rowStride + left)
            buffer.get(data, row * side, side)
        }

        val source = PlanarYUVLuminanceSource(data, side, side, 0, 0, side, side, false)

        // 常规白底黑码
        decodeBitmap(HybridBinarizer(source))?.let { return it }
        // 反色二维码（黑底白码）少见，用同一次灰度数据再试一遍，成本很低
        return decodeInverted(source)
    }

    private fun decodeBitmap(binarizer: com.google.zxing.Binarizer): String? = try {
        reader.decode(BinaryBitmap(binarizer), hints).text
    } catch (t: Throwable) {
        null
    }

    private fun decodeInverted(source: PlanarYUVLuminanceSource): String? = try {
        reader.decode(BinaryBitmap(HybridBinarizer(source.invert())), hints).text
    } catch (t: Throwable) {
        null
    }

    private fun onFound(text: String) {
        if (handled) return
        handled = true
        analysis?.clearAnalyzer()
        if (torchOn) camera?.cameraControl?.enableTorch(false)
        vibrate()
        setResult(RESULT_OK, Intent().putExtra(EXTRA_CONTENT, text))
        finish()
    }

    private fun vibrate() {
        runCatching {
            val v = getSystemService(Vibrator::class.java) ?: return
            if (android.os.Build.VERSION.SDK_INT >= 26) {
                v.vibrate(VibrationEffect.createOneShot(30, VibrationEffect.DEFAULT_AMPLITUDE))
            } else {
                @Suppress("DEPRECATION")
                v.vibrate(30)
            }
        }
    }

    override fun onDestroy() {
        maskView.stop()
        analysis?.clearAnalyzer()
        cameraProvider?.unbindAll()
        decodeExecutor.shutdown()
        super.onDestroy()
    }
}
