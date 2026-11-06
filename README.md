# 語音轉文字小工具（離線 Whisper 版）

Windows 桌面常駐小程式（非網頁）。按 **Alt+R** 開始錄音，每隔固定時間（預設 10 分鐘，可自選）自動切一段、在背景轉成文字，錄音不中斷；按 **Alt+X** 停止後合併成完整逐字稿並複製到剪貼簿，到任何能貼文字的地方按 **Ctrl+V** 即可貼上。錄音檔與逐字稿都留在本機資料夾。

辨識在本機執行（[faster-whisper](https://github.com/SYSTRAN/faster-whisper)），不需要網路，也不會把聲音傳到雲端。第一次使用時需要下載一次模型。

## 特色

- 全域快捷鍵：在任何程式裡都能按 Alt+R / Alt+X
- 分段錄音：每 N 分鐘切一段，邊錄邊轉寫，長時間會議也不用等到最後才開始轉
- 分段長度可在系統匣選單選 1–30 分鐘，或自訂 1–240 分鐘
- 停止後合併成完整逐字稿，自動複製到剪貼簿，Ctrl+V 貼上
- 錄音檔（wav）與逐字稿（txt）保留在 `recordings\<日期_時間>\`
- 紅色呼吸燈浮動視窗，顯示錄音時間、第幾段、已轉寫幾段
- 中英混合自動偵測，也可固定繁中、簡中、英文、日文
- 常駐系統匣，可設定開機自動啟動
- 浮動視窗不搶輸入焦點，游標仍留在原本的輸入框

## 系統需求

- Windows 10 / 11
- Python 3.11 – 3.14（建議 3.12；3.14 已實測可執行與打包）
- 一支可用的麥克風
- 約 1 GB 磁碟空間（套件 + `small` 模型）
- 第一次執行需要網路下載模型，之後可離線

## 快速開始

1. 把專案檔案放在同一個資料夾。
2. 雙擊 `start_whisper.bat`。第一次會自動建立 `.venv` 並安裝套件。
3. 第一次按 Alt+R 時會下載語音模型（約 460 MB），請稍候。
4. 說話，按 Alt+X 停止，等最後一段轉完（綠色 ✔），到任何地方按 Ctrl+V 貼上。

想要不依賴 Python 的版本，雙擊 `build_exe.bat` 打包，產物在 `dist\VoiceWhisper\VoiceWhisper.exe`（整個資料夾要一起保留）。

## 檔案說明

| 檔案 | 用途 |
|---|---|
| `voice_whisper.pyw` | 主程式（離線 Whisper 版） |
| `start_whisper.bat` | 一鍵啟動：自動找 Python、建 `.venv`、裝套件、背景啟動 |
| `requirements_whisper.txt` | 套件清單 |
| `build_exe.bat` | 打包成 exe（PyInstaller，`--onedir`） |
| `voice_debug.log` | 執行後自動產生的診斷記錄 |
| `recordings\` | 每次錄音一個資料夾：`part_001.wav`/`.txt`…、`transcript.txt` |
| `settings.json` | 記住分段長度等設定 |
| `USER_GUIDE.md` | 使用手冊 |
| `CHANGELOG.md` | 更新紀錄（目前版本 0.2.4） |
| `SKILL.md` | 給 Claude 的維護說明 |

## 可調整的設定

在 `voice_whisper.pyw` 開頭：

| 設定 | 預設 | 說明 |
|---|---|---|
| `MODEL_SIZE` | `small` | `tiny` / `base` / `small` / `medium`，越大越準也越慢 |
| `START_KEY` / `STOP_KEY` | `R` / `X` | 快捷鍵（皆搭配 Alt） |
| `DEFAULT_CHUNK_MIN` | `10` | 預設分段長度（分鐘）；平常直接在系統匣選單改即可 |
| `CUT_GRACE_SEC` | `5` | 時間到後最多再等幾秒找安靜處再切 |
| `SLICE_SEC` / `SLICE_MAX_SEC` | `20` / `29` | 錄音中每累積幾秒（找安靜處，最長幾秒硬切）就先送一小片去背景轉寫 |
| `SPEECH_RMS` | `0.006` | 低於此音量視為安靜（用來找切段點） |
| `DEFAULT_LANG` | `auto` | 預設辨識語言 |

## 疑難排解

詳見 `USER_GUIDE.md` 的「常見問題」。最常見的幾項：

- **按 Alt+R 沒反應**：確認系統匣有麥克風圖示；快捷鍵可能被其他軟體佔用。
- **一直顯示載入模型**：第一次下載需要網路與時間。
- **講英文變成中文**：右鍵系統匣圖示，辨識語言選「自動偵測」或「English (US)」。
- **打包時「存取被拒」**：先結束執行中的 `VoiceWhisper.exe`，再重新打包。

## 隱私

所有辨識都在你的電腦上完成。程式只在第一次下載模型時連網，錄音內容不會上傳。錄音檔與逐字稿只存在本機 `recordings` 資料夾（不會進 git），不需要時可自行刪除。`voice_debug.log` 會記錄辨識出來的文字，如不想保留可隨時刪除。

## 授權與致謝

使用 faster-whisper、OpenAI Whisper 模型、pystray、sounddevice。各套件適用其各自的授權條款。
