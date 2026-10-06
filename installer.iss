; ============================================================
; AutoRename - Inno Setup 설치 스크립트
; Inno Setup(https://jrsoftware.org/isinfo.php)으로 컴파일하세요.
; 사전 준비: PyInstaller --onedir 빌드 -> dist\AutoRename\ 폴더(AutoRename.exe + _internal\) 생성 완료 상태여야 함.
; ============================================================

#define MyAppName "AutoRename"
; ⚠️ updater.py 의 APP_VERSION 과 반드시 같게 맞춘다
#define MyAppVersion "1.3.1"
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
; 번들한 Python 3.14 는 Windows 10 이상 전용이다(python314.dll 이 Win7 에 없는 api-ms-win-core-path 를 쓰고,
; UCRT 도 Win10 부터 기본 내장). 미만이면 실행 때 "Failed to load Python DLL" 로만 죽으므로 설치 단계에서 막고 이유를 보여 준다.
MinVersion=10.0

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

[InstallDelete]
; 이전 버전의 라이브러리 폴더를 비우고 새로 깐다(빠진 파일이 남아 버전이 섞이는 것 방지).
Type: filesandordirs; Name: "{app}\_internal"

[Files]
; v1.3.1 부터 --onedir 빌드. 종전 --onefile 은 실행할 때마다 %TEMP%\_MEI… 에 파이썬을 풀어 썼는데,
; 백신이 그 폴더를 막거나 지우면 "Failed to load Python DLL" 로 아예 안 켜졌다(2026-10-06 배포처 PC 실측).
; 설치 폴더에 미리 풀어 두면 그 단계가 없어지고 켜지는 속도도 빨라진다.
Source: "dist\AutoRename\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "설치 완료 후 바로 실행"; Flags: nowait postinstall skipifsilent
; 트레이 「업데이트 확인」이 /SILENT 로 설치할 때는 위 항목이 건너뛰어지므로 여기서 다시 띄운다
Filename: "{app}\{#MyAppExeName}"; Flags: nowait runasoriginaluser; Check: WizardSilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
// 트레이 앱(pystray)은 Restart Manager 의 종료 요청에 응답하지 않아 위 CloseApplications 만으로는
// "모든 응용 프로그램을 자동으로 닫지 못했습니다" 창이 뜬다(2026-10-06 실측). 그래서 설치 직전에 직접 끈다.
// 설정·로그는 홈 디렉터리에 즉시 저장되는 구조라 강제 종료해도 잃는 것이 없다.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#MyAppExeName}', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Sleep(1000);  // 파일 잠금이 풀릴 시간
  Result := '';
end;
