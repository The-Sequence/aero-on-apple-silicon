#!/bin/bash
# Double-click to start the Windows 7 VM.
#
#   MODE=install ISO=/path/to/windows.iso ./Start-Windows7.command    install Windows
#   MODE=setup ./Start-Windows7.command                                boot with guest-tools.iso
#   ./Start-Windows7.command                                           normal boot
cd "$(dirname "$0")"
GUEST=win7 exec bash build/run-vm.sh
