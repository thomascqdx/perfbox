@echo off
rem One-click build: .venv setup -> pytest -> dist\perfbox.exe -> exe smoke check.
rem   build.bat [--skip-tests] [--no-pause]
rem Double-clicked runs pause at the end so the window stays open.
setlocal
cd /d "%~dp0"

set "SELF=%~nx0"
set "PY=.venv\Scripts\python.exe"
set "EXE=dist\perfbox.exe"
set "RC=0"
set "SKIP_TESTS="
set "NO_PAUSE="
call :detect_dblclick

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--skip-tests" (set "SKIP_TESTS=1" & shift & goto parse)
if /i "%~1"=="--no-pause" (set "NO_PAUSE=1" & shift & goto parse)
if /i "%~1"=="-h" goto usage
if /i "%~1"=="--help" goto usage
echo [build] unknown option: %~1
set "RC=2"
goto usage
:parsed

echo [build] [1/4] python environment
if not exist "%PY%" (
    echo [build] creating .venv with py -3.13
    py -3.13 -m venv .venv
    if errorlevel 1 goto fail
)
%PY% -c "import perfbox, pytest, PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [build] installing dependencies into .venv
    %PY% -m pip install -e . -r requirements-dev.txt
    if errorlevel 1 (
        echo [build] pip install failed; if offline, set HTTPS_PROXY and retry
        goto fail
    )
)

if defined SKIP_TESTS (
    echo [build] [2/4] tests skipped
) else (
    echo [build] [2/4] pytest
    %PY% -m pytest -q
    if errorlevel 1 goto fail
)

echo [build] [3/4] pyinstaller
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_exe.ps1
if errorlevel 1 goto fail

echo [build] [4/4] smoke check %EXE%
set "VER="
for /f "delims=" %%v in ('%PY% -c "import perfbox; print(perfbox.__version__)"') do set "VER=%%v"
if not defined VER goto fail
%EXE% --version | findstr /x /c:"perfbox %VER%" >nul
if errorlevel 1 (
    echo [build] %EXE% --version does not report %VER%
    goto fail
)

echo [build] OK: %CD%\%EXE% (%VER%)
goto finish

:usage
echo usage: %SELF% [--skip-tests] [--no-pause]
echo   --skip-tests  skip pytest before packaging
echo   --no-pause    never wait for a key press at the end
goto finish

:fail
set "RC=1"
echo [build] FAILED

:finish
if defined DBLCLICK if not defined NO_PAUSE pause
exit /b %RC%

:detect_dblclick
rem Launched from Explorer = cmd /c with this script on the command line.
setlocal EnableDelayedExpansion
set "CL=!cmdcmdline!"
set "R="
if /i not "!CL:%SELF%=!"=="!CL!" set "R=1"
endlocal & set "DBLCLICK=%R%"
exit /b 0
