package com.willen.remotemic.view

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.util.AttributeSet
import android.view.HapticFeedbackConstants
import android.view.MotionEvent
import android.view.View
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.min
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * 小米遥控器式方向环：
 *  - 画一圈厚白色环带（宽度 = 直径 25%），中心留给外层的「视频」圆钮；
 *  - 四段弧各自可点，按下时该段环带染成浅蓝、对应箭头略微放大；
 *  - 只负责「哪一向被按下」和触感反馈，具体发什么协议由 [onDirection] 交给 Activity，
 *    与原来的 btnUp/btnDown/btnLeft/btnRight 完全等价。
 */
class RingPadView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null
) : View(context, attrs) {

    /** (方向码: up/down/left/right, 动作: down) */
    var onDirection: ((code: String, action: String) -> Unit)? = null

    private val density = resources.displayMetrics.density

    private val ringPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        color = Color.WHITE
        strokeCap = Paint.Cap.BUTT
    }
    private val pressPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        color = 0x382B6CB0
    }
    private val arrowPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        color = Color.parseColor("#2B6CB0")
        strokeWidth = dp(2.2f)
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
    }

    private val arcRect = RectF()
    private var cx = 0f
    private var cy = 0f
    private var radius = 0f
    private var band = 0f

    /** 0 = 没按，1 上，2 右，3 下，4 左 */
    private var pressed = 0

    init {
        setLayerType(LAYER_TYPE_SOFTWARE, null)
        ringPaint.setShadowLayer(dp(16f), 0f, dp(7f), 0x1A10151C)
        isClickable = true
        contentDescription = "方向键"
    }

    private fun dp(v: Float): Float = v * density

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val availableW = MeasureSpec.getSize(widthMeasureSpec)
        val availableH = MeasureSpec.getSize(heightMeasureSpec)
        // 下方 56dp 留给「取消 / 确定」一行，再留 20dp 给方向环上下呼吸
        val target = min(
            dp(232f),
            min(availableW - dp(60f), availableH - dp(76f).toFloat())
        ).coerceAtLeast(dp(190f))
        val size = target.toInt()
        setMeasuredDimension(size, size)
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        cx = w / 2f
        cy = h / 2f
        band = w * 0.25f
        radius = (w - band) / 2f
        ringPaint.strokeWidth = band
        pressPaint.strokeWidth = band
        arcRect.set(cx - radius, cy - radius, cx + radius, cy + radius)
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        if (radius <= 0f) return

        canvas.drawCircle(cx, cy, radius, ringPaint)

        if (pressed != 0) {
            canvas.drawArc(arcRect, startAngleFor(pressed), 90f, false, pressPaint)
        }

        drawArrow(canvas, -90f, pressed == 1)
        drawArrow(canvas, 0f, pressed == 2)
        drawArrow(canvas, 90f, pressed == 3)
        drawArrow(canvas, 180f, pressed == 4)
    }

    private fun startAngleFor(dir: Int): Float = when (dir) {
        1 -> -135f
        2 -> -45f
        3 -> 45f
        else -> 135f
    }

    private fun drawArrow(canvas: Canvas, angleDeg: Float, active: Boolean) {
        val a = Math.toRadians(angleDeg.toDouble())
        val x = cx + cos(a).toFloat() * radius
        val y = cy + sin(a).toFloat() * radius
        val scale = if (active) 1.16f else 1f
        val half = dp(6f) * scale
        val depth = dp(4f) * scale

        val path = Path()
        when (angleDeg) {
            -90f -> {
                path.moveTo(x - half, y + depth)
                path.lineTo(x, y - depth)
                path.lineTo(x + half, y + depth)
            }
            0f -> {
                path.moveTo(x - depth, y - half)
                path.lineTo(x + depth, y)
                path.lineTo(x - depth, y + half)
            }
            90f -> {
                path.moveTo(x - half, y - depth)
                path.lineTo(x, y + depth)
                path.lineTo(x + half, y - depth)
            }
            else -> {
                path.moveTo(x + depth, y - half)
                path.lineTo(x - depth, y)
                path.lineTo(x + depth, y + half)
            }
        }
        canvas.drawPath(path, arrowPaint)
    }

    override fun onTouchEvent(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                val dir = directionAt(event.x, event.y)
                if (dir == 0) return false
                pressed = dir
                invalidate()
                performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                onDirection?.invoke(codeOf(dir), "down")
                return true
            }

            MotionEvent.ACTION_MOVE -> {
                if (pressed != 0) {
                    val dir = directionAt(event.x, event.y)
                    if (dir != 0 && dir != pressed) {
                        pressed = dir
                        invalidate()
                    }
                    return true
                }
            }

            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                if (pressed != 0) {
                    pressed = 0
                    invalidate()
                    return true
                }
            }
        }
        return super.onTouchEvent(event)
    }

    private fun directionAt(x: Float, y: Float): Int {
        val dx = x - cx
        val dy = y - cy
        val dist = sqrt(dx * dx + dy * dy)
        // 只有落在环带上才算；中间的空洞留给中心按钮
        if (dist < radius - band * 0.9f || dist > radius + band * 0.4f) return 0

        val deg = Math.toDegrees(atan2(dy.toDouble(), dx.toDouble()))
        return when {
            deg >= -135.0 && deg < -45.0 -> 1
            deg >= -45.0 && deg < 45.0 -> 2
            deg >= 45.0 && deg < 135.0 -> 3
            else -> 4
        }
    }

    private fun codeOf(dir: Int): String = when (dir) {
        1 -> "up"
        2 -> "right"
        3 -> "down"
        else -> "left"
    }
}
