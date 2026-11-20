package com.willen.remotemic

import android.view.MotionEvent
import android.view.View

/** 点按：按下即触发（复制/粘贴/媒体/滚轮/应用启动等）。 */
fun View.click(action: () -> Unit) {
    setOnClickListener { action() }
}

/** 点按型按键的别名，语义同 click。 */
fun View.press(action: () -> Unit) = click(action)

/** 按住型：ACTION_DOWN 触发 down，ACTION_UP / CANCEL 触发 up。 */
fun View.hold(down: () -> Unit, up: () -> Unit) {
    setOnTouchListener { _, event ->
        when (event.action) {
            MotionEvent.ACTION_DOWN -> { down(); true }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> { up(); true }
            else -> false
        }
    }
}
