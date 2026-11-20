package com.willen.remotemic.audio

/** 跨模块传递 PCM 帧的轻量总线：录音服务采集后回调给 WebSocket 发送层。 */
object AudioBus {
    var onPcm: ((ByteArray) -> Unit)? = null
}
