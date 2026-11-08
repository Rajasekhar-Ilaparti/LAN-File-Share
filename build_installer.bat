@echo off
rem Builds dist\LAN File Share.exe and then installer\Output\LAN-File-Share-Setup.exe
rem Needs: Python with pyinstaller, pillow, qrcode  +  Inno Setup 6 (winget install JRSoftware.InnoSetup)
setlocal
cd /d "%~dp0"

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo Inno Setup 6 was not found. Install it with:  winget install JRSoftware.InnoSetup
  exit /b 1
)

for /f "delims=" %%v in ('python file_share_app.py --version') do set "APPVER=%%v"
if not defined APPVER (
  echo Could not read APP_VERSION from file_share_app.py
  exit /b 1
)
echo Building version %APPVER%

echo [1/4] Drawing the app icon...
python file_share_app.py --make-icon || exit /b 1

echo [2/4] Building the app...
python -m PyInstaller --noconfirm --clean "LAN File Share.spec" || exit /b 1

echo [3/4] Drawing the setup wizard images...
python installer\make_wizard_images.py || exit /b 1

echo [4/4] Building the installer...
"%ISCC%" /Q "/DAppVersion=%APPVER%" installer\LANFileShare.iss || exit /b 1

echo.
echo Done:  installer\Output\
dir /b installer\Output\*.exe
