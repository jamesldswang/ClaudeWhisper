---
name: voice-to-clipboard-maintainer
description: Maintain, debug, and extend the Windows tray voice-to-text tool (voice_whisper.pyw): Alt+R/Alt+X global hotkeys, live offline Whisper transcription, floating overlay, clipboard output, PyInstaller packaging. Use when the user reports problems with this tool or asks to change its behavior.
---

# Voice-to-Clipboard (offline Whisper) — maintenance notes

A Windows desktop tray app (not a web app). Alt+R starts recording with a red breathing-dot overlay and live partial text; Alt+X stops, copies the full text to the clipboard, and the user pastes with Ctrl+V anywhere.

Primary file: `voice_whisper.pyw`. The user communicates in Traditional Chinese; reply in the language the user writes in.

## Project files

| File | Role |
|---|---|
| `voice_whisper.pyw` | The app (current, offline faster-whisper) |
| `start_whisper.bat` | Finds Python 3.11–3.13, creates `.venv`, installs `requirements_whisper.txt`, launches with `pythonw` |
| `build_exe.bat` | Kills running `VoiceWhisper.exe`, ensures `.venv`, runs PyInstaller `--onedir --noconsole` |
| `requirements_whisper.txt` | pystray, Pillow, numpy, sounddevice, faster-whisper |
| `voice_to_clip.pyw` (+ `start.bat`, `requirements.txt`) | Legacy version using Windows `SpeechRecognizer` via winrt. Obsolete (see History). Do not revive unless asked |
| `voice_debug.log` | Runtime log written next to the script/exe |

## Architecture (voice_whisper.pyw)

- **Threads**: Tk main loop (`Overlay`), a Win32 hotkey thread (`RegisterHotKey` ids 1 = start, 2 = stop, `MOD_ALT | MOD_NOREPEAT`), a recording worker thread per session, and the pystray tray icon (`run_detached`).
- **UI messaging**: all cross-thread UI updates go through `ui_queue` as `(kind, data)` tuples, drained every 40 ms in `Overlay.tick`. Kinds: `start`, `text`, `ready`, `note`, `state` (legacy, harmless), `done`, `error`, `quit`. Never touch Tk widgets from other threads.
- **Engine** (`class Engine`): `start()` / `stop()` / `toggle()` / `set_lang()` / `warm_up()`. State flags: `recording`, `finishing`.
  - `start()` sets state, queues `("start")`, spawns `_run`.
  - `_run` opens `sounddevice.InputStream` first (16 kHz mono float32, 100 ms blocks → `audio_q`), THEN loads the model, so audio spoken during first-time model loading is queued, not lost. Then `_loop`.
  - `_loop` segmentation: energy gate (`SPEECH_RMS`) + silence counter. A segment is finalized when silence ≥ `PAUSE_SEC`, length > `MAX_SEG_SEC`, or stop sentinel (`None`) arrives. Between finals, a partial transcription runs every `PARTIAL_EVERY` seconds on the growing buffer (beam 1); finals use beam 5. Before any speech, only the last ~1 s is kept.
  - `stop()` closes the stream and puts `None` in the queue; `_loop` transcribes the remainder, sets `finishing = False`, emits `("done", text)`. The Overlay's `done` handler copies to the clipboard (`set_clipboard`, CF_UNICODETEXT via ctypes).
  - `_fail(msg)` resets state and emits `("error", msg)`; use it for any failure in the worker.
- **Text joining** (`Engine.join`): Chinese/Japanese segments are concatenated without spaces; a space is inserted only between ASCII alphanumeric/punctuation boundaries so English phrases don't run together. `sep()` returns a space only for `en*` languages.
- **Transcription** (`Engine.transcribe`): `faster_whisper.WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")`, `vad_filter=True`, `condition_on_previous_text=False`, `temperature=0.0`, `initial_prompt` from `PROMPTS[lang]`. Language `auto` passes `language=None` (per-segment detection). A fixed `language="zh"` forces Chinese output and makes English get translated/transliterated — that was a past bug, so keep `auto` as default.
- **Overlay**: borderless, topmost, `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` so it never steals focus; rounded corners via DWM attribute 33. Shows the last `PREVIEW_CHARS` characters. Errors show with `show(7)` for 15 s.
- **Single instance**: named mutex `Local\VoiceToClipWhisperSingleton` (error 183 → exit). A second launch silently exits, which can look like "nothing happens".
- **Autostart**: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, value `VoiceToClipWhisper`. Frozen (PyInstaller) builds register `sys.executable`; script builds register `pythonw.exe "<script>"`.
- **Frozen awareness**: `FROZEN`, `BASE_DIR` (exe folder when frozen) are used for `LOG_PATH`. Preserve this when touching paths.

## Config constants (top of file)

`DEFAULT_LANG="auto"`, `LANGS`, `START_KEY="R"`, `STOP_KEY="X"`, `PREVIEW_CHARS=72`, `APP_NAME`, `MODEL_SIZE="small"`, `SR=16000`, `SPEECH_RMS=0.006`, `PAUSE_SEC=0.7`, `MAX_SEG_SEC=20`, `PARTIAL_EVERY=1.2`.

## Common tasks

- **Faster / lighter**: `MODEL_SIZE = "base"` or `"tiny"`. **More accurate**: `"medium"`.
- **Change hotkeys**: edit `START_KEY` / `STOP_KEY` (still combined with Alt). If registration fails the overlay shows an "already in use" error.
- **Add a language**: append `(label, code)` to `LANGS` (code like `ko-KR`; Whisper uses the part before `-`). Optionally add a `PROMPTS` entry; adjust `sep()` if the language uses spaces.
- **Force Traditional Chinese output**: Whisper is only steered by the prompt and may emit Simplified. A fix would be post-processing with OpenCC `s2twp` (add `opencc-python-reimplemented` to requirements and PyInstaller `--collect-all`).
- **Auto-paste after stop**: not implemented. Would require sending Ctrl+V (e.g. `SendInput`) to the previously focused window; the overlay is already non-activating so focus is preserved. Confirm with the user first since it writes into their active app.
- **Rebuild exe**: run `build_exe.bat`. Output `dist\VoiceWhisper\VoiceWhisper.exe`; the whole folder must stay together. Required PyInstaller flags: `--collect-all faster_whisper ctranslate2 onnxruntime tokenizers av _sounddevice_data huggingface_hub` and `--hidden-import pystray._win32`. The model is NOT bundled; it downloads to the HuggingFace cache on first use.

## Troubleshooting playbook

| Symptom | Likely cause / action |
|---|---|
| Stuck on "載入語音模型中" | First-run download (needs network). Check `voice_debug.log` for `model loaded` or errors |
| Recording starts but no text | Check mic selection/volume and Windows mic privacy; lower `SPEECH_RMS`; read log lines `partial:` / `final:` |
| English output in Chinese | Language not `auto`; ensure the tray language is auto-detect or English |
| Repeated phrases | Whisper hallucination at pauses; raise `PAUSE_SEC`, consider filtering identical consecutive segments |
| Hotkey does nothing | Another instance already running (mutex), or key conflict; check tray icon |
| Build: "存取被拒 / PermissionError ... .pyd" | Old `VoiceWhisper.exe` still running or AV scanning; kill process, delete `dist`/`build`, retry |
| exe: "Failed to load Python DLL" | User opened `build\VoiceWhisper\VoiceWhisper.exe`; the real one is in `dist\VoiceWhisper\` |
| `.bat` can't find Python | Needs 3.11–3.13. The script checks `py -3.x`, `%USERPROFILE%\.pyenv\pyenv-win\versions\3.x*`, `%LOCALAPPDATA%\Programs\Python\Python3xx`, then `python` on PATH. Delete stale `.venv` if created with a wrong version |
| `ModuleNotFoundError` in a built exe | Packages were not installed in the Python that ran PyInstaller; always build via `build_exe.bat` (uses `.venv`) |

## Batch-file pitfalls learned (cmd.exe)

- `for %%P in (3.12*)` with wildcards matches **files in the current directory**; put the wildcard inside `for /d %%D in ("...\%%P*")` instead.
- cmd does not support `[abc]` character classes in wildcards.
- An unescaped `)` inside an `echo` within a parenthesized block closes the block early; avoid parentheses in echoed text there.
- Use `chcp 65001` for UTF-8 Chinese output, and write batch files with CRLF line endings.

## History / decisions

1. First version used Windows `Windows.Media.SpeechRecognition` (winrt). On the user's machine it detected speech (`SPEECH_DETECTED`) but never raised `hypothesis_generated` / `result_generated`, even with online speech recognition enabled and zh-TW speech capability installed (`Language.Speech~~~zh-TW~0.0.1.0`). Python 3.14 also lacked reliable winrt support. Replaced by offline faster-whisper.
2. English being rendered as Chinese came from forcing `language="zh"`; fixed by an `auto` default with a mixed-language prompt.
3. Not verified by Claude on a real Windows machine for each edit: files are syntax-checked on Linux only. When changing code, say so, and ask the user to paste errors or `voice_debug.log`.

## Versioning

- Version lives in `__version__` at the top of `voice_whisper.pyw` (currently `0.1.0`). It is shown in the tray tooltip, the tray menu (disabled "版本" item), and the first `voice_debug.log` line of each run.
- Every behavior change: bump `__version__` (patch for fixes, minor for features) and add an entry to `CHANGELOG.md` (新增 / 修正 / 變更). Update the version mentioned in `README.md` and `USER_GUIDE.md` too.
- Source control is git (target repo `https://github.com/jamesldswang/ClaudeWhisper.git`). Claude could not push from the sandbox unless the user links GitHub under Settings → Connectors; otherwise give the user the git commands. Commit messages written by Claude end with the attribution lines required by the session.

## Working rules for edits

- Keep all UI changes on the Tk thread via `ui_queue`.
- Wrap every worker-thread failure in `_fail(...)` so `recording`/`finishing` never stay stuck true.
- Preserve the "open mic first, load model second" order.
- Do not log or transmit audio; the log may contain recognized text, so mention that when asking users to share it.
- After edits: `python -m py_compile voice_whisper.pyw`, then deliver the updated file and tell the user how to restart (quit from tray first; rebuild if using the exe).
