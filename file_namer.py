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
import unicodedata
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
# 잦고, 2.5는 신규 키에 404 — 기존 사용자만 쓸 수 있다. 공식 종료일은 미발표(2026-10-01 기준)), 사용자가 상황에 따라 직접 바꿔 쓸 수
# 있도록 config["gemini_model"]로 선택 가능하게 함(설정 GUI 드롭다운). DEFAULT_GEMINI_MODEL은 그 기본값이자
# config에 값이 없을 때(구버전 config 하위호환) 쓰이는 폴백.
GEMINI_MODEL_CHOICES = (
    "gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash",
    "gemini-3.5-flash-lite", "gemini-2.5-flash",
)
# 2026-09-08 기본값을 2.5 → 3.5 로 올림. 2.5는 신규 발급 키로는 이미 404가
# 떨어져서, 새로 설치하는 사람이 기본값 그대로 두면 아예 동작하지 않는 상태였다.
# 기존 사용자의 config에는 이미 gemini_model 값이 들어 있으므로 영향받지 않는다(신규 설치에만 적용).
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
OPENAI_MODEL = "gpt-5.6-luna"          # OpenAI 저비용/고속 모델 (2026-07 기준)
# Claude 모델도 config["claude_model"]로 고른다(설정 GUI 드롭다운, 2026-10-14 사용자 요청).
# 기본값은 종전 고정값(Sonnet 5) 그대로 — 기존 config엔 키가 없으므로 이 값으로 폴백된다.
CLAUDE_MODEL_CHOICES = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-sonnet-5", "claude-opus-5-5")
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5"
CLAUDE_MODEL = DEFAULT_CLAUDE_MODEL     # 하위호환용 별칭
CLAUDE_API_VERSION = "2023-06-01"       # Anthropic Messages API 버전 헤더 (안정적으로 유지되는 값)
AI_PROVIDERS = ("gemini", "openai", "claude")

# 일시적 API 오류(과부하/한도 초과 등) 발생 시 자동 재시도 설정.
# 429(요청 한도 초과), 500/502/503/504(서버 측 일시적 문제)는 보통 몇 초 뒤 재시도하면 성공하므로
# 곧바로 실패 처리하지 않고 잠깐 대기 후 다시 시도한다. 그 외 오류(401/400 등 설정 문제)는 재시도해도
# 똑같이 실패할 뿐이므로 재시도 대상에서 제외.
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 2          # 최초 시도 포함 최대 3번 시도
RETRY_DELAY_SECONDS = 4  # 재시도 사이 대기 시간(초)
# 요청 하나의 최대 대기. 종전 300초 — 과부하(503) 때 응답이 늦게 와서 파일 하나에 287초가 걸렸고(2026-10-06 실측),
# 큐가 직렬이라 그동안 다른 파일도 전부 멈췄다. 정상 응답은 2~12초.
REQUEST_TIMEOUT_SECONDS = 60
# 한 모델에서 재시도에 쓸 총 시간. 넘으면 재시도하지 않고 다음 모델로 넘긴다.
RETRY_BUDGET_SECONDS = 75

# 무료 키의 Gemini 한도는 **모델별**이다(2026-10-06 실측: 3.5·3.6 Flash 각각 하루 20회, 분당 5회).
# 그래서 고른 모델이 한도(429)·과부하(503)에 막히면 아래 순서로 다음 모델을 무료로 시도한다 → 하루 총량이 모델 수만큼 는다.
# 품질 순서: Flash 계열 먼저, Lite 는 마지막(실측에서 거래처명을 바꿔 쓰는 경향 — IBK기업은행→중소기업은행).
GEMINI_FALLBACK_ORDER = (
    "gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash",
    "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
)

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


# 파일명 형식에 쓸 수 있는 값(2.0.0~ time·original 추가)과 날짜 형식. 설정 GUI의 값 버튼·날짜 드롭다운과 짝이다.
FILENAME_TOKENS = ("date", "time", "summary", "site", "original")
DATE_FORMATS = {           # config["date_format"] → strftime
    "YYYYMMDD": "%Y%m%d",
    "YYMMDD": "%y%m%d",
    "YYYY-MM-DD": "%Y-%m-%d",
    "YYYY.MM.DD": "%Y.%m.%d",
    "YY.MM.DD": "%y.%m.%d",
    "MMDD": "%m%d",
}
DEFAULT_DATE_FORMAT = "YYYYMMDD"
_TOKEN_RE = re.compile(r"\{?(?<![A-Za-z])(" + "|".join(FILENAME_TOKENS) + r")(?![A-Za-z])\}?", re.IGNORECASE)


def format_date(yyyymmdd: str, date_format: str | None) -> str:
    """'20261014' 를 고른 날짜 형식으로. 형식을 모르거나 날짜를 못 읽으면 원래 값 그대로."""
    try:
        d = datetime.datetime.strptime(yyyymmdd, "%Y%m%d")
        return d.strftime(DATE_FORMATS.get(date_format or DEFAULT_DATE_FORMAT, "%Y%m%d"))
    except Exception:
        return yyyymmdd


def build_filename_from_format(fmt: str, date: str, summary: str, site: str,
                               time: str = "", original: str = "", date_format: str | None = None) -> str:
    """
    사용자 설정 filename_format으로 실제 파일명을 조립한다.
    자리표시자는 중괄호를 써도({date}) 안 써도(date) 둘 다 인식한다.
    값이 빈 자리표시자(site 가 없을 때 등)는 그 앞 구분자(_, -, 공백)까지 함께 지워서
    "20260710_요약_" 처럼 꼬리에 구분자만 남는 것을 막는다(맨 앞이면 뒤 구분자를 지운다).
    """
    fmt = (fmt or "").strip() or DEFAULT_FILENAME_FORMAT
    values = {
        "date": format_date(date, date_format),
        "time": time, "summary": summary, "site": site, "original": original,
    }
    for name, val in values.items():
        if not val:
            # '_'는 정규식 \b 기준으로 단어문자라 밑줄로 이어붙인 형식에서 \b가 안 먹으므로 영문자 경계로 직접 판정한다.
            pat = r"\{?(?<![A-Za-z])" + name + r"(?![A-Za-z])\}?"
            fmt = re.sub(r"[-_\s]*" + pat, "", fmt, flags=re.IGNORECASE)
            fmt = re.sub(r"^" + pat + r"[-_\s]*", "", fmt, flags=re.IGNORECASE)

    def _sub(m: "re.Match") -> str:
        return values.get(m.group(1).lower(), m.group(0))

    result = _TOKEN_RE.sub(_sub, fmt)
    return result.strip("_- ") or f"{values['date']}_{summary}" + (f"_{site}" if site else "")


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
        "claude_model": DEFAULT_CLAUDE_MODEL,
        # 무료 키가 한도(429)에 막혔을 때 대신 쓸 결제 연동 키. 비워두면 전환 없이 종전과 동일.
        "paid_api_key": "",
        # 유료 키를 쓸 공급자. 빈 문자열이면 위 ai_provider와 같은 공급자를 쓴다.
        "paid_provider": "",
        # 2.0.0~ AI 순위 목록. 비어 있으면 위 구버전 칸에서 만들어 쓴다(get_ai_chain 참고).
        "ai_chain": [],
        "watch_folders": [os.path.join(os.path.expanduser("~"), "Downloads")],
        "countdown_seconds": 3,
        "max_output_tokens": 99999,
        "max_preview_chars": 6000,   # 전신문처럼 핵심 필드가 뒤쪽에 있는 문서 대응
        "user_rules": "",           # 배포 기본값은 비워둠 (설정 GUI에 예시만 안내 문구로 표시됨)
        "move_rules": [],           # [{"keyword": "조건 설명(자연어)", "folder": "이동할 폴더 경로"}, ...]
        "filename_format": DEFAULT_FILENAME_FORMAT,  # custom_filename 미사용 시 기본 파일명 조립 형식
        "date_format": DEFAULT_DATE_FORMAT,          # 2.0.0~ {date} 의 모양 (DATE_FORMATS 키)
        # 2.0.0~ 이름 짓는 지침을 칸별로. [{"when": "어떤 파일일 때", "how": "이름은 이렇게"}, ...]
        # 비어 있으면 구버전 user_rules(통짜 글)를 그대로 쓴다(compose_user_rules 참고).
        "naming_rules": [],
        "common_rules": "",          # 2.0.0~ 모든 파일에 항상 같이 적용하는 지침
        "appearance_mode": "system", # 2.0.1~ 설정 창 화면 모드: system / light / dark
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


# ── AI 순위(ai_chain) ─────────────────────────────────────────────────────────
# 2.0.0 부터 「무료 키 → 유료 키」 대신 「1·2·3순위」 목록으로 키를 쓴다(2026-10-14 사용자 제안).
# 무료/유료는 이름뿐이고 실제로는 순서였기 때문 — 무료 칸에 유료 키를 넣으면 그냥 유료로 돈다.
# config["ai_chain"] = [{"provider": "claude", "api_key": "...", "model": "claude-haiku-5-5"}, ...]
# 비어 있으면 구버전 칸(ai_provider·*_api_key·paid_*)에서 만들어 쓴다(자동 마이그레이션).
AI_CHAIN_MAX = 3


def default_model_for(provider: str) -> str:
    return {"gemini": DEFAULT_GEMINI_MODEL, "claude": DEFAULT_CLAUDE_MODEL}.get(provider, OPENAI_MODEL)


def guess_provider(api_key: str) -> str | None:
    """키 앞부분으로 공급자를 짐작한다. 모르면 None. (실측: 유료 공급자=openai 칸에 Claude 키가 들어가 있던 설정)"""
    k = (api_key or "").strip()
    if k.startswith("sk-ant-"):
        return "claude"
    if k.startswith(("AIza", "AQ.")):
        return "gemini"
    if k.startswith("sk-"):
        return "openai"
    return None


def get_ai_chain(config: dict) -> list:
    """설정에서 AI 순위 목록을 꺼낸다. 키가 빈 항목·완전히 같은 항목은 빼고 최대 AI_CHAIN_MAX 개."""
    raw = config.get("ai_chain")
    entries = []
    if isinstance(raw, list) and raw:
        for e in raw:
            if isinstance(e, dict):
                entries.append((e.get("provider"), e.get("api_key"), e.get("model")))
    else:
        provider = (config.get("ai_provider") or "gemini").strip().lower()
        model_of = lambda p: config.get(f"{p}_model") if p in ("gemini", "claude") else None
        entries.append((provider, config.get(f"{provider}_api_key"), model_of(provider)))
        paid_key = config.get("paid_api_key")
        paid_provider = (config.get("paid_provider") or "").strip().lower() or provider
        # 구버전은 유료 공급자를 키와 따로 골라서, 키와 공급자가 어긋난 설정이 실제로 있었다 → 키 모양을 믿는다
        paid_provider = guess_provider(paid_key) or paid_provider
        entries.append((paid_provider, paid_key, model_of(paid_provider)))

    chain, seen = [], set()
    for provider, key, model in entries:
        provider = (provider or "").strip().lower()
        key = (key or "").strip()
        if provider not in AI_PROVIDERS or not key:
            continue
        model = (model or "").strip() or default_model_for(provider)
        if provider == "openai":
            model = OPENAI_MODEL
        sig = (provider, key, model)
        if sig in seen:  # 같은 키·같은 모델을 두 번 부르는 건 의미가 없다(같은 한도에 막힌다)
            continue
        seen.add(sig)
        chain.append({"provider": provider, "api_key": key, "model": model})
    return chain[:AI_CHAIN_MAX]


def chain_to_legacy(chain: list, config: dict) -> dict:
    """구버전(1.3.3 이하)이 같은 설정 파일을 읽어도 동작하도록 옛 칸을 채운다. 1순위=무료, 2순위=유료."""
    out = {}
    if not chain:
        return out
    out["ai_provider"] = chain[0]["provider"]
    for p in AI_PROVIDERS:
        first = next((e for e in chain if e["provider"] == p), None)
        if first:
            out[f"{p}_api_key"] = first["api_key"]
            if p in ("gemini", "claude"):
                out[f"{p}_model"] = first["model"]
    if len(chain) > 1:
        out["paid_api_key"] = chain[1]["api_key"]
        out["paid_provider"] = chain[1]["provider"]
    else:
        out["paid_api_key"] = ""
        out["paid_provider"] = ""
    return out


# ── 이름 짓는 지침(naming_rules + common_rules) ───────────────────────────────
# 2.0.0 부터 지침을 칸별로 저장하고, AI 에 보낼 때 「1번부터 처음 맞는 하나만」 글로 합친다.
# naming_rules 가 비어 있으면 구버전 통짜 user_rules 를 그대로 쓴다(기존 사용자 동작 보존).
def compose_user_rules(config: dict) -> str:
    rules = [r for r in (config.get("naming_rules") or [])
             if isinstance(r, dict) and ((r.get("when") or "").strip() or (r.get("how") or "").strip())]
    common = (config.get("common_rules") or "").strip()
    if not rules and not common:
        return (config.get("user_rules") or "").strip()

    lines = []
    if rules:
        lines.append(
            "[적용 방법] 아래 지침을 1번부터 차례로 확인하고, 처음 해당하는 지침 하나만 적용한다. "
            "지침이 파일명 형식을 정하면 그 형식대로 완성한 이름을 custom_filename 에 넣는다. "
            "지침이 '바꾸지 않음'이면 decision 을 \"ok\" 로 한다. 해당하는 지침이 없으면 기본 방식(summary/site)으로 짓는다."
        )
        for i, r in enumerate(rules, 1):
            when = (r.get("when") or "").strip()
            how = (r.get("how") or "").strip()
            lines.append(f"\n{i}. 조건: {when}\n   → {how}")
    if common:
        lines.append("\n[공통 규칙 — 어떤 지침을 적용하든 항상 같이 지킨다]\n" + common)
    return "\n".join(lines).strip()


# ── 「AI로 다듬기」(설정 창의 지침·이동 규칙 추가 창) ─────────────────────────
class RefineError(Exception):
    """사람에게 그대로 보여줄 한국어 메시지를 담는다."""


REFINE_PROMPTS = {
    "rule": (
        "너는 파일 자동 이름짓기 프로그램의 지침을 다듬는 도우미다. 사용자가 구어체로 적은 "
        "'어떤 파일일 때'와 '이름은 이렇게'를, 작은 AI 모델도 헷갈리지 않게 구체적이고 명확한 한국어로 고쳐라. "
        "뜻을 바꾸거나 새 조건을 지어내지 말고, 이름 형식은 자리 순서가 보이게 밑줄(_)로 이어 쓰고 날짜는 YYYYMMDD 처럼 자릿수를 밝혀라. "
        "가능하면 how 끝에 '예: ...' 로 완성된 이름 예시 하나를 붙여라. "
        "반드시 JSON 한 개만 출력: {\"when\": \"...\", \"how\": \"...\"}"
    ),
    "move": (
        "너는 파일 자동 분류 프로그램의 이동 조건을 다듬는 도우미다. 사용자가 구어체로 적은 조건을, "
        "작은 AI 모델이 파일 내용을 보고 해당 여부를 판단하기 쉽게 구체적인 한 문장의 한국어 명사구로 고쳐라. "
        "뜻을 바꾸거나 새 조건을 지어내지 마라. 반드시 JSON 한 개만 출력: {\"when\": \"...\"}"
    ),
    "common": (
        "너는 파일 자동 이름짓기 프로그램의 공통 규칙을 다듬는 도우미다. 사용자가 구어체로 적은 규칙을, "
        "작은 AI 모델이 지키기 쉽게 번호 붙인 짧은 문장들로 고쳐라. 뜻을 바꾸거나 새 규칙을 지어내지 마라. "
        "반드시 JSON 한 개만 출력: {\"when\": \"...\"} (when 에 다듬은 규칙 전체)"
    ),
}


def _refine_http_message(code: int) -> str:
    if code in (401, 403):
        return f"API 키가 올바르지 않습니다 (HTTP {code}). AI 탭에서 키를 확인해 주세요."
    if code == 429:
        return "사용 한도에 걸렸습니다 (HTTP 429). 잠시 뒤 다시 시도해 주세요."
    if code == 404:
        return "모델을 찾을 수 없습니다 (HTTP 404). AI 탭에서 다른 모델을 골라 주세요."
    if code >= 500:
        return f"AI 서버가 바쁩니다 (HTTP {code}). 잠시 뒤 다시 시도해 주세요."
    return f"요청이 거절됐습니다 (HTTP {code})."


REFINE_SCHEMA = {
    "type": "object",
    "properties": {"when": {"type": "string"}, "how": {"type": "string"}},
    "required": ["when", "how"],
    "additionalProperties": False,
}


def _simple_complete(entry: dict, system: str, user: str, timeout: int = 30) -> str:
    """순위 항목 하나로 짧은 JSON 생성(다듬기 전용). 공급자마다 JSON 출력을 강제한다 —
    강제하지 않으면 문자열 안에 줄바꿈을 그대로 넣은 깨진 JSON 이 가끔 와서 파싱이 실패했다(2026-10-14 실측)."""
    provider, key, model = entry["provider"], entry["api_key"], entry["model"]
    if provider == "claude":
        url = "https://api.anthropic.com/v1/messages"
        body = {"model": model, "max_tokens": 1000, "system": system,
                "messages": [{"role": "user", "content": user}],
                "output_config": {"format": {"type": "json_schema", "schema": REFINE_SCHEMA}}}
        headers = {"Content-Type": "application/json", "x-api-key": key, "anthropic-version": CLAUDE_API_VERSION}
    elif provider == "openai":
        url = "https://api.openai.com/v1/chat/completions"
        body = {"model": model, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    else:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"responseMimeType": "application/json"}}
        headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if provider == "claude":
        return "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
    if provider == "openai":
        return data["choices"][0]["message"]["content"]
    return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])


def refine_text(kind: str, when: str, how: str = "") -> dict:
    """
    설정 창 「AI로 다듬기」. 저장된 설정의 1순위 키로 한 번 호출해 {"when", "how"?, "model"} 을 돌려준다.
    실패하면 RefineError(사람에게 보여줄 메시지).
    """
    chain = get_ai_chain(load_config())
    if not chain:
        raise RefineError("저장된 API 키가 없어요. AI 탭에서 키를 넣고 저장한 뒤에 쓸 수 있어요.")
    entry = chain[0]
    # 오늘 날짜를 알려 줘야 예시 이름이 옛 연도(20240315 등)로 안 나온다
    system = REFINE_PROMPTS[kind] + f" 오늘 날짜는 {datetime.date.today().strftime('%Y-%m-%d')} 이니 예시 날짜도 이 근처로 써라."
    user = f"어떤 파일일 때: {when.strip()}"
    if kind == "rule":
        user += f"\n이름은 이렇게: {how.strip()}"
    if kind == "common":
        user = f"공통 규칙:\n{when.strip()}"
    try:
        text = _simple_complete(entry, system, user)
    except urllib.error.HTTPError as e:
        raise RefineError(_refine_http_message(e.code)) from e
    except urllib.error.URLError as e:
        if "timed out" in str(e.reason).lower():
            raise RefineError("응답 시간이 초과됐습니다. 잠시 뒤 다시 시도해 주세요.") from e
        raise RefineError("인터넷 연결을 확인해 주세요. AI 서버에 연결하지 못했습니다.") from e
    except (TimeoutError, OSError) as e:
        raise RefineError("응답 시간이 초과됐습니다. 잠시 뒤 다시 시도해 주세요.") from e
    except Exception as e:
        raise RefineError(f"응답을 읽지 못했습니다 ({e}).") from e

    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        # strict=False: 문자열 안의 날 줄바꿈·탭을 허용한다
        parsed = json.loads(m.group(0), strict=False) if m else {}
    except Exception:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    out_when = (parsed.get("when") or "").strip()
    if not out_when:
        raise RefineError("AI 응답이 비어 있거나 형식이 달랐습니다. 다시 시도해 주세요.")
    result = {"when": out_when, "model": entry["model"]}
    if kind == "rule":
        result["how"] = (parsed.get("how") or "").strip() or how.strip()
    return result


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


def nfc(s: str) -> str:
    """자모가 풀린 한글(NFD: 맥·일부 웹에서 받은 파일명/본문 'ㅅㅔㄱㅡㅁ')을 완성형으로 합친다. 결과가 확정적이라 모델에 맡기지 않는다."""
    return unicodedata.normalize("NFC", s or "")


def _truncate(text: str, limit: int) -> str:
    text = nfc(text)
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
        # IBK기업은행 등 일부 은행 xlsx 는 스타일 시트에 openpyxl 이 모르는 속성(styleId)이 있어 통째로 실패한다
        # (2026-10-14 실측: 거래내역·카드청구 엑셀). 셀 값만 필요하니 시트 XML 을 직접 읽는다.
        raw = _extract_xlsx_preview_raw(filepath, limit)
        return raw if raw else f"(엑셀을 읽을 수 없음: {e})"
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


def _extract_xlsx_preview_raw(filepath: str, limit: int) -> str:
    """openpyxl 이 못 여는 xlsx 용 대체 경로: zip 안의 sharedStrings·시트 XML 에서 셀 값만 읽는다(앞 3시트·30행)."""
    import zipfile
    import xml.etree.ElementTree as ET
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    try:
        with zipfile.ZipFile(filepath) as z:
            names = z.namelist()
            shared = []
            if "xl/sharedStrings.xml" in names:
                root = ET.fromstring(z.read("xl/sharedStrings.xml"))
                for si in root.findall("m:si", ns):
                    shared.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
            sheet_titles = []
            if "xl/workbook.xml" in names:
                wb_root = ET.fromstring(z.read("xl/workbook.xml"))
                sheet_titles = [s.get("name") for s in wb_root.findall("m:sheets/m:sheet", ns)]
            sheet_files = sorted(
                (n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
                key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)),
            )
            lines = []
            for idx, sf in enumerate(sheet_files[:3]):
                title = sheet_titles[idx] if idx < len(sheet_titles) else sf.rsplit("/", 1)[-1]
                lines.append(f"[시트: {title}]")
                root = ET.fromstring(z.read(sf))
                rows = root.findall("m:sheetData/m:row", ns)[:30]
                for row in rows:
                    vals = []
                    for cell in row.findall("m:c", ns):
                        t = cell.get("t")
                        v = cell.find("m:v", ns)
                        if t == "s" and v is not None and (v.text or "").isdigit():
                            i = int(v.text)
                            vals.append(shared[i] if i < len(shared) else "")
                        elif t == "inlineStr":
                            vals.append("".join(x.text or "" for x in cell.iter(f"{{{ns['m']}}}t")))
                        else:
                            vals.append(v.text if v is not None and v.text else "")
                    lines.append(" | ".join(vals))
                if not rows:
                    lines.append("(빈 시트)")
            return _truncate("\n".join(lines), limit) if lines else ""
    except Exception:
        return ""


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


IMAGE_MAX_SIDE = 2000          # 긴 변. 문서 글자를 읽기엔 충분하고 요청 크기 한도(인라인 약 20MB)와 거리가 멀다
IMAGE_MAX_BYTES = 3 * 1024 * 1024  # 이보다 큰 원본은 변 길이와 무관하게 다시 인코딩


def get_image_base64(filepath: str) -> tuple[str, str] | None:
    """
    이미지를 base64로 인코딩합니다. 크면 긴 변 IMAGE_MAX_SIDE 로 줄여 JPEG 로 다시 굽는다.
    원본을 그대로 보내면 고해상도 사진·스캔이 요청 크기 한도를 넘어 400 으로 실패하는데,
    400 은 '설정 오류'로 분류돼 유료 키로도 안 넘어가고 그냥 실패한다.
    """
    mime, _ = mimetypes.guess_type(filepath)
    if not mime:
        mime = "image/jpeg"
    try:
        with open(filepath, "rb") as f:
            raw = f.read()
    except Exception:
        return None

    try:
        from PIL import Image, ImageOps
        import io
        with Image.open(io.BytesIO(raw)) as im:
            if max(im.size) > IMAGE_MAX_SIDE or len(raw) > IMAGE_MAX_BYTES:
                im = ImageOps.exif_transpose(im)  # 폰 사진 회전 정보 반영(줄이면 EXIF 가 사라지므로 먼저)
                im = im.convert("RGB")             # 투명 PNG·팔레트·CMYK 를 JPEG 로 굽기 위해
                im.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=88)
                raw, mime = buf.getvalue(), "image/jpeg"
    except Exception:
        pass  # PIL 이 못 여는 형식이면 원본 그대로 보낸다(종전 동작)

    return mime, base64.b64encode(raw).decode("utf-8")


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


# ====================== 사실값 후보 추출 · 로컬 OCR ======================
# 원칙: 원문은 그대로 보내고 아래 결과는 "덧붙이기"만 한다. 정규식이 뽑은 값은 틀릴 수 있으므로
# 근거 줄과 함께 '후보'로만 주고 판단은 모델이 한다(거래처명처럼 판단이 필요한 값은 아예 뽑지 않는다).

_DATE_PATTERNS = [
    re.compile(r"(?<!\d)(20\d{2})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})(?!\d)"),
    re.compile(r"(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일"),
    re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)"),
]
_AMOUNT_RE = re.compile(
    r"(?:(USD|EUR|JPY|CNY|GBP|KRW|US\$|\$|€|¥|￦|₩)\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d{2}))"
    r"|(?:(\d{1,3}(?:,\d{3})+(?:\.\d+)?)\s*(USD|EUR|JPY|CNY|GBP|KRW|원))"
)
_BIZNO_RE = re.compile(r"(?<!\d)(\d{3})\s*-\s*(\d{2})\s*-\s*(\d{5})(?!\d)")


def _label_before(line: str, start: int) -> str:
    """값 바로 앞의 글자(최대 14자)를 근거 라벨로. 예: '작성일자 2026-08-31' → '작성일자'"""
    return re.sub(r"\s+", " ", line[max(0, start - 14):start]).strip(" :|[]()")


def extract_fact_candidates(text: str, max_each: int = 8) -> str:
    dates, amounts, biz = [], [], []
    for line in (text or "").splitlines():
        for pat in _DATE_PATTERNS:
            for m in pat.finditer(line):
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                try:
                    datetime.date(y, mo, d)
                except ValueError:
                    continue  # 계좌번호·승인번호 같은 숫자열이 날짜처럼 잡히는 것 거르기
                v = f"{y:04d}{mo:02d}{d:02d}"
                if v not in [x[0] for x in dates]:
                    dates.append((v, _label_before(line, m.start())))
        for m in _AMOUNT_RE.finditer(line):
            v = m.group(0).strip()
            if v not in [x[0] for x in amounts]:
                amounts.append((v, _label_before(line, m.start())))
        for m in _BIZNO_RE.finditer(line):
            v = "-".join(m.groups())
            if v not in biz:
                biz.append(v)

    out = []
    if dates:
        out.append("- 날짜 후보(YYYYMMDD): " + ", ".join(f"{v}({lab})" if lab else v for v, lab in dates[:max_each]))
    if amounts:
        out.append("- 금액 후보: " + ", ".join(f"{v}({lab})" if lab else v for v, lab in amounts[:max_each]))
    if biz:
        out.append("- 사업자등록번호: " + ", ".join(biz[:4]))
    if not out:
        return ""
    return ("[참고: 프로그램이 본문에서 기계적으로 찾은 값 후보 — 괄호는 바로 앞 글자. 틀릴 수 있으니 원문과 맞을 때만 써]\n"
            + "\n".join(out))


_OCR_PS = r"""
param([string]$Img, [string]$Out)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$at = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($t, $rt) { $m = $at.MakeGenericMethod($rt); $x = $m.Invoke($null, @($t)); $x.Wait(-1) | Out-Null; $x.Result }
[Windows.Globalization.Language, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
$e = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage((New-Object Windows.Globalization.Language 'ko'))
if (-not $e) { $e = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if (-not $e) { exit 3 }
$f = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Img)) ([Windows.Storage.StorageFile])
$s = Await ($f.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$d = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($s)) ([Windows.Graphics.Imaging.BitmapDecoder])
$b = Await ($d.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$r = Await ($e.RecognizeAsync($b)) ([Windows.Media.Ocr.OcrResult])
$s.Dispose()
($r.Lines | ForEach-Object { $_.Text }) -join "`n" | Out-File -FilePath $Out -Encoding utf8
"""


def local_ocr(image_bytes: bytes, timeout: int = 40) -> str:
    """
    윈도우 내장 OCR(WinRT, 외부 전송 없음). PowerShell 5.1 로 돌려 파이썬 의존성이 늘지 않는다.
    실패하면 빈 문자열 — 그 경우 종전처럼 이미지만 보낸다.
    """
    import subprocess
    tmp_dir = tempfile.mkdtemp(prefix="rw_ocr_")
    try:
        img = os.path.join(tmp_dir, "page.png")
        out = os.path.join(tmp_dir, "ocr.txt")
        ps1 = os.path.join(tmp_dir, "ocr.ps1")
        with open(img, "wb") as f:
            f.write(image_bytes)
        with open(ps1, "w", encoding="utf-8-sig") as f:  # PS 5.1 은 BOM 없으면 한글 스크립트를 ANSI 로 읽는다
            f.write(_OCR_PS)
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1, "-Img", img, "-Out", out],
            capture_output=True, timeout=timeout, creationflags=0x08000000,  # CREATE_NO_WINDOW
            stdin=subprocess.DEVNULL,  # --windowed exe 는 표준입력 핸들이 없어 상속하면 '핸들이 잘못됨'으로 실패할 수 있다
        )
        if not os.path.exists(out):
            return ""
        with open(out, "r", encoding="utf-8-sig") as f:
            return f.read().strip()
    except Exception:
        return ""
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def enrich_parts(parts: list, kind: str, config: dict, limit: int) -> list:
    """build_content_parts 결과에 OCR 글자와 사실값 후보를 덧붙인다(원래 parts 는 그대로 둔다)."""
    extra_text = ""
    if config.get("local_ocr", True) and kind in ("image", "pdf-image"):
        img = next((p["inline_data"]["data"] for p in parts if "inline_data" in p), None)
        if img:
            ocr = local_ocr(base64.b64decode(img))
            if ocr:
                extra_text = ocr
                parts = parts + [{"text": "[윈도우 내장 OCR로 읽은 글자 — 오인식이 있을 수 있으니 이미지와 다르면 이미지가 우선]\n"
                                          + _truncate(ocr, limit)}]

    if config.get("extract_facts", True):
        src = "\n".join(p["text"] for p in parts if "text" in p) + "\n" + extra_text
        facts = extract_fact_candidates(nfc(src))
        if facts:
            parts = [{"text": facts}] + parts
    return parts


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
    """
    429 응답 본문이 '일일 한도 소진'인지(분당 한도가 아니라) 판정한다.
    🚨 반드시 본문 **전체**를 넘길 것 — 표식(quotaId ...PerDay...)은 본문 1,000자 근처에 있어서
    로그용으로 500자 자른 본문으로 판정하면 항상 '분당'으로 오판한다(2026-10-06 실측, 그동안 일일 소진을 한 번도 기억 못 했다).
    """
    normalized = re.sub(r"[\s_\-]+", "", body_text or "").lower()
    if any(marker in normalized for marker in DAILY_QUOTA_MARKERS):
        return True
    # 표식이 없더라도 재시도 대기가 10분을 넘으면 분당 한도일 수 없다
    m = re.search(r'"retryDelay"\s*:\s*"(\d+)s"', body_text or "")
    return bool(m and int(m.group(1)) > 600)


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
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
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
이 경우 그 지침대로 완성한 최종 파일명(확장자 제외)을 custom_filename 필드에 그대로 채워.
날짜가 필요하면: 지침이 문서의 날짜(증빙날짜·입금날짜·작성일자·납부기한 등)를 가리키면 파일 내용에서 그 날짜를 찾아 쓰고,
그런 지시가 없거나 문서에서 날짜를 찾을 수 없을 때만 오늘 날짜 {today}를 써.
지침이 날짜 자릿수(예: yymmdd)를 정하지 않았으면 날짜는 항상 YYYYMMDD 8자리로 써.
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
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
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
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
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


def _call_claude(system_prompt: str, parts: list, api_key: str, max_output_tokens: int,
                  model: str = DEFAULT_CLAUDE_MODEL) -> tuple[dict, dict]:
    """Claude Messages API 호출 (output_config.format으로 구조화 출력 강제).
    model: config["claude_model"]로 사용자가 선택한 모델."""
    content = _parts_to_claude_content(parts)
    body = json.dumps({
        "model": model,
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
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    # 최신 모델은 content[0] 에 thinking 등 text 아닌 블록이 먼저 올 수 있다 — text 블록을 찾아 쓴다
    blocks = data.get("content") or []
    texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    if not texts:
        raise ValueError(f"AI 응답에 text 블록 없음 - stop_reason={data.get('stop_reason')} "
                         f"blocks={[b.get('type') for b in blocks]}")
    text = "".join(texts).strip()
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
             | "unavailable"  503 등 과부하·응답 시간 초과 → 다른 모델은 멀쩡할 수 있다
             | "fail"         그 외 실패(401/400 등). 키를 바꿔도 같은 결과일 가능성이 높다
    """
    started = time.time()
    for attempt in range(MAX_RETRIES + 1):
        try:
            parsed, usage = call_fn(system_prompt, parts, api_key, max_output_tokens)
            return "ok", parsed, usage
        except urllib.error.HTTPError as e:
            try:
                full_body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                full_body = "(응답 본문 읽기 실패)"
            body_text = full_body[:500]  # 로그용. 판정은 full_body 로 한다(_is_daily_quota_error 주석 참고)

            # 429는 대기로 버티지 않고 곧바로 빠져나가 상위에서 다음 모델/유료 키로 넘긴다.
            # (4초 대기로는 분당 한도가 회복되지 않으므로 재시도해봐야 같은 429만 맞는다)
            within_budget = time.time() - started + RETRY_DELAY_SECONDS < RETRY_BUDGET_SECONDS
            if e.code in RETRYABLE_HTTP_CODES and e.code != 429 and attempt < MAX_RETRIES and within_budget:
                _log_error(
                    f"[AI 재시도] {fullname}: {provider}({key_label} 키) HTTP {e.code} 일시적 오류 - "
                    f"{RETRY_DELAY_SECONDS}초 후 재시도 ({attempt + 1}/{MAX_RETRIES}) / {body_text}"
                )
                time.sleep(RETRY_DELAY_SECONDS)
                continue

            if e.code == 429:
                status = "quota_day" if _is_daily_quota_error(full_body) else "quota_minute"
            elif e.code in RETRYABLE_HTTP_CODES:
                status = "unavailable"  # 503 등 과부하 — 다른 모델은 멀쩡할 수 있다
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
            # 응답 시간 초과는 과부하와 같게 취급 — 다른 모델로 넘긴다
            if isinstance(e, TimeoutError) or "timed out" in str(e).lower():
                return "unavailable", None, None
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
    if provider == "claude":
        claude_model = config.get("claude_model", DEFAULT_CLAUDE_MODEL) or DEFAULT_CLAUDE_MODEL
        return functools.partial(_call_claude, model=claude_model), provider
    return {"openai": _call_openai}[provider], provider


def _free_gemini_stages(config: dict) -> list:
    """
    무료 Gemini 키로 시도할 (모델, 호출함수, 한도키) 순서. 고른 모델이 맨 앞이고 나머지는 GEMINI_FALLBACK_ORDER.
    오늘 일일 한도가 소진된 모델은 뺀다(어차피 429 — 파일마다 헛호출만 는다).
    config["gemini_model_rotation"] 이 False 면 고른 모델 하나만(종전 동작).
    """
    chosen = config.get("gemini_model", DEFAULT_GEMINI_MODEL) or DEFAULT_GEMINI_MODEL
    models = [chosen]
    if config.get("gemini_model_rotation", True):
        models += [m for m in GEMINI_FALLBACK_ORDER if m != chosen]
    stages = []
    for m in models:
        qk = f"gemini:{m}"
        if is_free_quota_exhausted(qk):
            continue
        stages.append((m, functools.partial(_call_gemini, model=m), qk))
    return stages


# ── 맥락 플러그인 (선택) ─────────────────────────────────────────────────────
# config["context_plugin"] 에 파이썬 파일 경로를 적으면 그 파일의 get_context(filepath, config) -> str 를 불러,
# 돌려준 글을 AI 에 「파일 맥락」으로 붙인다(예: 브라우저 기록에서 찾은 「어느 화면에서 받았나」).
# 플러그인이 open_settings() 를 가지면 트레이 메뉴에 「맥락 플러그인 설정」이 생긴다.
# 경로가 비었거나 파일이 없거나 오류가 나면 조용히 건너뛴다 — 이름짓기는 맥락 없이 계속한다.
CONTEXT_MAX_CHARS = 1500
_plugin_cache = {}


def load_context_plugin(config: dict | None = None):
    path = ((config if config is not None else load_config()).get("context_plugin") or "").strip()
    if not path or not os.path.isfile(path):
        return None
    try:
        mtime = os.path.getmtime(path)
        hit = _plugin_cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        import importlib.util
        spec = importlib.util.spec_from_file_location("autorename_context_plugin", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _plugin_cache[path] = (mtime, mod)
        return mod
    except Exception as e:
        _log_error(f"[맥락 플러그인 로드 실패] {path}: {e}")
        return None


def get_file_context(filepath: str, config: dict) -> str:
    mod = load_context_plugin(config)
    if not mod or not hasattr(mod, "get_context"):
        return ""
    try:
        return (mod.get_context(filepath, config) or "").strip()[:CONTEXT_MAX_CHARS]
    except Exception as e:
        _log_error(f"[맥락 플러그인 오류] {os.path.basename(filepath)}: {e}")
        return ""


def analyze_file(filepath: str) -> dict | None:
    """설정 파일에서 매번 동적으로 키와 추가 지침을 로드하여 가벼운 한도로 분석을 수행합니다."""
    config = load_config()
    chain = get_ai_chain(config)

    max_output_tokens = int(config.get("max_output_tokens", 99999))

    # 3만 자에서 다시 가벼운 2천 자 한도로 강제 제한 조정하여 429 에러 방지
    max_preview_chars = int(config.get("max_preview_chars", 6000))
    user_rules = compose_user_rules(config)
    move_rules = config.get("move_rules", []) or []
    filename_format = config.get("filename_format", "").strip() or DEFAULT_FILENAME_FORMAT

    if not chain:
        print("[경고] AI 순위에 키가 하나도 없습니다.")
        _log_error(f"[AI 분석 오류] {os.path.basename(filepath)}: API 키가 설정되지 않았습니다. (설정 → AI 탭)")
        return None

    dirpath, fullname = os.path.split(filepath)
    basename, ext = os.path.splitext(fullname)
    ext = ext.lower()
    today = datetime.date.today().strftime("%Y%m%d")
    domain = get_source_domain(filepath) or "출처불명"

    # 가벼운 2000자 한도로 텍스트 미리보기 데이터 파싱
    parts, _debug = build_content_parts(filepath, ext, max_preview_chars)
    parts = enrich_parts(parts, _debug, config, max_preview_chars)

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        basename=nfc(basename), ext=ext, domain=domain, today=today
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

    file_context = get_file_context(filepath, config)
    if file_context:
        system_prompt += (
            f"\n\n[파일 맥락 — 이 파일을 받을 때의 상황 (사용자 PC 기록)]\n{file_context}\n"
            "이 맥락은 어떤 지침에 해당하는지 고르거나 날짜·출처를 정할 때 참고해. "
            "파일 내용과 어긋나면 파일 내용을 우선해."
        )

    # AI 순위대로 시도할 계획을 세운다. 각 단계: (라벨, 키, 공급자/모델, 호출함수, 한도키).
    # Gemini 항목은 한도가 모델별이라 고른 모델이 막히면 다른 Gemini 모델로 돌린다(gemini_model_rotation).
    # 한도키에 키 끝 6자리를 넣는다 — 같은 공급자라도 키(프로젝트)가 다르면 한도가 따로다.
    rotation = config.get("gemini_model_rotation", True)
    key_plan = []
    for rank, entry in enumerate(chain, 1):
        label = f"{rank}순위"
        e_provider, e_key, e_model = entry["provider"], entry["api_key"], entry["model"]
        fp = e_key[-6:]
        if e_provider == "gemini":
            models = [e_model] + ([m for m in GEMINI_FALLBACK_ORDER if m != e_model] if rotation else [])
            added = 0
            for m in models:
                qk = f"gemini:{m}#{fp}"
                if is_free_quota_exhausted(qk):
                    continue
                key_plan.append((label, e_key, f"gemini/{m}", functools.partial(_call_gemini, model=m), qk))
                added += 1
            if not added:
                _log_error(f"[한도 소진] {fullname}: {label} Gemini 모델 한도를 오늘 모두 썼습니다. (내일 자동으로 다시 시도)")
        else:
            qk = f"{e_provider}:{e_model}#{fp}"
            if is_free_quota_exhausted(qk):
                _log_error(f"[한도 소진] {fullname}: {label} {e_provider}/{e_model} 오늘 한도 소진 — 건너뜁니다.")
                continue
            fn = functools.partial(_call_claude, model=e_model) if e_provider == "claude" else _call_openai
            key_plan.append((label, e_key, f"{e_provider}/{e_model}", fn, qk))

    parsed = usage = None
    for i, (key_label, key_value, key_provider, key_call_fn, stage_qk) in enumerate(key_plan):
        status, parsed, usage = _call_with_retries(
            key_call_fn, system_prompt, parts, key_value, max_output_tokens,
            fullname, key_provider, key_label,
        )
        if status == "ok":
            if key_label != "1순위":
                total = count_paid_call(stage_qk)
                _log_error(f"[{key_label}로 처리] {fullname}: {key_provider} - 오늘 이 단계 누적 {total}건")
            elif i > 0:
                _log_error(f"[다른 모델로 처리] {fullname}: {key_provider}")
            break

        # 한도(429)·과부하(503)·시간 초과로 막힌 경우에만 다음 단계(다른 모델 → 다음 순위)로 넘어간다.
        # 401/400 같은 설정 오류는 모델·키를 바꿔도 성격이 다르므로 괜히 소모하지 않고 여기서 끝낸다.
        if status in ("quota_day", "quota_minute", "unavailable"):
            if status == "quota_day":
                mark_free_quota_exhausted(stage_qk)
                _log_error(f"[한도 소진] {stage_qk}: 오늘은 이 단계를 건너뜁니다. (내일 자동으로 다시 시도)")
            if i + 1 < len(key_plan):
                nxt = key_plan[i + 1]
                _log_error(f"[다음 단계로 전환] {fullname}: {key_label} {key_provider} {status} → {nxt[0]} {nxt[2]}")
            continue

        return None

    if parsed is None:
        # 계획된 키를 다 써도 실패했거나(무료만 있고 429), 애초에 쓸 키가 없던 경우
        return None

    decision = parsed.get("decision", "ambiguous")
    reason = parsed.get("reason", "")

    def clean(s: str) -> str:
        s = nfc(s)
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
            suggested_name = build_filename_from_format(
                filename_format, today, summary, site,
                time=datetime.datetime.now().strftime("%H%M"),
                original=clean(basename),
                date_format=config.get("date_format") or DEFAULT_DATE_FORMAT,
            )

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
