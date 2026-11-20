package com.willen.remotemic

import android.Manifest
import android.app.AlertDialog
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import com.willen.remotemic.audio.AudioBus
import com.willen.remotemic.databinding.ActivityMainBinding
import com.willen.remotemic.service.RecorderService
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

    private val prefs by lazy { getSharedPreferences("remote_mic", MODE_PRIVATE) }

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
        Toast.makeText(this, "已绑定 ${device.name}", Toast.LENGTH_SHORT).show()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        client = buildClient()

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
                        current = current!!.copy(name = computer)
                        renderHeader()
                    }
                }
                "screenshot" -> onScreenshot(obj)
                "apps" -> onAppsFrame(obj)
            }
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
        binding.tvDevice.text = device?.name ?: "未绑定设备 · 点击绑定"
        binding.statusText.text = when {
            connected && active -> "控制中"
            connected -> "待命"
            else -> "未连接"
        }
        binding.statusDot.setBackgroundColor(
            when {
                connected && active -> 0xFF4CAF50.toInt()
                connected -> 0xFFFFA000.toInt()
                else -> 0xFF9E9E9E.toInt()
            }
        )
    }

    private fun isOnline(device: Device): Boolean =
        connected && device.token == current?.token

    private fun dotText(online: Boolean): String = if (online) "●" else "○"

    private fun dotColor(online: Boolean): Int =
        if (online) 0xFF4CAF50.toInt() else 0xFF9E9E9E.toInt()

    /** 点顶部设备名：直接下拉已绑定设备列表，可当场切换被控设备。 */
    private fun showDeviceMenu() {
        if (devices.isEmpty()) {
            showBindingDialog()
            return
        }
        if (devices.size == 1) {
            val only = devices[0]
            toast("当前只有一台设备：${only.name}（${if (isOnline(only)) "在线" else "未连接"}）")
            return
        }
        val popup = android.widget.PopupMenu(this, binding.tvDevice)
        devices.forEachIndexed { index, device ->
            val online = isOnline(device)
            val label = "${dotText(online)}  ${device.name}"
            val item = android.text.SpannableString(label)
            item.setSpan(
                android.text.style.ForegroundColorSpan(dotColor(online)),
                0, 1,
                android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE
            )
            popup.menu.add(0, index, index, item)
        }
        popup.setOnMenuItemClickListener { item ->
            val device = devices[item.itemId]
            if (device.token == current?.token && connected) {
                toast("已经在控制 ${device.name}")
            } else {
                connectTo(device)
                toast("正在切换到 ${device.name}")
            }
            true
        }
        popup.show()
    }

    /** 设备管理页：列出已绑定电脑（圆点表示在线）、扫码新增、删除、切换。 */
    private fun showBindingDialog() {
        val ctx = this
        val scroll = ScrollView(ctx)
        val list = LinearLayout(ctx).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(32, 16, 32, 16)
        }
        scroll.addView(list)

        fun rebuild() {
            list.removeAllViews()
            if (devices.isEmpty()) {
                list.addView(TextView(ctx).apply { text = "还没有绑定任何电脑，点下方「扫码绑定」" })
            }
            devices.forEach { device ->
                val online = isOnline(device)
                val row = LinearLayout(ctx).apply {
                    orientation = LinearLayout.HORIZONTAL
                    gravity = android.view.Gravity.CENTER_VERTICAL
                    setPadding(0, 8, 0, 8)
                }
                val dot = TextView(ctx).apply {
                    text = dotText(online)
                    setTextColor(dotColor(online))
                    textSize = 18f
                    setPadding(0, 0, 12, 0)
                }
                val info = LinearLayout(ctx).apply { orientation = LinearLayout.VERTICAL }
                info.addView(TextView(ctx).apply {
                    text = if (online) "${device.name}（在线）" else "${device.name}（未连接）"
                    textSize = 16f
                })
                info.addView(TextView(ctx).apply {
                    text = device.endpoint
                    textSize = 12f
                    setTextColor(0xFF888888.toInt())
                })
                info.setOnClickListener {
                    if (online) {
                        Toast.makeText(ctx, "已经在控制 ${device.name}", Toast.LENGTH_SHORT).show()
                    } else {
                        connectTo(device)
                        Toast.makeText(ctx, "正在切换到 ${device.name}", Toast.LENGTH_SHORT).show()
                        rebuild()
                    }
                }
                val del = Button(ctx).apply {
                    text = "删除"
                    setOnClickListener {
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
                }
                row.addView(dot, LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT))
                row.addView(info, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
                row.addView(del, LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT))
                list.addView(row)
            }

            val actions = LinearLayout(ctx).apply { orientation = LinearLayout.VERTICAL }
            actions.addView(Button(ctx).apply {
                text = "扫码绑定新电脑"
                setOnClickListener { startScan() }
            })
            actions.addView(Button(ctx).apply {
                text = "手动输入 IP / token"
                setOnClickListener { showManualAdd() }
            })
            list.addView(actions)
        }
        rebuild()

        AlertDialog.Builder(this)
            .setTitle("设备管理")
            .setView(scroll)
            .setNegativeButton("关闭", null)
            .show()
    }

    private fun startScan() {
        // 自建竖屏扫码页（CameraX + ZXing core），替换掉原来横屏的 journeyapps CaptureActivity
        scanLauncher.launch(
            Intent(this, com.willen.remotemic.scan.QrScanActivity::class.java)
        )
    }

    private fun showManualAdd() {
        val view = layoutInflater.inflate(R.layout.dialog_settings, null)
        val hostEdit = view.findViewById<EditText>(R.id.editHost)
        val portEdit = view.findViewById<EditText>(R.id.editPort)
        val tokenEdit = view.findViewById<EditText>(R.id.editToken)
        hostEdit.setText(current?.host ?: "192.168.1.100")
        portEdit.setText((current?.port ?: 8765).toString())
        tokenEdit.setText(current?.token ?: "")
        AlertDialog.Builder(this)
            .setTitle("手动添加设备")
            .setView(view)
            .setPositiveButton("连接") { _, _ ->
                val host = hostEdit.text.toString().trim()
                val port = portEdit.text.toString().trim().toIntOrNull() ?: 8765
                val token = tokenEdit.text.toString().trim()
                if (host.isBlank() || token.isBlank()) {
                    Toast.makeText(this, "IP 和 token 都要填", Toast.LENGTH_SHORT).show()
                    return@setPositiveButton
                }
                val device = Device(name = host, host = host, port = port, token = token)
                devices.removeAll { it.token == token || it.endpoint == device.endpoint }
                devices.add(device)
                saveDevices()
                connectTo(device)
            }
            .setNegativeButton("取消", null)
            .show()
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
        button.textSize = 12f
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
        button.iconGravity = com.google.android.material.button.MaterialButton.ICON_GRAVITY_TEXT_START
        button.iconSize = (22 * density).toInt()
        button.iconPadding = (6 * density).toInt()
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
        binding.btnVoice.text = when {
            openMode -> "点击结束"
            voiceOn -> "说话中…"
            else -> "按住说话·双击常开"
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

    private fun bindButtons() {
        binding.btnSettings.setOnClickListener { showBindingDialog() }
        binding.tvDevice.setOnClickListener { showDeviceMenu() }

        binding.btnUp.press { send("key", "code" to "up", "action" to "down") }
        binding.btnDown.press { send("key", "code" to "down", "action" to "down") }
        binding.btnLeft.press { send("key", "code" to "left", "action" to "down") }
        binding.btnRight.press { send("key", "code" to "right", "action" to "down") }
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
        binding.btnDelete.click { send("key", "code" to "delete", "action" to "down") }

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

    override fun onResume() {
        super.onResume()
        if (firstResume) {
            firstResume = false          // 首次进入不打扰正在建立的首连
        } else if (!connected) {
            client.wakeUp()
        }
    }

    override fun onDestroy() {
        cancelPendingVoiceStop()
        AudioBus.onPcm = null
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
