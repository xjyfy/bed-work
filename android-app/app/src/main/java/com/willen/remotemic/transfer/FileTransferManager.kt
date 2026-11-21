package com.willen.remotemic.transfer

import android.Manifest
import android.content.ContentValues
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import android.provider.OpenableColumns
import android.util.Log
import androidx.core.content.ContextCompat
import com.willen.remotemic.ws.RemoteWsClient
import org.json.JSONObject
import java.io.File
import java.io.OutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 手机 ↔ 电脑文件互传。
 *
 * 协议（和电脑端 `pc-service/remote_mic/files.py` 对齐）：
 * - 文本帧：`file_begin / file_ack / file_reject / file_end / file_result`（手机→电脑）
 *   、`file_offer / file_accept / file_end`（电脑→手机）；
 * - 二进制块：`MAGIC("FCHN")` + 数据，256KB 一块，用来和语音 PCM 帧区分。
 *
 * 权限：选文件走系统控件（Photo Picker / SAF），零存储权限；
 * 接文件用 MediaStore.Downloads 写入 `Download/RemoteMic`，同样零权限。
 */
class FileTransferManager(
    private val context: Context,
    private val clientProvider: () -> RemoteWsClient,
    private val onEvent: (title: String, message: String) -> Unit
) {

    /** 每次重连都会换一个新的 ws 客户端，所以这里每次现取，不能缓存旧引用。 */
    private val client: RemoteWsClient get() = clientProvider()

    companion object {
        const val MAGIC = "FCHN"
        const val CHUNK = 256 * 1024
        const val MAX_FILE_BYTES = 2L * 1024 * 1024 * 1024
        const val MAX_COUNT = 50
        private const val TAG = "FileTransfer"
        private const val SUB_DIR = "RemoteMic"

        /** 等电脑端 file_ack / file_reject 的上限。 */
        private const val ACK_TIMEOUT_MS = 10_000L

        /** 发送队列水位上限（约 8 个块）；超过就等电脑端消费，别在内存里无限堆。 */
        private const val MAX_QUEUE_BYTES = 2L * 1024 * 1024

        /** 队列持续不回落就判停滞，避免连接已死时死等。 */
        private const val STALL_TIMEOUT_MS = 30_000L

        private val RESERVED = setOf("con", "prn", "aux", "nul") +
            (1..9).map { "com$it" } + (1..9).map { "lpt$it" }
    }

    /** 电脑端是否允许这台遥控器传文件（auth_ok 与 files_allowed 帧同步过来）。 */
    @Volatile
    var allowed: Boolean = true

    @Volatile
    private var cancelFlag = false
    private var sendThread: Thread? = null
    private var sendingCount = 0

    private class ReceiveSession(val transferId: String, val name: String, val size: Long) {
        var stream: OutputStream? = null
        var uri: Uri? = null
        var file: File? = null
        var done = 0L
        var failed = false
    }

    /** 发送握手闸门：电脑端回 file_ack 才推块，回 file_reject 就立刻停。 */
    private class SendAck(val tid: String) {
        val latch = CountDownLatch(1)
        @Volatile var rejected: String? = null
    }

    @Volatile
    private var sendAck: SendAck? = null

    @Volatile
    private var receive: ReceiveSession? = null

    // ------------------------------------------------------------------ 发送

    fun isBusy(): Boolean = sendThread?.isAlive == true || receive != null

    /** 用户点了「传照片」或「传文件」后拿到 URI 列表，从这里开始发。 */
    fun sendUris(uris: List<Uri>) {
        if (uris.isEmpty()) return
        if (!allowed) {
            onEvent("无法发送", "电脑端已关闭这台遥控器的文件接收")
            return
        }
        if (uris.size > MAX_COUNT) {
            onEvent("文件太多", "一次最多 $MAX_COUNT 个文件，本次选了 ${uris.size} 个")
            return
        }
        if (isBusy()) {
            onEvent("正在传输", "上一个传输还没结束，稍等一下")
            return
        }
        cancelFlag = false
        sendingCount = uris.size
        sendThread = Thread({ runSend(uris) }, "file-send").apply { isDaemon = true; start() }
    }

    private fun runSend(uris: List<Uri>) {
        val total = uris.size
        var failure: String? = null
        var doneCount = 0
        for ((idx, uri) in uris.withIndex()) {
            if (cancelFlag) break
            val meta = queryMeta(uri)
            if (cancelFlag) break
            if (meta.second > MAX_FILE_BYTES) {
                failure = "${meta.first} 超过 2GB 上限"
                break
            }
            if (meta.second <= 0) {
                failure = "读不到 ${meta.first} 的大小"
                break
            }
            val tid = "u" + System.nanoTime().toString(16)
            val ack = SendAck(tid)
            sendAck = ack
            client.sendText(
                JSONObject()
                    .put("type", "file_begin").put("transfer_id", tid)
                    .put("name", meta.first).put("size", meta.second)
                    .put("mime", meta.third).put("index", idx + 1).put("count", total)
                    .toString()
            )
            // 必须等电脑端表态再推块：开关关了 / 超 2GB / 超 50 个都会回 file_reject，
            // 不等就会把整个文件白推一遍，还把它算进「发送完成」。
            val answered = runCatching { ack.latch.await(ACK_TIMEOUT_MS, TimeUnit.MILLISECONDS) }
                .getOrDefault(false)
            sendAck = null
            if (cancelFlag) {
                client.sendText(JSONObject().put("type", "file_abort").put("transfer_id", tid)
                    .put("reason", "手机取消").toString())
                break
            }
            if (!answered) {
                // 电脑端可能已经建好 .part 在等块，告诉它别等了
                client.sendText(JSONObject().put("type", "file_abort").put("transfer_id", tid)
                    .put("reason", "等待回应超时").toString())
                failure = "电脑端没有回应 ${meta.first}"
                break
            }
            val rejected = ack.rejected
            if (rejected != null) {
                failure = rejected
                break
            }
            val buffer = ByteArray(CHUNK + 4)
            buffer[0] = 'F'.code.toByte(); buffer[1] = 'C'.code.toByte()
            buffer[2] = 'H'.code.toByte(); buffer[3] = 'N'.code.toByte()
            var sent = 0L
            var ok = true
            try {
                context.contentResolver.openInputStream(uri)?.use { input ->
                    while (!cancelFlag) {
                        val n = input.read(buffer, 4, CHUNK)
                        if (n <= 0) break
                        val payload = if (n == CHUNK) buffer else buffer.copyOf(n + 4)
                        if (!awaitQueueRoom()) {
                            ok = false
                            if (!cancelFlag) failure = "电脑端接收停滞，已停止发送"
                            break
                        }
                        if (!client.sendBinary(payload)) {
                            ok = false
                            failure = "连接已断开"
                            break
                        }
                        sent += n
                        if (sent % (CHUNK * 4) < CHUNK || sent >= meta.second) {
                            TransferBus.publish(
                                TransferProgress(
                                    sending = true, name = meta.first, index = idx + 1, count = total,
                                    bytesDone = sent, bytesTotal = meta.second
                                )
                            )
                        }
                    }
                } ?: run {
                    ok = false
                    failure = "无法读取 ${meta.first}"
                }
            } catch (e: Exception) {
                Log.w(TAG, "read uri failed: ${e.message}")
                ok = false
                failure = e.message ?: "读取失败"
            }
            if (cancelFlag) {
                client.sendText(JSONObject().put("type", "file_abort").put("transfer_id", tid)
                    .put("reason", "手机取消").toString())
                break
            }
            if (!ok) {
                client.sendText(JSONObject().put("type", "file_abort").put("transfer_id", tid)
                    .put("reason", failure ?: "读取失败").toString())
                break
            }
            client.sendText(JSONObject().put("type", "file_end").put("transfer_id", tid).toString())
            doneCount++
            if (sent < meta.second) {
                failure = "${meta.first} 只发出 ${sent}/${meta.second} 字节"
                break
            }
        }

        val result = when {
            failure != null -> TransferProgress(
                sending = true, name = "", index = doneCount, count = total,
                finished = true, error = failure
            )
            cancelFlag -> TransferProgress(
                sending = true, name = "", index = doneCount, count = total,
                finished = true, error = "已取消"
            )
            else -> TransferProgress(
                sending = true, name = "", index = doneCount, count = total,
                finished = true
            )
        }
        TransferBus.publish(result)
        val failText = failure
        if (failText != null) onEvent("传输失败", failText)
        else if (!cancelFlag) onEvent("发送完成", "已发送 $doneCount 个文件到电脑")
        sendingCount = 0
    }

    /** 等发送队列回落到水位以下；取消或长时间停滞返回 false。 */
    private fun awaitQueueRoom(): Boolean {
        val deadline = System.currentTimeMillis() + STALL_TIMEOUT_MS
        while (client.queueSize() > MAX_QUEUE_BYTES) {
            if (cancelFlag) return false
            if (System.currentTimeMillis() > deadline) return false
            try {
                Thread.sleep(20)
            } catch (_: InterruptedException) {
                return false
            }
        }
        return true
    }

    fun cancel() {
        cancelFlag = true
        // 放行正在等握手的发送线程，别让它白等 10 秒
        sendAck?.latch?.countDown()
        val rx = receive
        if (rx != null) {
            receive = null
            // 必须告诉电脑端停发：只清本地的话它会继续把整个文件推完并报成功
            client.sendText(JSONObject().put("type", "file_end").put("transfer_id", rx.transferId)
                .put("ok", false).put("reason", "手机取消接收").toString())
            finishReceive(rx, ok = false, reason = "已取消")
        }
    }

    // ------------------------------------------------------------------ 接收（电脑→手机）

    fun onText(obj: JSONObject) {
        when (obj.optString("type")) {
            "files_allowed" -> allowed = obj.optBoolean("allow", true)
            "file_offer" -> beginReceive(
                obj.optString("transfer_id"),
                obj.optString("name"),
                obj.optLong("size"),
                obj.optString("mime"),
                obj.optInt("index", 1),
                obj.optInt("count", 1)
            )
            "file_end" -> {
                val rx = receive ?: return
                receive = null
                finishReceive(rx, ok = obj.optBoolean("ok", true), reason = obj.optString("reason"))
            }
            "file_ack" -> {
                val gate = sendAck
                if (gate != null && gate.tid == obj.optString("transfer_id")) {
                    gate.latch.countDown()
                }
            }
            "file_reject" -> {
                val reason = obj.optString("reason").ifBlank { "电脑端拒收" }
                val gate = sendAck
                val tid = obj.optString("transfer_id")
                if (gate != null && (tid.isEmpty() || gate.tid == tid)) {
                    // 正在等握手：交给发送线程收尾，进度与提示由它统一发
                    gate.rejected = reason
                    gate.latch.countDown()
                } else {
                    TransferBus.publish(TransferProgress(true, finished = true, error = reason))
                    onEvent("电脑端拒收", reason)
                }
            }
            "file_result" -> {
                if (!obj.optBoolean("ok", true)) {
                    val reason = obj.optString("reason")
                    onEvent("电脑端接收失败", reason)
                }
            }
        }
    }

    private fun beginReceive(transferId: String, name: String, size: Long, mime: String, index: Int, count: Int) {
        if (!allowed) {
            client.sendText(JSONObject().put("type", "file_end").put("transfer_id", transferId)
                .put("ok", false).put("reason", "本机已关闭接收").toString())
            return
        }
        if (size > MAX_FILE_BYTES) {
            client.sendText(JSONObject().put("type", "file_end").put("transfer_id", transferId)
                .put("ok", false).put("reason", "超过 2GB").toString())
            return
        }
        if (receive != null) {
            // 上一轮没收到 file_end（异常），先收尾再开新的
            receive?.let { finishReceive(it, ok = false, reason = "上传输未完成") }
        }
        // 文件名来自电脑端帧，不能直接当路径用：`../` 能写出下载目录
        val safeName = sanitizeName(name)
        val session = ReceiveSession(transferId, safeName, size)
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                val values = ContentValues().apply {
                    put(MediaStore.Downloads.DISPLAY_NAME, safeName)
                    put(MediaStore.Downloads.MIME_TYPE, mime.ifBlank { "application/octet-stream" })
                    put(MediaStore.Downloads.RELATIVE_PATH, "${Environment.DIRECTORY_DOWNLOADS}/$SUB_DIR")
                    put(MediaStore.Downloads.IS_PENDING, 1)
                }
                val uri = context.contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                    ?: throw IllegalException("无法在下载目录创建文件")
                session.uri = uri
                session.stream = context.contentResolver.openOutputStream(uri)
            } else {
                // Android 9 及以下写公共下载目录要 WRITE_EXTERNAL_STORAGE，
                // 没授权就别静默失败，明确告诉电脑端原因
                if (ContextCompat.checkSelfPermission(context, Manifest.permission.WRITE_EXTERNAL_STORAGE)
                    != PackageManager.PERMISSION_GRANTED) {
                    throw IllegalException("Android 9 及以下接收文件需要存储权限，请在系统设置里授予")
                }
                val dir = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), SUB_DIR)
                dir.mkdirs()
                val target = uniqueFile(dir, safeName)
                session.file = target
                session.stream = target.outputStream()
            }
        } catch (e: Exception) {
            Log.w(TAG, "create sink failed: ${e.message}")
            onEvent("接收失败", "无法写入下载目录：${e.message}")
            client.sendText(JSONObject().put("type", "file_end").put("transfer_id", transferId)
                .put("ok", false).put("reason", "手机端无法写入").toString())
            return
        }
        receive = session
        client.sendText(JSONObject().put("type", "file_accept").put("transfer_id", transferId).toString())
        TransferBus.publish(
            TransferProgress(sending = false, name = safeName, index = index, count = count,
                bytesDone = 0, bytesTotal = size)
        )
    }

    /** 二进制帧：MAGIC 开头才算文件块，否则是语音回传（这里不处理）。 */
    fun onBinary(bytes: ByteArray) {
        val rx = receive ?: return
        if (bytes.size < 4) return
        if (!(bytes[0] == 'F'.code.toByte() && bytes[1] == 'C'.code.toByte() &&
                bytes[2] == 'H'.code.toByte() && bytes[3] == 'N'.code.toByte())) return
        val payload = bytes.copyOfRange(4, bytes.size)
        try {
            rx.stream?.write(payload)
            rx.done += payload.size
            TransferBus.publish(
                TransferProgress(sending = false, name = rx.name, bytesDone = rx.done,
                    bytesTotal = rx.size, index = 1, count = 1)
            )
        } catch (e: Exception) {
            Log.w(TAG, "write failed: ${e.message}")
            receive = null
            finishReceive(rx, ok = false, reason = e.message ?: "写入失败")
        }
    }

    private fun finishReceive(rx: ReceiveSession, ok: Boolean, reason: String) {
        try {
            rx.stream?.flush()
            rx.stream?.close()
        } catch (_: Exception) {
        }
        if (ok && rx.size > 0 && rx.done < rx.size) {
            finishReceiveError(rx, "只收到 ${rx.done}/${rx.size} 字节")
            return
        }
        if (!ok) {
            finishReceiveError(rx, reason.ifBlank { "接收失败" })
            return
        }
        try {
            val uri = rx.uri
            if (uri != null) {
                val values = ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }
                context.contentResolver.update(uri, values, null, null)
            }
        } catch (e: Exception) {
            Log.w(TAG, "finalize failed: ${e.message}")
        }
        TransferBus.publish(TransferProgress(false, name = rx.name, bytesDone = rx.done, bytesTotal = rx.size, finished = true, index = 1, count = 1))
        onEvent("已接收文件", "${rx.name} → 下载/RemoteMic")
    }

    private fun finishReceiveError(rx: ReceiveSession, reason: String) {
        try {
            rx.stream?.close()
        } catch (_: Exception) {
        }
        // 失败就删掉半成品，手机上不留残片（和电脑端一致）
        try {
            rx.uri?.let { context.contentResolver.delete(it, null, null) }
            rx.file?.delete()
        } catch (e: Exception) {
            Log.w(TAG, "delete partial failed: ${e.message}")
        }
        TransferBus.publish(TransferProgress(false, name = rx.name, finished = true, error = reason, index = 1, count = 1))
        onEvent("接收失败", reason)
    }

    // ------------------------------------------------------------------ 工具

    private fun queryMeta(uri: Uri): Triple<String, Long, String> {
        var name = "文件"
        var size = 0L
        try {
            context.contentResolver.query(uri, null, null, null, null)?.use { c ->
                if (c.moveToFirst()) {
                    val nameIdx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    val sizeIdx = c.getColumnIndex(OpenableColumns.SIZE)
                    if (nameIdx >= 0) c.getString(nameIdx)?.let { name = it }
                    if (sizeIdx >= 0 && !c.isNull(sizeIdx)) size = c.getLong(sizeIdx)
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "query name failed: ${e.message}")
        }
        if (size <= 0) {
            // 有些 provider 不给 SIZE。先取文件描述符的真实长度；
            // 拿不到再完整读一遍数字节——available() 只是「当前不阻塞可读字节数」，
            // 不是总长，用它会让声明尺寸与实际不符，电脑端 finish() 校验必然失败。
            size = runCatching {
                context.contentResolver.openFileDescriptor(uri, "r")?.use { it.statSize } ?: -1L
            }.getOrDefault(-1L)
            if (size <= 0) size = countBytes(uri)
        }
        val mime = context.contentResolver.getType(uri) ?: "application/octet-stream"
        return Triple(name, size, mime)
    }

    /** 完整读一遍只为了数长度（不发送）。大文件会慢，但总比发出去尺寸不符强。 */
    private fun countBytes(uri: Uri): Long {
        var total = 0L
        try {
            context.contentResolver.openInputStream(uri)?.use { input ->
                val buf = ByteArray(CHUNK)
                while (!cancelFlag) {
                    val n = input.read(buf)
                    if (n <= 0) break
                    total += n
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "count bytes failed: ${e.message}")
            return 0L
        }
        return if (cancelFlag) 0L else total
    }

    /**
     * 清洗电脑端传来的文件名，规则与电脑端 `files.sanitize_name` 对齐：
     * 去目录部分、替换非法字符、避开 Windows 保留名、限长。
     * 不清洗的话 `../../x` 这类名字能让我们写出下载目录。
     */
    private fun sanitizeName(raw: String): String {
        var base = raw.replace('\\', '/').substringAfterLast('/').trim().trim('.', ' ')
        base = base.map { if (it in "<>:\"/\\|?*" || it.code < 32) '_' else it }.joinToString("")
        if (base.isEmpty()) base = "文件"
        // Windows 把首个点之前的整段当设备名，nul.a.b 照样是 NUL 设备
        if (base.substringBefore('.').lowercase() in RESERVED) base = "_$base"
        val dot = base.lastIndexOf('.')
        val stem = if (dot > 0) base.substring(0, dot) else base
        val ext = if (dot > 0) base.substring(dot) else ""
        return stem.take(120) + ext.take(20)
    }

    private fun uniqueFile(dir: File, name: String): File {
        var candidate = File(dir, name)
        if (!candidate.exists()) return candidate
        val dot = name.lastIndexOf('.')
        val stem = if (dot > 0) name.substring(0, dot) else name
        val ext = if (dot > 0) name.substring(dot) else ""
        for (i in 1..999) {
            candidate = File(dir, "$stem ($i)$ext")
            if (!candidate.exists()) return candidate
        }
        return File(dir, "$stem-${System.currentTimeMillis()}$ext")
    }

    private class IllegalException(message: String) : Exception(message)
}
