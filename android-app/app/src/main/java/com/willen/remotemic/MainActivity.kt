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
    private lateinit var recorderFailReceiver: android.content.BroadcastReceiver
    private var firstResume = true

    private val devices = mutableListOf<Device>()
    private var current: Device? = null

    private var connected = false
    private var active = false
    private var voiceOn = false
    /**
     * 电脑端虚拟声卡是否就绪（`status.audio_ready`）。
     * 未就绪时电脑端会直接丢弃 PCM，手机端必须禁用语音入口并说明原因，
     * 否则用户对着手机说了半天、界面还显示"说话中"，电脑上一个字都没上屏。
     * 默认 true：首帧 status 到达前不误禁用。
     */
    private var audioReady = true
    /** 电脑端是否停在 Alt+Tab 切换浮层（`status.mode`）。 */
    private var switcherMode = false
    /** Ctrl+1..9 循环的当前位置（`status.tab_index`，0 = 还没切过）。 */
    private var tabIndex = 0
    private var openMode = false

    private val uiHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private var pendingVoiceStop: Runnable? = null
    private var lastVoiceUpAt = 0L
    private var suppressStopOnce = false
    private val waveAnimators = mutableListOf<ObjectAnimator>()
    private var deleteRepeat: Runnable? = null
    /** 截图解码中：连点截屏会起一堆解码线程，这里同一时刻只允许一张在路上。 */
    private var shotPending = false

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
    /** 当前打开的设备抽屉 / 顶栏下拉：销毁界面时要一并收掉，否则窗口泄漏。 */
    private var deviceSheet: BottomSheetDialog? = null
    private var devicePopup: PopupWindow? = null
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
        registerRecorderFailReceiver()
    }

    /**
     * 录音服务起不来（权限被拒 / 麦克风被占用）时会广播失败：
     * 界面必须跟着退出"说话中"，否则波形一直动、通知栏却早已没有录音。
     */
    private fun registerRecorderFailReceiver() {
        recorderFailReceiver = object : android.content.BroadcastReceiver() {
            override fun onReceive(context: android.content.Context?, intent: Intent?) {
                cancelPendingVoiceStop()
                voiceOn = false
                openMode = false
                suppressStopOnce = false
                updateVoiceButton()
            }
        }
        ContextCompat.registerReceiver(
            this,
            recorderFailReceiver,
            android.content.IntentFilter(RecorderService.ACTION_START_FAILED),
            ContextCompat.RECEIVER_NOT_EXPORTED
        )
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

    /**
     * 权限结果：RECORD_AUDIO 被拒后必须给降级提示，否则点语音键会起一个
     * 拿不到麦克风的前台服务（通知栏写着"正在录音"却一个字节都没有）。
     */
    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQ_PERM) return
        val index = permissions.indexOf(Manifest.permission.RECORD_AUDIO)
        if (index >= 0 && grantResults.getOrNull(index) != PackageManager.PERMISSION_GRANTED) {
            toast("没有麦克风权限，语音功能不可用（可在系统设置里开启）")
            updateVoiceButton()
        }
    }

    /** 每次实时查权限：用户可能中途去系统设置里打开或关掉。 */
    private fun micGranted(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED

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
            // 断线后电脑端的浮层/Tab 状态无从得知，复位成默认文案，别留着过期的指示
            applyModeAndTab("normal", 0)
            renderHeader()
        }

        override fun onMessage(text: String) {
            val obj = runCatching { JSONObject(text) }.getOrNull() ?: return
            when (obj.optString("type")) {
                "status" -> {
                    active = obj.optBoolean("active", active)
                    applyAudioReady(obj.optBoolean("audio_ready", audioReady))
                    applyModeAndTab(obj.optString("mode", "normal"), obj.optInt("tab_index", 0))
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
                "auth_ok" -> {
                    fileTransfer.allowed = obj.optBoolean("files_allowed", true)
                    // auth_ok 里嵌了一份 status，首帧就能知道声卡就绪与否
                    obj.optJSONObject("status")?.let {
                        applyAudioReady(it.optBoolean("audio_ready", audioReady))
                    }
                }
                "files_allowed" -> {
                    fileTransfer.allowed = obj.optBoolean("allow", true)
                    toast(if (fileTransfer.allowed) "电脑端已允许传文件" else "电脑端已关闭文件接收")
                }
                "screenshot" -> onScreenshot(obj)
                "apps" -> onAppsFrame(obj)
                "file_offer", "file_ack", "file_end", "file_reject", "file_result" -> fileTransfer.onText(obj)
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
        // host 会被直接拼进 "ws://$host:$port"：只要含 / @ : 空格 等字符，OkHttp 就会把它
        // 解析成另一台主机或另一个端口（恶意二维码可借此把手机引到攻击者的机器）。
        // 这里只放行 IPv4 / 主机名字符集，端口也必须在合法范围内，否则当作格式不对。
        if (!HOST_PATTERN.matches(host)) return null
        val port = obj.optInt("port", 8765)
        if (port !in 1..65535) return null
        return Device(
            name = obj.optString("name", host),
            host = host,
            port = port,
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

        // 连点顶栏不该叠出多个下拉；同时记到字段上，onDestroy 好收掉
        devicePopup?.dismiss()
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
        devicePopup = popup
        popup?.showAsDropDown(
            binding.tvDevice,
            (binding.tvDevice.width - dp(280)) / 2,
            dp(6)
        )
    }

    /** 设备管理：底部玻璃抽屉，列出已绑定电脑（圆点表示在线）、扫码新增、删除、切换。 */
    private fun showBindingDialog() {
        if (isFinishing || isDestroyed) return

        // 认证失败时会再弹一次，旧抽屉若还开着就会叠两层；而且旧 sheet 的 dismiss 回调
        // 会把新 sheet 的刷新函数清掉。先收掉旧的，dismiss 回调只认"自己仍是当前 sheet"。
        val previous = deviceSheet
        deviceSheet = null
        previous?.let { runCatching { it.dismiss() } }

        val sheet = BottomSheetDialog(this)
        deviceSheet = sheet
        val view = layoutInflater.inflate(R.layout.dialog_device, null)
        sheet.setContentView(view)
        sheet.behavior.peekHeight = (resources.displayMetrics.heightPixels * 0.55f).toInt()

        val list = view.findViewById<LinearLayout>(R.id.deviceList)
        val closeIcon = view.findViewById<View>(R.id.btnCloseSheetIcon)
        val closeText = view.findViewById<View>(R.id.btnCloseSheet)
        val scan = view.findViewById<View>(R.id.btnScanBind)
        view.findViewById<TextView>(R.id.tvAppVersion).text = "手机遥控麦 v${appVersion()}"

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
        sheet.setOnDismissListener {
            // 只有"自己还是当前抽屉"时才清刷新函数：被新 sheet 顶掉的旧 sheet 迟到回调
            // 不能把新 sheet 的 rebuild 清掉（否则列表不再随连接状态更新）。
            if (deviceSheet === sheet) {
                deviceSheet = null
                deviceListRefresh = null
            }
        }
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
        // 连点会起多个解码线程、还会互相覆盖 ShotHolder 里的图，同一时刻只放一张在路上
        if (shotPending) {
            toast("上一张截图还在处理")
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
        if (isFinishing || isDestroyed) return
        if (!obj.optBoolean("ok", false)) {
            toast("截屏失败：${obj.optString("reason", "未知原因")}")
            return
        }
        val b64 = obj.optString("image", "")
        if (b64.isBlank()) {
            toast("截屏失败：没有收到图片")
            return
        }
        // 查看页还在前台时不能换图：ShotHolder.set 会 recycle 旧 Bitmap，
        // 而 ZoomImageView 正在画它，会直接崩 "Canvas: trying to use a recycled bitmap"。
        if (shotPending || !lifecycle.currentState.isAtLeast(androidx.lifecycle.Lifecycle.State.RESUMED)) {
            android.util.Log.i("MainActivity", "screenshot dropped: busy or not resumed")
            return
        }
        shotPending = true
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
                shotPending = false
                // 界面已销毁就别再 startActivity（会抛异常/泄漏），图解出来也要就地回收
                if (isFinishing || isDestroyed) {
                    bitmap?.recycle()
                    return@runOnUiThread
                }
                if (bitmap == null) {
                    toast("截图解码失败")
                    return@runOnUiThread
                }
                if (!lifecycle.currentState.isAtLeast(androidx.lifecycle.Lifecycle.State.RESUMED)) {
                    bitmap.recycle()
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
     * 图标是电脑应用自己的 256×256 PNG，四张 base64 合计可达数百 KB；
     * 存 prefs 是为了冷启动没连上电脑时也能先显示上次的图标/名称。
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
        if (!micGranted()) {
            toast("没有麦克风权限，无法录音")
            return
        }
        if (!audioReady) {
            toast("电脑端虚拟声卡未就绪，请在电脑端设置页安装或重新探测声卡")
            return
        }
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

    /**
     * 把电脑端的切换浮层状态与 Tab 序号显示到对应按键上。
     *
     * 不新增控件：直接改「切换应用」「切换 Tab」两个键的标签与颜色，
     * 用户一眼能看出电脑现在停在浮层里（该用方向键+确定/取消）以及切到了第几个 Tab。
     */
    private fun applyModeAndTab(mode: String, index: Int) {
        val inSwitcher = mode == "switcher"
        if (inSwitcher != switcherMode) {
            switcherMode = inSwitcher
            binding.tvSwitcherLabel.text = if (inSwitcher) "浮层中" else "切换应用"
            binding.tvSwitcherLabel.setTextColor(
                ContextCompat.getColor(this, if (inSwitcher) R.color.accent else R.color.muted)
            )
            binding.btnSwitcher.contentDescription =
                if (inSwitcher) "切换应用，电脑当前停在切换浮层：方向键移动、确定选中、取消退出"
                else "切换应用"
        }
        if (index != tabIndex) {
            tabIndex = index
            binding.tvTabLabel.text = if (index > 0) "Tab $index" else "切换 Tab"
            binding.tvTabLabel.setTextColor(
                ContextCompat.getColor(this, if (index > 0) R.color.accent else R.color.muted)
            )
            binding.btnTabSwitch.contentDescription =
                if (index > 0) "切换 Tab，当前第 $index 个" else "切换 Tab"
        }
    }

    /** App 版本号，设备管理抽屉底部显示（用 PackageManager 取，不依赖 BuildConfig 开关）。 */
    private fun appVersion(): String = runCatching {
        packageManager.getPackageInfo(packageName, 0).versionName ?: "?"
    }.getOrDefault("?")

    /** 声卡就绪状态变化：刷新语音键；变为未就绪时把正在进行的语音会话收掉。 */
    private fun applyAudioReady(ready: Boolean) {
        if (ready == audioReady) return
        audioReady = ready
        if (!ready && (voiceOn || openMode)) {
            resetVoiceState()
            toast("电脑端虚拟声卡未就绪，语音已停止")
        }
        updateVoiceButton()
    }

    private fun updateVoiceButton() {
        val live = voiceOn || openMode
        val usable = micGranted() && audioReady
        binding.tvVoiceTitle.text = if (live) "说话中" else "按住说话"
        binding.tvVoiceSub.text = when {
            !micGranted() -> "无麦克风权限"
            !audioReady -> "电脑声卡未就绪"
            openMode -> "点击结束"
            voiceOn -> "松手/点击结束"
            else -> "双击常开"
        }
        // 不用 isEnabled=false：那样连按下缩放/震动都没了，用户不知道为啥按不动。
        // 改为半透明 + 副标题说明原因，点击时给明确提示。
        binding.btnVoice.alpha = if (usable) 1f else 0.5f
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
                // 没麦克风权限就不发 voice.down、也不起录音服务，只提示。
                // 按钮不做 isEnabled=false：那样连触摸/震动反馈都没了，用户不知道为啥按不动。
                if (!micGranted()) {
                    toast("没有麦克风权限，去系统设置开启后再用语音")
                    return@hold
                }
                // 声卡未就绪时不许开新会话；已在说话中则放行，让用户能正常结束
                if (!audioReady && !voiceOn && !openMode) {
                    toast("电脑端虚拟声卡未就绪，请在电脑端设置页安装或重新探测声卡")
                    return@hold
                }
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

    /**
     * singleTask 下从桌面图标 / 快捷开关磁贴再次点开不会新建实例，只会走到这里。
     * 顺便探一次活，让"点开就能用"不依赖上一次连接的残余状态。
     */
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        client.wakeUp()
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

        // 挂着的窗口全部收掉：Activity 销毁后抽屉/下拉/进度浮窗还贴在 WindowManager 上就是窗口泄漏
        devicePopup?.dismiss()
        devicePopup = null
        deviceSheet?.let { runCatching { it.dismiss() } }
        deviceSheet = null
        deviceListRefresh = null
        dismissTransferDialog()

        // 录音前台服务跟着界面走：界面销毁后 voiceOn/openMode 归零，服务若还在跑，
        // 就会出现"通知栏写着正在录音、界面却显示没开麦"的失同步。
        val stopRecorder = Intent(this, RecorderService::class.java).setAction(RecorderService.ACTION_STOP)
        runCatching { startService(stopRecorder) }
            // 已在后台时 startService 会抛 IllegalStateException，退回 stopService（onDestroy 里同样会停采集）
            .onFailure { runCatching { stopService(stopRecorder) } }

        // 传输前台服务必须晚于 startForegroundService 满 6 秒才能停（见 stopTransferService），
        // 不能跟着 uiHandler 一起清掉就完事：把"还没到点的那次停止"用 applicationContext 重排一遍，
        // 回调不再引用 Activity；其余延迟回调（进度浮窗、语音停录、连删）全部清空，避免继续持有已销毁的界面。
        val pendingTransferStop = transferServiceStarted && transferStopScheduled
        val transferStartedAt = transferServiceStartedAt
        uiHandler.removeCallbacksAndMessages(null)
        if (pendingTransferStop) {
            val appCtx = applicationContext
            val wait = (6000L - (android.os.SystemClock.elapsedRealtime() - transferStartedAt))
                .coerceAtLeast(600L)
            uiHandler.postDelayed({ runCatching { TransferService.stop(appCtx) } }, wait)
        }

        runCatching { unregisterReceiver(screenReceiver) }
        runCatching { unregisterReceiver(recorderFailReceiver) }
        client.close()
        super.onDestroy()
    }

    companion object {
        private const val REQ_PERM = 100

        /** 与电脑端 server.py 的 CLIP_MAX_CHARS 保持一致。 */
        private const val CLIP_MAX_CHARS = 100_000

        /** 二维码 host 白名单：IPv4 / 主机名（字母数字点连字符），首尾必须是字母数字。见 parseQr。 */
        private val HOST_PATTERN = Regex("^[A-Za-z0-9]([A-Za-z0-9.\\-]*[A-Za-z0-9])?$")
    }
}
