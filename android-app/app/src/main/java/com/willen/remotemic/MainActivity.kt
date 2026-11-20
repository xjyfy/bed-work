package com.willen.remotemic

import android.Manifest
import android.animation.ObjectAnimator
import android.animation.ValueAnimator
import android.app.AlertDialog
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.Typeface
import android.graphics.drawable.ColorDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.view.Gravity
import android.view.HapticFeedbackConstants
import android.view.MotionEvent
import android.view.View
import android.view.animation.LinearInterpolator
import android.widget.Button
import android.widget.EditText
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.PopupWindow
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding
import com.google.android.material.bottomsheet.BottomSheetDialog
import com.willen.remotemic.audio.AudioBus
import com.willen.remotemic.databinding.ActivityMainBinding
import com.willen.remotemic.service.RecorderService
import com.willen.remotemic.service.TransferService
import com.willen.remotemic.transfer.FileTransferManager
import com.willen.remotemic.transfer.TransferBus
import com.willen.remotemic.transfer.TransferProgress
import com.willen.remotemic.ws.RemoteWsClient
import org.json.JSONArray
import org.json.JSONObject

/** 已绑定的电脑：token 来自扫码，之后每次连接都带它做认证。 */
data class Device(
    val name: String,
    val host: String,
    val port: Int,
    val token: String
) {
    val endpoint: String get() = "$host:$port"
}

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var client: RemoteWsClient
    private lateinit var screenReceiver: android.content.BroadcastReceiver
    private var firstResume = true

    private val devices = mutableListOf<Device>()
    private var current: Device? = null

    private var connected = false
    private var active = false
    private var voiceOn = false
    private var openMode = false

    private val uiHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private var pendingVoiceStop: Runnable? = null
    private var lastVoiceUpAt = 0L
    private var suppressStopOnce = false
    private val waveAnimators = mutableListOf<ObjectAnimator>()
    private var deleteRepeat: Runnable? = null

    private val prefs by lazy { getSharedPreferences("remote_mic", MODE_PRIVATE) }

    // ------------------------------------------------------------------ 文件互传
    private lateinit var fileTransfer: FileTransferManager
    private var transferDialog: AlertDialog? = null

    /**
     * 设备管理列表的重建函数，对话框显示期间有效。
     * 扫码新增 / 手动添加 / 连接状态变化后调用它，列表立刻更新，
     * 不用再"关掉设置再重新打开"才能看到新电脑。
     */
    private var deviceListRefresh: (() -> Unit)? = null
    private var transferProgressBar: ProgressBar? = null
    private var transferNameView: TextView? = null
    private var transferDetailView: TextView? = null
    private var transferTitleView: TextView? = null
    private var transferObserver: ((TransferProgress?) -> Unit)? = null

    /** 照片/视频：系统 Photo Picker（无需任何存储权限），最多 50 张 */
    private val pickMediaLauncher = registerForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode != RESULT_OK) return@registerForActivityResult
        val uris = collectUris(result.data) ?: return@registerForActivityResult
        if (uris.isEmpty()) return@registerForActivityResult
        startSendUris(uris)
    }

    /** 通用文件：SAF 的 ACTION_OPEN_DOCUMENT（同样无需存储权限），支持多选 */
    private val pickFileLauncher = registerForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode != RESULT_OK) return@registerForActivityResult
        val uris = collectUris(result.data) ?: return@registerForActivityResult
        if (uris.isEmpty()) return@registerForActivityResult
        startSendUris(uris)
    }

    private fun collectUris(data: Intent?): List<Uri>? {
        if (data == null) return null
        val clip = data.clipData
        if (clip != null && clip.itemCount > 0) {
            val list = mutableListOf<Uri>()
            for (i in 0 until clip.itemCount) list.add(clip.getItemAt(i).uri)
            return list
        }
        data.data?.let { return listOf(it) }
        return null
    }

    // 扫码绑定：扫到 PC 设置页的二维码后新增设备并立刻连接
    private val scanLauncher = registerForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode != RESULT_OK) return@registerForActivityResult
        val content = result.data?.getStringExtra(com.willen.remotemic.scan.QrScanActivity.EXTRA_CONTENT)
        if (content.isNullOrBlank()) return@registerForActivityResult
        val device = parseQr(content)
        if (device == null) {
            Toast.makeText(this, "二维码格式不对，请用电脑端设置页的绑定二维码", Toast.LENGTH_LONG).show()
            return@registerForActivityResult
        }
        devices.removeAll { it.token == device.token || it.endpoint == device.endpoint }
        devices.add(device)
        saveDevices()
        connectTo(device)
        // 扫码页返回时设备管理对话框还开着，这里立刻重建列表，否则要退出重进才看得到新电脑
        deviceListRefresh?.invoke()
        Toast.makeText(this, "已绑定 ${device.name}", Toast.LENGTH_SHORT).show()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        // Android 15 起默认边到边：自己按状态栏 / 导航栏 inset 留白，避免顶栏被状态栏盖住
        WindowCompat.setDecorFitsSystemWindows(window, false)
        ViewCompat.setOnApplyWindowInsetsListener(binding.root) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            v.updatePadding(
                left = dp(16) + bars.left,
                top = dp(10) + bars.top,
                right = dp(16) + bars.right,
                bottom = dp(12) + bars.bottom
            )
            insets
        }

        client = buildClient()

        // 传输线程里回调也走这里，Toast 必须切回主线程（否则直接崩：
        // "Can't toast on a thread that has not called Looper.prepare()"）
        fileTransfer = FileTransferManager(this, { client }) { title, message ->
            runOnUiThread {
                if (isFinishing || isDestroyed) return@runOnUiThread
                Toast.makeText(this, if (title.isBlank()) message else "$title：$message", Toast.LENGTH_SHORT).show()
            }
        }
        observeTransfer()

        AudioBus.onPcm = { pcm -> client.sendBinary(pcm) }

        loadDevices()
        ensurePermissions()
        bindButtons()
        applyAppSlotsFromPrefs()   // 先用上次缓存的图标/名称垫上，连上后再刷新
        updateVoiceButton()
        renderHeader()

        val saved = devices.firstOrNull { it.token == prefs.getString("current_token", "") }
            ?: devices.firstOrNull()
        if (saved != null) {
            connectTo(saved)
        } else {
            showBindingDialog()
        }
        registerScreenReceiver()
    }

    /**
     * 亮屏 / 解锁时立刻校验连接：熄屏后系统会挂起网络，socket 多半已死，
     * 但用户不希望在后台常驻（费电 + 通知栏），所以不做保活，改成「亮屏即重连」。
     */
    private fun registerScreenReceiver() {
        val filter = android.content.IntentFilter().apply {
            addAction(Intent.ACTION_SCREEN_ON)
            addAction(Intent.ACTION_USER_PRESENT)
        }
        screenReceiver = object : android.content.BroadcastReceiver() {
            override fun onReceive(context: android.content.Context?, intent: Intent?) {
                android.util.Log.i("MainActivity", "screen event: ${intent?.action} -> wakeUp")
                client.wakeUp()
            }
        }
        registerReceiver(screenReceiver, filter)
    }

    // ------------------------------------------------------------------ 权限

    private fun ensurePermissions() {
        val needed = mutableListOf<String>()
        if (ActivityCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            needed.add(Manifest.permission.RECORD_AUDIO)
        }
        if (ActivityCompat.checkSelfPermission(this, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            needed.add(Manifest.permission.CAMERA)
        }
        if (android.os.Build.VERSION.SDK_INT >= 33 &&
            ActivityCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            needed.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        if (needed.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, needed.toTypedArray(), REQ_PERM)
        }
    }

    // ------------------------------------------------------------------ 设备

    private fun loadDevices() {
        devices.clear()
        val raw = prefs.getString("devices", "") ?: ""
        if (raw.isNotBlank()) {
            runCatching {
                val arr = JSONArray(raw)
                for (i in 0 until arr.length()) {
                    val o = arr.getJSONObject(i)
                    devices.add(
                        Device(
                            name = o.optString("name", "电脑"),
                            host = o.optString("host", ""),
                            port = o.optInt("port", 8765),
                            token = o.optString("token", "")
                        )
                    )
                }
            }
        }
    }

    private fun saveDevices() {
        val arr = JSONArray()
        devices.forEach {
            arr.put(
                JSONObject()
                    .put("name", it.name)
                    .put("host", it.host)
                    .put("port", it.port)
                    .put("token", it.token)
            )
        }
        prefs.edit().putString("devices", arr.toString()).apply()
    }

    private fun connectTo(device: Device) {
        current = device
        prefs.edit().putString("current_token", device.token).apply()
        client.close()
        client = buildClient()
        client.connect(device.host, device.port, device.token, android.os.Build.MODEL)
        renderHeader()
    }

    private fun buildClient(): RemoteWsClient = RemoteWsClient(object : RemoteWsClient.Listener {
        override fun onReady(isActive: Boolean) {
            connected = true
            active = isActive
            resetVoiceState()
            sendHello()
            renderHeader()
        }

        override fun onDisconnected() {
            connected = false
            active = false
            resetVoiceState()
            renderHeader()
        }

        override fun onMessage(text: String) {
            val obj = runCatching { JSONObject(text) }.getOrNull() ?: return
            when (obj.optString("type")) {
                "status" -> {
                    active = obj.optBoolean("active", active)
                    val computer = obj.optString("computer", "")
                    if (computer.isNotBlank() && current != null) {
                        // 电脑真名只有连上之后才知道（手动输入 IP 添加时只能先记 IP）：
                        // 收到就回填进已绑定列表并落盘，设备管理/下拉列表以后都显示电脑名。
                        val idx = devices.indexOfFirst { it.token == current!!.token }
                        if (idx >= 0 && devices[idx].name != computer) {
                            devices[idx] = devices[idx].copy(name = computer)
                            saveDevices()
                        }
                        current = current!!.copy(name = computer)
                        renderHeader()
                    }
                }
                "auth_ok" -> fileTransfer.allowed = obj.optBoolean("files_allowed", true)
                "files_allowed" -> {
                    fileTransfer.allowed = obj.optBoolean("allow", true)
                    toast(if (fileTransfer.allowed) "电脑端已允许传文件" else "电脑端已关闭文件接收")
                }
                "screenshot" -> onScreenshot(obj)
                "apps" -> onAppsFrame(obj)
                "file_offer", "file_end", "file_reject", "file_result" -> fileTransfer.onText(obj)
            }
        }

        override fun onBinary(bytes: ByteArray) {
            fileTransfer.onBinary(bytes)
        }

        override fun onAuthFailed(reason: String) {
            connected = false
            active = false
            renderHeader()
            Toast.makeText(this@MainActivity, "认证失败（$reason），请重新扫码绑定", Toast.LENGTH_LONG).show()
            showBindingDialog()
        }
    })

    private fun parseQr(content: String): Device? {
        val obj = runCatching { JSONObject(content) }.getOrNull() ?: return null
        if (obj.optString("t") != "remote-mic") return null
        val host = obj.optString("host", "")
        val token = obj.optString("token", "")
        if (host.isBlank() || token.isBlank()) return null
        return Device(
            name = obj.optString("name", host),
            host = host,
            port = obj.optInt("port", 8765),
            token = token
        )
    }

    private fun renderHeader() {
        val device = current
        // 在线状态由左侧圆点 + 状态文字表达，设备名后面不再缀「已连接」
        binding.tvDevice.text = device?.name ?: "未绑定设备 · 点击扫码"
        binding.statusText.text = when {
            connected && active -> "控制中"
            connected -> "待命"
            else -> "未连接"
        }
        val dotColor = when {
            connected && active -> 0xFF2F9E63.toInt()
            connected -> 0xFFFFA000.toInt()
            else -> 0xFF9E9E9E.toInt()
        }
        binding.statusDot.backgroundTintList = ColorStateList.valueOf(dotColor)
        // 连接状态一变（扫码后建连、切换设备、掉线）就同步刷新设备管理列表里的圆点
        deviceListRefresh?.invoke()
    }

    private fun isOnline(device: Device): Boolean =
        connected && device.token == current?.token

    /** 点顶部设备名：下拉已绑定设备列表，可当场切换被控设备。 */
    private fun showDeviceMenu() {
        if (devices.isEmpty()) {
            // 一台都没绑定时不必先进设备管理，点顶栏直接开扫码页（绑定只能扫码）
            startScan()
            return
        }

        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(6), dp(6), dp(6), dp(6))
            background = ContextCompat.getDrawable(this@MainActivity, R.drawable.bg_device_pop)
        }

        var popup: PopupWindow? = null

        devices.forEach { device ->
            val online = isOnline(device)
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(dp(10), 0, dp(10), 0)
                background = ContextCompat.getDrawable(this@MainActivity, R.drawable.bg_pop_item)
            }

            val dot = View(this).apply {
                background = ContextCompat.getDrawable(this@MainActivity, R.drawable.bg_dot)
                backgroundTintList = ColorStateList.valueOf(
                    ContextCompat.getColor(
                        this@MainActivity,
                        if (online) R.color.ok else R.color.dot_off
                    )
                )
            }
            row.addView(dot, LinearLayout.LayoutParams(dp(7), dp(7)))

            row.addView(TextView(this).apply {
                text = device.name
                textSize = 13f
                typeface = Typeface.DEFAULT_BOLD
                setTextColor(ContextCompat.getColor(this@MainActivity, R.color.text))
            }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
                .apply { marginStart = dp(8) })

            row.addView(TextView(this).apply {
                text = if (online) "在线" else "未连接"
                textSize = 11f
                setTextColor(ContextCompat.getColor(this@MainActivity, R.color.muted))
            })

            val check = ImageView(this).apply {
                setImageResource(R.drawable.ic_check)
                imageTintList = ColorStateList.valueOf(
                    ContextCompat.getColor(this@MainActivity, R.color.accent)
                )
                alpha = if (device.token == current?.token) 1f else 0f
            }
            row.addView(check, LinearLayout.LayoutParams(dp(17), dp(17)).apply { marginStart = dp(6) })

            row.click {
                popup?.dismiss()
                if (device.token == current?.token && connected) {
                    toast("已经在控制 ${device.name}")
                } else {
                    connectTo(device)
                    toast("正在切换到 ${device.name}")
                }
            }
            content.addView(row, LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dp(46)
            ))
        }

        val sepLp = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, dp(1)
        )
        sepLp.setMargins(dp(8), dp(5), dp(8), dp(5))
        content.addView(View(this).apply {
            setBackgroundColor(ContextCompat.getColor(this@MainActivity, R.color.line))
        }, sepLp)

        val add = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(10), 0, dp(10), 0)
            background = ContextCompat.getDrawable(this@MainActivity, R.drawable.bg_pop_item)
        }
        add.addView(ImageView(this).apply {
            setImageResource(R.drawable.ic_qr)
            imageTintList = ColorStateList.valueOf(
                ContextCompat.getColor(this@MainActivity, R.color.accent)
            )
        }, LinearLayout.LayoutParams(dp(17), dp(17)))
        add.addView(TextView(this).apply {
            text = "绑定新设备"
            textSize = 13f
            setTextColor(ContextCompat.getColor(this@MainActivity, R.color.accent))
        }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            .apply { marginStart = dp(8) })
        add.click {
            popup?.dismiss()
            startScan()
        }
        content.addView(add, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, dp(44)
        ))

        popup = PopupWindow(
            content,
            dp(280),
            LinearLayout.LayoutParams.WRAP_CONTENT,
            true
        ).apply {
            isOutsideTouchable = true
            elevation = dp(14).toFloat()
            setBackgroundDrawable(ColorDrawable(android.graphics.Color.TRANSPARENT))
        }
        popup?.showAsDropDown(
            binding.tvDevice,
            (binding.tvDevice.width - dp(280)) / 2,
            dp(6)
        )
    }

    /** 设备管理：底部玻璃抽屉，列出已绑定电脑（圆点表示在线）、扫码新增、删除、切换。 */
    private fun showBindingDialog() {
        if (isFinishing || isDestroyed) return

        val sheet = BottomSheetDialog(this)
        val view = layoutInflater.inflate(R.layout.dialog_device, null)
        sheet.setContentView(view)
        sheet.behavior.peekHeight = (resources.displayMetrics.heightPixels * 0.55f).toInt()

        val list = view.findViewById<LinearLayout>(R.id.deviceList)
        val closeIcon = view.findViewById<View>(R.id.btnCloseSheetIcon)
        val closeText = view.findViewById<View>(R.id.btnCloseSheet)
        val scan = view.findViewById<View>(R.id.btnScanBind)

        fun rebuild() {
            list.removeAllViews()
            if (devices.isEmpty()) {
                list.addView(TextView(this).apply {
                    text = "还没有绑定任何电脑，点下方「扫码绑定设备」"
                    textSize = 13f
                    setTextColor(ContextCompat.getColor(this@MainActivity, R.color.muted))
                    setPadding(0, dp(8), 0, dp(8))
                })
            }
            devices.forEach { device ->
                val online = isOnline(device)
                val row = LinearLayout(this).apply {
                    orientation = LinearLayout.HORIZONTAL
                    gravity = Gravity.CENTER_VERTICAL
                    setPadding(dp(12), dp(12), dp(10), dp(12))
                    background = ContextCompat.getDrawable(this@MainActivity, R.drawable.bg_device_row)
                }

                val dot = View(this).apply {
                    background = ContextCompat.getDrawable(this@MainActivity, R.drawable.bg_dot)
                    backgroundTintList = ColorStateList.valueOf(
                        ContextCompat.getColor(
                            this@MainActivity,
                            if (online) R.color.ok else R.color.dot_off
                        )
                    )
                }
                row.addView(dot, LinearLayout.LayoutParams(dp(8), dp(8)))

                val info = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
                info.addView(TextView(this).apply {
                    text = device.name
                    textSize = 15f
                    typeface = Typeface.DEFAULT_BOLD
                    setTextColor(ContextCompat.getColor(this@MainActivity, R.color.text))
                })
                info.addView(TextView(this).apply {
                    text = "${if (online) "在线" else "未连接"} · ${device.endpoint}"
                    textSize = 11f
                    setTextColor(ContextCompat.getColor(this@MainActivity, R.color.muted))
                    setPadding(0, dp(2), 0, 0)
                })
                info.click {
                    if (online) {
                        toast("已经在控制 ${device.name}")
                    } else {
                        connectTo(device)
                        toast("正在切换到 ${device.name}")
                        rebuild()
                    }
                }
                row.addView(
                    info,
                    LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
                        .apply { marginStart = dp(10) }
                )

                val del = ImageButton(this).apply {
                    setImageResource(R.drawable.ic_trash)
                    imageTintList = ColorStateList.valueOf(
                        ContextCompat.getColor(this@MainActivity, R.color.primary)
                    )
                    background = ContextCompat.getDrawable(this@MainActivity, R.drawable.bg_ghost_delete)
                    contentDescription = "删除 ${device.name}"
                    setPadding(dp(11), dp(11), dp(11), dp(11))
                }
                del.click {
                    devices.removeAll { it.token == device.token }
                    saveDevices()
                    if (current?.token == device.token) {
                        current = null
                        connected = false
                        client.close()
                        prefs.edit().putString("current_token", "").apply()
                        renderHeader()
                    }
                    rebuild()
                }
                row.addView(del, LinearLayout.LayoutParams(dp(44), dp(44)))

                list.addView(
                    row,
                    LinearLayout.LayoutParams(
                        LinearLayout.LayoutParams.MATCH_PARENT,
                        LinearLayout.LayoutParams.WRAP_CONTENT
                    ).apply { if (list.childCount > 0) topMargin = dp(8) }
                )
            }
        }
        rebuild()

        scan.click { startScan() }
        closeIcon.click { sheet.dismiss() }
        closeText.click { sheet.dismiss() }

        deviceListRefresh = { rebuild() }
        sheet.setOnDismissListener { deviceListRefresh = null }
        sheet.setOnShowListener {
            sheet.findViewById<View>(com.google.android.material.R.id.design_bottom_sheet)
                ?.setBackgroundColor(android.graphics.Color.TRANSPARENT)
        }
        sheet.window?.let { win ->
            win.navigationBarColor = ContextCompat.getColor(this, R.color.white)
            if (Build.VERSION.SDK_INT >= 29) {
                win.isNavigationBarContrastEnforced = false
            }
        }
        sheet.show()
    }

    private fun startScan() {
        // 自建竖屏扫码页（CameraX + ZXing core），替换掉原来横屏的 journeyapps CaptureActivity
        scanLauncher.launch(
            Intent(this, com.willen.remotemic.scan.QrScanActivity::class.java)
        )
    }

    // ------------------------------------------------------------------ 发送

    private fun send(type: String, vararg kv: Pair<String, Any?>) {
        if (!connected) return
        val obj = JSONObject().put("type", type)
        kv.forEach { obj.put(it.first, it.second) }
        client.sendText(obj.toString())
    }

    private fun sendHello() {
        send("hello", "device" to android.os.Build.MODEL, "ts" to System.currentTimeMillis())
    }

    // ------------------------------------------------------------------ 文件互传

    private fun canTransfer(): Boolean {
        if (!connected) {
            toast("未连接电脑")
            return false
        }
        if (!fileTransfer.allowed) {
            toast("电脑端已关闭这台遥控器的文件接收")
            return false
        }
        if (fileTransfer.isBusy()) {
            toast("正在传输，稍等一下")
            return false
        }
        return true
    }

    /** 照片/视频：系统 Photo Picker（Android 13+ 的系统 UI，零存储权限） */
    private fun pickMedia() {
        if (!canTransfer()) return
        val intent = if (android.os.Build.VERSION.SDK_INT >= 33) {
            Intent("android.provider.action.PICK_IMAGES")
                .setType("image/*")
                .putExtra("android.provider.extra.PICK_IMAGES_MAX", FileTransferManager.MAX_COUNT)
        } else {
            Intent(Intent.ACTION_OPEN_DOCUMENT)
                .addCategory(Intent.CATEGORY_OPENABLE)
                .setType("image/*")
                .putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
        }
        try {
            pickMediaLauncher.launch(intent)
        } catch (e: Exception) {
            android.util.Log.w("MainActivity", "photo picker unavailable: ${e.message}")
            pickDocument()
        }
    }

    /** 通用文件：SAF 的 ACTION_OPEN_DOCUMENT，PDF/压缩包/视频都能选，零存储权限 */
    private fun pickDocument() {
        if (!canTransfer()) return
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT)
            .addCategory(Intent.CATEGORY_OPENABLE)
            .setType("*/*")
            .putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
        try {
            pickFileLauncher.launch(intent)
        } catch (e: Exception) {
            toast("系统文件选择器不可用：${e.message}")
        }
    }

    private fun startSendUris(uris: List<Uri>) {
        if (!canTransfer()) return
        if (uris.size > FileTransferManager.MAX_COUNT) {
            toast("一次最多 ${FileTransferManager.MAX_COUNT} 个文件")
            return
        }
        fileTransfer.sendUris(uris)
        // 先起前台服务再开传：小文件几百毫秒就传完，若等服务启动完再判断"已完成"，
        // 会出现 stopService 早于 startForeground，系统直接杀进程。
        startTransferService()
        showTransferDialog()
    }

    private var transferServiceStarted = false
    private var transferServiceStartedAt = 0L
    private var transferStopScheduled = false

    private fun observeTransfer() {
        if (transferObserver != null) return
        val observer: (TransferProgress?) -> Unit = { p ->
            uiHandler.post {
                when {
                    p == null -> dismissTransferDialog()
                    p.finished -> {
                        updateTransferViews(p)
                        uiHandler.postDelayed({ dismissTransferDialog() }, 600)
                        if (p.error != null) {
                            if (p.error != "已取消") toast("传输失败：${p.error}")
                        } else {
                            toast(if (p.sending) "发送完成" else "已接收文件")
                        }
                        stopTransferService()
                        uiHandler.postDelayed({ if (TransferBus.current?.finished == true) TransferBus.publish(null) }, 1200)
                    }
                    else -> {
                        showTransferDialog()
                        updateTransferViews(p)
                        startTransferService()
                    }
                }
            }
        }
        transferObserver = observer
        TransferBus.observe(observer)
    }

    private fun startTransferService() {
        if (transferServiceStarted) return
        transferServiceStarted = true
        transferServiceStartedAt = android.os.SystemClock.elapsedRealtime()
        transferStopScheduled = false
        try {
            TransferService.start(this)
        } catch (e: Exception) {
            android.util.Log.w("MainActivity", "start transfer service failed: ${e.message}")
            transferServiceStarted = false
        }
    }

    /** 停止前台服务：必须晚于 startForegroundService + 5s，否则系统判"超时未进前台"直接杀进程。 */
    private fun stopTransferService() {
        if (!transferServiceStarted || transferStopScheduled) return
        transferStopScheduled = true
        val elapsed = android.os.SystemClock.elapsedRealtime() - transferServiceStartedAt
        val wait = (6000L - elapsed).coerceAtLeast(600L)
        uiHandler.postDelayed({ stopTransferServiceNow() }, wait)
    }

    private fun stopTransferServiceNow() {
        transferStopScheduled = false
        if (!transferServiceStarted) return
        transferServiceStarted = false
        try {
            TransferService.stop(this)
        } catch (_: Exception) {
        }
    }

    private fun showTransferDialog() {
        if (isFinishing || isDestroyed) return
        if (transferDialog?.isShowing == true) return
        val view = layoutInflater.inflate(R.layout.dialog_transfer, null)
        transferTitleView = view.findViewById(R.id.tvTransferTitle)
        transferNameView = view.findViewById(R.id.tvTransferName)
        transferDetailView = view.findViewById(R.id.tvTransferDetail)
        transferProgressBar = view.findViewById(R.id.progressTransfer)
        view.findViewById<Button>(R.id.btnTransferCancel).setOnClickListener {
            fileTransfer.cancel()
            dismissTransferDialog()
        }
        // 后台传递：只收起浮窗，传输继续，进度交给通知栏（遥控器界面照常可用）
        view.findViewById<Button>(R.id.btnTransferBackground).setOnClickListener {
            dismissTransferDialog()
            toast("已转到后台，进度见通知栏")
        }
        val dialog = AlertDialog.Builder(this)
            .setView(view)
            .setCancelable(true)
            .create()
        dialog.setOnCancelListener { /* 按返回键=后台传递，不打断传输 */ }
        dialog.show()
        transferDialog = dialog
        TransferBus.current?.let { updateTransferViews(it) }
    }

    private fun dismissTransferDialog() {
        try {
            transferDialog?.dismiss()
        } catch (_: Exception) {
        }
        transferDialog = null
    }

    private fun updateTransferViews(p: TransferProgress) {
        transferTitleView?.text = when {
            p.error != null -> "传输失败"
            p.finished -> "传输完成"
            p.sending -> "正在发送到电脑"
            else -> "正在接收电脑文件"
        }
        transferNameView?.text = p.summary()
        transferDetailView?.text = p.detail()
        transferProgressBar?.let {
            it.isIndeterminate = p.bytesTotal <= 0 || p.finished
            it.progress = p.percent
        }
    }

    // ------------------------------------------------------------------ 远程粘贴

    /**
     * 读手机剪贴板并发到电脑，由电脑写入剪贴板并（有输入焦点时）执行 Ctrl+V。
     *
     * 本地先做类型与长度校验，不合格直接提示，不把脏数据推给电脑端；
     * 电脑端写入失败只在电脑端弹托盘提示，手机端不再二次报错。
     */
    private fun sendClipboard() {
        if (!connected) {
            toast("未连接电脑")
            return
        }
        val cm = getSystemService(ClipboardManager::class.java)
        if (cm == null || !cm.hasPrimaryClip()) {
            toast("剪贴板为空")
            return
        }
        val clip = cm.primaryClip
        if (clip == null || clip.itemCount <= 0) {
            toast("剪贴板为空")
            return
        }
        val text = clip.getItemAt(0).text?.toString()
        if (text.isNullOrEmpty()) {
            toast("暂不支持该类型内容（仅支持文本）")
            return
        }
        if (text.length > CLIP_MAX_CHARS) {
            toast("文本超长：${text.length} 字符，上限 $CLIP_MAX_CHARS")
            return
        }
        send("clip", "text" to text)
    }

    private fun toast(msg: String) {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    // ------------------------------------------------------------------ 截屏

    /** 请求电脑端截屏：结果通过 onMessage 的 "screenshot" 帧回来。 */
    private fun requestScreenshot() {
        if (!connected) {
            toast("未连接电脑")
            return
        }
        if (!active) {
            toast("当前是待命状态，控制权不在这台手机")
            return
        }
        send("screenshot")
    }

    /**
     * 收到电脑端的截图：base64 → Bitmap → 全屏浮层显示。
     *
     * 解码放工作线程（1600x900 的 JPEG 在主线解会顿一下）；图片只进内存，
     * 交给 [ShotHolder] 传给查看页，退出即释放，不写任何文件。
     */
    private fun onScreenshot(obj: JSONObject) {
        if (!obj.optBoolean("ok", false)) {
            toast("截屏失败：${obj.optString("reason", "未知原因")}")
            return
        }
        val b64 = obj.optString("image", "")
        if (b64.isBlank()) {
            toast("截屏失败：没有收到图片")
            return
        }
        Thread {
            val bitmap = runCatching {
                val bytes = android.util.Base64.decode(b64, android.util.Base64.DEFAULT)
                val bounds = android.graphics.BitmapFactory.Options().apply { inJustDecodeBounds = true }
                android.graphics.BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
                var sample = 1
                while (bounds.outWidth / (sample * 2) >= 1600 && bounds.outHeight / (sample * 2) >= 900) {
                    sample *= 2
                }
                android.graphics.BitmapFactory.decodeByteArray(
                    bytes, 0, bytes.size,
                    android.graphics.BitmapFactory.Options().apply { inSampleSize = sample }
                )
            }.getOrNull()

            runOnUiThread {
                if (bitmap == null) {
                    toast("截图解码失败")
                    return@runOnUiThread
                }
                com.willen.remotemic.shot.ShotHolder.set(bitmap)
                startActivity(
                    Intent(this, com.willen.remotemic.shot.ShotViewerActivity::class.java)
                )
            }
        }.start()
    }

    // ------------------------------------------------------------------ 快捷应用图标

    private fun appButton(index: Int): com.google.android.material.button.MaterialButton = when (index) {
        1 -> binding.appBtn1
        2 -> binding.appBtn2
        3 -> binding.appBtn3
        else -> binding.appBtn4
    }

    /**
     * 电脑端推来的四个栏位（名称 + 图标 data URL）。
     *
     * 存进本地 prefs，下次冷启动没连上电脑也能先显示上次的图标/名称；
     * 图标是 16~32px 的小 PNG，base64 一两 KB，直接放 prefs 没有压力。
     */
    private fun onAppsFrame(obj: JSONObject) {
        val slots = obj.optJSONArray("slots") ?: return
        val cache = JSONArray()
        for (i in 0 until slots.length()) {
            val item = slots.optJSONObject(i) ?: continue
            val index = item.optString("slot", "").toIntOrNull() ?: continue
            if (index !in 1..4) continue
            val name = item.optString("name", "")
            val icon = item.optString("icon", "")
            applyAppButton(index, name, icon)
            cache.put(JSONObject().put("slot", index).put("name", name).put("icon", icon))
        }
        prefs.edit().putString("app_slots", cache.toString()).apply()
    }

    private fun applyAppSlotsFromPrefs() {
        val raw = prefs.getString("app_slots", "") ?: return
        val arr = runCatching { JSONArray(raw) }.getOrNull() ?: return
        for (i in 0 until arr.length()) {
            val item = arr.optJSONObject(i) ?: continue
            val index = item.optInt("slot", 0)
            if (index !in 1..4) continue
            applyAppButton(index, item.optString("name", ""), item.optString("icon", ""))
        }
    }

    private fun applyAppButton(index: Int, name: String, iconDataUrl: String) {
        val button = appButton(index)
        button.text = name.ifBlank { "应用$index" }
        button.textSize = 11f
        button.maxLines = 1
        button.ellipsize = android.text.TextUtils.TruncateAt.END
        val bitmap = decodeDataUrlIcon(iconDataUrl)
        if (bitmap == null) {
            button.icon = null
            return
        }
        val density = resources.displayMetrics.density
        button.icon = android.graphics.drawable.BitmapDrawable(resources, bitmap)
        // MaterialButton 默认会把图标按主题色染色，应用图标就变成一团色块；
        // 这里必须把 iconTint 清成 null，才显示原色图标。
        @Suppress("UsePropertyAccessSyntax")
        button.iconTint = null
        // 模仿桌面图标：图标在上、文字在下，按钮本身透明不画底色
        button.iconGravity = com.google.android.material.button.MaterialButton.ICON_GRAVITY_TEXT_TOP
        button.iconSize = (52 * density).toInt()
        button.iconPadding = (6 * density).toInt()
        button.backgroundTintList = android.content.res.ColorStateList.valueOf(android.graphics.Color.TRANSPARENT)
    }

    private fun decodeDataUrlIcon(dataUrl: String): android.graphics.Bitmap? {
        if (!dataUrl.startsWith("data:image/")) return null
        val comma = dataUrl.indexOf(',')
        if (comma <= 0) return null
        val payload = dataUrl.substring(comma + 1)
        return runCatching {
            val bytes = android.util.Base64.decode(payload, android.util.Base64.DEFAULT)
            android.graphics.BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
        }.getOrNull()
    }

    // ------------------------------------------------------------------ 语音

    private fun startVoice() {
        if (voiceOn) return
        voiceOn = true
        val intent = Intent(this, RecorderService::class.java).setAction(RecorderService.ACTION_START)
        androidx.core.content.ContextCompat.startForegroundService(this, intent)
        updateVoiceButton()
    }

    private fun stopVoice() {
        if (!voiceOn) return
        voiceOn = false
        val intent = Intent(this, RecorderService::class.java).setAction(RecorderService.ACTION_STOP)
        startService(intent)
        updateVoiceButton()
    }

    private fun updateVoiceButton() {
        val live = voiceOn || openMode
        binding.tvVoiceTitle.text = if (live) "说话中" else "按住说话"
        binding.tvVoiceSub.text = when {
            openMode -> "点击结束"
            voiceOn -> "松手/点击结束"
            else -> "双击常开"
        }
        binding.btnVoice.isActivated = live
        binding.ivVoiceIcon.visibility = if (live) View.GONE else View.VISIBLE
        binding.voiceWave.visibility = if (live) View.VISIBLE else View.GONE
        setWaveAnimating(live)
    }

    /** 说话中：五根波形柱错峰伸缩；结束后停下并复位。 */
    private fun setWaveAnimating(on: Boolean) {
        val bars = listOf(
            binding.voiceBar1,
            binding.voiceBar2,
            binding.voiceBar3,
            binding.voiceBar4,
            binding.voiceBar5
        )
        if (on) {
            if (waveAnimators.isNotEmpty()) return
            bars.forEachIndexed { index, bar ->
                val animator = ObjectAnimator.ofFloat(
                    bar, View.SCALE_Y, 0.45f, 1.05f, 0.55f, 0.9f
                )
                animator.duration = 700L + index * 90L
                animator.startDelay = index * 70L
                animator.repeatCount = ValueAnimator.INFINITE
                animator.interpolator = LinearInterpolator()
                animator.start()
                waveAnimators.add(animator)
            }
        } else {
            waveAnimators.forEach { it.cancel() }
            waveAnimators.clear()
            bars.forEach { it.scaleY = 1f }
        }
    }

    private fun cancelPendingVoiceStop() {
        pendingVoiceStop?.let { uiHandler.removeCallbacks(it) }
        pendingVoiceStop = null
    }

    private fun resetVoiceState() {
        cancelPendingVoiceStop()
        stopVoice()
        openMode = false
        suppressStopOnce = false
        lastVoiceUpAt = 0L
        updateVoiceButton()
    }

    // ------------------------------------------------------------------ 远程粘贴

    // ------------------------------------------------------------------ 按钮

    /** 退格：单击删一个；长按 350ms 后开始连删，间隔从 120ms 逐步降到 34ms。 */
    private fun bindDeleteKey(view: View) {
        var interval = 120L
        val repeat = object : Runnable {
            override fun run() {
                send("key", "code" to "delete", "action" to "down")
                interval = (interval - 8L).coerceAtLeast(34L)
                deleteRepeat = this
                uiHandler.postDelayed(this, interval)
            }
        }

        // 键盘 / 无障碍单击（触摸路径被下面的 OnTouchListener 消费，不会重复）
        view.setOnClickListener {
            it.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
            send("key", "code" to "delete", "action" to "down")
        }

        view.setOnTouchListener { v, event ->
            when (event.action) {
                MotionEvent.ACTION_DOWN -> {
                    v.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                    v.animate().scaleX(.95f).scaleY(.95f).setDuration(90).start()
                    stopDeleteRepeat()
                    // 按下先删一个，再等 350ms 进入连删
                    send("key", "code" to "delete", "action" to "down")
                    interval = 120L
                    deleteRepeat = repeat
                    uiHandler.postDelayed(repeat, 350L)
                    true
                }

                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    v.animate().scaleX(1f).scaleY(1f).setDuration(120).start()
                    stopDeleteRepeat()
                    true
                }

                else -> false
            }
        }
    }

    private fun stopDeleteRepeat() {
        deleteRepeat?.let { uiHandler.removeCallbacks(it) }
        deleteRepeat = null
    }

    private fun bindButtons() {
        binding.btnSettings.click { showBindingDialog() }
        binding.tvDevice.click { showDeviceMenu() }

        // 方向环：与原来的四个方向键完全等价，只发 up/right/down/left 的 key.down
        binding.ringPad.onDirection = { code: String, action: String ->
            send("key", "code" to code, "action" to action)
        }
        binding.btnOk.click { send("key", "code" to "ok", "action" to "down") }
        binding.btnCancel.click { send("key", "code" to "esc", "action" to "down") }

        // 语音：按住临时说话；快速双击常开；常开态单击结束并退出常开
        binding.btnVoice.hold(
            down = {
                val now = android.os.SystemClock.uptimeMillis()
                val doubleTap = lastVoiceUpAt != 0L && (now - lastVoiceUpAt) in 1..400
                cancelPendingVoiceStop()
                if (doubleTap) {
                    openMode = true
                    suppressStopOnce = true
                    if (!voiceOn) {
                        send("voice", "action" to "down")
                        startVoice()
                    }
                } else if (!openMode && !voiceOn) {
                    send("voice", "action" to "down")
                    startVoice()
                }
                updateVoiceButton()
            },
            up = {
                lastVoiceUpAt = android.os.SystemClock.uptimeMillis()
                if (openMode) {
                    if (suppressStopOnce) {
                        suppressStopOnce = false
                    } else {
                        openMode = false
                        if (voiceOn) {
                            send("voice", "action" to "up")
                            stopVoice()
                        }
                    }
                } else if (voiceOn) {
                    val stop = Runnable {
                        pendingVoiceStop = null
                        if (!openMode && voiceOn) {
                            send("voice", "action" to "up")
                            stopVoice()
                        }
                        updateVoiceButton()
                    }
                    pendingVoiceStop = stop
                    uiHandler.postDelayed(stop, 320)
                }
                updateVoiceButton()
            }
        )

        binding.appBtn1.click { send("launch", "slot" to "1") }
        binding.appBtn2.click { send("launch", "slot" to "2") }
        binding.appBtn3.click { send("launch", "slot" to "3") }
        binding.appBtn4.click { send("launch", "slot" to "4") }

        binding.btnPaste.click { sendClipboard() }
        binding.btnScreenshot.click { requestScreenshot() }
        bindDeleteKey(binding.btnDelete)

        binding.btnSendMedia.click { pickMedia() }
        binding.btnSendFile.click { pickDocument() }

        binding.btnSwitcher.click { send("switcher", "action" to "enter") }
        binding.btnTabSwitch.click { send("key", "code" to "tab_next", "action" to "down") }

        binding.btnNewChat.click { send("key", "code" to "new_chat", "action" to "down") }
        binding.btnSend.click { send("send") }

        binding.btnPrev.click { send("key", "code" to "prev_track", "action" to "down") }
        binding.btnNext.click { send("key", "code" to "next_track", "action" to "down") }
        binding.btnPlayPause.click { send("key", "code" to "play_pause", "action" to "down") }
        binding.btnVolUp.click { send("key", "code" to "vol_up", "action" to "down") }
        binding.btnVolDown.click { send("key", "code" to "vol_down", "action" to "down") }
        binding.btnVideoPlayPause.click { send("key", "code" to "video_play_pause", "action" to "down") }
        binding.btnScrollNext.click { send("key", "code" to "scroll_next", "action" to "down") }
        binding.btnScrollPrev.click { send("key", "code" to "scroll_prev", "action" to "down") }
    }

    override fun onPause() {
        stopDeleteRepeat()
        super.onPause()
    }

    override fun onResume() {
        super.onResume()
        if (firstResume) {
            firstResume = false          // 首次进入不打扰正在建立的首连
        } else if (!connected) {
            client.wakeUp()
        }
        // 从扫码页/手动添加返回时，若设备管理对话框还开着，顺手刷一遍（连接状态也会跟着变绿）
        deviceListRefresh?.invoke()
    }

    override fun onDestroy() {
        cancelPendingVoiceStop()
        stopDeleteRepeat()
        waveAnimators.forEach { it.cancel() }
        waveAnimators.clear()
        AudioBus.onPcm = null
        transferObserver?.let { TransferBus.forget(it) }
        transferObserver = null
        dismissTransferDialog()
        runCatching { unregisterReceiver(screenReceiver) }
        client.close()
        super.onDestroy()
    }

    companion object {
        private const val REQ_PERM = 100

        /** 与电脑端 server.py 的 CLIP_MAX_CHARS 保持一致。 */
        private const val CLIP_MAX_CHARS = 100_000
    }
}
