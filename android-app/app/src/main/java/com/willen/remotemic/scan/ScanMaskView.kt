package com.willen.remotemic.scan

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.util.AttributeSet
import android.view.View
import android.view.animation.LinearInterpolator
import kotlin.math.min

/**
 * 扫码取景框遮罩：四角描边 + 半透明暗角 + 来回移动的扫描线。
 * 纯自绘，不依赖第三方库，尺寸跟着控件实时算。
 */
class ScanMaskView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null
) : View(context, attrs) {

    private val dimPaint = Paint().apply { color = 0xA6000000.toInt() }

    private val framePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = 0xFF4CAF50.toInt()
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
    }

    private val linePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = 0xCC8BC34A.toInt()
        style = Paint.Style.STROKE
    }

    private val hole = RectF()
    private var lineRatio = 0f

    private val animator = ValueAnimator.ofFloat(0f, 1f).apply {
        duration = 1800
        repeatCount = ValueAnimator.INFINITE
        repeatMode = ValueAnimator.REVERSE
        interpolator = LinearInterpolator()
        addUpdateListener {
            lineRatio = it.animatedValue as Float
            invalidate()
        }
    }

    fun start() {
        if (!animator.isStarted) animator.start()
    }

    fun stop() {
        animator.cancel()
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        val side = min(w, h) * 0.74f
        val cx = w / 2f
        val cy = h / 2f - h * 0.06f
        hole.set(cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2)
        framePaint.strokeWidth = dp(3f)
        linePaint.strokeWidth = dp(2f)
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w = width.toFloat()
        val h = height.toFloat()

        // 四周压暗，只留中间取景框
        canvas.drawRect(0f, 0f, w, hole.top, dimPaint)
        canvas.drawRect(0f, hole.bottom, w, h, dimPaint)
        canvas.drawRect(0f, hole.top, hole.left, hole.bottom, dimPaint)
        canvas.drawRect(hole.right, hole.top, w, hole.bottom, dimPaint)

        // 四角括号
        val c = dp(28f)
        val l = hole.left
        val t = hole.top
        val r = hole.right
        val b = hole.bottom
        canvas.drawLine(l, t, l + c, t, framePaint)
        canvas.drawLine(l, t, l, t + c, framePaint)
        canvas.drawLine(r - c, t, r, t, framePaint)
        canvas.drawLine(r, t, r, t + c, framePaint)
        canvas.drawLine(l, b - c, l, b, framePaint)
        canvas.drawLine(l, b, l + c, b, framePaint)
        canvas.drawLine(r - c, b, r, b, framePaint)
        canvas.drawLine(r, b - c, r, b, framePaint)

        // 扫描线
        val y = hole.top + hole.height() * lineRatio
        canvas.drawLine(hole.left + dp(6f), y, hole.right - dp(6f), y, linePaint)
    }

    private fun dp(v: Float) = v * resources.displayMetrics.density

    override fun onDetachedFromWindow() {
        stop()
        super.onDetachedFromWindow()
    }
}
