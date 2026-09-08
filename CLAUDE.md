\# CLAUDE.md — Download Rename Watcher



이 문서는 Claude Code / Cowork가 이 프로젝트를 새 세션에서 이어받을 때 읽는 컨텍스트 파일입니다.

사람에게 매번 처음부터 설명하지 않아도 되도록, 지금까지의 설계 결정과 알려진 이슈를 압축해서 기록합니다.



\## 프로젝트 개요



Windows용 다운로드 폴더 자동 정리 프로그램. `C:\\Users\\PC\\Downloads`를 감시하다가 새 파일이 생기면

AI(Gemini/OpenAI/Claude 중 설정에서 고른 공급자)가 파일 내용을 보고 이름을 자동으로 정리해준다.

콘솔창 없이 시스템 트레이 아이콘으로 상주. (2026-07부터 Gemini 전용에서 다중 공급자 지원으로 전환, 아래 "AI 공급자 선택 기능" 항목 참고.)



\## 파일 구성 (3개, 같은 폴더에 있어야 함)



\- \*\*`rename\_watcher.py`\*\* — 핵심 엔진. watchdog으로 폴더 감시, 파일 안정화/잠금 대기, rename 실행,

&#x20; 실행취소 이력, 7일 지난 파일 자동 분류, 일반 이동 규칙(move\_rules) 적용, 토스트/팝업 UI(tkinter), 로그 기록.

\- \*\*`file\_namer.py`\*\* — AI 공급자(Gemini/OpenAI/Claude) 호출 담당. 파일 내용 추출(엑셀/워드/PDF/이미지/텍스트),

&#x20; 프롬프트 구성, 공급자별 API 호출(`_call_gemini`/`_call_openai`/`_call_claude`) 및 응답 파싱, 설정 파일(config.json)

&#x20; 로드/저장, 사용자 수정 학습 로그.

\- \*\*`tray\_app.py`\*\* — 진입점. pystray로 트레이 아이콘 + 우클릭 메뉴, 별도 프로세스로 뜨는 환경설정 GUI.

&#x20; `pythonw.exe tray\_app.py`로 실행하면 콘솔창 없이 동작.



\## 실행 방법



```

pythonw.exe tray\_app.py

```



필요 패키지: `watchdog openpyxl python-docx pymupdf pillow pystray`



\## 설정 파일 (사용자 홈 폴더에 저장, 코드에 하드코딩 금지)



\- `\~/rename\_watcher\_config.json` — API 키, 감시 폴더\*\*들\*\*(`watch\_folders` 리스트, 2026-07 다중 폴더 지원 추가), 카운트다운 초, 토큰 한도, 미리보기 길이, user\_rules(사용자 정의 지침), move\_rules(조건→폴더 자동 이동 규칙 리스트, 2026-07 추가)

\- `\~/rename\_watcher\_learning\_log.json` — 사용자가 "애매함" 팝업에서 AI 추천과 다르게 직접 수정한 사례 누적

\- `\~/rename\_watcher\_pattern\_summary.json` — 학습 로그 10개마다 AI가 요약한 패턴 (백그라운드 전용, GUI에 노출 안 함)

\- `\~/rename\_watcher\_failed\_files.json` — AI 분석 실패한 파일 목록 (2026-07 추가, 아래 "에러 파일 재시도 기능" 참고)

\- `\~/rename\_watcher.log` — 실행 로그. 1MB 넘으면 자동 로테이션(최근 500줄만 유지)



\*\*중요:\*\* API 키를 코드에 절대 하드코딩하지 말 것. 과거에 실수로 박혔다가 제거한 이력 있음.



\## 핵심 동작 흐름



1\. 새 파일 생성 감지 (`\~$` 임시파일, `.crdownload` 등 진행중 파일은 무시)

2\. 파일 크기 안정화 대기 → Gemini로 내용 분석 → 파일 잠금 해제 대기 → rename

&#x20;  (분석 전에 잠금 확인하면 분석 자체가 핸들을 다시 잡아버려서 순서가 중요함)

3\. AI 판단 3분류:

&#x20;  - \*\*ok\*\*: 이름 이미 적절 → 토스트만, 변경 없음

&#x20;  - \*\*rename\*\*: 명확히 변경 필요 → 자동으로 파일명 변경 + 토스트. 기본 형식은 `filename\_format` 설정값

&#x20;    (기본 `{date}\_{summary}\_{site}`)이지만, 사용자 정의 지침(user\_rules)이 특정 케이스에 대해 형식을 지정했다면

&#x20;    AI가 `custom\_filename`에 완성된 이름을 채워 그게 최우선 적용됨 (아래 "파일명 형식 하드코딩 제거" 항목 참고).

&#x20;  - \*\*ambiguous\*\*: 애매함 → 3초 카운트다운 확인 팝업 (입력창에 AI 추천값 미리 채워짐, 수정 가능)

4\. 일반 사용자 정의 이동 규칙 (2026-07 추가, `move\_rules`): 설정 GUI에서 "조건 설명 + 폴더"를 여러 개 등록 가능.

&#x20;  AI 프롬프트에 규칙 목록(인덱스 포함)을 주입 → AI가 파일이 조건에 맞으면 응답 JSON의 `matched\_rule\_index`에 인덱스를 채움

&#x20;  → `rename\_watcher.apply\_general\_move\_rule()`이 그 인덱스로 `move\_rules`에서 폴더를 찾아 이동시킴. decision이 ok/rename/ambiguous

&#x20;  어느 경우든(이름을 바꾸든 안 바꾸든) 적용됨. 이동 대상 폴더는 사용자가 GUI에서 직접 고른 경로만 쓰이므로 AI가 임의 경로를

&#x20;  만들어낼 위험은 없음. (과거엔 전신문만 별도 하드코딩된 고정 경로 자동 이동 로직이 있었으나 2026-07에 완전히 제거하고,

&#x20;  이 일반 move\_rules로 통합함 — 아래 "전신문 하드코딩 제거" 항목 참고.)

5\. 사용자가 "애매함" 팝업에서 AI 추천과 다르게 직접 수정하면 → 학습 로그에 기록 (동일값이면 기록 안 함)

&#x20;  → 10개 쌓이면 백그라운드 스레드에서 패턴 요약 갱신 → 이후 모든 분석 프롬프트에 참고용으로 슬쩍 끼워 넣음



\## 트레이 메뉴 기능



감시 일시정지/재개, 이름변경 실행 취소, 에러 파일 재시도(2026-07 추가, 아래 항목 참고), 지금 정리하기(7일↑ 파일을 확장자별 하위 폴더로 자동 분류:

문서/스프레드시트/이미지/압축파일/설치파일/기타), 환경 설정 및 추가 지침(별도 프로세스 GUI), 로그 보기, 종료.



\## 해결된 버그 / 알려진 함정 (재발 주의)



\- \*\*엑셀/PDF 파일이 계속 "Python에서 사용 중" 잠김\*\*: openpyxl/PyMuPDF가 파일 핸들을 오래 잡음.

&#x20; → 원본을 직접 열지 않고 임시 복사본(`\_read\_via\_copy`)을 만들어 분석 후 즉시 삭제하는 방식으로 완전 해결.

\- \*\*`\~$` 엑셀/워드 임시 잠금 파일이 새 파일로 오인식됨\*\* → `is\_ignored`에서 `\~$` 접두사 무시 처리함.

\- \*\*직접 rename한 결과 파일이 watchdog에 의해 다시 "새 파일"로 트리거됨(무한 루프)\*\* →

&#x20; `mark\_self\_renamed()` / `is\_self\_renamed()`로 자기 자신이 만든 경로는 이벤트 무시 (TTL 10초 자동 정리).

\- \*\*Gemini 2.5 Flash 응답이 JSON 중간에서 잘림(`finishReason: MAX\_TOKENS`)\*\*: thinking 토큰이 maxOutputTokens

&#x20; 한도를 같이 소모하기 때문. → `max\_output\_tokens` 기본값을 99999로 크게 잡아둠(실제 응답은 짧아서 비용 문제 없음).

\- \*\*미리보기 글자수/행수 제한 때문에 핵심 필드가 잘려서 AI가 못 봄\*\* (전신문의 Debtor 필드, 카드 사용내역의

&#x20; P열 카드번호 등) → 엑셀은 행 30개까지, 열 제한 없음으로 확대. `max\_preview\_chars` 기본 6000자.

&#x20; \*\*패턴\*\*: 뭔가 AI가 못 알아본다는 제보가 오면 먼저 "미리보기가 잘려서 못 본 거 아닌지" 의심할 것.

\- \*\*구버전 바이너리 .xls 파일에서 커스텀 프롬프트(셀 지정 규칙)가 완전히 무시됨 (2026-07 발생)\*\*: 카드사

&#x20; 다운로드 내역이 `.xls`(엑셀 97-2003 바이너리 포맷)로 오는 경우가 많은데, `extract\_excel\_preview()`가

&#x20; `openpyxl`만 썼음 → openpyxl은 `.xls`를 아예 못 읽어서(`InvalidFileException: openpyxl does not support the

&#x20; old .xls file format`) 즉시 "(엑셀을 읽을 수 없음: ...)"만 반환, AI가 셀 내용을 통째로 못 보고 파일명/도메인만으로

&#x20; 추측함 → 사용자가 "B2셀 카드이용날짜, P2셀 카드번호로 명명해줘" 같은 셀 지정 커스텀 프롬프트를 넣어도 씹히는 것처럼

&#x20; 보였던 근본 원인. \*\*패턴\*\*: 위 항목("미리보기 잘려서 못 봄")과 별개로, 커스텀 프롬프트가 안 먹히는 제보가 오면

&#x20; 확장자가 `.xls`(구버전)인지도 반드시 확인할 것 — `.xlsx`/`.xlsm`은 openpyxl 정상 동작하지만 `.xls`만 이 문제 있음.

&#x20; `extract\_excel\_preview()`에 확장자 분기를 추가해 `.xls`는 `\_extract\_excel\_preview\_xls()`(xlrd 사용)로 처리하도록

&#x20; 수정함. xlrd는 날짜 타입 셀(`XL\_CELL\_DATE`)을 숫자 시리얼로 반환하므로 `xlrd.xldate\_as\_datetime()`으로 변환해서

&#x20; 사람이 읽을 수 있는 날짜 문자열로 만들어줌. `requirements.txt`에 `xlrd` 추가함. \*\*중요: exe 재빌드 필요\*\*.

&#x20; \*\*배포 시 주의\*\*: 새 패키지를 requirements.txt에 추가하는 것만으로는 이미 설치된 사용자 환경에 반영 안 됨 —

&#x20; 소스로 직접 실행 중인 사용자는 `pip install xlrd`를 별도로 안내해야 하고(이 건에서 실제로 빠뜨렸다가 사용자가

&#x20; "No module named 'xlrd'" 오류를 만나 지적함), exe 빌드 환경에도 설치 후 재빌드해야 함. \*\*패턴\*\*: requirements.txt에

&#x20; 새 패키지를 추가할 때마다 설치 명령어(`pip install <패키지>`)를 반드시 함께 안내할 것.

\- \*\*API 요청 타임아웃 25초 → 5분으로 연장 (2026-07 추가)\*\*: PDF 등 분석이 오래 걸리는 파일에서

&#x20; "The read operation timed out" 오류가 발생 → `_call_gemini`/`_call_openai`/`_call_claude`/`update_pattern_summary`

&#x20; 4곳 모두에서 `urllib.request.urlopen(req, timeout=25)`로 하드코딩돼 있던 것을 `timeout=300`(5분)으로 늘림.

&#x20; \*\*주의\*\*: 이 타임아웃 오류(`socket.timeout`/`URLError`)는 `RETRYABLE_HTTP_CODES` 기반 재시도 로직 대상이 아님

&#x20; (그 로직은 `urllib.error.HTTPError`만 잡음) — 즉 타임아웃이 나면 재시도 없이 바로 실패 처리됨. 재발 시 재시도 대상에

&#x20; 타임아웃도 포함시킬지 검토할 것.

\- \*\*에러 파일 로그 + 재시도 기능 (2026-07 추가)\*\*: 기존엔 AI 분석이 실패하면 그 자리에서 수동 확인 팝업만 뜨고,

&#x20; 팝업을 놓치거나 그냥 닫으면 그 파일은 원래 이름 그대로 방치되며 다시 분석을 시도할 방법이 없었음(watchdog은 파일

&#x20; "생성" 이벤트에만 반응하므로, 이미 존재하는 파일을 재분석시킬 트리거가 없었음). → 실패한 파일을 별도 기록해두고

&#x20; 트레이 메뉴에서 한번에 재시도할 수 있게 함. `file_namer.py`에 `FAILED_FILES_PATH`

&#x20; (`~/rename_watcher_failed_files.json`) 및 `load_failed_files()`/`save_failed_files()`/`add_failed_file()`/

&#x20; `remove_failed_file()` 헬퍼 추가(기존 학습 로그와 동일하게 `_load_json_safe`/`_save_json_safe` 재사용).

&#x20; `rename_watcher.handle_rename()`에서: AI 분석이 실패(예외 또는 `None` 반환)하면 `add_failed_file()`로 기록(같은

&#x20; 파일이 다시 실패해도 중복 누적 안 하고 사유/시각만 갱신) → 이후 수동 팝업에서 사용자가 직접 이름을 정해 해결하면

&#x20; `remove_failed_file()`로 제거. 반대로 AI 분석이 성공하면(어떤 decision이든) 그 즉시 `remove_failed_file()` 호출해서

&#x20; 예전 실패 기록이 있었다면 정리. 새 함수 `rename_watcher.retry_failed_files()`가 실패 목록을 순회하며 각 파일에

&#x20; `handle_rename()`을 다시 호출(파일이 이미 없어졌으면 조용히 목록에서 제거), 네트워크 호출이 섞여 있어 트레이 UI가

&#x20; 멈추지 않도록 별도 스레드에서 실행. `tray_app.py` 트레이 메뉴에 "에러 파일 재시도" 항목 추가(이름변경 실행취소

&#x20; 바로 아래).

\- \*\*에러 파일 기록 당일 한정 (2026-08-26 추가)\*\*: 실패 기록이 계속 쌓여 며칠 지난 파일까지 재시도 대상이 되던 것을,
&#x20; 사용자 요청으로 \*\*그날 실패분만\*\* 유지하도록 변경. `file\_namer.load\_failed\_files()`가 로드 시점에 timestamp의
&#x20; 날짜가 오늘이 아닌 항목을 걸러내고 파일에도 즉시 반영(별도 정리 스케줄 불필요 — add/remove/재시도 모두 이 함수를
&#x20; 거치므로 한 곳 수정으로 전 경로 적용됨). timestamp는 isoformat이라 `startswith(오늘날짜)` 비교로 충분함.
&#x20; \*\*중요: exe 재빌드 필요\*\*(소스 실행 사용자는 재시작만 하면 됨).

\- \*\*환경설정 GUI가 화면보다 커서 저장 버튼이 안 보임\*\*: `resizable(False, False)` + 고정 630px가 원인이었음.

&#x20; → 화면 크기 기준 동적 크기 제한 + `resizable(True, True)` + 스크롤 가능한 캔버스로 전면 수정함.

\- \*\*원드라이브 동기화 여부\*\*: 사용자는 원드라이브 폴더 동기화를 쓰지 않음(로컬 전용) → config.json 등

&#x20; 민감 정보 파일의 클라우드 유출 위험 낮음. 이 전제가 바뀌면(원드라이브 켜면) 재점검 필요.

\- \*\*Gemini 모델 404 에러 (2026-07 발생)\*\*: `gemini-2.5-flash`가 "no longer available to new users"로

&#x20; 새로 발급받은 API 키에서 404 반환하기 시작함(기존 키는 2026-10-16 셧다운 전까지 계속 동작 가능하나,

&#x20; 신규 키는 즉시 막힘). `file\_namer.MODEL`을 `gemini-3.5-flash`(2026-07 기준 안정 최신 모델)로 교체함.

&#x20; \*\*패턴\*\*: Gemini 모델은 수시로 deprecate되므로, "API 호출 실패"/404 제보 오면 가장 먼저 최신 모델명을

&#x20; ai.google.dev/gemini-api/docs/models 에서 확인할 것. 이 사건을 계기로 API 에러 시 응답 본문(HTTPError.read())까지

&#x20; 로그에 남기도록 개선함(전에는 "HTTP Error 404: Not Found"만 찍혀서 원인 파악 불가했음).

\- \*\*Gemini 3.5 응답이 가끔 유효하지 않은 JSON을 냄 (2026-07 발생)\*\*: "Expecting ',' delimiter" 같은 파싱 에러가

&#x20; `finishReason: STOP`(정상 종료)인데도 발생함 → 토큰 제한이 아니라 문자열 값 안에 이스케이프 안 된 큰따옴표/줄바꿈이

&#x20; 들어간 게 원인으로 추정됨. 시스템 프롬프트에 "문자열 안에 큰따옴표/줄바꿈/백슬래시 쓰지 말 것" 규칙을 명시적으로

&#x20; 추가함. 파싱 실패 시 원본 응답 텍스트(800자)도 로그에 남기도록 개선(`raw\_text\_for\_log`).

\- \*\*구조화된 출력으로 근본 전환 + thinking 토큰 절감 (2026-07 추가)\*\*: 위 JSON 파싱 실패 건의 근본 원인을 프롬프트

&#x20; 지침만으로 막는 대신, Gemini의 구조화 출력 기능으로 API 레벨에서 스키마를 강제함. \*\*최초 시도한

&#x20; `generationConfig.responseFormat.text.{mimeType, schema}` 중첩 구조는 실제로는 존재하지 않는 필드였고 즉시

&#x20; `HTTP 400 Invalid value ... response_format.text.mime_type` 에러로 확인됨\*\* → 기존에도 쓰던 flat

&#x20; `responseMimeType: "application/json"` + `responseSchema: {...}` 필드가 맞는 방식이었음(3.x에서도 안 바뀜).

&#x20; 또한 Gemini의 Schema는 JSON Schema가 아니라 OpenAPI 3.0 서브셋이라 `"type": ["string","null"]` 같은 타입 배열을

&#x20; 못 쓰고, null 허용은 `"nullable": true`로 표현해야 함(이것도 처음엔 잘못 넣었다가 수정). `file\_namer.RESPONSE\_SCHEMA`

&#x20; 상수에 필드별 타입/enum/required/nullable을 정의(decision은 ok/rename/ambiguous enum, special\_category는

&#x20; wire\_transfer 또는 null 등). 또한 평균 4천→1만2천 토큰까지 치솟던 원인이 Gemini 3.5의 기본 thinking 레벨(medium)로

&#x20; 추정되어, `generationConfig.thinkingConfig.thinkingLevel: "low"`를 추가해 단순 분류/파일명 작업에 불필요한 사고

&#x20; 토큰 소모를 줄임(Gemini 2.5 시절 `thinkingBudget`과는 다른 3.x 전용 파라미터, 이 필드는 400 에러 없이 정상 동작).

&#x20; 시스템 프롬프트의 장황한 "오직 JSON으로만 응답해..." 지침 블록도 스키마가 강제하므로 짧게 정리함.

&#x20; \*\*패턴\*\*: Gemini API 필드명은 공식 문서/SDK 예제에서 실제 사용되는 걸 확인하고 쓸 것 — 그럴듯한 이름을 추측해서

&#x20; 넣으면 400 에러로 바로 걸러지긴 하지만 시간 낭비이니 애초에 예제 코드 기준으로 작성. \*\*중요: 이 변경은 exe

&#x20; 재빌드가 필요함\*\*(`build\_exe.bat` → `installer.iss` 컴파일 후 재배포해야 실제 동작에 반영됨).

\- \*\*파일명 형식 하드코딩 제거 + 사용자 지침 최우선 규칙 (2026-07 추가)\*\*: 기존엔 일반 케이스 파일명이 프롬프트와

&#x20; Python 코드 양쪽에 `"{today}\_{summary}\_{site}"`로 하드코딩돼 있어서, 사용자가 커스텀 프롬프트(`user\_rules`)에

&#x20; 특정 케이스에 대한 파일명 형식을 적어도 종종 무시됨. → 두 가지로 해결:

&#x20; (1) 기본 조합 형식을 `config.filename\_format`(기본값 `"{date}\_{summary}\_{site}"`)으로 설정 GUI화.

&#x20; `file\_namer.build\_filename\_from\_format()`이 `{date}/{summary}/{site}` 자리표시자로 조립하며, site가 비어있으면

&#x20; `{site}` 앞 구분자까지 함께 제거. 설정 GUI(`tray\_app.show\_settings\_gui`)에 "기본 파일명 형식" Entry 추가.

&#x20; (2) 그래도 case-by-case 예외가 필요할 수 있어서, 응답 스키마에 `custom\_filename`(nullable) 필드를 추가함.

&#x20; 사용자 정의 지침(`user\_rules`)이 특정 케이스에 대해 구체적인 파일명 형식을 명시했다면 AI가 완성된 최종 파일명을

&#x20; `custom\_filename`에 채우고, 이 값은 `filename\_format` 조합보다 최우선으로 적용됨

&#x20; (`file\_namer.analyze\_file()`의 `suggested\_name` 결정 순서: custom\_filename → 기본 형식 조합).

&#x20; \*\*중괄호 선택사항화 (2026-07 추가)\*\*: 사용자가 "굳이 중괄호 안 써도 알아서 인식하지 않냐"고 물어봐서,

&#x20; `build\_filename\_from\_format()`이 `{date}` 같은 중괄호 표기와 `date` 같은 맨 단어 표기를 모두 인식하도록 정규식을

&#x20; 고침. \*\*주의\*\*: 정규식 `\b`(단어 경계)는 밑줄(`\_`)을 단어문자로 취급해서 `date\_summary\_site`처럼 밑줄로 붙어있으면

&#x20; `\bdate\b`가 매치 안 됨(놓칠 뻔한 버그, 실제 테스트로 발견) → 영문자 앞뒤만 보는 `(?\<!\[A-Za-z\])date(?!\[A-Za-z\])`

&#x20; 방식으로 교체해서 해결. GUI 설명 문구도 중괄호가 선택사항임을 명시하도록 수정.

&#x20; 해당 케이스가 아니면 custom\_filename은 null로 응답하도록 프롬프트에 명시.

\- \*\*전신문(해외송금 확인서) 하드코딩 완전 제거 (2026-07 추가)\*\*: 위 custom\_filename 기능이 생기면서, 전신문만을 위해

&#x20; 존재하던 하드코딩된 특수 케이스(프롬프트의 `[특수 케이스: 전신문 / 해외송금 확인서]` 섹션, 응답 스키마의

&#x20; `special_category`/`sender`/`currency_amount`/`value_date` 필드, `file\_namer.analyze\_file()`의 wire\_transfer 분기,

&#x20; `rename\_watcher.py`의 `WIRE_TRANSFER_FOLDER` 상수 및 전용 이동 로직)를 전부 삭제함. 이제 전신문 인식/네이밍은

&#x20; 사용자가 환경설정의 "AI 추가 설정 및 커스텀 프롬프트 지침"(user\_rules)에 형식을 직접 적으면 AI가 `custom\_filename`으로

&#x20; 처리하고, 지정 폴더로의 자동 이동은 일반 `move\_rules`에 "전신문/해외송금 확인서" 조건으로 등록하면 처리됨.

&#x20; 즉 전신문은 더 이상 특별 취급되지 않고 다른 모든 케이스와 동일하게 사용자가 GUI에서 직접 설정하는 방식으로 통합됨.

&#x20; \*\*주의\*\*: 기존에 이 로직에 의존하던 사용자는 배포판 업데이트 후 환경설정 GUI에서 커스텀 프롬프트("전신문/해외송금

&#x20; 확인서면 '전신문\_송금인 통화금액\_YYMMDD' 형식으로 지어줘" 등)와 이동 규칙("전신문/해외송금 확인서" →

&#x20; `D:\문서\송금\전신문` 같은 목적지 폴더)을 직접 등록해야 예전과 동일하게 동작함.

\- \*\*AI 공급자 선택 기능 (Gemini/OpenAI/Claude, 2026-07 추가)\*\*: 사용자마다 자기가 쓰고 싶은 AI로 바꿔 쓸 수 있게

&#x20; Gemini 전용 구조를 다중 공급자 구조로 리팩터링함. `file\_namer.py`에 `_call_gemini()`/`_call_openai()`/`_call_claude()`

&#x20; 세 함수를 만들어 각 공급자의 REST API를 직접 호출하고, `(parsed: dict, usage: dict)`로 정규화해서 반환하게 통일함

&#x20; (`analyze\_file()`은 `config["ai_provider"]`에 따라 셋 중 하나를 골라 호출할 뿐, 그 이후 decision/summary/site/

&#x20; custom\_filename 처리 로직은 공급자와 무관하게 완전히 동일). 설정 GUI(`tray\_app.show\_settings\_gui`)에 "AI 공급자"

&#x20; 콤보박스와 Gemini/OpenAI/Claude API 키 입력칸 3개를 모두 상시 노출(선택된 공급자의 키만 실제 사용, 나머지는

&#x20; 미리 입력해뒀다가 나중에 공급자만 바꿔도 되게 함). config 스키마에 `ai_provider`(기본값 "gemini"), `openai_api_key`,

&#x20; `claude_api_key` 추가(누락 키 자동 보완 로직 덕분에 구버전 config.json도 하위호환됨).

&#x20; \*\*공급자별 구현 디테일\*\*: 프롬프트/응답 스키마는 공급자와 무관하게 하나로 유지하되, 스키마 "문법"만 공급자별로

&#x20; 다르게 표현함 — Gemini는 OpenAPI 3.0 서브셋이라 `RESPONSE_SCHEMA`에서 null 허용을 `"nullable": true`로 표현(기존

&#x20; 그대로 유지). OpenAI/Claude는 진짜 JSON Schema를 쓰므로 `RESPONSE_JSON_SCHEMA`를 새로 만들어 `"type": [...,

&#x20; "null"]` + `"additionalProperties": false`로 표현. OpenAI는 `chat/completions` 엔드포인트에

&#x20; `response_format: {type: "json_schema", json_schema: {name, schema, strict: true}}`로 구조화 출력을 강제하고

&#x20; (모델은 저비용/고속인 `gpt-5.6-luna` 사용), 멀티모달 이미지는 `image_url: {url: "data:{mime};base64,..."}`로 변환.

&#x20; Claude는 Messages API(`/v1/messages`)에 최신 GA 기능인 `output_config: {format: {type: "json_schema", schema}}`를

&#x20; 사용(과거 베타 `output_format` 파라미터도 계속 동작하지만 신규 방식 사용, `x-api-key`+`anthropic-version` 헤더 필요,

&#x20; `system` 프롬프트는 messages 배열이 아니라 최상위 파라미터로 분리, 이미지는 `{type:"image", source:{type:"base64",

&#x20; media_type, data}}`로 변환), 모델은 사용자가 명시적으로 요청한 `claude-sonnet-5`(Haiku 4.5 대신 — Claude Max

&#x20; 구독 토큰 여유가 있다는 이유로 지정, 단 API 자체는 Max 구독과 별개 결제이므로 실제로는 안 쓰기로 함, 코드에는

&#x20; 남겨둠). 이 조사 과정에서 최초 시도했던 필드명(OpenAI `max_completion_tokens` 추측, Claude 구버전 tool-use 방식)

&#x20; 대신 각 공급자 공식 문서에서 실제 curl 예제를 확인하고 정확한 필드명으로 구현함(Gemini 때와 같은 400 에러 재발 방지).

&#x20; \*\*중요\*\*: 기본값은 그대로 Gemini이며(사용자가 Claude API는 별도 결제 필요해서 "일단 Gemini 유지"를 선택함),

&#x20; OpenAI/Claude 경로는 코드상 구현은 완료됐지만 아직 실제 API 키로 테스트해보지는 않은 상태 — 나중에 누군가 다른

&#x20; 공급자를 실제로 켜서 쓸 때 첫 호출에서 문제가 발견되면(에러 메시지 형식 차이 등) 그때 다듬을 것.

\- \*\*일시적 API 오류(429/503 등) 자동 재시도 (2026-07 추가)\*\*: Gemini 3.5 사용 중 "high demand, please try

&#x20; again later" 같은 HTTP 503 오류가 반복 발생 → 서버 과부하 같은 일시적 오류는 사람이 매번 로그 보고 수동 확인

&#x20; 안 해도 되게 자동 재시도를 붙임. `file\_namer.RETRYABLE\_HTTP\_CODES = {429, 500, 502, 503, 504}`에 해당하는

&#x20; 오류만 재시도 대상(401/400처럼 설정 자체가 잘못된 경우는 재시도해도 똑같이 실패하므로 제외). `analyze\_file()`의

&#x20; 공급자 호출 부분을 `MAX_RETRIES`(2, 즉 최초 시도 포함 최대 3번) 루프로 감싸서, 재시도 대상 오류면

&#x20; `RETRY_DELAY_SECONDS`(4초) 대기 후 다시 시도하고 재시도 로그(`[AI 재시도]`)를 남김. 공급자 3종(Gemini/OpenAI/

&#x20; Claude) 모두 동일한 재시도 로직을 공유함(공급자별 호출 함수는 실패 시 그대로 예외를 던지기만 하므로 자연스럽게

&#x20; 재사용됨). 그 외(파싱 실패 등) 오류는 기존처럼 즉시 실패 처리.

\- \*\*Gemini 모델 버전 선택 기능 (2026-07 추가)\*\*: 재시도 로직을 붙여도 Gemini 3.5의 503 "high demand" 오류가

&#x20; 계속 잦아서, 모델 버전 자체를 사용자가 상황에 따라 바꿔 쓸 수 있게 함(기존엔 `file\_namer.GEMINI\_MODEL` 상수 하나로

&#x20; 고정). `GEMINI\_MODEL\_CHOICES = ("gemini-2.5-flash", "gemini-3.5-flash")`, `DEFAULT\_GEMINI\_MODEL =

&#x20; "gemini-2.5-flash"`로 정의하고, config에 `gemini\_model` 키를 추가함(기본값 2.5 — 사용자가 "3.5 계속 오류나서

&#x20; 못쓰겠다"며 2.5로 되돌려달라고 요청해서 기본값 자체를 2.5로 바꿈). `tray\_app.py` 설정 GUI에 "Gemini 모델" 드롭다운을

&#x20; 추가(Gemini API Key 입력칸 바로 아래, 2.5/3.5 옵션에 각각 안정성/종료예정일 설명을 라벨에 표기). `file\_namer.\_call\_gemini()`가

&#x20; 하드코딩된 상수 대신 `model` 매개변수를 받아 URL에 사용하도록 변경, `analyze\_file()`에서 `functools.partial(\_call\_gemini,

&#x20; model=gemini\_model)`로 config값을 주입. thinking 절감 파라미터는 버전별로 필드명이 달라서(3.x는 `thinkingLevel`,

&#x20; 2.5는 `thinkingBudget`) `model.startswith("gemini-3")`로 분기 처리함(3.5용 `thinkingLevel: "low"`를 2.5에 그대로

&#x20; 보내면 필드 자체가 없어 예상치 못한 오류가 날 수 있어서 사전에 분기해둠). 백그라운드 패턴 요약(`update\_pattern\_summary()`)도

&#x20; 동일하게 config의 `gemini\_model`을 읽도록 맞춤. \*\*중요 — 사용자에게 안내한 트레이드오프\*\*: `gemini-2.5-flash`는

&#x20; 2026-10-16 전체 종료 예정이고, 신규 발급 API 키는 이미 404로 막혀 있음(과거 이력 참고). 기존 키로는 그 날짜까지 계속

&#x20; 동작하지만, 종료일 이후에는 다시 3.5나 다른 공급자로 전환해야 함 — 재발 시 이 GEMINI\_MODEL\_CHOICES 드롭다운에서

&#x20; 3.5로 바꾸기만 하면 됨.

\- \*\*다중 폴더 감시 지원 (2026-07 추가)\*\*: `watch\_folder`(단일 문자열) → `watch\_folders`(리스트)로 전환.

&#x20; `file\_namer.load\_config()`가 구버전 `watch\_folder` 값을 자동으로 리스트에 마이그레이션함(하위호환).

&#x20; `rename\_watcher.WATCH\_FOLDERS`는 리스트, `start\_watcher()`가 한 Observer에 폴더별로 `schedule()`을 여러 번

&#x20; 호출하는 방식(감시 폴더마다 별도 Observer 안 만듦). 7일 정리(`run\_cleanup`)도 모든 감시 폴더에 대해 각각 실행됨.

&#x20; 트레이 → 환경설정 GUI에서 리스트박스 + "폴더 추가"/"선택 삭제" 버튼으로 여러 폴더 관리 가능(단일 입력창 제거됨).

\- \*\*소규모 배포 패키징 (2026-07 추가)\*\*: 표시 이름은 "AutoRename"으로 변경(코드 파일명 rename\_watcher.py 등은 그대로 유지,

&#x20; 트레이 타이틀/설정창 타이틀/설치파일명만 변경). `requirements.txt`, `build\_exe.bat`(PyInstaller onefile/windowed),

&#x20; `installer.iss`(Inno Setup, 관리자 권한 불필요, Windows 시작 자동실행 옵션, UTF-8 BOM 필요 — 한글 깨짐 주의),

&#x20; `app\_icon.ico`, `설치및이용가이드.docx`(Gemini API 키 발급법 포함) 배포용으로 준비됨. bat 파일은 반드시 영어로만

&#x20; 작성할 것(과거 한글 포함 시 한글 Windows cmd 코드페이지(CP949)와 충돌해서 깨짐 오류 발생한 이력 있음).

&#x20; 최초 실행 시 API 키 없으면 자동으로 설정창이 뜨도록 tray\_app.py main()에 추가됨. 자동 업데이트 기능은 필요성이

&#x20; 낮다고 판단되어 미구현(수동 재배포로 충분, 소규모 도구라 업데이트 빈도 낮음).



\## 설계 배경 (어떤 사용 패턴을 전제로 만들었나)



\- 정형 서류(계좌 통지서, 세금 관련 서식, 수출입 서류 등)를 자주 받는 사무 환경을 전제로 한다.

&#x20; 파일명이 `report.xls`, `print.png` 처럼 내용을 전혀 말해주지 않는 경우가 많아 자동 분류의 이득이 크다.

\- 하루 다운로드량 약 40개, 파일 종류 다양(엑셀/PDF/이미지 등 전부 포함).

\- Gemini 무료 티어(하루 약 250회, 분당 약 10회) 사용 중. 프로젝트 단위로 한도 적용됨 — 급하면 새 프로젝트로

&#x20; 키 재발급하면 한도 리셋되지만 어뷰징 소지 있어 상시 권장 방법은 아님, 비상용으로만 언급했음.

\- \*\*구독과 API 결제는 별개다 (2026-07 확인)\*\*: claude.ai / Claude Code 용 구독과 console.anthropic.com 의

&#x20; API 종량제 결제는 완전히 분리된 풀이다(구글 Gemini Pro 구독과 Gemini API 가 별개인 것과 같은 구조).

&#x20; API 를 쓰려면 별도로 결제 수단을 등록해야 한다. 그래서 기본값은 무료 티어가 있는 Gemini 로 두고,

&#x20; Claude/OpenAI 는 설정 GUI 에서 공급자 + API 키만 바꾸면 켜지도록 만들어만 뒀다.

\- 커스텀 지침(user\_rules) 예시: "카드 사용내역 엑셀이면 카드 끝번호 4자리를 제목에 포함",

&#x20; "거래 상대방이 있는 문서면 그 회사명을 site 자리에 강제로 붙임" 등 — 환경설정 GUI 의 텍스트 영역에 사람이 직접 입력한다.



\## 앞으로 이어서 할 만한 것 (제안했으나 아직 미적용)



\- 실행취소 이력의 영속화 (현재는 메모리에만 있어서 프로그램 재시작하면 취소 불가)

\- 분류 신뢰도(confidence) 필드 추가해서 애매한 rename 오판 방지

\- 트레이 메뉴에 "학습 로그/패턴 요약 보기" (사용자가 원치 않아 보류 중, 다시 원하면 추가)



