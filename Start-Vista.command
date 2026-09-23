#!/bin/bash
# Double-click to start the Windows Vista VM.
#
#   MODE=install ISO=/path/to/windows.iso ./Start-Vista.command    install Windows
#   MODE=setup ./Start-Vista.command                                boot with guest-tools.iso
#   ./Start-Vista.command                                           normal boot
cd "$(dirname "$0")"
GUEST=vista exec bash build/run-vm.sh
