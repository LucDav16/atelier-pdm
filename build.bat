@echo off
rem ================================================================
rem  Builds Atelier PDM and its installer:  Output\AtelierPDM-Setup-2.9.0exe
rem  Needs: Python 3.10+ (python.org) and Inno Setup 6 (jrsoftware.org)
rem  The bulky build files go to %LOCALAPPDATA%\Atelier PDM Build so they
rem  don't sync to your cloud drive. Only the finished installer lands here.
rem ================================================================
setlocal
cd /d "%~dp0"
set "WORK=%LOCALAPPDATA%\Atelier PDM Build"
for %%F in (atelier_pdm.py atelier_panel.py atelier_secure.py atelier_setup.py atelier_launcher.py atelier_cam.py atelier_cam_ui.py atelier.spec installer.iss atelier.ico atelier_solidcraft.ico atelier_physcad.ico atelier_cam.ico) do (
  if not exist "%%F" ( echo Missing %%F - run make_build_kit.py again. & goto :fail )
)
findstr /c:"_secure.install" atelier_pdm.py >nul || ( echo atelier_pdm.py hasn't been upgraded - run:  py apply_v2_6.py & goto :fail )

where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% --version >nul 2>nul || ( echo Python isn't installed. Get it from https://www.python.org/downloads/ & goto :fail )

if not exist "%WORK%\venv\Scripts\python.exe" (
  echo Creating the build environment in "%WORK%" ...
  %PY% -m venv "%WORK%\venv" || goto :fail
)
"%WORK%\venv\Scripts\python.exe" -m pip install --upgrade pip >nul
"%WORK%\venv\Scripts\python.exe" -m pip install --upgrade pyinstaller cryptography pypdf || goto :fail

echo.
echo Building the program...
"%WORK%\venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --distpath "%WORK%\dist" --workpath "%WORK%\build" atelier.spec || goto :fail

set "ISCC="
for %%P in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe") do (
  if exist %%P set "ISCC=%%~P"
)
if not defined ISCC (
  echo.
  echo The program is built in "%WORK%\dist\Atelier PDM", but Inno Setup 6 wasn't found,
  echo so there's no installer yet. Install it with:   winget install JRSoftware.InnoSetup
  echo then run build.bat again.
  goto :end
)
echo.
echo Building the installer...
"%ISCC%" /Qp "/DDistDir=%WORK%\dist\Atelier PDM" "/O%~dp0Output" installer.iss || goto :fail
echo.
echo Done:  %~dp0Output\AtelierPDM-Setup-2.9.5.exe
goto :end

:fail
echo.
echo Build stopped - see the message above.
:end
echo.
pause
