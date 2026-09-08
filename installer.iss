; ============================================================
; AutoRename - Inno Setup 설치 스크립트
; Inno Setup(https://jrsoftware.org/isinfo.php)으로 컴파일하세요.
; 사전 준비: build_exe.bat 실행 -> dist\AutoRename.exe 생성 완료 상태여야 함.
; ============================================================

#define MyAppName "AutoRename"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "AutoRename"
#define MyAppExeName "AutoRename.exe"

[Setup]
; 관리자 권한 없이 각자 PC에 설치 가능하도록 사용자 폴더에 설치
AppId={{8F2C7B10-4A3D-4E9E-9B2A-2C6C7A0F2E11}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\AutoRename
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=AutoRenameSetup
SetupIconFile=app_icon.ico
Compression=lzma
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}

; ── 업그레이드 설치 대응 ────────────────────────────────────────────────
; 트레이에서 실행 중이면 AutoRename.exe 가 잠겨 있어 덮어쓰기가 실패한다.
; Restart Manager 로 실행 중인 인스턴스를 찾아 자동으로 닫고 설치한다.
; (닫지 못하면 재부팅을 요구하는 대신 사용자에게 물어보게 둔다)
CloseApplications=yes
CloseApplicationsFilter=AutoRename.exe
; 설치가 끝나면 [Run] 항목이 다시 띄우므로 Restart Manager 쪽 자동 재시작은 끈다
RestartApplications=no

; 사용자 설정은 설치 폴더가 아니라 홈 디렉터리에 있으므로 재설치·제거해도 보존된다.
;   ~\rename_watcher_config.json        설정(API 키·감시 폴더·사용자 지침)
;   ~\rename_watcher_learning_log.json  사용자 수정 학습 기록
;   ~\rename_watcher_pattern_summary.json
;   ~\rename_watcher_quota_state.json   무료/유료 키 전환 상태
;   ~\rename_watcher.log                동작 로그
; [Files] 는 exe 하나만 설치하고 [UninstallDelete] 도 {app} 만 지우므로 위 파일은 건드리지 않는다.

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "autostart"; Description: "Windows 시작 시 자동으로 실행"; GroupDescription: "추가 옵션:"; Flags: checkedonce
Name: "desktopicon"; Description: "바탕화면에 바로가기 만들기"; GroupDescription: "추가 옵션:"; Flags: unchecked

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "설치 완료 후 바로 실행"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
