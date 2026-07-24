@echo off
setlocal enabledelayedexpansion

echo ============================================
echo   Conductor ^— Build Windows Installer
echo ============================================
echo.

:: Use venv Python/pip/pyinstaller if the dev venv exists
if exist "venv\Scripts\python.exe" (
    set PYTHON=venv\Scripts\python.exe
    set PIP=venv\Scripts\pip.exe
    set PYINSTALLER=venv\Scripts\pyinstaller.exe
) else (
    set PYTHON=python
    set PIP=pip
    set PYINSTALLER=pyinstaller
)

:: Verify Python is available
%PYTHON% --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.10+ and re-run.
    pause & exit /b 1
)

:: Install / upgrade PyInstaller
echo [1/4] Checking PyInstaller...
%PIP% show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo       Installing PyInstaller...
    %PIP% install pyinstaller
    if errorlevel 1 ( echo ERROR: Failed to install PyInstaller. & pause & exit /b 1 )
) else (
    echo       PyInstaller OK.
)

:: Generate icon
echo.
echo [2/4] Generating icon...
%PYTHON% build_icon.py
if errorlevel 1 ( echo ERROR: Icon generation failed. & pause & exit /b 1 )

:: PyInstaller build — skip if exe already exists (pass --rebuild to force)
echo.
if exist "dist\Conductor\Conductor.exe" (
    echo %* | find /i "--rebuild" >nul
    if errorlevel 1 (
        echo [3/4] Executable already built — skipping. Pass --rebuild to force.
        goto :inno
    )
)
echo [3/4] Building executable (this takes a minute)...
%PYINSTALLER% conductor.spec --clean --noconfirm
if errorlevel 1 ( echo ERROR: PyInstaller build failed. & pause & exit /b 1 )
echo       Executable built: dist\Conductor\Conductor.exe
:inno

:: Run Inno Setup via Python (avoids batch syntax issues with spaces in paths)
echo.
echo [4/4] Building installer...
%PYTHON% run_inno.py
if errorlevel 2 ( pause & exit /b 0 )
if errorlevel 1 ( echo ERROR: Inno Setup build failed. & pause & exit /b 1 )

echo.
echo ============================================
echo   Done!
echo   Installer: Output\Conductor_Setup.exe
echo ============================================
echo.
pause
