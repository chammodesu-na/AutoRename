# -*- coding: utf-8 -*-
"""
rename_watcher.py (고성능 실시간 감시 엔진)
------------------------
다운로드 폴더 내의 변경 사항을 완벽하게 줄 세워서 감시하며(FIFO Queue),
설정 파일의 실시간 동적 자동 감지 및 핫스왑 기능을 탑재했습니다.
"""

import os
import sys
import time
import threading
import queue
import datetime
import tkinter as tk
from tkinter import font as tkfont

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

try:
    from file_namer import (
        analyze_file, load_config, save_config, CONFIG_PATH,
        record_user_correction, get_source_domain,
        load_failed_files, add_failed_file, remove_failed_file,
    )
    AI_AVAILABLE = True
except ImportError:
    AI_AVAILABLE = False

# ====================== 메모리 구조 로드 ======================
COUNTDOWN_SECONDS = 3
WATCH_FOLDERS = [os.path.join(os.path.expanduser("~"), "Downloads")]
TOAST_DURATION_MS = 4000
IGNORE_SUFFIXES = (".crdownload", ".tmp", ".part", ".download")
STABILIZE_CHECK_INTERVAL = 0.2
STABILIZE_CHECKS_REQUIRED = 2
LOG_FILE = os.path.join(os.path.expanduser("~"), "rename_watcher.log")
LOG_MAX_BYTES = 1 * 1024 * 1024  # 1MB 넘으면 로테이션
LOG_KEEP_LINES = 500              # 로테이션 시 최근 N줄만 남김

# 전역 큐 및 감시자 인스턴스 홀더
event_queue = queue.Queue()
observer_instance = None
observer_lock = threading.Lock()

# 실행 취소 세션 관리 큐 (최대 10개)
rename_history = []
history_lock = threading.Lock()

paused = threading.Event()


def rotate_log_if_needed():
    """로그 파일이 너무 커지면 최근 LOG_KEEP_LINES줄만 남기고 정리."""
    try:
        if not os.path.exists(LOG_FILE):
            return
        if os.path.getsize(LOG_FILE) < LOG_MAX_BYTES:
            return
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        trimmed = lines[-LOG_KEEP_LINES:]
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"[로그 로테이션됨, 이전 {len(lines) - len(trimmed)}줄 정리됨]\n")
            f.writelines(trimmed)
    except Exception:
        pass  # 로테이션 실패해도 프로그램 동작에는 지장 없도록 무시


def log(message: str):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {message}"
    try:
        print(line)
    except Exception:
        pass
    try:
        rotate_log_if_needed()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def is_ignored(path: str) -> bool:
    name = os.path.basename(path)
    if name.startswith(".") or name.startswith("~$"):  # ~$: 엑셀/워드 임시 잠금 파일
        return True
    return name.lower().endswith(IGNORE_SUFFIXES)


def wait_until_stable(path: str, timeout: float = 15.0) -> bool:
    start = time.time()
    last_size = -1
    stable_count = 0
    while time.time() - start < timeout:
        if not os.path.exists(path):
            return False
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        if size == last_size:
            stable_count += 1
            if stable_count >= STABILIZE_CHECKS_REQUIRED:
                return True
        else:
            stable_count = 0
            last_size = size
        time.sleep(STABILIZE_CHECK_INTERVAL)
    return True


def wait_until_unlocked(path: str, timeout: float = 10.0, interval: float = 0.5) -> bool:
    """
    파일을 배타적으로 열 수 있을 때까지 대기.
    크기가 안정됐어도 브라우저/다른 프로세스가 핸들을 잡고 있을 수 있음.
    """
    start = time.time()
    while time.time() - start < timeout:
        try:
            with open(path, "r+b"):
                return True
        except (OSError, IOError):
            time.sleep(interval)
    return False  # 타임아웃 내에 핸들이 안 풀림 -> 이름변경 건너뜀


self_renamed_paths = {}
self_renamed_lock = threading.Lock()
SELF_RENAMED_TTL = 10.0


def mark_self_renamed(path: str):
    norm = os.path.normcase(os.path.abspath(path))
    with self_renamed_lock:
        self_renamed_paths[norm] = time.time()


def is_self_renamed(path: str) -> bool:
    norm = os.path.normcase(os.path.abspath(path))
    now = time.time()
    with self_renamed_lock:
        stale = [k for k, t in self_renamed_paths.items() if now - t > SELF_RENAMED_TTL]
        for k in stale:
            del self_renamed_paths[k]

        if norm in self_renamed_paths:
            del self_renamed_paths[norm]
            return True
    return False


class NewFileHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        path = event.src_path
        if is_ignored(path):
            return
        if is_self_renamed(path):
            return
        event_queue.put(("created", path, time.time()))

    def on_moved(self, event):
        if event.is_directory:
            return
        dest = event.dest_path
        if is_ignored(dest):
            return
        if is_self_renamed(dest):
            return
        event_queue.put(("created", dest, time.time()))


# ====================== 공통 UI 다크 테마 ======================
BG = "#1e1e22"
FG = "#f2f2f2"
SUB = "#9a9aa2"
ACCENT = "#6ee7b7"
ACCENT_DIM = "#2d4a3e"
DANGER = "#ff8585"
ENTRY_BG = "#2a2a2f"


class Toast:
    def __init__(self, title: str, message: str, accent: str = ACCENT):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self.root.configure(bg=BG)

        W, H = 380, 95
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = sw - W - 24
        y = sh - H - 60
        self.root.geometry(f"{W}x{H}+{x}+{y}")

        title_font = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        msg_font = tkfont.Font(family="Segoe UI", size=9)

        outer = tk.Frame(self.root, bg=BG, highlightthickness=1, highlightbackground="#3a3a40")
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x", padx=14, pady=(12, 2))
        tk.Label(header, text=title, fg=accent, bg=BG, font=title_font).pack(side="left")

        tk.Label(outer, text=message, fg=SUB, bg=BG, font=msg_font,
                  anchor="w", justify="left", wraplength=350).pack(fill="x", padx=14)

        self.root.bind("<Button-1>", lambda e: self.close())
        outer.bind("<Button-1>", lambda e: self.close())

        self.root.after(10, self.fade_in)
        self.root.after(TOAST_DURATION_MS, self.fade_out)

    def fade_in(self, alpha=0.0):
        alpha = min(alpha + 0.15, 0.95)
        try:
            self.root.attributes("-alpha", alpha)
        except tk.TclError:
            return
        if alpha < 0.95:
            self.root.after(15, lambda: self.fade_in(alpha))

    def fade_out(self, alpha=0.95):
        alpha = max(alpha - 0.1, 0)
        try:
            self.root.attributes("-alpha", alpha)
        except tk.TclError:
            self.close()
            return
        if alpha <= 0:
            self.close()
        else:
            self.root.after(20, lambda: self.fade_out(alpha))

    def close(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


def show_toast(title: str, message: str, accent: str = ACCENT):
    threading.Thread(target=lambda: Toast(title, message, accent).run(), daemon=True).start()


class RenamePopup:
    def __init__(self, filepath: str, suggested_name: str | None = None, reason: str | None = None):
        self.filepath = filepath
        self.dirpath, self.fullname = os.path.split(filepath)
        self.basename, self.ext = os.path.splitext(self.fullname)
        self.suggested_name = suggested_name
        self.reason = reason
        self.remaining = COUNTDOWN_SECONDS * 1000
        self.tick_ms = 30
        self.cancelled = False
        self.confirmed = False

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self.root.configure(bg=BG)

        W, H = (380, 150) if reason else (380, 130)
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = sw - W - 24
        y = sh - H - 60
        self.root.geometry(f"{W}x{H}+{x}+{y}")

        title_font = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        sub_font = tkfont.Font(family="Segoe UI", size=9)
        entry_font = tkfont.Font(family="Segoe UI", size=11)

        outer = tk.Frame(self.root, bg=BG, highlightthickness=1, highlightbackground="#3a3a40")
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x", padx=14, pady=(10, 2))

        tk.Label(header, text="이름 확인 필요", fg=ACCENT, bg=BG, font=title_font).pack(side="left")
        tk.Label(header, text="✕", fg=SUB, bg=BG, cursor="hand2", font=sub_font).pack(side="right")
        header.winfo_children()[-1].bind("<Button-1>", lambda e: self.on_cancel())

        tk.Label(outer, text=self.fullname, fg=SUB, bg=BG, font=sub_font, anchor="w").pack(fill="x", padx=14)

        if self.reason:
            tk.Label(outer, text=self.reason, fg=SUB, bg=BG, font=sub_font, anchor="w", wraplength=350, justify="left").pack(fill="x", padx=14, pady=(2, 0))

        entry_row = tk.Frame(outer, bg=BG)
        entry_row.pack(fill="x", padx=14, pady=(8, 6))

        initial_value = self.suggested_name if self.suggested_name else self.basename
        self.entry_var = tk.StringVar(value=initial_value)
        self.entry = tk.Entry(entry_row, textvariable=self.entry_var, bg=ENTRY_BG, fg=FG, insertbackground=FG, relief="flat", font=entry_font)
        self.entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 6))

        tk.Label(entry_row, text=self.ext, fg=SUB, bg=BG, font=entry_font).pack(side="left")

        bar_frame = tk.Frame(outer, bg=ACCENT_DIM, height=4)
        bar_frame.pack(fill="x", side="bottom")
        self.bar = tk.Frame(bar_frame, bg=ACCENT, height=4, width=W)
        self.bar.place(x=0, y=0, relheight=1, width=W)
        self.bar_width = W

        self.entry.bind("<Return>", lambda e: self.on_confirm())
        self.entry.bind("<Key>", lambda e: self.pause_timer())
        self.entry.focus_force()
        self.entry.select_range(0, "end")
        self.entry.icursor("end")

        self.root.after(10, self.fade_in)
        self.timer_id = self.root.after(400, self.tick)

    def fade_in(self, alpha=0.0):
        alpha = min(alpha + 0.15, 0.97)
        try:
            self.root.attributes("-alpha", alpha)
        except tk.TclError:
            return
        if alpha < 0.97:
            self.root.after(15, lambda: self.fade_in(alpha))

    def pause_timer(self):
        if self.timer_id is not None:
            self.root.after_cancel(self.timer_id)
            self.timer_id = None

    def tick(self):
        self.remaining -= self.tick_ms
        if self.remaining <= 0:
            self.on_timeout()
            return
        frac = self.remaining / (COUNTDOWN_SECONDS * 1000)
        try:
            self.bar.place_configure(width=int(self.bar_width * frac))
        except tk.TclError:
            return
        self.timer_id = self.root.after(self.tick_ms, self.tick)

    def on_timeout(self):
        self.on_confirm()

    def on_cancel(self):
        self.cancelled = True
        self.close()

    def on_confirm(self):
        self.confirmed = True
        self.close()

    def close(self):
        if self.timer_id is not None:
            try:
                self.root.after_cancel(self.timer_id)
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self) -> str | None:
        self.root.mainloop()
        if self.cancelled:
            return None
        new_base = self.entry_var.get().strip()
        if new_base and new_base != self.basename:
            return new_base
        return None


def sanitize_filename(name: str) -> str:
    invalid = '<>:"/\\|?*'
    for ch in invalid:
        name = name.replace(ch, "")
    return name.strip()


def do_rename(filepath: str, new_base: str) -> str | None:
    new_base = sanitize_filename(new_base)
    if not new_base:
        return None
    dirpath, fullname = os.path.split(filepath)
    _, ext = os.path.splitext(fullname)
    new_path = os.path.join(dirpath, new_base + ext)

    counter = 1
    final_path = new_path
    while os.path.exists(final_path):
        final_path = os.path.join(dirpath, f"{new_base} ({counter}){ext}")
        counter += 1

    try:
        mark_self_renamed(final_path)
        os.rename(filepath, final_path)
        
        with history_lock:
            rename_history.append((filepath, final_path))
            if len(rename_history) > 10:
                rename_history.pop(0)
                
        return final_path
    except OSError as e:
        log(f"[이름변경 실패] {e}")
        return None


def move_to_folder(filepath: str, dest_dir: str) -> str | None:
    """파일을 지정 폴더로 이동. 폴더 없으면 생성. 성공 시 새 경로, 실패 시 None."""
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception as e:
        log(f"[폴더 생성 실패] {dest_dir}: {e}")
        return None

    fullname = os.path.basename(filepath)
    dest = os.path.join(dest_dir, fullname)

    counter = 1
    while os.path.exists(dest):
        stem, ext = os.path.splitext(fullname)
        dest = os.path.join(dest_dir, f"{stem} ({counter}){ext}")
        counter += 1

    try:
        mark_self_renamed(dest)
        os.rename(filepath, dest)
        return dest
    except OSError as e:
        log(f"[이동 실패] {e}")
        return None


def undo_last_rename() -> tuple[bool, str]:
    with history_lock:
        if not rename_history:
            return False, "되돌릴 변경 이력이 존재하지 않습니다."
        orig_path, current_path = rename_history.pop()

    if not os.path.exists(current_path):
        return False, f"바뀐 파일이 해당 위치에 없습니다: {os.path.basename(current_path)}"

    if os.path.exists(orig_path):
        return False, f"이미 원본 파일명이 생성되어 있어 충돌합니다: {os.path.basename(orig_path)}"

    try:
        mark_self_renamed(orig_path)
        os.rename(current_path, orig_path)
        orig_name = os.path.basename(orig_path)
        cur_name = os.path.basename(current_path)
        log(f"[실행 취소 성공] {cur_name} -> {orig_name}")
        return True, f"'{orig_name}'으로 복원 완료!"
    except OSError as e:
        log(f"[실행 취소 실패] {e}")
        return False, "파일 롤백 중 시스템 에러가 발생했습니다."


def apply_general_move_rule(path: str, matched_rule_index) -> str | None:
    """
    설정 GUI에서 등록한 '조건 -> 폴더' 이동 규칙 중 AI가 매칭시킨 규칙이 있으면
    해당 폴더로 파일을 이동한다. 매칭 없거나 설정이 유효하지 않으면 아무것도 하지 않는다.
    이동 성공 시 새 경로, 아니면 None을 반환한다.
    """
    if matched_rule_index is None or not AI_AVAILABLE:
        return None
    try:
        idx = int(matched_rule_index)
    except (TypeError, ValueError):
        return None

    config = load_config()
    move_rules = config.get("move_rules", []) or []
    if not (0 <= idx < len(move_rules)):
        return None

    dest_dir = (move_rules[idx].get("folder") or "").strip()
    if not dest_dir:
        return None

    fullname = os.path.basename(path)
    moved_path = move_to_folder(path, dest_dir)
    if moved_path:
        log(f"[이동 규칙 적용] {fullname} → {dest_dir}")
        show_toast("이동 규칙에 따라 폴더 이동됨", f'"{fullname}"\n→ {dest_dir}', accent=ACCENT)
        return moved_path
    else:
        log(f"[이동 규칙 적용 실패] {fullname} → {dest_dir}")
        show_toast("이동 규칙 적용 실패", f'"{fullname}"', accent="#ff8585")
        return None


def handle_rename(filepath: str):
    if paused.is_set():
        log(f"[일시정지 중, 건너뜀] {os.path.basename(filepath)}")
        return

    if not wait_until_stable(filepath):
        return
    if not os.path.exists(filepath):
        return

    fullname = os.path.basename(filepath)
    basename, ext = os.path.splitext(fullname)

    result = None
    failure_reason = None
    if AI_AVAILABLE:
        try:
            log(f"[AI 분석 중...] {fullname}")
            result = analyze_file(filepath)
            if result is None:
                failure_reason = "AI 분석 실패 (로그 참고)"
        except Exception as e:
            log(f"[AI 분석 오류] {e}")
            failure_reason = str(e)

    # AI 분석(파일 읽기)이 끝난 후 핸들이 완전히 풀렸는지 확인하고 rename 진행
    if not wait_until_unlocked(filepath):
        log(f"[잠금 해제 실패, 건너뜀] {fullname}")
        return

    if result is None:
        if AI_AVAILABLE:
            try:
                add_failed_file(filepath, failure_reason or "AI 분석 실패")
            except Exception:
                pass
        log(f"[AI 분석 실패, 수동 확인 필요] {fullname} (자세한 원인은 위 로그 참고)")
        popup = RenamePopup(filepath, suggested_name=None, reason=None)
        new_base = popup.run()
        if new_base:
            final_path = do_rename(filepath, new_base)
            if final_path:
                log(f"[이름변경 완료] {fullname} -> {os.path.basename(final_path)}")
                if AI_AVAILABLE:
                    try:
                        remove_failed_file(filepath)  # 수동으로 해결됨
                    except Exception:
                        pass
        else:
            log(f"[AI 분석 실패로 유지] {fullname}")
        return

    # 여기 도달했다는 건 AI 분석 자체는 성공했다는 뜻 -> 혹시 이전에 실패 기록이 있었다면 제거
    if AI_AVAILABLE:
        try:
            remove_failed_file(filepath)
        except Exception:
            pass

    decision = result.get("decision")
    suggested_name = result.get("suggested_name")
    reason = result.get("reason", "")
    usage = result.get("usage", {})

    if usage:
        p_tok = usage.get("promptTokenCount", 0)
        t_tok = usage.get("thoughtsTokenCount", 0)
        c_tok = usage.get("candidatesTokenCount", 0)
        tot = usage.get("totalTokenCount", 0)
        log(f"[토큰 사용량] 총 {tot}개 소비 (질문: {p_tok} / 추론: {t_tok} / 답변: {c_tok})")

    matched_rule_index = result.get("matched_rule_index")

    if decision == "ok":
        log(f"[적절함, 유지] {fullname} ({reason})")
        show_toast("이름이 명확해서 유지할게요", f'"{fullname}"', accent=ACCENT)
        apply_general_move_rule(filepath, matched_rule_index)
        return

    if decision == "rename" and suggested_name:
        final_path = do_rename(filepath, suggested_name)
        if final_path:
            new_name = os.path.basename(final_path)
            log(f"[자동 변경 완료] {fullname} -> {new_name}")
            show_toast("파일명 정리 완료", f'"{fullname}"\n→ "{new_name}"', accent=ACCENT)
            apply_general_move_rule(final_path, matched_rule_index)
        else:
            log(f"[자동 변경 실패, 유지] {fullname}")
        return

    popup = RenamePopup(filepath, suggested_name=suggested_name, reason=reason)
    new_base = popup.run()
    if new_base:
        final_path = do_rename(filepath, new_base)
        if final_path:
            log(f"[이름변경 완료] {fullname} -> {os.path.basename(final_path)}")
            if AI_AVAILABLE:
                try:
                    domain = get_source_domain(filepath) or "출처불명"
                    record_user_correction(
                        filepath=filepath,
                        ai_suggested=suggested_name,
                        user_final=new_base,
                        domain=domain,
                        ext=ext,
                    )
                except Exception as e:
                    log(f"[학습 로그 기록 실패] {e}")
            apply_general_move_rule(final_path, matched_rule_index)
    else:
        log(f"[애매함 판단, 사용자가 유지 선택] {fullname}")
        apply_general_move_rule(filepath, matched_rule_index)


def retry_failed_files():
    """AI 분석에 실패해 기록된 파일들을 한번에 다시 시도. 트레이 메뉴 "에러 파일 재시도"에서 호출됨.
    네트워크 호출이 포함돼 시간이 걸릴 수 있으므로 트레이 UI를 막지 않도록 별도 스레드에서 실행."""
    if not AI_AVAILABLE:
        show_toast("재시도 불가", "AI 모듈을 불러올 수 없습니다.", accent="#ff8585")
        return

    entries = load_failed_files()
    if not entries:
        show_toast("재시도할 파일 없음", "에러로 기록된 파일이 없습니다.", accent=ACCENT)
        return

    def _run():
        log(f"[에러 파일 재시도 시작] 총 {len(entries)}개")
        missing = 0
        for entry in entries:
            fp = entry.get("filepath")
            if not fp or not os.path.exists(fp):
                missing += 1
                try:
                    remove_failed_file(fp)
                except Exception:
                    pass
                continue
            log(f"[에러 파일 재시도] {os.path.basename(fp)}")
            try:
                handle_rename(fp)
            except Exception as e:
                log(f"[에러 파일 재시도 실패] {os.path.basename(fp)}: {e}")
        remain = len(load_failed_files())
        show_toast(
            "에러 파일 재시도 완료",
            f"{len(entries)}개 시도 (없어진 파일 {missing}개 제외)\n남은 에러: {remain}개",
            accent=ACCENT,
        )

    threading.Thread(target=_run, daemon=True).start()


def gui_dispatcher():
    while True:
        try:
            kind, path, ts = event_queue.get()
            handle_rename(path)
            event_queue.task_done()
        except Exception as e:
            log(f"[디스패처 에러] {e}")


def start_watcher(folder_paths: list):
    global observer_instance
    with observer_lock:
        if observer_instance:
            try:
                observer_instance.stop()
                observer_instance.join()
            except Exception:
                pass
        observer_instance = Observer()
        handler = NewFileHandler()
        scheduled = 0
        for folder_path in folder_paths:
            if not folder_path:
                continue
            if not os.path.isdir(folder_path):
                log(f"[감시 폴더 없음, 건너뜀] {folder_path}")
                continue
            observer_instance.schedule(handler, folder_path, recursive=False)
            scheduled += 1
            log(f"[감시 시작] {folder_path}")
        if scheduled > 0:
            observer_instance.start()
        else:
            observer_instance = None
            log("[감시 실패] 유효한 감시 폴더가 없습니다.")


def reload_config():
    global WATCH_FOLDERS, COUNTDOWN_SECONDS
    if not AI_AVAILABLE:
        return
    config = load_config()
    COUNTDOWN_SECONDS = int(config.get("countdown_seconds", 3))
    new_folders = config.get("watch_folders", WATCH_FOLDERS)
    if isinstance(new_folders, str):
        new_folders = [new_folders]
    new_folders = [f for f in new_folders if f]

    if new_folders != WATCH_FOLDERS or not observer_instance:
        WATCH_FOLDERS = new_folders
        start_watcher(WATCH_FOLDERS)


def config_auto_reloader():
    """설정 JSON 파일의 수정을 감시하여 실시간으로 부모 트레이 감시 엔진에 적용시킵니다."""
    last_mtime = 0
    while True:
        try:
            if os.path.exists(CONFIG_PATH):
                mtime = os.path.getmtime(CONFIG_PATH)
                if mtime != last_mtime:
                    last_mtime = mtime
                    reload_config()
        except Exception:
            pass
        time.sleep(1.0)


# ====================== 7일 지난 파일 정리 ======================

# 확장자 → 하위 폴더명 매핑 테이블
CLEANUP_RULES = {
    "문서":       {".pdf", ".docx", ".doc", ".txt", ".hwp", ".pptx", ".ppt", ".md"},
    "스프레드시트": {".xlsx", ".xls", ".xlsm", ".csv"},
    "이미지":     {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".svg"},
    "압축파일":   {".zip", ".rar", ".7z", ".tar", ".gz"},
    "설치파일":   {".exe", ".msi", ".dmg", ".pkg"},
}
CLEANUP_DAYS = 7
CLEANUP_FOLDER_OTHER = "기타"


def classify_file(ext: str) -> str:
    """확장자를 보고 분류 폴더명 반환. 해당 없으면 '기타'."""
    ext = ext.lower()
    for folder, exts in CLEANUP_RULES.items():
        if ext in exts:
            return folder
    return CLEANUP_FOLDER_OTHER


def run_cleanup() -> tuple[int, int]:
    """
    감시 중인 모든 폴더 안의 파일 중 수정일 기준 CLEANUP_DAYS일 이상 된 파일을
    각 폴더 하위의 확장자별 폴더로 이동. (폴더/숨김파일/~$임시파일 제외)
    반환값: (이동된 파일 수, 실패 수)
    """
    moved = 0
    failed = 0
    cutoff = time.time() - CLEANUP_DAYS * 86400

    for watch_folder in WATCH_FOLDERS:
        try:
            entries = os.listdir(watch_folder)
        except Exception as e:
            log(f"[정리 실패] 폴더 읽기 오류({watch_folder}): {e}")
            continue

        for name in entries:
            if name.startswith(".") or name.startswith("~$"):
                continue

            src = os.path.join(watch_folder, name)

            if os.path.isdir(src):
                continue

            try:
                mtime = os.path.getmtime(src)
            except OSError:
                continue

            if mtime >= cutoff:
                continue  # 아직 7일 안 됨

            _, ext = os.path.splitext(name)
            folder_name = classify_file(ext)
            dest_dir = os.path.join(watch_folder, folder_name)

            try:
                os.makedirs(dest_dir, exist_ok=True)
                dest = os.path.join(dest_dir, name)

                # 같은 이름 파일 이미 있으면 번호 붙이기
                counter = 1
                while os.path.exists(dest):
                    stem, ext2 = os.path.splitext(name)
                    dest = os.path.join(dest_dir, f"{stem} ({counter}){ext2}")
                    counter += 1

                mark_self_renamed(dest)  # 이동 결과 경로도 재트리거 방지 등록
                os.rename(src, dest)
                log(f"[정리 이동] {name} → {folder_name}/ ({watch_folder})")
                moved += 1
            except Exception as e:
                log(f"[정리 실패] {name}: {e}")
                failed += 1

    return moved, failed


def start_background_threads():
    reload_config()
    if not observer_instance:
        log(f"유효한 감시 폴더가 없습니다: {WATCH_FOLDERS}")
        return False

    # 설정 실시간 핫스왑 리로더 실행
    t_reloader = threading.Thread(target=config_auto_reloader, daemon=True)
    t_reloader.start()

    t2 = threading.Thread(target=gui_dispatcher, daemon=True)
    t2.start()
    return True


def main():
    if not start_background_threads():
        sys.exit(1)
    log("감시를 시작합니다. 종료하려면 Ctrl+C를 누르세요.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("종료합니다.")


if __name__ == "__main__":
    main()