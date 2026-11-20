"""电脑端服务启动入口。

用法（建议用 pythonw.exe 启动，不弹命令行窗口）：
    pythonw run.py
    python  run.py     # 便于调试，日志同时打到控制台
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

from remote_mic.server import main

if __name__ == "__main__":
    main()
