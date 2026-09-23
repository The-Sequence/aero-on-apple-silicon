#!/bin/bash
#
# Aero on Apple Silicon - one-click setup.
#
#   1. checks you are on an Apple Silicon Mac with Homebrew
#   2. installs the Homebrew packages QEMU and DXVK need
#   3. downloads the prebuilt runtime (QEMU + DXVK) for this release
#   4. downloads VMware's official Tools ISO and extracts just the
#      SVGA 3D display driver from it  (we are not allowed to redistribute
#      it, so it is fetched from VMware's own server)
#   4b. downloads SPICE guest tools 0.141 for clipboard sharing
#
# Downloads are cached in ~/Library/Caches/AeroOnAppleSilicon and verified by
# SHA-256, so each one happens at most once per Mac. Copies you already have
# in Downloads or on the Desktop are found and reused.
#   5. builds guest-tools.iso, with your Mac's GPU name baked in
#
# Nothing here touches your VMs. Re-running it is safe.
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# --auto: no questions, no "press Return" at the end (used by START HERE).
AUTO=0
[ "${1:-}" = "--auto" ] && AUTO=1

# Everything downloaded is kept in one cache folder OUTSIDE this project, so
# deleting or re-cloning the project never downloads anything twice.  Each
# file is checked against a known SHA-256 before it is trusted.
CACHE="${AERO_CACHE:-$HOME/Library/Caches/AeroOnAppleSilicon}"

RUNTIME_TAG="v1.0.0"
RUNTIME_FILE="aero-runtime-macos-arm64.tar.gz"
RUNTIME_SHA256="499c5a58451c02731d3b320d5d2919247b39f651d602adc5f0b90abea3ddad8e"
RUNTIME_URL="${RUNTIME_URL:-https://github.com/The-Sequence/aero-on-apple-silicon/releases/download/$RUNTIME_TAG/$RUNTIME_FILE}"
RUNTIME_CACHED="$CACHE/aero-runtime-$RUNTIME_TAG-macos-arm64.tar.gz"

# VMware Tools 10.3.10, the last release with the Vista/7 SVGA 3D driver.
VMTOOLS_FILE="VMware-tools-windows-10.3.10-12406962.iso"
VMTOOLS_SHA256="edb889e6cce11aeb568dbf471cee1b3dc26ca72bc671b660ad4872911edbf6da"
VMTOOLS_URL="${VMTOOLS_URL:-https://packages.vmware.com/tools/releases/10.3.10/windows/$VMTOOLS_FILE}"
VMTOOLS_ISO="$CACHE/$VMTOOLS_FILE"

# SPICE guest tools 0.141: the virtio-serial driver and the clipboard agent
# (spice-vdagent) for Windows 7 and Vista.  Same pinning and caching.
SPICE_FILE="spice-guest-tools-0.141.exe"
SPICE_SHA256="b5be0754802bcd7f7fe0ccdb877f8a6224ba13a2af7d84eb087a89b3b0237da2"
SPICE_URL="${SPICE_URL:-https://www.spice-space.org/download/binaries/spice-guest-tools/spice-guest-tools-0.141/$SPICE_FILE}"
SPICE_EXE="$CACHE/$SPICE_FILE"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; [ "$AUTO" = 1 ] || read -r -p "Press Return to close." _ || true; exit 1; }

sha_ok() {   # sha_ok <file> <sha256>
    [ -f "$1" ] && [ "$(shasum -a 256 "$1" | cut -d' ' -f1)" = "$2" ]
}

# fetch <url> <dest> <sha256> - resumable download, verified before use.
fetch() {
    local url="$1" dest="$2" sha="$3"
    mkdir -p "$(dirname "$dest")"
    curl -fL -C - --retry 3 --progress-bar "$url" -o "$dest.part" || return 1
    if sha_ok "$dest.part" "$sha"; then
        mv "$dest.part" "$dest"
    else
        rm -f "$dest.part"
        warn "The download did not match its expected checksum and was discarded."
        return 1
    fi
}

# adopt <name> <sha256> <dest> - reuse a copy the user already has (Downloads,
# Desktop, the old per-project downloads/ folder) instead of downloading.
adopt() {
    local name="$1" sha="$2" dest="$3" f
    while IFS= read -r f; do
        if sha_ok "$f" "$sha"; then
            mkdir -p "$(dirname "$dest")"
            # cp -c clones on APFS: no extra disk space, and deleting the
            # original later does not break the cache.
            cp -c "$f" "$dest" 2>/dev/null || cp "$f" "$dest"
            echo "  Found an existing copy: $f"
            return 0
        fi
    done < <(find "$ROOT/downloads" "$HOME/Downloads" "$HOME/Desktop" -maxdepth 3 -iname "$name" -type f 2>/dev/null)
    return 1
}

# ---------------------------------------------------------------- 1. checks
say "Checking this Mac"
[ "$(uname -s)" = "Darwin" ] || die "This only runs on macOS."
[ "$(uname -m)" = "arm64" ] || die "This needs an Apple Silicon Mac (M1 or newer)."
command -v brew >/dev/null 2>&1 || die "Homebrew is required: https://brew.sh"
echo "  macOS $(sw_vers -productVersion), $(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo Apple Silicon)"

# ---------------------------------------------------------- 2. dependencies
say "Installing dependencies with Homebrew (this can take a while the first time)"
PKGS=(glib pixman sdl2-compat gnutls libpng jpeg-turbo zstd libslirp libusb molten-vk vulkan-loader p7zip cdrtools)
MISSING=()
for p in "${PKGS[@]}"; do
    brew list --formula "$p" >/dev/null 2>&1 || MISSING+=("$p")
done
if [ ${#MISSING[@]} -gt 0 ]; then
    echo "  Installing: ${MISSING[*]}"
    brew install "${MISSING[@]}"
else
    echo "  All present."
fi

# ------------------------------------------------------------- 3. runtime
say "Runtime (patched QEMU + DXVK)"
mkdir -p "$CACHE"
if [ -x "$ROOT/runtime/bin/qemu-system-x86_64" ] && [ "$(cat "$ROOT/runtime/.version" 2>/dev/null)" = "$RUNTIME_TAG" ]; then
    echo "  Already installed ($RUNTIME_TAG)."
else
    if sha_ok "$RUNTIME_CACHED" "$RUNTIME_SHA256"; then
        echo "  Using the cached copy (nothing to download)."
    elif adopt "$RUNTIME_FILE" "$RUNTIME_SHA256" "$RUNTIME_CACHED"; then
        :
    else
        echo "  Downloading $RUNTIME_TAG (about 8 MB)"
        if ! fetch "$RUNTIME_URL" "$RUNTIME_CACHED" "$RUNTIME_SHA256"; then
            # Private repo / no public access: try the GitHub CLI if logged in.
            if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
                echo "  Direct download failed; trying the GitHub CLI"
                rm -f "$RUNTIME_CACHED.part"
                gh release download "$RUNTIME_TAG" --repo The-Sequence/aero-on-apple-silicon \
                    --pattern "$RUNTIME_FILE" --clobber --output "$RUNTIME_CACHED.part" \
                    && sha_ok "$RUNTIME_CACHED.part" "$RUNTIME_SHA256" \
                    && mv "$RUNTIME_CACHED.part" "$RUNTIME_CACHED" \
                    || die "Download failed. Get $RUNTIME_FILE from the release page and put it in $CACHE."
            else
                die "Download failed. Get $RUNTIME_FILE from the release page and put it in $CACHE."
            fi
        fi
    fi
    # Never swap the runtime out from under a running VM.
    if pgrep -f -- "$ROOT/runtime/bin/qemu-system" >/dev/null 2>&1; then
        warn "A VM from this folder is running on the current runtime, so it is not being replaced now."
        warn "Shut the VM down and run setup again to update to $RUNTIME_TAG."
        RUNTIME_DEFERRED=1
    else
        rm -rf "$ROOT/runtime" && mkdir -p "$ROOT/runtime"
    tar xzf "$RUNTIME_CACHED" -C "$ROOT/runtime" --strip-components=1
    [ -x "$ROOT/runtime/bin/qemu-system-x86_64" ] || die "The runtime archive did not contain bin/qemu-system-x86_64."
    xattr -dr com.apple.quarantine "$ROOT/runtime" 2>/dev/null || true
    printf '%s\n' "$RUNTIME_TAG" > "$ROOT/runtime/.version"
    echo "  Installed."
    fi
fi

# ------------------------------------------------- 4. VMware display driver
say "VMware SVGA 3D display driver"
DRV="$CACHE/driver-10.3.10"
if [ -f "$DRV/vm3d.inf" ] && [ "$(ls "$DRV" | wc -l | tr -d ' ')" -ge 27 ]; then
    echo "  Already extracted (nothing to download)."
else
    mkdir -p "$ROOT/downloads"
    if sha_ok "$VMTOOLS_ISO" "$VMTOOLS_SHA256"; then
        echo "  Using the cached VMware Tools ISO (nothing to download)."
    elif adopt "$VMTOOLS_FILE" "$VMTOOLS_SHA256" "$VMTOOLS_ISO"; then
        :
    else
        echo "  This downloads VMware Tools 10.3.10 from VMware's own server (about 120 MB), once."
        echo "  Only the display driver is used; VMware Tools is never installed in the guest."
        a=""
        [ "$AUTO" = 1 ] || read -r -p "  Download now? [Y/n] " a || a=""
        case "$a" in [nN]*) warn "Skipped. guest-tools.iso will carry the VMware Tools installer instead, and Windows will extract the driver itself."; ;; *)
            fetch "$VMTOOLS_URL" "$VMTOOLS_ISO" "$VMTOOLS_SHA256" \
                || warn "Download failed - run this again to resume it, or the guest will extract the driver instead." ;;
        esac
    fi
    if [ -f "$VMTOOLS_ISO" ]; then
        echo "  Extracting the driver"
        # The display driver lives in VmVideo.cab inside setup64.exe, with
        # mangled names like _vm3dmp.sys_Vista.<GUID>.  Unpack ISO -> exe ->
        # cab, then restore the names the INF expects.
        rm -rf "$ROOT/downloads/vmt" && mkdir -p "$ROOT/downloads/vmt" "$DRV"
        7z x -y -o"$ROOT/downloads/vmt" "$VMTOOLS_ISO" >/dev/null 2>&1 || true
        SETUP_EXE="$(find "$ROOT/downloads/vmt" -maxdepth 1 -iname 'setup64.exe' | head -1)"
        [ -n "$SETUP_EXE" ] || SETUP_EXE="$(find "$ROOT/downloads/vmt" -maxdepth 1 -iname 'setup.exe' | head -1)"
        if [ -n "$SETUP_EXE" ]; then
            7z e -y -o"$ROOT/downloads/vmt/cabs" "$SETUP_EXE" "VmVideo.cab" >/dev/null 2>&1 || true
            if [ -f "$ROOT/downloads/vmt/cabs/VmVideo.cab" ]; then
                7z e -y -o"$ROOT/downloads/vmt/video" "$ROOT/downloads/vmt/cabs/VmVideo.cab" >/dev/null 2>&1 || true
                for f in "$ROOT/downloads/vmt/video"/_vm3d*; do
                    [ -f "$f" ] || continue
                    n="$(basename "$f")"
                    n="${n#_}"                 # leading underscore
                    n="${n%%_Vista.*}"         # trailing _Vista.<GUID>
                    n="${n%%_Win8.*}"
                    n="${n/_debug/-debug}"     # the INF spells these with a dash
                    n="${n/_stats/-stats}"
                    cp "$f" "$DRV/$n"
                done
            fi
        fi
        if [ -f "$DRV/vm3d.inf" ]; then
            MISSING_FILES=0
            while read -r want; do
                [ -f "$DRV/$want" ] || MISSING_FILES=$((MISSING_FILES + 1))
            done < <(sed -n '/\[SourceDisksFiles\]/,/^\[/p' "$DRV/vm3d.inf" | sed -n 's/^\([A-Za-z0-9._-]*\) *= *1.*/\1/p')
            if [ "$MISSING_FILES" -eq 0 ]; then
                echo "  Extracted $(ls "$DRV" | wc -l | tr -d ' ') driver files."
                rm -rf "$ROOT/downloads/vmt"   # the unpacked installer is no longer needed
            else
                warn "$MISSING_FILES driver files named by the INF are missing; the guest will extract the driver itself instead."
                rm -f "$DRV/vm3d.inf"
            fi
        fi
        if [ ! -f "$DRV/vm3d.inf" ]; then
            warn "Could not unpack the driver on the Mac."
            warn "guest-tools.iso will carry the VMware Tools installer, and SETUP.CMD will extract the driver inside Windows (it does not install Tools)."
            mkdir -p "$ROOT/downloads/vmtools-installer"
            [ -n "${SETUP_EXE:-}" ] && cp "$SETUP_EXE" "$ROOT/downloads/vmtools-installer/" || true
        fi
    fi
fi

# ----------------------------------------------- 4b. clipboard components
say "Clipboard sharing (SPICE guest tools)"
CLIP="$CACHE/clipboard-0.141"
if [ -f "$CLIP/vioserial/w7/amd64/vioser.inf" ] && [ -f "$CLIP/vdagent/64/vdservice.exe" ]; then
    echo "  Already extracted (nothing to download)."
else
    if sha_ok "$SPICE_EXE" "$SPICE_SHA256"; then
        echo "  Using the cached copy (nothing to download)."
    elif adopt "$SPICE_FILE" "$SPICE_SHA256" "$SPICE_EXE"; then
        :
    else
        echo "  Downloading SPICE guest tools 0.141 from spice-space.org (about 10 MB), once."
        fetch "$SPICE_URL" "$SPICE_EXE" "$SPICE_SHA256" \
            || warn "Download failed - clipboard sharing will not be set up. Run this again to retry."
    fi
    if [ -f "$SPICE_EXE" ]; then
        rm -rf "$ROOT/downloads/sgt" && mkdir -p "$ROOT/downloads/sgt"
        7z x -y -o"$ROOT/downloads/sgt" "$SPICE_EXE" >/dev/null 2>&1 || true
        rm -rf "$CLIP" && mkdir -p "$CLIP"
        # w7 = Windows 7, 2k8 = Vista (Vista shares Server 2008's drivers)
        for os_dir in w7 2k8; do
            for arch in amd64 x86; do
                src="$ROOT/downloads/sgt/drivers/vioserial/$os_dir/$arch"
                [ -f "$src/vioser.inf" ] && mkdir -p "$CLIP/vioserial/$os_dir/$arch" && cp "$src"/* "$CLIP/vioserial/$os_dir/$arch/"
            done
        done
        for bits in 32 64; do
            [ -f "$ROOT/downloads/sgt/$bits/vdservice.exe" ] && mkdir -p "$CLIP/vdagent/$bits" \
                && cp "$ROOT/downloads/sgt/$bits/vdservice.exe" "$ROOT/downloads/sgt/$bits/vdagent.exe" "$CLIP/vdagent/$bits/"
        done
        # The driver's signing certificate, so Windows installs it without a
        # "Would you like to install this device software?" prompt.
        if [ -f "$CLIP/vioserial/w7/amd64/vioser.cat" ]; then
            openssl pkcs7 -inform DER -in "$CLIP/vioserial/w7/amd64/vioser.cat" -print_certs 2>/dev/null |
                awk '/^subject=.*Red Hat, Inc/ {grab=1} grab {print} /END CERTIFICATE/ && grab {exit}' |
                openssl x509 -outform DER -out "$CLIP/redhat.cer" 2>/dev/null || true
        fi
        rm -rf "$ROOT/downloads/sgt"
        if [ -f "$CLIP/vioserial/w7/amd64/vioser.inf" ] && [ -f "$CLIP/vdagent/64/vdservice.exe" ]; then
            echo "  Extracted the clipboard driver and agent."
        else
            warn "Could not unpack the clipboard components; clipboard sharing will not be set up."
            rm -rf "$CLIP"
        fi
    fi
fi

# ------------------------------------------------------- 5. guest-tools.iso
say "Building guest-tools.iso"
GPU_NAME="${GPU_NAME:-$(system_profiler SPDisplaysDataType 2>/dev/null | awk -F': ' '/Chipset Model/ {print $2; exit}')}"
[ -n "$GPU_NAME" ] || GPU_NAME="Apple GPU"
GPU_HEX="$(python3 -c "import sys; print(''.join('%02x00' % b for b in sys.argv[1].encode()) + '0000')" "$GPU_NAME")"
echo "  Windows will show this GPU name: $GPU_NAME"

STAGE="$ROOT/downloads/iso-stage"
rm -rf "$STAGE" && mkdir -p "$STAGE"
cp -R "$ROOT/guest/." "$STAGE/"
sed -e "s/@@GPU_NAME@@/$GPU_NAME/" -e "s/@@GPU_HEX@@/$GPU_HEX/" \
    "$STAGE/gpuname/APPLY.CMD.in" > "$STAGE/gpuname/APPLY.CMD"
rm -f "$STAGE/gpuname/APPLY.CMD.in"
[ -f "$DRV/vm3d.inf" ] && { mkdir -p "$STAGE/graphics"; cp "$DRV"/* "$STAGE/graphics/"; }
[ -d "$ROOT/downloads/vmtools-installer" ] && { mkdir -p "$STAGE/vmtools"; cp "$ROOT/downloads/vmtools-installer"/* "$STAGE/vmtools/"; }
[ -d "$CLIP" ] && cp -R "$CLIP" "$STAGE/clipboard"

rm -f "$ROOT/guest-tools.iso"
hdiutil makehybrid -iso -joliet -default-volume-name AEROTOOLS \
    -o "$ROOT/guest-tools.iso" "$STAGE" >/dev/null
echo "  $(du -h "$ROOT/guest-tools.iso" | cut -f1) guest-tools.iso"
rm -rf "$STAGE"

say "Setup finished"
[ "$AUTO" = 1 ] && exit 0   # START HERE takes it from here
cat <<EOF

Next steps:

  1. Install Windows from your own ISO:
       MODE=install ISO=/path/to/windows.iso ./Start-Windows7.command
     (or ./Start-Vista.command)

  2. Once Windows is installed, boot with the tools disc:
       MODE=setup ./Start-Windows7.command
     In Windows, open the CD drive and run SETUP.CMD, then reboot.

  3. After that just double-click Start-Windows7.command.

EOF
[ "$AUTO" = 1 ] || read -r -p "Press Return to close." _ || true
