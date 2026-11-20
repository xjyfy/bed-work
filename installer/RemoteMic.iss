; 手机遥控麦 —— Windows 安装包（Inno Setup 6）
;
; 设计口径见 docs/history/打包与安装包需求_2026-11-20.md：
;   · 装到 %ProgramFiles%\RemoteMic（要装驱动，本来就需要管理员）
;   · 运行期数据（config.json / logs / icons）在 %LOCALAPPDATA%\RemoteMic，升级与卸载都不动它
;   · VB-CABLE 随包内置，静默安装；已装则跳过
;   · WebView2 缺失时用随包引导器联网安装（不内置 CEF，省 100MB）
;   · 防火墙只放行专用/域网络的 8765，不按端口对公网开放
;
; 编译：ISCC.exe installer\RemoteMic.iss   （或跑 pc-service\build_installer.ps1）
;
; 2026-11-20 修复（"安装失败。已为系统安装 Microsoft Edge Webview2 Runtime."）：
;   1. WebView2 检测漏判 —— 机器级安装的注册表键在 32 位视图（WOW6432Node），
;      原写法只查 HKLM（64 位模式下被重定向到 64 位视图），所以判成"没装"→ 仍去跑引导器；
;      现改为 HKLM64 + HKLM32 + HKCU 三处都查，再加文件路径兜底。
;   2. WebView2 引导器在"已安装"时会自己弹框并返回非 0，Inno 的 [Run] 见到非 0 就判定安装失败并回滚。
;      → 把 WebView2 / VB-CABLE / netsh 三步从 [Run] 移到 [Code]，用 Exec 忽略返回码，
;        失败只做提示，绝不让整个安装失败（netsh 删除不存在规则本来就会返回 1，同理）。
;   3. 卸载时驱动卸载挪到 usUninstall —— 原在 usPostUninstall，那时 {app} 里的 VBCABLE_Setup 已被删掉，
;      FileExists 恒假，驱动永远卸不掉。

#define AppName "手机遥控麦"
#define AppVersion "1.0.2"
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
; 前置组件（WebView2 / VB-CABLE / 防火墙）在 [Code] 的 InstallPrerequisites 里执行，
; 因为这三步的工具在"已完成/规则不存在"时会返回非 0，放在 [Run] 会让安装整体失败并回滚。
; 这里只保留最后的启动入口。
Filename: "{app}\{#AppExe}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM {#AppExe} /F"; Flags: runhidden waituntilterminated; RunOnceId: "KillService"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""RemoteMic 8765"""; Flags: runhidden waituntilterminated; RunOnceId: "DelFirewall"

[Code]
const
  WebView2Guid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

{ ---------------- 检测 ---------------- }

function WebView2Installed(): Boolean;
var
  RegPath: String;
  Pv: String;
begin
  RegPath := 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WebView2Guid;
  Pv := '';

  { 机器级 64 位 / 机器级 32 位（WOW6432Node）/ 当前用户，三处都查。
    注意 Inno 的 setup 进程是 32 位：在 ArchitecturesInstallIn64BitMode 下
    HKLM 会被解析成 64 位视图，只看 HKLM 会漏掉装在 WOW6432Node 的 WebView2。
    判断 pv 值非空，避免把卸载残留的空键当成"已安装"。 }
  Result := RegQueryStringValue(HKLM64, RegPath, 'pv', Pv) and (Pv <> '') and (Pv <> '0.0.0.0');
  if not Result then
    Result := RegQueryStringValue(HKLM32, RegPath, 'pv', Pv) and (Pv <> '') and (Pv <> '0.0.0.0');
  if not Result then
    Result := RegQueryStringValue(HKCU, RegPath, 'pv', Pv) and (Pv <> '') and (Pv <> '0.0.0.0');
end;

function DriverInstalled(): Boolean;
var
  Code: Integer;
begin
  { 用程序自己的检测逻辑（pnputil + 设备节点），比注册表关键字靠谱 }
  Result := False;
  if FileExists(ExpandConstant('{app}\{#AppExe}')) then
  begin
    Exec(ExpandConstant('{app}\{#AppExe}'), '--check-driver', '',
         SW_HIDE, ewWaitUntilTerminated, Code);
    Result := (Code = 0);
  end;
end;

{ ---------------- 安装前置组件（失败不中断安装） ---------------- }

procedure InstallPrerequisites();
var
  Code: Integer;
  WebOk, DrvOk, FwOk, AddStarted: Boolean;
  Msg: String;
begin
  { 1) WebView2：只有缺失才装；引导器"已装"时会弹框，所以检测必须准 }
  WebOk := WebView2Installed();
  if not WebOk then
  begin
    if FileExists(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe')) then
      Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'), '/silent /install', '',
           SW_HIDE, ewWaitUntilTerminated, Code);
    WebOk := WebView2Installed();
  end;

  { 2) VB-CABLE：-i -h 静默安装，无需重启；3010/1641 也视为装上了 }
  DrvOk := DriverInstalled();
  if not DrvOk then
  begin
    if FileExists(ExpandConstant('{app}\_internal\vb-cable\VBCABLE_Setup_x64.exe')) then
      Exec(ExpandConstant('{app}\_internal\vb-cable\VBCABLE_Setup_x64.exe'), '-i -h', '',
           SW_HIDE, ewWaitUntilTerminated, Code);
    DrvOk := DriverInstalled();
  end;

  { 3) 防火墙：先删旧规则（不存在时 netsh 返回 1，属正常），再加新规则 }
  Exec(ExpandConstant('{sys}\netsh.exe'),
       'advfirewall firewall delete rule name="RemoteMic 8765"', '',
       SW_HIDE, ewWaitUntilTerminated, Code);

  AddStarted := Exec(ExpandConstant('{sys}\netsh.exe'),
       'advfirewall firewall add rule name="RemoteMic 8765" dir=in action=allow protocol=TCP ' +
       'localport=8765 program="' + ExpandConstant('{app}\{#AppExe}') + '" profile=private,domain',
       '', SW_HIDE, ewWaitUntilTerminated, Code);
  FwOk := AddStarted and (Code = 0);

  { 4) 有缺项才提示，安装本身已经成功 }
  if (not WebOk) or (not DrvOk) or (not FwOk) then
  begin
    Msg := '安装已完成，但以下组件未就绪：' + #13#10;
    if not WebOk then
      Msg := Msg + '· WebView2 运行时缺失：设置页会回退到简易界面（联网后可手动安装常见运行库）' + #13#10;
    if not DrvOk then
      Msg := Msg + '· VB-CABLE 虚拟声卡未装好：语音输入暂不可用，可在设置页点「安装虚拟声卡」重试' + #13#10;
    if not FwOk then
      Msg := Msg + '· 防火墙规则添加失败：手机可能连不上，请在 Windows 安全中心允许 {#AppExe} 通过专用网络' + #13#10;
    MsgBox(Msg, mbInformation, MB_OK);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    InstallPrerequisites();
end;

{ ---------------- 卸载 ---------------- }

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
  Code: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    { 驱动卸载必须在文件删除之前：此阶段程序目录里的 VBCABLE_Setup 还在，
      usPostUninstall 时它已经被删掉，FileExists 恒假，驱动永远卸不掉。 }
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
  end
  else if CurUninstallStep = usPostUninstall then
  begin
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
