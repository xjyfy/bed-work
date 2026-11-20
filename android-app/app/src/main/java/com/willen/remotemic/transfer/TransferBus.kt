package com.willen.remotemic.transfer

/** 传输进度：发（手机→电脑）和收（电脑→手机）共用一套，浮窗和通知栏都读它。 */
data class TransferProgress(
    val sending: Boolean,
    val name: String = "",
    val index: Int = 0,
    val count: Int = 0,
    val bytesDone: Long = 0,
    val bytesTotal: Long = 0,
    val finished: Boolean = false,
    val error: String? = null
) {
    val percent: Int
        get() = if (bytesTotal <= 0) 0 else (((bytesDone * 100) / bytesTotal).toInt().coerceIn(0, 100))

    fun summary(): String = when {
        error != null -> "失败：$error"
        finished -> "已完成 $count 个文件"
        count > 1 -> "第 $index/$count 个 · $name"
        else -> name
    }

    fun detail(): String {
        val done = human(bytesDone) + " / " + human(bytesTotal)
        return "$done  ·  $percent%"
    }

    private fun human(v: Long): String {
        var value = v.toDouble()
        val units = listOf("B", "KB", "MB", "GB")
        var i = 0
        while (value >= 1024 && i < units.lastIndex) {
            value /= 1024
            i++
        }
        return if (i == 0) "${value.toInt()} ${units[i]}" else String.format("%.1f %s", value, units[i])
    }
}

/**
 * 进度总线：传输线程只负责 publish，界面浮窗与前台服务各自 observe。
 * 不持有 Context，避免内存泄漏。
 */
object TransferBus {
    @Volatile
    var current: TransferProgress? = null
        private set

    private val listeners = java.util.concurrent.CopyOnWriteArrayList<(TransferProgress?) -> Unit>()

    fun observe(l: (TransferProgress?) -> Unit) {
        listeners.add(l)
        current?.let { l(it) }
    }

    fun forget(l: (TransferProgress?) -> Unit) {
        listeners.remove(l)
    }

    fun publish(p: TransferProgress?) {
        current = p
        listeners.forEach { runCatching { it(p) } }
    }
}
