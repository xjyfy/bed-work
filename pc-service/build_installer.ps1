# 一键构建 Windows 安装包（PyInstaller onedir → Inno Setup）
#
# 用法（在仓库根目录或任意位置）：
#     powershell -ExecutionPolicy Bypass -File pc-service\build_installer.ps1
# 产物：
#     pc-service\dist\RemoteMicService\...        运行目录（可直接跑）
#     dist-installer\RemoteMicSetup-<版本>.exe    安装包
#
# 前置：
#   · 仓库 .venv 里已装 pyinstaller（uv pip install pyinstaller）
#   · 已安装 Inno Setup 6（winget install --id JRSoftware.InnoSetup -e）
#   · installer\redist\MicrosoftEdgeWebview2Setup.exe 已就位
#   · pc-service\vendor\vb-cable\ 里有 VBCABLE_Setup_x64.exe（驱动包，随包分发）

$ErrorAll = @()
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

$py = Join-Path $root '.venv\Scripts\python.exe'
$pyinstaller = Join-Path $root '.venv\Scripts\pyinstaller.exe'
$iss = Join-Path $root 'installer\RemoteMic.iss'

if (-not (Test-Path $pyinstaller)) { throw "缺少 PyInstaller：uv pip install --python .venv\Scripts\python.exe pyinstaller" }
if (-not (Test-Path 'pc-service\vendor\vb-cable\VBCABLE_Setup_x64.exe')) { throw '缺少 VB-CABLE 驱动包：pc-service\vendor\vb-cable\' }
if (-not (Test-Path 'installer\redist\MicrosoftEdgeWebview2Setup.exe')) { throw '缺少 WebView2 引导器：installer\redist\' }

# 版本号有两处：remote_mic/__init__.py（设置页显示）与 iss 的 AppVersion（安装包/卸载注册表）。
# 不一致会让用户看到的版本和实际装的不是一个，打包前直接拦住。
$pyVer = (& $py -c "import sys; sys.path.insert(0,'pc-service'); from remote_mic import __version__; print(__version__)")
$issText = Get-Content 'installer\RemoteMic.iss' -Raw -Encoding UTF8
if ($issText -notmatch '#define\s+AppVersion\s+"([^"]+)"') { throw 'iss 里找不到 #define AppVersion' }
$issVer = $Matches[1]
if ($pyVer -ne $issVer) { throw "版本号不一致：remote_mic/__init__.py=$pyVer，RemoteMic.iss=$issVer（两处都要改）" }
Write-Host "      版本号一致：$pyVer"

Write-Host '[1/3] PyInstaller 打包…'
& $pyinstaller --noconfirm --clean 'pc-service\RemoteMicService.spec' --distpath 'pc-service\dist' --workpath 'pc-service\build'
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（exit=$LASTEXITCODE）" }

$exe = 'pc-service\dist\RemoteMicService\RemoteMicService.exe'
if (-not (Test-Path $exe)) { throw '打包产物缺失' }
Write-Host '[2/3] 自检：--check-driver'
# GUI 子系统程序用 Start-Process -Wait 才可靠：直接 & 调用 PowerShell 不等待，
# 进程没退就读退出码会拿到上一条命令的假值
$probe = Start-Process -FilePath $exe -ArgumentList '--check-driver' -WindowStyle Hidden -PassThru -Wait
Write-Host "      驱动检测退出码 = $($probe.ExitCode)（0=已装，1=未装，都算正常）"

Write-Host '[3/3] Inno Setup 编译…'
$iscc = @(
    'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
    'C:\Program Files\Inno Setup 6\ISCC.exe',
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw '找不到 ISCC.exe，请先安装 Inno Setup 6' }
& $iscc $iss
if ($LASTEXITCODE -ne 0) { throw "ISCC 失败（exit=$LASTEXITCODE）" }

Get-ChildItem 'dist-installer\*.exe' | Sort-Object LastWriteTime -Descending |
    Select-Object -First 1 Name, @{n = 'MB'; e = { '{0:N1}' -f ($_.Length / 1MB) } }, LastWriteTime

