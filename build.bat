@echo off
REM Builds dist\I2CPoke.exe from the files in this folder.
REM Needs Python for Windows from python.org (tick "Add to PATH" during install).
cd /d "%~dp0"
echo Installing build tools...
py -m pip install --upgrade -r requirements.txt pyinstaller || goto :err
echo Building...
py -m PyInstaller --noconfirm --onefile --windowed --name I2CPoke --hidden-import hid i2cpoke_gui.pyw || goto :err
echo.
echo Done:  %~dp0dist\I2CPoke.exe
pause
exit /b 0
:err
echo.
echo Build failed - see messages above.
pause
exit /b 1
