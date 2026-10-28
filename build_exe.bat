@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === 打包成 exe ===
taskkill /f /im VoiceWhisper.exe >nul 2>nul
timeout /t 2 /nobreak >nul
set "PY="
for %%V in (3.12 3.11 3.13) do if not defined PY (py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V")
for %%P in (3.12 3.11 3.13) do if not defined PY for /d %%D in ("%USERPROFILE%\.pyenv\pyenv-win\versions\%%P*") do if exist "%%D\python.exe" if not defined PY set PY="%%D\python.exe"
for %%V in (312 311 313) do if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
if not defined PY python -c "import sys;sys.exit(0 if (3,11)<=sys.version_info[:2]<=(3,13) else 1)" >nul 2>nul && set "PY=python"
if not defined PY (
  echo 找不到 Python 3.11-3.13。以下是診斷資訊，請截圖給我：
  echo --- py 啟動器 ---
  py -0p
  echo --- PATH 上的 python ---
  where python
  python --version
  echo --- pyenv 資料夾 ---
  dir /b "%USERPROFILE%\.pyenv\pyenv-win\versions"
  pause
  exit /b
)
echo 使用的 Python: %PY%
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv || (echo 建立 .venv 失敗 & rmdir /s /q .venv & pause & exit /b)
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements_whisper.txt pyinstaller || (echo 安裝套件失敗 & pause & exit /b)
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --noconsole --name VoiceWhisper ^
  --hidden-import pystray._win32 ^
  --collect-all faster_whisper --collect-all ctranslate2 --collect-all onnxruntime ^
  --collect-all tokenizers --collect-all av --collect-all _sounddevice_data ^
  --collect-all huggingface_hub ^
  voice_whisper.pyw || (echo 打包失敗 & pause & exit /b)
echo.
echo 完成！請執行 dist\VoiceWhisper\VoiceWhisper.exe  (整個 VoiceWhisper 資料夾要一起保留)
pause
