package com.willen.remotemic.ws

import android.os.Handler
import android.os.Looper
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.Buffer
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * WebSocket 客户端：连接 -> token 认证 -> 就绪；断线自动重连。
 * 认证失败不再重连，交由界面提示重新绑定。
 *
 * 不做后台常驻保活：熄屏后系统会挂起网络，socket 必然死掉（用户也不需要熄屏遥控）。
 * 取而代之的是 [wakeUp]：亮屏 / 解锁 / 回到界面时立刻探一次活，socket 已死就马上重连，
 * 让"解锁后能马上用"而不是等 3 秒退避周期。
 */
class RemoteWsClient(private val listener: Listener) {

    interface Listener {
        /** 认证通过且已就绪；active=false 表示已连上但控制权在别的遥控器手上 */
        fun onReady(active: Boolean)
        fun onDisconnected()
        fun onMessage(text: String)
        fun onAuthFailed(reason: String)
    }

    private val client = OkHttpClient.Builder()
        .pingInterval(10, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private var ws: WebSocket? = null
    private var stopped = false
    private var currentUrl: String? = null
    private var token: String = ""
    private var device: String = ""

    @Volatile private var ready = false
    @Volatile private var lastRxAt = 0L
    @Volatile private var lastConnectAt = 0L
    private val main = Handler(Looper.getMainLooper())
    private val reconnectRunnable = Runnable { open() }

    fun connect(host: String, port: Int, authToken: String, deviceName: String) {
        stopped = false
        token = authToken
        device = deviceName
        currentUrl = "ws://$host:$port"
        open()
    }

    private fun open() {
        val url = currentUrl ?: return
        main.removeCallbacks(reconnectRunnable)
        val old = ws
        ws = null
        old?.cancel()               // 旧 socket 先关掉，避免两条连接同时在线
        ready = false
        lastConnectAt = System.currentTimeMillis()
        val request = Request.Builder().url(url).build()
        ws = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(TAG, "ws open, authenticating")
                lastRxAt = System.currentTimeMillis()
                val auth = JSONObject()
                    .put("type", "auth")
                    .put("token", token)
                    .put("device", device)
                webSocket.send(auth.toString())
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                lastRxAt = System.currentTimeMillis()
                Log.i(TAG, "ws message: $text")
                val obj = runCatching { JSONObject(text) }.getOrNull()
                when (obj?.optString("type")) {
                    "auth_ok" -> {
                        ready = true
                        main.post { notifyReady(webSocket, obj.optBoolean("active", false)) }
                    }
                    "auth_fail" -> {
                        stopped = true
                        val reason = obj.optString("reason", "auth_failed")
                        main.post { listener.onAuthFailed(reason) }
                    }
                    "pong" -> Unit          // 探活回包，lastRxAt 已经更新
                    else -> main.post { listener.onMessage(text) }
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "ws closed: $code $reason")
                ready = false
                scheduleReconnect()
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "ws failure: ${t.message}")
                ready = false
                scheduleReconnect()
            }
        })
    }

    private fun notifyReady(websocket: WebSocket, active: Boolean) {
        if (ws !== websocket) return
        listener.onReady(active)
    }

    private fun scheduleReconnect() {
        main.post { listener.onDisconnected() }
        if (stopped) return
        main.removeCallbacks(reconnectRunnable)
        main.postDelayed(reconnectRunnable, RECONNECT_DELAY_MS)
    }

    /**
     * 亮屏 / 解锁 / 回到界面时调用：
     * - 没连上 → 立刻重连（不等 3 秒退避）
     * - 已连上 → 发一个应用层 ping，3 秒内没有任何回包就判定 socket 已死并重连
     */
    fun wakeUp() {
        if (stopped) return
        if (!ready) {
            // SCREEN_ON 与 USER_PRESENT 会前后脚到达，避免重复 cancel/重开造成额外握手
            if (System.currentTimeMillis() - lastConnectAt < 1500) return
            Log.i(TAG, "wakeUp: not ready -> reconnect now")
            main.removeCallbacks(reconnectRunnable)
            open()
            return
        }
        val sentAfter = lastRxAt
        ws?.send("{\"type\":\"ping\",\"ts\":${System.currentTimeMillis()}}")
        main.postDelayed({
            if (stopped || !ready) return@postDelayed
            if (lastRxAt <= sentAfter) {
                Log.i(TAG, "wakeUp: no pong in 3s -> reconnect")
                open()
            }
        }, PROBE_TIMEOUT_MS)
    }

    fun sendText(json: String): Boolean = ws?.send(json) ?: false

    fun sendBinary(bytes: ByteArray): Boolean {
        val buffer = Buffer().write(bytes)
        return ws?.send(buffer.readByteString()) ?: false
    }

    fun close() {
        stopped = true
        ready = false
        main.removeCallbacksAndMessages(null)
        ws?.close(1000, "bye")
        ws = null
    }

    companion object {
        private const val TAG = "RemoteWsClient"
        private const val RECONNECT_DELAY_MS = 3000L
        private const val PROBE_TIMEOUT_MS = 3000L
    }
}
