package com.willen.remotemic

import android.Manifest
import android.app.AlertDialog
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
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
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

    private val devices = mutableListOf<Device>()
    private var current: Device? = null

    private var connected = false
    private var active = false
    private var voiceOn = false
    private var openMode = false
    private var selectMode = false

    private val uiHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private var pendingVoiceStop: Runnable? = null
    private var lastVoiceUpAt = 0L
    private var suppressStopOnce = false

    private val prefs by lazy { getSharedPreferences("remote_mic", MODE_PRIVATE) }

    // 扫码绑定：扫到 PC 设置页的二维码后新增设备并立刻连接
    private val scanLauncher = registerForActivityResult(ScanContract()) { result ->
        val content = result.contents
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
        updateVoiceButton()
        renderHeader()

        val saved = devices.firstOrNull { it.token == prefs.getString("current_token", "") }
            ?: devices.firstOrNull()
        if (saved != null) {
            connectTo(saved)
        } else {
            showBindingDialog()
        }
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
            resetSelectMode()
            renderHeader()
        }

        override fun onMessage(text: String) {
            val obj = runCatching { JSONObject(text) }.getOrNull() ?: return
            if (obj.optString("type") == "status") {
                active = obj.optBoolean("active", active)
                val computer = obj.optString("computer", "")
                if (computer.isNotBlank() && current != null) {
                    current = current!!.copy(name = computer)
                    renderHeader()
                }
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
        binding.tvDevice.text = when {
            device == null -> "未绑定设备 · 点击绑定"
            connected && active -> "${device.name} · 已连接"
            connected -> "${device.name} · 待命（控制权在其他遥控器）"
            else -> "${device.name} · 未连接"
        }
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

    /** 设备绑定页：列出已绑定电脑、扫码新增、删除、切换。 */
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
                val row = LinearLayout(ctx).apply {
                    orientation = LinearLayout.HORIZONTAL
                    setPadding(0, 6, 0, 6)
                }
                val mark = if (device.token == current?.token) "● " else ""
                val pick = Button(ctx).apply {
                    text = "$mark${device.name}\n${device.endpoint}"
                    isAllCaps = false
                    setOnClickListener {
                        connectTo(device)
                        Toast.makeText(ctx, "已切换到 ${device.name}", Toast.LENGTH_SHORT).show()
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
                row.addView(pick, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
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
            .setTitle("设备绑定")
            .setView(scroll)
            .setNegativeButton("关闭", null)
            .show()
    }

    private fun startScan() {
        val opts = ScanOptions().apply {
            setDesiredBarcodeFormats(ScanOptions.QR_CODE)
            setPrompt("对准电脑端设置页的绑定二维码")
            setBeepEnabled(false)
            setOrientationLocked(false)
        }
        scanLauncher.launch(opts)
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

    private fun resetSelectMode() {
        selectMode = false
        binding.btnCopy.text = "复制"
    }

    // ------------------------------------------------------------------ 按钮

    private fun bindButtons() {
        binding.btnSettings.setOnClickListener { showBindingDialog() }
        binding.tvDevice.setOnClickListener { showBindingDialog() }

        binding.btnUp.press { send("key", "code" to "up", "action" to "down") }
        binding.btnDown.press { send("key", "code" to "down", "action" to "down") }
        binding.btnLeft.press { send("key", "code" to "left", "action" to "down") }
        binding.btnRight.press { send("key", "code" to "right", "action" to "down") }
        binding.btnOk.click {
            send("key", "code" to "ok", "action" to "down")
            if (selectMode) resetSelectMode()
        }
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

        binding.btnCopy.click {
            selectMode = !selectMode
            if (selectMode) {
                send("select", "action" to "enter")
                binding.btnCopy.text = "退出选区"
            } else {
                send("select", "action" to "exit")
                binding.btnCopy.text = "复制"
            }
        }
        binding.btnPaste.click { send("key", "code" to "paste", "action" to "down") }
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

    override fun onDestroy() {
        cancelPendingVoiceStop()
        AudioBus.onPcm = null
        client.close()
        super.onDestroy()
    }

    companion object {
        private const val REQ_PERM = 100
    }
}
