"""已安装应用枚举：注册表 + 开始菜单 + 商店应用，带发布者/版本/安装时间/大小/图标。

供设置页（pywebview / tkinter 两套界面）共用。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import winreg
from typing import Optional

from . import paths

logger = logging.getLogger(__name__)

# 明显是卸载/更新/安装器的关键字，不进应用列表
_EXE_BLOCK = re.compile(
    r"(^unins|uninstall|uninst|setup|update|updater|repair|remove|卸载|安装|install\.exe)",
    re.IGNORECASE,
)
_NAME_BLOCK = re.compile(
    r"(卸载|uninstall|remove only|更新程序|update\b|setup|修复工具|安装程序|安装包)",
    re.IGNORECASE,
)

# 驱动 / 运行库 / 系统组件：装了也不算"应用"
_NAME_SYSTEM = re.compile(
    r"(Redistributable|\.NET Desktop Runtime|\.NET Framework|ASP\.NET Core|Runtime - |"
    r"Desktop Runtime|Shared Framework|Chipset Device Software|Network Connections Drivers|"
    r"Serial IO|System Firmware|Management Engine|Application Compatibility Fix Database|"
    r"UXP WebView Support|VBCABLE|Virtual Audio Cable|Windows Media Player Legacy|"
    r"Microsoft Visual C\+\+|Intel\(R\)|Intel®|Driver|驱动)",
    re.IGNORECASE,
)

# Windows 自带的商店应用（AUMID 前缀），不算"后安装的应用"
_UWP_BLOCK_PREFIX = (
    "Microsoft.WindowsStore_",
    "Microsoft.Xbox",
    "Microsoft.Paint_",
    "Microsoft.WindowsCalculator_",
    "Microsoft.WindowsNotepad_",
    "Microsoft.ScreenSketch_",
    "Microsoft.WindowsAlarms_",
    "Microsoft.WindowsSoundRecorder_",
    "Microsoft.WindowsCamera_",
    "Microsoft.Windows.Photos_",
    "Microsoft.ZuneVideo_",
    "Microsoft.MicrosoftStickyNotes_",
    "Microsoft.WindowsTerminal_",
    "Microsoft.GetHelp_",
    "MicrosoftWindows.Client.",
    "Microsoft.SecHealthUI_",
    "windows.immersivecontrolpanel_",
    "Microsoft.PowerAutomateDesktop_",
    "Microsoft.MicrosoftOfficeHub_",
    "Clipchamp.Clipchamp_",
    "Microsoft.YourPhone_",
    "Microsoft.Todos_",
    "Microsoft.Windows.Battery",
    "Microsoft.ZuneMusic_",
    "Microsoft.WidgetsPlatform",
    "MicrosoftWindows.CrossDevice",
    "MicrosoftWindows.Quintet",
    "AppUp.Intel",  # Intel 显卡控制面板属驱动组件
)


_ARCH_TAIL = r"(?:\s*[（(]\s*(?:x64|x86|64-?位|32-?位|64-?bit|32-?bit)\s*[)）])?"
_VER_TAIL = re.compile(
    r"(?:\s+v?\d+(?:\.\d+)+|\s*版本\s*\d+(?:\.\d+)*)"
    r"(?:[-+_][0-9A-Za-z.\-]*)?"
    + _ARCH_TAIL
    + r"\s*$"
)
_VER_PAREN = re.compile(r"\s*[（(]\s*v?\d+(?:\.\d+){1,4}[^）)]*[)）]\s*$")
_VER_BARE = re.compile(r"\s+v?\d+\.\d+(?:\.\d+){0,2}$")


def _clean_name(name: str, exe: str) -> str:
    """去掉明显的版本尾巴／括号后缀；名称缺失或乱码时用 exe 文件名兜底。"""
    raw = (name or "").replace("\u00ae", "").strip()
    if not raw or "\ufffd" in raw:
        if exe and not exe.startswith("uwp:"):
            raw = os.path.splitext(os.path.basename(exe))[0]
        elif exe.startswith("uwp:"):
            raw = exe.split("!", 1)[-1]
        return raw.strip()

    for pat in (_VER_PAREN, _VER_TAIL, _VER_BARE):
        trimmed = pat.sub("", raw)
        if len(trimmed.strip()) >= 2:  # 别把整个名字削没了
            raw = trimmed
    return raw.strip()


def _clean_version(raw: str) -> str:
    """清理版本串：去掉 SHA 说明、全零版本、过长尾巴。"""
    ver = (raw or "").strip()
    if not ver:
        return ""
    if "SHA:" in ver:
        ver = ver.split("SHA:", 1)[0].strip()
    if re.fullmatch(r"[\s,0.]+", ver):
        return ""
    if len(ver) > 22:
        ver = ver[:22].rstrip(" ,.-")
    return ver


def _read(key, name: str) -> str:
    try:
        value, _ = winreg.QueryValueEx(key, name)
        return str(value)
    except OSError:
        return ""


def _exe_from(text: str) -> str:
    if not text:
        return ""
    m = re.search(r'"([^"]+\.exe)"', text, re.IGNORECASE) or re.search(r"(\S+\.exe)", text, re.IGNORECASE)
    path = (m.group(1) if m else text).strip().strip('"')
    if path.lower().endswith(".exe") and os.path.exists(path):
        return path
    return ""


def _blocked(name: str, path: str) -> bool:
    base = os.path.basename(path) if path and not path.startswith("uwp:") else ""
    if base and base.lower() in {"chrome_proxy.exe"}:  # Chrome 网页应用快捷方式，不算独立应用
        return True
    if base and _EXE_BLOCK.search(base):
        return True
    if name and _NAME_BLOCK.search(name):
        return True
    if name and _NAME_SYSTEM.search(name):
        return True
    if not name.strip():
        return True
    if name.strip() in {"配置工具", "卸载", "Setup", "Update"}:
        return True
    if path.startswith("uwp:"):
        aumid = path[4:]
        if any(aumid.startswith(pre) for pre in _UWP_BLOCK_PREFIX):
            return True
    if path and not path.startswith("uwp:"):
        low = path.lower()
        win = os.environ.get("SystemRoot", r"C:\Windows").lower()
        if low.startswith(win):
            return True
        if "\\windowsapps\\" in low:
            return True
    return False


# ------------------------------------------------------------------ 注册表

def _registry_entries() -> list[dict]:
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0),
    ]
    out: list[dict] = []
    for hive, path, extra in roots:
        try:
            with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | extra) as key:
                count = winreg.QueryInfoKey(key)[0]
                for i in range(count):
                    try:
                        sub = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, sub) as item:
                            name = _read(item, "DisplayName").strip()
                            icon = _read(item, "DisplayIcon")
                            loc = _read(item, "InstallLocation")
                            publisher = _read(item, "Publisher").strip()
                            version = _read(item, "DisplayVersion").strip()
                            install_date = _read(item, "InstallDate").strip()
                            size_kb = _read(item, "EstimatedSize").strip()
                            system = _read(item, "SystemComponent")
                            release = _read(item, "ReleaseType")
                            parent = _read(item, "ParentKeyName")
                    except OSError:
                        continue
                    if not name or system == "1" or parent:
                        continue
                    if release and release.lower() in ("update", "hotfix", "security update"):
                        continue
                    exe = _exe_from(icon) or _exe_from(loc)
                    out.append(
                        {
                            "name": name,
                            "exe": exe,
                            "publisher": publisher,
                            "version": _clean_version(version),
                            "install_date": _fmt_date(install_date),
                            "size": _fmt_size(size_kb),
                        }
                    )
        except OSError:
            continue
    return out


def _fmt_date(raw: str) -> str:
    """InstallDate 常见格式 YYYYMMDD。"""
    if re.fullmatch(r"\d{8}", raw or ""):
        return f"{raw[:4]}/{int(raw[4:6])}/{int(raw[6:])}"
    return ""


def _fmt_size(raw: str) -> str:
    try:
        kb = int(raw)
    except (TypeError, ValueError):
        return ""
    if kb <= 0:
        return ""
    mb = kb / 1024
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.0f} MB"


# ------------------------------------------------------------------ 开始菜单 / 商店应用

_PWSH_CACHE: list[str] = []


def _pwsh_candidates() -> list[str]:
    """按优先级返回可用的 PowerShell：系统自带的更稳，PATH 里可能是受限的运行时。"""
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    cands = [
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        r"C:\Program Files\PowerShell\7-preview\pwsh.exe",
        os.path.join(sysroot, r"System32\WindowsPowerShell\v1.0\powershell.exe"),
    ]
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            cands.append(found)
    return [c for c in cands if c and os.path.exists(c)]


def _run_pwsh(script: str) -> str:
    global _PWSH_CACHE
    if not _PWSH_CACHE:
        _PWSH_CACHE = _pwsh_candidates()
    if not _PWSH_CACHE:
        logger.warning("no PowerShell found, app listing will be degraded")
        return ""
    # 统一输出编码，避免 Windows PowerShell 5.1 下中文变成乱码
    prelude = "$OutputEncoding=[Text.Encoding]::UTF8;[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
    for exe in _PWSH_CACHE:
        try:
            out = subprocess.run(
                [exe, "-NoProfile", "-NonInteractive", "-Command", prelude + script],
                capture_output=True,
                text=True,
                timeout=40,
                encoding="utf-8",
                errors="ignore",
            )
            if (out.stdout or "").strip():
                return out.stdout.strip()
            if out.returncode != 0:
                logger.warning("pwsh (%s) rc=%s stderr=%s", exe, out.returncode, (out.stderr or "")[:200])
        except Exception as exc:  # noqa: BLE001
            logger.warning("pwsh (%s) failed: %s", exe, exc)
    return ""


def _run_ps1(script_path: str) -> str:
    """跑一个 .ps1 文件（需要多行 C# 内联时用；-Command 拼长脚本容易踩引号）。"""
    global _PWSH_CACHE
    if not _PWSH_CACHE:
        _PWSH_CACHE = _pwsh_candidates()
    if not _PWSH_CACHE:
        logger.warning("no PowerShell found, skip script run")
        return ""
    for exe in _PWSH_CACHE:
        try:
            out = subprocess.run(
                [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script_path],
                capture_output=True,
                text=True,
                timeout=60,
                encoding="utf-8",
                errors="ignore",
            )
            if out.returncode == 0:
                return (out.stdout or "").strip()
            logger.warning("ps1 (%s) rc=%s stderr=%s", exe, out.returncode, (out.stderr or "")[:200])
        except Exception as exc:  # noqa: BLE001
            logger.warning("ps1 (%s) failed: %s", exe, exc)
    return ""


def _start_menu_links() -> list[tuple[str, str]]:
    script = (
        "$sh = New-Object -ComObject WScript.Shell; $items = @();"
        "$dirs = @(\"$env:ProgramData\\Microsoft\\Windows\\Start Menu\\Programs\","
        "\"$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs\");"
        "foreach ($d in $dirs) {"
        " Get-ChildItem -LiteralPath $d -Recurse -Filter *.lnk -ErrorAction SilentlyContinue |"
        " ForEach-Object { $t = $sh.CreateShortcut($_.FullName).TargetPath;"
        "  if ($t -and $t.ToLower().EndsWith('.exe')) {"
        "   $items += [pscustomobject]@{ name = $_.BaseName; exe = $t } } } };"
        "$items | ConvertTo-Json -Compress"
    )
    raw = _run_pwsh(script)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [(str(d.get("name", "")), str(d.get("exe", ""))) for d in data]


def _store_apps() -> list[tuple[str, str]]:
    script = (
        "Get-StartApps | Where-Object { $_.AppID -like '*!*' } | "
        "Select-Object Name, AppID | ConvertTo-Json -Compress"
    )
    raw = _run_pwsh(script)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [(str(d.get("Name", "")), "uwp:" + str(d.get("AppID", ""))) for d in data]


# ------------------------------------------------------------------ 图标

_ICON_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "remote-mic", "icons")  # 老版本兼容，仅作说明


def icon_cache_dir() -> str:
    # 与 config.json / logs 统一到 %LOCALAPPDATA%\RemoteMic\icons
    # （老版本放在 %LOCALAPPDATA%\remote-mic\icons，那批缓存不再读取，会自然重建）
    path = os.path.join(str(paths.data_dir()), "icons")
    os.makedirs(path, exist_ok=True)
    return path


def _icon_path(exe: str) -> str:
    # 缓存键里带上 exe 的修改时间：应用升级换了图标后能自动重画，
    # 不用重启服务，也不用清 %LOCALAPPDATA%\remote-mic\icons。
    # （UWP 的键按 AUMID，拿不到 mtime，走 _uwp_icon_path）
    stamp = ""
    try:
        if not exe.startswith("uwp:"):
            stamp = str(int(os.path.getmtime(exe)))
    except OSError:
        stamp = ""
    digest = hashlib.md5(f"{exe.lower()}|{stamp}".encode("utf-8")).hexdigest()[:16]
    # 后缀带 _256：手机端要把图标当桌面图标那样放大显示，32px 会糊；
    # 换名字也顺手把老的低清缓存作废（不会去读旧文件）
    return os.path.join(icon_cache_dir(), f"{digest}_256.png")


# 用 Windows Shell 的 IShellItemImageFactory 取 256px 大图标（ExtractAssociatedIcon
# 只能给 32px，放大就糊）。拿不到再退回 32px 再放大兜底。
_ICON_PS1 = r"""$OutputEncoding=[Text.Encoding]::UTF8;[Console]::OutputEncoding=[Text.Encoding]::UTF8
Add-Type -AssemblyName System.Drawing
$src = @"
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

public static class JumboIcon {
    [StructLayout(LayoutKind.Sequential)]
    public struct SIZE { public int cx; public int cy; public SIZE(int x, int y) { cx = x; cy = y; } }

    [ComImport, Guid("bcc18b79-ba16-442f-80c4-8a59c30c463b"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellItemImageFactory {
        [PreserveSig] int GetImage(SIZE size, int flags, out IntPtr phbm);
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    private static extern void SHCreateItemFromParsingName(
        [MarshalAs(UnmanagedType.LPWStr)] string pszPath,
        IntPtr pbc,
        [MarshalAs(UnmanagedType.LPStruct)] Guid riid,
        [MarshalAs(UnmanagedType.Interface)] out IShellItemImageFactory ppv);

    [StructLayout(LayoutKind.Sequential)]
    private struct BITMAPINFOHEADER {
        public uint biSize; public int biWidth; public int biHeight;
        public ushort biPlanes; public ushort biBitCount; public uint biCompression;
        public uint biSizeImage; public int biXPelsPerMeter; public int biYPelsPerMeter;
        public uint biClrUsed; public uint biClrImportant;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct BITMAPINFO { public BITMAPINFOHEADER bmiHeader; public uint bmiColors; }

    [DllImport("gdi32.dll")] private static extern IntPtr CreateCompatibleDC(IntPtr hdc);
    [DllImport("gdi32.dll")] private static extern bool DeleteDC(IntPtr hdc);
    [DllImport("gdi32.dll")] private static extern int GetDIBits(IntPtr hdc, IntPtr hbm, uint start, uint lines,
        byte[] bits, ref BITMAPINFO bmi, uint usage);
    [DllImport("gdi32.dll")] private static extern bool DeleteObject(IntPtr hObject);

    public static Bitmap Get(string path, int size) {
        IShellItemImageFactory f = null;
        IntPtr hbm = IntPtr.Zero, hdc = IntPtr.Zero;
        try {
            Guid iid = new Guid("bcc18b79-ba16-442f-80c4-8a59c30c463b");
            SHCreateItemFromParsingName(path, IntPtr.Zero, iid, out f);
            int hr = f.GetImage(new SIZE(size, size), 0x1 | 0x4, out hbm);
            if (hr != 0 || hbm == IntPtr.Zero) throw new Exception("GetImage hr=" + hr);

            // 必须自己 GetDIBits 取 32bpp 原始位（含 alpha）。Image.FromHbitmap 会丢 alpha，
            // 透明区直接变成不透明的黑块——手机上看就是四个黑方块。
            BITMAPINFO bmi = new BITMAPINFO();
            bmi.bmiHeader.biSize = (uint)Marshal.SizeOf(typeof(BITMAPINFOHEADER));
            bmi.bmiHeader.biWidth = size;
            bmi.bmiHeader.biHeight = -size;
            bmi.bmiHeader.biPlanes = 1;
            bmi.bmiHeader.biBitCount = 32;
            bmi.bmiHeader.biCompression = 0;

            byte[] buf = new byte[size * size * 4];
            hdc = CreateCompatibleDC(IntPtr.Zero);
            int got = GetDIBits(hdc, hbm, 0, (uint)size, buf, ref bmi, 0);
            if (got == 0) throw new Exception("GetDIBits failed");

            int maxA = 0;
            for (int i = 3; i < buf.Length; i += 4) { if (buf[i] > maxA) maxA = buf[i]; }
            if (maxA == 0) {
                // 万一 shell 给的是 24bpp 语义，纯黑当透明兜底
                for (int i = 0; i < buf.Length; i += 4) {
                    if (buf[i] == 0 && buf[i + 1] == 0 && buf[i + 2] == 0) buf[i + 3] = 0;
                }
            }

            Bitmap bmp = new Bitmap(size, size, PixelFormat.Format32bppArgb);
            BitmapData bd = bmp.LockBits(new Rectangle(0, 0, size, size), ImageLockMode.WriteOnly, PixelFormat.Format32bppArgb);
            Marshal.Copy(buf, 0, bd.Scan0, buf.Length);
            bmp.UnlockBits(bd);
            return bmp;
        } finally {
            if (hdc != IntPtr.Zero) DeleteDC(hdc);
            if (hbm != IntPtr.Zero) DeleteObject(hbm);
            if (f != null) Marshal.ReleaseComObject(f);
        }
    }
}
"@
$sdc = @([System.Drawing.Bitmap].Assembly.Location, [System.Drawing.Rectangle].Assembly.Location)
try { Add-Type -TypeDefinition $src -ReferencedAssemblies $sdc -ErrorAction Stop } catch { }

$pairs = Get-Content -LiteralPath '__JSON__' -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($p in $pairs.pairs) {
    $dest = $p[1]
    try {
        $img = [JumboIcon]::Get($p[0], 256)
        if ($img -ne $null) {
            $img.Save($dest, [System.Drawing.Imaging.ImageFormat]::Png)
            $img.Dispose()
            continue
        }
    } catch { }
    try {
        $i = [System.Drawing.Icon]::ExtractAssociatedIcon($p[0])
        if ($i) {
            $b = $i.ToBitmap()
            if ($b.Width -lt 256) { $big = New-Object System.Drawing.Bitmap($b, 256, 256); $b.Dispose(); $b = $big }
            $b.Save($dest, [System.Drawing.Imaging.ImageFormat]::Png)
            $b.Dispose()
        }
    } catch { }
}
"""


def missing_icons(exes: list[str]) -> bool:
    """有任何一个需要图标的可执行文件还没有缓存 PNG。"""
    for exe in exes:
        path = str(exe or "")
        if not path or path.startswith("uwp:") or not os.path.exists(path):
            continue
        if not os.path.exists(_icon_path(path)):
            return True
    return False


def ensure_icons(exes: list[str]) -> None:
    """批量把 exe 关联图标导出为 256px PNG（有缓存则跳过）。"""
    todo = [e for e in exes if e and not e.startswith("uwp:") and os.path.exists(e) and not os.path.exists(_icon_path(e))]
    if not todo:
        return
    payload = {"pairs": [[e, _icon_path(e)] for e in todo]}
    work_dir = icon_cache_dir()
    tmp_json = os.path.join(work_dir, "_icons_request.json")
    with open(tmp_json, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)

    script_path = os.path.join(work_dir, "_icons_export.ps1")
    with open(script_path, "w", encoding="utf-8-sig") as fh:
        fh.write(_ICON_PS1.replace("__JSON__", tmp_json.replace("'", "''")))

    _run_ps1(script_path)
    for path in (tmp_json, script_path):
        try:
            os.remove(path)
        except OSError:
            pass


def _uwp_icon_path(aumid: str) -> str:
    digest = hashlib.md5(aumid.lower().encode("utf-8")).hexdigest()[:16]
    return os.path.join(icon_cache_dir(), f"uwp_{digest}.png")


def _ensure_uwp_icon(aumid: str) -> None:
    """商店应用没有 exe，图标从 AppxManifest 的 Logo 导出。"""
    target = _uwp_icon_path(aumid)
    if os.path.exists(target):
        return
    family = aumid.split("!", 1)[0].strip()
    if not family:
        return
    script = (
        "$fam='%s'; $out='%s';"
        "$p = Get-AppxPackage | Where-Object { $_.PackageFamilyName -eq $fam } | Select-Object -First 1;"
        "if ($p) {"
        " $dir = $p.InstallLocation;"
        " $assets = Join-Path $dir 'Assets';"
        " if (-not (Test-Path -LiteralPath $assets)) { $assets = $dir };"
        " $pats = 'Square44x44Logo.targetsize-256.png','Square44x44Logo.scale-400.png',"
        "  'Square44x44Logo*.png','Square150x150Logo*.png','logo.png','*.png';"
        " $src = $null;"
        " foreach ($pt in $pats) {"
        "  $f = Get-ChildItem -LiteralPath $assets -Filter $pt -ErrorAction SilentlyContinue | Select-Object -First 1;"
        "  if ($f) { $src = $f.FullName; break } }"
        " if ($src) { Add-Type -AssemblyName System.Drawing;"
        "  $img = [System.Drawing.Image]::FromFile($src);"
        "  $img.Save($out, [System.Drawing.Imaging.ImageFormat]::Png); $img.Dispose() } }"
    ) % (family, target.replace("'", "''"))
    _run_pwsh(script)


def icon_data_url(exe: str) -> str:
    if not exe:
        return ""
    if exe.startswith("uwp:"):
        _ensure_uwp_icon(exe[4:])
        path = _uwp_icon_path(exe[4:])
    else:
        path = _icon_path(exe)
    if not os.path.exists(path):
        return ""
    try:
        import base64

        with open(path, "rb") as fh:
            return "data:image/png;base64," + base64.b64encode(fh.read()).decode("ascii")
    except OSError:
        return ""


def _exe_meta(exes: list[str]) -> dict[str, dict]:
    """从 exe 的文件版本信息补发布者 / 版本号。"""
    uniq = sorted({e for e in exes if e and not e.startswith("uwp:") and os.path.exists(e)})
    if not uniq:
        return {}
    tmp_json = os.path.join(icon_cache_dir(), "_meta_request.json")
    with open(tmp_json, "w", encoding="utf-8") as fh:
        json.dump(uniq, fh, ensure_ascii=False)
    script = (
        "$d = Get-Content -LiteralPath '%s' -Raw -Encoding UTF8 | ConvertFrom-Json;"
        "$r = foreach ($p in $d) { try { $v = (Get-Item -LiteralPath $p -ErrorAction Stop).VersionInfo;"
        " [pscustomobject]@{ p = $p; company = $v.CompanyName; desc = $v.FileDescription;"
        " ver = $v.ProductVersion; fver = $v.FileVersion } } catch {} };"
        "$r | ConvertTo-Json -Compress"
    ) % tmp_json.replace("'", "''")
    raw = _run_pwsh(script)
    try:
        os.remove(tmp_json)
    except OSError:
        pass
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        data = [data]
    return {
        str(d.get("p", "")).lower(): d
        for d in data
        if isinstance(d, dict) and d.get("p")
    }


# ------------------------------------------------------------------ 汇总

def list_app_entries(with_icons: bool = True) -> list[dict]:
    """返回应用列表：{name, exe, publisher, version, install_date, size, icon}"""
    regs = _registry_entries()
    links = _start_menu_links()

    # 名字 -> 快捷方式 exe，用来给缺 DisplayIcon 的注册表项补路径
    link_map: dict[str, str] = {}
    for name, exe in links:
        link_map.setdefault(name.strip().lower(), exe)

    used_exe: set[str] = set()
    used_base: set[str] = set()
    used_name: set[str] = set()
    items: list[dict] = []

    for entry in regs:
        exe = entry["exe"]
        display = _clean_name(entry["name"], exe)
        if _blocked(display, exe) or _blocked(entry["name"], exe):
            continue
        if not exe:
            key = entry["name"].strip().lower()
            exe = link_map.get(key, "")
            if not exe:
                for lname, lexe in link_map.items():
                    if lname.startswith(key) or key.startswith(lname):
                        exe = lexe
                        break
            if not exe:
                # 没有可执行文件就无法启动，这类多为驱动/组件残留，不入列表
                continue
            # 补到 exe 之后再按 exe 过滤一次（如 Chrome 网页应用代理）
            if _blocked(display, exe) or _blocked(entry["name"], exe):
                continue
        if exe and exe.lower() in used_exe:
            continue
        entry["name"] = display
        entry["exe"] = exe
        if exe:
            used_exe.add(exe.lower())
            used_base.add(os.path.basename(exe).lower())
        used_name.add(display.lower())
        items.append(entry)

    for name, exe in links:
        if not exe or exe.lower() in used_exe:
            continue
        display = _clean_name(name, exe)
        if _blocked(display, exe) or _blocked(name, exe) or display.lower() in used_name:
            continue
        base = os.path.basename(exe).lower()
        if base in used_base:  # 同一个 exe 已被注册表条目收录，跳过快捷方式重复项
            continue
        used_exe.add(exe.lower())
        used_base.add(base)
        used_name.add(display.lower())
        items.append(
            {"name": display, "exe": exe, "publisher": "", "version": "", "install_date": "", "size": ""}
        )

    for name, exe in _store_apps():
        display = _clean_name(name, exe)
        if _blocked(display, exe) or _blocked(name, exe) or display.lower() in used_name:
            continue
        used_name.add(display.lower())
        items.append(
            {"name": display, "exe": exe, "publisher": "", "version": "", "install_date": "", "size": ""}
        )

    items.sort(key=lambda x: x["name"].lower())

    # 开始菜单 / 商店条目没有发布者和版本，从 exe 文件版本信息补全
    meta = _exe_meta([it["exe"] for it in items if not it.get("publisher") or not it.get("version")])
    for it in items:
        if it["exe"].startswith("uwp:"):
            continue
        info = meta.get(it["exe"].lower(), {})
        if not it.get("publisher"):
            it["publisher"] = str(info.get("company") or "").strip()
        if not it.get("version"):
            ver = str(info.get("ver") or info.get("fver") or "").strip()
            it["version"] = _clean_version(ver)

    if with_icons:
        ensure_icons([it["exe"] for it in items])
        for it in items:
            it["icon"] = icon_data_url(it["exe"])
    else:
        for it in items:
            it["icon"] = ""
    return items


# ------------------------------------------------------------------ 单条解析

_LIST_CACHE: dict = {"ts": 0.0, "items": []}
_LIST_TTL = 120.0


def cached_entries() -> list[dict]:
    """带 TTL 的整表缓存，避免设置页每次渲染都重新枚举。"""
    import time

    now = time.time()
    if _LIST_CACHE["items"] and now - _LIST_CACHE["ts"] < _LIST_TTL:
        return _LIST_CACHE["items"]
    items = list_app_entries()
    _LIST_CACHE["ts"] = now
    _LIST_CACHE["items"] = items
    return items


def invalidate_cache() -> None:
    _LIST_CACHE["ts"] = 0.0
    _LIST_CACHE["items"] = []


def peek_entries() -> list[dict]:
    """只读现有缓存，**绝不触发枚举**。

    设置页打开时要立刻出界面：枚举一次要跑注册表 + 两次 PowerShell，
    同步等它就会白屏 1~2 秒。这里拿不到就返回空，由后台预热线程补上。
    """
    return _LIST_CACHE["items"]


def warm_cache_async() -> None:
    """服务启动时后台预热一次整表缓存（设置页第一次打开就不用等枚举）。"""
    import threading

    if _LIST_CACHE["items"]:
        return

    def run() -> None:
        try:
            cached_entries()
            logger.info("app list cache warmed: %d entries", len(_LIST_CACHE["items"]))
        except Exception as exc:  # noqa: BLE001
            logger.warning("warm app list cache failed: %s", exc)

    threading.Thread(target=run, daemon=True, name="applist-warm").start()


def describe_app(value: str, allow_scan: bool = True) -> dict:
    """把配置里的应用值解析成友好名称 + 图标。

    支持三种写法：真实 exe 路径、启动名（Doubao / chrome）、uwp:<AUMID>。
    """
    raw = (value or "").strip()
    if not raw:
        return {"name": "", "icon": "", "resolved": ""}

    try:
        items = cached_entries() if allow_scan else peek_entries()
    except Exception:  # noqa: BLE001
        items = []

    if raw.lower().startswith("uwp:"):
        aumid = raw[4:]
        for it in items:
            if it["exe"].lower() == raw.lower():
                return {"name": it["name"] or aumid.split("!")[-1], "icon": icon_data_url(raw), "resolved": raw}
        return {"name": aumid.split("!")[-1], "icon": icon_data_url(raw), "resolved": raw}

    base = ""
    if os.path.exists(raw):
        norm = os.path.normpath(raw).lower()
        for it in items:
            if not it["exe"].startswith("uwp:") and os.path.normpath(it["exe"]).lower() == norm:
                return {"name": it["name"], "icon": it["icon"] or icon_data_url(raw), "resolved": it["exe"]}
        base = os.path.basename(norm)
    else:
        name = os.path.basename(raw)
        base = name.lower() if name.lower().endswith(".exe") else (name + ".exe").lower()

    for it in items:
        if not it["exe"].startswith("uwp:") and os.path.basename(it["exe"]).lower() == base:
            return {"name": it["name"], "icon": it["icon"] or icon_data_url(it["exe"]), "resolved": it["exe"]}

    stem = os.path.splitext(os.path.basename(raw))[0]
    return {"name": stem, "icon": icon_data_url(raw), "resolved": raw}
