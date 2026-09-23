@echo off
rem Installs the host GPU name as a startup task (called by SETUP.CMD).
rem Makes Windows report your Mac GPU name. Undo with UNDOGPU.CMD.
setlocal EnableExtensions
net session >nul 2>&1 || (echo Please run SETUP.CMD as Administrator. & exit /b 1)
if not exist "%SystemRoot%\HostGpuName" mkdir "%SystemRoot%\HostGpuName"
copy /y "%~dp0APPLY.CMD" "%SystemRoot%\HostGpuName\APPLY.CMD" >nul
schtasks /create /tn "HostGpuName" /ru SYSTEM /sc onstart /rl highest /tr "%SystemRoot%\HostGpuName\APPLY.CMD" /f
schtasks /run /tn "HostGpuName"
echo Done. The new name shows after the task finishes (about 30 s) and fully after a reboot.
