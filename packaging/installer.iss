; display-kvm Windows 安装包脚本
; 构建（在 display-kvm 目录下）：
;   bash packaging/build_win.sh              # 一键：装环境 -> 打 exe -> 打安装包
; 或只编译安装包（需已装 Inno Setup 6）：
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
; 产物：dist\installer\Display-KVM-Setup-1.0.1.exe

#define MyAppName "Display KVM"
#define MyAppNameCn "显示器输入源自动切换"
#define MyAppVersion "1.0.10"
#define MyAppExeName "display-kvm.exe"
#define MyAppCtlName "display-kvm-ctl.exe"

[Setup]
AppId={{7C1A2E9F-4B36-4D8A-9E52-DISPLAYKVM100}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=display-kvm
DefaultDirName={localappdata}\Programs\display-kvm
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 每用户安装：不需要管理员权限，不弹 UAC
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename=Display-KVM-Setup-{#MyAppVersion}
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 我们的程序在运行时占用控制口，安装/卸载前先结束它
CloseApplications=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "autostart"; Description: "开机自动启动 {#MyAppName}"; \
    GroupDescription: "其他:"; Flags: checkedonce
Name: "desktopicon"; Description: "创建桌面快捷方式"; \
    GroupDescription: "其他:"; Flags: unchecked

[Files]
Source: "..\dist\display-kvm.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\display-kvm-ctl.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
; 与 app.py 托盘里的「开机自启」开关写的是同一个注册表值，两边互相兼容
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "display-kvm"; \
    ValueData: """{app}\{#MyAppExeName}"""; \
    Tasks: autostart; Flags: uninsdeletevalue

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    Comment: "{#MyAppNameCn}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]

[Code]
// 安装/卸载前结束正在运行的实例，避免文件被占用
procedure KillApp;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{cmd}'), '/C taskkill /F /IM display-kvm.exe /T>nul 2>nul', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function InitializeSetup(): Boolean;
begin
  KillApp;
  Result := True;
end;

function InitializeUninstall(): Boolean;
begin
  KillApp;
  Result := True;
end;
