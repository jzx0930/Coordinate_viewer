@echo off
setlocal
title Coordinate Viewer (Desktop)
cd /d "%~dp0"

echo ============================================
echo        Coordinate Viewer - Desktop
echo ============================================
echo.

set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY goto :nopython

echo Using Python: %PY%
echo.

%PY% -c "import pandas, numpy, pyqtgraph, PyQt5, qdarktheme" >nul 2>&1
if errorlevel 1 goto :install
goto :run

:install
echo First run: installing pandas / numpy / pyqtgraph / PyQt5 / pyqtdarktheme ...
echo This may take a few minutes. Please wait.
echo.
%PY% -m pip install --upgrade pip
%PY% -m pip install pandas numpy pyqtgraph PyQt5 pyqtdarktheme
if errorlevel 1 goto :installfail
echo.
echo Installation done.
echo.
goto :run

:run
%PY% coordinate_viewer_desktop.py
if errorlevel 1 pause
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
