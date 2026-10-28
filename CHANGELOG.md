# 更新紀錄

版本號格式：`主版本.次版本.修訂`。每次修改程式時，請同步更新 `voice_whisper.pyw` 裡的 `__version__` 與這份檔案。

## [0.1.0] - 2026-10-28

第一個有版本號的版本，整理自先前所有的修改。

### 新增
- 離線 Whisper 版（`voice_whisper.pyw`）：使用 faster-whisper 在本機辨識，不需要網路（第一次需下載模型）。
- Alt+R 開始錄音、Alt+X 停止並複製到剪貼簿，Ctrl+V 貼上。
- 紅色呼吸燈浮動視窗，邊講邊顯示文字；視窗不搶輸入焦點。
- 系統匣圖示與右鍵選單：切換辨識語言、開機自動啟動、顯示版本、結束。
- 辨識語言：自動偵測（中英混合）、繁體中文、简体中文、English (US)、日本語。
- `start_whisper.bat`：自動尋找 Python 3.11–3.13（含 pyenv）、建立 `.venv`、安裝套件並啟動。
- `build_exe.bat`：以 PyInstaller `--onedir` 打包成 exe，打包前自動關閉執行中的舊程式。
- `voice_debug.log` 診斷記錄；啟動時寫入版本號。
- 文件：`README.md`、`USER_GUIDE.md`、`SKILL.md`、`.gitignore`。

### 修正
- 中英混講時英文被翻成中文：預設語言改為自動偵測，並調整提示詞。
- 中文模式下英文句子之間沒有空格：加入智慧接句，只在英文／數字相接處補空格。
- 批次檔找不到 pyenv 的 Python：修正 cmd 萬用字元的寫法。
- 打包時「存取被拒」：打包前先結束舊的 `VoiceWhisper.exe`。

### 變更
- 放棄 Windows 內建 `SpeechRecognizer`：在測試的電腦上能偵測到說話，卻不回傳任何文字，改用離線 Whisper。舊版檔案（`voice_to_clip.pyw`、`start.bat`、`requirements.txt`）保留僅供參考，可刪除。
