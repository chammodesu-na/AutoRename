# -*- coding: utf-8 -*-
"""
tray_app.py
------------
시스템 트레이 아이콘을 구동하는 최상위 부모 인스턴스.
실행 취소 이벤트 리시버 및 독립 프로세스로 안전하게 작동하는 환경 설정 GUI를 포함합니다.
"""

import os
import sys
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw
import pystray

import rename_watcher as watcher
import updater


def create_icon_image(color="#6ee7b7"):
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 6
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=color,
    )
    draw.ellipse(
        [size // 2 - 6, size // 2 - 6, size // 2 + 6, size // 2 + 6],
        fill="#1e1e22",
    )
    return img


def create_paused_icon_image():
    return create_icon_image(color="#9a9aa2")


def open_log_file():
    if not os.path.exists(watcher.LOG_FILE):
        try:
            with open(watcher.LOG_FILE, "w", encoding="utf-8") as f:
                f.write("")
        except Exception:
            return
    try:
        os.startfile(watcher.LOG_FILE)
    except AttributeError:
        try:
            subprocess.Popen(["xdg-open", watcher.LOG_FILE])
        except Exception:
            pass


def message_box(text: str, title: str = "AutoRename", yes_no: bool = False) -> bool:
    """트레이 콜백 스레드에서도 안전하게 뜨는 윈도우 기본 메시지창. 예/아니오면 '예'일 때 True."""
    import ctypes
    MB_YESNO, MB_ICONINFO, MB_TOPMOST, MB_SETFOREGROUND, IDYES = 0x4, 0x40, 0x40000, 0x10000, 6
    flags = MB_ICONINFO | MB_TOPMOST | MB_SETFOREGROUND | (MB_YESNO if yes_no else 0)
    return ctypes.windll.user32.MessageBoxW(None, text, title, flags) == IDYES


_update_lock = threading.Lock()


def run_update_check(on_installing):
    """업데이트 확인 → (새 버전이면) 다운로드·검증 → 조용히 설치. on_installing() 은 앱 종료 콜백."""
    if not _update_lock.acquire(blocking=False):
        return  # 이미 확인/다운로드 중
    try:
        current = updater.APP_VERSION
        try:
            info = updater.check_latest()
        except Exception as e:
            watcher.log(f"[업데이트 확인 실패] {e}")
            message_box(f"업데이트 정보를 가져오지 못했습니다.\n인터넷 연결을 확인해 주세요.\n\n({e})")
            return

        if not info["newer"]:
            message_box(f"최신 버전을 사용 중입니다. (v{current})")
            return

        if not getattr(sys, "frozen", False):
            # 소스(.py)로 돌리는 경우엔 설치본을 깔면 안 된다 — 다른 위치에 exe 가 따로 생긴다.
            message_box(f"새 버전 v{info['version']} 이 있습니다. (현재 v{current})\n\n"
                        "소스로 실행 중이라 자동 설치는 하지 않습니다. git pull 로 받아 주세요.")
            return

        if not info["url"]:
            message_box(f"새 버전 v{info['version']} 이 있지만 설치 파일이 아직 올라오지 않았습니다.\n"
                        f"잠시 뒤 다시 시도하거나 아래에서 직접 받아 주세요.\n\n{updater.RELEASES_PAGE}")
            return

        size_mb = info["size"] / 1024 / 1024
        if not message_box(
            f"새 버전 v{info['version']} 이 있습니다. (현재 v{current})\n\n"
            f"지금 받아서 설치할까요? (약 {size_mb:.0f}MB)\n"
            "설치하는 동안 잠시 꺼졌다가 자동으로 다시 켜지고, 설정은 그대로 유지됩니다.",
            yes_no=True,
        ):
            return

        watcher.show_toast("업데이트 다운로드 중", f"v{info['version']} ({size_mb:.0f}MB)를 받고 있습니다…", accent="#6ee7b7")
        watcher.log(f"[업데이트 다운로드] v{current} -> v{info['version']}")
        try:
            path = updater.download_installer(info)
        except Exception as e:
            watcher.log(f"[업데이트 다운로드 실패] {e}")
            message_box(f"다운로드에 실패했습니다.\n\n{e}\n\n직접 받기: {updater.RELEASES_PAGE}")
            return

        watcher.log(f"[업데이트 설치 시작] {path}")
        updater.launch_installer(path)
        on_installing()
    finally:
        _update_lock.release()


def show_settings_gui():
    """독립된 프로세스로 실행되어 스레드 락 없이 안전하게 구동되는 환경설정 GUI 창

    2026-10-14 개편: 한 장짜리 긴 스크롤 → 탭 3개(일반 / AI / 규칙) + 하단 고정 저장 버튼.
    - AI 탭은 고른 공급자의 키·모델만 보인다(나머지 공급자 칸은 숨기되 값은 그대로 저장).
    - 유료 키는 「고급 설정」 아래로 접고, API 키는 가려서 「보기」를 눌렀을 때만 보인다.
    - 저장할 때 화면에 없는 설정 키(gemini_model_rotation 등)도 지우지 않고 그대로 둔다.
    """
    from file_namer import (
        load_config, save_config, DEFAULT_FILENAME_FORMAT, AI_PROVIDERS, DEFAULT_GEMINI_MODEL,
        DEFAULT_CLAUDE_MODEL, OPENAI_MODEL,
    )

    config = load_config()

    root = tk.Tk()
    root.title("AutoRename 설정")
    root.configure(bg="#1e1e22")
    root.attributes("-topmost", True)

    # ---- 화면 크기에 맞춰 창 크기/위치를 동적으로 결정 ----
    # 작은 화면(노트북)에서 작업표시줄까지 빼면 창이 화면 밖으로 밀려나던 문제가 있어 화면 크기 기준으로 제한한다.
    desired_w, desired_h = 560, 620
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    win_w = max(min(desired_w, sw - 80), 440)
    win_h = max(min(desired_h, sh - 120), 420)
    x = max((sw - win_w) // 2, 0)
    y = max((sh - win_h) // 2, 0)
    root.geometry(f"{win_w}x{win_h}+{x}+{y}")
    root.resizable(True, True)
    root.minsize(440, 420)

    # ---- 색·글꼴 ----
    fg_mint = "#6ee7b7"
    bg_dark = "#1e1e22"
    bg_entry = "#2a2a2f"
    bg_button = "#3a3a40"
    fg_white = "#f2f2f2"
    sub_color = "#9a9aa2"
    DANGER_FG = "#ff8585"

    header_font = ("Malgun Gothic", 14, "bold")
    label_font = ("Malgun Gothic", 10, "bold")
    section_font = ("Malgun Gothic", 10, "bold")
    desc_font = ("Malgun Gothic", 8)
    body_font = ("Malgun Gothic", 10)

    # ---- ttk 스타일 ----
    # 윈도우 기본 테마(vista)는 콤보박스 fieldbackground 를 무시해서 흰 바탕에 흰 글씨(#f2f2f2)가 되어
    # 포커스를 잃으면 글자가 거의 안 보였다(2026-10-14). clam 은 색 지정이 먹으므로 이걸 쓰고,
    # readonly·focus 상태별 색까지 고정한다.
    style = ttk.Style()
    style.theme_use("clam")
    style.configure(
        "TCombobox", fieldbackground=bg_entry, background=bg_button, foreground=fg_white,
        arrowcolor=fg_white, bordercolor=bg_button, lightcolor=bg_entry, darkcolor=bg_entry,
        selectbackground=bg_entry, selectforeground=fg_white, padding=4,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", bg_entry), ("disabled", bg_dark)],
        foreground=[("readonly", fg_white), ("disabled", sub_color)],
        selectbackground=[("readonly", bg_entry)],
        selectforeground=[("readonly", fg_white)],
        background=[("active", "#4a4a52"), ("pressed", "#4a4a52")],
        bordercolor=[("focus", fg_mint)],
    )
    # 펼친 목록(popdown)은 tk Listbox 라 option DB 로 색을 준다
    root.option_add("*TCombobox*Listbox.background", bg_entry)
    root.option_add("*TCombobox*Listbox.foreground", fg_white)
    root.option_add("*TCombobox*Listbox.selectBackground", fg_mint)
    root.option_add("*TCombobox*Listbox.selectForeground", bg_dark)
    root.option_add("*TCombobox*Listbox.font", body_font)

    # clam 은 탭 본문 둘레에 밝은 테두리를 그리므로 테두리 색을 전부 배경색으로 지운다
    style.configure(
        "TNotebook", background=bg_dark, borderwidth=0, tabmargins=(24, 0, 24, 0),
        bordercolor=bg_dark, lightcolor=bg_dark, darkcolor=bg_dark,
    )
    style.configure(
        "TNotebook.Tab", background=bg_entry, foreground=sub_color, padding=(18, 7),
        borderwidth=0, font=label_font, lightcolor=bg_entry, bordercolor=bg_dark,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", bg_dark), ("active", "#34343a")],
        foreground=[("selected", fg_mint), ("active", fg_white)],
        lightcolor=[("selected", bg_dark)],
    )

    # ---- 공통 위젯 도우미 ----
    def flat_button(parent, text, command, fg=fg_white, primary=False, **kw):
        if primary:
            return tk.Button(
                parent, text=text, command=command, bg=fg_mint, fg=bg_dark,
                font=("Malgun Gothic", 10, "bold"), relief="flat", cursor="hand2",
                activebackground="#58d0a3", activeforeground=bg_dark, padx=18, pady=6, **kw,
            )
        return tk.Button(
            parent, text=text, command=command, bg=bg_button, fg=fg, relief="flat", cursor="hand2",
            activebackground=bg_entry, activeforeground=fg, padx=10, pady=3, **kw,
        )

    def flat_entry(parent, **kw):
        return tk.Entry(
            parent, bg=bg_entry, fg=fg_white, insertbackground=fg_white, relief="flat",
            font=body_font, highlightthickness=1, highlightbackground=bg_button,
            highlightcolor=fg_mint, **kw,
        )

    def wrap_label(parent, text, fg=sub_color, font=desc_font):
        """창 폭에 맞춰 자동 줄바꿈되는 안내문. (wraplength=0 이면 줄바꿈 없이 잘려 나간다)"""
        lbl = tk.Label(parent, text=text, fg=fg, bg=bg_dark, font=font, justify="left", anchor="w")

        def _rewrap(event):
            width = max(event.width - 4, 120)
            # 같은 값을 다시 넣으면 <Configure>가 또 돌아 무한 반복이 되므로 바뀔 때만 적용한다
            if lbl.cget("wraplength") != width:
                lbl.configure(wraplength=width)

        lbl.bind("<Configure>", _rewrap)
        return lbl

    def section(parent, title, desc=None, top=18):
        tk.Label(parent, text=title, fg=fg_mint, bg=bg_dark, font=section_font).pack(anchor="w", pady=(top, 4))
        if desc:
            wrap_label(parent, desc).pack(anchor="w", fill="x", pady=(0, 6))

    def secret_entry(parent, value):
        """API 키 입력칸. 기본은 가려 두고 「보기」를 누르면 보인다."""
        row = tk.Frame(parent, bg=bg_dark)
        ent = flat_entry(row, show="●")
        ent.pack(side="left", fill="x", expand=True, ipady=3)
        ent.insert(0, value or "")

        def toggle():
            hidden = ent.cget("show") != ""
            ent.configure(show="" if hidden else "●")
            btn.configure(text="숨기기" if hidden else "보기")

        btn = flat_button(row, "보기", toggle, width=5)
        btn.pack(side="left", padx=(6, 0))
        return row, ent

    def make_listbox(parent, height=5):
        frame = tk.Frame(parent, bg=bg_dark)
        lst = tk.Listbox(
            frame, bg=bg_entry, fg=fg_white, relief="flat", font=body_font,
            selectbackground=fg_mint, selectforeground=bg_dark,
            highlightthickness=1, highlightbackground=bg_button, highlightcolor=fg_mint,
            height=height, activestyle="none", selectmode="extended",
        )
        lst.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(frame, orient="vertical", command=lst.yview)
        sb.pack(side="right", fill="y")
        lst.configure(yscrollcommand=sb.set)
        return frame, lst

    # ---- 레이아웃: 헤더 / 탭 / 하단 고정 버튼줄 ----
    header = tk.Frame(root, bg=bg_dark)
    header.pack(side="top", fill="x", padx=24, pady=(16, 10))
    tk.Label(header, text="AutoRename 설정", fg=fg_mint, bg=bg_dark, font=header_font).pack(side="left")
    tk.Label(header, text=f"v{updater.APP_VERSION}", fg=sub_color, bg=bg_dark, font=desc_font).pack(side="left", padx=(8, 0), pady=(6, 0))

    bottom = tk.Frame(root, bg=bg_entry)
    bottom.pack(side="bottom", fill="x")

    nb = ttk.Notebook(root)
    nb.pack(side="top", fill="both", expand=True)

    # 탭마다 스크롤되는 페이지. 내용이 창보다 짧으면 스크롤바를 숨긴다.
    page_canvases = []
    page_syncs = []
    # 탭을 바꾸면 그 탭이 처음 제 크기를 갖게 되므로 폭·스크롤바를 다시 맞춘다
    nb.bind("<<NotebookTabChanged>>", lambda e: root.after_idle(lambda: [s() for s in page_syncs]))

    def make_page(title):
        page = tk.Frame(nb, bg=bg_dark)
        cv = tk.Canvas(page, bg=bg_dark, highlightthickness=0)
        sb = tk.Scrollbar(page, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(cv, bg=bg_dark)
        body = tk.Frame(inner, bg=bg_dark)
        body.pack(fill="both", expand=True, padx=24, pady=(4, 16))
        win = cv.create_window((0, 0), window=inner, anchor="nw")

        # 폭 맞추기와 스크롤바 판단을 나눈다. 프레임 <Configure> 에서 폭까지 건드리면 폭 지정이 먹지 않았다
        # (실측: 캔버스 항목 폭 539 인데 프레임은 요청폭 707 그대로 → 「규칙」 탭 오른쪽이 잘림).
        # 또 탭이 숨어 있던 동안 프레임이 요청폭으로 커지면, 같은 폭 값을 다시 넣어도 Tk 가 무시한다.
        # 그럴 땐 다른 값을 한 번 넣고 다음 이벤트 때 원래 값을 넣어야 다시 적용된다(같은 호출 안에서 두 번 넣으면 소용없음).
        def _fit_width(_event=None):
            cw = cv.winfo_width()
            if cw <= 1:
                return
            cv.itemconfig(win, width=cw)
            if inner.winfo_width() not in (cw, 1):
                cv.itemconfig(win, width=cw - 1)
                root.after(1, lambda: cv.itemconfig(win, width=cv.winfo_width()))

        def _sync_scroll(_event=None):
            # 숨은 탭은 높이가 1px 이라 판단하면 안 된다(스크롤바가 괜히 붙는다)
            ch = cv.winfo_height()
            if ch <= 1:
                return
            cv.configure(scrollregion=cv.bbox("all"))
            needs = inner.winfo_reqheight() > ch + 1
            if needs and not sb.winfo_ismapped():
                sb.pack(side="right", fill="y")
            elif not needs and sb.winfo_ismapped():
                sb.pack_forget()
                cv.yview_moveto(0)

        inner.bind("<Configure>", _sync_scroll)
        cv.bind("<Configure>", lambda e: (_fit_width(), _sync_scroll()))
        page_syncs.append(lambda: (_fit_width(), _sync_scroll()))
        nb.add(page, text=title)
        page_canvases.append((cv, inner))
        return body

    def on_mousewheel(event):
        # 지침 입력칸·목록 위에서는 그 위젯이 스스로 스크롤하게 둔다
        if isinstance(event.widget, (tk.Text, tk.Listbox)):
            return
        try:
            cv, inner = page_canvases[nb.index("current")]
        except Exception:
            return
        if inner.winfo_reqheight() > cv.winfo_height():
            cv.yview_scroll(int(-1 * (event.delta / 120)), "units")

    root.bind_all("<MouseWheel>", on_mousewheel)

    def on_close():
        root.unbind_all("<MouseWheel>")
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    # =========================================================================
    # 탭 1. 일반 — 감시 폴더, 카운트다운
    # =========================================================================
    tab_general = make_page("  일반  ")

    section(tab_general, "감시 대상 폴더", "이 폴더들에 새 파일이 들어오면 AI 가 보고 이름을 바꿉니다. 여러 개 넣을 수 있습니다.", top=10)
    folders_list_frame, lst_folders = make_listbox(tab_general, height=6)
    folders_list_frame.pack(fill="x")

    existing_folders = config.get("watch_folders")
    if not existing_folders:
        legacy = config.get("watch_folder")
        existing_folders = [legacy] if legacy else []
    for f in existing_folders:
        lst_folders.insert("end", f)

    def add_folder():
        selected = filedialog.askdirectory(parent=root)
        if selected and selected not in lst_folders.get(0, "end"):
            lst_folders.insert("end", selected)

    def remove_selected_folder():
        for idx in reversed(lst_folders.curselection()):
            lst_folders.delete(idx)

    folder_btn_row = tk.Frame(tab_general, bg=bg_dark)
    folder_btn_row.pack(fill="x", pady=(6, 0))
    flat_button(folder_btn_row, "+ 폴더 추가", add_folder).pack(side="left")
    flat_button(folder_btn_row, "선택 삭제", remove_selected_folder, fg=DANGER_FG).pack(side="left", padx=(6, 0))

    section(tab_general, "카운트다운 (초)", "이름을 바꾸기 전에 알림을 띄우고 기다리는 시간입니다. 그동안 취소할 수 있습니다.")
    ent_sec = flat_entry(tab_general, width=8)
    ent_sec.pack(anchor="w", ipady=3)
    ent_sec.insert(0, str(config.get("countdown_seconds", 3)))

    # =========================================================================
    # 탭 2. AI — 공급자, (고른 공급자의) 키·모델, 고급: 유료 키
    # =========================================================================
    tab_ai = make_page("  AI  ")

    PROVIDER_LABELS = {"gemini": "Google Gemini", "openai": "OpenAI (GPT)", "claude": "Anthropic (Claude)"}
    PROVIDER_LABELS_REV = {v: k for k, v in PROVIDER_LABELS.items()}

    # 목록에 없는 값이 config 에 있으면 저장할 때 기본값으로 덮이므로, 쓸 만한 모델은 여기 다 올려 둔다.
    GEMINI_MODEL_LABELS = {
        "gemini-3.5-flash": "Gemini 3.5 Flash (기본)",
        "gemini-3.6-flash": "Gemini 3.6 Flash",
        "gemini-3.7-flash": "Gemini 3.7 Flash (최신급, 수요 몰리면 503)",
        "gemini-3.8-flash": "Gemini 3.8 Flash (최신, 수요 몰리면 503)",
        "gemini-3.5-flash-lite": "Gemini 3.5 Flash-Lite (가장 빠르고 가벼움)",
        "gemini-2.5-flash": "Gemini 2.5 Flash (신규 키는 404 — 기존 사용자만)",
    }
    GEMINI_MODEL_LABELS_REV = {v: k for k, v in GEMINI_MODEL_LABELS.items()}
    CLAUDE_MODEL_LABELS = {
        "claude-haiku-5-5": "Claude Haiku 5.5 (가장 싸고 빠름)",
        "claude-sonnet-5-5": "Claude Sonnet 5.5 (최신 Sonnet)",
        "claude-sonnet-5": "Claude Sonnet 5 (기본)",
        "claude-opus-5-5": "Claude Opus 5.5 (가장 똑똑함, 비쌈)",
    }
    CLAUDE_MODEL_LABELS_REV = {v: k for k, v in CLAUDE_MODEL_LABELS.items()}

    section(tab_ai, "AI 공급자", "파일 이름을 지을 AI 를 고릅니다. 고른 공급자의 키와 모델만 아래에 보입니다.", top=10)
    cmb_provider = ttk.Combobox(tab_ai, values=list(PROVIDER_LABELS.values()), state="readonly")
    current_provider = config.get("ai_provider", "gemini")
    if current_provider not in AI_PROVIDERS:
        current_provider = "gemini"
    cmb_provider.set(PROVIDER_LABELS[current_provider])
    cmb_provider.pack(fill="x")

    # 공급자별 칸 묶음 — 고른 것만 보이고, 나머지도 값은 그대로 들고 있다가 같이 저장된다.
    provider_host = tk.Frame(tab_ai, bg=bg_dark)
    provider_host.pack(fill="x")
    provider_frames = {}

    def provider_block(key):
        fr = tk.Frame(provider_host, bg=bg_dark)
        provider_frames[key] = fr
        return fr

    def field_label(parent, text):
        tk.Label(parent, text=text, fg=fg_white, bg=bg_dark, font=label_font).pack(anchor="w", pady=(14, 4))

    # Gemini
    fr = provider_block("gemini")
    field_label(fr, "Gemini API 키")
    row, ent_gemini_key = secret_entry(fr, config.get("gemini_api_key", ""))
    row.pack(fill="x")
    field_label(fr, "Gemini 모델")
    cmb_gemini_model = ttk.Combobox(fr, values=list(GEMINI_MODEL_LABELS.values()), state="readonly")
    current_gemini_model = config.get("gemini_model", DEFAULT_GEMINI_MODEL)
    if current_gemini_model not in GEMINI_MODEL_LABELS:
        current_gemini_model = DEFAULT_GEMINI_MODEL
    cmb_gemini_model.set(GEMINI_MODEL_LABELS[current_gemini_model])
    cmb_gemini_model.pack(fill="x")
    wrap_label(fr, "무료 한도는 모델별로 걸립니다. 막히면 다른 Gemini 모델로 자동 전환합니다.").pack(anchor="w", fill="x", pady=(4, 0))

    # OpenAI (모델은 코드에 고정)
    fr = provider_block("openai")
    field_label(fr, "OpenAI API 키")
    row, ent_openai_key = secret_entry(fr, config.get("openai_api_key", ""))
    row.pack(fill="x")
    wrap_label(fr, f"모델: {OPENAI_MODEL} (고정)").pack(anchor="w", fill="x", pady=(6, 0))

    # Claude
    fr = provider_block("claude")
    field_label(fr, "Claude API 키")
    row, ent_claude_key = secret_entry(fr, config.get("claude_api_key", ""))
    row.pack(fill="x")
    field_label(fr, "Claude 모델")
    cmb_claude_model = ttk.Combobox(fr, values=list(CLAUDE_MODEL_LABELS.values()), state="readonly")
    current_claude_model = config.get("claude_model", DEFAULT_CLAUDE_MODEL)
    if current_claude_model not in CLAUDE_MODEL_LABELS:
        current_claude_model = DEFAULT_CLAUDE_MODEL
    cmb_claude_model.set(CLAUDE_MODEL_LABELS[current_claude_model])
    cmb_claude_model.pack(fill="x")

    def show_provider_fields(_event=None):
        chosen = PROVIDER_LABELS_REV.get(cmb_provider.get(), "gemini")
        for key, frame in provider_frames.items():
            if key == chosen:
                frame.pack(fill="x")
            else:
                frame.pack_forget()

    cmb_provider.bind("<<ComboboxSelected>>", show_provider_fields)
    show_provider_fields()

    # 고급 설정 — 유료(결제 연동) 키. 무료 키가 한도에 걸리면 자동으로 이 키로 넘어간다.
    PAID_PROVIDER_SAME = "무료와 동일"
    PAID_PROVIDER_LABELS = {"": PAID_PROVIDER_SAME, **PROVIDER_LABELS}
    PAID_PROVIDER_LABELS_REV = {v: k for k, v in PAID_PROVIDER_LABELS.items()}

    advanced_open = {"v": False}
    btn_advanced = tk.Button(
        tab_ai, text="", bg=bg_dark, fg=sub_color, relief="flat", cursor="hand2", anchor="w",
        activebackground=bg_dark, activeforeground=fg_white, font=label_font, bd=0,
    )
    btn_advanced.pack(anchor="w", pady=(22, 0))
    advanced_frame = tk.Frame(tab_ai, bg=bg_dark)

    field_label(advanced_frame, "유료 공급자")
    cmb_paid_provider = ttk.Combobox(advanced_frame, values=list(PAID_PROVIDER_LABELS.values()), state="readonly")
    current_paid_provider = (config.get("paid_provider") or "").strip().lower()
    if current_paid_provider not in PAID_PROVIDER_LABELS:
        current_paid_provider = ""
    cmb_paid_provider.set(PAID_PROVIDER_LABELS[current_paid_provider])
    cmb_paid_provider.pack(fill="x")
    field_label(advanced_frame, "유료 API 키")
    row, ent_paid_key = secret_entry(advanced_frame, config.get("paid_api_key", ""))
    row.pack(fill="x")
    wrap_label(
        advanced_frame,
        "무료 키가 한도(429)에 걸리면 자동으로 이 키로 넘어갑니다. 비워두면 전환하지 않습니다. "
        "과금되는 키이며, 위에서 고른 유료 공급자의 키를 넣어야 합니다. "
        "Gemini 는 한도가 프로젝트 단위라 같은 프로젝트의 키는 소용없습니다 — 결제 연결된 다른 프로젝트의 키를 쓰세요.",
    ).pack(anchor="w", fill="x", pady=(6, 0))

    def toggle_advanced(force=None):
        advanced_open["v"] = (not advanced_open["v"]) if force is None else force
        if advanced_open["v"]:
            advanced_frame.pack(fill="x", after=btn_advanced)
            btn_advanced.configure(text="▾ 고급 설정 (유료 키)")
        else:
            advanced_frame.pack_forget()
            btn_advanced.configure(text="▸ 고급 설정 (유료 키)")

    btn_advanced.configure(command=toggle_advanced)
    toggle_advanced(force=False)

    # =========================================================================
    # 탭 3. 규칙 — 커스텀 지침, 기본 파일명 형식, 자동 이동 규칙
    # =========================================================================
    tab_rules = make_page("  규칙  ")

    section(
        tab_rules, "커스텀 지침",
        "특정 파일을 어떻게 이름 지을지 한글로 자유롭게 적으세요. 여기 적은 형식이 아래 기본 형식보다 항상 우선합니다.",
        top=10,
    )
    rules_frame = tk.Frame(tab_rules, bg=bg_dark)
    rules_frame.pack(fill="both", expand=True)
    txt_rules = tk.Text(
        rules_frame, bg=bg_entry, fg=fg_white, insertbackground=fg_white, font=body_font,
        wrap="word", relief="flat", highlightthickness=1, highlightbackground=bg_button,
        highlightcolor=fg_mint, height=14, width=1, padx=8, pady=6, undo=True,
    )
    # width=1: Text 기본 폭(80자)이 창보다 넓어 오른쪽이 잘리고 스크롤바가 밀려나던 것 방지(폭은 fill 로 채운다).
    # 스크롤바를 먼저 pack 해야 공간이 모자랄 때 스크롤바가 아니라 입력칸이 줄어든다.
    rules_scroll = tk.Scrollbar(rules_frame, orient="vertical", command=txt_rules.yview)
    rules_scroll.pack(side="right", fill="y")
    txt_rules.pack(side="left", fill="both", expand=True)
    txt_rules.configure(yscrollcommand=rules_scroll.set)

    # 실제 저장된 지침이 비어 있으면, 회색 예시 한 줄만 안내용으로 보여준다 (저장되는 값 아님)
    RULES_PLACEHOLDER = "예: 카드 사용내역 엑셀이면 카드 끝번호 4자리를 제목에 포함해줘"
    existing_rules = config.get("user_rules", "").strip()
    placeholder_state = {"active": not existing_rules}
    if existing_rules:
        txt_rules.insert("1.0", existing_rules)
    else:
        txt_rules.insert("1.0", RULES_PLACEHOLDER)
        txt_rules.configure(fg=sub_color)

    def on_rules_focus_in(event):
        if placeholder_state["active"]:
            txt_rules.delete("1.0", "end")
            txt_rules.configure(fg=fg_white)
            placeholder_state["active"] = False

    def on_rules_focus_out(event):
        if not txt_rules.get("1.0", "end-1c").strip():
            txt_rules.delete("1.0", "end")
            txt_rules.insert("1.0", RULES_PLACEHOLDER)
            txt_rules.configure(fg=sub_color)
            placeholder_state["active"] = True

    txt_rules.bind("<FocusIn>", on_rules_focus_in)
    txt_rules.bind("<FocusOut>", on_rules_focus_out)

    section(
        tab_rules, "기본 파일명 형식",
        "date(날짜) · summary(내용요약) · site(출처)를 원하는 순서로 적으세요. 예: date_summary_site — site 가 없으면 자동으로 빠집니다.",
    )
    entry_filename_format = flat_entry(tab_rules)
    entry_filename_format.pack(fill="x", ipady=3)
    entry_filename_format.insert(0, (config.get("filename_format") or "").strip() or DEFAULT_FILENAME_FORMAT)

    section(tab_rules, "자동 이동 규칙", "AI 가 파일이 조건에 맞다고 판단하면 지정한 폴더로 옮깁니다. 예: 카드 사용내역 엑셀 → D:\\카드정리")
    move_list_frame, lst_move_rules = make_listbox(tab_rules, height=5)
    move_list_frame.pack(fill="x")

    move_rules_state = [dict(r) for r in (config.get("move_rules") or [])]

    def refresh_move_rules_listbox():
        lst_move_rules.delete(0, "end")
        for r in move_rules_state:
            lst_move_rules.insert("end", f'{r.get("keyword", "")}  →  {r.get("folder", "")}')

    refresh_move_rules_listbox()

    def add_move_rule():
        dialog = tk.Toplevel(root)
        dialog.title("이동 규칙 추가")
        dialog.configure(bg=bg_dark)
        dialog.attributes("-topmost", True)
        dialog.resizable(False, False)
        dialog.transient(root)

        tk.Label(dialog, text="조건 설명 (예: 카드 사용내역 엑셀 파일)", fg=fg_white, bg=bg_dark, font=label_font).pack(anchor="w", padx=16, pady=(16, 4))
        ent_condition = flat_entry(dialog, width=46)
        ent_condition.pack(fill="x", padx=16, ipady=3)
        ent_condition.focus_set()

        tk.Label(dialog, text="이동할 폴더", fg=fg_white, bg=bg_dark, font=label_font).pack(anchor="w", padx=16, pady=(16, 4))
        dest_row = tk.Frame(dialog, bg=bg_dark)
        dest_row.pack(fill="x", padx=16)
        ent_dest = flat_entry(dest_row)
        ent_dest.pack(side="left", fill="x", expand=True, ipady=3)

        def browse_dest():
            selected = filedialog.askdirectory(parent=dialog)
            if selected:
                ent_dest.delete(0, "end")
                ent_dest.insert(0, selected)

        flat_button(dest_row, "찾기...", browse_dest).pack(side="left", padx=(6, 0))

        def confirm_add():
            cond = ent_condition.get().strip()
            dest = ent_dest.get().strip()
            if not cond or not dest:
                messagebox.showerror("입력 필요", "조건 설명과 폴더를 모두 입력하세요.", parent=dialog)
                return
            move_rules_state.append({"keyword": cond, "folder": dest})
            refresh_move_rules_listbox()
            dialog.destroy()

        btn_row = tk.Frame(dialog, bg=bg_dark)
        btn_row.pack(fill="x", padx=16, pady=20)
        flat_button(btn_row, "추가", confirm_add, primary=True).pack(side="left")
        flat_button(btn_row, "취소", dialog.destroy).pack(side="left", padx=(8, 0))

    def remove_selected_move_rule():
        for idx in reversed(lst_move_rules.curselection()):
            del move_rules_state[idx]
        refresh_move_rules_listbox()

    move_btn_row = tk.Frame(tab_rules, bg=bg_dark)
    move_btn_row.pack(fill="x", pady=(6, 0))
    flat_button(move_btn_row, "+ 규칙 추가", add_move_rule).pack(side="left")
    flat_button(move_btn_row, "선택 삭제", remove_selected_move_rule, fg=DANGER_FG).pack(side="left", padx=(6, 0))

    # =========================================================================
    # 저장
    # =========================================================================
    def on_save():
        sec_val = ent_sec.get().strip()
        if not sec_val.isdigit():
            nb.select(0)
            messagebox.showerror("데이터 오류", "카운트다운은 반드시 숫자(초)로 입력해야 합니다.", parent=root)
            return

        folders = [f for f in lst_folders.get(0, "end") if f.strip()]
        if not folders:
            nb.select(0)
            messagebox.showerror("데이터 오류", "감시 대상 폴더를 최소 1개 이상 추가해야 합니다.", parent=root)
            return

        rules_val = "" if placeholder_state["active"] else txt_rules.get("1.0", "end-1c").strip()

        # 화면에 없는 키(gemini_model_rotation 등)를 지우지 않도록 기존 설정 위에 덮어쓴다
        new_config = dict(config)
        new_config.update({
            "ai_provider": PROVIDER_LABELS_REV.get(cmb_provider.get(), "gemini"),
            "gemini_model": GEMINI_MODEL_LABELS_REV.get(cmb_gemini_model.get(), DEFAULT_GEMINI_MODEL),
            "gemini_api_key": ent_gemini_key.get().strip(),
            "openai_api_key": ent_openai_key.get().strip(),
            "claude_api_key": ent_claude_key.get().strip(),
            "claude_model": CLAUDE_MODEL_LABELS_REV.get(cmb_claude_model.get(), DEFAULT_CLAUDE_MODEL),
            "paid_api_key": ent_paid_key.get().strip(),
            "paid_provider": PAID_PROVIDER_LABELS_REV.get(cmb_paid_provider.get(), ""),
            "watch_folders": folders,
            "countdown_seconds": int(sec_val),
            "max_output_tokens": config.get("max_output_tokens", 1500),
            "max_preview_chars": config.get("max_preview_chars", 2000),
            "user_rules": rules_val,
            "move_rules": move_rules_state,
            "filename_format": entry_filename_format.get().strip() or DEFAULT_FILENAME_FORMAT,
        })

        if save_config(new_config):
            messagebox.showinfo("설정 적용 완료", "설정이 저장되었습니다.\n실행 중인 감시 엔진에 바로 적용됩니다.", parent=root)
            on_close()
        else:
            messagebox.showerror("저장 실패", "설정 파일 작성에 실패했습니다.", parent=root)

    bottom_inner = tk.Frame(bottom, bg=bg_entry)
    bottom_inner.pack(fill="x", padx=24, pady=10)
    flat_button(bottom_inner, "저장", on_save, primary=True).pack(side="right")
    flat_button(bottom_inner, "취소", on_close).pack(side="right", padx=(0, 8))
    root.bind("<Control-s>", lambda e: on_save())

    root.mainloop()


def main():
    if not watcher.start_background_threads():
        return

    # 최초 실행(또는 선택된 공급자의 API 키 미설정 상태)이면 설정 창을 자동으로 띄워 안내
    try:
        from file_namer import load_config
        cfg = load_config()
        provider = cfg.get("ai_provider", "gemini")
        key_field = {"gemini": "gemini_api_key", "openai": "openai_api_key", "claude": "claude_api_key"}.get(provider, "gemini_api_key")
        if not cfg.get(key_field, "").strip():
            watcher.show_toast(
                "API 키 설정이 필요합니다",
                "잠시 후 설정 창이 열립니다. 선택된 AI 공급자의 API 키를 입력해야 자동 정리가 동작합니다.",
                accent="#ff8585",
            )
            if getattr(sys, 'frozen', False):
                subprocess.Popen([sys.executable, "--settings"])
            else:
                subprocess.Popen([sys.executable, __file__, "--settings"])
    except Exception as e:
        watcher.log(f"[최초 실행 안내 실패] {e}")

    icon_holder = {}

    def on_cleanup(icon, item):
        """7일 지난 파일 분류 정리 실행"""
        def _run():
            watcher.log("[정리 시작] 7일 이상 된 파일 분류 중...")
            moved, failed = watcher.run_cleanup()
            if moved == 0 and failed == 0:
                watcher.show_toast("정리 완료", "7일 이상 된 파일이 없습니다.", accent="#6ee7b7")
            elif failed == 0:
                watcher.show_toast("정리 완료", f"{moved}개 파일을 분류했습니다.", accent="#6ee7b7")
            else:
                watcher.show_toast("정리 완료", f"{moved}개 이동 / {failed}개 실패", accent="#ff8585")
        threading.Thread(target=_run, daemon=True).start()

    def on_toggle_pause(icon, item):
        if watcher.paused.is_set():
            watcher.paused.clear()
            watcher.log("[감시 재개]")
        else:
            watcher.paused.set()
            watcher.log("[감시 일시정지]")
        refresh_menu()

    def on_open_log(icon, item):
        open_log_file()

    def on_undo(icon, item):
        """실행취소 트레이 리시버 인터페이스"""
        success, message = watcher.undo_last_rename()
        if success:
            watcher.show_toast("이름 복원 완료", message, accent="#6ee7b7")
        else:
            watcher.show_toast("실행 취소 불가", message, accent="#ff8585")

    def on_retry_failed(icon, item):
        """AI 분석 실패로 기록된 파일들을 한번에 재시도"""
        watcher.retry_failed_files()

    def on_open_settings(icon, item):
        """환경 설정 및 추가 지침 편집기를 독립 서브프로세스로 구동하여 안정성 극대화"""
        try:
            if getattr(sys, 'frozen', False):
                # PyInstaller 패키징 환경
                subprocess.Popen([sys.executable, "--settings"])
            else:
                # 일반 파이썬 실행 환경
                subprocess.Popen([sys.executable, __file__, "--settings"])
        except Exception as e:
            watcher.log(f"[설정 창 실행 실패] {e}")

    def on_quit(icon, item):
        watcher.log("[프로그램 종료]")
        icon.stop()

    def on_check_update(icon, item):
        def _exit_for_install():
            # 설치 프로그램이 exe 를 덮어쓸 수 있게 스스로 비켜 준다(설치 후 자동 재실행됨).
            watcher.log("[업데이트 설치를 위해 종료]")
            icon.stop()
            os._exit(0)
        threading.Thread(target=run_update_check, args=(_exit_for_install,), daemon=True).start()

    def pause_label_text(item=None):
        return "감시 재개" if watcher.paused.is_set() else "감시 일시정지"

    def build_menu():
        return pystray.Menu(
            pystray.MenuItem(pause_label_text, on_toggle_pause),
            pystray.MenuItem("이름변경 실행 취소", on_undo),
            pystray.MenuItem("에러 파일 재시도", on_retry_failed),
            pystray.MenuItem("지금 정리하기 (7일↑)", on_cleanup),
            pystray.MenuItem("환경 설정 및 추가 지침", on_open_settings),
            pystray.MenuItem("로그 보기", on_open_log),
            pystray.MenuItem(f"업데이트 확인 (현재 v{updater.APP_VERSION})", on_check_update),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", on_quit),
        )

    def refresh_menu():
        icon = icon_holder.get("icon")
        if icon:
            icon.icon = create_paused_icon_image() if watcher.paused.is_set() else create_icon_image()
            icon.menu = build_menu()
            icon.update_menu()

    icon = pystray.Icon(
        "rename_watcher",
        icon=create_icon_image(),
        title="AutoRename",
        menu=build_menu(),
    )
    icon_holder["icon"] = icon

    watcher.log("[트레이 아이콘 시작]")
    icon.run()


if __name__ == "__main__":
    # 실행 인자 확인을 통해 설정 창과 트레이 아이콘 분할 기동
    if len(sys.argv) > 1 and sys.argv[1] == "--settings":
        show_settings_gui()
    else:
        main()
