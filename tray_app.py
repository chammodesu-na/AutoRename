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


def show_settings_gui():
    """독립된 프로세스로 실행되어 스레드 락 없이 안전하게 구동되는 환경설정 GUI 창"""
    from file_namer import (
        load_config, save_config, DEFAULT_FILENAME_FORMAT, AI_PROVIDERS, DEFAULT_GEMINI_MODEL,
    )

    config = load_config()

    root = tk.Tk()
    root.title("AutoRename 설정")
    root.configure(bg="#1e1e22")
    root.attributes("-topmost", True)

    # ---- 화면 크기에 맞춰 창 크기/위치를 동적으로 결정 ----
    # 고정 630px 높이가 작은 화면(특히 노트북)에서 작업표시줄까지 빼면
    # 화면 밖으로 밀려나 저장 버튼이 안 보이는 문제가 있었음 -> 화면 크기 기준으로 제한.
    desired_w, desired_h = 540, 630
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()

    win_w = min(desired_w, sw - 80)
    win_h = min(desired_h, sh - 120)  # 작업표시줄/타이틀바 여유 확보
    win_w = max(win_w, 420)   # 너무 작아지지 않도록 최소값 보장
    win_h = max(win_h, 360)

    x = max((sw - win_w) // 2, 0)
    y = max((sh - win_h) // 2, 0)
    root.geometry(f"{win_w}x{win_h}+{x}+{y}")

    # 창 크기 조절 허용 (이전에는 False로 막혀 있어서 화면보다 큰 창을 줄일 방법이 없었음)
    root.resizable(True, True)
    root.minsize(420, 360)

    # 스타일 정의
    fg_mint = "#6ee7b7"
    bg_dark = "#1e1e22"
    bg_entry = "#2a2a2f"
    fg_white = "#f2f2f2"
    sub_color = "#9a9aa2"
    DANGER_FG = "#ff8585"

    header_font = ("Malgun Gothic", 15, "bold")
    label_font = ("Malgun Gothic", 10, "bold")
    desc_font = ("Malgun Gothic", 8)

    # ---- 스크롤 가능한 컨테이너 ----
    # 창이 화면보다 작게 강제돼도 내용이 넘치면 마우스 휠로 끝까지 볼 수 있게 함.
    outer_canvas = tk.Canvas(root, bg=bg_dark, highlightthickness=0)
    outer_canvas.pack(side="left", fill="both", expand=True)

    scrollbar = tk.Scrollbar(root, orient="vertical", command=outer_canvas.yview)
    scrollbar.pack(side="right", fill="y")
    outer_canvas.configure(yscrollcommand=scrollbar.set)

    content = tk.Frame(outer_canvas, bg=bg_dark)
    content_window = outer_canvas.create_window((0, 0), window=content, anchor="nw")

    def on_content_configure(event):
        outer_canvas.configure(scrollregion=outer_canvas.bbox("all"))

    def on_canvas_configure(event):
        # 컨텐츠 프레임 너비를 캔버스 너비에 맞춰서 가로로 꽉 차게 유지
        outer_canvas.itemconfig(content_window, width=event.width)

    content.bind("<Configure>", on_content_configure)
    outer_canvas.bind("<Configure>", on_canvas_configure)

    def on_mousewheel(event):
        outer_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    outer_canvas.bind_all("<MouseWheel>", on_mousewheel)

    def on_close():
        outer_canvas.unbind_all("<MouseWheel>")
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    # 이제부터 위젯은 root가 아니라 content 위에 배치한다.

    # 제목 헤더
    lbl_title = tk.Label(content, text="Download Rename Watcher 설정", fg=fg_mint, bg=bg_dark, font=header_font)
    lbl_title.pack(pady=(20, 15))

    # 설정 필드 배치
    fields_frame = tk.Frame(content, bg=bg_dark)
    fields_frame.pack(fill="x", padx=30)
    fields_frame.grid_columnconfigure(1, weight=1)

    # ── 적응형 안내문 ────────────────────────────────────────────────────────
    # tk.Label은 wraplength가 0이면 줄바꿈을 아예 하지 않아서, 창보다 긴 안내문이 통째로 잘려 나간다.
    # 게다가 기본 anchor가 center라 오른쪽뿐 아니라 왼쪽까지 잘린다(문장 앞부분이 사라진다).
    # 그래서 창 크기가 바뀔 때마다 각 라벨이 실제로 쓸 수 있는 폭을 재서 wraplength를 다시 잡아준다.
    _adaptive_labels = []

    def register_adaptive(label, min_width=200):
        """이 라벨을 창 폭에 맞춰 자동 줄바꿈 대상으로 등록한다."""
        label.configure(justify="left", anchor="w")
        _adaptive_labels.append((label, min_width))
        return label

    def relayout_adaptive(event=None):
        total = content.winfo_width()
        if total <= 1:  # 아직 배치 전이면 폭을 잴 수 없다
            return
        for label, min_width in _adaptive_labels:
            try:
                # 라벨이 화면에서 실제로 시작하는 x 위치만큼은 쓸 수 없는 폭이다
                indent = label.winfo_rootx() - content.winfo_rootx()
            except Exception:
                indent = 0
            width = max(min_width, total - indent - 24)
            # 같은 값을 다시 넣으면 <Configure>가 또 돌아 무한 반복이 되므로 바뀔 때만 적용한다
            if label.cget("wraplength") != width:
                label.configure(wraplength=width)

    # 스크롤 영역 갱신 핸들러가 이미 걸려 있으므로 add="+" 로 덧붙인다(안 그러면 기존 핸들러가 지워진다)
    content.bind("<Configure>", relayout_adaptive, add="+")

    # AI 공급자 선택 (사람마다 원하는 AI로 바꿔 쓸 수 있게 함)
    PROVIDER_LABELS = {"gemini": "Google Gemini", "openai": "OpenAI (GPT)", "claude": "Anthropic (Claude)"}
    PROVIDER_LABELS_REV = {v: k for k, v in PROVIDER_LABELS.items()}

    tk.Label(fields_frame, text="AI 공급자 :", fg=fg_white, bg=bg_dark, font=label_font).grid(row=0, column=0, sticky="w", pady=6)
    style = ttk.Style()
    style.theme_use(style.theme_use())
    style.configure("Provider.TCombobox", fieldbackground=bg_entry, background=bg_entry, foreground=fg_white)
    cmb_provider = ttk.Combobox(
        fields_frame, values=list(PROVIDER_LABELS.values()), state="readonly",
        style="Provider.TCombobox",
    )
    current_provider = config.get("ai_provider", "gemini")
    if current_provider not in AI_PROVIDERS:
        current_provider = "gemini"
    cmb_provider.set(PROVIDER_LABELS[current_provider])
    cmb_provider.grid(row=0, column=1, sticky="ew", padx=10, pady=6)

    # 공급자별 API 키 (전부 입력해두고 위에서 고른 공급자의 키만 실제로 사용됨)
    tk.Label(fields_frame, text="Gemini API Key :", fg=fg_white, bg=bg_dark, font=label_font).grid(row=1, column=0, sticky="w", pady=6)
    ent_gemini_key = tk.Entry(fields_frame, bg=bg_entry, fg=fg_white, insertbackground=fg_white, relief="flat")
    ent_gemini_key.grid(row=1, column=1, sticky="ew", padx=10, pady=6)
    ent_gemini_key.insert(0, config.get("gemini_api_key", ""))

    # Gemini 모델 버전 선택 (3.5는 최신이지만 수요 급증 시 503 오류 잦음 / 2.5는 안정적이나 2026-10-16 전체 종료 예정)
    GEMINI_MODEL_LABELS = {
        "gemini-3.5-flash": "Gemini 3.5 Flash (기본, 가끔 503 과부하 오류)",
        "gemini-2.5-flash": "Gemini 2.5 Flash (2026-10-16 종료 예정, 신규 키는 404)",
    }
    GEMINI_MODEL_LABELS_REV = {v: k for k, v in GEMINI_MODEL_LABELS.items()}
    tk.Label(fields_frame, text="Gemini 모델 :", fg=fg_white, bg=bg_dark, font=label_font).grid(row=2, column=0, sticky="w", pady=6)
    style.configure("GeminiModel.TCombobox", fieldbackground=bg_entry, background=bg_entry, foreground=fg_white)
    cmb_gemini_model = ttk.Combobox(
        fields_frame, values=list(GEMINI_MODEL_LABELS.values()), state="readonly",
        style="GeminiModel.TCombobox",
    )
    current_gemini_model = config.get("gemini_model", DEFAULT_GEMINI_MODEL)
    if current_gemini_model not in GEMINI_MODEL_LABELS:
        current_gemini_model = DEFAULT_GEMINI_MODEL
    cmb_gemini_model.set(GEMINI_MODEL_LABELS[current_gemini_model])
    cmb_gemini_model.grid(row=2, column=1, sticky="ew", padx=10, pady=6)

    tk.Label(fields_frame, text="OpenAI API Key :", fg=fg_white, bg=bg_dark, font=label_font).grid(row=3, column=0, sticky="w", pady=6)
    ent_openai_key = tk.Entry(fields_frame, bg=bg_entry, fg=fg_white, insertbackground=fg_white, relief="flat")
    ent_openai_key.grid(row=3, column=1, sticky="ew", padx=10, pady=6)
    ent_openai_key.insert(0, config.get("openai_api_key", ""))

    tk.Label(fields_frame, text="Claude API Key :", fg=fg_white, bg=bg_dark, font=label_font).grid(row=4, column=0, sticky="w", pady=6)
    ent_claude_key = tk.Entry(fields_frame, bg=bg_entry, fg=fg_white, insertbackground=fg_white, relief="flat")
    ent_claude_key.grid(row=4, column=1, sticky="ew", padx=10, pady=6)
    ent_claude_key.insert(0, config.get("claude_api_key", ""))

    # 유료(결제 연동) 키 — 무료 키가 한도에 걸리면 자동으로 이 키로 넘어간다. 비워두면 전환 없음.
    # 공급자는 따로 고를 수 있고, "무료와 동일"이면 빈 문자열로 저장돼 위 ai_provider를 그대로 따른다.
    PAID_PROVIDER_SAME = "무료와 동일"
    PAID_PROVIDER_LABELS = {"": PAID_PROVIDER_SAME, **PROVIDER_LABELS}
    PAID_PROVIDER_LABELS_REV = {v: k for k, v in PAID_PROVIDER_LABELS.items()}

    tk.Label(fields_frame, text="유료 공급자 :", fg=fg_white, bg=bg_dark, font=label_font).grid(row=5, column=0, sticky="w", pady=6)
    style.configure("PaidProvider.TCombobox", fieldbackground=bg_entry, background=bg_entry, foreground=fg_white)
    cmb_paid_provider = ttk.Combobox(
        fields_frame, values=list(PAID_PROVIDER_LABELS.values()), state="readonly",
        style="PaidProvider.TCombobox",
    )
    current_paid_provider = (config.get("paid_provider") or "").strip().lower()
    if current_paid_provider not in PAID_PROVIDER_LABELS:
        current_paid_provider = ""
    cmb_paid_provider.set(PAID_PROVIDER_LABELS[current_paid_provider])
    cmb_paid_provider.grid(row=5, column=1, sticky="ew", padx=10, pady=6)

    tk.Label(fields_frame, text="유료 API Key :", fg=fg_white, bg=bg_dark, font=label_font).grid(row=6, column=0, sticky="w", pady=6)
    ent_paid_key = tk.Entry(fields_frame, bg=bg_entry, fg=fg_white, insertbackground=fg_white, relief="flat")
    ent_paid_key.grid(row=6, column=1, sticky="ew", padx=10, pady=6)
    ent_paid_key.insert(0, config.get("paid_api_key", ""))
    lbl_paid_desc = tk.Label(
        fields_frame,
        text="※ 무료 키가 한도(429)에 걸리면 자동으로 이 키로 넘어갑니다. 비워두면 전환하지 않습니다.\n"
             "과금되는 키이며, 위에서 고른 유료 공급자의 키를 넣어야 합니다.\n"
             "Gemini는 한도가 프로젝트 단위라 같은 프로젝트의 키는 소용없습니다 — 결제 연결된 다른 프로젝트의 키를 쓰세요.",
        fg="#9aa0a6", bg=bg_dark, font=("Malgun Gothic", 8),
    )
    register_adaptive(lbl_paid_desc)
    lbl_paid_desc.grid(row=7, column=1, sticky="ew", padx=10)

    # 지연 타이머 (초)
    tk.Label(fields_frame, text="카운트다운(초) :", fg=fg_white, bg=bg_dark, font=label_font).grid(row=8, column=0, sticky="w", pady=6)
    ent_sec = tk.Entry(fields_frame, bg=bg_entry, fg=fg_white, insertbackground=fg_white, relief="flat", width=10)
    ent_sec.grid(row=8, column=1, sticky="w", padx=10, pady=6)
    ent_sec.insert(0, str(config.get("countdown_seconds", 3)))

    # 감시 대상 폴더 (여러 개)
    lbl_folders = tk.Label(content, text="감시 대상 폴더 (여러 개 가능) :", fg=fg_mint, bg=bg_dark, font=label_font)
    lbl_folders.pack(anchor="w", padx=30, pady=(20, 5))

    folders_frame = tk.Frame(content, bg=bg_dark)
    folders_frame.pack(fill="x", padx=30)

    list_frame = tk.Frame(folders_frame, bg=bg_dark)
    list_frame.pack(fill="x")

    lst_folders = tk.Listbox(
        list_frame, bg=bg_entry, fg=fg_white, relief="flat",
        selectbackground=fg_mint, selectforeground=bg_dark,
        highlightthickness=1, highlightbackground="#3a3a40",
        height=5, activestyle="none",
    )
    lst_folders.pack(side="left", fill="both", expand=True)
    lst_scroll = tk.Scrollbar(list_frame, orient="vertical", command=lst_folders.yview)
    lst_scroll.pack(side="right", fill="y")
    lst_folders.configure(yscrollcommand=lst_scroll.set)

    existing_folders = config.get("watch_folders")
    if not existing_folders:
        legacy = config.get("watch_folder")
        existing_folders = [legacy] if legacy else []
    for f in existing_folders:
        lst_folders.insert("end", f)

    folder_btn_row = tk.Frame(folders_frame, bg=bg_dark)
    folder_btn_row.pack(fill="x", pady=(6, 0))

    def add_folder():
        selected = filedialog.askdirectory()
        if selected:
            current = list(lst_folders.get(0, "end"))
            if selected not in current:
                lst_folders.insert("end", selected)

    def remove_selected_folder():
        sel = lst_folders.curselection()
        for idx in reversed(sel):
            lst_folders.delete(idx)

    btn_add_folder = tk.Button(folder_btn_row, text="+ 폴더 추가", command=add_folder, bg="#3a3a40", fg=fg_white, relief="flat", activebackground="#2a2a2f", activeforeground=fg_white)
    btn_add_folder.pack(side="left")

    btn_remove_folder = tk.Button(folder_btn_row, text="선택 삭제", command=remove_selected_folder, bg="#3a3a40", fg=DANGER_FG, relief="flat", activebackground="#2a2a2f", activeforeground=DANGER_FG)
    btn_remove_folder.pack(side="left", padx=(6, 0))

    # 추가 지침 프롬프트 레이블
    lbl_rules = tk.Label(content, text="AI 추가 설정 및 커스텀 프롬프트 지침 :", fg=fg_mint, bg=bg_dark, font=label_font)
    lbl_rules.pack(anchor="w", padx=30, pady=(20, 5))

    lbl_rules_desc = tk.Label(content, text="※ 수출신고필증, 계약서 등 특정 파일에 대한 분류 가이드를 한글로 자유롭게 기술하세요.", fg=sub_color, bg=bg_dark, font=desc_font)
    register_adaptive(lbl_rules_desc)
    lbl_rules_desc.pack(anchor="w", fill="x", padx=30, pady=(0, 5))

    # 텍스트 영역 (높이를 고정 줄 수로 지정해 작은 화면에서도 항상 일정 부분 보이게 함) + 스크롤바
    rules_frame = tk.Frame(content, bg=bg_dark)
    rules_frame.pack(fill="both", expand=True, padx=30, pady=5)

    txt_rules = tk.Text(rules_frame, bg=bg_entry, fg=fg_white, insertbackground=fg_white, font=("Malgun Gothic", 10), wrap="word", relief="flat", highlightthickness=1, highlightbackground="#3a3a40", height=8)
    txt_rules.pack(side="left", fill="both", expand=True)

    rules_scroll = tk.Scrollbar(rules_frame, orient="vertical", command=txt_rules.yview)
    rules_scroll.pack(side="right", fill="y")
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

    # 기본 파일명 형식 (위 커스텀 프롬프트가 특정 케이스 형식을 지정하면 그쪽이 항상 우선 적용됨)
    lbl_filename_format = tk.Label(content, text="기본 파일명 형식 :", fg=fg_mint, bg=bg_dark, font=label_font)
    lbl_filename_format.pack(anchor="w", padx=30, pady=(20, 5))

    lbl_filename_format_desc = tk.Label(
        content,
        text="※ date(날짜) summary(내용요약) site(사이트명)를 원하는 순서로 적으세요. 중괄호는 없어도 됩니다. (예: date_summary_site) site가 없으면 자동으로 빠집니다. 전신문처럼 형식이 다른 케이스는 위 커스텀 프롬프트에 직접 적어두면 그게 최우선 적용됨",
        fg=sub_color, bg=bg_dark, font=desc_font,
    )
    register_adaptive(lbl_filename_format_desc)
    lbl_filename_format_desc.pack(anchor="w", fill="x", padx=30, pady=(0, 5))

    entry_filename_format = tk.Entry(content, bg=bg_entry, fg=fg_white, insertbackground=fg_white, font=("Malgun Gothic", 10), relief="flat", highlightthickness=1, highlightbackground="#3a3a40")
    entry_filename_format.pack(fill="x", padx=30, pady=(0, 5))
    entry_filename_format.insert(0, (config.get("filename_format") or "").strip() or DEFAULT_FILENAME_FORMAT)

    # 자동 이동 규칙 (조건에 맞으면 지정한 폴더로 자동 이동)
    lbl_move_rules = tk.Label(content, text="자동 이동 규칙 (조건에 맞으면 폴더로 이동) :", fg=fg_mint, bg=bg_dark, font=label_font)
    lbl_move_rules.pack(anchor="w", padx=30, pady=(20, 5))

    lbl_move_rules_desc = tk.Label(
        content,
        text="※ 예: '카드 사용내역 엑셀' → D:\\카드정리 폴더. AI가 파일이 조건에 맞다고 판단하면 자동으로 이동시킵니다.",
        fg=sub_color, bg=bg_dark, font=desc_font,
    )
    register_adaptive(lbl_move_rules_desc)
    lbl_move_rules_desc.pack(anchor="w", fill="x", padx=30, pady=(0, 5))

    move_rules_frame = tk.Frame(content, bg=bg_dark)
    move_rules_frame.pack(fill="x", padx=30)

    move_rules_list_frame = tk.Frame(move_rules_frame, bg=bg_dark)
    move_rules_list_frame.pack(fill="x")

    lst_move_rules = tk.Listbox(
        move_rules_list_frame, bg=bg_entry, fg=fg_white, relief="flat",
        selectbackground=fg_mint, selectforeground=bg_dark,
        highlightthickness=1, highlightbackground="#3a3a40",
        height=5, activestyle="none",
    )
    lst_move_rules.pack(side="left", fill="both", expand=True)
    move_rules_scroll = tk.Scrollbar(move_rules_list_frame, orient="vertical", command=lst_move_rules.yview)
    move_rules_scroll.pack(side="right", fill="y")
    lst_move_rules.configure(yscrollcommand=move_rules_scroll.set)

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

        tk.Label(dialog, text="조건 설명 (예: 카드 사용내역 엑셀 파일)", fg=fg_white, bg=bg_dark, font=label_font).pack(anchor="w", padx=16, pady=(16, 4))
        ent_condition = tk.Entry(dialog, bg=bg_entry, fg=fg_white, insertbackground=fg_white, relief="flat", width=46)
        ent_condition.pack(fill="x", padx=16)
        ent_condition.focus_set()

        tk.Label(dialog, text="이동할 폴더", fg=fg_white, bg=bg_dark, font=label_font).pack(anchor="w", padx=16, pady=(16, 4))
        dest_row = tk.Frame(dialog, bg=bg_dark)
        dest_row.pack(fill="x", padx=16)
        ent_dest = tk.Entry(dest_row, bg=bg_entry, fg=fg_white, insertbackground=fg_white, relief="flat")
        ent_dest.pack(side="left", fill="x", expand=True)

        def browse_dest():
            selected = filedialog.askdirectory(parent=dialog)
            if selected:
                ent_dest.delete(0, "end")
                ent_dest.insert(0, selected)

        tk.Button(dest_row, text="찾기...", command=browse_dest, bg="#3a3a40", fg=fg_white, relief="flat", activebackground="#2a2a2f", activeforeground=fg_white).pack(side="left", padx=(6, 0))

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
        tk.Button(btn_row, text="추가", command=confirm_add, bg=fg_mint, fg=bg_dark, font=("Malgun Gothic", 10, "bold"), relief="flat", cursor="hand2").pack(side="left")
        tk.Button(btn_row, text="취소", command=dialog.destroy, bg="#3a3a40", fg=fg_white, relief="flat", activebackground="#2a2a2f", activeforeground=fg_white).pack(side="left", padx=(8, 0))

    def remove_selected_move_rule():
        sel = lst_move_rules.curselection()
        for idx in reversed(sel):
            del move_rules_state[idx]
        refresh_move_rules_listbox()

    move_rules_btn_row = tk.Frame(move_rules_frame, bg=bg_dark)
    move_rules_btn_row.pack(fill="x", pady=(6, 0))

    btn_add_move_rule = tk.Button(move_rules_btn_row, text="+ 규칙 추가", command=add_move_rule, bg="#3a3a40", fg=fg_white, relief="flat", activebackground="#2a2a2f", activeforeground=fg_white)
    btn_add_move_rule.pack(side="left")

    btn_remove_move_rule = tk.Button(move_rules_btn_row, text="선택 삭제", command=remove_selected_move_rule, bg="#3a3a40", fg=DANGER_FG, relief="flat", activebackground="#2a2a2f", activeforeground=DANGER_FG)
    btn_remove_move_rule.pack(side="left", padx=(6, 0))

    # 저장 프로세스
    def on_save():
        sec_val = ent_sec.get().strip()
        if not sec_val.isdigit():
            messagebox.showerror("데이터 오류", "카운트다운은 반드시 숫자형(초)으로 입력해야 합니다.")
            return

        folders = [f for f in lst_folders.get(0, "end") if f.strip()]
        if not folders:
            messagebox.showerror("데이터 오류", "감시 대상 폴더를 최소 1개 이상 추가해야 합니다.")
            return

        rules_val = "" if placeholder_state["active"] else txt_rules.get("1.0", "end-1c").strip()
        filename_format_val = entry_filename_format.get().strip() or DEFAULT_FILENAME_FORMAT
        provider_val = PROVIDER_LABELS_REV.get(cmb_provider.get(), "gemini")
        gemini_model_val = GEMINI_MODEL_LABELS_REV.get(cmb_gemini_model.get(), DEFAULT_GEMINI_MODEL)

        new_config = {
            "ai_provider": provider_val,
            "gemini_model": gemini_model_val,
            "gemini_api_key": ent_gemini_key.get().strip(),
            "openai_api_key": ent_openai_key.get().strip(),
            "claude_api_key": ent_claude_key.get().strip(),
            "paid_api_key": ent_paid_key.get().strip(),
            "paid_provider": PAID_PROVIDER_LABELS_REV.get(cmb_paid_provider.get(), ""),
            "watch_folders": folders,
            "countdown_seconds": int(sec_val),
            "max_output_tokens": config.get("max_output_tokens", 1500),
            "max_preview_chars": config.get("max_preview_chars", 2000),
            "user_rules": rules_val,
            "move_rules": move_rules_state,
            "filename_format": filename_format_val,
        }

        if save_config(new_config):
            messagebox.showinfo("설정 적용 완료", "설정이 안전하게 저장되었습니다!\n실행 중인 감시 엔진에 실시간 적용됩니다.")
            on_close()
        else:
            messagebox.showerror("저장 실패", "설정 파일 작성에 실패했습니다.")

    btn_save = tk.Button(
        content, text="설정 및 추가 지침 저장하기", command=on_save,
        bg=fg_mint, fg=bg_dark, font=("Malgun Gothic", 10, "bold"),
        relief="flat", cursor="hand2", padx=20, pady=8,
        activebackground="#58d0a3", activeforeground=bg_dark
    )
    btn_save.pack(pady=20)

    # 위젯 배치가 끝나야 폭을 잴 수 있으므로, 창이 그려진 직후 한 번 접어준다.
    # (이후로는 창 크기가 바뀔 때마다 <Configure>가 알아서 다시 접는다)
    root.after(120, relayout_adaptive)

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
