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
    """독립된 프로세스로 실행되는 환경설정 창 (2.0.0 — CustomTkinter, Claude 톤 · iOS 설정 앱 스타일).

    - 탭: 일반(감시 폴더·카운트다운) / AI(1·2·3순위) / 규칙(이름 짓는 지침·파일명 형식·자동 이동)
    - 윈도우가 다크 모드면 다크로 그린다(set_appearance_mode("system")).
    - 저장할 때 화면에 없는 설정 키도 지우지 않고, 구버전(1.3.x)이 읽는 옛 칸도 같이 채운다.
    시안: Claude 디자인 캔버스 「AutoRename 설정 창 시안」(2026-10-14).
    """
    import re
    import datetime
    import tkinter.font as tkfont
    import customtkinter as ctk
    from file_namer import (
        load_config, save_config, DEFAULT_FILENAME_FORMAT, DEFAULT_GEMINI_MODEL, DEFAULT_CLAUDE_MODEL,
        OPENAI_MODEL, AI_CHAIN_MAX, DATE_FORMATS, DEFAULT_DATE_FORMAT, get_ai_chain, chain_to_legacy,
        compose_user_rules, build_filename_from_format, guess_provider, default_model_for,
        refine_text, RefineError,
    )

    config = load_config()

    # 화면 모드: 설정값(appearance_mode) — system 이면 윈도우 다크/라이트 설정을 따른다
    APPEARANCE_UI = {"system": "시스템", "light": "라이트", "dark": "다크"}
    APPEARANCE_UI_REV = {v: k for k, v in APPEARANCE_UI.items()}
    appearance_state = {"v": config.get("appearance_mode") if config.get("appearance_mode") in APPEARANCE_UI else "system"}
    ctk.set_appearance_mode(appearance_state["v"])
    root = ctk.CTk()
    root.title("AutoRename 설정")
    root.attributes("-topmost", True)

    # ---- 색 (밝은, 어두운) ----
    BG = ("#F5F4EE", "#1F1E1D")
    CARD = ("#FFFFFF", "#2A2926")
    BORDER = ("#E6E3D8", "#3A3935")
    TEXT = ("#1F1E1D", "#F2F0E8")
    SUB = ("#6B6962", "#A8A59C")
    FIELD = ("#F7F6F1", "#232220")
    SEG = ("#EAE7DD", "#33322E")
    SEG_SEL = ("#FFFFFF", "#4A4843")
    SEG_HOVER = ("#E0DCCF", "#3D3C37")
    ACCENT = ("#B85A38", "#C96A47")
    ACCENT_H = ("#9E4A2C", "#B85A38")
    BADGE_BG = ("#F6E5DC", "#4A2E24")
    BADGE_TX = ("#8F4426", "#F2B9A3")
    WARN_BG, WARN_BD, WARN_TX = ("#FBF5E4", "#332C1C"), ("#EEDFB3", "#4D4122"), ("#7A5A0B", "#E8C46A")
    ERR_BG, ERR_BD, ERR_TX = ("#FBECEA", "#3A2222"), ("#F1CFCB", "#5A2E2E"), ("#9E2F25", "#F29B9B")
    SUG_BG, SUG_BD = ("#FBF1EB", "#33261F"), ("#F0DACD", "#4A2E24")
    THUMB = ("#CFCABB", "#55534D")
    root.configure(fg_color=BG)

    # ---- 글꼴: Pretendard 가 깔려 있으면 그것, 없으면 맑은 고딕 ----
    families = set(tkfont.families(root))
    FAMILY = "Pretendard" if "Pretendard" in families else "Malgun Gothic"

    def F(size=13, bold=False):
        return ctk.CTkFont(family=FAMILY, size=size, weight="bold" if bold else "normal")

    # ---- 창 크기 ----
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    win_w = max(min(580, sw - 80), 460)
    win_h = max(min(780, sh - 100), 520)
    root.geometry(f"{win_w}x{win_h}+{max((sw - win_w) // 2, 0)}+{max((sh - win_h) // 2, 0)}")
    root.minsize(460, 520)

    # CTk 는 창 아이콘을 자기 로고로 바꾸므로 그 뒤에 앱 아이콘으로 덮는다
    def _set_icon():
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        ico = os.path.join(base, "app_icon.ico")
        if os.path.exists(ico):
            try:
                root.iconbitmap(ico)
            except Exception:
                pass
    root.after(300, _set_icon)

    # 창 폭에 맞춰 줄바꿈되는 안내문들
    wrap_labels = []

    def on_root_resize(_e=None):
        width = max(root.winfo_width() - 130, 240)
        for lbl, extra in wrap_labels:
            try:
                lbl.configure(wraplength=width - extra)
            except Exception:
                pass

    root.bind("<Configure>", lambda e: on_root_resize() if e.widget is root else None, add="+")

    # ---- 공통 위젯 ----
    def label(parent, text, size=13, bold=False, color=TEXT, wrap_extra=None, **kw):
        lbl = ctk.CTkLabel(parent, text=text, font=F(size, bold), text_color=color, anchor="w", justify="left", **kw)
        if wrap_extra is not None:
            wrap_labels.append((lbl, wrap_extra))
            lbl.configure(wraplength=max(win_w - 130 - wrap_extra, 240))
        return lbl

    def card(parent, title=None, desc=None):
        fr = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER)
        fr.pack(fill="x", pady=(0, 12))
        inner = ctk.CTkFrame(fr, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=16)
        if title:
            label(inner, title, 15, True).pack(anchor="w")
        if desc:
            label(inner, desc, 12, color=SUB, wrap_extra=40).pack(anchor="w", fill="x", pady=(2, 8))
        return inner

    def button(parent, text, command, kind="ghost", width=0, height=36, **kw):
        styles = {
            "primary": dict(fg_color=ACCENT, hover_color=ACCENT_H, text_color="#FFFFFF", font=F(14, True)),
            "secondary": dict(fg_color=SEG, hover_color=SEG_HOVER, text_color=TEXT, font=F(14)),
            "ghost": dict(fg_color="transparent", hover_color=SEG, text_color=ACCENT, font=F(13, True)),
            "icon": dict(fg_color="transparent", hover_color=SEG, text_color=SUB, font=F(14)),
            "field": dict(fg_color=FIELD, hover_color=SEG, text_color=TEXT, font=F(13),
                          border_width=1, border_color=BORDER),
            "chip": dict(fg_color=CARD, hover_color=SEG, text_color=TEXT, font=F(12),
                         border_width=1, border_color=BORDER, corner_radius=16),
        }
        opts = {"corner_radius": 10, **styles[kind], **kw}
        return ctk.CTkButton(parent, text=text, command=command, width=width, height=height, **opts)

    def entry(parent, value="", show=None, **kw):
        e = ctk.CTkEntry(parent, fg_color=FIELD, border_color=BORDER, border_width=1, text_color=TEXT,
                         corner_radius=10, height=38, font=F(13), show=show, **kw)
        if value:
            e.insert(0, value)
        return e

    def option(parent, values, current, command=None, width=140):
        om = ctk.CTkOptionMenu(
            parent, values=values, command=command, width=width, height=38, corner_radius=10,
            fg_color=FIELD, button_color=FIELD, button_hover_color=SEG, text_color=TEXT,
            dropdown_fg_color=CARD, dropdown_text_color=TEXT, dropdown_hover_color=SEG,
            font=F(13), dropdown_font=F(13), dynamic_resizing=False,
        )
        om.set(current)
        return om

    def segmented(parent, values, current, command, height=34, size=13):
        sb = ctk.CTkSegmentedButton(
            parent, values=values, command=command, height=height, corner_radius=10, border_width=3,
            fg_color=SEG, selected_color=SEG_SEL, selected_hover_color=SEG_SEL,
            unselected_color=SEG, unselected_hover_color=SEG_HOVER, text_color=TEXT, font=F(size, True),
        )
        sb.set(current)
        return sb

    def textbox(parent, value="", height=90):
        tb = ctk.CTkTextbox(parent, fg_color=FIELD, border_color=BORDER, border_width=1, text_color=TEXT,
                            corner_radius=10, height=height, font=F(13), wrap="word",
                            scrollbar_button_color=THUMB, scrollbar_button_hover_color=SUB)
        if value:
            tb.insert("1.0", value)
        return tb

    def list_box(parent):
        """행 목록을 담는 옅은 둥근 상자"""
        fr = ctk.CTkFrame(parent, fg_color=FIELD, corner_radius=12)
        fr.pack(fill="x")
        return fr

    def badge(parent, text, strong=True):
        return ctk.CTkLabel(parent, text=text, width=24, height=24, corner_radius=12, font=F(11, True),
                            fg_color=BADGE_BG if strong else SEG, text_color=BADGE_TX if strong else SUB)

    def separator_line(parent):
        ctk.CTkFrame(parent, height=1, fg_color=BORDER).pack(fill="x", padx=10)

    # ---- 레이아웃: 헤더 / 탭 / 본문 / 하단 ----
    header = ctk.CTkFrame(root, fg_color="transparent")
    header.pack(fill="x", padx=28, pady=(22, 12))
    label(header, "AutoRename", 24, True).pack(side="left")
    ctk.CTkLabel(header, text=f"v{updater.APP_VERSION}", font=F(12), text_color=SUB, fg_color=SEG,
                 corner_radius=10, height=24, width=60).pack(side="left", padx=(10, 0), pady=(4, 0))

    bottom = ctk.CTkFrame(root, fg_color="transparent")
    bottom.pack(side="bottom", fill="x")
    ctk.CTkFrame(bottom, height=1, fg_color=BORDER).pack(fill="x")
    bottom_inner = ctk.CTkFrame(bottom, fg_color="transparent")
    bottom_inner.pack(fill="x", padx=28, pady=(14, 18))

    pages = {}
    tab_names = ["일반", "AI", "규칙"]

    def show_tab(name):
        for n, pg in pages.items():
            if n == name:
                pg.pack(fill="both", expand=True, padx=(28, 14), pady=(0, 6))
            else:
                pg.pack_forget()

    tabs = segmented(root, tab_names, "일반", show_tab, height=38, size=14)
    tabs.pack(fill="x", padx=28, pady=(0, 14))

    for n in tab_names:
        pages[n] = ctk.CTkScrollableFrame(
            root, fg_color="transparent", scrollbar_button_color=THUMB, scrollbar_button_hover_color=SUB,
        )

    # =========================================================================
    # 탭 1. 일반
    # =========================================================================
    pg = pages["일반"]
    folders_state = list(config.get("watch_folders") or ([config["watch_folder"]] if config.get("watch_folder") else []))

    c = card(pg, "감시 폴더", "새 파일이 들어오면 AI가 보고 이름을 바꿉니다.")
    folders_box = list_box(c)

    def render_folders():
        for w in folders_box.winfo_children():
            w.destroy()
        if not folders_state:
            label(folders_box, "감시할 폴더가 없어요. 하나 이상 추가해 주세요.", 12, color=SUB).pack(anchor="w", padx=14, pady=12)
        for i, path in enumerate(folders_state):
            if i:
                separator_line(folders_box)
            row = ctk.CTkFrame(folders_box, fg_color="transparent")
            row.pack(fill="x", padx=(14, 4), pady=2)
            label(row, "▸", 13, color=ACCENT).pack(side="left", padx=(0, 8))
            label(row, path, 13).pack(side="left", fill="x", expand=True)
            button(row, "✕", lambda p=path: (folders_state.remove(p), render_folders()), "icon", width=40, height=40).pack(side="right")

    def add_folder():
        sel = filedialog.askdirectory(parent=root)
        if sel and sel not in folders_state:
            folders_state.append(sel)
            render_folders()

    render_folders()
    button(c, "+  폴더 추가", add_folder, "ghost", height=34).pack(anchor="w", pady=(8, 0))

    c = card(pg)
    row = ctk.CTkFrame(c, fg_color="transparent")
    row.pack(fill="x")
    txt = ctk.CTkFrame(row, fg_color="transparent")
    txt.pack(side="left", fill="x", expand=True)
    label(txt, "카운트다운", 15, True).pack(anchor="w")
    label(txt, "이름 바꾸기 전에 알림을 띄우고 기다립니다. 그동안 취소할 수 있어요.", 12, color=SUB, wrap_extra=140).pack(anchor="w", fill="x")
    label(row, "초", 13, color=SUB).pack(side="right", padx=(6, 0))
    ent_sec = entry(row, str(config.get("countdown_seconds", 3)), width=60, justify="center")
    ent_sec.pack(side="right")

    def change_appearance(ui):
        appearance_state["v"] = APPEARANCE_UI_REV[ui]
        ctk.set_appearance_mode(appearance_state["v"])   # 누르는 즉시 바뀐다(저장하면 다음에도 유지)

    c = card(pg, "화면 모드", "시스템을 고르면 윈도우의 다크·라이트 설정을 따라갑니다.")
    segmented(c, list(APPEARANCE_UI.values()), APPEARANCE_UI[appearance_state["v"]], change_appearance).pack(fill="x")

    # =========================================================================
    # 탭 2. AI — 1·2·3순위
    # =========================================================================
    pg = pages["AI"]
    PROVIDER_UI = {"gemini": "Gemini", "openai": "GPT", "claude": "Claude"}
    PROVIDER_UI_REV = {v: k for k, v in PROVIDER_UI.items()}
    MODEL_LABELS = {
        "gemini": {
            "gemini-3.5-flash": "Gemini 3.5 Flash (기본)",
            "gemini-3.6-flash": "Gemini 3.6 Flash",
            "gemini-3.7-flash": "Gemini 3.7 Flash · 최신급",
            "gemini-3.8-flash": "Gemini 3.8 Flash · 최신",
            "gemini-3.5-flash-lite": "Gemini 3.5 Flash-Lite · 가장 가벼움",
            "gemini-2.5-flash": "Gemini 2.5 Flash · 기존 키만",
        },
        "claude": {
            "claude-haiku-5-5": "Claude Haiku 5.5 · 가장 싸고 빠름",
            "claude-sonnet-5-5": "Claude Sonnet 5.5 · 최신 Sonnet",
            "claude-sonnet-5": "Claude Sonnet 5",
            "claude-opus-5-5": "Claude Opus 5.5 · 가장 똑똑함, 비쌈",
        },
    }

    label(pg, "위 순위가 한도·과부하로 막히면 다음 순위로 넘어갑니다.", 12, color=SUB, wrap_extra=0).pack(anchor="w", fill="x", padx=2, pady=(0, 10))

    chain_state = [dict(e) for e in get_ai_chain(config)] or [{"provider": "claude", "api_key": "", "model": DEFAULT_CLAUDE_MODEL}]
    chain_widgets = []
    chain_host = ctk.CTkFrame(pg, fg_color="transparent")
    chain_host.pack(fill="x")
    add_rank_btn = button(pg, "", lambda: None, "field", height=46, corner_radius=14, text_color=ACCENT, font=F(14, True))
    rotation_var = ctk.BooleanVar(value=bool(config.get("gemini_model_rotation", True)))
    rotation_card = ctk.CTkFrame(pg, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER)
    rot_inner = ctk.CTkFrame(rotation_card, fg_color="transparent")
    rot_inner.pack(fill="x", padx=18, pady=14)
    ctk.CTkSwitch(rot_inner, text="Gemini 한도에 막히면 다른 Gemini 모델로 자동 전환", variable=rotation_var,
                  font=F(13), text_color=TEXT, progress_color=ACCENT, button_color="#FFFFFF",
                  button_hover_color="#F2F0E8", fg_color=SEG, switch_width=44, switch_height=24).pack(anchor="w")

    def collect_chain():
        for st, w in zip(chain_state, chain_widgets):
            st["api_key"] = w["key"].get().strip()
            if w.get("model"):
                labels = MODEL_LABELS[st["provider"]]
                st["model"] = {v: k for k, v in labels.items()}.get(w["model"].get(), st["model"])

    def render_chain():
        for w in chain_host.winfo_children():
            w.destroy()
        chain_widgets.clear()
        for idx, st in enumerate(chain_state):
            chain_widgets.append(build_rank_card(idx, st))
        # 순서: 순위 카드들(chain_host) → 「+ N순위 추가」 → Gemini 자동 전환. 매번 다시 pack 해서 순서를 지킨다.
        add_rank_btn.pack_forget()
        rotation_card.pack_forget()
        n = len(chain_state)
        if n < AI_CHAIN_MAX:
            add_rank_btn.configure(text=f"+  {n + 1}순위 추가", command=add_rank)
            add_rank_btn.pack(fill="x", pady=(0, 12))
        if any(s["provider"] == "gemini" for s in chain_state):
            rotation_card.pack(fill="x", pady=(0, 12))

    def change_provider(idx, ui_name):
        collect_chain()
        st = chain_state[idx]
        new = PROVIDER_UI_REV[ui_name]
        if new != st["provider"]:
            st["provider"] = new
            st["model"] = default_model_for(new)
        render_chain()

    def move_up(idx):
        collect_chain()
        chain_state[idx - 1], chain_state[idx] = chain_state[idx], chain_state[idx - 1]
        render_chain()

    def remove_rank(idx):
        collect_chain()
        del chain_state[idx]
        render_chain()

    def add_rank():
        collect_chain()
        chain_state.append({"provider": "gemini", "api_key": "", "model": DEFAULT_GEMINI_MODEL})
        render_chain()

    def build_rank_card(idx, st):
        fr = ctk.CTkFrame(chain_host, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER)
        fr.pack(fill="x", pady=(0, 12))
        inner = ctk.CTkFrame(fr, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=14)

        top = ctk.CTkFrame(inner, fg_color="transparent")
        top.pack(fill="x")
        ctk.CTkLabel(top, text=f"{idx + 1}순위", height=24, corner_radius=12, font=F(12, True), width=58,
                     fg_color=BADGE_BG if idx == 0 else SEG, text_color=BADGE_TX if idx == 0 else SUB).pack(side="left")
        if len(chain_state) > 1:
            button(top, "✕", lambda: remove_rank(idx), "icon", width=36, height=32).pack(side="right")
        if idx > 0:
            button(top, "↑", lambda: move_up(idx), "icon", width=36, height=32).pack(side="right")

        seg = segmented(inner, list(PROVIDER_UI.values()), PROVIDER_UI[st["provider"]],
                        lambda v: change_provider(idx, v), height=32, size=13)
        seg.pack(fill="x", pady=(10, 10))

        grid = ctk.CTkFrame(inner, fg_color="transparent")
        grid.pack(fill="x")
        grid.grid_columnconfigure(1, weight=1)
        label(grid, "API 키", 12, color=SUB).grid(row=0, column=0, sticky="w", padx=(0, 12))
        key_row = ctk.CTkFrame(grid, fg_color="transparent")
        key_row.grid(row=0, column=1, sticky="ew")
        ent = entry(key_row, st.get("api_key", ""), show="●")
        ent.pack(side="left", fill="x", expand=True)

        def toggle():
            hidden = ent.cget("show") != ""
            ent.configure(show="" if hidden else "●")
            eye.configure(text="숨김" if hidden else "보기")

        eye = button(key_row, "보기", toggle, "field", width=52, height=38)
        eye.pack(side="left", padx=(6, 0))

        warn = label(grid, "", 11, color=ERR_TX)

        def check_key(_e=None):
            g = guess_provider(ent.get())
            if g and g != st["provider"]:
                warn.configure(text=f"이 키는 {PROVIDER_UI[g]} 키로 보여요. 위에서 공급자를 확인해 주세요.")
                warn.grid(row=1, column=1, sticky="w", pady=(4, 0))
            else:
                warn.grid_forget()

        ent.bind("<KeyRelease>", check_key)
        check_key()

        w = {"key": ent}
        label(grid, "모델", 12, color=SUB).grid(row=2, column=0, sticky="w", padx=(0, 12), pady=(10, 0))
        if st["provider"] in MODEL_LABELS:
            labels = MODEL_LABELS[st["provider"]]
            cur = labels.get(st["model"]) or labels[default_model_for(st["provider"])]
            om = option(grid, list(labels.values()), cur, width=300)
            om.grid(row=2, column=1, sticky="ew", pady=(10, 0))
            w["model"] = om
        else:
            label(grid, f"{OPENAI_MODEL} (고정)", 13).grid(row=2, column=1, sticky="w", pady=(10, 0))
        return w

    render_chain()

    # =========================================================================
    # 탭 3. 규칙
    # =========================================================================
    pg = pages["규칙"]
    rules_state = [dict(when=r.get("when", ""), how=r.get("how", "")) for r in (config.get("naming_rules") or []) if isinstance(r, dict)]
    common_state = {"text": (config.get("common_rules") or "").strip()}
    if not rules_state and not common_state["text"]:
        # 구버전 통짜 지침은 버리지 않고 「공통」으로 옮긴다 — 동작이 그대로 유지된다
        common_state["text"] = (config.get("user_rules") or "").strip()
    move_state = [dict(r) for r in (config.get("move_rules") or []) if isinstance(r, dict)]

    # ---- 이름 짓는 지침 ----
    c = card(pg, "이름 짓는 지침", "위에서부터 확인해 처음 맞는 지침 하나만 씁니다. 아래 기본 형식보다 우선해요.")
    rules_box = list_box(c)

    def first_line(s, n=60):
        s = (s or "").strip().splitlines()[0] if (s or "").strip() else ""
        return s if len(s) <= n else s[:n - 1] + "…"

    def render_rules():
        for w in rules_box.winfo_children():
            w.destroy()
        for i, r in enumerate(rules_state):
            row = ctk.CTkFrame(rules_box, fg_color="transparent", cursor="hand2")
            row.pack(fill="x", padx=(12, 4), pady=4)
            badge(row, str(i + 1)).pack(side="left", padx=(0, 10))
            # 버튼을 글보다 먼저 pack — 글이 길면 나중에 넣은 버튼이 눌려 뭉개졌다(2026-10-14). ↑(순서 올리기)는 사용자 요청으로 뺐다.
            button(row, "✕", lambda i=i: (rules_state.pop(i), render_rules()), "icon", width=34, height=40).pack(side="right")
            col = ctk.CTkFrame(row, fg_color="transparent", cursor="hand2")
            col.pack(side="left", fill="x", expand=True)
            l1 = label(col, first_line(r["when"], 40) or "(조건 없음)", 13, True)
            l1.pack(anchor="w")
            l2 = label(col, first_line(r["how"]), 12, color=SUB)
            l2.pack(anchor="w")
            for wdg in (row, col, l1, l2):
                wdg.bind("<Button-1>", lambda e, i=i: open_rule_dialog(i))
            separator_line(rules_box)
        row = ctk.CTkFrame(rules_box, fg_color="transparent", cursor="hand2")
        row.pack(fill="x", padx=(12, 4), pady=4)
        badge(row, "—", strong=False).pack(side="left", padx=(0, 10))
        button(row, "›", lambda: open_common_dialog(), "icon", width=34, height=40).pack(side="right")
        col = ctk.CTkFrame(row, fg_color="transparent", cursor="hand2")
        col.pack(side="left", fill="x", expand=True)
        l1 = label(col, "공통 (항상 적용)", 13, True)
        l1.pack(anchor="w")
        l2 = label(col, first_line(common_state["text"]) or "비어 있음 — 눌러서 적기", 12, color=SUB)
        l2.pack(anchor="w")
        for wdg in (row, col, l1, l2):
            wdg.bind("<Button-1>", lambda e: open_common_dialog())

    # ---- 기본 파일명 형식 ----
    c2_title = "기본 파일명 형식"
    TOKENS = [("날짜", "date"), ("시각", "time"), ("내용요약", "summary"), ("출처", "site"), ("원래 이름", "original")]
    SEP_UI = {"_": "구분 _", "-": "구분 -", " ": "구분 공백"}
    SEP_UI_REV = {v: k for k, v in SEP_UI.items()}
    fmt_var = ctk.StringVar(value=(config.get("filename_format") or "").strip() or DEFAULT_FILENAME_FORMAT)
    fmt_history = []
    m = re.search(r"\}([_\- ])\{", fmt_var.get())
    sep_state = {"sep": m.group(1) if m else "_"}
    date_fmt_state = {"v": config.get("date_format") if config.get("date_format") in DATE_FORMATS else DEFAULT_DATE_FORMAT}

    # ---- 자동 이동 ----
    def render_moves():
        for w in move_box.winfo_children():
            w.destroy()
        if not move_state:
            label(move_box, "아직 규칙이 없어요.", 12, color=SUB).pack(anchor="w", padx=14, pady=12)
        for i, r in enumerate(move_state):
            if i:
                separator_line(move_box)
            row = ctk.CTkFrame(move_box, fg_color="transparent", cursor="hand2")
            row.pack(fill="x", padx=(14, 4), pady=4)
            button(row, "✕", lambda i=i: (move_state.pop(i), render_moves()), "icon", width=40, height=40).pack(side="right")
            col = ctk.CTkFrame(row, fg_color="transparent", cursor="hand2")
            col.pack(side="left", fill="x", expand=True)
            l1 = label(col, first_line(r.get("keyword", ""), 40), 13, True)
            l1.pack(anchor="w")
            l2 = label(col, "→ " + first_line(r.get("folder", ""), 56), 12, color=SUB)
            l2.pack(anchor="w")
            for wdg in (row, col, l1, l2):
                wdg.bind("<Button-1>", lambda e, i=i: open_move_dialog(i))

    render_rules()
    button(c, "+  지침 추가", lambda: open_rule_dialog(None), "ghost", height=34).pack(anchor="w", pady=(8, 0))

    c = card(pg)
    head = ctk.CTkFrame(c, fg_color="transparent")
    head.pack(fill="x")
    label(head, c2_title, 15, True).pack(side="left")

    def fmt_set(v, record=True):
        if record:
            fmt_history.append(fmt_var.get())
        fmt_var.set(v)

    button(head, "비우기", lambda: fmt_set(""), "icon", width=56, height=30, font=F(12)).pack(side="right")
    button(head, "↶ 되돌리기", lambda: fmt_set(fmt_history.pop(), False) if fmt_history else None,
           "icon", width=84, height=30, font=F(12)).pack(side="right")

    frow = ctk.CTkFrame(c, fg_color="transparent")
    frow.pack(fill="x", pady=(8, 8))
    ent_fmt = ctk.CTkEntry(frow, textvariable=fmt_var, fg_color=FIELD, border_color=BORDER, border_width=1,
                           text_color=TEXT, corner_radius=10, height=38, font=F(13))
    ent_fmt.pack(side="left", fill="x", expand=True)

    def change_sep(ui):
        sep = SEP_UI_REV[ui]
        sep_state["sep"] = sep
        fmt_set(re.sub(r"\}[_\- ]+\{", "}" + sep + "{", fmt_var.get()))

    option(frow, list(SEP_UI.values()), SEP_UI[sep_state["sep"]], change_sep, width=104).pack(side="left", padx=(8, 0))

    chips = ctk.CTkFrame(c, fg_color="transparent")
    chips.pack(fill="x")

    def add_token(tok):
        cur = fmt_var.get().rstrip()
        fmt_set(cur + (sep_state["sep"] if cur and not cur.endswith(("_", "-", " ")) else "") + "{" + tok + "}")

    # 1줄: [+ 날짜][날짜 형식 ▾][+ 시각]  2줄: [+ 내용요약][+ 출처][+ 원래 이름]
    chip_row1 = ctk.CTkFrame(chips, fg_color="transparent")
    chip_row1.pack(fill="x", pady=(0, 6))
    chip_row2 = ctk.CTkFrame(chips, fg_color="transparent")
    chip_row2.pack(fill="x")
    button(chip_row1, "+ 날짜", lambda: add_token("date"), "chip", width=64, height=30).pack(side="left", padx=(0, 4))
    date_om = option(chip_row1, list(DATE_FORMATS.keys()), date_fmt_state["v"],
                     lambda v: (date_fmt_state.__setitem__("v", v), update_preview()), width=124)
    date_om.configure(height=30)
    date_om.pack(side="left", padx=(0, 12))
    for parent, (ui, tok) in zip((chip_row1, chip_row2, chip_row2, chip_row2), TOKENS[1:]):
        button(parent, f"+ {ui}", lambda t=tok: add_token(t), "chip", width=64, height=30).pack(side="left", padx=(0, 6))

    preview = label(c, "", 12, color=SUB, wrap_extra=40)
    preview.pack(anchor="w", fill="x", pady=(8, 0))

    def update_preview(*_):
        now = datetime.datetime.now()
        try:
            name = build_filename_from_format(fmt_var.get(), now.strftime("%Y%m%d"), "거래내역_엑셀", "IBK기업은행",
                                              time=now.strftime("%H%M"), original="lkj",
                                              date_format=date_fmt_state["v"])
        except Exception:
            name = "(형식을 읽지 못했어요)"
        preview.configure(text=f"미리보기   {name}.xlsx")

    fmt_var.trace_add("write", update_preview)
    update_preview()

    c = card(pg, "자동 이동", "조건에 맞으면 지정한 폴더로 옮깁니다.")
    move_box = list_box(c)
    render_moves()
    button(c, "+  규칙 추가", lambda: open_move_dialog(None), "ghost", height=34).pack(anchor="w", pady=(8, 0))

    # =========================================================================
    # 추가·편집 창 (지침 / 공통 / 이동 규칙) — 「AI로 다듬기」 포함
    # =========================================================================
    def open_dialog(kind, title, initial, on_ok, ok_text):
        dlg = ctk.CTkToplevel(root)
        dlg.title(title)
        dlg.configure(fg_color=CARD)
        dlg.transient(root)
        dlg.attributes("-topmost", True)
        # 창 크기는 설정 창에 맞추고 사용자가 늘릴 수 있게 둔다. 입력칸이 남는 공간을 채운다
        # (고정 크기에 스크롤만 두면 긴 지침이 답답하고, 공통 지침 창에선 저장 버튼이 밀려 안 보였다 — 2026-10-14).
        dlg.resizable(True, True)
        dlg.minsize(440, 480)
        dw = max(min(root.winfo_width() - 40, 640), 460)
        dh = max(root.winfo_height() - 60, 560)
        rx, ry = root.winfo_rootx(), root.winfo_rooty()
        dlg.geometry(f"{dw}x{dh}+{rx + max((root.winfo_width() - dw) // 2, 0)}+{ry + 30}")
        dlg.after(250, _set_icon_for(dlg))

        # 버튼줄을 본문보다 먼저 pack 해야 창이 작아져도 항상 맨 아래에 남는다(pack 은 먼저 넣은 것에 자리를 먼저 준다)
        foot = ctk.CTkFrame(dlg, fg_color="transparent")
        foot.pack(side="bottom", fill="x", padx=24, pady=(8, 20))

        body = ctk.CTkFrame(dlg, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=(20, 0))
        label(body, title, 18, True).pack(anchor="w", pady=(0, 12))

        fields = {}
        if kind == "rule":
            label(body, "어떤 파일일 때", 13, True).pack(anchor="w", pady=(0, 4))
            fields["when"] = textbox(body, initial.get("when", ""), 64)
            fields["when"].pack(fill="x", pady=(0, 10))
            label(body, "이름은 이렇게", 13, True).pack(anchor="w", pady=(0, 4))
            fields["how"] = textbox(body, initial.get("how", ""), 80)
            fields["how"].pack(fill="both", expand=True, pady=(0, 10))   # 긴 쪽이 남는 공간을 채운다
        elif kind == "move":
            label(body, "이런 파일이면", 13, True).pack(anchor="w", pady=(0, 4))
            fields["when"] = textbox(body, initial.get("keyword", ""), 64)
            fields["when"].pack(fill="both", expand=True, pady=(0, 10))
        else:
            label(body, "모든 파일에 항상 같이 적용할 지침", 13, True).pack(anchor="w", pady=(0, 4))
            fields["when"] = textbox(body, initial.get("text", ""), 120)
            fields["when"].pack(fill="both", expand=True, pady=(0, 10))

        # AI로 다듬기 — 저장된 설정의 1순위 키로 호출. 화면에서 고쳤어도 저장 전이면 반영되지 않는다.
        saved_chain = get_ai_chain(load_config())
        arow = ctk.CTkFrame(body, fg_color="transparent")
        arow.pack(fill="x", pady=(0, 10))
        refine_btn = button(arow, "✦  AI로 다듬기", None, "field", height=38, text_color=ACCENT, font=F(13, True), width=130)
        refine_btn.pack(side="left")
        model_hint = saved_chain[0]["model"] if saved_chain else ""
        label(arow, f"AI 탭 1순위({model_hint})로 한 번 호출해요 · 0.1센트 미만" if saved_chain else "AI 탭 1순위 키로 한 번 호출해요",
              11, color=SUB).pack(side="left", padx=(10, 0))

        # CTkFrame 은 비어 있으면 기본 높이 200px 을 차지해 입력칸 아래에 빈 공간이 생긴다 → height=1
        status = ctk.CTkFrame(body, fg_color="transparent", height=1)
        status.pack(fill="x")
        status_labels = []

        def rewrap_status(_e=None):
            width = max(status.winfo_width() - 40, 200)
            for lb in status_labels:
                if lb.winfo_exists():
                    lb.configure(wraplength=width)

        status.bind("<Configure>", rewrap_status, add="+")

        def box(bg, bd, title_text, title_color, msg, actions=()):
            for w in status.winfo_children():
                w.destroy()
            fr = ctk.CTkFrame(status, fg_color=bg, border_color=bd, border_width=1, corner_radius=12)
            fr.pack(fill="x")
            inner = ctk.CTkFrame(fr, fg_color="transparent")
            inner.pack(fill="x", padx=14, pady=12)
            label(inner, title_text, 12, True, color=title_color).pack(anchor="w")
            lbl = label(inner, msg, 13)
            status_labels[:] = [lbl]
            lbl.configure(wraplength=max(status.winfo_width() - 40, 200) if status.winfo_width() > 1 else dw - 90)
            lbl.pack(anchor="w", fill="x", pady=(4, 0))
            if actions:
                ar = ctk.CTkFrame(inner, fg_color="transparent")
                ar.pack(fill="x", pady=(8, 0))
                for text, cmd, k in reversed(actions):
                    button(ar, text, cmd, k, height=32, font=F(12, True)).pack(side="right", padx=(6, 0))

        def clear_status():
            for w in status.winfo_children():
                w.destroy()

        if not saved_chain:
            refine_btn.configure(state="disabled", text_color=SUB)
            box(WARN_BG, WARN_BD, "저장된 API 키가 없어요", WARN_TX,
                "AI 탭에서 키를 넣고 저장한 뒤에 쓸 수 있어요. 다듬기 없이 적은 그대로 추가해도 됩니다.")

        def get(name):
            return fields[name].get("1.0", "end-1c").strip() if name in fields else ""

        def put(name, value):
            fields[name].delete("1.0", "end")
            fields[name].insert("1.0", value)

        def do_refine():
            when = get("when")
            if not when:
                box(WARN_BG, WARN_BD, "먼저 내용을 적어 주세요", WARN_TX, "적은 문장을 AI가 알아듣기 쉽게 다듬어 드려요.")
                return
            refine_btn.configure(state="disabled", text="다듬는 중…")
            clear_status()

            def work():
                try:
                    res = refine_text(kind, when, get("how"))
                    dlg.after(0, lambda: show_suggestion(res))
                except RefineError as e:
                    msg = str(e)
                    dlg.after(0, lambda: show_error(msg))
                except Exception as e:  # 예상 못 한 오류도 창이 굳지 않게
                    msg = f"알 수 없는 오류: {e}"
                    dlg.after(0, lambda: show_error(msg))

            threading.Thread(target=work, daemon=True).start()

        def reset_btn():
            if dlg.winfo_exists():
                refine_btn.configure(state="normal", text="✦  AI로 다듬기")

        def show_error(msg):
            if not dlg.winfo_exists():
                return
            reset_btn()
            box(ERR_BG, ERR_BD, "다듬기에 실패했어요", ERR_TX, msg + " 적은 그대로 추가할 수는 있어요.",
                actions=[("다시 시도", do_refine, "field")])

        def show_suggestion(res):
            if not dlg.winfo_exists():
                return
            reset_btn()
            text = res["when"] if kind != "rule" else f"조건  {res['when']}\n\n이름  {res.get('how', '')}"

            def use():
                put("when", res["when"])
                if kind == "rule":
                    put("how", res.get("how", ""))
                clear_status()

            box(SUG_BG, SUG_BD, f"AI가 다듬은 내용 · {res.get('model', '')}", BADGE_TX, text,
                actions=[("원래 문장 쓰기", clear_status, "icon"), ("이걸로 쓰기", use, "field")])

        refine_btn.configure(command=do_refine)

        dest = None
        if kind == "move":
            label(body, "이 폴더로 옮기기", 13, True).pack(anchor="w", pady=(10, 4))
            drow = ctk.CTkFrame(body, fg_color="transparent")
            drow.pack(fill="x")
            dest = entry(drow, initial.get("folder", ""))
            dest.pack(side="left", fill="x", expand=True)

            def browse():
                sel = filedialog.askdirectory(parent=dlg)
                if sel:
                    dest.delete(0, "end")
                    dest.insert(0, sel)

            button(drow, "찾기", browse, "field", width=60, height=38).pack(side="left", padx=(6, 0))

        def ok():
            if kind == "rule":
                if not get("when") or not get("how"):
                    messagebox.showerror("입력 필요", "「어떤 파일일 때」와 「이름은 이렇게」를 모두 적어 주세요.", parent=dlg)
                    return
                on_ok({"when": get("when"), "how": get("how")})
            elif kind == "move":
                if not get("when") or not dest.get().strip():
                    messagebox.showerror("입력 필요", "조건과 폴더를 모두 입력해 주세요.", parent=dlg)
                    return
                on_ok({"keyword": get("when"), "folder": dest.get().strip()})
            else:
                on_ok({"text": get("when")})
            dlg.destroy()

        button(foot, ok_text, ok, "primary", width=90, height=42, corner_radius=12).pack(side="right")
        button(foot, "취소", dlg.destroy, "secondary", width=80, height=42, corner_radius=12).pack(side="right", padx=(0, 8))
        dlg.after(150, lambda: (dlg.lift(), dlg.focus_force(), dlg.grab_set()))

    def _set_icon_for(win):
        def _f():
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            ico = os.path.join(base, "app_icon.ico")
            if os.path.exists(ico):
                try:
                    win.iconbitmap(ico)
                except Exception:
                    pass
        return _f

    def open_rule_dialog(i):
        init = rules_state[i] if i is not None else {}

        def done(v):
            if i is None:
                rules_state.append(v)
            else:
                rules_state[i] = v
            render_rules()

        open_dialog("rule", "지침 추가" if i is None else "지침 편집", init, done, "추가" if i is None else "저장")

    def open_common_dialog():
        def done(v):
            common_state["text"] = v["text"]
            render_rules()

        open_dialog("common", "공통 지침", {"text": common_state["text"]}, done, "저장")

    def open_move_dialog(i):
        init = move_state[i] if i is not None else {}

        def done(v):
            if i is None:
                move_state.append(v)
            else:
                move_state[i] = v
            render_moves()

        open_dialog("move", "이동 규칙 추가" if i is None else "이동 규칙 편집", init, done, "추가" if i is None else "저장")

    # =========================================================================
    # 저장
    # =========================================================================
    def on_close():
        root.destroy()

    def on_save():
        sec_val = ent_sec.get().strip()
        if not sec_val.isdigit():
            tabs.set("일반"); show_tab("일반")
            messagebox.showerror("데이터 오류", "카운트다운은 숫자(초)로 입력해 주세요.", parent=root)
            return
        if not folders_state:
            tabs.set("일반"); show_tab("일반")
            messagebox.showerror("데이터 오류", "감시 폴더를 하나 이상 추가해 주세요.", parent=root)
            return
        collect_chain()
        chain = [dict(e) for e in chain_state if e.get("api_key")]
        for e in chain:
            if e["provider"] == "openai":
                e["model"] = OPENAI_MODEL
        if not chain:
            tabs.set("AI"); show_tab("AI")
            messagebox.showerror("데이터 오류", "AI 탭에 API 키를 하나 이상 넣어 주세요.", parent=root)
            return

        new_config = dict(config)   # 화면에 없는 키(max_output_tokens 등)는 그대로 둔다
        new_config.update({
            "ai_chain": chain,
            "gemini_model_rotation": bool(rotation_var.get()),
            "watch_folders": list(folders_state),
            "countdown_seconds": int(sec_val),
            "naming_rules": [dict(r) for r in rules_state],
            "common_rules": common_state["text"],
            "filename_format": fmt_var.get().strip() or DEFAULT_FILENAME_FORMAT,
            "date_format": date_fmt_state["v"],
            "move_rules": [dict(r) for r in move_state],
            "appearance_mode": appearance_state["v"],
        })
        new_config.update(chain_to_legacy(chain, config))          # 1.3.x 가 읽는 옛 AI 칸
        new_config["user_rules"] = compose_user_rules(new_config)  # 1.3.x 가 읽는 옛 지침 칸

        if save_config(new_config):
            messagebox.showinfo("설정 저장", "저장했어요.\n실행 중인 감시 엔진에 바로 적용됩니다.", parent=root)
            on_close()
        else:
            messagebox.showerror("저장 실패", "설정 파일을 쓰지 못했습니다.", parent=root)

    button(bottom_inner, "저장", on_save, "primary", width=96, height=42, corner_radius=12).pack(side="right")
    button(bottom_inner, "취소", on_close, "secondary", width=80, height=42, corner_radius=12).pack(side="right", padx=(0, 10))
    root.bind("<Control-s>", lambda e: on_save())
    root.protocol("WM_DELETE_WINDOW", on_close)

    show_tab("일반")
    root.after(200, on_root_resize)
    root.mainloop()


def main():
    if not watcher.start_background_threads():
        return

    # 최초 실행(또는 선택된 공급자의 API 키 미설정 상태)이면 설정 창을 자동으로 띄워 안내
    try:
        from file_namer import load_config, get_ai_chain
        cfg = load_config()
        if not get_ai_chain(cfg):
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

    def on_open_config_folder(icon, item):
        """설정 파일을 탐색기에서 선택한 채로 연다 — AI 에이전트에게 경로를 알려 줄 때 쓴다(AI_설정가이드.md)."""
        from file_namer import CONFIG_PATH
        try:
            subprocess.Popen(["explorer", "/select,", CONFIG_PATH])
        except Exception as e:
            watcher.log(f"[설정 파일 위치 열기 실패] {e}")

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

    def on_open_plugin_settings(icon, item):
        """맥락 플러그인(config["context_plugin"])의 자체 설정 창 — 플러그인이 open_settings() 를 가질 때만 메뉴에 보인다."""
        try:
            if getattr(sys, 'frozen', False):
                subprocess.Popen([sys.executable, "--plugin-settings"])
            else:
                subprocess.Popen([sys.executable, __file__, "--plugin-settings"])
        except Exception as e:
            watcher.log(f"[맥락 플러그인 설정 창 실행 실패] {e}")

    def has_plugin_settings(item=None):
        from file_namer import load_context_plugin
        plugin = load_context_plugin()
        return bool(plugin and hasattr(plugin, "open_settings"))

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
            pystray.MenuItem("맥락 플러그인 설정", on_open_plugin_settings, visible=has_plugin_settings),
            pystray.MenuItem("로그 보기", on_open_log),
            pystray.MenuItem("설정 파일 위치 열기", on_open_config_folder),
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
    elif len(sys.argv) > 1 and sys.argv[1] == "--plugin-settings":
        from file_namer import load_context_plugin
        plugin = load_context_plugin()
        if plugin and hasattr(plugin, "open_settings"):
            plugin.open_settings()
    else:
        main()
