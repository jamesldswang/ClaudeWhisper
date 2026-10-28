@echo off
chcp 65001 >nul
cd /d "%~dp0"
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
if not exist ".venv\Scripts\pythonw.exe" (
  echo 第一次執行：建立環境並安裝套件，請稍候...
  %PY% -m venv .venv || (echo 建立 .venv 失敗 & rmdir /s /q .venv & pause & exit /b)
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements_whisper.txt || (echo 安裝套件失敗 & rmdir /s /q .venv & pause & exit /b)
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0voice_whisper.pyw"
