; 手机遥控麦 —— Windows 安装包（Inno Setup 6）
;
; 设计口径见仓库 `打包与安装包需求_2026-11-20.md`：
;   · 装到 %ProgramFiles%\RemoteMic（要装驱动，本来就需要管理员）
;   · 运行期数据（config.json / logs / icons）在 %LOCALAPPDATA%\RemoteMic，升级与卸载都不动它
;   · VB-CABLE 随包内置，静默安装；已装则跳过
;   · WebView2 缺失时用随包引导器联网安装（不内置 CEF，省 100MB）
;   · 防火墙只放行专用/域网络的 8765，不按端口对公网开放
;
; 编译：ISCC.exe installer\RemoteMic.iss   （或跑 pc-service\build_installer.ps1）

#define AppName "手机遥控麦"
#define AppVersion "1.0.0"
#define AppExe "RemoteMicService.exe"

[Setup]
AppId={{8B6E2F31-4C7A-4B2E-9A51-6D0C3B8E7F10}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=WILLEN
DefaultDirName={autopf}\RemoteMic
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist-installer
OutputBaseFilename=RemoteMicSetup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExe}
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "lang\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked
Name: "autostart"; Description: "开机自动启动（登录后自动运行托盘服务）"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "..\pc-service\dist\RemoteMicService\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "redist\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: not WebView2Installed

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{#AppName} 设置"; Filename: "{app}\{#AppExe}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; 开机自启与托盘右键里的勾选项是同一个值，装完托盘里就是勾上的
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "RemoteMic"; ValueData: """{app}\{#AppExe}"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
; 1) WebView2 运行时（设置页是 pywebview 界面，Win10 可能没有）
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; \
  StatusMsg: "正在安装 WebView2 运行时…"; Flags: waituntilterminated; Check: not WebView2Installed
; 2) VB-CABLE 虚拟声卡（-i -h = 静默安装，无需重启）
Filename: "{app}\_internal\vb-cable\VBCABLE_Setup_x64.exe"; Parameters: "-i -h"; \
  StatusMsg: "正在安装 VB-CABLE 虚拟声卡…"; Flags: runhidden waituntilterminated; Check: not DriverInstalled
; 3) 防火墙：先删旧规则再加，只放行专用/域网络
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""RemoteMic 8765"""; \
  Flags: runhidden waituntilterminated; StatusMsg: "正在配置防火墙…"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""RemoteMic 8765"" dir=in action=allow protocol=TCP localport=8765 program=""{app}\{#AppExe}"" profile=private,domain"; \
  Flags: runhidden waituntilterminated; StatusMsg: "正在放行局域网 8765 端口…"
; 4) 装完直接启动（静默安装不弹）
Filename: "{app}\{#AppExe}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM {#AppExe} /F"; Flags: runhidden waituntilterminated; RunOnceId: "KillService"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""RemoteMic 8765"""; Flags: runhidden waituntilterminated; RunOnceId: "DelFirewall"

[Code]
const
  WebView2Guid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function DriverInstalled(): Boolean;
var
  Code: Integer;
begin
  // 用程序自己的检测逻辑（pnputil + 设备节点），比注册表关键字靠谱
  Result := False;
  if FileExists(ExpandConstant('{app}\{#AppExe}')) then
  begin
    Exec(ExpandConstant('{app}\{#AppExe}'), '--check-driver', '',
         SW_HIDE, ewWaitUntilTerminated, Code);
    Result := (Code = 0);
  end;
end;

function WebView2Installed(): Boolean;
begin
  Result :=
    RegKeyExists(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WebView2Guid) or
    RegKeyExists(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WebView2Guid) or
    RegKeyExists(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\' + WebView2Guid);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if not DriverInstalled() then
      MsgBox('VB-CABLE 虚拟声卡没有装上，语音功能暂时不可用。' + #13#10 +
             '可以在设置页点「安装虚拟声卡」重试；若仍失败，可能是安全策略拦了未签名驱动。',
             mbInformation, MB_OK);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
  Code: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // 驱动动了会影响其他在用 CABLE 的软件，必须由用户确认
    if DriverInstalled() then
    begin
      if MsgBox('是否同时卸载 VB-CABLE 虚拟声卡？' + #13#10 +
                '注意：录像/直播等其他软件如果也在用 CABLE，卸载后会一起失效。',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      begin
        if FileExists(ExpandConstant('{app}\_internal\vb-cable\VBCABLE_Setup_x64.exe')) then
          Exec(ExpandConstant('{app}\_internal\vb-cable\VBCABLE_Setup_x64.exe'), '-u -h', '',
               SW_HIDE, ewWaitUntilTerminated, Code);
      end;
    end;

    DataDir := ExpandConstant('{localappdata}\RemoteMic');
    if DirExists(DataDir) then
    begin
      if MsgBox('是否删除配置与日志（%LOCALAPPDATA%\RemoteMic）？' + #13#10 +
                '配置里有绑定 token，删掉之后手机需要重新扫码绑定。',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
