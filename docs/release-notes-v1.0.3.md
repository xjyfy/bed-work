# v1.0.3 · 覆盖安装不再需要手动退出托盘

## 修复

- **首次运行弹大量「终端 / 命令提示符」黑框**
  - 原因：主程序是 GUI 子系统（`console=False`），自己没有控制台。`applist.py` 枚举已装应用时反复调 `pwsh` / `powershell`（每次还会按候选列表试两个 exe），`settings_ui.py` 同理，`actioner.py` 用 `shell=True` 起 `cmd.exe`——这些控制台子进程在无控制台的父进程下会**各自新建一个控制台窗口**，于是首启时刷一片黑框。
  - 修法：所有外部命令调用统一加 `creationflags=CREATE_NO_WINDOW`（`applist._run_pwsh` / `_run_ps1`、`settings_ui._run_pwsh`、`actioner` 的 `shell=True` 启动），`driver.py` 原本就有。输出仍通过管道读回，功能不变。
- **覆盖安装弹「安装程序无法自动关闭所有应用程序 / 选择操作」**
  - 原因：托盘进程只有一个 `pystray` 的隐藏消息窗口，Windows Restart Manager 给它发关闭消息后它不会退出主循环，Inno 的 `CloseApplications=yes` 因此判定"关不掉"，只能弹框让用户点「重试 / 忽略 / 取消」——而点哪个都不能顺利装完（文件仍被占用）。
  - 修法：`[Code]` 新增 `PrepareToInstall`，进入安装流程前先 `taskkill /IM RemoteMicService.exe /F /T`，再用 `taskkill /FI` 轮询确认进程确实退出（最多等 3 秒），然后才开始拷贝文件。装完由 `[Run]` 重新拉起服务，`HKCU\...\Run` 自启项不受影响。
  - 效果：以后覆盖安装**不需要**再手动右键托盘「退出」，也不会再弹这个框。

## 说明

- 修复在安装器自身里，所以**装本版这一步就已经自动生效**：不需要先手动退托盘再点安装。
- 卸载路径原本就有 `taskkill`（`[UninstallRun]`），不受影响。
- 杀进程会让服务断一下，装完由 `[Run]`「立即启动」和 `HKCU\...\Run` 自启项接回来；运行期数据（`%LOCALAPPDATA%\RemoteMic` 的 token、四栏位、日志）不动。
- 曾试过用 `taskkill /FI ... /NH` 的返回码做"进程是否已退"的轮询，实测**无匹配也返回 1**、与有匹配无法区分，故改为 `/F` 强杀后固定等 900 ms 释放句柄。

## 未做

- 未推送到 GitHub（按要求仅更新本地仓）。
