# v1.0.2 · 托盘发送文件修复 + 音频/传输可观测（取消自签证书）

## 修复

- **托盘「发送文件到手机…」点了完全没反应**
  - `filedialog.py` 把 `ctypes.create_unicode_buffer` 直接赋给 `c_wchar_p` 字段，新版 ctypes 抛 `TypeError: incompatible types, c_wchar_Array_65536 instance instead of c_wchar_p`。异常被托盘线程兜底吞掉，界面上就是"点了没动静"。改为 `ctypes.cast(buf, c_wchar_p)`，并给 `GetOpenFileNameW` 补 `argtypes`。
  - 顺手修多选解析：`c_wchar_Array` 切片会在第一个 `NUL` 处截断，多选时只能拿到目录名。改为 `wstring_at(addressof(buf), len(buf))` 整段读出再按 `\0` 拆。

## 变更

- **取消自签代码签名**：证书（个人存储 + 受信任的根）与构建步骤一并移除，安装包恢复未签名状态，首次运行会重新出现 SmartScreen 提示。

## 新增

- **B2 音频可观测**：`status.audio` 增加 `level_ms`（缓冲水位）、`overflows`、`underruns`、`dropped_samples`、`frames_in/out`、`device/hostapi`。语音卡不卡不用再翻日志。
- **B6 电脑端传输进度**：托盘 tooltip 显示 `发送 2/5 45% 文件名`，每跨 10% 刷新一次；结束或失败自动恢复成连接状态。
- **B7 重连可观测**：`status.clients[]` 增加 `reconnects`（同一遥控器第几次重连）与 `uptime_s`（当前会话时长）；日志新增 `remote reconnect #n` 与断线时的会话时长，便于判断是"被杀后自动重连"还是"正常退出"。

## 已知限制

- 仅同一局域网；音频为未压缩 PCM（16 kHz/16 bit/单声道）
- 不做后台常驻保活：熄屏断连属预期，亮屏自动重连（实测 0.3 s）
- 安装包与主程序均未签名，SmartScreen 首次运行会提示「未知发布者」（v1.0.1 的自签方案已按你的要求撤掉）
- 大文件传输无断点续传（单文件 ≤ 2 GB、一次 ≤ 50 个）

## 升级

直接运行 `RemoteMicSetup-1.0.2.exe` 覆盖安装，`%LOCALAPPDATA%\RemoteMic` 下的绑定 token 与四栏位不会丢。
