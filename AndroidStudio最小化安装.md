# Android Studio 最小化安装说明（Windows）

> **安装结果（2026-11-15）**：Studio Rabbit 2 Patch 1、JDK17、Gradle 8.9、最小 SDK 三包均已装好；`assembleDebug` 构建成功，APK 已产出。本文档保留作为环境说明与重装参考。

目标：装可视化 Android Studio，但只装 IDE 本体 + 本工程必需的最小 SDK；**不装模拟器系统镜像、NDK、CMake、旧 API 平台及 Wear/TV 等额外组件**。
本机：Windows 11 专业版 / AMD64。

---

## 一、安装包

- 版本：Android Studio **Rabbit 2 Patch 1**（2026.2.2.9）
- 直链（已测连通）：
  `https://edgedl.me.gvt1.com/android/studio/install/2026.2.2.9/android-studio-rabbit2-patch1-windows.exe`
- 大小约 1.5 GB；SHA-256：`00e98c1c23e1d27a8079951291d1ae310bbc8c7ef1030ab9db1f45a1bb96543b`
- Studio 自带 JBR（JetBrains Runtime，JDK 17），**无需单独安装 JDK**。

## 二、安装时的最小化取舍

安装向导 / 首次启动里，按下面取舍：

| 项目 | 处理 |
| --- | --- |
| Android Studio 主程序 | 安装 |
| Android SDK Platform-Tools（adb） | 安装（必需） |
| Android SDK Platform 35 | 安装（compileSdk/targetSdk 35） |
| Build-Tools 35.0.0 | 安装 |
| Android Emulator + 系统镜像（AVD） | **不装**，真机调试即可 |
| NDK / CMake | **不装**（本工程纯 Kotlin，无原生代码） |
| 旧版 SDK Platform（34 及以下） | **不装** |
| Wear OS / TV / Automotive 组件 | **不装** |

## 三、安装步骤

1. 运行 `android-studio.exe`（如向导要求，同意 UAC 提权）。
2. 安装向导中：
   - 若有组件勾选页，只保留 Android Studio 本体；**取消** Emulator / 额外虚拟设备选项。
   - 安装路径默认即可（`C:\Program Files\Android\Android Studio`）。
3. 首次启动的 Setup Wizard：
   - 若提示自动下载全套 SDK / 模拟器，选择 **自定义 / 跳过**，不要一键默认装全量。
   - 进入 Studio 后打开 `Settings → Languages & Frameworks → Android SDK → SDK Platforms`，只勾选 **Android 35**。
   - 切到 **SDK Tools** 页，只勾选：**Android SDK Platform-Tools**、**Android SDK Build-Tools 35.0.0**；其余（Emulator、NDK、CMake、Sources for SDK）取消。
   - Apply 下载最小 SDK 集。

## 四、打开工程并构建

1. Studio → `Open` → 选择 `android-app/` 目录。
2. 首次 Gradle Sync：工程已指定 AGP 8.7.3 / Gradle 8.9 / Kotlin 1.9.24，按提示下载对应 Gradle 与依赖即可。
3. 真机：一加 13 开开发者选项 + USB 调试，连上后设备下拉选中；或菜单 `Build → Build APK(s)` 出包。
4. 产物路径：`android-app\app\build\outputs\apk\debug\app-debug.apk`，可 `adb install -r` 装机或传手机手动安装。

## 五、工程与版本对齐

| 项 | 工程配置 |
| --- | --- |
| compileSdk / targetSdk | 35 |
| minSdk | 26 |
| AGP | 8.7.3 |
| Gradle | 8.9 |
| Kotlin | 1.9.24 |
| 构建工具 | build-tools 35.0.0 |

## 六、可能踩到的点

- **wrapper jar 缺失**：工程当前只有 `gradle-wrapper.properties`，没有 `gradle-wrapper.jar`。若 Studio 同步报 wrapper 相关错误，用 Studio 内置终端执行一次 `gradle wrapper --gradle-version 8.9`（或让 Studio 用自带 Gradle 生成），再重新 Sync。
- **依赖下载慢**：AGP / AndroidX 依赖从 Google Maven 拉取；如遇网络问题，可在 `settings.gradle.kts` 仓库加国内镜像，不要在业务代码加内网代理。
- **SDK 许可**：若同步报 license 错误，在 SDK Manager 接受许可协议后重试。
- **磁盘**：Studio 本体 + 最小 SDK 约占 5–6 GB；本机 C 盘剩余 37.6 GB，够用。
