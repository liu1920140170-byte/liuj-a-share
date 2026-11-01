@echo off
rem ============================================================
rem  Wyckoff A-Share Reviewer - launcher (ASCII only on purpose:
rem  cmd.exe parses .bat with the system codepage, so non-ASCII
rem  text here can break parsing. All Chinese output comes from
rem  the Python program instead.)
rem
rem  Usage:
rem    run.bat                    start web UI and open browser
rem    run.bat web                same as above
rem    run.bat 600519             one-shot CLI analysis
rem    run.bat 600519 --loop 60   refresh every 60 seconds
rem
rem  It auto-detects a usable Python; if none is installed it
rem  falls back to the bundled DSH runtime so it still runs.
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"
set "PYEXE="
set "PYARGS="
set "PYSRC="

rem ---------- 1) official Windows py launcher ----------
if exist "%SystemRoot%\py.exe" (
  "%SystemRoot%\py.exe" -3 -c "import sys" >nul 2>nul
  if not errorlevel 1 (
    set "PYEXE=%SystemRoot%\py.exe"
    set "PYARGS=-3"
    set "PYSRC=py launcher"
  )
)

rem ---------- 2) python on PATH (skip Microsoft Store stub) ----------
if not defined PYEXE (
  for /f "delims=" %%P in ('where python 2^>nul') do (
    echo %%P|findstr /i "WindowsApps" >nul
    if errorlevel 1 if not defined PYEXE (
      set "PYEXE=%%P"
      set "PYSRC=PATH"
    )
  )
)

rem ---------- 3) common install locations ----------
if not defined PYEXE (
  for %%V in (314 313 312 311 310 39 38) do (
    if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
      set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
      set "PYSRC=local install"
    )
  )
)
if not defined PYEXE (
  for %%R in ("%ProgramFiles%" "%ProgramFiles(x86)%" "C:") do (
    if not defined PYEXE if exist "%%~R\Python312\python.exe" (
      set "PYEXE=%%~R\Python312\python.exe"
      set "PYSRC=system install"
    )
  )
)

rem ---------- 4) bundled DSH runtime (zero-install fallback) ----------
if not defined PYEXE (
  for /d %%D in ("%USERPROFILE%\.dsh\dsh-runtimes\*") do (
    if not defined PYEXE if exist "%%~fD\dependencies\python\python.exe" (
      set "PYEXE=%%~fD\dependencies\python\python.exe"
      set "PYSRC=DSH bundled runtime"
    )
  )
)

if not defined PYEXE (
  echo.
  echo [ERROR] No usable Python found.
  echo         Recommended:  winget install -e --id Python.Python.3.12
  echo         Or download from https://www.python.org/downloads/
  echo         and check "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)

"%PYEXE%" %PYARGS% -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 3)" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python 3.8+ required, found: "%PYEXE%"
  pause
  exit /b 1
)

echo [INFO] Python: %PYEXE%  (%PYSRC%)
echo.

if "%~1"=="" goto web
if /i "%~1"=="web" goto web
if /i "%~1"=="selftest" goto selftest

"%PYEXE%" %PYARGS% "%~dp0main.py" %*
goto end

:selftest
echo [INFO] running self tests ...
"%PYEXE%" %PYARGS% "%~dp0tests\smoke_datasource.py"
"%PYEXE%" %PYARGS% "%~dp0tests\scan_demo.py"
"%PYEXE%" %PYARGS% "%~dp0tests\check_contract.py" 600519
echo [INFO] self tests finished.
goto end

:web
"%PYEXE%" %PYARGS% "%~dp0server.py" --open

:end
echo.
pause
