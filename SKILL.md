---
name: voice-to-clipboard-maintainer
description: Maintain, debug, and extend the Windows tray voice-to-text tool (voice_whisper.pyw): Alt+R/Alt+X global hotkeys, chunked recording (every N minutes) with background offline Whisper transcription, local wav + transcript files, floating overlay, clipboard output, PyInstaller packaging. Use when the user reports problems with this tool or asks to change its behavior.
---

# Voice-to-Clipboard (offline Whisper) — maintenance notes

A Windows desktop tray app (not a web app). Alt+R starts recording with a red breathing-dot overlay. Every N minutes (user-selectable, default 10) the recording is cut into `part_NNN.wav` and transcribed in the background while recording continues seamlessly into the next part. Alt+X stops; the last (shorter) part is transcribed too, all parts are merged into `transcript.txt`, the full transcript is copied to the clipboard, and the user pastes with Ctrl+V anywhere. Audio and text files stay in a local `recordings` folder. There is NO live partial text ("跑馬燈") any more: it was removed in v0.2.0 at the user's request.

Primary file: `voice_whisper.pyw`. The user communicates in Traditional Chinese; reply in the language the user writes in.

## Project files

| File | Role |
|---|---|
| `voice_whisper.pyw` | The app (current, offline faster-whisper) |
| `start_whisper.bat` | Finds Python 3.11–3.14, creates `.venv`, installs `requirements_whisper.txt`, launches with `pythonw` |
| `build_exe.bat` | Kills running `VoiceWhisper.exe`, ensures `.venv`, runs PyInstaller `--onedir --noconsole` |
| `requirements_whisper.txt` | pystray, Pillow, numpy, sounddevice, faster-whisper |
| `voice_to_clip.pyw` (+ `start.bat`, `requirements.txt`) | Legacy version using Windows `SpeechRecognizer` via winrt. Obsolete (see History). Do not revive unless asked |
| `voice_debug.log` | Runtime log written next to the script/exe |
| `recordings\<YYYY-MM-DD_HHMMSS>\` | One folder per session: `part_001.wav` + `part_001.txt`, … , `transcript.txt` (git-ignored; contains private audio) |
| `settings.json` | Persisted user settings, currently `{"chunk_min": 10}` (git-ignored) |

## Architecture (voice_whisper.pyw)

- **Threads**: Tk main loop (`Overlay`), a Win32 hotkey thread (`RegisterHotKey` ids 1 = start, 2 = stop, `MOD_ALT | MOD_NOREPEAT`), per session a recording thread (`_run` -> `_record_loop`) AND a transcription thread (`_transcribe_worker`), and the pystray tray icon (`run_detached`).
- **UI messaging**: all cross-thread UI updates go through `ui_queue` as `(kind, data)` tuples, drained every 40 ms in `Overlay.tick`. Kinds: `start`, `progress` `(cut_count, done_count, last_text)`, `note`, `ready`, `stopping`, `busy`, `done` `(full_text, session_dir, parts)`, `error`, `ask_chunk`, `open_dir`, `quit`. Never touch Tk widgets from other threads (the tray menu also only posts `ask_chunk` / `open_dir`).
- **Engine** (`class Engine`): `start()` / `stop()` / `toggle()` / `set_lang()` / `set_chunk_min()` / `warm_up()`. State flags: `recording`, `finishing`. Counters: `cut_count`, `done_count`, `last_text`. `chunk_min` is loaded from / saved to `settings.json`.
  - `start()` refuses while `finishing` (previous session still transcribing -> `busy`), creates `REC_DIR/<timestamp>` as `session_dir`, fresh `audio_q` + `parts_q`, queues `("start")`, spawns `_run`.
  - `_run` opens `sounddevice.InputStream` (16 kHz mono float32, 100 ms blocks -> `audio_q`), starts `_transcribe_worker(parts_q, session_dir)`, then runs `_record_loop`. Recording never waits for the model; the model loads lazily inside the worker (or earlier via `warm_up` at app start).
  - `_record_loop` streams every block straight into the open `part_NNN.wav` (stdlib `wave`, int16), so a crash loses at most the unflushed tail. When the part reaches `chunk_min * 60 * SR` samples it waits for a quiet block (RMS < `SPEECH_RMS`), at most `CUT_GRACE_SEC` extra, then closes the file, and immediately opens the next part. Independently, every block is also appended to an in-memory `buf`; once `buf` holds `SLICE_SEC` (20) s it is flushed at the next quiet block, or forcibly at `SLICE_MAX_SEC` (29) s (Whisper's window is 30 s, so going over costs a whole extra window). `flush(last)` puts `(idx, path, audio, last)` in `parts_q` (`audio=None` if < 0.5 s); closing a part flushes with `last=True`. `chunk_min` is re-read every block, so changing it mid-recording applies to the current part. On the stop sentinel (`None`) it closes the last part (deleted if < 0.5 s), then puts `None` in `parts_q`.
  - `_transcribe_worker` processes slices in order: `transcribe(audio)` (time logged as `part N slice Xs -> Ys`), collects pieces; on a `last=True` item it joins the pieces into `part_NNN.txt` and emits `progress`. A failing part is logged and replaced by a `[第 N 段轉寫失敗…]` marker; the wav is kept. After `None`, it joins non-empty part texts with blank lines into `transcript.txt`, sets `finishing = False`, emits `done`. The Overlay copies `full_text` to the clipboard (`set_clipboard`, CF_UNICODETEXT via ctypes).
  - `stop()` sets `recording=False, finishing=True`, emits `stopping`, closes the stream, puts `None` in `audio_q`.
  - `_fail(msg)` resets state and emits `("error", msg)`; use it for any failure in the worker.
- **Text joining** (`Engine.join`): Chinese/Japanese segments are concatenated without spaces; a space is inserted only between ASCII alphanumeric/punctuation boundaries so English phrases don't run together. `sep()` returns a space only for `en*` languages.
- **Transcription** (`Engine.transcribe`): `faster_whisper.WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")`, `beam_size=1` (since v0.2.4; 5 was 2-3x slower and could not keep up with live slicing), `vad_filter=True`, `condition_on_previous_text=False`, `temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0]` with `compression_ratio_threshold=2.4` (NEVER a single `0.0`: that disables the fallback re-decode and caused endless "the 6th, the 6th…" loops in v0.2.0), if the result is empty and a prompt was used, `transcribe()` retries once with `initial_prompt=None` (v0.2.2: quiet English audio + the Chinese `auto` prompt made Whisper drop the whole part as no-speech; gain normalization and `log_prob_threshold=None` did not help), output passed through `collapse_repeats()` (a phrase repeated 4+ times in a row is kept once) and `strip_prompt_echo()` (removes prompt clauses of 6+ chars copied into the result, collapses runs like `，，。`), `initial_prompt` from `PROMPTS[lang]`. PROMPTS must be natural example sentences (style: Traditional Chinese with English words, punctuation), NEVER instructions: Whisper treats the prompt as preceding transcript and copied '中文請用繁體，英文請照原文輸出' into short/quiet clips (v0.2.5). Language `auto` passes `language=None` (per-segment detection). A fixed `language="zh"` forces Chinese output and makes English get translated/transliterated — that was a past bug, so keep `auto` as default.
- **Overlay**: borderless, topmost, `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` so it never steals focus; rounded corners via DWM attribute 33. While recording, the title is refreshed every tick by `rec_title()` (elapsed time, current part number, transcribed/cut counts, chunk length); a `note` (e.g. model loading) temporarily overrides it until `ready`. The text line shows the latest finished slice (`最新：…`, last `PREVIEW_CHARS` chars). After `done` it shows the transcript path for 6 s. Errors show with `show(7)` for 15 s. Quitting from the tray while recording calls `stop()` first so the current wav header is finalized.
- **Chunk-length UI**: tray submenu `分段長度：每 N 分鐘` with radio presets `CHUNK_PRESETS` plus `自訂分鐘數…`, which opens `Overlay.ask_chunk()` (Tk `Toplevel` with a Spinbox 1–240, Enter = OK, Esc = cancel). Tray item `開啟錄音資料夾` opens the current session folder (or `recordings`) via `os.startfile`.
- **PyAV stub**: `load_model()` inserts an empty `av` module into `sys.modules` before importing faster_whisper. faster_whisper imports PyAV at import time but only uses it to decode files; we always pass numpy arrays. PyAV's unsigned `.pyd` is blocked by Windows Smart App Control, so do not remove the stub.
- **Single instance**: named mutex `Local\VoiceToClipWhisperSingleton` (error 183 → exit). A second launch silently exits, which can look like "nothing happens".
- **Autostart**: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, value `VoiceToClipWhisper`. Frozen (PyInstaller) builds register `sys.executable`; script builds register `pythonw.exe "<script>"`.
- **Frozen awareness**: `FROZEN`, `BASE_DIR` (exe folder when frozen) are used for `LOG_PATH`, `REC_DIR`, `SETTINGS_PATH`. Preserve this when touching paths.

## Config constants (top of file)

`DEFAULT_LANG="auto"`, `LANGS`, `START_KEY="R"`, `STOP_KEY="X"`, `PREVIEW_CHARS=72`, `APP_NAME`, `MODEL_SIZE="small"`, `SR=16000`, `SPEECH_RMS=0.006` (quiet threshold used to pick the cut point), `DEFAULT_CHUNK_MIN=10`, `CHUNK_PRESETS=[1,3,5,10,15,20,30]`, `CUT_GRACE_SEC=5`, `SLICE_SEC=20`, `SLICE_MAX_SEC=29`. (`PAUSE_SEC`, `MAX_SEG_SEC`, `PARTIAL_EVERY` were removed together with live partial text in v0.2.0.)

## Common tasks

- **Faster / lighter**: `MODEL_SIZE = "base"` or `"tiny"`. **More accurate**: `"medium"`.
- **Change default chunk length**: `DEFAULT_CHUNK_MIN` (only used when `settings.json` has no value); users normally change it from the tray menu.
- **Chunk boundaries**: a part ends at the first quiet 100 ms block after the limit, so words are rarely cut, but a sentence can still be split across two parts (each part is a separate paragraph in `transcript.txt`). Whisper handles long inputs (it windows internally), so 10–30 min parts are fine; very short chunks only add overhead.
- **Change hotkeys**: edit `START_KEY` / `STOP_KEY` (still combined with Alt). If registration fails the overlay shows an "already in use" error.
- **Add a language**: append `(label, code)` to `LANGS` (code like `ko-KR`; Whisper uses the part before `-`). Optionally add a `PROMPTS` entry; adjust `sep()` if the language uses spaces.
- **Force Traditional Chinese output**: Whisper is only steered by the prompt and may emit Simplified. A fix would be post-processing with OpenCC `s2twp` (add `opencc-python-reimplemented` to requirements and PyInstaller `--collect-all`).
- **Auto-paste after stop**: not implemented. Would require sending Ctrl+V (e.g. `SendInput`) to the previously focused window; the overlay is already non-activating so focus is preserved. Confirm with the user first since it writes into their active app.
- **Rebuild exe**: run `build_exe.bat`. Output `dist\VoiceWhisper\VoiceWhisper.exe`; the whole folder must stay together. Required PyInstaller flags: `--collect-all faster_whisper ctranslate2 onnxruntime tokenizers av _sounddevice_data huggingface_hub` and `--hidden-import pystray._win32`. The model is NOT bundled; it downloads to the HuggingFace cache on first use.

## Troubleshooting playbook

| Symptom | Likely cause / action |
|---|---|
| Stuck on "載入語音模型中" | First-run download (needs network). Check `voice_debug.log` for `model loaded` or errors |
| Recording starts but no text | Check mic selection/volume and Windows mic privacy; lower `SPEECH_RMS`; read log lines `cut part N` / `part N:` |
| English output in Chinese | Language not `auto`; ensure the tray language is auto-detect or English |
| Repeated phrases ("the 6th, the 6th, …") | Whisper hallucination loop. Keep the temperature fallback list + `compression_ratio_threshold`; `collapse_repeats()` is the backstop. Reproduce by re-transcribing the saved `part_NNN.wav` |
| Alt+R shows 「上一次錄音還在轉寫中」 | Previous session's parts are still being transcribed (`finishing`); wait for the green ✔ |
| A part transcribes empty or garbled although the user spoke | Mic input too quiet (check per-second RMS of the saved wav; ~0.002 RMS / 0.03 peak is too low). Ask the user to raise the mic level in Windows sound settings. The no-prompt retry covers the empty case |
| Transcription falls behind during recording | CPU slower than real time for `MODEL_SIZE`; parts queue up and finish after Alt+X. Use `base`/`tiny` or accept the wait |
| `DLL load failed` / 「應用程式控制原則已封鎖此檔案」 in log | Windows Smart App Control blocks unsigned DLLs (`ctranslate2.dll` is required and cannot be stubbed). The user must turn SAC off (Windows 安全性 > 應用程式與瀏覽器控制 > 智慧型應用程式控制). Event log `Microsoft-Windows-CodeIntegrity/Operational` ids 3033/3077 name the blocked file. A stale instance whose model failed keeps the mutex, so kill old `pythonw.exe` before relaunching |
| Hotkey does nothing | Another instance already running (mutex), or key conflict; check tray icon |
| Build: "存取被拒 / PermissionError ... .pyd" | Old `VoiceWhisper.exe` still running or AV scanning; kill process, delete `dist`/`build`, retry |
| exe: "Failed to load Python DLL" | User opened `build\VoiceWhisper\VoiceWhisper.exe`; the real one is in `dist\VoiceWhisper\` |
| `.bat` can't find Python | Needs 3.11–3.14 (3.14 verified working on the user's machine, 2026-11-05). The script checks `py -3.x`, `%USERPROFILE%\.pyenv\pyenv-win\versions\3.x*`, `%LOCALAPPDATA%\Programs\Python\Python3xx`, `%ProgramFiles%\Python3xx`, then `python` on PATH. Delete stale `.venv` if created with a wrong version |
| `ModuleNotFoundError` in a built exe | Packages were not installed in the Python that ran PyInstaller; always build via `build_exe.bat` (uses `.venv`) |

## Batch-file pitfalls learned (cmd.exe)

- `for %%P in (3.12*)` with wildcards matches **files in the current directory**; put the wildcard inside `for /d %%D in ("...\%%P*")` instead.
- cmd does not support `[abc]` character classes in wildcards.
- An unescaped `)` inside an `echo` within a parenthesized block closes the block early; avoid parentheses in echoed text there.
- Use `chcp 65001` for UTF-8 Chinese output, and write batch files with CRLF line endings.

## History / decisions

1. First version used Windows `Windows.Media.SpeechRecognition` (winrt). On the user's machine it detected speech (`SPEECH_DETECTED`) but never raised `hypothesis_generated` / `result_generated`, even with online speech recognition enabled and zh-TW speech capability installed (`Language.Speech~~~zh-TW~0.0.1.0`). Python 3.14 also lacked reliable winrt support. Replaced by offline faster-whisper.
2. English being rendered as Chinese came from forcing `language="zh"`; fixed by an `auto` default with a mixed-language prompt.
3. Verification: Claude Code may run on the user's Windows machine with the project `.venv` and can test the pipeline headlessly (import the .pyw via `importlib.machinery.SourceFileLoader`, feed numpy blocks into `_record_loop`, generate test speech with PowerShell `System.Speech`), but cannot press hotkeys or hear the mic. When changing code, say what was and was not verified, and ask the user to paste errors or `voice_debug.log`.
4. v0.2.0 (2026-11-06): the user asked to drop the live partial text ("取消跑馬燈") and instead record in fixed-length chunks (default 10 min, chosen in a UI) that are transcribed in parallel with ongoing recording, keep all wav files locally, and merge everything into one transcript after Alt+X. The same day, Windows Smart App Control started blocking `av` and `ctranslate2` DLLs; `av` was stubbed, SAC was turned off by the user for `ctranslate2`.
5. v0.2.4 (2026-11-06): to shorten the wait after Alt+X, audio is transcribed in ~20 s slices while recording. Measured on the user's 8-core CPU (small, int8): a 3.4 s clip takes ~6 s with `language="zh"` and no VAD, ~8.6 s zh+VAD, ~12 s auto without VAD, ~19 s auto+VAD (fixed 30 s window + language detection + VAD overhead dominate, not clip length). The user chose to keep `auto` + VAD. A separate unmerged branch `worktree-timing-stats` (also labelled v0.2.3, timing.txt per session) exists; it must be renumbered if merged later.
6. v0.2.5 (2026-11-06): the user saw '請用繁體，請照原文…' in transcripts; it was the instruction-style prompt being echoed. Replaced with a natural example sentence plus `strip_prompt_echo()`. On the offending 31 s clip the old prompt produced only '請用繁體', the new one the real speech. Also seen once: a 20 s slice took 46.7 s (machine busy right after a PyInstaller build); if slices regularly take longer than real time, live slicing falls behind.

## Versioning

- Version lives in `__version__` at the top of `voice_whisper.pyw` (currently `0.2.5`). It is shown in the tray tooltip, the tray menu (disabled "版本" item), and the first `voice_debug.log` line of each run.
- Every behavior change: bump `__version__` (patch for fixes, minor for features) and add an entry to `CHANGELOG.md` (新增 / 修正 / 變更). Update the version mentioned in `README.md` and `USER_GUIDE.md` too.
- Source control is git (target repo `https://github.com/jamesldswang/ClaudeWhisper.git`). Claude could not push from the sandbox unless the user links GitHub under Settings → Connectors; otherwise give the user the git commands. Commit messages written by Claude end with the attribution lines required by the session.

## Working rules for edits

- Keep all UI changes on the Tk thread via `ui_queue`.
- Wrap every worker-thread failure in `_fail(...)` so `recording`/`finishing` never stay stuck true.
- Preserve the "open mic first, load model second" order.
- Do not log or transmit audio; the log may contain recognized text, so mention that when asking users to share it.
- After edits: `python -m py_compile voice_whisper.pyw`, then deliver the updated file and tell the user how to restart (quit from tray first; rebuild if using the exe).
