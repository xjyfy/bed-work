# 手机遥控麦（小米蓝牙遥控器改造）

用 Android 手机当「遥控器」，通过局域网 WebSocket 遥控 Windows 电脑的鼠标键盘、媒体键、语音输入与应用切换。

## 组成

| 目录 | 说明 |
| --- | --- |
| `android-app/` | 手机端（Kotlin + zxing 扫码绑定），五分区界面：媒体 / 功能 / 方向 / 语音 / 快捷 |
| `pc-service/` | 电脑端服务（Python + websockets + pystray 托盘 + pywebview 设置页） |
| `*.md` | 方案、可行性评估、开发清单与二期计划 |

## 电脑端启动

```bat
cd pc-service
启动电脑端服务.bat
```

按钮内部调用 `wscript.exe start-hidden.vbs`，以隐藏窗口方式启动 `python.exe run.py`，启动后不留命令行窗口。
运行日志写 `pc-service\logs\service.log`（5MB × 3 滚动）。

首次运行前装依赖：

```bat
python -m pip install -r pc-service\requirements.txt
```

## 配置

把 `pc-service/config.example.json` 复制为 `pc-service/config.json` 再按需修改。
`config.json` 含遥控器绑定 token，**不要提交到仓库**（已加入 `.gitignore`）。

关键字段：

- `computer_name`：显示在手机端顶栏的电脑名
- `remotes[]`：已绑定遥控器（`id` / `name` / `token`）
- `apps{1..4}`：四个应用栏位的 exe 完整路径，或 `uwp:<AUMID>`（商店应用）
- `slot_send_hotkey{}`：栏位级发送快捷键，可选 `enter` / `ctrl+enter`（留空表示沿用按前台程序查表）
- `voice_hotkey`：语音键组合（默认 `ctrl+win`）

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

## 安全提示

- 服务监听 `0.0.0.0:8765`，请只在可信局域网使用；绑定 token 泄露后请在设置页删除该遥控器并重新绑定。
- 本机若给 `pythonw.exe` 加了入站 Block 防火墙规则，会导致手机连不上，服务脚本因此使用 `python.exe` + 隐藏控制台。
