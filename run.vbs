' 소스로 실행할 때 쓰는 런처. 콘솔창 없이 트레이 앱만 띄운다.
' 이 스크립트가 놓인 폴더를 기준으로 tray_app.py 를 찾으므로 폴더를 옮겨도 그대로 동작한다.
' (시작프로그램에 이 파일의 바로가기를 넣어두면 부팅 시 자동 실행된다.)
Dim fso, here
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("Wscript.Shell").Run "pythonw.exe """ & here & "\tray_app.py""", 0, False
