package com.willen.remotemic.shot

import android.graphics.Bitmap

/**
 * 截屏图片的内存中转：只在内存里存在，退出浏览页立刻 recycle。
 *
 * 不走 Intent extra 传输——一张 1600x900 的图直接塞进 Intent 很容易顶到
 * Binder 1MB 事务上限，正好在"偶尔崩"的边界上。
 */
object ShotHolder {
    private var bitmap: Bitmap? = null

    fun set(value: Bitmap) {
        clear()
        bitmap = value
    }

    fun get(): Bitmap? = bitmap

    fun clear() {
        val current = bitmap
        bitmap = null
        runCatching { current?.recycle() }
    }
}
