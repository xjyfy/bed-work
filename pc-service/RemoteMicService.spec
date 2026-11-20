# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包脚本（onedir，**不要 onefile**）。

- onefile 每次启动都要解包到临时目录，托盘/驱动/常驻服务会明显拖慢冷启动，杀软误报也更多；
- 用 GUI 子系统（console=False），日志自己写 `%LOCALAPPDATA%\\RemoteMic\\logs\\service.log`；
- 随包带 `vb-cable` 驱动目录和 `config.example.json`，安装包直接引用。

用法（pyinstaller 已装在仓库 .venv 里）：
    .venv\\Scripts\\pyinstaller.exe --noconfirm pc-service\\RemoteMicService.spec
产物：
    pc-service/dist/RemoteMicService/RemoteMicService.exe
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# spec 里 __file__ 不可靠，用 specpath（pyinstaller 会注入）
SPEC_DIR = Path(SPECPATH).resolve()  # noqa: F821

datas = [
    (str(SPEC_DIR / "vendor" / "vb-cable"), "vb-cable"),
    (str(SPEC_DIR / "config.example.json"), "."),
]
datas += collect_data_files("webview")
datas += collect_data_files("qrcode")

hiddenimports = [
    # 托盘后端（pystray 按平台动态 import）
    "pystray._win32",
    "PIL._tkinter_finder",
    # 设置页：pywebview 的 winforms 后端 + pythonnet
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "clr_loader",
    "pythonnet",
    # 音视频 / 键鼠
    "sounddevice",
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    # 旧机器没有 WebView2 时回退到 tkinter 设置页
    "tkinter",
    "tkinter.filedialog",
    "tkinter.messagebox",
]
hiddenimports += collect_submodules("websockets")

a = Analysis(  # noqa: F821
    [str(SPEC_DIR / "run.py")],
    pathex=[str(SPEC_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib", "pandas", "scipy", "PIL.ImageQt", "PyQt5", "PyQt6",
        "PySide2", "PySide6", "notebook", "IPython",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RemoteMicService",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # upx 压过的 exe 更容易被杀软误报，不启用
    console=False,      # GUI 子系统：双击不弹黑框；`--install-driver` 用弹窗反馈
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="RemoteMicService",
)
