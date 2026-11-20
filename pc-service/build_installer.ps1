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

# ---- 代码签名（自签证书；没证书就跳过，不阻塞构建）----
# 证书由 pc-service\make_codesign_cert.ps1 生成并导入「当前用户/受信任的根」，
# 这样本机双击安装包不会再出「未知发布者」。其他机器要信任需导入同一个 .cer。
$certSubject = 'CN=WILLEN RemoteMic, O=WILLEN, C=CN'

function Sign-Exe([string]$Path) {
    $cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
        Where-Object { $_.Subject -eq $certSubject } | Select-Object -First 1
    if (-not $cert) {
        Write-Host '      跳过签名：未找到证书（先跑 pc-service\make_codesign_cert.ps1）'
        return
    }
    # 刚跑过 --check-driver / 安装包刚编译完时文件可能还被占用，退避重试几次
    $sig = $null
    for ($i = 0; $i -lt 6; $i++) {
        try {
            $sig = Set-AuthenticodeSignature -FilePath $Path -Certificate $cert -HashAlgorithm SHA256 -TimestampServer 'http://timestamp.digicert.com'
            break
        }
        catch {
            Start-Sleep -Seconds 2
        }
    }
    if (-not $sig) { throw "签名失败：$Path（文件被占用或签名服务不可用）" }
    if ($sig.Status -ne 'Valid') { throw "签名失败：$Path（$($sig.StatusMessage)）" }
    Write-Host "      已签名 $(Split-Path $Path -Leaf)"
}

if (-not (Test-Path $pyinstaller)) { throw "缺少 PyInstaller：uv pip install --python .venv\Scripts\python.exe pyinstaller" }
if (-not (Test-Path 'pc-service\vendor\vb-cable\VBCABLE_Setup_x64.exe')) { throw '缺少 VB-CABLE 驱动包：pc-service\vendor\vb-cable\' }
if (-not (Test-Path 'installer\redist\MicrosoftEdgeWebview2Setup.exe')) { throw '缺少 WebView2 引导器：installer\redist\' }

Write-Host '[1/4] PyInstaller 打包…'
& $pyinstaller --noconfirm --clean 'pc-service\RemoteMicService.spec' --distpath 'pc-service\dist' --workpath 'pc-service\build'
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（exit=$LASTEXITCODE）" }

$exe = 'pc-service\dist\RemoteMicService\RemoteMicService.exe'
if (-not (Test-Path $exe)) { throw '打包产物缺失' }
Write-Host '[2/4] 自检：--check-driver'
# 用 Start-Process -Wait 等它真正退出：直接 & 调用 GUI 子系统程序 PowerShell 不等待，
# 进程没退就签名会报 "The process cannot access the file ... being used by another process"
$probe = Start-Process -FilePath $exe -ArgumentList '--check-driver' -WindowStyle Hidden -PassThru -Wait
Write-Host "      驱动检测退出码 = $($probe.ExitCode)（0=已装，1=未装，都算正常）"

Write-Host '[3/4] 代码签名（主程序）…'
Sign-Exe $exe

Write-Host '[4/4] Inno Setup 编译…'
$iscc = @(
    'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
    'C:\Program Files\Inno Setup 6\ISCC.exe',
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw '找不到 ISCC.exe，请先安装 Inno Setup 6' }
& $iscc $iss
if ($LASTEXITCODE -ne 0) { throw "ISCC 失败（exit=$LASTEXITCODE）" }

$setup = Get-ChildItem 'dist-installer\*.exe' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($setup) {
    Write-Host '[4/4] 代码签名（安装包）…'
    Sign-Exe $setup.FullName
}

$setup | Select-Object Name, @{n = 'MB'; e = { '{0:N1}' -f ($_.Length / 1MB) } }, LastWriteTime

