@echo off
rem ---------------------------------------------------------------------
rem  Aero on Apple Silicon - guest setup for Windows Vista / 7 (x64 or x86)
rem
rem  Installs:
rem    1. the VMware SVGA 3D display driver (graphics acceleration / Aero)
rem    2. the GPU watchdog (TDR) timeout raise, so slow 3D frames don't
rem       kill the driver
rem    3. the auto-resize agent (window size -> Windows resolution); it also
rem       reports sound, internet and Aero status to the setup screen
rem    4. clipboard sharing (virtio-serial driver + SPICE agent)
rem    5. sound: makes sure the Windows audio services start
rem    6. the host GPU name, shown in Device Manager and dxdiag
rem
rem  This does NOT install VMware Tools. Tools makes the login screen crawl.
rem ---------------------------------------------------------------------
setlocal EnableExtensions

net session >nul 2>&1
if not errorlevel 1 goto :elevated
echo Requesting administrator rights...
> "%TEMP%\aero-elevate.vbs" echo Set sh = CreateObject^("Shell.Application"^)
>>"%TEMP%\aero-elevate.vbs" echo sh.ShellExecute "cmd.exe", "/k ""%~f0"" elevated", "%~dp0", "runas", 1
cscript //nologo "%TEMP%\aero-elevate.vbs"
exit /b

:elevated
cd /d "%~dp0"
set "LOG=C:\aero-setup.log"
echo ==== %DATE% %TIME% ==== > "%LOG%"
echo.
echo Aero on Apple Silicon - guest setup
echo ==================================
echo.

rem ---- 1. graphics driver -------------------------------------------------
echo [1/6] Graphics driver (VMware SVGA 3D)
if exist "%~dp0graphics\vm3d.inf" goto :havedriver
echo   The driver is not on this disc. Extracting it from VMware Tools...
if not exist "%~dp0vmtools\setup64.exe" if not exist "%~dp0vmtools\setup.exe" (
  echo   FAILED: no driver and no VMware Tools installer on the disc.
  echo   Re-run Setup.command on the Mac.
  goto :tdr
)
set "VMT=%~dp0vmtools\setup64.exe"
if not exist "%VMT%" set "VMT=%~dp0vmtools\setup.exe"
rem /a is an administrative install: it only unpacks files, it installs nothing.
"%VMT%" /a /s /v"/qn TARGETDIR=C:\VMwareToolsExtract" >>"%LOG%" 2>&1
for /f "delims=" %%F in ('dir /b /s "C:\VMwareToolsExtract\vm3d.inf" 2^>nul') do set "INF=%%F"
if defined INF goto :install
echo   FAILED to extract - see %LOG%
goto :tdr
:havedriver
set "INF=%~dp0graphics\vm3d.inf"
:install
echo   Installing %INF%
pnputil -i -a "%INF%" >>"%LOG%" 2>&1
if errorlevel 1 (echo   pnputil reported an error - see %LOG%) else (echo   Installed.)

rem ---- 2. TDR -------------------------------------------------------------
:tdr
echo.
echo [2/6] GPU watchdog timeout
rem Under emulation one 3D frame can take seconds; Windows would otherwise
rem reset the driver and 3D apps fail with D3DERR_DEVICELOST.
set "K=HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers"
reg add "%K%" /v TdrDelay /t REG_DWORD /d 60 /f >>"%LOG%" 2>&1
reg add "%K%" /v TdrDdiDelay /t REG_DWORD /d 60 /f >>"%LOG%" 2>&1
echo   Set to 60 seconds.

rem ---- 3. auto-resize agent ----------------------------------------------
echo.
echo [3/6] Auto-resize agent
set "DEST=%ProgramFiles%\QemuResAgent"
if not exist "%DEST%" mkdir "%DEST%"
taskkill /im qemu-res-agent.exe /f >nul 2>&1
copy /y "%~dp0QemuResAgent\qemu-res-agent.exe" "%DEST%\" >>"%LOG%" 2>&1
if errorlevel 1 (
  echo   FAILED to copy - see %LOG%
) else (
  reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run" /v QemuResAgent /t REG_SZ /d "\"%DEST%\qemu-res-agent.exe\"" /f >>"%LOG%" 2>&1
  start "" "%DEST%\qemu-res-agent.exe"
  echo   Installed and started. Its log is C:\qemu-res-agent.log
)

rem ---- 4. clipboard ---------------------------------------------------------
echo.
echo [4/6] Clipboard sharing
if not exist "%~dp0clipboard\vdagent" (
  echo   Skipped (not on this disc - run Setup on the Mac again to add it).
  goto :audio
)
for /f "tokens=4-5 delims=[]. " %%a in ('ver') do set "NTVER=%%a.%%b"
set "VIOOS=w7"
if "%NTVER%"=="6.0" set "VIOOS=2k8"
set "ARCH=amd64"
set "BITS=64"
if /i "%PROCESSOR_ARCHITECTURE%"=="x86" if not defined PROCESSOR_ARCHITEW6432 (set "ARCH=x86" & set "BITS=32")
if exist "%~dp0clipboard\redhat.cer" (
  certutil -addstore -f TrustedPublisher "%~dp0clipboard\redhat.cer" >>"%LOG%" 2>&1
)
pnputil -i -a "%~dp0clipboard\vioserial\%VIOOS%\%ARCH%\vioser.inf" >>"%LOG%" 2>&1
if errorlevel 1 rundll32 setupapi.dll,InstallHinfSection DefaultInstall 132 "%~dp0clipboard\vioserial\%VIOOS%\%ARCH%\vioser.inf" >>"%LOG%" 2>&1
set "VDDIR=%ProgramFiles%\SPICE Guest Tools"
if not exist "%VDDIR%" mkdir "%VDDIR%"
copy /y "%~dp0clipboard\vdagent\%BITS%\vdservice.exe" "%VDDIR%\" >>"%LOG%" 2>&1
copy /y "%~dp0clipboard\vdagent\%BITS%\vdagent.exe" "%VDDIR%\" >>"%LOG%" 2>&1
pushd "%VDDIR%"
vdservice.exe install >>"%LOG%" 2>&1
popd
sc config vdservice start= auto >>"%LOG%" 2>&1
sc start vdservice >>"%LOG%" 2>&1
echo   Installed. Copy and paste text between the Mac and Windows after the restart.

:audio
rem ---- 5. sound -------------------------------------------------------------
echo.
echo [5/6] Sound
rem Windows has built-in drivers for QEMU's sound device; only the audio
rem services need to be running and set to start automatically.
sc config AudioEndpointBuilder start= auto >>"%LOG%" 2>&1
sc config Audiosrv start= auto >>"%LOG%" 2>&1
net start AudioEndpointBuilder >>"%LOG%" 2>&1
net start Audiosrv >>"%LOG%" 2>&1
echo   Audio services set to start automatically.

rem ---- 6. GPU name --------------------------------------------------------
echo.
echo [6/6] GPU name in Device Manager / dxdiag
if not exist "%~dp0gpuname\SETGPU.CMD" (
  echo   Skipped (no gpuname folder on the disc).
  goto :done
)
call "%~dp0gpuname\SETGPU.CMD" >>"%LOG%" 2>&1
echo   Installed. Note: this only changes the NAME Windows displays.
echo   The actual rendering already runs on your Mac's GPU.

:done
rem ---- WinSAT ----------------------------------------------------------------
rem Windows turns Aero on only after it has rated the graphics driver.  If the
rem driver is already running (the VM window resized itself), rate it now.
set "DRV_ACTIVE="
for /f "skip=1 delims=" %%n in ('wmic path Win32_VideoController get Name 2^>nul') do (
  echo %%n | find /i "VMware" >nul && set "DRV_ACTIVE=1"
)
if not defined DRV_ACTIVE goto :restartonly
echo.
echo ============================================================
echo  The graphics driver is already running - that is why the VM
echo  window just resized itself.
echo.
echo  Windows only turns Aero on after rating the new driver with
echo  WinSAT (Windows Experience Index). It takes about 5-15 minutes.
echo ============================================================
choice /c YN /m "Run WinSAT now"
if errorlevel 2 goto :winsatlater
echo.
echo Running WinSAT - the screen may flicker and go dark briefly. Please wait...
winsat formal >>"%LOG%" 2>&1
echo WinSAT finished.
goto :finish
:winsatlater
echo   Later: Control Panel ^> Performance Information and Tools ^> Rate this computer.
goto :finish

:restartonly
echo.
echo  (The graphics driver starts after the restart. If Aero is not on
echo   afterwards: Control Panel ^> Performance Information and Tools ^>
echo   Rate this computer, then restart again.)

:finish
echo.
echo ============================================================
echo  Finished. Log: %LOG%
echo.
echo  Now RESTART Windows once (Start ^> Restart). After that, shut
echo  Windows down and go back to the setup screen on your Mac -
echo  it checks that everything works.
echo ============================================================
echo.
pause
