# -*- coding: utf-8 -*-
"""
語音轉文字小工具 - 離線 Whisper 版 (Windows 桌面程式，非網頁)
  Alt+R  開始錄音（紅色呼吸燈）。每 N 分鐘(預設 10)自動切一段存成 wav，
         背景同步轉成文字，錄音不中斷，繼續錄下一段
  Alt+X  停止錄音，最後一段(不足 N 分鐘也算)轉完後，合併成完整逐字稿，
         複製到剪貼簿 -> 到任何地方 Ctrl+V 貼上
  錄音檔與逐字稿都留在 recordings/<日期_時間>/ (part_001.wav/.txt …, transcript.txt)
  系統匣(右下角)圖示：左鍵切換錄音，右鍵選單可設定分段長度 / 開啟錄音資料夾 /
  換語言 / 會議模式 / 開機自動啟動 / 結束
  會議模式開啟時，Alt+X 後再分辨說話者(離線)並勘誤(詞庫；設定 API 金鑰後加上 Claude)，
  複製到剪貼簿的是整理好的版本，見 postprocess.py
  音源可選「麥克風」或「電腦播放的聲音」(喇叭輸出，WASAPI loopback，需 soundcard 套件)；
  開啟「即時寫入記事本」時，每轉好一小片就直接插入記事本尾端(不模擬鍵盤、不搶焦點)

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

import postprocess

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
__version__ = "0.4.2"
APP_NAME = "VoiceToClipWhisper"
MODEL_SIZE = "small"           # tiny / base / small / medium  (越大越準越慢)
SR = 16000
SPEECH_RMS = 0.006              # 低於此音量視為安靜
DEFAULT_CHUNK_MIN = 10          # 預設每幾分鐘切一段錄音送去轉文字
CHUNK_PRESETS = [1, 3, 5, 10, 15, 20, 30]   # 系統匣選單的快速選項(分鐘)
CUT_GRACE_SEC = 5               # 時間到後最多再等幾秒，找安靜的瞬間再切，避免切在字中間
SLICE_SEC = 20                  # 錄音中滿幾秒後，找安靜瞬間把一小片送去背景轉寫(停止後只剩最後一小片要等)
SLICE_MAX_SEC = 29              # 一小片最長幾秒就硬切；Whisper 以 30 秒為一個窗口，超過會多算一整窗、反而變慢
NOTEPAD_SLICE_SEC = 8           # 「即時寫入記事本」開啟時改用較短的小片，文字較快出現(稍微犧牲準確度)
SOURCES = [("麥克風", "mic"), ("電腦播放的聲音 (喇叭輸出)", "loopback")]
# ---------------------------------------------------------------

ui_queue: "queue.Queue" = queue.Queue()
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


FROZEN = getattr(sys, "frozen", False)
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "voice_debug.log")
REC_DIR = os.path.join(BASE_DIR, "recordings")          # 每次錄音一個子資料夾
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")
SPEAKER_MODEL_DIR = os.path.join(BASE_DIR, "models", "speaker")
GLOSSARY_PATH = os.path.join(BASE_DIR, "glossary.txt")


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


# ----------------------------- 即時寫入記事本 -----------------------------
EM_GETSEL, EM_SETSEL, EM_REPLACESEL, WM_GETTEXTLENGTH = 0x00B0, 0x00B1, 0x00C2, 0x000E
_WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


class NotepadWriter:
    """把文字直接插入記事本的文字框尾端(EM_REPLACESEL)，不模擬鍵盤、不搶焦點：
    你在別的視窗打字也不會被干擾，記事本不必在最前面。
    Windows 11 新版記事本的文字框類別是 RichEditD2DPT，舊版是 Edit"""
    EDIT_CLASSES = ("RichEditD2DPT", "Edit")

    def __init__(self):
        self.hwnd = None            # 記事本主視窗

    @staticmethod
    def _class(hwnd):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buf, 64)
        return buf.value

    def _find_main(self):
        h = user32.FindWindowW("Notepad", None)
        return h or None

    def open(self):
        """找現有的記事本；沒有就開一個新的。回傳是否成功"""
        if self.hwnd and user32.IsWindow(self.hwnd):
            return True
        self.hwnd = self._find_main()
        if not self.hwnd:
            import subprocess
            subprocess.Popen(["notepad.exe"])
            for _ in range(50):              # 最多等 5 秒
                time.sleep(0.1)
                self.hwnd = self._find_main()
                if self.hwnd and self._edit():
                    break
        return bool(self.hwnd)

    def _edit(self):
        """目前顯示中的文字框(新版記事本有分頁，只取看得到的那個)"""
        found = []

        def cb(h, _):
            if self._class(h) in self.EDIT_CLASSES and user32.IsWindowVisible(h):
                found.append(h)
                return False
            return True

        user32.EnumChildWindows(self.hwnd, _WNDENUMPROC(cb), 0)
        return found[0] if found else None

    def write(self, text):
        if not text:
            return True
        if not self.open():
            return False
        edit = self._edit()
        if not edit:
            log(f"notepad: no edit control (class of top={self._class(self.hwnd)})")
            return False
        n = user32.SendMessageW(edit, WM_GETTEXTLENGTH, 0, 0)
        user32.SendMessageW(edit, EM_SETSEL, n, n)                 # 游標移到最後
        buf = ctypes.create_unicode_buffer(text)                   # argtypes 是 LPARAM(整數)，要傳位址
        user32.SendMessageW(edit, EM_REPLACESEL, 1, ctypes.addressof(buf))
        return True


user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.FindWindowW.restype = wt.HWND
user32.EnumChildWindows.argtypes = [wt.HWND, _WNDENUMPROC, wt.LPARAM]


# ----------------------------- 電腦播放的聲音 (WASAPI loopback) -----------------------------
def loopback_capture(push, stop_event, on_open):
    """錄「預設播放裝置」正在播出的聲音，每 0.1 秒以 16 kHz 單聲道 float32 呼叫 push(chunk)。
    在自己的執行緒裡跑；開啟失敗會丟例外。用 soundcard 套件(sounddevice 不支援 loopback)"""
    # soundcard 第一次 import 時會在「當下這條執行緒」初始化 COM，而且把「已初始化過」(S_FALSE)當成錯誤
    # (Error 0x100000001)。所以第一次讓它自己來；之後每次錄音是新的執行緒、套件已載入，才由我們初始化
    if "soundcard" in sys.modules:
        ctypes.windll.ole32.CoInitializeEx(None, 0)   # COINIT_MULTITHREADED
    import warnings
    warnings.filterwarnings("ignore", message=".*discontinuity.*")
    import soundcard as sc
    spk = sc.default_speaker()
    mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
    log(f"loopback device: {spk.name}")
    block = int(SR * 0.1)
    with mic.recorder(samplerate=SR, blocksize=block) as rec:
        on_open()
        while not stop_event.is_set():
            data = rec.record(numframes=block)
            if data.ndim > 1:
                data = data.mean(axis=1)
            push(data.astype(np.float32))


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
# Whisper 把提示詞當成「前面已經說過的話」，不是指令；寫成「請用繁體…」這種句子，
# 聲音很短或很安靜時會被原封不動抄進結果。所以這裡放的是「風格範例」：繁體中文夾英文、有標點
PROMPTS = {
    "auto": "好的，我們今天先看一下這個 project 的 schedule，然後再討論 API 的部分。",
    "zh-TW": "好的，我們今天先討論一下這個問題，然後再決定下一步。",
    "zh-CN": "以下是简体中文的句子。",
}

_CLAUSE_SPLIT = re.compile(r"[，,。.、；;！!？?]+")     # 不以空白分句，否則中英混合句會被拆成單字


def strip_prompt_echo(text, prompt):
    """保險：把結果裡被照抄的提示詞子句刪掉(只刪 6 字以上的整句，避免誤刪正常內容)"""
    if not prompt or not text:
        return text
    for clause in _CLAUSE_SPLIT.split(prompt):
        if len(clause) >= 6 and clause in text:
            text = re.sub(re.escape(clause) + r"[，,。.、；;！!？?\s]*", "", text)
    text = re.sub(r"[，,、﹑]+([。！？])", r"\1", text)      # 「，，。」→「。」
    text = re.sub(r"([，,、﹑])[，,、﹑]+", r"\1", text)      # 「，，」→「，」
    return text.strip(" ，,、﹑")


_CJK = r"㐀-鿿豈-﫿"
_SMALL_PUNCT = str.maketrans({"﹐": "，", "﹑": "，", "﹌": "，", "﹒": "。", "﹔": "；", "﹕": "：", "﹖": "？", "﹗": "！"})
_HALF_TO_FULL = {",": "，", "?": "？", "!": "！", ";": "；", ":": "：", ".": "。"}
_HALF_NEAR_CJK = re.compile(rf"(?<=[{_CJK}])\s*([,?!;:]|\.(?!\.))\s*|\s*([,?!;:])\s*(?=[{_CJK}])")
_SPACE_IN_CJK = re.compile(rf"(?<=[{_CJK}，。、？！；：])\s+(?=[{_CJK}，。、？！；：])")


def normalize_punct(text):
    """實測(2026-11-10 會議)：Whisper 對中文會混用 ﹐﹑﹌、半形 ,? 與「- 」對話記號，統一成全形標點。
    只改緊鄰中文字的半形標點，英文句子與 3.14、... 不受影響"""
    if not text:
        return text
    text = text.translate(_SMALL_PUNCT).replace("�", "")      # 半個中文字的位元組被切開會變成 �
    text = re.sub(r"(?:^|(?<=[\s，。、？！；：,.?!;:]))-\s+", "", text)   # 「- 對,對」行首/句後的對話記號
    text = _HALF_NEAR_CJK.sub(lambda m: _HALF_TO_FULL[m.group(1) or m.group(2)], text)
    text = _SPACE_IN_CJK.sub("", text)
    text = re.sub(r"([，、])[，、]+", r"\1", text)
    return text.strip()


_REPEAT_RE = re.compile(r"(\S.{1,40}?)(?:[\s,，、。.]*\1){3,}")


def collapse_repeats(text):
    """保險：同一片語連續出現 4 次以上(Whisper 幻覺)只留一次"""
    return _REPEAT_RE.sub(r"\1", text)


class Engine:
    """錄音與轉寫分兩條執行緒同時進行：
    錄音執行緒 -> 每 chunk_min 分鐘把錄音存成 part_NNN.wav，接著錄下一段；
                  同時每 SLICE_SEC～SLICE_MAX_SEC 秒(找安靜瞬間)把記憶體裡的一小片聲音丟進 parts_q
    轉寫執行緒 -> 依序轉寫每一小片，同一段的小片接起來寫成 part_NNN.txt；停止後合併成 transcript.txt
    這樣按下停止時只剩最後一小片(<= SLICE_MAX_SEC 秒)要轉，不必等整段
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
        settings = load_settings()
        try:
            self.chunk_min = max(1, int(settings.get("chunk_min", DEFAULT_CHUNK_MIN)))
        except (TypeError, ValueError):
            self.chunk_min = DEFAULT_CHUNK_MIN
        self.meeting = bool(settings.get("meeting_mode", False))
        # 刻意只讀使用者在選單填入的金鑰、不讀環境變數：逐字稿要不要送出去必須是明確的選擇
        self.api_key = str(settings.get("anthropic_api_key", "")).strip()
        self.source = settings.get("source", "mic") if settings.get("source") in ("mic", "loopback") else "mic"
        self.to_notepad = bool(settings.get("to_notepad", False))
        self.notepad = NotepadWriter()
        self.lb_stop = threading.Event()

    def _save(self, key, value):
        s = load_settings()
        s[key] = value
        save_settings(s)
        log(f"{key} = {value}")

    def set_source(self, code):
        """錄音中不能換音源，下次開始錄音生效"""
        if not self.recording and not self.finishing:
            self.source = code
            self._save("source", code)

    def toggle_notepad(self):
        """錄音中也可切換，下一小片起生效"""
        self.to_notepad = not self.to_notepad
        self._save("to_notepad", self.to_notepad)
        if self.to_notepad and self.recording:
            threading.Thread(target=self.notepad.open, daemon=True).start()

    def set_chunk_min(self, minutes):
        """錄音中也可改，從目前這一段起生效"""
        self.chunk_min = max(1, min(240, int(minutes)))
        s = load_settings()
        s["chunk_min"] = self.chunk_min
        save_settings(s)
        log(f"chunk_min = {self.chunk_min}")

    def toggle_meeting(self):
        """錄音中也可切換，以按下 Alt+X 當時的狀態為準"""
        self.meeting = not self.meeting
        s = load_settings()
        s["meeting_mode"] = self.meeting
        save_settings(s)
        log(f"meeting_mode = {self.meeting}")

    def set_api_key(self, key):
        self.api_key = key.strip()
        s = load_settings()
        s["anthropic_api_key"] = self.api_key
        save_settings(s)
        log(f"ai correction {'enabled' if self.api_key else 'disabled'}")

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
        """回傳 (文字, [(開始秒, 結束秒, 句子), ...])，秒數相對於這片聲音的開頭"""
        prompt = PROMPTS.get(self.lang)
        text, segments = self._transcribe(audio, prompt)
        if not text and prompt:
            # 實測：小聲的英文配上中文提示詞，可能整段被判成「沒說話」而回傳空白；不帶提示詞再試一次
            text, segments = self._transcribe(audio, None)
            if text:
                log("empty with prompt, retried without prompt")
        return text, segments

    def _transcribe(self, audio, prompt):
        model = self.load_model()
        lang = None if self.lang == "auto" else self.lang.split("-")[0]
        segs, _ = model.transcribe(
            audio,
            language=lang,
            initial_prompt=prompt,
            beam_size=1,                # 5 較準但慢 2~3 倍；邊錄邊轉要跟得上錄音速度
            # 不可固定成 0.0：輸出重複太嚴重(壓縮率 > 2.4)時，要靠提高 temperature 重新解碼，
            # 否則會出現 "the 6th, the 6th, the 6th…" 這種無限重複的幻覺。
            # 但最多只重解 2 次：實測(2026-11-10 會議)6 段重試讓 20 秒的聲音轉了 96～180 秒，整場拖慢 7 分鐘
            temperature=[0.0, 0.4, 0.8],
            compression_ratio_threshold=2.4,
            condition_on_previous_text=False,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        segs = list(segs)
        text = normalize_punct(collapse_repeats(self.join([x.text.strip() for x in segs]).strip()))
        segments = [(x.start, x.end, strip_prompt_echo(normalize_punct(collapse_repeats(x.text.strip())), prompt))
                    for x in segs]
        return strip_prompt_echo(text, prompt), [x for x in segments if x[2]]

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
        self.audio_q = queue.Queue()
        self.parts_q = queue.Queue()
        log(f"session start: {self.session_dir} (每 {self.chunk_min} 分鐘一段)")
        ui_queue.put(("start", None))
        beep(880)
        threading.Thread(target=self._run, daemon=True).start()

    def _fail(self, msg):
        self.recording = False
        self.finishing = False
        self.lb_stop.set()
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

        if self.to_notepad:
            threading.Thread(target=self.notepad.open, daemon=True).start()
        if self.source == "loopback":
            if not self._start_loopback(q):
                return
        else:
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

    def _start_loopback(self, q):
        """開背景執行緒錄喇叭輸出；等它真的開好(或失敗)才回來。回傳是否成功"""
        self.lb_stop = stop = threading.Event()
        opened, err = threading.Event(), []

        def worker():
            try:
                loopback_capture(q.put, stop, opened.set)
            except Exception as e:
                log(f"loopback error: {e!r}")
                err.append(e)
                if opened.is_set() and self.recording:
                    ui_queue.put(("note", f"錄電腦聲音中斷：{e}"))
                    self.stop()                 # 已錄的部分照常收尾、轉寫
            finally:
                opened.set()

        threading.Thread(target=worker, daemon=True).start()
        opened.wait(10)
        if err or not opened.is_set():
            e = err[0] if err else "逾時"
            if isinstance(e, ImportError):
                msg = "缺少 soundcard 套件，無法錄電腦播放的聲音\n請重新執行 start_whisper.bat 或 build_exe.bat 安裝"
            else:
                msg = f"無法錄電腦播放的聲音\n請確認有預設的播放裝置(喇叭/耳機)\n{e}"
            self._fail(msg)
            return False
        return True

    def _record_loop(self, q):
        """把麥克風聲音寫進 part_NNN.wav；時間到就收尾這一段、立刻開下一段。
        另外每 SLICE_SEC～SLICE_MAX_SEC 秒把累積的聲音(一小片)交給轉寫執行緒，邊錄邊轉"""
        idx, wf, path, n = 0, None, "", 0
        buf, buf_n = [], 0                    # 還沒送去轉寫的聲音
        flushed_n = 0                         # 整場錄音已交出去的樣本數，用來換算每片的起始時間

        def flush(last):
            nonlocal buf, buf_n, flushed_n
            audio = np.concatenate(buf) if buf_n >= SR // 2 else None   # 不到 0.5 秒不轉
            start = flushed_n / SR
            flushed_n += buf_n
            buf, buf_n = [], 0
            if audio is not None or last:
                self.parts_q.put((idx, path, audio, last, start))

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
            flush(True)
            self._progress()

        open_part()
        while True:
            c = q.get()
            if c is None:
                break
            c = np.clip(c, -1.0, 1.0)
            wf.writeframes((c * 32767).astype(np.int16).tobytes())
            n += len(c)
            buf.append(c)
            buf_n += len(c)
            quiet = float(np.sqrt(np.mean(c * c))) < SPEECH_RMS
            limit = self.chunk_min * 60 * SR
            if n >= limit and (quiet or n >= limit + CUT_GRACE_SEC * SR):
                close_part()
                open_part()
            elif buf_n >= (NOTEPAD_SLICE_SEC if self.to_notepad else SLICE_SEC) * SR \
                    and (quiet or buf_n >= SLICE_MAX_SEC * SR):
                flush(False)
        close_part()
        self.parts_q.put(None)               # 告訴轉寫執行緒：沒有下一段了

    def _transcribe_worker(self, parts_q, session_dir):
        texts = []
        pieces = []                          # 目前這一段已轉好的小片文字
        segments = []                        # 整場錄音的句子 (開始秒, 結束秒, 文字)
        while True:
            item = parts_q.get()
            if item is None:
                break
            idx, path, audio, last, start = item
            if audio is not None:
                try:
                    t0 = time.time()
                    piece, segs = self.transcribe(audio)
                    segments += [(start + s, start + e, t) for s, e, t in segs]
                    log(f"part {idx} slice {len(audio) / SR:.1f}s -> {time.time() - t0:.1f}s")
                except Exception as e:
                    log(f"transcribe part {idx} error: {e!r}")
                    piece = f"[第 {idx} 段部分轉寫失敗：{e}，錄音檔 {os.path.basename(path)} 仍保留]"
                    segments.append((start, start + len(audio) / SR, piece))
                pieces.append(piece)
                self.last_text = piece or self.last_text
                if piece and self.to_notepad:
                    try:
                        if not self.notepad.write(piece + "\r\n"):
                            ui_queue.put(("note", "找不到記事本的文字框，文字仍會存檔並在停止後複製"))
                    except Exception as e:
                        log(f"notepad write error: {e!r}")
            if not last:
                self._progress()
                continue
            text = self.join(pieces).strip()
            pieces = []
            log(f"part {idx}: {text}")
            try:
                with open(path[:-4] + ".txt", "w", encoding="utf-8") as f:
                    f.write(text + "\n")
            except Exception as e:
                log(f"write part txt error: {e!r}")
            texts.append(text)
            self.done_count += 1
            self._progress()

        full = "\n\n".join(t for t in texts if t).strip()
        try:
            with open(os.path.join(session_dir, "transcript.txt"), "w", encoding="utf-8") as f:
                f.write(full + "\n")
        except Exception as e:
            log(f"write transcript error: {e!r}")
        result, fname, warning = full, "transcript.txt", ""
        if self.meeting and segments:
            result, fname, warning = self._post_process(session_dir, segments, full)
        self.finishing = False
        log(f"done: {len(texts)} parts -> {session_dir}")
        ui_queue.put(("done", (result, session_dir, len(texts), fname, warning)))

    def _post_process(self, session_dir, segments, full):
        """⑤ 分辨說話者 → transcript_v1.txt，⑥ 勘誤 → transcript_v2.txt。
        回傳 (要複製的文字, 檔名, 警告)。任何一步失敗都退回上一步的成果，逐字稿不會因此不見"""
        note = lambda s: ui_queue.put(("note", s))
        try:
            t0 = time.time()
            turns = postprocess.diarize(session_dir, SPEAKER_MODEL_DIR, note)
            speakers = postprocess.assign_speakers(segments, turns)
            lines = postprocess.build_lines(segments, speakers, self.join)
            v1 = postprocess.format_lines(lines)
            with open(os.path.join(session_dir, "transcript_v1.txt"), "w", encoding="utf-8") as f:
                f.write(v1 + "\n")
            log(f"diarize: {max(speakers) + 1} speakers, {len(lines)} lines, {time.time() - t0:.1f}s")
        except Exception as e:
            log(f"diarize error: {e!r}")
            return full, "transcript.txt", f"分辨說話者失敗，已複製未標記的逐字稿：{e}"
        try:
            terms, replacements = postprocess.load_glossary(GLOSSARY_PATH)
            for x in lines:
                x["text"] = postprocess.apply_glossary(x["text"], replacements)
            warning = ""
            if self.api_key:
                changed, rejected, warning = postprocess.ai_correct(lines, terms, self.api_key, note)
                log(f"ai correction: {changed} changed, {rejected} rejected, warning={warning!r}")
                if not warning:
                    try:     # 認不出名字只是少了名字，不影響已勘誤好的內容
                        mapping, warning = postprocess.ai_identify_speakers(lines, terms, self.api_key, note)
                        log(f"speaker names: {mapping}, warning={warning!r}")
                    except Exception as e:
                        log(f"speaker names error: {e!r}")
                        warning = f"辨識說話者名字失敗：{e}"
            v2 = postprocess.format_lines(lines)
            with open(os.path.join(session_dir, "transcript_v2.txt"), "w", encoding="utf-8") as f:
                f.write(v2 + "\n")
            return v2, "transcript_v2.txt", warning
        except Exception as e:
            log(f"correction error: {e!r}")
            return v1, "transcript_v1.txt", f"勘誤失敗，已複製未勘誤的版本：{e}"

    def stop(self):
        if not self.recording:
            return
        self.recording = False
        self.finishing = True
        log("stop requested")
        ui_queue.put(("stopping", None))
        beep(600)
        self.lb_stop.set()
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

        def source_item(name, code):
            return Item(name, lambda: eng.set_source(code),
                        checked=lambda item: eng.source == code, radio=True,
                        enabled=lambda item: not eng.recording and not eng.finishing)

        def chunk_item(m):
            return Item(f"{m} 分鐘", lambda: eng.set_chunk_min(m),
                        checked=lambda item: eng.chunk_min == m, radio=True)

        return Menu(
            Item("開始 / 停止錄音", lambda: eng.toggle(), default=True),
            Item(f"開始錄音  (Alt+{START_KEY})", lambda: eng.start()),
            Item(f"停止並複製  (Alt+{STOP_KEY})", lambda: eng.stop()),
            Menu.SEPARATOR,
            Item(lambda item: f"音源：{dict((c, n) for n, c in SOURCES)[eng.source]}",
                 Menu(*[source_item(n, c) for n, c in SOURCES])),
            Item("即時寫入記事本", lambda: eng.toggle_notepad(),
                 checked=lambda item: eng.to_notepad),
            Item(lambda item: f"分段長度：每 {eng.chunk_min} 分鐘", Menu(
                *[chunk_item(m) for m in CHUNK_PRESETS],
                Menu.SEPARATOR,
                Item("自訂分鐘數…", lambda: ui_queue.put(("ask_chunk", None))),
            )),
            Item("開啟錄音資料夾", lambda: ui_queue.put(("open_dir", None))),
            Item("辨識語言", Menu(*[lang_item(n, c) for n, c in LANGS])),
            Menu.SEPARATOR,
            Item("會議模式 (分辨說話者＋勘誤)", lambda: eng.toggle_meeting(),
                 checked=lambda item: eng.meeting),
            Item("編輯詞庫…", lambda: ui_queue.put(("open_glossary", None))),
            Item(lambda item: f"AI 勘誤金鑰… ({'已啟用' if eng.api_key else '未設定'})",
                 lambda: ui_queue.put(("ask_key", None))),
            Menu.SEPARATOR,
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

    # ---- AI 勘誤金鑰設定視窗 ----
    def ask_key(self):
        if getattr(self, "key_dlg", None) and self.key_dlg.winfo_exists():
            self.key_dlg.lift()
            self.key_dlg.focus_force()
            return
        d = self.key_dlg = tk.Toplevel(self.root)
        d.title("AI 勘誤金鑰")
        d.resizable(False, False)
        d.attributes("-topmost", True)
        font = ("Microsoft JhengHei UI", 11)
        frm = tk.Frame(d, padx=20, pady=16)
        frm.pack()
        tk.Label(frm, text="Anthropic API 金鑰：", font=font).pack(anchor="w")
        var = tk.StringVar(value=self.engine.api_key)
        entry = tk.Entry(frm, textvariable=var, width=46, show="•", font=font)
        entry.pack(pady=8)
        tk.Label(frm, justify="left", fg="#666", font=("Microsoft JhengHei UI", 9),
                 text="填入後，會議模式的逐字稿「文字」會傳送到 Anthropic 校對，並依用量計費；\n"
                      "聲音不會傳送。金鑰以明文存在本機 settings.json。\n"
                      "清除金鑰就恢復完全離線，只套用詞庫。").pack(anchor="w")

        def save(key):
            self.engine.set_api_key(key)
            d.destroy()

        btns = tk.Frame(frm)
        btns.pack(anchor="e", pady=(12, 0))
        tk.Button(btns, text="確定", width=8, command=lambda: save(var.get()), font=font).pack(side="left", padx=4)
        tk.Button(btns, text="清除金鑰", width=8, command=lambda: save(""), font=font).pack(side="left", padx=4)
        tk.Button(btns, text="取消", width=8, command=d.destroy, font=font).pack(side="left", padx=(4, 0))
        d.bind("<Return>", lambda e: save(var.get()))
        d.bind("<Escape>", lambda e: d.destroy())
        d.update_idletasks()
        sw, sh = d.winfo_screenwidth(), d.winfo_screenheight()
        d.geometry(f"+{(sw - d.winfo_reqwidth()) // 2}+{(sh - d.winfo_reqheight()) // 3}")
        d.focus_force()
        entry.focus_set()

    def open_glossary(self):
        try:
            postprocess.ensure_glossary(GLOSSARY_PATH)
            os.startfile(GLOSSARY_PATH)
        except Exception as e:
            log(f"open glossary error: {e!r}")

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
        what = "錄電腦聲音" if eng.source == "loopback" else "錄音中"
        return (f"{what} {t}　第 {eng.cut_count + 1} 段　已轉寫 {self.done}/{self.cut}"
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
            eng = self.engine
            src = "正在錄電腦播放的聲音" if eng.source == "loopback" else "錄音中"
            dst = "，文字會即時寫入記事本" if eng.to_notepad else f"，每 {eng.chunk_min} 分鐘自動切一段並在背景轉成文字"
            self.set_text(src + dst + "…")
            self.set_tray(True)
            self.show()
        elif kind == "progress":
            self.cut, self.done, last = data
            if self.anim:
                if last:
                    self.set_text(f"最新：{last}")
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
            full, folder, parts, fname, warning = data
            self.anim = False
            self.set_tray(False)
            if full:
                ok = set_clipboard(full)
                self.draw_dot("#2ecc71", 8)
                self.title.config(text=f"✔ 完整逐字稿已複製 ({parts} 段)，Ctrl+V 貼上" if ok else "複製失敗")
                saved = f"已存到：{os.path.join(folder, fname)}"
                self.set_text(f"{warning}\n{saved}" if warning else saved, full=bool(warning))
                self.show(4 if warning else 3)
                self.hide_later(15000 if warning else 6000)
            else:
                self.draw_dot("#aaaaaa", 8)
                self.title.config(text="沒有辨識到內容 (錄音檔仍保留)")
                self.set_text(folder)
                self.show()
                self.hide_later(4000)
        elif kind == "ask_chunk":
            self.ask_chunk()
        elif kind == "open_dir":
            self.open_dir()
        elif kind == "ask_key":
            self.ask_key()
        elif kind == "open_glossary":
            self.open_glossary()
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
