# -*- coding: utf-8 -*-
"""
語音轉文字小工具 - 離線 Whisper 版 (Windows 桌面程式，非網頁)
  Alt+R  開始錄音 + 即時轉文字（紅色呼吸燈）
  Alt+X  停止錄音，文字自動複製到剪貼簿 -> 到任何地方 Ctrl+V 貼上
  系統匣(右下角)圖示：左鍵切換錄音，右鍵選單可換語言 / 開機自動啟動 / 結束

使用 faster-whisper 在本機辨識，不需要網路 (第一次執行會下載模型)。
"""
import asyncio
import ctypes
import ctypes.wintypes as wt
import datetime
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
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
__version__ = "0.1.0"
APP_NAME = "VoiceToClipWhisper"
MODEL_SIZE = "small"           # tiny / base / small / medium  (越大越準越慢)
SR = 16000
SPEECH_RMS = 0.006              # 低於此音量視為安靜
PAUSE_SEC = 0.7                 # 安靜超過這麼久 -> 這一句結束
MAX_SEG_SEC = 20                # 一句最長秒數
PARTIAL_EVERY = 1.2             # 邊講邊更新的間隔(秒)
# ---------------------------------------------------------------

ui_queue: "queue.Queue" = queue.Queue()
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


FROZEN = getattr(sys, "frozen", False)
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "voice_debug.log")


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


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


class Engine:
    def __init__(self):
        self.lang = DEFAULT_LANG
        self.model = None
        self.model_lock = threading.Lock()
        self.recording = False
        self.finishing = False
        self.segments = []
        self.hyp = ""
        self.audio_q = queue.Queue()
        self.stream = None

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

    def display(self):
        return self.join(self.segments + ([self.hyp] if self.hyp else []))

    # ---- 模型 ----
    def load_model(self):
        with self.model_lock:
            if self.model is None:
                ui_queue.put(("note", "載入語音模型中(第一次需下載，請稍候)…"))
                from faster_whisper import WhisperModel
                self.model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
                log("model loaded")
        return self.model

    def warm_up(self):
        try:
            self.load_model()
        except Exception as e:
            log(f"warm_up error: {e!r}")

    def transcribe(self, audio, final):
        model = self.load_model()
        lang = None if self.lang == "auto" else self.lang.split("-")[0]
        segs, _ = model.transcribe(
            audio,
            language=lang,
            initial_prompt=PROMPTS.get(self.lang),
            beam_size=5 if final else 1,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        text = self.join([x.text.strip() for x in segs]).strip()
        log(f"{'final' if final else 'partial'}: {text}")
        return text

    # ---- 開始 / 停止 ----
    def toggle(self):
        if self.recording:
            self.stop()
        else:
            self.start()

    def start(self):
        if self.recording or self.finishing:
            return
        self.recording = True
        self.segments = []
        self.hyp = ""
        self.audio_q = queue.Queue()
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

    def _run(self):
        q = self.audio_q

        def cb(indata, frames, t, status):
            q.put(indata[:, 0].copy())

        try:
            # 先開麥克風，載入模型期間的聲音先排隊，不會漏掉
            self.stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32",
                                         blocksize=int(SR * 0.1), callback=cb)
            self.stream.start()
        except Exception as e:
            log(f"mic error: {e!r}")
            self._fail("無法開啟麥克風\n請確認已接上麥克風，並在「設定 > 隱私權與安全性 > 麥克風」允許桌面應用程式存取\n" + str(e))
            return
        try:
            self.load_model()
            ui_queue.put(("ready", None))
            self._loop(q)
        except Exception as e:
            log(f"run error: {e!r}")
            self._fail(f"辨識失敗：{e}\n(第一次執行需要網路下載模型)")

    def _loop(self, q):
        buf, n = [], 0
        speech, silent, since = False, 0.0, 0.0
        stop = False
        while not stop:
            chunks = [q.get()]
            while True:
                try:
                    chunks.append(q.get_nowait())
                except queue.Empty:
                    break
            for c in chunks:
                if c is None:
                    stop = True
                    break
                buf.append(c)
                n += len(c)
                dur = len(c) / SR
                if float(np.sqrt(np.mean(c * c))) > SPEECH_RMS:
                    speech, silent = True, 0.0
                else:
                    silent += dur
                since += dur
            if not speech:
                if n > SR * 2:                      # 還沒開口就只保留最後 1 秒
                    buf = [np.concatenate(buf)[-SR:]]
                    n = len(buf[0])
                continue
            audio = np.concatenate(buf)
            final = stop or silent >= PAUSE_SEC or len(audio) > SR * MAX_SEG_SEC
            if final:
                text = self.transcribe(audio, True)
                if text:
                    self.segments.append(text)
                self.hyp = ""
                buf, n, speech, silent, since = [], 0, False, 0.0, 0.0
                ui_queue.put(("text", self.display()))
            elif since >= PARTIAL_EVERY:
                self.hyp = self.transcribe(audio, False)
                since = 0.0
                ui_queue.put(("text", self.display()))
        self.finishing = False
        ui_queue.put(("done", self.display().strip()))

    def stop(self):
        if not self.recording:
            return
        self.recording = False
        self.finishing = True
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

        return Menu(
            Item("開始 / 停止錄音", lambda: eng.toggle(), default=True),
            Item(f"開始錄音  (Alt+{START_KEY})", lambda: eng.start()),
            Item(f"停止並複製  (Alt+{STOP_KEY})", lambda: eng.stop()),
            Menu.SEPARATOR,
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

    # ---- 主迴圈 ----
    def tick(self):
        if self.anim:
            p = (math.sin(time.time() * 2.6) + 1) / 2
            self.draw_dot(f"#{int(105 + 150 * p):02x}1a1a", 6 + 3 * p)
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
            self.title.config(text="錄音中…  按 Alt+X 停止並複製")
            self.set_text("請開始說話…")
            self.set_tray(True)
            self.show()
        elif kind == "text":
            if self.anim:
                self.set_text(data)
        elif kind == "state":
            if self.anim:
                self.title.config(text=f"錄音中…  [{data}]  Alt+X 停止")
        elif kind == "note":
            self.title.config(text=data)
        elif kind == "ready":
            if self.anim:
                self.title.config(text="錄音中…  按 Alt+X 停止並複製")
        elif kind == "done":
            self.anim = False
            self.set_tray(False)
            if data:
                ok = set_clipboard(data)
                self.draw_dot("#2ecc71", 8)
                self.title.config(text="✔ 已複製，請按 Ctrl+V 貼上" if ok else "複製失敗")
                self.set_text(data)
                self.show()
                self.hide_later(3500)
            else:
                self.draw_dot("#aaaaaa", 8)
                self.title.config(text="沒有辨識到內容")
                self.set_text("")
                self.show()
                self.hide_later(2000)
        elif kind == "error":
            self.anim = False
            self.set_tray(False)
            self.draw_dot("#f39c12", 8)
            self.title.config(text="無法錄音")
            self.set_text(data, full=True)
            self.show(7)
            self.hide_later(15000)
        elif kind == "quit":
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
