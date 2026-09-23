#!/bin/bash
#
# Aero on Apple Silicon - start here.
#
# Opens the full-screen control screen (mouse and keyboard) when this Terminal
# can show it, and the plain step-by-step wizard otherwise.  Both do the same
# things and share the same files.
#
#   ./START\ HERE.command                  open the control screen
#   ./START\ HERE.command --plain          force the plain wizard
#   ./START\ HERE.command --start <conf>   open it and start that VM
#                                          (what the Desktop launchers do)
#
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1

MODE_ARG="${1:-}"
START_CONF="${2:-}"

if [ "$MODE_ARG" != "--plain" ] && [ -z "${AERO_PLAIN:-}" ] && [ -t 0 ] && [ -t 1 ]; then
    PY=""
    for c in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        [ -x "$c" ] || continue
        # Without Apple's command-line tools, /usr/bin/python3 is only a stub
        # that pops up an installer dialog - never run it in that case.
        if [ "$c" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then continue; fi
        if "$c" -c 'import curses, locale' >/dev/null 2>&1; then PY="$c"; break; fi
    done
    if [ -n "$PY" ]; then
        printf '\033[8;32;104t'     # ask Terminal for a 104x32 window
        sleep 0.3
        if [ "$MODE_ARG" = "--start" ] && [ -n "$START_CONF" ]; then
            "$PY" "$ROOT/build/aero_tui.py" --start "$START_CONF"
        else
            "$PY" "$ROOT/build/aero_tui.py"
        fi
        rc=$?
        printf '\033[0m'; clear
        [ "$rc" -eq 3 ] || exit "$rc"   # 3 = "use the plain wizard instead"
    fi
fi

# A Desktop launcher without the control screen: start the VM directly.
if [ "$MODE_ARG" = "--start" ] && [ -f "$START_CONF" ]; then
    get() { sed -n "s/^$1=//p" "$START_CONF" | head -1; }
    echo "Starting $(get VM_NAME). Shut it down from Windows' Start menu."
    GUEST="$(get GUEST)" DISK="$(get DISK)" VM_CPUS="$(get VM_CPUS)" MEM="$(get MEM)" \
    AUDIO_DEVICE="$(get AUDIO_DEVICE)" NIC="$(get NIC)" CLIPBOARD="$(get CLIPBOARD)" \
    HOST_LOG="$ROOT/logs/$(basename "$START_CONF" .conf)-run.log" \
        exec bash "$ROOT/build/run-vm.sh"
fi
exec bash "$ROOT/build/wizard.sh"
