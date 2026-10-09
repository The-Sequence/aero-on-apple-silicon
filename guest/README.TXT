Aero on Apple Silicon - guest tools
===================================

Run SETUP.CMD as Administrator inside Windows Vista or 7, then reboot.

It installs the VMware SVGA 3D display driver, raises the GPU watchdog
timeout, installs the auto-resize agent, and makes Windows display your
Mac's GPU name.

It does NOT install VMware Tools, which slows the guest down badly.

gpuname\UNDOGPU.CMD undoes the GPU name change.

TUNE.CMD (optional, new in 1.1): run as Administrator, then reboot. It stops
Windows background services that waste the emulated CPU (Search indexing,
SuperFetch, Windows Update, Defender, scheduled defrag), switches to the High
performance power plan, sets a plain-colour desktop (the wallpaper is decoded
on the emulated CPU) and limits Firefox to two content processes. It never
touches the Themes or Desktop Window Manager services that Aero needs. Undo
steps are at the bottom of the file.
