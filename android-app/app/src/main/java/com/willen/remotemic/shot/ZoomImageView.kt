package com.willen.remotemic.shot

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Paint
import android.util.AttributeSet
import android.view.GestureDetector
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import android.view.View
import kotlin.math.max
import kotlin.math.min

/**
 * 截图查看控件：等比铺满、双指缩放（1x~8x）、单指拖动、双击复位。
 *
 * 用 Matrix 手工算，比 ImageView 的 scaleType 灵活：缩放上限、拖动边界、
 * 双击回弹都要自己控制，ImageView 那套改不动。
 */
class ZoomImageView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null
) : View(context, attrs) {

    private val paint = Paint(Paint.FILTER_BITMAP_FLAG).apply {
        isFilterBitmap = true
        isAntiAlias = true
        isDither = true
    }

    private var bitmap: Bitmap? = null
    private var baseScale = 1f        // 铺满所需的缩放
    private var userScale = 1f        // 用户额外放大倍数
    private var offsetX = 0f
    private var offsetY = 0f

    private val scaleDetector = ScaleGestureDetector(context, object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
        override fun onScale(detector: ScaleGestureDetector): Boolean {
            val target = (userScale * detector.scaleFactor).coerceIn(MAX_ZOOM_DOWN, MAX_ZOOM_UP)
            if (target != userScale) {
                userScale = target
                clampOffset()
                invalidate()
            }
            return true
        }
    })

    private val gestureDetector = GestureDetector(context, object : GestureDetector.SimpleOnGestureListener() {
        override fun onDown(e: MotionEvent): Boolean = true

        override fun onScroll(
            e1: MotionEvent?,
            e2: MotionEvent,
            distanceX: Float,
            distanceY: Float
        ): Boolean {
            if (userScale <= 1f) return false      // 没放大就别拖动，省得整张图跟着乱跑
            offsetX -= distanceX
            offsetY -= distanceY
            clampOffset()
            invalidate()
            return true
        }

        override fun onDoubleTap(e: MotionEvent): Boolean {
            if (userScale > 1.02f) {
                resetZoom()
            } else {
                // 双击在点位附近放大 2.5 倍
                userScale = 2.5f
                offsetX = (width / 2f - e.x) * (userScale - 1f)
                offsetY = (height / 2f - e.y) * (userScale - 1f)
                clampOffset()
                invalidate()
            }
            return true
        }
    })

    fun setBitmap(value: Bitmap?) {
        bitmap = value
        computeBaseScale()
        invalidate()
    }

    fun resetZoom() {
        userScale = 1f
        offsetX = 0f
        offsetY = 0f
        invalidate()
    }

    private fun computeBaseScale() {
        val b = bitmap ?: return
        if (width == 0 || height == 0 || b.width == 0 || b.height == 0) {
            baseScale = 1f
            return
        }
        baseScale = min(width.toFloat() / b.width, height.toFloat() / b.height)
    }

    private fun clampOffset() {
        val b = bitmap ?: return
        val scale = baseScale * userScale
        val maxX = max(0f, (b.width * scale - width) / 2f)
        val maxY = max(0f, (b.height * scale - height) / 2f)
        offsetX = offsetX.coerceIn(-maxX, maxX)
        offsetY = offsetY.coerceIn(-maxY, maxY)
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        // 转屏后重新铺满（转屏会把画面尺寸变掉，用户放大状态保留比例即可）
        computeBaseScale()
        offsetX = 0f
        offsetY = 0f
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val b = bitmap ?: return
        val scale = baseScale * userScale
        canvas.save()
        canvas.translate(width / 2f + offsetX, height / 2f + offsetY)
        canvas.scale(scale, scale)
        canvas.drawBitmap(b, -b.width / 2f, -b.height / 2f, paint)
        canvas.restore()
    }

    override fun onTouchEvent(event: MotionEvent): Boolean {
        if (bitmap == null) return false
        scaleDetector.onTouchEvent(event)
        gestureDetector.onTouchEvent(event)
        parent?.requestDisallowInterceptTouchEvent(true)
        return true
    }

    companion object {
        private const val MAX_ZOOM_UP = 8f
        private const val MAX_ZOOM_DOWN = 1f
    }
}
