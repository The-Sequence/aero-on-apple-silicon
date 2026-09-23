Aero on Apple Silicon - guest tools
===================================

Run SETUP.CMD as Administrator inside Windows Vista or 7, then reboot.

It installs the VMware SVGA 3D display driver, raises the GPU watchdog
timeout, installs the auto-resize agent, and makes Windows display your
Mac's GPU name.

It does NOT install VMware Tools, which slows the guest down badly.

gpuname\UNDOGPU.CMD undoes the GPU name change.
