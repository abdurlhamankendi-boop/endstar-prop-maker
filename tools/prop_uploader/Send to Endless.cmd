@echo off
rem Drag a model file onto this icon, or double-click it to choose one.
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 goto nopython
where pythonw >nul 2>&1
if errorlevel 1 goto nopython
python -c "import tkinter" >nul 2>&1
if errorlevel 1 goto nopython
start "" pythonw "%~dp0Send to Endless.pyw" %*
exit /b 0

:nopython
echo.
echo This tool needs Python 3 with its window parts (tkinter), and this PC does not have them yet.
echo Install Python 3 from python.org, tick "Add python.exe to PATH" on the first screen, then open this again.
echo.
pause
exit /b 1
