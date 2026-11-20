# 生成（或复用）自签代码签名证书，并导入「当前用户\受信任的根证书颁发机构」。
#
# 用法：pwsh -File pc-service\make_codesign_cert.ps1
#
# 说明：
#   · 自签证书只解决「本机不再弹未知发布者」；换一台机器要信任，需把导出的
#     %LOCALAPPDATA%\RemoteMic\RemoteMicCodesign.cer 导入那台机器的「受信任的根」。
#   · 对外分发（别人装的机器不受你控制）仍需 OV/EV 商业证书。
#   · 生成的证书留在 当前用户\个人 存储里，build_installer.ps1 会自动找它签名。

$subject = 'CN=WILLEN RemoteMic, O=WILLEN, C=CN'

$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
    Where-Object { $_.Subject -eq $subject } | Select-Object -First 1

if (-not $cert) {
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $subject `
        -KeyUsage DigitalSignature -FriendlyName 'RemoteMic 自签代码签名' `
        -CertStoreLocation 'Cert:\CurrentUser\My' -NotAfter (Get-Date).AddYears(5) `
        -KeyExportPolicy Exportable -HashAlgorithm SHA256
    Write-Host "已创建证书：$($cert.Thumbprint)"
}
else {
    Write-Host "复用已有证书：$($cert.Thumbprint)"
}

$cer = Join-Path $env:LOCALAPPDATA 'RemoteMic\RemoteMicCodesign.cer'
New-Item -ItemType Directory -Force -Path (Split-Path $cer) | Out-Null
Export-Certificate -Cert $cert -FilePath $cer -Type CERT | Out-Null
certutil -addstore -user Root $cer | Out-Null

Write-Host "证书文件：$cer"
Write-Host "到期时间：$($cert.NotAfter)"
