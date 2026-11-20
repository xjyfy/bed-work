package com.willen.remotemic

import android.view.HapticFeedbackConstants
import android.view.MotionEvent
import android.view.View

/** 点按：按下即触发（复制/粘贴/媒体/滚轮/应用启动等），并带轻微缩放与震动反馈。 */
fun View.click(action: () -> Unit) {
    setOnClickListener {
        performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
        action()
    }
    setOnTouchListener { v, event ->
        when (event.action) {
            MotionEvent.ACTION_DOWN -> v.animate().scaleX(.95f).scaleY(.95f).setDuration(90).start()
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL ->
                v.animate().scaleX(1f).scaleY(1f).setDuration(120).start()
        }
        false
    }
}

/** 点按型按键的别名，语义同 click。 */
fun View.press(action: () -> Unit) = click(action)

/** 按住型：ACTION_DOWN 触发 down，ACTION_UP / CANCEL 触发 up；按下即震动。 */
fun View.hold(down: () -> Unit, up: () -> Unit) {
    setOnTouchListener { v, event ->
        when (event.action) {
            MotionEvent.ACTION_DOWN -> {
                performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                v.animate().scaleX(.98f).scaleY(.98f).setDuration(90).start()
                down()
                true
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                v.animate().scaleX(1f).scaleY(1f).setDuration(120).start()
                up()
                true
            }
            else -> false
        }
    }
}
