@echo off
rem Removes the startup task. Restoring "VMware SVGA 3D" fully: reinstall or
rem update the VMware SVGA driver, or roll back the qcow2 snapshot pre-gpu-name.
schtasks /delete /tn "HostGpuName" /f
echo Task removed. Reboot; HardwareInformation.* return to VMware values.
