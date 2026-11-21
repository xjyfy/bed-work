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
import okio.ByteString
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * WebSocket 客户端：连接 -> token 认证 -> 就绪；断线自动重连（指数退避）。
 * 认证失败不再重连，交由界面提示重新绑定。
 *
 * 不做后台常驻保活：熄屏后系统会挂起网络，socket 必然死掉（用户也不需要熄屏遥控）。
 * 取而代之的是 [wakeUp]：亮屏 / 解锁 / 回到界面时立刻探一次活，socket 已死就马上重连，
 * 让"解锁后能马上用"而不是等退避周期。
 *
 * **同一时刻只维持一条连接**：服务端对同一台遥控器是「踢旧保新」，客户端一旦叠出两条，
 * 两条会互相把对方顶掉，形成「每 20 秒断一次」的重连风暴；被顶掉的那条还会让截图、
 * 文件回包发不出去（表现为按键能用但截屏/传文件没反应）。因此这里做了三件事：
 *   1. 建连单飞（[connecting]）+ 代际号（[generation]）：旧 socket 的迟到回调一律作废；
 *   2. 用 cancel() 而不是 close() 收掉旧 socket，立即断开、不用等对端 close 帧；
 *   3. 重连指数退避 3s→5s→10s→20s→30s，服务端因「被顶掉」而关连接时退避 10s。
 */
class RemoteWsClient(private val listener: Listener) {

    interface Listener {
        /** 认证通过且已就绪；active=false 表示已连上但控制权在别的遥控器手上 */
        fun onReady(active: Boolean)
        fun onDisconnected()
        fun onMessage(text: String)
        fun onAuthFailed(reason: String)
        /** 二进制帧（文件块，MAGIC 开头）；默认空实现，只有文件传输会用到 */
        fun onBinary(bytes: ByteArray) {}
    }

    // 全进程共用一个 OkHttpClient：每个实例自带独立的线程池与连接池，close() 只关 socket
    // 不会释放它们；切换设备时每次新建就会不断累积线程/连接。配置与原来完全一致。
    private val client = sharedClient

    // 这几个字段在 OkHttp 回调线程与主线程之间读写（webSocket !== ws 的身份判重尤其敏感），
    // 不加 @Volatile 会读到旧值，导致旧 socket 的回调被当成当前连接处理。
    @Volatile private var ws: WebSocket? = null
    @Volatile private var stopped = false
    @Volatile private var currentUrl: String? = null
    @Volatile private var token: String = ""
    @Volatile private var device: String = ""

    @Volatile private var ready = false
    @Volatile private var lastRxAt = 0L
    @Volatile private var lastConnectAt = 0L

    /** 建连代际：只有最新一代的回调才作数，旧 socket 的迟到回调全部忽略。 */
    private val generation = AtomicInteger(0)

    /** 建连单飞：同一时刻只允许一次建连在进行（cancel 是异步的，否则会叠出多条连接）。 */
    private val connecting = AtomicBoolean(false)

    /** 当前重连退避时长；连上后回到初始值，避免"断一次就永久 30 秒"。 */
    @Volatile private var backoffMs = RECONNECT_DELAY_MS

    private val main = Handler(Looper.getMainLooper())
    private val reconnectRunnable = Runnable { open(force = false) }

    private var probeSentAt = 0L
    private val probeRunnable = Runnable {
        if (stopped || !ready) return@Runnable
        if (lastRxAt <= probeSentAt) {
            Log.i(TAG, "wakeUp: no pong in 3s -> reconnect")
            open(force = true)          // 半死 socket：不等退避，直接重建
        }
    }

    fun connect(host: String, port: Int, authToken: String, deviceName: String) {
        stopped = false
        token = authToken
        device = deviceName
        currentUrl = "ws://$host:$port"
        backoffMs = RECONNECT_DELAY_MS
        open(force = true)
    }

    /**
     * @param force true = 无条件重建（切设备 / 探活失败）；false = 已有连接或正在建连就跳过。
     */
    private fun open(force: Boolean) {
        val url = currentUrl ?: return
        if (stopped) return

        main.removeCallbacks(reconnectRunnable)
        main.removeCallbacks(probeRunnable)

        if (!force) {
            // 已经有一条 socket（就绪或正在认证）就别再叠一条 —— 叠出来的第二条会被服务端
            // 当成"新连接"把第一条踢掉，然后两条互相踢，就是那个 20 秒一轮的重连风暴。
            if (ready || ws != null || connecting.get()) return
        }
        if (!connecting.compareAndSet(false, true)) return

        val old = ws
        ws = null
        ready = false
        // cancel() 立即断；close() 是优雅关闭，要等对端回 close 帧，socket 会以 ESTABLISHED 挂很久
        old?.cancel()

        val gen = generation.incrementAndGet()
        lastConnectAt = System.currentTimeMillis()
        val request = Request.Builder().url(url).build()
        ws = client.newWebSocket(request, listenerOf(gen))
    }

    private fun listenerOf(gen: Int) = object : WebSocketListener() {

        /** 只有"当前那条 + 当前这一代"的回调才作数。 */
        private fun isCurrent(socket: WebSocket) = socket === ws && gen == generation.get()

        override fun onOpen(webSocket: WebSocket, response: Response) {
            if (!isCurrent(webSocket)) return
            connecting.set(false)
            Log.i(TAG, "ws open, authenticating")
            lastRxAt = System.currentTimeMillis()
            val auth = JSONObject()
                .put("type", "auth")
                .put("token", token)
                .put("device", device)
            webSocket.send(auth.toString())
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            if (!isCurrent(webSocket)) return
            lastRxAt = System.currentTimeMillis()
            val array = bytes.toByteArray()
            // 文件块直接在 IO 线程处理（边收边写盘），不切主线程，避免大文件把队列撑爆
            listener.onBinary(array)
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            if (!isCurrent(webSocket)) return
            lastRxAt = System.currentTimeMillis()
            val obj = runCatching { JSONObject(text) }.getOrNull()
            // 只打类型和长度：截屏帧的 base64 有几百 KB、apps 帧带 4 个图标，
            // 全文进 logcat 会瞬间刷爆缓冲区，也把画面内容泄进了日志。
            Log.i(TAG, "ws message: type=${obj?.optString("type") ?: "?"} len=${text.length}")
            when (obj?.optString("type")) {
                "auth_ok" -> {
                    ready = true
                    connecting.set(false)
                    backoffMs = RECONNECT_DELAY_MS
                    main.post { notifyReady(webSocket, obj.optBoolean("active", false)) }
                    // 认证帧里带 files_allowed，界面需要据此决定传文件按钮是否可用
                    main.post { listener.onMessage(text) }
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
            // 关键：旧 socket 被 cancel/替换后仍会回调，若不判身份就会触发
            // "重连 → 取消新连接 → 新连接又回调 → 再重连" 的死循环。
            if (!isCurrent(webSocket)) return
            Log.i(TAG, "ws closed: $code $reason")
            failAndMaybeReconnect(replaced = code == CLOSE_REPLACED)
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            if (!isCurrent(webSocket)) return
            Log.w(TAG, "ws failure: ${t.message}")
            failAndMaybeReconnect(replaced = false)
        }
    }

    private fun notifyReady(websocket: WebSocket, active: Boolean) {
        if (ws !== websocket) return
        listener.onReady(active)
    }

    /**
     * 连接结束（正常关闭 / 异常 / 被服务端顶掉）后的收尾与重连安排。
     * replaced=true 表示服务端把这条连接当成"同一台设备的旧连接"踢掉了：
     * 此时对方已经接管，重连要退让久一点（10 秒），且不叠加退避，避免两条连接来回互踢。
     */
    private fun failAndMaybeReconnect(replaced: Boolean) {
        ws = null
        ready = false
        connecting.set(false)
        main.removeCallbacks(probeRunnable)
        main.post { listener.onDisconnected() }
        if (stopped) return

        main.removeCallbacks(reconnectRunnable)
        val delay = if (replaced) REPLACED_DELAY_MS else backoffMs
        Log.i(TAG, "reconnect in ${delay}ms")
        main.postDelayed(reconnectRunnable, delay)
        if (!replaced) backoffMs = minOf(backoffMs * 2, MAX_BACKOFF_MS)
    }

    /**
     * 亮屏 / 解锁 / 回到界面时调用：
     * - 没连上 → 立刻重连（不等退避）
     * - 已连上 → 发一个应用层 ping，3 秒内没有任何回包就判定 socket 已死并重连
     */
    fun wakeUp() {
        if (stopped) return
        if (!ready) {
            // SCREEN_ON 与 USER_PRESENT 会前后脚到达，避免重复 cancel/重开造成额外握手
            if (System.currentTimeMillis() - lastConnectAt < 1500) return
            Log.i(TAG, "wakeUp: not ready -> reconnect now")
            main.removeCallbacks(reconnectRunnable)
            open(force = true)
            return
        }
        probeSentAt = lastRxAt
        ws?.send("{\"type\":\"ping\",\"ts\":${System.currentTimeMillis()}}")
        main.removeCallbacks(probeRunnable)
        main.postDelayed(probeRunnable, PROBE_TIMEOUT_MS)
    }

    fun sendText(json: String): Boolean = ws?.send(json) ?: false

    /** 待发送队列的字节数。OkHttp 的队列无上限，发送侧据此做背压，避免大文件把内存堆爆。 */
    fun queueSize(): Long = ws?.queueSize() ?: 0L

    fun sendBinary(bytes: ByteArray): Boolean {
        val buffer = Buffer().write(bytes)
        return ws?.send(buffer.readByteString()) ?: false
    }

    fun close() {
        stopped = true
        ready = false
        connecting.set(false)
        main.removeCallbacksAndMessages(null)
        generation.incrementAndGet()        // 让在飞的旧 socket 回调全部作废
        ws?.cancel()                        // 立即断，不留 ESTABLISHED 残留
        ws = null
    }

    companion object {
        private const val TAG = "RemoteWsClient"
        private const val RECONNECT_DELAY_MS = 3000L
        private const val PROBE_TIMEOUT_MS = 3000L

        /** 被服务端「踢旧保新」关掉时的 close code（见 pc-service `server.py`）。 */
        private const val CLOSE_REPLACED = 4001

        /** 被顶掉后让位一段时间，避免两条客户端来回互踢。 */
        private const val REPLACED_DELAY_MS = 10_000L
        private const val MAX_BACKOFF_MS = 30_000L

        /** 见 [client]：所有 RemoteWsClient 实例共用，空闲线程/连接由 OkHttp 自己按超时回收。 */
        private val sharedClient: OkHttpClient by lazy {
            OkHttpClient.Builder()
                .pingInterval(10, TimeUnit.SECONDS)
                .retryOnConnectionFailure(true)
                .build()
        }
    }
}
