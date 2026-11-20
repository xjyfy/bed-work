# v1.0.1 · 安装包装不上/误报修复 + 状态可见性 + 代码签名

## 修复

- **安装包在已装 WebView2 的机器上报"安装失败"并回滚**
  - WebView2 检测漏判：机器级安装的键在 `HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-…}`，而 Inno 的 `HKLM` 在 64 位安装模式下被解析成 64 位视图。改为 `HKLM64 / HKLM32 / HKCU` 三处都查，并按 `pv` 值判断。
  - WebView2 引导器在"已安装"时会自己弹框并返回非 0；`[Run]` 见到非 0 就让整个安装失败。现在 WebView2 / VB-CABLE / 防火墙三步都在 `[Code]` 里用 `Exec` 执行并忽略返回码，缺项只在装完提示，不再回滚。
- **已装 VB-CABLE 被判定"未装"**
  - `pnputil` 里的 INF 原始名是 `vbmmecable64_win10.inf`，旧关键字 `vbmme_cable64` 永远匹配不上（`mme` 与 `cable` 之间没有下划线）。改为宽松匹配 `vbmmecable / vbaudio / vbcable / vb-audio`。
  - `Get-PnpDevice` 兜底不再被 `is_frozen()` 关掉（之前打包版一律跳过兜底），并改用 powershell 绝对路径。
- **卸载时 VB-CABLE 永远卸不掉**：驱动卸载从 `usPostUninstall` 挪到 `usUninstall`——前者阶段 `{app}` 里的安装器已被删除，`FileExists` 恒假。
- **开发态启动失败提示读旧日志路径**：`start-hidden.vbs` 优先读 `%LOCALAPPDATA%\RemoteMic\logs\service.log`，不存在再回退 `pc-service\logs`。

## 新增

- **代码签名**：自签证书 `CN=WILLEN RemoteMic`，主程序与安装包均做 SHA256 + DigiCert 时间戳签名；本机（已导入受信任根）双击不再提示"未知发布者"。其他机器需导入 `%LOCALAPPDATA%\RemoteMic\RemoteMicCodesign.cer` 到"受信任的根证书颁发机构"。对外分发仍需商业证书。
- **`status` 帧新增 `audio_ready` / `mode` / `tab_index`**：手机端能区分"声卡没装好"和"输出流没打开"，也能看到是否停在 Alt+Tab 浮层、当前 Tab 序号。
- **图标缓存自动失效**：缓存键加入 exe 修改时间，应用升级换了图标会自动重画；图标缓存目录统一到 `%LOCALAPPDATA%\RemoteMic\icons`。
- **UWP 前台识别**：前台窗口属于 `ApplicationFrameHost.exe` 时，遍历子窗口解析到真实应用进程；发送键与栏位匹配支持 `uwp:<PackageFamilyName>!App`。

## 已知限制

- 仅同一局域网；音频仍为未压缩 PCM（16 kHz/16 bit/单声道）
- 不做后台常驻保活：熄屏断连属预期，亮屏自动重连（实测 0.3 s）
- 自签证书只在本机（或已导入该证书的机器）受信任；公开分发仍会触发 SmartScreen
- 大文件传输无断点续传（单文件 ≤ 2 GB、一次 ≤ 50 个）

## 升级

直接运行 `RemoteMicSetup-1.0.1.exe` 覆盖安装即可：`%LOCALAPPDATA%\RemoteMic` 下的配置、绑定 token、四栏位不会丢。
