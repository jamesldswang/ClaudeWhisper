# -*- coding: utf-8 -*-
"""
語音轉文字小工具 - 離線 Whisper 版 (Windows 桌面程式，非網頁)
  Alt+R  開始錄音（紅色呼吸燈）。每 N 分鐘(預設 10)自動切一段存成 wav，
         背景同步轉成文字，錄音不中斷，繼續錄下一段
  Alt+X  停止錄音，最後一段(不足 N 分鐘也算)轉完後，合併成完整逐字稿，
         複製到剪貼簿 -> 到任何地方 Ctrl+V 貼上
  錄音檔與逐字稿都留在 recordings/<日期_時間>/ (part_001.wav/.txt …, transcript.txt)
  系統匣(右下角)圖示：左鍵切換錄音，右鍵選單可設定分段長度 / 開啟錄音資料夾 /
  換語言 / 開機自動啟動 / 結束

使用 faster-whisper 在本機辨識，不需要網路 (第一次執行會下載模型)。
"""
import asyncio
import ctypes
import ctypes.wintypes as wt
import datetime
import json
import math
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import wave
import winreg
import winsound

import pystray
from PIL import Image, ImageDraw
from pystray import Menu
from pystray import MenuItem as Item

import numpy as np
import sounddevice as sd

# ----------------------------- 設定 -----------------------------
DEFAULT_LANG = "auto"          # 預設語言
LANGS = [
    ("自動偵測 (中英混合)", "auto"),
    ("繁體中文 (台灣)", "zh-TW"),
    ("简体中文", "zh-CN"),
    ("English (US)", "en-US"),
    ("日本語", "ja-JP"),
]
START_KEY = "R"                 # Alt+R
STOP_KEY = "X"                  # Alt+X
PREVIEW_CHARS = 72              # 浮動視窗顯示最後幾個字
__version__ = "0.2.3"
APP_NAME = "VoiceToClipWhisper"
MODEL_SIZE = "small"           # tiny / base / small / medium  (越大越準越慢)
SR = 16000
SPEECH_RMS = 0.006              # 低於此音量視為安靜
DEFAULT_CHUNK_MIN = 10          # 預設每幾分鐘切一段錄音送去轉文字
CHUNK_PRESETS = [1, 3, 5, 10, 15, 20, 30]   # 系統匣選單的快速選項(分鐘)
CUT_GRACE_SEC = 5               # 時間到後最多再等幾秒，找安靜的瞬間再切，避免切在字中間
# ---------------------------------------------------------------

ui_queue: "queue.Queue" = queue.Queue()
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


FROZEN = getattr(sys, "frozen", False)
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "voice_debug.log")
REC_DIR = os.path.join(BASE_DIR, "recordings")          # 每次錄音一個子資料夾
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"save settings error: {e!r}")


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


def fmt_dur(sec):
    """秒數 -> 1:02:03 / 02:03 / 4.2 秒"""
    if sec < 60:
        return f"{sec:.1f} 秒"
    e = int(round(sec))
    h, m, s = e // 3600, e % 3600 // 60, e % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def beep(freq):
    threading.Thread(target=lambda: winsound.Beep(freq, 70), daemon=True).start()


# ----------------------------- 剪貼簿 -----------------------------
def set_clipboard(text):
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    data = text.encode("utf-16-le") + b"\x00\x00"
    for _ in range(20):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(13, h)  # CF_UNICODETEXT
    finally:
        user32.CloseClipboard()
    return True


# ----------------------------- 開機自動啟動 -----------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_command():
    if FROZEN:
        return f'"{sys.executable}"'
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = exe[:-10] + "pythonw.exe"
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def toggle_autostart():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if autostart_enabled():
            winreg.DeleteValue(k, APP_NAME)
        else:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _launch_command())


# ----------------------------- 辨識引擎 (faster-whisper) -----------------------------
PROMPTS = {
    "auto": "以下是繁體中文與 English 混合的語音內容。中文請用繁體，英文請照原文輸出，不要翻譯。",
    "zh-TW": "以下是繁體中文的語音內容，請使用繁體中文和標點符號。",
    "zh-CN": "以下是简体中文的句子。",
}


_REPEAT_RE = re.compile(r"(\S.{1,40}?)(?:[\s,，、。.]*\1){3,}")


def collapse_repeats(text):
    """保險：同一片語連續出現 4 次以上(Whisper 幻覺)只留一次"""
    return _REPEAT_RE.sub(r"\1", text)


class Engine:
    """錄音與轉寫分兩條執行緒同時進行：
    錄音執行緒 -> 每 chunk_min 分鐘把錄音存成 part_NNN.wav，丟進 parts_q，接著錄下一段
    轉寫執行緒 -> 從 parts_q 依序取出 wav 轉成文字(part_NNN.txt)；停止後合併成 transcript.txt
    """

    def __init__(self):
        self.lang = DEFAULT_LANG
        self.model = None
        self.model_lock = threading.Lock()
        self.recording = False
        self.finishing = False
        self.audio_q = queue.Queue()
        self.parts_q = queue.Queue()
        self.stream = None
        self.session_dir = ""
        self.cut_count = 0        # 已切出(送去轉寫)的段數
        self.done_count = 0       # 已轉寫完成的段數
        self.last_text = ""
        self.t_start = 0.0        # 開始錄音的時間點
        self.t_stop = 0.0         # 按下停止的時間點
        try:
            self.chunk_min = max(1, int(load_settings().get("chunk_min", DEFAULT_CHUNK_MIN)))
        except (TypeError, ValueError):
            self.chunk_min = DEFAULT_CHUNK_MIN

    def set_chunk_min(self, minutes):
        """錄音中也可改，從目前這一段起生效"""
        self.chunk_min = max(1, min(240, int(minutes)))
        s = load_settings()
        s["chunk_min"] = self.chunk_min
        save_settings(s)
        log(f"chunk_min = {self.chunk_min}")

    # ---- 文字組合 ----
    def sep(self):
        return " " if self.lang.startswith("en") else ""

    def join(self, parts):
        """接句子：英文/數字相接時補空格，中文之間不加"""
        out = ""
        for p in parts:
            if not p:
                continue
            if out:
                a, b = out[-1], p[0]
                if self.sep() or (a.isascii() and b.isascii() and not a.isspace() and not b.isspace()
                                  and (a.isalnum() or a in ".,?!;:")) :
                    out += " "
            out += p
        return out

    # ---- 模型 ----
    def load_model(self):
        with self.model_lock:
            if self.model is None:
                ui_queue.put(("note", "載入語音模型中(第一次需下載，請稍候)…"))
                # faster_whisper 在 import 時就載入 PyAV(av)，但我們只餵 numpy 音訊、用不到它。
                # PyAV 的 .pyd 未簽章，會被 Windows「智慧型應用程式控制」封鎖 → 先放空殼模組頂替。
                import types
                sys.modules.setdefault("av", types.ModuleType("av"))
                from faster_whisper import WhisperModel
                self.model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
                log("model loaded")
                ui_queue.put(("ready", None))
        return self.model

    def warm_up(self):
        try:
            self.load_model()
        except Exception as e:
            log(f"warm_up error: {e!r}")

    def transcribe(self, audio):
        prompt = PROMPTS.get(self.lang)
        text = self._transcribe(audio, prompt)
        if not text and prompt:
            # 實測：小聲的英文配上中文提示詞，可能整段被判成「沒說話」而回傳空白；不帶提示詞再試一次
            text = self._transcribe(audio, None)
            if text:
                log("empty with prompt, retried without prompt")
        return text

    def _transcribe(self, audio, prompt):
        model = self.load_model()
        lang = None if self.lang == "auto" else self.lang.split("-")[0]
        segs, _ = model.transcribe(
            audio,
            language=lang,
            initial_prompt=prompt,
            beam_size=5,
            # 不可固定成 0.0：輸出重複太嚴重(壓縮率 > 2.4)時，要靠提高 temperature 重新解碼，
            # 否則會出現 "the 6th, the 6th, the 6th…" 這種無限重複的幻覺
            temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
            compression_ratio_threshold=2.4,
            condition_on_previous_text=False,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        return collapse_repeats(self.join([x.text.strip() for x in segs]).strip())

    # ---- 開始 / 停止 ----
    def toggle(self):
        if self.recording:
            self.stop()
        else:
            self.start()

    def start(self):
        if self.recording:
            return
        if self.finishing:
            ui_queue.put(("busy", None))
            return
        self.session_dir = os.path.join(REC_DIR, datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S"))
        try:
            os.makedirs(self.session_dir, exist_ok=True)
        except Exception as e:
            ui_queue.put(("error", f"無法建立錄音資料夾\n{self.session_dir}\n{e}"))
            return
        self.recording = True
        self.cut_count = self.done_count = 0
        self.last_text = ""
        self.t_start = time.time()
        self.t_stop = 0.0
        self.audio_q = queue.Queue()
        self.parts_q = queue.Queue()
        log(f"session start: {self.session_dir} (每 {self.chunk_min} 分鐘一段)")
        ui_queue.put(("start", None))
        beep(880)
        threading.Thread(target=self._run, daemon=True).start()

    def _fail(self, msg):
        self.recording = False
        self.finishing = False
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
        except Exception:
            pass
        self.stream = None
        ui_queue.put(("error", msg))

    def _progress(self):
        ui_queue.put(("progress", (self.cut_count, self.done_count, self.last_text)))

    def _run(self):
        q = self.audio_q

        def cb(indata, frames, t, status):
            q.put(indata[:, 0].copy())

        try:
            self.stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32",
                                         blocksize=int(SR * 0.1), callback=cb)
            self.stream.start()
        except Exception as e:
            log(f"mic error: {e!r}")
            self._fail("無法開啟麥克風\n請確認已接上麥克風，並在「設定 > 隱私權與安全性 > 麥克風」允許桌面應用程式存取\n" + str(e))
            return
        # 轉寫在另一條執行緒，模型載入、轉文字都不會擋住錄音
        threading.Thread(target=self._transcribe_worker, args=(self.parts_q, self.session_dir),
                         daemon=True).start()
        try:
            self._record_loop(q)
        except Exception as e:
            log(f"record error: {e!r}")
            self.parts_q.put(None)
            self._fail(f"錄音失敗：{e}\n已錄下的部分在：{self.session_dir}")

    def _record_loop(self, q):
        """把麥克風聲音寫進 part_NNN.wav；時間到就收尾這一段、交給轉寫，立刻開下一段"""
        idx, wf, path, n = 0, None, "", 0

        def open_part():
            nonlocal idx, wf, path, n
            idx += 1
            path = os.path.join(self.session_dir, f"part_{idx:03d}.wav")
            wf = wave.open(path, "wb")
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SR)
            n = 0

        def close_part():
            wf.close()
            if n < SR // 2:                   # 不到 0.5 秒(例如剛切完就停止)就丟掉
                try:
                    os.remove(path)
                except OSError:
                    pass
                return
            self.cut_count += 1
            log(f"cut part {idx}: {n / SR:.1f}s")
            self.parts_q.put((idx, path))
            self._progress()

        open_part()
        while True:
            c = q.get()
            if c is None:
                break
            wf.writeframes((np.clip(c, -1.0, 1.0) * 32767).astype(np.int16).tobytes())
            n += len(c)
            limit = self.chunk_min * 60 * SR
            if n >= limit:
                quiet = float(np.sqrt(np.mean(c * c))) < SPEECH_RMS
                if quiet or n >= limit + CUT_GRACE_SEC * SR:
                    close_part()
                    open_part()
        close_part()
        self.parts_q.put(None)               # 告訴轉寫執行緒：沒有下一段了

    def _transcribe_worker(self, parts_q, session_dir):
        texts = []
        stats = []                           # (段號, 錄音秒數, 轉換秒數)
        while True:
            item = parts_q.get()
            if item is None:
                break
            idx, path = item
            audio_sec = 0.0
            t0 = time.time()
            try:
                with wave.open(path, "rb") as wf:
                    raw = wf.readframes(wf.getnframes())
                audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                audio_sec = len(audio) / SR
                text = self.transcribe(audio)
            except Exception as e:
                log(f"transcribe part {idx} error: {e!r}")
                text = f"[第 {idx} 段轉寫失敗：{e}，錄音檔 {os.path.basename(path)} 仍保留]"
            conv_sec = time.time() - t0
            stats.append((idx, audio_sec, conv_sec))
            log(f"part {idx}: 錄音 {fmt_dur(audio_sec)}，轉換 {fmt_dur(conv_sec)} | {text}")
            try:
                with open(path[:-4] + ".txt", "w", encoding="utf-8") as f:
                    f.write(text + "\n")
            except Exception as e:
                log(f"write part txt error: {e!r}")
            texts.append(text)
            self.done_count += 1
            self.last_text = text or self.last_text
            self._progress()

        full = "\n\n".join(t for t in texts if t).strip()
        try:
            with open(os.path.join(session_dir, "transcript.txt"), "w", encoding="utf-8") as f:
                f.write(full + "\n")
        except Exception as e:
            log(f"write transcript error: {e!r}")
        timing = self._write_timing(session_dir, stats)
        self.finishing = False
        log(f"done: {len(texts)} parts -> {session_dir} | {timing['summary']}")
        ui_queue.put(("done", (full, session_dir, len(texts), timing)))

    def _write_timing(self, session_dir, stats):
        """把錄音時間、轉換時間寫進 timing.txt (transcript.txt 保持只有文字，方便貼上)"""
        t_end = time.time()
        t_stop = self.t_stop or t_end
        rec_sec = sum(a for _, a, _ in stats)                 # 實際錄到的聲音長度
        conv_sec = sum(c for _, _, c in stats)                # 轉換總耗時(含第一次載入模型)
        wait_sec = max(0.0, t_end - t_stop)                   # 按下停止後等多久才拿到逐字稿
        speed = rec_sec / conv_sec if conv_sec > 0 else 0.0
        summary = (f"錄音 {fmt_dur(rec_sec)}，轉換 {fmt_dur(conv_sec)}"
                   f"（{speed:.1f}x 即時），停止後等待 {fmt_dur(wait_sec)}")
        fmt = "%Y-%m-%d %H:%M:%S"
        lines = [
            f"開始錄音：{time.strftime(fmt, time.localtime(self.t_start))}",
            f"停止錄音：{time.strftime(fmt, time.localtime(t_stop))}",
            f"轉換完成：{time.strftime(fmt, time.localtime(t_end))}",
            f"錄音長度：{fmt_dur(rec_sec)}",
            f"轉換耗時：{fmt_dur(conv_sec)}（速度 {speed:.1f}x 即時，模型 {MODEL_SIZE}）",
            f"停止後等待：{fmt_dur(wait_sec)}",
            "",
            "段落\t錄音長度\t轉換耗時",
        ] + [f"part_{i:03d}\t{fmt_dur(a)}\t{fmt_dur(c)}" for i, a, c in stats]
        try:
            with open(os.path.join(session_dir, "timing.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            log(f"write timing error: {e!r}")
        return {"rec": rec_sec, "conv": conv_sec, "wait": wait_sec, "summary": summary}

    def stop(self):
        if not self.recording:
            return
        self.recording = False
        self.finishing = True
        self.t_stop = time.time()
        log(f"stop requested (錄了 {fmt_dur(self.t_stop - self.t_start)})")
        ui_queue.put(("stopping", None))
        beep(600)
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
        self.stream = None
        self.audio_q.put(None)

    def set_lang(self, code):
        if not self.recording and not self.finishing:
            self.lang = code


# ----------------------------- 全域熱鍵 -----------------------------
def hotkey_thread(engine):
    MOD_ALT, MOD_NOREPEAT = 0x0001, 0x4000
    ok1 = user32.RegisterHotKey(None, 1, MOD_ALT | MOD_NOREPEAT, ord(START_KEY))
    ok2 = user32.RegisterHotKey(None, 2, MOD_ALT | MOD_NOREPEAT, ord(STOP_KEY))
    if not (ok1 and ok2):
        ui_queue.put(("error", f"Alt+{START_KEY} / Alt+{STOP_KEY} 已被其他程式佔用"))
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == 0x0312:  # WM_HOTKEY
            if msg.wParam == 1:
                engine.start()
            elif msg.wParam == 2:
                engine.stop()


# ----------------------------- 系統匣圖示 -----------------------------
def make_icon(recording):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(225, 30, 30, 255) if recording else (50, 50, 50, 255))
    d.rounded_rectangle((25, 13, 39, 37), radius=7, fill="white")
    d.arc((19, 23, 45, 47), 0, 180, fill="white", width=3)
    d.line((32, 47, 32, 53), fill="white", width=3)
    d.line((24, 53, 40, 53), fill="white", width=3)
    return img


# ----------------------------- 浮動視窗 (UI) -----------------------------
class Overlay:
    BG = "#202020"

    def __init__(self, engine):
        self.engine = engine
        self.anim = False
        self.hide_job = None
        self.dot_color = None
        self.rec_start = 0.0
        self.cut = self.done = 0
        self.title_note = ""

        r = self.root = tk.Tk()
        r.withdraw()
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.attributes("-alpha", 0.97)
        r.configure(bg="#3a3a3a")

        body = tk.Frame(r, bg=self.BG, padx=18, pady=12)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        head = tk.Frame(body, bg=self.BG)
        head.pack(fill="x")
        self.canvas = tk.Canvas(head, width=22, height=22, bg=self.BG, highlightthickness=0)
        self.canvas.pack(side="left")
        self.title = tk.Label(head, text="語音轉文字", bg=self.BG, fg="white",
                              font=("Microsoft JhengHei UI", 11, "bold"))
        self.title.pack(side="left", padx=(8, 0))

        self.lbl = tk.Label(body, text="", bg=self.BG, fg="#e6e6e6", justify="left", anchor="nw",
                            wraplength=520, height=3, width=1,
                            font=("Microsoft JhengHei UI", 13))
        self.lbl.pack(fill="x", pady=(10, 0))

        self.W = 560
        self.draw_dot("#ff2a2a", 8)
        r.update_idletasks()
        self.style_window()

        # 系統匣
        self.tray = pystray.Icon(APP_NAME, make_icon(False), f"語音轉文字 v{__version__} (Alt+R 開始)", self.build_menu())
        self.tray.run_detached()

        r.after(40, self.tick)
        threading.Thread(target=self.engine.warm_up, daemon=True).start()

    # ---- 選單 ----
    def build_menu(self):
        eng = self.engine

        def lang_item(name, code):
            return Item(name, lambda: eng.set_lang(code),
                        checked=lambda item: eng.lang == code, radio=True)

        def chunk_item(m):
            return Item(f"{m} 分鐘", lambda: eng.set_chunk_min(m),
                        checked=lambda item: eng.chunk_min == m, radio=True)

        return Menu(
            Item("開始 / 停止錄音", lambda: eng.toggle(), default=True),
            Item(f"開始錄音  (Alt+{START_KEY})", lambda: eng.start()),
            Item(f"停止並複製  (Alt+{STOP_KEY})", lambda: eng.stop()),
            Menu.SEPARATOR,
            Item(lambda item: f"分段長度：每 {eng.chunk_min} 分鐘", Menu(
                *[chunk_item(m) for m in CHUNK_PRESETS],
                Menu.SEPARATOR,
                Item("自訂分鐘數…", lambda: ui_queue.put(("ask_chunk", None))),
            )),
            Item("開啟錄音資料夾", lambda: ui_queue.put(("open_dir", None))),
            Item("辨識語言", Menu(*[lang_item(n, c) for n, c in LANGS])),
            Item("開機自動啟動", lambda: toggle_autostart(),
                 checked=lambda item: autostart_enabled()),
            Menu.SEPARATOR,
            Item(f"版本 v{__version__}", lambda: None, enabled=False),
            Item("結束", lambda: ui_queue.put(("quit", None))),
        )

    # ---- 視窗樣式：不搶焦點、圓角 ----
    def style_window(self):
        try:
            wid = self.root.winfo_id()
            hwnd = user32.GetParent(wid) or wid
            style = user32.GetWindowLongW(hwnd, -20)
            user32.SetWindowLongW(hwnd, -20, style | 0x08000000 | 0x00000080)  # NOACTIVATE | TOOLWINDOW
            pref = ctypes.c_int(2)  # 圓角 (Win11)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
        except Exception:
            pass

    def draw_dot(self, color, radius):
        c = self.canvas
        c.delete("all")
        c.create_oval(11 - radius, 11 - radius, 11 + radius, 11 + radius, fill=color, outline="")

    def show(self, lines=3):
        self.lbl.config(height=lines)
        if self.hide_job:
            self.root.after_cancel(self.hide_job)
            self.hide_job = None
        self.root.update_idletasks()
        h = self.root.winfo_reqheight()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{self.W}x{h}+{(sw - self.W) // 2}+{sh - h - 120}")
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.style_window()

    def hide_later(self, ms):
        if self.hide_job:
            self.root.after_cancel(self.hide_job)
        self.hide_job = self.root.after(ms, self.root.withdraw)

    def set_text(self, s, full=False):
        if full:
            self.lbl.config(text=s)
            return
        s = s.replace("\n", " ")
        if len(s) > PREVIEW_CHARS:
            s = "…" + s[-PREVIEW_CHARS:]
        self.lbl.config(text=s)

    def set_tray(self, recording):
        try:
            self.tray.icon = make_icon(recording)
            self.tray.title = "錄音中… (Alt+X 停止)" if recording else f"語音轉文字 v{__version__} (Alt+R 開始)"
        except Exception:
            pass

    # ---- 分段長度設定視窗 ----
    def ask_chunk(self):
        if getattr(self, "dlg", None) and self.dlg.winfo_exists():
            self.dlg.lift()
            self.dlg.focus_force()
            return
        d = self.dlg = tk.Toplevel(self.root)
        d.title("分段長度設定")
        d.resizable(False, False)
        d.attributes("-topmost", True)
        font = ("Microsoft JhengHei UI", 11)
        frm = tk.Frame(d, padx=20, pady=16)
        frm.pack()
        tk.Label(frm, text="錄音每隔幾分鐘自動切一段，送去轉成文字：", font=font).grid(row=0, column=0, columnspan=3, sticky="w")
        var = tk.StringVar(value=str(self.engine.chunk_min))
        spin = tk.Spinbox(frm, from_=1, to=240, width=6, textvariable=var, font=font, justify="center")
        spin.grid(row=1, column=0, pady=10, sticky="w")
        tk.Label(frm, text="分鐘 (1–240)", font=font).grid(row=1, column=1, sticky="w")
        tk.Label(frm, text="錄音中修改會從目前這一段起生效。\n錄音檔與逐字稿存在 recordings 資料夾。",
                 font=("Microsoft JhengHei UI", 9), fg="#666", justify="left").grid(row=2, column=0, columnspan=3, sticky="w")

        def ok(_=None):
            try:
                m = int(var.get())
                if not 1 <= m <= 240:
                    raise ValueError
            except ValueError:
                spin.config(bg="#ffd6d6")
                return
            self.engine.set_chunk_min(m)
            d.destroy()

        btns = tk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=3, pady=(12, 0), sticky="e")
        tk.Button(btns, text="確定", width=8, command=ok, font=font).pack(side="left", padx=4)
        tk.Button(btns, text="取消", width=8, command=d.destroy, font=font).pack(side="left")
        d.bind("<Return>", ok)
        d.bind("<Escape>", lambda e: d.destroy())
        d.update_idletasks()
        sw, sh = d.winfo_screenwidth(), d.winfo_screenheight()
        d.geometry(f"+{(sw - d.winfo_reqwidth()) // 2}+{(sh - d.winfo_reqheight()) // 3}")
        d.focus_force()
        spin.focus_set()
        spin.selection_range(0, "end")

    def open_dir(self, path=None):
        path = path or self.engine.session_dir or REC_DIR
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)
        except Exception as e:
            log(f"open dir error: {e!r}")

    def rec_title(self):
        e = int(time.time() - self.rec_start)
        h, m, s = e // 3600, e % 3600 // 60, e % 60
        t = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        eng = self.engine
        return (f"錄音中 {t}　第 {eng.cut_count + 1} 段　已轉寫 {self.done}/{self.cut}"
                f"　(每 {eng.chunk_min} 分切段，Alt+X 停止)")

    # ---- 主迴圈 ----
    def tick(self):
        if self.anim:
            p = (math.sin(time.time() * 2.6) + 1) / 2
            self.draw_dot(f"#{int(105 + 150 * p):02x}1a1a", 6 + 3 * p)
            if not self.title_note:
                self.title.config(text=self.rec_title())
        try:
            while True:
                kind, data = ui_queue.get_nowait()
                self.handle(kind, data)
        except queue.Empty:
            pass
        self.root.after(40, self.tick)

    def handle(self, kind, data):
        if kind == "start":
            self.anim = True
            self.rec_start = time.time()
            self.cut = self.done = 0
            self.title_note = ""
            self.title.config(text=self.rec_title())
            self.set_text(f"錄音中，每 {self.engine.chunk_min} 分鐘自動切一段並在背景轉成文字…")
            self.set_tray(True)
            self.show()
        elif kind == "progress":
            self.cut, self.done, last = data
            if self.anim:
                if last:
                    self.set_text(f"第 {self.done} 段：{last}")
            else:
                self.title.config(text=f"已停止，轉寫剩下的段落中 ({self.done}/{self.cut})，請稍候…")
        elif kind == "note":
            if self.anim:
                self.title_note = data
            self.title.config(text=data)
        elif kind == "ready":
            self.title_note = ""
            if self.anim:
                self.title.config(text=self.rec_title())
        elif kind == "stopping":
            self.anim = False
            self.title_note = ""
            self.set_tray(False)
            self.draw_dot("#f1c40f", 8)
            self.title.config(text="已停止，轉寫剩下的段落中，請稍候…")
            self.show()
        elif kind == "busy":
            self.title.config(text="上一次錄音還在轉寫中，完成後才能再開始")
            self.show()
        elif kind == "done":
            full, folder, parts, timing = data
            self.anim = False
            self.set_tray(False)
            if full:
                ok = set_clipboard(full)
                self.draw_dot("#2ecc71", 8)
                self.title.config(text=f"✔ 完整逐字稿已複製 ({parts} 段)，Ctrl+V 貼上" if ok else "複製失敗")
                self.set_text(f"{timing['summary']}\n已存到：{os.path.join(folder, 'transcript.txt')}", full=True)
                self.show()
                self.hide_later(6000)
            else:
                self.draw_dot("#aaaaaa", 8)
                self.title.config(text="沒有辨識到內容 (錄音檔仍保留)")
                self.set_text(f"{timing['summary']}\n{folder}", full=True)
                self.show()
                self.hide_later(4000)
        elif kind == "ask_chunk":
            self.ask_chunk()
        elif kind == "open_dir":
            self.open_dir()
        elif kind == "error":
            self.anim = False
            self.set_tray(False)
            self.draw_dot("#f39c12", 8)
            self.title.config(text="無法錄音")
            self.set_text(data, full=True)
            self.show(7)
            self.hide_later(15000)
        elif kind == "quit":
            if self.engine.recording:          # 讓正在寫的 wav 正常收尾，錄音檔不會壞掉
                self.engine.stop()
                time.sleep(0.5)
            try:
                self.tray.stop()
            except Exception:
                pass
            self.root.destroy()
            os._exit(0)

    def run(self):
        self.root.mainloop()


# ----------------------------- 啟動 -----------------------------
def main():
    # 只允許一個執行個體
    kernel32.CreateMutexW(None, False, "Local\\VoiceToClipWhisperSingleton")
    if kernel32.GetLastError() == 183:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    log(f"=== VoiceToClipWhisper v{__version__} start (model={MODEL_SIZE}, lang={DEFAULT_LANG}) ===")
    engine = Engine()
    ui = Overlay(engine)
    threading.Thread(target=hotkey_thread, args=(engine,), daemon=True).start()
    ui.run()


if __name__ == "__main__":
    main()
