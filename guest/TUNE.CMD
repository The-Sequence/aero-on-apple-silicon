@echo off
rem Aero on Apple Silicon: free the emulated CPU from Windows 7 background work.
rem Run inside the Windows 7 VM: right-click > Run as administrator. Reboot after.
rem Every change is reversible (see the UNDO lines at the bottom).

echo Stopping and disabling background services...
rem Never add Themes or UxSms here: Aero needs both.
for %%S in (WSearch SysMain wuauserv WinDefend defragsvc WerSvc DiagTrack) do (
  sc stop %%S >nul 2>&1
  sc config %%S start= disabled >nul 2>&1
)

echo Disabling scheduled defrag and Customer Experience tasks...
schtasks /Change /TN "\Microsoft\Windows\Defrag\ScheduledDefrag" /Disable >nul 2>&1
schtasks /Change /TN "\Microsoft\Windows\Application Experience\ProgramDataUpdater" /Disable >nul 2>&1
schtasks /Change /TN "\Microsoft\Windows\Customer Experience Improvement Program\Consolidator" /Disable >nul 2>&1

echo High performance power plan...
powercfg -setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c

echo Turning off prefetch and boot tracing...
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters" /v EnablePrefetcher /t REG_DWORD /d 0 /f >nul
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters" /v EnableSuperfetch /t REG_DWORD /d 0 /f >nul

echo Plain-colour login and desktop background (wallpaper is decoded on the emulated CPU)...
reg add "HKCU\Control Panel\Desktop" /v Wallpaper /t REG_SZ /d "" /f >nul
reg add "HKCU\Control Panel\Colors" /v Background /t REG_SZ /d "30 70 140" /f >nul

echo Firefox: fewer content processes (less emulated CPU per page)...
for /d %%P in ("%APPDATA%\Mozilla\Firefox\Profiles\*") do (
  echo user_pref("dom.ipc.processCount", 2);>> "%%P\user.js"
)

echo Done. Reboot Windows for everything to take effect.
pause

rem ---------------------------------------------------------------- UNDO
rem sc config WSearch start= delayed-auto & sc config SysMain start= auto
rem sc config wuauserv start= delayed-auto & sc config WinDefend start= auto
rem sc config defragsvc start= demand & sc config WerSvc start= demand
rem Re-enable the three scheduled tasks in Task Scheduler; delete user.js from the Firefox profile.
