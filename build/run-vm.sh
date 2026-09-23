#!/bin/bash
#
# Shared launcher for the Windows 7 / Vista VMs.
#
# Not run directly - Start-Windows7.command and Start-Vista.command set
# GUEST and source this.
#
#   MODE=install ISO=/path/to/windows.iso   install Windows from your own ISO
#   MODE=setup                              boot with guest-tools.iso attached
#   MODE=run (default)                      boot the installed system
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUEST="${GUEST:-win7}"

QEMU="${QEMU:-$ROOT/runtime/bin/qemu-system-x86_64}"
QEMU_IMG="${QEMU_IMG:-$ROOT/runtime/bin/qemu-img}"
VM_DIR="${VM_DIR:-$ROOT/vms}"
DISK="${DISK:-$VM_DIR/$GUEST.qcow2}"
DISK_SIZE="${DISK_SIZE:-40G}"
# A bare number means gigabytes ("20" -> "20G"), never bytes.
case "$DISK_SIZE" in *[0-9]) DISK_SIZE="${DISK_SIZE}G" ;; esac
GUEST_TOOLS_ISO="${GUEST_TOOLS_ISO:-$ROOT/guest-tools.iso}"
HOST_LOG="${HOST_LOG:-$ROOT/logs/$GUEST-host.log}"
MODE="${MODE:-run}"
ISO="${ISO:-}"

# The DXVK libraries live next to the QEMU binary; the device resolves them
# through $VMVGA_LIBDIR first.
export VMVGA_LIBDIR="${VMVGA_LIBDIR:-$ROOT/runtime/lib}"
# DXVK writes <exe>_d3d9.log into the working directory unless told otherwise.
export DXVK_LOG_PATH="${DXVK_LOG_PATH:-$ROOT/logs}"
# BIOS/firmware images shipped with the runtime.
QEMU_DATA="${QEMU_DATA:-$ROOT/runtime/share/qemu}"

MEM="${MEM:-4096}"
VM_CPUS="${VM_CPUS:-4}"          # 1 socket x N cores (Windows client editions
                                 # only use 2 sockets)
TCG_THREAD="${TCG_THREAD:-multi}"  # one host thread per vCPU; =single is safer
TB_SIZE="${TB_SIZE:-1024}"       # TCG translation cache, MiB
CPU_MODEL="${CPU_MODEL:-max}"
HPET="${HPET:-on}"
VMVGA_DEBUG="${VMVGA_DEBUG:-off}"
VGPU_GENERATION="${VGPU_GENERATION:-9}"
# Vista's in-box HD Audio driver has no MSI and its HDA path is unreliable
# here, so Vista defaults to the USB audio class device instead.
if [ "$GUEST" = "vista" ]; then
    AUDIO_DEVICE="${AUDIO_DEVICE:-usb}"
else
    AUDIO_DEVICE="${AUDIO_DEVICE:-hda}"
fi
# CoreAudio's ~46 ms default is far too little for a TCG guest that stalls.
AUDIO_BUFFER_US="${AUDIO_BUFFER_US:-46440}"
AUDIO_BUFFERS="${AUDIO_BUFFERS:-8}"
HDA_MSI="${HDA_MSI:-auto}"
HDA_USE_TIMER="${HDA_USE_TIMER:-on}"
# e1000 (Intel PRO/1000): Vista and 7, 32- and 64-bit, have a driver built in.
NIC="${NIC:-e1000}"
# Clipboard sharing: virtio-serial + QEMU's vdagent (the SPICE agent in the
# guest, installed by SETUP.CMD, does the Windows side).
CLIPBOARD="${CLIPBOARD:-on}"
# The VM window's red close button pulls the plug on Windows, which can wreck
# an install. It is off; shut down from Windows or the setup screen instead.
WINDOW_CLOSE="${WINDOW_CLOSE:-off}"
# Control socket the setup screen uses (shut down, keys, discs, status).
# Kept short: macOS limits socket paths to 104 characters.
QMP_SOCK="${QMP_SOCK:-/tmp/aero-$(printf '%s' "$DISK" | md5 | cut -c1-12).sock}"

# ---------------------------------------------------------------------
# Rendering and stability fixes.  All are opt-in and independent, and all are
# on by default here because together they are what makes Aero work.  Set any
# to 0 to bisect a problem, e.g. EXPERIMENTAL_OFF=1 disables the lot.
#
#   DXVK_CHUNK_FREE_GRACE / DXVK_HOLD_EMPTY_CHUNKS
#       don't hand DXVK memory chunks back while Metal may still be using them
#   DXVK_MVK_COMPLETION_FENCE
#       release a submission's resources only once Metal reports the command
#       buffer complete, not when MoltenVK's in-buffer timeline signal fires
#   DXVK_MVK_STATIC_VERTEX_STRIDES
#       never use dynamic vertex strides (garbage-stride fetches on MoltenVK)
#   VMVGA_ARTIFACT_FULL_REPLAY + VMVGA_REPLAY_NORESET
#       replay the D3D9 state before each draw: the Aero window-border fix
#   VMVGA_X8_AS_A8, VMVGA_DMA_PARTIAL_UPLOAD, VMVGA_PRESERVE_ON_RECREATE,
#   VMVGA_CLEAR_PRESERVE, DXVK_FORCE_DEPTH_LOAD, DXVK_FORCE_COLOR_LOAD,
#   DXVK_NO_COLOR_STORE_DISCARD
#       surface/format and render-pass behaviour DWM depends on
#   VMVGA_DYNAMIC_RES
#       resizing the host window switches Windows to that exact resolution
#   VMVGA_D3D9_EXTRA_CAPS
#       report UYVY/YUY2/A8B8G8R8 when DXVK supports them
# See TECHNICAL.md for what each one fixes and how it was found.
# ---------------------------------------------------------------------
if [ "${EXPERIMENTAL_OFF:-0}" != "1" ]; then
    export DXVK_CHUNK_FREE_GRACE="${DXVK_CHUNK_FREE_GRACE:-1}"
    export DXVK_MVK_COMPLETION_FENCE="${DXVK_MVK_COMPLETION_FENCE:-1}"
    export DXVK_MVK_STATIC_VERTEX_STRIDES="${DXVK_MVK_STATIC_VERTEX_STRIDES:-1}"
    export DXVK_HOLD_EMPTY_CHUNKS="${DXVK_HOLD_EMPTY_CHUNKS:-1}"
    export DXVK_MVK_WAIT_IDLE_ON_RESOURCE_DESTROY="${DXVK_MVK_WAIT_IDLE_ON_RESOURCE_DESTROY:-0}"
    export DXVK_FORCE_DEPTH_LOAD="${DXVK_FORCE_DEPTH_LOAD:-1}"
    export DXVK_FORCE_COLOR_LOAD="${DXVK_FORCE_COLOR_LOAD:-1}"
    export DXVK_NO_COLOR_STORE_DISCARD="${DXVK_NO_COLOR_STORE_DISCARD:-1}"
    export DXVK_FORCE_FULL_RENDER_AREA="${DXVK_FORCE_FULL_RENDER_AREA:-0}"
    export VMVGA_DMA_PARTIAL_UPLOAD="${VMVGA_DMA_PARTIAL_UPLOAD:-1}"
    export VMVGA_X8_AS_A8="${VMVGA_X8_AS_A8:-1}"
    export VMVGA_BLIT_FULL="${VMVGA_BLIT_FULL:-0}"
    export VMVGA_DYNAMIC_RES="${VMVGA_DYNAMIC_RES:-1}"
    export VMVGA_UPLOAD_FRESH_STAGING="${VMVGA_UPLOAD_FRESH_STAGING:-0}"
    export VMVGA_ARTIFACT_FULL_REPLAY="${VMVGA_ARTIFACT_FULL_REPLAY:-1}"
    export VMVGA_REPLAY_NORESET="${VMVGA_REPLAY_NORESET:-1}"
    export VMVGA_COVERAGE_GATED_PRESENT="${VMVGA_COVERAGE_GATED_PRESENT:-0}"
    export VMVGA_PRESERVE_ON_RECREATE="${VMVGA_PRESERVE_ON_RECREATE:-1}"
    export VMVGA_CLEAR_PRESERVE="${VMVGA_CLEAR_PRESERVE:-1}"
    export VMVGA_D3D9_EXTRA_CAPS="${VMVGA_D3D9_EXTRA_CAPS:-1}"
fi

if [ ! -x "$QEMU" ]; then
    echo "QEMU is missing: $QEMU"
    echo "Run Setup.command first (or build/build-from-source.sh)."
    read -r -p "Press Return to close." _ || true
    exit 1
fi

mkdir -p "$VM_DIR" "$(dirname "$HOST_LOG")"
if [ ! -f "$DISK" ]; then
    echo "Creating a $DISK_SIZE disk: $DISK"
    "$QEMU_IMG" create -f qcow2 "$DISK" "$DISK_SIZE" >/dev/null
fi

args=()
[ -d "$QEMU_DATA" ] && args+=( -L "$QEMU_DATA" )
args+=(
    -M "pc,vmport=on,hpet=$HPET"
    -accel "tcg,tb-size=$TB_SIZE,thread=$TCG_THREAD"
    -cpu "$CPU_MODEL"
    # VMware's driver checks the platform, so present VMware-consistent SMBIOS
    # values to match the VMPort + VMVGA hardware this VM exposes.
    -smbios type=1,manufacturer=VMware\,product=VMware\ Virtual\ Platform
    -smp "$VM_CPUS,sockets=1,cores=$VM_CPUS,threads=1"
    -m "$MEM"
    -rtc base=localtime
    # -display sdl is required: the 3D path presents through QEMU's SDL window.
    -display "sdl,window-close=$WINDOW_CLOSE"
    -vga none
    -device "vmvga,vgpu=$VGPU_GENERATION,debug=$VMVGA_DEBUG"
    # if=ide: neither Vista nor 7 has an in-box AHCI or virtio disk driver.
    -drive "file=$DISK,format=qcow2,if=ide"
    -netdev user,id=net0
    -device "$NIC,netdev=net0"
    -qmp "unix:$QMP_SOCK,server=on,wait=off"
    -usb -device usb-tablet
    -name "$GUEST"
)

case "$AUDIO_DEVICE" in
  hda)
    args+=(
        -audiodev "coreaudio,id=snd0,out.buffer-length=$AUDIO_BUFFER_US,out.buffer-count=$AUDIO_BUFFERS"
        -device "intel-hda,msi=$HDA_MSI"
        # hda-output, not hda-duplex: the duplex codec wants a capture voice
        # that CoreAudio cannot create here.
        -device "hda-output,audiodev=snd0,use-timer=$HDA_USE_TIMER"
    )
    ;;
  usb)
    args+=(
        -audiodev "coreaudio,id=snd0,out.buffer-length=$AUDIO_BUFFER_US,out.buffer-count=$AUDIO_BUFFERS"
        -device "usb-audio,audiodev=snd0,multi=off"
    )
    ;;
  none) ;;
  *) echo "Unknown AUDIO_DEVICE=$AUDIO_DEVICE (hda, usb or none)" >&2; exit 1 ;;
esac

if [ "$CLIPBOARD" = on ]; then
    args+=(
        -device virtio-serial-pci
        -chardev qemu-vdagent,id=vdagent,name=vdagent,clipboard=on
        -device virtserialport,chardev=vdagent,name=com.redhat.spice.0
    )
fi

# One CD drive is always present (id cd0), so discs can be inserted and
# ejected while the VM runs.
CD_FILE=""
case "$MODE" in
  install)
    [ -n "$ISO" ] || { echo "MODE=install needs your Windows ISO: ISO=/path/to/windows.iso" >&2; exit 1; }
    [ -f "$ISO" ] || { echo "ISO not found: $ISO" >&2; exit 1; }
    CD_FILE="$ISO"
    args+=( -boot order=dc,menu=on )
    echo "Installing Windows onto $DISK"
    ;;
  setup)
    if [ -f "$GUEST_TOOLS_ISO" ]; then
        CD_FILE="$GUEST_TOOLS_ISO"
        echo "Booting with guest-tools.iso attached. In Windows, open the CD and run SETUP.CMD as Administrator."
    else
        echo "guest-tools.iso is missing - run Setup.command first. Booting without it."
    fi
    args+=( -boot order=c,menu=on )
    ;;
  run)
    args+=( -boot order=c,menu=on )
    ;;
  *)
    echo "Unknown MODE=$MODE (use run, setup or install)" >&2
    exit 1
    ;;
esac

if [ -n "$CD_FILE" ]; then
    args+=( -drive "if=none,id=cd0,media=cdrom,readonly=on,file=$CD_FILE" )
else
    args+=( -drive "if=none,id=cd0,media=cdrom,readonly=on" )
fi
args+=( -device "ide-cd,drive=cd0,id=cdrom,bus=ide.1,unit=0" )

# Appended verbatim, e.g. EXTRA_QEMU_ARGS="-snapshot"
if [ -n "${EXTRA_QEMU_ARGS:-}" ]; then
    # shellcheck disable=SC2206
    args+=( $EXTRA_QEMU_ARGS )
fi

# One VM per disk: a second launch would truncate the running VM's log and
# then fail on the locked disk.
if pgrep -f -- "[q]emu-system.*file=$DISK" >/dev/null 2>&1; then
    echo "This VM is already running. Not starting a second copy."
    read -r -p "Press Return to close." _ || true
    exit 1
fi

rm -f "$QMP_SOCK"
# Keep the Mac awake while the VM runs ($$ becomes QEMU after exec).
command -v caffeinate >/dev/null 2>&1 && { caffeinate -i -m -w $$ >/dev/null 2>&1 & }
echo "Log: $HOST_LOG"
exec env DXVK_LOG_LEVEL="${DXVK_LOG_LEVEL:-info}" "$QEMU" "${args[@]}" \
    > >(tee "$HOST_LOG") 2>&1
