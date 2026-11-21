# v1.2.1 · 换用独立 release 签名 + 全新应用图标

发布日期：2026-11-21

## 下载

- **手机端**：`RemoteMic-1.2.1.apk`（Android 8.0+，约 6 MB，`versionCode=4`）
- **电脑端**：`RemoteMicSetup-1.1.0.exe`（Windows 10/11 x64，约 28 MB；本版电脑端无代码改动，沿用 1.1.0）

## ⚠️ 升级方式变了：必须卸载重装

本版起 APK 改用**独立 release 签名**，不再复用 debug 签名。签名不同意味着：

- **无法覆盖安装** 1.2.0 及更早的 debug 签名包，必须先卸载旧版再装本版。
- 卸载会清空手机端 SharedPreferences，**已绑定的电脑列表 / token 会丢**，装好后需重新扫码绑定一次。
- 电脑端 `config.json` 不受影响；用电脑端设置页原有的绑定二维码重新扫码即可恢复（token 不变）。
- 从本版往后，同签名的后续版本可以正常覆盖升级、不再丢绑定。

## 签名与密钥管理

- release keystore：RSA 2048 / SHA256withRSA，有效期 10000 天，别名 `remotemic`，DN `CN=xjyfy RemoteMic, OU=bed-work, O=xjyfy, C=CN`。
- 证书 SHA-256：`6567fa4f2fd1132515930f29df5f28f661584033d8939118868951449e4344cd`（`apksigner verify --print-certs` 可核对）。
- **存放**：仓库根 `signing/remotemic-release.keystore` + `signing/keystore.properties`（含密码）。两者都被 `.gitignore` 屏蔽，**不进公开仓库**——私钥一旦公开，任何人都能签同包名 APK 覆盖用户安装。
- **复用**：换机器 / 换项目时把整个 `signing/` 目录拷过去即可沿用同一签名。`app/build.gradle.kts` 从 `signing/keystore.properties` 读取；文件缺失时 release 回退 debug 签名并打印告警，**回退包禁止对外发布**。
- **备份提醒**：keystore 不在 Git 里，请自行离线备份（丢失后将无法再发布可覆盖升级的同签名包）。
- PKCS12 密钥库的 key 密码必须与 store 密码相同，properties 里两者已对齐。

## 全新应用图标（手机端 + 电脑端统一）

此前 manifest 没有 `android:icon`，一直用系统默认图标；电脑端托盘是纯色圆点。本版两端统一换成正式品牌图标：

- 设计：**浅蓝对角渐变底（`#F8FCFF → #D9EAF9 → #B4D3EF`）+ 暖/冷光斑 + 玻璃高光 + 白色厚方向环 + 中心白圆钮 + 深蓝麦克风（`#173F60`）**——即 App 的环形方向键与语音两个签名元素。设计稿见 `design/app-icon/icon.html`。
- **手机端**：自适应图标（`mipmap-anydpi-v26` + `drawable/ic_launcher_background.xml` / `ic_launcher_foreground.xml` 矢量），minSdk 26 全设备覆盖；环外缘落在中心安全区内，小尺寸仍可读。
- **电脑端**：同一设计渲染为位图资产 `pc-service/remote_mic/assets/app_icon.png` / `.ico`——
  - 托盘图标改用品牌图 + 右下角状态点（保留灰=未连接 / 绿=控制中 / 黄=待命语义）；
  - exe 图标（PyInstaller `icon=`）与安装包图标（Inno `SetupIconFile`）均换为 `.ico`；
  - 资产随 PyInstaller `datas` 打包到 `_internal/remote_mic/assets/`，`paths.asset_path()` 统一解析。
- 预览见 [`assets/应用图标.png`](assets/应用图标.png)。
- 已知：ColorOS 若开启「统一图标重绘」（设置 → 桌面与锁屏 → 图标风格），抽屉里会把第三方图标重画；本设计本身为浅底，重绘后观感接近。系统「应用详情」页始终显示原始图标；覆盖安装后若见旧图标属系统图标缓存，卸载重装即刷新。

## 验证

- `apksigner verify --print-certs` 确认签名者为 `CN=xjyfy RemoteMic`，指纹与 `signing/` keystore 一致，且不同于旧 debug 证书。
- `aapt dump badging` 确认 `versionName=1.2.1`、`versionCode=4`；APK 内仅位图 mipmap、无 adaptive 资源。
- 真机（一加 13 / ColorOS 17）卸载旧 debug 包后安装本版成功；系统应用详情页显示新图标；扫码重绑后连接 / 传输 / 语音实测通过。