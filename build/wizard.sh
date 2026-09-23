#!/bin/bash
#
# Aero on Apple Silicon - the plain-text setup wizard.  START HERE.command runs
# this when the full-screen interface is not available (or with --plain).
#
# It looks at what is already here first - tools, downloads, VMs and how far
# each VM got - and carries on from there.  Run it as often as you like:
# nothing is downloaded twice and nothing is erased without asking.
#
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
CACHE="${AERO_CACHE:-$HOME/Library/Caches/AeroOnAppleSilicon}"
VMS="$ROOT/vms"
LOGS="$ROOT/logs"
mkdir -p "$VMS" "$LOGS"

VMTOOLS_FILE="VMware-tools-windows-10.3.10-12406962.iso"
VMTOOLS_SHA256="edb889e6cce11aeb568dbf471cee1b3dc26ca72bc671b660ad4872911edbf6da"
RUNTIME_TAG="$(sed -n 's/^RUNTIME_TAG="\(.*\)"/\1/p' "$ROOT/Setup.command" | head -1)"
QEMU_IMG="$ROOT/runtime/bin/qemu-img"

# ------------------------------------------------------------------ output
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
say()   { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()    { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
todo()  { printf '  \033[1;33m•\033[0m %s\n' "$*"; }
bad()   { printf '  \033[1;31m✗\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m!   %s\033[0m\n' "$*"; }
err()   { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; }
# If the window's input is gone (closed, or piped), stop rather than carry on.
pause() { read -r -p "$(printf '\033[1;32m%s\033[0m' "${1:-Press Return to continue.}") " _ || { echo; exit 0; }; }

ask() {   # ask <prompt> <default>
    local a=""
    read -r -p "$(printf '%s [%s]: ' "$1" "$2")" a || a=""
    printf '%s' "${a:-$2}"
}

yes_no() {   # yes_no <prompt> <y|n default>
    local a
    a=$(ask "$1 (y/n)" "$2")
    case "$a" in [yY]*) return 0 ;; *) return 1 ;; esac
}

# menu <prompt> <option>...  -> echoes the number chosen (1 = default)
menu() {
    local prompt="$1" i=1 a
    shift
    for o in "$@"; do printf '    %d) %s\n' "$i" "$o" >&2; i=$((i + 1)); done
    while true; do
        a=$(ask "$prompt" "1")
        case "$a" in
            ''|*[!0-9]*) ;;
            *) if [ "$a" -ge 1 ] && [ "$a" -le "$#" ]; then printf '%s' "$a"; return; fi ;;
        esac
        printf '    Please type a number from 1 to %d.\n' "$#" >&2
    done
}

# Finder drag-and-drop pastes an escaped, sometimes quoted path.
clean_path() {
    local p="$1"
    p="${p%\"}"; p="${p#\"}"; p="${p%\'}"; p="${p#\'}"
    printf '%s' "$p" | sed -e 's/\\\(.\)/\1/g' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}

human_size() {   # bytes -> "12.3 GB"
    awk -v b="${1:-0}" 'BEGIN { if (b >= 1073741824) printf "%.1f GB", b/1073741824; else printf "%.0f MB", b/1048576 }'
}

sha_ok() { [ -f "$1" ] && [ "$(shasum -a 256 "$1" | cut -d' ' -f1)" = "$2" ]; }

# ------------------------------------------------------------ VM records
# vms/<name>.conf holds KEY=value lines, values taken literally (no quoting,
# never sourced), so names with spaces are safe.
conf_get() {   # conf_get <file> <KEY>
    sed -n "s/^$2=//p" "$1" 2>/dev/null | head -1
}

conf_set() {   # conf_set <file> <KEY> <value>
    local f="$1" k="$2" v="$3" tmp
    tmp="$(mktemp)"
    [ -f "$f" ] && grep -v "^$k=" "$f" > "$tmp"
    printf '%s=%s\n' "$k" "$v" >> "$tmp"
    mv "$tmp" "$f"
}

disk_used_bytes() {   # space the qcow2 really takes on the Mac
    [ -f "$1" ] || { echo 0; return; }
    echo $(( $(du -k "$1" | cut -f1) * 1024 ))
}

disk_virtual_bytes() {
    [ -x "$QEMU_IMG" ] && [ -f "$1" ] || { echo 0; return; }
    "$QEMU_IMG" info --output=json "$1" 2>/dev/null |
        python3 -c 'import json,sys; print(json.load(sys.stdin).get("virtual-size",0))' 2>/dev/null || echo 0
}

vm_running() { pgrep -f -- "[q]emu-system.*file=$1" >/dev/null 2>&1; }

stage_label() {
    case "$1" in
        new)       echo "Windows not installed yet" ;;
        installed) echo "Windows installed, drivers not set up yet" ;;
        check)     echo "set up - checks not done yet" ;;
        ready)     echo "ready to use" ;;
        *)         echo "unknown" ;;
    esac
}

# ------------------------------------------------------- Terminal windows
# Runs a command in a NEW Terminal window.  Echoes the window id, or nothing
# when Terminal automation is not allowed (then the caller runs it inline).
open_window() {
    local cmd="$1" title="$2" esc
    esc=$(printf '%s' "cd $(printf %q "$ROOT"); clear; echo '=== $title ==='; $cmd" | sed 's/\\/\\\\/g; s/"/\\"/g')
    osascript <<EOF 2>/dev/null
tell application "Terminal"
    activate
    set w to do script "$esc"
    return id of (window 1 whose tabs contains w)
end tell
EOF
}

# Waits for a VM to start and then to close.  Returns 1 if it never started
# or died within 45 s (then shows the end of its log).
wait_for_vm() {   # wait_for_vm <disk> <log> <what>
    local disk="$1" log="$2" what="$3" spin='|/-\' i=0 t0 started=0
    t0=$(date +%s)
    for _ in $(seq 1 15); do
        vm_running "$disk" && { started=1; break; }
        sleep 2
    done
    while vm_running "$disk"; do
        printf '\r  %s  %s  (%s used so far)   ' "${spin:i++%4:1}" "$what" "$(human_size "$(disk_used_bytes "$disk")")"
        sleep 3
    done
    printf '\r%*s\r' 80 ''
    if [ "$started" = 0 ] || [ $(( $(date +%s) - t0 )) -lt 45 ]; then
        err "The VM closed almost immediately - it did not start properly."
        echo "  Last lines of its log ($log):"
        tail -8 "$log" 2>/dev/null | sed 's/^/    /'
        return 1
    fi
    ok "$what: the VM has closed."
}

# run_vm <mode> <conf> <title> [extra env assignments...]
run_vm() {
    local mode="$1" conf="$2" title="$3" disk guest cpus mem size iso log cmd win
    shift 3
    disk="$(conf_get "$conf" DISK)"; guest="$(conf_get "$conf" GUEST)"
    cpus="$(conf_get "$conf" VM_CPUS)"; mem="$(conf_get "$conf" MEM)"
    size="$(conf_get "$conf" DISK_SIZE)"; iso="$(conf_get "$conf" ISO)"
    log="$LOGS/$(basename "$conf" .conf)-$mode.log"
    cmd="MODE=$mode GUEST=$guest DISK=$(printf %q "$disk") DISK_SIZE=$size VM_CPUS=$cpus MEM=$mem"
    cmd="$cmd AUDIO_DEVICE=$(conf_get "$conf" AUDIO_DEVICE) NIC=$(conf_get "$conf" NIC) CLIPBOARD=$(conf_get "$conf" CLIPBOARD)"
    cmd="$cmd ISO=$(printf %q "$iso") HOST_LOG=$(printf %q "$log") bash build/run-vm.sh"
    win=$(open_window "$cmd; echo; echo 'The VM has closed. You can close this window.'; exit" "$title - KEEP THIS WINDOW OPEN")
    if [ -n "$win" ]; then
        warn "A second Terminal window is running the VM. Do NOT close it while Windows is running."
        warn "The VM window's red close button is disabled on purpose: shut down from Windows' Start menu."
    fi
    if [ -z "$win" ]; then
        warn "Could not open a second Terminal window, so the VM runs from this one."
        MODE=$mode GUEST=$guest DISK="$disk" DISK_SIZE=$size VM_CPUS=$cpus MEM=$mem ISO="$iso" HOST_LOG="$log" \
            bash build/run-vm.sh
        return 0
    fi
    wait_for_vm "$disk" "$log" "$title"
}

# ================================================================ start
clear
cat <<'BANNER'
================================================================
  Aero on Apple Silicon
  Windows 7 / Vista with Aero, 3D rendered by your Mac's GPU
================================================================
BANNER

# --------------------------------------------------------------- checks
say "Checking this Mac"
[ "$(uname -m)" = "arm64" ] || { err "This needs an Apple Silicon Mac (M1 or newer)."; pause; exit 1; }
if ! command -v brew >/dev/null 2>&1; then
    bad "Homebrew is not installed. Install it from https://brew.sh, then run this again."
    pause "Press Return to close."; exit 1
fi
ok "Apple Silicon, macOS $(sw_vers -productVersion), Homebrew present"
FREE_BYTES=$(( $(df -k "$ROOT" | awk 'NR==2 {print $4}') * 1024 ))
if [ "$FREE_BYTES" -lt $((25 * 1073741824)) ]; then
    warn "Only $(human_size "$FREE_BYTES") free on this disk. A Windows install needs 15-25 GB."
else
    ok "$(human_size "$FREE_BYTES") free on this disk"
fi

# ------------------------------------------------------------ tools state
tools_status() {   # sets TOOLS_OK=1 when everything is in place
    TOOLS_OK=1
    local missing=0 p
    for p in glib pixman sdl2-compat gnutls libpng jpeg-turbo zstd libslirp libusb molten-vk vulkan-loader p7zip; do
        brew list --formula "$p" >/dev/null 2>&1 || missing=$((missing + 1))
    done
    if [ "$missing" = 0 ]; then ok "Homebrew packages installed"; else todo "$missing Homebrew package(s) to install"; TOOLS_OK=0; fi

    if [ -x "$ROOT/runtime/bin/qemu-system-x86_64" ] && [ "$(cat "$ROOT/runtime/.version" 2>/dev/null)" = "$RUNTIME_TAG" ]; then
        ok "Runtime $RUNTIME_TAG installed"
    elif [ -f "$CACHE/aero-runtime-$RUNTIME_TAG-macos-arm64.tar.gz" ]; then
        todo "Runtime $RUNTIME_TAG downloaded, not unpacked yet"; TOOLS_OK=0
    else
        todo "Runtime $RUNTIME_TAG not downloaded yet (about 8 MB)"; TOOLS_OK=0
    fi

    if [ -f "$CACHE/driver-10.3.10/vm3d.inf" ]; then
        ok "VMware display driver extracted"
    elif sha_ok "$CACHE/$VMTOOLS_FILE" "$VMTOOLS_SHA256"; then
        todo "VMware Tools ISO downloaded, driver not extracted yet"; TOOLS_OK=0
    else
        todo "VMware Tools ISO not downloaded yet (about 120 MB)"; TOOLS_OK=0
    fi

    if [ -f "$ROOT/guest-tools.iso" ]; then
        # Rebuild the disc if anything in guest/ changed after it was built.
        if [ -n "$(find "$ROOT/guest" -newer "$ROOT/guest-tools.iso" -type f 2>/dev/null | head -1)" ]; then
            todo "Guest tools disc is out of date"; TOOLS_OK=0
        else
            ok "Guest tools disc built"
        fi
    else
        todo "Guest tools disc not built yet"; TOOLS_OK=0
    fi
}

supply_vmware_iso() {
    echo
    echo "Drag your $VMTOOLS_FILE into this window and press Return"
    echo "(or just press Return to go back)."
    local raw f
    read -r -p "File: " raw || raw=""
    f=$(clean_path "${raw:-}")
    [ -n "$f" ] || return 1
    if [ ! -f "$f" ]; then bad "Not a file: $f"; return 1; fi
    echo "  Checking it..."
    if sha_ok "$f" "$VMTOOLS_SHA256"; then
        mkdir -p "$CACHE"
        cp -c "$f" "$CACHE/$VMTOOLS_FILE" 2>/dev/null || cp "$f" "$CACHE/$VMTOOLS_FILE"
        ok "That's the right file. Using it (it will not be downloaded)."
        return 0
    fi
    bad "That is not VMware Tools 10.3.10 (build 12406962). Other versions carry a"
    bad "different display driver that this project has not been tested with."
    return 1
}

run_setup() {
    echo
    if bash "$ROOT/Setup.command" --auto; then
        return 0
    fi
    err "Setup did not finish - see the messages above."
    return 1
}

say "Step 1: tools and downloads"
tools_status
if [ "$TOOLS_OK" = 1 ]; then
    echo
    case $(menu "Everything is already here. What now?" \
            "Use it as it is" \
            "Rebuild the guest tools disc" \
            "Download everything again (clears the cache)") in
        1) : ;;
        2) safe_rm "$ROOT/guest-tools.iso"; run_setup || { pause; exit 1; } ;;
        3) if yes_no "Delete the downloads in $CACHE and fetch them again?" n; then
               safe_rm "$CACHE" "$ROOT/runtime" "$ROOT/guest-tools.iso"
               run_setup || { pause; exit 1; }
           fi ;;
    esac
else
    echo
    while true; do
        c=$(menu "Some things are missing. How do you want to get them?" \
                "Get what is missing (only downloads what is not already on this Mac)" \
                "I already have the VMware Tools ISO - let me point to it" \
                "Quit for now")
        case "$c" in
            1) run_setup && break; pause "Press Return to close."; exit 1 ;;
            2) supply_vmware_iso ;;
            3) exit 0 ;;
        esac
    done
    echo
    tools_status
    [ "$TOOLS_OK" = 1 ] || { err "Something is still missing - see above."; pause; exit 1; }
fi

# --------------------------------------------------------------- VM list
list_vms() {   # fills VM_CONFS and prints a numbered list
    VM_CONFS=()
    local f disk
    # Adopt stray disks that have no record (e.g. made by the Start-*.command
    # launchers) so they are not forgotten.
    for disk in "$VMS"/*.qcow2; do
        [ -f "$disk" ] || continue
        f="${disk%.qcow2}.conf"
        [ -f "$f" ] && continue
        conf_set "$f" VM_NAME "$(basename "$disk" .qcow2)"
        conf_set "$f" DISK "$disk"
        case "$(basename "$disk")" in *[Vv]ista*) conf_set "$f" GUEST vista ;; *) conf_set "$f" GUEST win7 ;; esac
        conf_set "$f" VM_CPUS 4; conf_set "$f" MEM 4096; conf_set "$f" DISK_SIZE 40G
        if [ "$(disk_used_bytes "$disk")" -gt $((3 * 1073741824)) ]; then conf_set "$f" STAGE installed; else conf_set "$f" STAGE new; fi
    done
    for f in "$VMS"/*.conf; do
        [ -f "$f" ] || continue
        VM_CONFS+=("$f")
    done
}

describe_vm() {   # one line about a VM
    local f="$1" name disk stage used state=""
    name="$(conf_get "$f" VM_NAME)"; disk="$(conf_get "$f" DISK)"; stage="$(conf_get "$f" STAGE)"
    used="$(human_size "$(disk_used_bytes "$disk")")"
    vm_running "$disk" && state=" - RUNNING NOW"
    if [ ! -f "$disk" ]; then
        if [ "$stage" = new ] || [ -z "$stage" ]; then state=" - not started yet"; else state=" - WARNING: its disk file is missing"; fi
    fi
    printf '%s (%s, %s used) - %s%s' "$name" "$(conf_get "$f" GUEST | sed 's/win7/Windows 7/; s/vista/Vista/')" "$used" "$(stage_label "$stage")" "$state"
}

create_vm() {   # sets CONF to the new record
    say "New VM"
    local sel guest label name safe size cores mem total_cores total_mem def_cores def_mem
    sel=$(menu "Which Windows?" "Windows 7" "Windows Vista")
    if [ "$sel" = 2 ]; then guest=vista; label="Windows Vista"; else guest=win7; label="Windows 7"; fi

    while true; do
        name=$(ask "Name for this VM" "$label")
        safe=$(printf '%s' "$name" | tr -cs 'A-Za-z0-9._-' '-' | sed 's/^-*//; s/-*$//')
        [ -n "$safe" ] || { echo "  Please use at least one letter or number."; continue; }
        if [ -f "$VMS/$safe.conf" ] || [ -f "$VMS/$safe.qcow2" ]; then
            warn "There is already a VM called \"$name\"."
            case $(menu "What should happen?" "Pick a different name" "Continue setting up the existing one instead") in
                1) continue ;;
                2) CONF="$VMS/$safe.conf"; list_vms >/dev/null; return 0 ;;
            esac
        fi
        break
    done

    size=$(ask "Disk size in GB (it only uses what it needs)" "40")
    size="${size%[gG][bB]}"; size="${size%[gG]}"
    case "$size" in ''|*[!0-9]*) warn "Not a number, using 40."; size=40 ;; esac
    [ "$size" -ge 30 ] || { warn "Windows 7 with updates needs more than 20 GB plus its page file - using 30."; size=30; }

    total_cores=$(sysctl -n hw.perflevel0.logicalcpu 2>/dev/null || sysctl -n hw.ncpu)
    def_cores=$(( total_cores > 4 ? 4 : total_cores ))
    cores=$(ask "CPU cores (this Mac has $total_cores fast cores)" "$def_cores")
    case "$cores" in ''|*[!0-9]*) cores=$def_cores ;; esac
    { [ "$cores" -ge 1 ] && [ "$cores" -le "$total_cores" ]; } || { warn "Using $def_cores."; cores=$def_cores; }
    if [ "$cores" -gt 4 ]; then
        warn "More than 4 cores barely speeds Windows up under emulation and makes installs more likely to fail."
        yes_no "Use 4 instead?" y && cores=4
    fi

    total_mem=$(( $(sysctl -n hw.memsize) / 1048576 ))
    def_mem=$(( total_mem >= 16384 ? 4096 : 2048 ))
    mem=$(ask "Memory in MB (this Mac has $total_mem MB)" "$def_mem")
    case "$mem" in ''|*[!0-9]*) mem=$def_mem ;; esac
    { [ "$mem" -ge 1024 ] && [ "$mem" -le $(( total_mem - 4096 )) ]; } || { warn "Using $def_mem MB."; mem=$def_mem; }

    CONF="$VMS/$safe.conf"
    : > "$CONF"
    conf_set "$CONF" VM_NAME "$name"
    conf_set "$CONF" GUEST "$guest"
    conf_set "$CONF" DISK "$VMS/$safe.qcow2"
    conf_set "$CONF" DISK_SIZE "${size}G"
    conf_set "$CONF" VM_CPUS "$cores"
    conf_set "$CONF" MEM "$mem"
    conf_set "$CONF" STAGE new
    if [ "$guest" = vista ]; then conf_set "$CONF" AUDIO_DEVICE usb; else conf_set "$CONF" AUDIO_DEVICE hda; fi
    conf_set "$CONF" NIC e1000
    conf_set "$CONF" CLIPBOARD on
    ok "Saved: $label, $cores cores, $mem MB RAM, ${size} GB disk"
}

# ---------------------------------------------------- Windows ISO choice
choose_iso() {   # choose_iso <conf>; stores ISO in the record
    local conf="$1" prev raw f bytes label
    label="$(conf_get "$conf" GUEST | sed 's/win7/Windows 7/; s/vista/Windows Vista/')"
    prev="$(conf_get "$conf" ISO)"
    if [ -n "$prev" ] && [ -f "$prev" ]; then
        echo "  Last time you used: $prev"
        case $(menu "Which installer?" "Use that one again" "Use a different ISO") in
            1) return 0 ;;
        esac
    elif [ -n "$prev" ]; then
        warn "The ISO used last time is gone: $prev"
    fi
    while true; do
        echo
        echo "Drag your $label installation ISO into this window and press Return."
        read -r -p "ISO: " raw || { echo; return 1; }
        f=$(clean_path "${raw:-}")
        [ -n "$f" ] || { echo "  (Press Return on an empty line again to go back.)"; read -r raw || return 1; [ -z "$raw" ] && return 1; f=$(clean_path "$raw"); }
        if [ ! -f "$f" ]; then bad "Not a file: $f"; continue; fi
        case "$f" in *.[iI][sS][oO]) ;; *) bad "That is not an .iso file."; continue ;; esac
        if [ "$(basename "$f")" = "$VMTOOLS_FILE" ] || [ "$(basename "$f")" = "guest-tools.iso" ]; then
            bad "That is the driver disc, not Windows. Drag the Windows installer ISO."; continue
        fi
        bytes=$(stat -f %z "$f")
        if [ "$bytes" -lt $((1500 * 1048576)) ]; then
            bad "That file is only $(human_size "$bytes") - Windows installers are 2.5 GB or more."; continue
        fi
        case "$(basename "$f" | tr 'A-Z' 'a-z')" in
            *vista*) [ "$label" = "Windows Vista" ] || yes_no "The file name mentions Vista but this VM is Windows 7. Use it anyway?" n || continue ;;
            *win*7*|*windows*7*) [ "$label" = "Windows 7" ] || yes_no "The file name mentions Windows 7 but this VM is Vista. Use it anyway?" n || continue ;;
        esac
        conf_set "$conf" ISO "$f"
        ok "Using $(basename "$f") ($(human_size "$bytes"))"
        return 0
    done
}

# ------------------------------------------------------------- the stages
stage_install() {   # stage_install <conf>
    local conf="$1" disk used vbytes
    disk="$(conf_get "$conf" DISK)"
    say "Install Windows"
    if [ -f "$disk" ]; then
        vbytes="$(disk_virtual_bytes "$disk")"
        if [ "$vbytes" -lt 1073741824 ]; then
            warn "The disk from an earlier attempt is broken (too small). Making a new one."
            safe_rm "$disk"
        else
            used="$(disk_used_bytes "$disk")"
            if [ "$used" -gt $((3 * 1073741824)) ]; then
                echo "  This VM's disk already holds $(human_size "$used") - Windows may already be installed."
                case $(menu "What should happen?" \
                        "Windows is installed - move on to the drivers" \
                        "Boot the installer again (continue or repair the install)" \
                        "Erase this disk and install from scratch") in
                    1) conf_set "$conf" STAGE installed; return 0 ;;
                    2) ;;
                    3) yes_no "Really erase $(conf_get "$conf" VM_NAME)'s disk? This cannot be undone" n || return 1
                       safe_rm "$disk" ;;
                esac
            fi
        fi
    fi
    choose_iso "$conf" || return 1
    cat <<'EOF'

A VM window opens and boots the Windows installer.
  * Install Windows normally, onto the single unallocated disk.
  * It reboots a few times on its own. Let it.
  * Answer the first-run questions until you reach the desktop.
  * Then SHUT WINDOWS DOWN (Start > Shut down). This window waits for that.
EOF
    pause "Press Return to start the installer."
    run_vm install "$conf" "Installing $(conf_get "$conf" VM_NAME)" || return 1
    used="$(disk_used_bytes "$disk")"
    echo "  The disk now holds $(human_size "$used")."
    if [ "$used" -lt $((3 * 1073741824)) ]; then
        warn "That is too little for an installed Windows - the install probably did not finish."
        yes_no "Did Windows finish installing and reach the desktop anyway?" n || return 1
    else
        yes_no "Did Windows finish installing and reach the desktop?" y || return 1
    fi
    conf_set "$conf" STAGE installed
    ok "Windows installed."
}

stage_drivers() {   # stage_drivers <conf>
    local conf="$1" log
    say "Graphics driver and tools inside Windows"
    cat <<'EOF'
The VM starts with a CD called AEROTOOLS attached. Inside Windows:
  1. Open Computer, then the AEROTOOLS CD drive.
  2. Double-click SETUP.CMD and say Yes to the admin prompt.
  3. If the VM window resizes itself, the graphics driver is already
     running. Say Y when SETUP offers WinSAT (or Control Panel >
     Performance Information and Tools > Rate this computer) - Windows
     only turns Aero on after rating the new driver.
  4. Restart Windows once (Start > Restart). Aero comes on after that.
  5. Then SHUT WINDOWS DOWN. This window waits for that.

It installs the display driver, the auto-resize agent, the GPU watchdog fix
and your Mac's GPU name. It does NOT install VMware Tools.
EOF
    pause "Press Return to start the VM."
    log="$LOGS/$(basename "$conf" .conf)-setup.log"
    run_vm setup "$conf" "Driver setup for $(conf_get "$conf" VM_NAME)" || return 1
    # After the restart, the 3D driver draws through the host GPU: the device
    # logs non-zero D3D9 draw counts.  That is proof the driver is active.
    if grep -aq 'draw9=[1-9]' "$log" 2>/dev/null; then
        ok "The 3D graphics driver is active (the Mac GPU is drawing Windows)."
        conf_set "$conf" STAGE ready
        return 0
    fi
    warn "The 3D graphics driver did not show up as active during that session."
    echo "  That is normal if you did not restart Windows after SETUP.CMD."
    case $(menu "What happened?" \
            "SETUP.CMD finished (I shut down without restarting) - mark it done" \
            "Something went wrong - I'll run this step again later") in
        1) conf_set "$conf" STAGE ready; ok "Marked as done. The driver loads on the next boot." ;;
        2) return 1 ;;
    esac
}

make_launcher() {   # make_launcher <conf>
    local conf="$1" name launcher
    name="$(conf_get "$conf" VM_NAME)"
    launcher="$HOME/Desktop/$name.command"
    if [ -f "$launcher" ] && grep -qF -- "--start $(printf %q "$conf")" "$launcher"; then
        ok "Desktop launcher already there: $name.command"
        return 0
    fi
    if [ -f "$launcher" ] && ! grep -q "Created by Aero on Apple Silicon" "$launcher"; then
        yes_no "There is already a \"$name.command\" on your Desktop for something else. Replace it?" n || return 0
    fi
    cat > "$launcher" <<EOF
#!/bin/bash
# Opens Aero on Apple Silicon and starts "$name". Created by Aero on Apple Silicon.
cd $(printf %q "$ROOT") || { echo "The project folder has moved: $ROOT"; read -r -p "Press Return."; exit 1; }
exec ./START\\ HERE.command --start $(printf %q "$conf")
EOF
    chmod +x "$launcher"
    ok "Created a launcher on your Desktop: $name.command"
}

start_vm() {   # start_vm <conf>
    local conf="$1"
    open_window "GUEST=$(conf_get "$conf" GUEST) DISK=$(printf %q "$(conf_get "$conf" DISK)") VM_CPUS=$(conf_get "$conf" VM_CPUS) MEM=$(conf_get "$conf" MEM) HOST_LOG=$(printf %q "$LOGS/$(basename "$conf" .conf)-run.log") bash build/run-vm.sh; exit" "$(conf_get "$conf" VM_NAME)" >/dev/null \
        || warn "Could not open a window - use the Desktop launcher instead."
}

# continue_vm <conf>: take one VM as far as it can go, from wherever it is.
continue_vm() {
    local conf="$1" disk stage
    disk="$(conf_get "$conf" DISK)"

    if vm_running "$disk"; then
        warn "$(conf_get "$conf" VM_NAME) is running right now."
        case $(menu "What should happen?" "Wait here until it is shut down, then continue" "Leave it and go back") in
            1) echo "  Waiting for it to close..."; while vm_running "$disk"; do sleep 3; done; ok "It has closed." ;;
            2) return 0 ;;
        esac
    fi

    stage="$(conf_get "$conf" STAGE)"
    if [ ! -f "$disk" ] && [ "$stage" != new ] && [ -n "$stage" ]; then
        warn "$(conf_get "$conf" VM_NAME)'s disk file is gone: $disk"
        case $(menu "What should happen?" \
                "I moved it - let me point to the .qcow2 file" \
                "Start this VM over (install Windows again)" \
                "Remove it from the list (deletes only its settings)" \
                "Go back") in
            1) local raw f
               read -r -p "Drag the .qcow2 file here: " raw || return 1
               f=$(clean_path "$raw")
               case "$f" in *.qcow2) ;; *) bad "That is not a .qcow2 disk."; return 1 ;; esac
               [ -f "$f" ] || { bad "Not a file: $f"; return 1; }
               conf_set "$conf" DISK "$f"; disk="$f"; ok "Using $f" ;;
            2) conf_set "$conf" STAGE new ;;
            3) yes_no "Remove $(conf_get "$conf" VM_NAME) from the list?" n && safe_rm "$conf"; return 0 ;;
            4) return 0 ;;
        esac
    fi

    while true; do
        stage="$(conf_get "$conf" STAGE)"
        case "$stage" in
            new|'')
                stage_install "$conf" || { warn "Stopped. Run START HERE again to pick up from here."; return 1; } ;;
            installed)
                stage_drivers "$conf" || { warn "Stopped. Run START HERE again to pick up from here."; return 1; } ;;
            check)
                # The plain wizard has no automatic checks; treat it as ready.
                conf_set "$conf" STAGE ready ;;
            ready)
                say "$(conf_get "$conf" VM_NAME) is ready"
                make_launcher "$conf"
                case $(menu "What now?" \
                        "Start it" \
                        "Run the driver setup again (e.g. after a driver problem)" \
                        "Finish here") in
                    1) start_vm "$conf"; return 0 ;;
                    2) conf_set "$conf" STAGE installed ;;
                    3) return 0 ;;
                esac ;;
            *)
                conf_set "$conf" STAGE new ;;
        esac
    done
}


# ------------------------------------------------------------- removing
# Deletes only paths inside this project or the download cache - never
# anything else, whatever a settings file says.
safe_rm() {   # safe_rm <path>...
    local p real root_real cache_real
    root_real="$(cd "$ROOT" && pwd -P)"
    cache_real="$(cd "$CACHE" 2>/dev/null && pwd -P || printf '%s' "$CACHE")"
    for p in "$@"; do
        [ -e "$p" ] || [ -L "$p" ] || continue
        real="$(cd "$(dirname "$p")" 2>/dev/null && pwd -P)/$(basename "$p")"
        case "$real" in
            "$root_real"/*|"$cache_real"|"$cache_real"/*) rm -rf "$p" ;;
            *) warn "Not deleting $p - it is outside this project." ;;
        esac
    done
}

size_of() {   # human size of files/folders (missing ones count as 0)
    local total=0 p
    for p in "$@"; do [ -e "$p" ] && total=$(( total + $(du -sk "$p" | cut -f1) )); done
    human_size $(( total * 1024 ))
}

delete_vm() {   # delete_vm <conf>
    local conf="$1" name disk slug launcher
    name="$(conf_get "$conf" VM_NAME)"; disk="$(conf_get "$conf" DISK)"
    slug="$(basename "$conf" .conf)"; launcher="$HOME/Desktop/$name.command"
    if vm_running "$disk"; then
        bad "$name is running. Shut Windows down first, then delete it."
        return 1
    fi
    say "Delete $name"
    echo "  This permanently deletes:"
    [ -f "$disk" ] && echo "    - its disk, with Windows and everything in it ($(size_of "$disk"))"
    echo "    - its settings"
    ls "$LOGS/$slug"-*.log >/dev/null 2>&1 && echo "    - its log files"
    if [ -f "$launcher" ] && grep -qF "$disk" "$launcher" 2>/dev/null; then echo "    - its Desktop launcher"; else launcher=""; fi
    echo
    local typed
    read -r -p "  Type the VM's name ($name) to confirm, or press Return to cancel: " typed || typed=""
    [ "$typed" = "$name" ] || { echo "  Cancelled. Nothing was deleted."; return 1; }
    safe_rm "$disk" "$conf" "$LOGS/$slug"-*.log
    # The launcher lives on the Desktop, outside the project, so it is removed
    # only after checking it is ours and points at this VM's disk.
    if [ -n "$launcher" ] && grep -q "Created by Aero on Apple Silicon" "$launcher"; then rm -f "$launcher"; fi
    ok "$name deleted."
}

remove_menu() {
    local c conf_list f i
    while true; do
        say "Remove things"
        c=$(menu "What should be removed?" \
                "Delete a VM" \
                "Downloaded files in the cache ($(size_of "$CACHE"))" \
                "The runtime in this folder ($(size_of "$ROOT/runtime"))" \
                "The guest tools disc ($(size_of "$ROOT/guest-tools.iso"))" \
                "Everything except my VMs (cache, runtime, disc, logs)" \
                "Back")
        case "$c" in
            1) list_vms
               [ "${#VM_CONFS[@]}" -gt 0 ] || { echo "  There are no VMs."; continue; }
               conf_list=()
               for f in "${VM_CONFS[@]}"; do conf_list+=("$(describe_vm "$f")"); done
               conf_list+=("Back")
               i=$(menu "Which VM?" "${conf_list[@]}")
               [ "$i" -le "${#VM_CONFS[@]}" ] && delete_vm "${VM_CONFS[$((i - 1))]}" ;;
            2) yes_no "Delete the downloads? They are fetched again the next time you set up tools" n && safe_rm "$CACHE" && ok "Removed." ;;
            3) yes_no "Delete the runtime? VMs cannot start until the tools are set up again" n && safe_rm "$ROOT/runtime" && ok "Removed." ;;
            4) yes_no "Delete the guest tools disc? It is rebuilt automatically when needed" y && safe_rm "$ROOT/guest-tools.iso" && ok "Removed." ;;
            5) yes_no "Delete the cache, runtime, disc and logs? Your VMs are kept" n \
                   && safe_rm "$CACHE" "$ROOT/runtime" "$ROOT/guest-tools.iso" "$ROOT/downloads" "$LOGS" \
                   && mkdir -p "$LOGS" && ok "Removed. Your VMs are untouched." ;;
            6) return 0 ;;
        esac
    done
}

# ============================================================ main menu
while true; do
    say "Your VMs"
    list_vms
    if [ "${#VM_CONFS[@]}" -eq 0 ]; then
        echo "  None yet."
        CONF=""
        create_vm && continue_vm "$CONF"
    else
        opts=()
        for f in "${VM_CONFS[@]}"; do opts+=("$(describe_vm "$f")"); done
        opts+=("Create a new VM" "Remove things (VMs, downloads...)" "Quit")
        c=$(menu "Pick a VM to continue with, or create a new one" "${opts[@]}")
        if [ "$c" -le "${#VM_CONFS[@]}" ]; then
            continue_vm "${VM_CONFS[$((c - 1))]}"
        elif [ "$c" -eq $(( ${#VM_CONFS[@]} + 1 )) ]; then
            CONF=""
            create_vm && continue_vm "$CONF"
        elif [ "$c" -eq $(( ${#VM_CONFS[@]} + 2 )) ]; then
            remove_menu
            continue
        else
            break
        fi
    fi
    echo
    yes_no "Back to the list of VMs?" n || break
done

cat <<'EOF'

 Tips:
   * Resize the VM window and Windows follows the size.
   * A plain colour desktop background makes logging in much faster.
   * Do not install VMware Tools.
   * Problems? TECHNICAL.md in this folder, or the logs/ folder.

EOF
pause "Press Return to close this window."
