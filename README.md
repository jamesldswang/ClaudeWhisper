# 語音轉文字小工具（離線 Whisper 版）

Windows 桌面常駐小程式（非網頁）。按 **Alt+R** 開始錄音並即時轉成文字，按 **Alt+X** 停止，文字自動複製到剪貼簿，到任何能貼文字的地方按 **Ctrl+V** 即可貼上。

辨識在本機執行（[faster-whisper](https://github.com/SYSTRAN/faster-whisper)），不需要網路，也不會把聲音傳到雲端。第一次使用時需要下載一次模型。

## 特色

- 全域快捷鍵：在任何程式裡都能按 Alt+R / Alt+X
- 紅色呼吸燈浮動視窗，邊講邊顯示轉出的文字
- 停止後自動複製到剪貼簿，Ctrl+V 貼上
- 中英混合自動偵測，也可固定繁中、簡中、英文、日文
- 常駐系統匣，可設定開機自動啟動
- 浮動視窗不搶輸入焦點，游標仍留在原本的輸入框

## 系統需求

- Windows 10 / 11
- Python 3.11 – 3.13（建議 3.12；3.14 的相容性不確定，不建議）
- 一支可用的麥克風
- 約 1 GB 磁碟空間（套件 + `small` 模型）
- 第一次執行需要網路下載模型，之後可離線

## 快速開始

1. 把專案檔案放在同一個資料夾。
2. 雙擊 `start_whisper.bat`。第一次會自動建立 `.venv` 並安裝套件。
3. 第一次按 Alt+R 時會下載語音模型（約 460 MB），請稍候。
4. 說話，按 Alt+X 停止，到任何地方按 Ctrl+V 貼上。

想要不依賴 Python 的版本，雙擊 `build_exe.bat` 打包，產物在 `dist\VoiceWhisper\VoiceWhisper.exe`（整個資料夾要一起保留）。

## 檔案說明

| 檔案 | 用途 |
|---|---|
| `voice_whisper.pyw` | 主程式（離線 Whisper 版） |
| `start_whisper.bat` | 一鍵啟動：自動找 Python、建 `.venv`、裝套件、背景啟動 |
| `requirements_whisper.txt` | 套件清單 |
| `build_exe.bat` | 打包成 exe（PyInstaller，`--onedir`） |
| `voice_to_clip.pyw`、`start.bat`、`requirements.txt` | 舊版：使用 Windows 內建語音辨識。在部分電腦上聽得到聲音卻沒有文字，已由 Whisper 版取代，可刪除 |
| `voice_debug.log` | 執行後自動產生的診斷記錄 |
| `USER_GUIDE.md` | 使用手冊 |
| `CHANGELOG.md` | 更新紀錄（目前版本 0.1.0） |
| `SKILL.md` | 給 Claude 的維護說明 |

## 可調整的設定

在 `voice_whisper.pyw` 開頭：

| 設定 | 預設 | 說明 |
|---|---|---|
| `MODEL_SIZE` | `small` | `tiny` / `base` / `small` / `medium`，越大越準也越慢 |
| `START_KEY` / `STOP_KEY` | `R` / `X` | 快捷鍵（皆搭配 Alt） |
| `PAUSE_SEC` | `0.7` | 停頓多久算一句結束 |
| `SPEECH_RMS` | `0.006` | 低於此音量視為安靜；麥克風音量小可調低 |
| `MAX_SEG_SEC` | `20` | 一句最長秒數 |
| `PARTIAL_EVERY` | `1.2` | 邊講邊更新文字的間隔（秒） |
| `DEFAULT_LANG` | `auto` | 預設辨識語言 |

## 疑難排解

詳見 `USER_GUIDE.md` 的「常見問題」。最常見的幾項：

- **按 Alt+R 沒反應**：確認系統匣有麥克風圖示；快捷鍵可能被其他軟體佔用。
- **一直顯示載入模型**：第一次下載需要網路與時間。
- **講英文變成中文**：右鍵系統匣圖示，辨識語言選「自動偵測」或「English (US)」。
- **打包時「存取被拒」**：先結束執行中的 `VoiceWhisper.exe`，再重新打包。

## 隱私

所有辨識都在你的電腦上完成。程式只在第一次下載模型時連網，錄音內容不會上傳，也不會存成音檔。`voice_debug.log` 會記錄辨識出來的文字，如不想保留可隨時刪除。

## 授權與致謝

使用 faster-whisper、OpenAI Whisper 模型、pystray、sounddevice。各套件適用其各自的授權條款。
