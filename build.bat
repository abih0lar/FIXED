@echo off
REM ============================================================
REM  ALADIN - build ALADIN.exe (single-file control panel)
REM  Run inside the FIXED folder (where app.py and .env live).
REM
REM  What it does:
REM   1. embeds a SANITIZED copy of .env into app.py via inject_env.py:
REM      all strategy settings are kept (that's the product), but your
REM      MT5 credentials, Telegram/Gemini tokens and DRM secret are
REM      BLANKED - so you can safely sell/give the exe away.
REM   2. builds dist\ALADIN.exe. The bot engine is bundled INSIDE the
REM      exe as bytecode (PyInstaller) and runs in-process - NO .py
REM      source files are ever written to disk next to the exe.
REM
REM  Result: dist\ALADIN.exe is safe to distribute AND does not expose
REM  your source code. Each buyer enters their OWN MT5 account in the
REM  Broker tab and their license key in the License tab.
REM  If you change settings in .env later, just re-run this script.
REM ============================================================
cd /d "%~dp0"

echo [1/3] Embedding sanitized .env (no credentials) ...
python inject_env.py
if errorlevel 1 goto :err

echo [2/3] Building ALADIN.exe (engine bundled INSIDE the exe) ...
py -m PyInstaller --noconfirm --onefile --windowed --name ALADIN --icon logo.ico --collect-all MetaTrader5 app.py
if errorlevel 1 goto :err

echo [3/3] Done.
echo.
echo Executable created at:  dist\ALADIN.exe
echo It is self-contained - no .env copy needed next to it.
pause
exit /b 0

:err
echo.
echo BUILD FAILED - see the error above.
pause
exit /b 1
