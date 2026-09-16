@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

python -m pip install -r requirements.txt
if errorlevel 1 (
  echo pip 설치에 실패했습니다.
  exit /b 1
)

python -m PyInstaller --noconfirm --clean --onefile --noconsole --name FloatingClock main.py
if errorlevel 1 (
  echo PyInstaller 빌드에 실패했습니다.
  exit /b 1
)

echo.
echo 빌드 완료: dist\FloatingClock.exe
echo settings.json 은 exe 와 같은 폴더에 생성됩니다.
endlocal
