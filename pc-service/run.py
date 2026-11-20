"""电脑端服务启动入口。

用法：
    pythonw run.py                 # 正常启动（开发期；打包后直接双击 RemoteMicService.exe）
    python  run.py                 # 便于调试，日志同时打到控制台
    RemoteMicService.exe --install-driver     # 只装 VB-CABLE 虚拟声卡然后退出
    RemoteMicService.exe --uninstall-driver   # 卸载 VB-CABLE 然后退出

设置页里「安装虚拟声卡」按钮走的就是同一个 driver.install()，
所以装完才发现没声音的用户不需要重跑安装包。
"""

import ctypes
import sys

# 必须在任何 GUI / 坐标 API 之前声明 DPI 感知（Per-Monitor V2），
# 否则 GetWindowRect / 光标坐标会被系统按缩放比例虚拟化，导致鼠标落点偏移。
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:  # noqa: BLE001 - 旧系统回退到系统级 DPI 感知
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        pass


def _driver_action(argv: list[str]) -> str:
    if "--install-driver" in argv:
        return "install"
    if "--uninstall-driver" in argv:
        return "uninstall"
    if "--check-driver" in argv:
        return "check"
    return ""


if __name__ == "__main__":
    action = _driver_action(sys.argv[1:])
    if action:
        from remote_mic import driver

        sys.exit(driver.main(action))

    from remote_mic.server import main

    main()
