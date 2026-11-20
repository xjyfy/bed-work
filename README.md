# 手机遥控麦（小米蓝牙遥控器改造）

用 Android 手机当「遥控器」，通过局域网 WebSocket 遥控 Windows 电脑的鼠标键盘、媒体键、语音输入与应用切换。

## 组成

| 目录 | 说明 |
| --- | --- |
| `android-app/` | 手机端（Kotlin + zxing 扫码绑定），五分区界面：媒体 / 功能 / 方向 / 语音 / 快捷 |
| `pc-service/` | 电脑端服务（Python + websockets + pystray 托盘 + pywebview 设置页） |
| `*.md` | 方案、可行性评估、开发清单与二期计划 |

## 电脑端启动

双击 `pc-service\启动电脑端服务.bat` 即可（不需要开命令行）。

- 该 bat 只做一件事：`start "" wscript.exe start-hidden.vbs`，随后立即退出，不留命令行窗口。
- `start-hidden.vbs` 按顺序找解释器：仓库根 `.venv\Scripts\python.exe` → `%LOCALAPPDATA%\Programs\Python\Python313\python.exe` → `PATH` 上的 `python.exe`，然后以隐藏窗口运行 `run.py`。
- 启动失败时会**弹窗**给出退出码、实际使用的解释器与日志尾部（不再「闪一下就没了」）。
- 运行日志写 `pc-service\logs\service.log`（5MB × 3 滚动）。

首次运行（或换了机器）先双击 `pc-service\安装依赖.bat`：有 `uv` 就建 `.venv` 装 Python 3.12 + 依赖，没有则用 `python -m venv` + pip。

`config.json` 首次启动会自动从 `config.example.json` 生成（**已有文件绝不覆盖**），不需要手工复制。

## 配置

`config.json` 首次启动自动生成（来自 `config.example.json`），之后按需修改。
`config.json` 含遥控器绑定 token，**不要提交到仓库**（已加入 `.gitignore`）。

关键字段：

- `computer_name`：显示在手机端顶栏的电脑名
- `remotes[]`：已绑定遥控器（`id` / `name` / `token`）
- `apps{1..4}`：四个应用栏位的 exe 完整路径，或 `uwp:<AUMID>`（商店应用）。点栏位时**应用已在运行（含最小化到托盘、停在屏幕外）只切换窗口，不会新开实例**；后台运行但没有窗口时只给托盘气泡提示；确实没启动才拉起
- `slot_send_hotkey{}`：栏位级发送快捷键，可选 `enter` / `ctrl+enter`（留空表示沿用按前台程序查表）
- `voice_hotkey`：语音键组合（默认 `ctrl+win`）
- `clip_policy`：远程粘贴时是否顺带发 `Ctrl+V`，`auto`（默认）/ `always` / `caret_only`。`auto` 会粘到所有不是桌面 / 任务栏 / 资源管理器 的前台应用（豆包、DeepSeek Harness、微信这类 Electron/Qt 应用没有 Win32 光标，只认光标会漏粘）

## 托盘菜单

- 状态（颜色随连接状态变化）
- 切换控制遥控器（多台手机时先连优先，可手动指定控制权）
- 设置…（pywebview 弹窗：电脑名、绑定二维码、遥控器表、四栏位应用、发送快捷键）
- 刷新声卡设备 / 退出

## 协议

首帧认证：`{"type":"auth","token":...,"device":...}`
成功：`{"type":"auth_ok","active":bool,"computer":...,"status":{...}}`
失败：`{"type":"auth_fail","reason":"token_invalid"}`
状态广播：`{"type":"status","computer","active_id","clients":[{id,name,ip,active}],"voice","active"}`

远程粘贴：`{"type":"clip","text":"手机剪贴板内容"}` —— 电脑端写入剪贴板，并按 `clip_policy`（默认 `auto`）决定是否再补一个 Ctrl+V：能确认是输入控件就粘，桌面/任务栏/资源管理器只写剪贴板，其余应用默认也粘（Electron/Qt 应用没有 Win32 光标，只看光标会漏粘）。
文本上限 100000 字符，手机端先校验（类型不支持 / 超长直接提示），服务端再兜一层；写入失败只在电脑端托盘气泡提示。

连接保活策略：**不做后台常驻**。熄屏后系统会挂起网络、连接会断（手机端也不需要在熄屏时遥控），
App 改为在**亮屏 / 解锁 / 回到界面**时立刻探活并重连（实测亮屏→重连 0.3 秒）。

## 安全提示

- 服务监听 `0.0.0.0:8765`，请只在可信局域网使用；绑定 token 泄露后请在设置页删除该遥控器并重新绑定。
- 本机若给 `pythonw.exe` 加了入站 Block 防火墙规则，会导致手机连不上，服务脚本因此使用 `python.exe` + 隐藏控制台。
