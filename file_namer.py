# -*- coding: utf-8 -*-
"""
file_namer.py (다중 AI 공급자 지원)
---------------
다운로드된 파일(엑셀/PDF/워드/이미지/기타)에 대해 AI가 분석을 수행합니다.
설정(ai_provider)에 따라 Gemini/OpenAI/Claude 중 하나를 골라 호출하며, 프롬프트와 응답 스키마,
그 이후 파일명 결정 로직은 공급자와 무관하게 동일하게 동작합니다.
API 제한 우회를 위해 수집 데이터와 토큰 소모량을 최소화하는 최적의 라이트 스펙으로 롤백되었습니다.
"""

import os
import re
import time
import json
import base64
import functools
import shutil
import tempfile
import mimetypes
import datetime
import urllib.request
import urllib.error
from urllib.parse import urlparse

# ====================== 설정 파일 경로 ======================
CONFIG_PATH = os.path.join(os.path.expanduser("~"), "rename_watcher_config.json")
LEARNING_LOG_PATH = os.path.join(os.path.expanduser("~"), "rename_watcher_learning_log.json")
FAILED_FILES_PATH = os.path.join(os.path.expanduser("~"), "rename_watcher_failed_files.json")
PATTERN_SUMMARY_PATH = os.path.join(os.path.expanduser("~"), "rename_watcher_pattern_summary.json")
LOG_FILE_PATH = os.path.join(os.path.expanduser("~"), "rename_watcher.log")

# ====================== AI 공급자별 모델/버전 상수 ======================
# Gemini는 모델별로 안정성/속도/무료한도가 자주 바뀌고(3.5는 최신이지만 수요 급증 시 503 "high demand" 오류가
# 잦고, 2.5는 안정적이나 2026-10-16 전체 종료 예정이라 신규 키는 이미 404), 사용자가 상황에 따라 직접 바꿔 쓸 수
# 있도록 config["gemini_model"]로 선택 가능하게 함(설정 GUI 드롭다운). DEFAULT_GEMINI_MODEL은 그 기본값이자
# config에 값이 없을 때(구버전 config 하위호환) 쓰이는 폴백.
GEMINI_MODEL_CHOICES = ("gemini-2.5-flash", "gemini-3.5-flash")
# 2026-09-08 기본값을 2.5 → 3.5 로 올림. 2.5는 2026-10-16 전체 종료 예정이고 신규 발급 키로는 이미 404가
# 떨어져서, 새로 설치하는 사람이 기본값 그대로 두면 아예 동작하지 않는 상태였다.
# 기존 사용자의 config에는 이미 gemini_model 값이 들어 있으므로 영향받지 않는다(신규 설치에만 적용).
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
OPENAI_MODEL = "gpt-5.6-luna"          # OpenAI 저비용/고속 모델 (2026-07 기준)
CLAUDE_MODEL = "claude-sonnet-5"        # 사용자 요청으로 Haiku 대신 Sonnet 5 사용
CLAUDE_API_VERSION = "2023-06-01"       # Anthropic Messages API 버전 헤더 (안정적으로 유지되는 값)
AI_PROVIDERS = ("gemini", "openai", "claude")

# 일시적 API 오류(과부하/한도 초과 등) 발생 시 자동 재시도 설정.
# 429(요청 한도 초과), 500/502/503/504(서버 측 일시적 문제)는 보통 몇 초 뒤 재시도하면 성공하므로
# 곧바로 실패 처리하지 않고 잠깐 대기 후 다시 시도한다. 그 외 오류(401/400 등 설정 문제)는 재시도해도
# 똑같이 실패할 뿐이므로 재시도 대상에서 제외.
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 2          # 최초 시도 포함 최대 3번 시도
RETRY_DELAY_SECONDS = 4  # 재시도 사이 대기 시간(초)

# ====================== 무료 키 소진 시 유료 키 자동 전환 (2026-09-08 추가) ======================
# 무료 티어는 "분당 N회"와 "하루 N회" 두 한도가 따로 걸리는데 둘 다 429로 온다. 성격이 달라서 대응도 다르다.
#   - 분당 한도: 잠깐 기다리면 풀린다 → 이번 파일만 유료 키로 넘기고, 다음 파일은 다시 무료 키부터 시도
#   - 일일 한도: 그날 자정까지 계속 막힌다 → 무료 키를 건너뛰고 바로 유료 키를 쓴다.
#     구분하지 않으면 파일마다 "어차피 실패할 429"를 재시도까지 포함해 세 번씩 더 쏘게 된다.
# 재시도 대기(4초 × 2회 = 8초)는 서버 일시 오류(503)엔 맞지만 분당 한도 회복(최대 60초)엔 턱없이 짧다.
# 그래서 429는 대기로 버티지 않고 곧바로 유료 키로 넘긴다 — 실패해서 수동 팝업이 뜨는 것보다 낫다.
QUOTA_STATE_PATH = os.path.join(os.path.expanduser("~"), "rename_watcher_quota_state.json")

# 429 응답 본문에서 "일일 한도"를 가려내는 표식. Gemini는 quotaId/quotaMetric에
# GenerateRequestsPerDayPerProjectPerModel 처럼 적어 보낸다. 공백·밑줄·하이픈을 지우고 소문자로
# 비교하므로 "per day", "Per-Day", "PerDay" 가 전부 같은 표식으로 잡힌다.
DAILY_QUOTA_MARKERS = ("perday", "dailylimit", "quotaexceededday")

# custom_filename을 쓰지 않는 일반 케이스의 새 파일명 조립 형식. {date}/{summary}/{site} 자리표시자 사용.
# 설정 GUI에서 사용자가 자유롭게 바꿀 수 있음 (필드 순서를 프롬프트가 아니라 여기서 강제하던 것을 사용자 설정으로 이관).
DEFAULT_FILENAME_FORMAT = "{date}_{summary}_{site}"


def build_filename_from_format(fmt: str, date: str, summary: str, site: str) -> str:
    """
    사용자 설정 filename_format으로 실제 파일명을 조립한다.
    date/summary/site 자리표시자는 중괄호를 써도({date}) 안 써도(date) 둘 다 인식한다.
    site가 비어있으면 site 자리표시자와 그 앞에 붙은 구분자(_, -, 공백)까지 함께 제거해서
    "20260710_요약_" 처럼 꼬리에 구분자만 남는 것을 방지한다.
    """
    fmt = (fmt or "").strip() or DEFAULT_FILENAME_FORMAT
    if not site:
        # '_'는 정규식 \b 기준으로 단어문자라 {date}_{summary}_{site}처럼 밑줄로 이어붙인 형식에서
        # \b가 안 먹으므로, 영문자 경계((?<![A-Za-z])...(?![A-Za-z]))로 직접 판정한다.
        fmt = re.sub(r"[-_\s]*\{?(?<![A-Za-z])site(?![A-Za-z])\}?", "", fmt, flags=re.IGNORECASE)

    values = {"date": date, "summary": summary, "site": site}

    def _sub(m: "re.Match") -> str:
        return values.get(m.group(1).lower(), m.group(0))

    result = re.sub(r"\{?(?<![A-Za-z])(date|summary|site)(?![A-Za-z])\}?", _sub, fmt, flags=re.IGNORECASE)
    return result.strip("_- ") or f"{date}_{summary}" + (f"_{site}" if site else "")


def _log_error(message: str):
    """
    콘솔이 없는 pythonw/exe 환경에서도 사용자가 '로그 보기'로 API 오류를 확인할 수 있도록,
    rename_watcher.py가 쓰는 것과 같은 로그 파일에 직접 기록한다.
    (순환 import를 피하기 위해 rename_watcher.log()를 호출하지 않고 파일에 직접 append)
    """
    try:
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass

# 수정 사례가 이 개수만큼 새로 쌓일 때마다 패턴 요약을 다시 생성
LEARNING_BATCH_SIZE = 10
# 패턴 요약 시 한 번에 참고하는 최근 사례 개수 (너무 오래된 건 제외)
LEARNING_RECENT_LIMIT = 50

# 분석 대상 확장자 그룹
EXCEL_EXTS = (".xlsx", ".xls", ".xlsm")
WORD_EXTS = (".docx", ".doc")
PDF_EXTS = (".pdf",)
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
TEXT_EXTS = (".txt", ".csv", ".md")


def load_config() -> dict:
    """설정 JSON 파일을 로드합니다. 파일이 없으면 기본값을 자동으로 생성합니다."""
    default_config = {
        "ai_provider": "gemini",    # "gemini" | "openai" | "claude". 사용자마다 원하는 공급자 선택 가능
        "gemini_model": DEFAULT_GEMINI_MODEL,  # "gemini-2.5-flash" | "gemini-3.5-flash". 503/404 상황에 맞춰 선택
        "gemini_api_key": "",
        "openai_api_key": "",
        "claude_api_key": "",
        # 무료 키가 한도(429)에 막혔을 때 대신 쓸 결제 연동 키. 비워두면 전환 없이 종전과 동일.
        "paid_api_key": "",
        # 유료 키를 쓸 공급자. 빈 문자열이면 위 ai_provider와 같은 공급자를 쓴다.
        "paid_provider": "",
        "watch_folders": [os.path.join(os.path.expanduser("~"), "Downloads")],
        "countdown_seconds": 3,
        "max_output_tokens": 99999,
        "max_preview_chars": 6000,   # 전신문처럼 핵심 필드가 뒤쪽에 있는 문서 대응
        "user_rules": "",           # 배포 기본값은 비워둠 (설정 GUI에 예시만 안내 문구로 표시됨)
        "move_rules": [],           # [{"keyword": "조건 설명(자연어)", "folder": "이동할 폴더 경로"}, ...]
        "filename_format": DEFAULT_FILENAME_FORMAT,  # custom_filename 미사용 시 기본 파일명 조립 형식
    }
    if not os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(default_config, f, ensure_ascii=False, indent=2)
            return default_config
        except Exception:
            return default_config
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
            # 구버전 단일 "watch_folder" 설정 -> 리스트로 마이그레이션
            if "watch_folders" not in config:
                legacy = config.get("watch_folder")
                config["watch_folders"] = [legacy] if legacy else list(default_config["watch_folders"])
            # 누락된 키가 있으면 보완
            for k, v in default_config.items():
                if k not in config:
                    config[k] = v
            return config
    except Exception:
        return default_config


def save_config(config: dict) -> bool:
    """설정을 파일에 저장합니다."""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def get_source_domain(filepath: str) -> str | None:
    """Windows Zone.Identifier(ADS)에서 다운로드 출처 도메인 추출."""
    ads_path = filepath + ":Zone.Identifier"
    try:
        with open(ads_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except (OSError, FileNotFoundError):
        return None

    match = re.search(r"HostUrl=(\S+)", content)
    if not match:
        match = re.search(r"ReferrerUrl=(\S+)", content)
    if not match:
        return None

    try:
        domain = urlparse(match.group(1).strip()).netloc
        return domain or None
    except Exception:
        return None


def _truncate(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit] + "\n...(중략/글자수제한 초과)"
    return text


def extract_excel_preview(filepath: str, limit: int) -> str:
    """엑셀 파일에서 대표 데이터만 빠르게 수집하도록 롤백합니다.
    구버전 바이너리 .xls는 openpyxl이 읽지 못하므로(InvalidFileException) xlrd로 별도 처리한다.
    (카드사 다운로드 내역이 여전히 .xls로 오는 경우가 많아, 이 분기가 없으면 AI가 셀 내용을 전혀
    못 보고 "(엑셀을 읽을 수 없음)"만 받아 커스텀 프롬프트의 셀 지정 규칙을 못 지키는 문제가 있었음.)"""
    if filepath.lower().endswith(".xls"):
        return _extract_excel_preview_xls(filepath, limit)
    import openpyxl
    try:
        wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    except Exception as e:
        return f"(엑셀을 읽을 수 없음: {e})"
    try:
        lines = []
        for sheet_name in wb.sheetnames[:3]:
            ws = wb[sheet_name]
            lines.append(f"[시트: {sheet_name}]")
            row_count = 0
            for row in ws.iter_rows(max_row=30, values_only=True):
                row_count += 1
                cells = row
                lines.append(" | ".join("" if c is None else str(c) for c in cells))
            if row_count == 0:
                lines.append("(빈 시트)")
        return _truncate("\n".join(lines), limit)
    finally:
        wb.close()  # 예외 발생 시에도 반드시 파일 핸들 해제


def _extract_excel_preview_xls(filepath: str, limit: int) -> str:
    """구버전 바이너리 .xls(엑셀 97-2003) 전용 미리보기. openpyxl 미지원이라 xlrd 사용."""
    import xlrd
    try:
        wb = xlrd.open_workbook(filepath)
    except Exception as e:
        return f"(엑셀(.xls)을 읽을 수 없음: {e})"
    lines = []
    for sheet_name in wb.sheet_names()[:3]:
        ws = wb.sheet_by_name(sheet_name)
        lines.append(f"[시트: {sheet_name}]")
        row_count = 0
        for r in range(min(30, ws.nrows)):
            row_count += 1
            cells = []
            for c in range(ws.ncols):
                ctype = ws.cell_type(r, c)
                value = ws.cell_value(r, c)
                if ctype == xlrd.XL_CELL_DATE:
                    try:
                        dt = xlrd.xldate_as_datetime(value, wb.datemode)
                        value = dt.strftime("%Y-%m-%d") if dt.time().hour == dt.time().minute == 0 else dt.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        pass
                elif ctype == xlrd.XL_CELL_NUMBER and float(value).is_integer():
                    value = int(value)
                cells.append("" if value == "" else str(value))
            lines.append(" | ".join(cells))
        if row_count == 0:
            lines.append("(빈 시트)")
    return _truncate("\n".join(lines), limit)


def extract_word_preview(filepath: str, limit: int) -> str:
    """워드 문서에서 핵심 문장 30개만 수집하도록 롤백합니다."""
    import docx
    try:
        doc = docx.Document(filepath)
    except Exception as e:
        return f"(워드 문서를 읽을 수 없음: {e})"
    paras = [p.text for p in doc.paragraphs if p.text.strip()]
    return _truncate("\n".join(paras[:30]), limit)


def extract_pdf_preview(filepath: str, limit: int) -> str:
    """PDF 파일에서 앞의 3페이지만 가볍게 분석하도록 롤백합니다."""
    import fitz  # PyMuPDF
    try:
        doc = fitz.open(filepath)
    except Exception as e:
        return f"(PDF를 읽을 수 없음: {e})"
    try:
        text_parts = []
        for page in doc[:3]:
            text_parts.append(page.get_text())
        text = "\n".join(text_parts).strip()
        if not text:
            return "(텍스트 추출 불가, 스캔본일 가능성)"
        # 실제 페이지 수를 같이 실어 보낸다. 안 보내면 모델이 본문의 "(1면)", "(2면)" 같은
        # 서식 표기를 세서 장수를 추측한다(국세 납부서 1페이지 → "2장"으로 나온 사례).
        return pdf_page_count_note(doc.page_count) + "\n\n" + _truncate(text, limit)
    finally:
        doc.close()


def pdf_page_count_note(page_count: int) -> str:
    return (
        f"[시스템이 PDF 파일에서 직접 읽은 실제 페이지 수: {page_count}]\n"
        "장수·총페이지수가 필요하면 반드시 이 숫자를 쓰고, 본문의 '(1면)', '(2면)', 페이지 번호 등을 세어 다시 계산하지 마."
    )


def get_pdf_page_count(filepath: str) -> int | None:
    """PDF 실제 페이지 수. 못 읽으면 None."""
    def _count(tmp_path):
        import fitz
        doc = fitz.open(tmp_path)
        try:
            return doc.page_count
        finally:
            doc.close()
    try:
        n = _read_via_copy(filepath, _count)
    except Exception:
        return None
    return n if isinstance(n, int) else None


PAGE_SUFFIX_RE = re.compile(r"_(\d+)장(?=(\.pdf)?$)", re.IGNORECASE)


def fix_pdf_page_suffix(name: str, page_count: int | None) -> str:
    """
    AI가 붙인 '_N장' 표기를 실제 페이지 수로 강제 보정한다(1페이지면 표기 제거).
    이름 끝에 이미 '_N장'이 있을 때만 건드린다 — 장수 규칙을 안 쓰는 사용자 이름엔 영향 없음.
    """
    if not name or not page_count:
        return name
    if page_count == 1:
        return PAGE_SUFFIX_RE.sub("", name)
    return PAGE_SUFFIX_RE.sub(f"_{page_count}장", name)


def extract_text_preview(filepath: str, limit: int) -> str:
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            return _truncate(f.read(), limit)
    except Exception as e:
        return f"(텍스트를 읽을 수 없음: {e})"


def get_image_base64(filepath: str) -> tuple[str, str] | None:
    """이미지를 base64로 인코딩합니다."""
    mime, _ = mimetypes.guess_type(filepath)
    if not mime:
        mime = "image/jpeg"
    try:
        with open(filepath, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
        return mime, data
    except Exception:
        return None


def _read_via_copy(filepath: str, func, *args):
    """
    원본 파일을 직접 열지 않고 임시 복사본을 만들어서 분석한다.
    openpyxl/PyMuPDF 등이 파일 핸들을 오래 물고 있어도 원본에는 영향 없도록 함
    (다운로드 폴더 파일이 Python에 의해 계속 잠기는 문제 방지).
    """
    tmp_dir = tempfile.mkdtemp(prefix="rw_")
    ext = os.path.splitext(filepath)[1]
    tmp_path = os.path.join(tmp_dir, "copy" + ext)
    try:
        shutil.copy2(filepath, tmp_path)
    except Exception as e:
        try:
            os.rmdir(tmp_dir)
        except Exception:
            pass
        return f"(파일 복사 실패: {e})"

    try:
        return func(tmp_path, *args)
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        try:
            os.rmdir(tmp_dir)
        except Exception:
            pass


def build_content_parts(filepath: str, ext: str, limit: int) -> tuple[list, str]:
    """확장자 타입에 따른 가벼운 데이터 파싱."""
    if ext in EXCEL_EXTS:
        preview = _read_via_copy(filepath, extract_excel_preview, limit)
        return [{"text": f"[엑셀 내용 미리보기]\n{preview}"}], "excel-text"

    if ext in WORD_EXTS:
        preview = _read_via_copy(filepath, extract_word_preview, limit)
        return [{"text": f"[워드 문서 내용 미리보기]\n{preview}"}], "word-text"

    if ext in PDF_EXTS:
        preview = _read_via_copy(filepath, extract_pdf_preview, limit)
        if "텍스트 추출 불가" in preview:
            def _render_first_page(tmp_path):
                import fitz
                doc = fitz.open(tmp_path)
                try:
                    page = doc[0]
                    pix = page.get_pixmap(dpi=150)
                    return pix.tobytes("png")
                finally:
                    doc.close()

            try:
                img_data = _read_via_copy(filepath, _render_first_page)
                if isinstance(img_data, bytes):
                    base64_data = base64.b64encode(img_data).decode("utf-8")
                    page_count = get_pdf_page_count(filepath)
                    note = ("\n" + pdf_page_count_note(page_count)) if page_count else ""
                    return [
                        {"text": "[PDF 텍스트가 추출되지 않아 첫 페이지를 렌더링한 이미지입니다. 이미지를 분석해 정답을 찾아주세요]" + note},
                        {"inline_data": {"mime_type": "image/png", "data": base64_data}},
                    ], "pdf-image"
            except Exception:
                pass
        return [{"text": f"[PDF 내용 미리보기]\n{preview}"}], "pdf-text"

    if ext in TEXT_EXTS:
        preview = extract_text_preview(filepath, limit)
        return [{"text": f"[텍스트 내용 미리보기]\n{preview}"}], "plain-text"

    if ext in IMAGE_EXTS:
        img = get_image_base64(filepath)
        if img:
            mime, data = img
            return [
                {"text": "[첨부된 이미지의 내용 전체를 완벽하게 파악하여 최선의 이름을 지어주세요]"},
                {"inline_data": {"mime_type": mime, "data": data}},
            ], "image"
        return [{"text": "(이미지를 읽을 수 없음)"}], "image-failed"

    return [{"text": "(이 파일 형식은 내용을 파싱할 수 없습니다. 파일명과 확장자, 도메인 정보를 바탕으로 최선의 유추를 시도하세요)"}], "unknown"


# ====================== 사용자 수정 학습 로그 ======================
# user_rules(환경설정 화면에 보이는 항목)와는 별개로 백그라운드에서만 동작.
# "애매함" 판단 후 사용자가 직접 입력/수정한 이름을 누적 기록하고,
# 일정량(LEARNING_BATCH_SIZE)마다 AI가 패턴을 요약해서 다음 호출의 프롬프트에 슬쩍 끼워 넣는다.

def _load_json_safe(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json_safe(path: str, data) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


# ====================== 무료/유료 키 전환 상태 ======================
# 오늘 어떤 (공급자, 모델) 조합의 무료 한도가 소진됐는지, 유료 키를 몇 번 썼는지를 파일에 남긴다.
# 파일에 남기는 이유는 프로그램을 껐다 켜도 "오늘은 이미 무료가 소진됐다"를 기억하기 위해서다.
# 날짜가 바뀌면 통째로 버려지므로 따로 지우는 코드가 필요 없다.
# ⚠️ 로컬 날짜 기준이라 Gemini 무료 한도의 실제 리셋 시각(미국 태평양시 자정)과는 어긋날 수 있다.
#    빨리 풀리면 유료 키를 조금 더 쓰게 되고, 늦게 풀리면 무료 키로 429를 한 번 더 맞고 넘어갈 뿐이라
#    어느 쪽도 동작이 깨지지는 않는다.

def _is_daily_quota_error(body_text: str) -> bool:
    """429 응답 본문이 '일일 한도 소진'인지(분당 한도가 아니라) 판정한다."""
    normalized = re.sub(r"[\s_\-]+", "", body_text or "").lower()
    return any(marker in normalized for marker in DAILY_QUOTA_MARKERS)


def _load_quota_state() -> dict:
    state = _load_json_safe(QUOTA_STATE_PATH, {})
    if not isinstance(state, dict):
        return {}
    if state.get("date") != datetime.date.today().isoformat():
        return {}  # 날짜가 바뀌면 자동 초기화
    return state


def is_free_quota_exhausted(quota_key: str) -> bool:
    return quota_key in (_load_quota_state().get("exhausted") or [])


def mark_free_quota_exhausted(quota_key: str) -> None:
    state = _load_quota_state()
    state["date"] = datetime.date.today().isoformat()
    exhausted = list(state.get("exhausted") or [])
    if quota_key not in exhausted:
        exhausted.append(quota_key)
    state["exhausted"] = exhausted
    _save_json_safe(QUOTA_STATE_PATH, state)


def count_paid_call(quota_key: str) -> int:
    """유료 키로 호출한 횟수를 오늘치로 누적하고 누적값을 돌려준다(과금 가시성용)."""
    state = _load_quota_state()
    state["date"] = datetime.date.today().isoformat()
    counts = dict(state.get("paid_calls") or {})
    counts[quota_key] = int(counts.get(quota_key, 0)) + 1
    state["paid_calls"] = counts
    _save_json_safe(QUOTA_STATE_PATH, state)
    return counts[quota_key]


def load_learning_log() -> list:
    return _load_json_safe(LEARNING_LOG_PATH, [])


def save_learning_log(entries: list) -> bool:
    return _save_json_safe(LEARNING_LOG_PATH, entries)


# ====================== AI 분석 실패 파일 기록 ======================
# analyze_file()이 실패(API 오류/파싱 실패/타임아웃 등)한 파일을 별도로 기록해두고,
# 트레이 메뉴의 "에러 파일 재시도"에서 이 목록을 다시 훑어 재분석을 시도할 수 있게 함.
# 같은 파일이 다시 실패하면 사유/시각만 갱신(중복 누적 안 함), 성공하면 목록에서 제거.
# 기록은 당일 한정 — 날짜가 지난 실패 기록은 로드 시점에 자동 삭제됨(재시도 대상도 당일 실패분만).

def load_failed_files() -> list:
    entries = _load_json_safe(FAILED_FILES_PATH, [])
    today = datetime.date.today().isoformat()
    fresh = [e for e in entries if str(e.get("timestamp", "")).startswith(today)]
    if len(fresh) != len(entries):
        _save_json_safe(FAILED_FILES_PATH, fresh)
    return fresh


def save_failed_files(entries: list) -> bool:
    return _save_json_safe(FAILED_FILES_PATH, entries)


def add_failed_file(filepath: str, reason: str = "") -> bool:
    entries = [e for e in load_failed_files() if e.get("filepath") != filepath]
    entries.append({
        "filepath": filepath,
        "reason": reason,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
    })
    return save_failed_files(entries)


def remove_failed_file(filepath: str) -> bool:
    entries = load_failed_files()
    new_entries = [e for e in entries if e.get("filepath") != filepath]
    if len(new_entries) == len(entries):
        return False
    return save_failed_files(new_entries)


def load_pattern_summary() -> str:
    data = _load_json_safe(PATTERN_SUMMARY_PATH, {})
    return data.get("summary", "")


def save_pattern_summary(summary: str, based_on_count: int) -> bool:
    return _save_json_safe(PATTERN_SUMMARY_PATH, {
        "summary": summary,
        "based_on_count": based_on_count,
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    })


def record_user_correction(filepath: str, ai_suggested: str | None,
                            user_final: str, domain: str, ext: str):
    """
    '애매함' 또는 수동 입력 케이스에서 사용자가 최종 확정한 이름을 학습 로그에 기록.
    AI 추천값과 사용자 최종값이 사실상 같으면(추천을 그대로 받아들인 경우) 기록하지 않음
    -> 정말로 '사람이 직접 다르게 판단한 사례'만 패턴 학습에 활용.
    """
    original_name = os.path.splitext(os.path.basename(filepath))[0]

    if ai_suggested:
        ai_norm = ai_suggested.strip().lower()
        user_norm = user_final.strip().lower()
        if ai_norm == user_norm:
            return  # AI 추천을 그대로 받아들인 케이스는 학습 가치가 적어 제외

    log = load_learning_log()
    log.append({
        "original_name": original_name,
        "ext": ext,
        "domain": domain,
        "ai_suggested": ai_suggested or "",
        "user_final": user_final,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
    })
    save_learning_log(log)

    # 직전 패턴 요약 이후 얼마나 쌓였는지 확인해서, 배치 크기에 도달하면 재요약 시도
    summary_meta = _load_json_safe(PATTERN_SUMMARY_PATH, {})
    based_on_count = summary_meta.get("based_on_count", 0)
    if len(log) - based_on_count >= LEARNING_BATCH_SIZE:
        # API 호출이 포함되어 1~2초 걸릴 수 있으므로, 사용자 흐름(팝업/토스트)을
        # 막지 않도록 별도 스레드에서 조용히 처리한다.
        import threading
        threading.Thread(
            target=lambda: _safe_update_pattern_summary(log),
            daemon=True,
        ).start()


def _safe_update_pattern_summary(log: list):
    try:
        update_pattern_summary(log)
    except Exception:
        pass  # 패턴 요약 실패해도 일반 이름변경 동작에는 영향 없도록 무시


def update_pattern_summary(log: list | None = None):
    """최근 수정 사례들을 모아 AI에게 패턴 요약을 요청하고 결과를 저장."""
    config = load_config()
    api_key = config.get("gemini_api_key", "").strip()
    if not api_key:
        return
    gemini_model = config.get("gemini_model", DEFAULT_GEMINI_MODEL) or DEFAULT_GEMINI_MODEL

    if log is None:
        log = load_learning_log()
    recent = log[-LEARNING_RECENT_LIMIT:]
    if not recent:
        return

    cases_text = "\n".join(
        f'- 원본:"{e["original_name"]}" (확장자:{e["ext"]}, 출처:{e["domain"]}) '
        f'AI추천:"{e["ai_suggested"]}" -> 사용자최종:"{e["user_final"]}"'
        for e in recent
    )

    prompt = (
        "아래는 파일 이름 자동 정리 프로그램에서, 사용자가 AI 추천 이름을 직접 수정한 사례들이야. "
        "이 사례들을 보고 사용자가 선호하는 이름 짓는 패턴이나 규칙을 한국어로 2~4문장 이내로 간결하게 요약해줘. "
        "특정 도메인/확장자에 대한 일관된 선호가 보이면 그것도 짚어줘. "
        "패턴이 뚜렷하지 않으면 무리해서 지어내지 말고 짧게 '뚜렷한 패턴 없음'이라고만 답해. "
        "다른 설명 없이 요약 문장만 출력해.\n\n"
        f"[수정 사례 목록]\n{cases_text}"
    )

    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 300},
    }).encode("utf-8")

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{gemini_model}:generateContent?key={api_key}"
    )
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        candidates = data.get("candidates", [])
        summary = candidates[0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return  # 실패해도 조용히 무시 (다음 배치에서 다시 시도됨)

    if summary:
        save_pattern_summary(summary, based_on_count=len(log))


SYSTEM_PROMPT_TEMPLATE = """\
너는 다운로드 폴더 파일 정리 도우미야. 사용자가 방금 다운로드한 파일 하나를 보고 판단해.

현재 파일명(확장자 제외): "{basename}"
확장자: "{ext}"
다운로드 출처 도메인: "{domain}"
오늘 날짜: {today}

다음 3가지 중 하나로 분류해:
1. "ok" - 기존 파일명만 봐도 어떤 파일인지 명확하게 알 수 있는 경우. (예: "2026_업무보고서.pdf", "이체확인증_국민은행.pdf") 굳이 바꿀 필요가 없다면 이 분류를 써.
2. "rename" - 파일명이 의미 없는 코드, 숫자, 임시명(예: "IMG_2381", "document(3)", "다운로드", "20260630_4910010136...", "VPWSSetup_RD (5)")이라서 무슨 파일인지 직관적으로 알 수 없는 경우. 웬만하면 이 분류를 선택해서 새 이름을 지어줘. 내용이 없거나 zip, exe 같은 압축/설치 파일이라도 파일명과 출처 도메인을 바탕으로 최대한 추정해서 새 이름을 지어내야 해.
3. "ambiguous" - 파일 내용이 완전히 깨져있거나, 너조차도 도저히 무슨 용도의 파일인지 1%도 감을 잡을 수 없는 극단적인 경우에만 제한적으로 사용해. (웬만하면 1번이나 2번을 선택해라).

[최우선 규칙: 사용자 정의 지침에 특정 케이스의 파일명 형식이 지정된 경우]
아래 어딘가에 [사용자 정의 추가 지침] 섹션이 있고, 그 안에 이 파일과 같은 종류/조건에 대해 구체적인 파일명 형식(순서, 접두사, 구분자 등)이
명시돼 있다면(예: 전신문/해외송금 확인서, 카드 사용내역 등 특정 케이스에 대한 형식 지정), 다른 모든 규칙(아래 summary/site 조합 방식)보다
이 지침을 최우선으로 따라야 해.
이 경우 그 지침대로 완성한 최종 파일명(확장자 제외, 날짜가 필요하면 오늘 날짜 {today} 사용)을 custom_filename 필드에 그대로 채워.
custom_filename을 채웠다면 summary/site는 참고용일 뿐 실제로는 무시되니 대충 채워도 무방해.
사용자 정의 지침에 해당 케이스에 대한 구체적 형식 지정이 없으면 custom_filename은 반드시 null로 둬.

새 이름을 지을 때(custom_filename을 쓰지 않는 일반적인 경우) 아래 두 필드를 채워. 이 값들을 최종 파일명으로 어떻게 조합할지(순서, 날짜 위치 등)는
시스템이 별도 설정으로 처리하니 너는 이 두 필드 내용만 정확히 채우면 돼.
- summary(내용요약): 한글 2~4단어 이내, 핵심 키워드 중심의 간결한 명사구 (예: 연말정산_안내서, 상품_카탈로그, 보안프로그램_설치파일)
- site(사이트명): 도메인을 보고 실제 회사/서비스명 추정 (예: kbstar.com -> KB국민은행, github.com -> 깃허브). 확실하지 않으면 도메인 일부 그대로. 도메인이 "출처불명"이면 site는 빈 문자열로.
- 파일명에 쓸 수 없는 특수문자(\\ / : * ? " < > |) 절대 사용 금지

[인간이 작성한 이상한 파일명 및 상황 예시]
1. 기존파일명: "20260630_142311", 확장자: ".png", 도메인: "m.kbstar.com", 내용: (은행 이체 완료 화면 이미지)
   -> 응답: {{"decision": "rename", "summary": "이체확인증", "site": "KB국민은행", "reason": "국민은행 이체 완료 캡처 화면임"}}

2. 기존파일명: "양식_최종_완성(3)", 확장자: ".docx", 도메인: "출처불명", 내용: "본 계약서는 갑과 을 사이의..."
   -> 응답: {{"decision": "rename", "summary": "표준_용역계약서", "site": "", "reason": "본문 시작 부분에서 계약서 양식임을 확인"}}

3. 기존파일명: "VPWSSetup_RD", 확장자: ".exe", 도메인: "viva.co.kr", 내용: (내용 없음)
   -> 응답: {{"decision": "rename", "summary": "보안프로그램_설치파일", "site": "비바리퍼블리카", "reason": "토스 운영사 도메인의 보안 설치 파일로 추정"}}

4. 기존파일명: "2026_하반기_기획서", 확장자: ".pdf", 도메인: "출처불명", 내용: "2026년도 하반기 전략 기획..."
   -> 응답: {{"decision": "ok", "summary": "", "site": "", "reason": "이미 직관적이고 완벽한 파일명임"}}

예시 끝.

응답은 지정된 JSON 스키마 필드만 채워. reason은 짧게 한 줄로. decision이 "ok"면 summary/site는 빈 문자열로 둬도 돼.
matched_rule_index는 아래 [사용자 정의 이동 규칙 목록]이 있을 때만 사용하는 필드이며, 해당하는 규칙이 없으면 null로 응답해.
custom_filename은 위 [최우선 규칙]에서 설명한 대로, 사용자 정의 지침이 이 파일의 케이스에 대해 명시적 파일명 형식을 지정한 경우에만 채우고,
그 외에는 반드시 null로 응답해.
"""

# 구조화된 출력(responseMimeType + responseSchema) 강제용 JSON 스키마.
# 스키마를 강제하면 모델 출력이 항상 문법적으로 유효한 JSON이 되어 파싱 실패(따옴표/줄바꿈 이스케이프 문제 등)를 원천 차단하고,
# "오직 JSON으로만 응답해" 류의 장황한 지시문을 줄여 프롬프트 토큰도 절약할 수 있다.
# 주의: Gemini REST API의 Schema는 JSON Schema가 아니라 OpenAPI 3.0 서브셋이라
# type에 배열([ "string", "null" ])을 못 쓰고, null 허용은 반드시 "nullable": true로 표현해야 함.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["ok", "rename", "ambiguous"]},
        "summary": {"type": "string"},
        "site": {"type": "string"},
        "reason": {"type": "string"},
        "matched_rule_index": {"type": "integer", "nullable": True},
        "custom_filename": {"type": "string", "nullable": True},
    },
    "required": [
        "decision", "summary", "site", "reason", "matched_rule_index", "custom_filename",
    ],
}

# OpenAI(response_format.json_schema)와 Claude(output_config.format)용 표준 JSON Schema.
# 둘 다 Gemini와 달리 진짜 JSON Schema를 쓰므로 null 허용을 "type": [..., "null"]로 표현하고
# additionalProperties: false를 명시해야 한다(Gemini의 OpenAPI 서브셋과 다른 부분).
RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["ok", "rename", "ambiguous"]},
        "summary": {"type": "string"},
        "site": {"type": "string"},
        "reason": {"type": "string"},
        "matched_rule_index": {"type": ["integer", "null"]},
        "custom_filename": {"type": ["string", "null"]},
    },
    "required": [
        "decision", "summary", "site", "reason", "matched_rule_index", "custom_filename",
    ],
    "additionalProperties": False,
}


def _parts_to_openai_content(parts: list) -> list:
    """Gemini 스타일 parts({"text":...} / {"inline_data":{"mime_type","data"}})를
    OpenAI chat/completions의 멀티모달 content 배열 형식으로 변환한다."""
    content = []
    for p in parts:
        if "text" in p:
            content.append({"type": "text", "text": p["text"]})
        elif "inline_data" in p:
            mime = p["inline_data"]["mime_type"]
            data = p["inline_data"]["data"]
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
    return content


def _parts_to_claude_content(parts: list) -> list:
    """Gemini 스타일 parts를 Claude Messages API의 content 배열 형식으로 변환한다."""
    content = []
    for p in parts:
        if "text" in p:
            content.append({"type": "text", "text": p["text"]})
        elif "inline_data" in p:
            mime = p["inline_data"]["mime_type"]
            data = p["inline_data"]["data"]
            content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
    return content


def _call_gemini(system_prompt: str, parts: list, api_key: str, max_output_tokens: int,
                  model: str = DEFAULT_GEMINI_MODEL) -> tuple[dict, dict]:
    """Gemini generateContent 호출. 성공 시 (파싱된 dict, usage dict) 반환.
    실패 시 urllib.error.HTTPError(네트워크/API 오류) 또는 ValueError(파싱 실패)를 그대로 던진다.
    model: config["gemini_model"]로 사용자가 선택한 버전(예: gemini-2.5-flash / gemini-3.5-flash).
    thinking 절감 파라미터는 버전에 따라 필드명이 달라서(3.x는 thinkingLevel, 2.5는 thinkingBudget) 분기함."""
    if model.startswith("gemini-3"):
        thinking_config = {"thinkingLevel": "low"}
    else:
        thinking_config = {"thinkingBudget": 0}  # 2.5 계열: thinking 비활성화로 토큰/속도 절감
    body = json.dumps({
        "contents": [{"role": "user", "parts": parts}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "thinkingConfig": thinking_config,
        },
    }).encode("utf-8")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    raw_text = ""
    try:
        candidates = data.get("candidates", [])
        text = candidates[0]["content"]["parts"][0]["text"].strip()
        raw_text = text
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            text = match.group(0)
        parsed = json.loads(text)
    except Exception as e:
        finish_reason = ""
        try:
            finish_reason = data.get("candidates", [{}])[0].get("finishReason", "")
        except Exception:
            pass
        detail = f" (finishReason={finish_reason})" if finish_reason else ""
        snippet = raw_text[:800].replace("\n", " ⏎ ")
        raise ValueError(f"AI 응답 파싱 실패 - {e}{detail} | 원본 응답: {snippet}") from e

    usage = data.get("usageMetadata", {})
    return parsed, usage


def _call_openai(system_prompt: str, parts: list, api_key: str, max_output_tokens: int) -> tuple[dict, dict]:
    """OpenAI chat/completions 호출 (response_format.json_schema로 구조화 출력 강제)."""
    content = _parts_to_openai_content(parts)
    body = json.dumps({
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "file_analysis",
                "schema": RESPONSE_JSON_SCHEMA,
                "strict": True,
            },
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    text = data["choices"][0]["message"]["content"].strip()
    try:
        parsed = json.loads(text)
    except Exception as e:
        raise ValueError(f"AI 응답 파싱 실패 - {e} | 원본 응답: {text[:800]}") from e

    usage_raw = data.get("usage", {})
    usage = {
        "promptTokenCount": usage_raw.get("prompt_tokens", 0),
        "candidatesTokenCount": usage_raw.get("completion_tokens", 0),
        "totalTokenCount": usage_raw.get("total_tokens", 0),
    }
    return parsed, usage


def _call_claude(system_prompt: str, parts: list, api_key: str, max_output_tokens: int) -> tuple[dict, dict]:
    """Claude Messages API 호출 (output_config.format으로 구조화 출력 강제)."""
    content = _parts_to_claude_content(parts)
    body = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": max(256, min(int(max_output_tokens), 8192)),
        "system": system_prompt,
        "messages": [{"role": "user", "content": content}],
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": RESPONSE_JSON_SCHEMA,
            }
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": CLAUDE_API_VERSION,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    text = data["content"][0]["text"].strip()
    try:
        parsed = json.loads(text)
    except Exception as e:
        raise ValueError(f"AI 응답 파싱 실패 - {e} | 원본 응답: {text[:800]}") from e

    usage_raw = data.get("usage", {})
    usage = {
        "promptTokenCount": usage_raw.get("input_tokens", 0),
        "candidatesTokenCount": usage_raw.get("output_tokens", 0),
        "totalTokenCount": usage_raw.get("input_tokens", 0) + usage_raw.get("output_tokens", 0),
    }
    return parsed, usage


def _call_with_retries(call_fn, system_prompt: str, parts: list, api_key: str,
                       max_output_tokens: int, fullname: str, provider: str,
                       key_label: str) -> tuple[str, dict | None, dict | None]:
    """
    키 하나로 최대 MAX_RETRIES + 1 번까지 시도한다.

    반환: (status, parsed, usage)
      status = "ok"           성공
             | "quota_day"    429이고 일일 한도 소진 → 오늘은 이 키를 더 쓸 수 없다
             | "quota_minute" 429이고 분당 한도 → 잠시 뒤엔 다시 쓸 수 있다
             | "fail"         그 외 실패(401/400 등). 키를 바꿔도 같은 결과일 가능성이 높다
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            parsed, usage = call_fn(system_prompt, parts, api_key, max_output_tokens)
            return "ok", parsed, usage
        except urllib.error.HTTPError as e:
            try:
                body_text = e.read().decode("utf-8", errors="ignore")[:500]
            except Exception:
                body_text = "(응답 본문 읽기 실패)"

            # 429는 대기로 버티지 않고 곧바로 빠져나가 상위에서 유료 키로 넘긴다.
            # (4초 대기로는 분당 한도가 회복되지 않으므로 재시도해봐야 같은 429만 맞는다)
            if e.code in RETRYABLE_HTTP_CODES and e.code != 429 and attempt < MAX_RETRIES:
                _log_error(
                    f"[AI 재시도] {fullname}: {provider}({key_label} 키) HTTP {e.code} 일시적 오류 - "
                    f"{RETRY_DELAY_SECONDS}초 후 재시도 ({attempt + 1}/{MAX_RETRIES}) / {body_text}"
                )
                time.sleep(RETRY_DELAY_SECONDS)
                continue

            if e.code == 429:
                status = "quota_day" if _is_daily_quota_error(body_text) else "quota_minute"
            else:
                status = "fail"

            print(f"[API 호출 실패] HTTP {e.code} {e.reason} / {body_text}")
            _log_error(
                f"[AI 분석 오류] {fullname}: {provider}({key_label} 키) API 호출 실패 - "
                f"HTTP {e.code} {e.reason} / {body_text}"
            )
            return status, None, None
        except Exception as e:
            print(f"[API 호출 실패] {e}")
            _log_error(f"[AI 분석 오류] {fullname}: {provider}({key_label} 키) API 호출 실패 - {e}")
            return "fail", None, None

    return "fail", None, None


def _build_call_fn(provider: str, config: dict):
    """
    공급자에 맞는 호출 함수와 한도 집계용 키를 만든다.
    Gemini는 한도가 모델별로 따로 걸리므로 집계 키에 모델명까지 넣는다.
    """
    if provider == "gemini":
        gemini_model = config.get("gemini_model", DEFAULT_GEMINI_MODEL) or DEFAULT_GEMINI_MODEL
        return functools.partial(_call_gemini, model=gemini_model), f"{provider}:{gemini_model}"
    return {"openai": _call_openai, "claude": _call_claude}[provider], provider


def analyze_file(filepath: str) -> dict | None:
    """설정 파일에서 매번 동적으로 키와 추가 지침을 로드하여 가벼운 한도로 분석을 수행합니다."""
    config = load_config()
    provider = (config.get("ai_provider") or "gemini").strip().lower()
    if provider not in AI_PROVIDERS:
        provider = "gemini"
    api_key = {
        "gemini": config.get("gemini_api_key", ""),
        "openai": config.get("openai_api_key", ""),
        "claude": config.get("claude_api_key", ""),
    }[provider].strip()

    # 무료 키가 한도에 막혔을 때 대신 쓸 유료(결제 연동) 키. 비워두면 전환 없이 종전과 동일하게 동작한다.
    paid_api_key = (config.get("paid_api_key") or "").strip()

    # 유료 쪽 공급자는 따로 고를 수 있다. 비었거나 값이 이상하면 무료와 같은 공급자를 쓴다
    # (설정 GUI의 "무료와 동일" 선택지가 빈 문자열로 저장된다).
    paid_provider = (config.get("paid_provider") or "").strip().lower()
    if paid_provider not in AI_PROVIDERS:
        paid_provider = provider

    max_output_tokens = int(config.get("max_output_tokens", 99999))

    # 3만 자에서 다시 가벼운 2천 자 한도로 강제 제한 조정하여 429 에러 방지
    max_preview_chars = int(config.get("max_preview_chars", 6000))
    user_rules = config.get("user_rules", "").strip()
    move_rules = config.get("move_rules", []) or []
    filename_format = config.get("filename_format", "").strip() or DEFAULT_FILENAME_FORMAT

    # 유료 키만 넣어둔 경우도 정상 동작해야 하므로 둘 다 비었을 때만 막는다.
    if not api_key and not paid_api_key:
        print(f"[경고] {provider} API 키가 공백이거나 로드되지 않았습니다.")
        _log_error(f"[AI 분석 오류] {os.path.basename(filepath)}: {provider} API 키가 설정되지 않았습니다.")
        return None

    dirpath, fullname = os.path.split(filepath)
    basename, ext = os.path.splitext(fullname)
    ext = ext.lower()
    today = datetime.date.today().strftime("%Y%m%d")
    domain = get_source_domain(filepath) or "출처불명"

    # 가벼운 2000자 한도로 텍스트 미리보기 데이터 파싱
    parts, _debug = build_content_parts(filepath, ext, max_preview_chars)

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        basename=basename, ext=ext, domain=domain, today=today
    )

    # 실시간 사용자 지정 추가 지침 합성
    if user_rules:
        system_prompt += f"\n\n[사용자 정의 추가 지침 (최우선 강제사항)]\n{user_rules}\n\n너는 본 추가 지침을 다른 모든 기존 룰보다 최우선하여 최종 결정을 내려야 한다."

    # 사용자 정의 이동 규칙 목록 (설정 GUI에서 등록한 조건 -> 폴더 매핑)
    if move_rules:
        rules_text = "\n".join(
            f'{i}: "{r.get("keyword", "").strip()}"'
            for i, r in enumerate(move_rules)
            if r.get("keyword", "").strip()
        )
        if rules_text:
            system_prompt += (
                "\n\n[사용자 정의 이동 규칙 목록 (조건: 인덱스)]\n"
                f"{rules_text}\n"
                "이 파일이 위 조건 중 하나에 해당한다고 판단되면, 그 조건의 인덱스 번호를 "
                "matched_rule_index 필드에 정수로 응답해. 해당하는 조건이 없거나 확신이 서지 않으면 "
                "matched_rule_index는 반드시 null로 응답해. 두 개 이상 해당해도 가장 명확히 맞는 것 하나의 인덱스만 골라."
            )

    # 과거 사용자 수정 이력에서 추출된 패턴 요약 (참고용, 환경설정 화면에는 노출 안 됨)
    pattern_summary = load_pattern_summary()
    if pattern_summary and "뚜렷한 패턴 없음" not in pattern_summary:
        system_prompt += (
            f"\n\n[참고: 과거 사용자가 직접 수정했던 이름들에서 관찰된 경향]\n{pattern_summary}\n"
            "이 경향은 참고만 하고, 위의 사용자 정의 추가 지침이나 명백한 파일 내용과 충돌하면 그쪽을 우선해."
        )

    call_fn, quota_key = _build_call_fn(provider, config)
    paid_call_fn, paid_quota_key = _build_call_fn(paid_provider, config)

    # 무료 키 → 유료 키 순서로 시도할 계획을 세운다. 각 단계는 자기 공급자의 호출 함수를 갖는다
    # (유료 공급자를 따로 고를 수 있으므로 호출 함수가 서로 다를 수 있다).
    # 오늘 이미 무료 일일 한도가 소진된 게 확인됐고 유료 키가 있으면 무료 키는 아예 건너뛴다.
    key_plan = []
    if api_key and not (paid_api_key and is_free_quota_exhausted(quota_key)):
        key_plan.append(("무료", api_key, provider, call_fn))
    if paid_api_key:
        key_plan.append(("유료", paid_api_key, paid_provider, paid_call_fn))

    parsed = usage = None
    for key_label, key_value, key_provider, key_call_fn in key_plan:
        status, parsed, usage = _call_with_retries(
            key_call_fn, system_prompt, parts, key_value, max_output_tokens,
            fullname, key_provider, key_label,
        )
        if status == "ok":
            if key_label == "유료":
                total = count_paid_call(paid_quota_key)
                _log_error(f"[유료 키 사용] {fullname}: {paid_quota_key} - 오늘 유료 호출 누적 {total}건")
            break

        # 한도(429)로 막힌 경우에만 다음 키로 넘어간다. 401/400 같은 설정 오류는
        # 키를 바꿔도 성격이 다르므로 유료 키를 괜히 소모하지 않고 여기서 끝낸다.
        if key_label == "무료" and status in ("quota_day", "quota_minute"):
            if status == "quota_day":
                mark_free_quota_exhausted(quota_key)
                _log_error(
                    f"[무료 한도 소진] {quota_key}: 오늘 남은 건은 유료 키로 처리합니다. "
                    f"(내일 자동으로 무료 키부터 다시 시도)"
                )
            if len(key_plan) > 1:
                _log_error(f"[유료 키로 전환] {fullname}: 무료 키가 429로 막혀 유료 키로 재시도합니다.")
            continue

        return None

    if parsed is None:
        # 계획된 키를 다 써도 실패했거나(무료만 있고 429), 애초에 쓸 키가 없던 경우
        return None

    decision = parsed.get("decision", "ambiguous")
    reason = parsed.get("reason", "")

    def clean(s: str) -> str:
        for ch in '<>:"/\\|?*':
            s = s.replace(ch, "")
        return s.strip()

    # 최우선: 사용자 정의 지침이 이 파일의 케이스에 명시적 파일명 형식을 지정한 경우, 그 완성된 이름을
    # 기본 조합 형식보다 우선해서 그대로 사용한다.
    suggested_name = None
    custom_filename = clean(parsed.get("custom_filename") or "")
    if custom_filename and decision in ("rename", "ambiguous"):
        suggested_name = custom_filename

    if suggested_name is None and decision in ("rename", "ambiguous"):
        summary = clean(parsed.get("summary", ""))
        site = clean(parsed.get("site", ""))
        if summary:
            suggested_name = build_filename_from_format(filename_format, today, summary, site)

    # 장수는 모델 판단에 맡기지 않고 실제 PDF 페이지 수로 마지막에 덮는다.
    if suggested_name and ext in PDF_EXTS:
        suggested_name = fix_pdf_page_suffix(suggested_name, get_pdf_page_count(filepath))

    # 사용자 정의 이동 규칙 매칭 결과 (없으면 None)
    raw_idx = parsed.get("matched_rule_index")
    matched_rule_index = None
    if isinstance(raw_idx, bool):
        matched_rule_index = None
    elif isinstance(raw_idx, int):
        matched_rule_index = raw_idx
    elif isinstance(raw_idx, str) and raw_idx.strip().lstrip("-").isdigit():
        matched_rule_index = int(raw_idx.strip())

    return {
        "decision": decision,
        "suggested_name": suggested_name,
        "reason": reason,
        "matched_rule_index": matched_rule_index,
        "usage": usage,  # 감시자 로깅을 위해 토큰 데이터 리턴 (공급자별 usage를 Gemini 필드명으로 정규화함)
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("사용법: python file_namer.py <파일경로>")
        sys.exit(1)
    result = analyze_file(sys.argv[1])
    print(json.dumps(result, ensure_ascii=False, indent=2))
