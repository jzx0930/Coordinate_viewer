@echo off
setlocal
title Coordinate Viewer
cd /d "%~dp0"

echo ============================================
echo            Coordinate Viewer
echo ============================================
echo.

REM --- Find a usable Python launcher ---
set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY goto :nopython

echo Using Python: %PY%
echo.

REM --- Suppress Streamlit first-run email prompt ---
if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit"
if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
    > "%USERPROFILE%\.streamlit\credentials.toml" echo [general]
    >> "%USERPROFILE%\.streamlit\credentials.toml" echo email = ""
)

REM --- Check packages; install only if missing ---
%PY% -c "import streamlit, plotly, pandas" >nul 2>&1
if errorlevel 1 goto :install
goto :run

:install
echo First run: installing streamlit / plotly / pandas ...
echo This may take a few minutes. Please wait.
echo.
%PY% -m pip install --upgrade pip
%PY% -m pip install streamlit plotly pandas
if errorlevel 1 goto :installfail
echo.
echo Installation done.
echo.
goto :run

:run
echo Launching... a browser tab will open.
echo To stop the app, just close this black window.
echo.
%PY% -m streamlit run coordinate_viewer.py
pause
exit /b 0

:installfail
echo.
echo [ERROR] Package installation failed. Check your internet and retry.
pause
exit /b 1

:nopython
echo [ERROR] Python not found.
echo Please install Python from https://www.python.org/downloads/
echo and tick "Add Python to PATH" during installation.
echo.
pause
exit /b 1
