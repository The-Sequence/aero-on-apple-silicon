# Third-party components and licenses

This repository contains **patches and scripts only**. It ships no Microsoft or
VMware files. The release runtime contains builds of the projects below.

| Component | License | Where it comes from |
|---|---|---|
| QEMU 9.2.2 | GPL-2.0-only (parts LGPL/BSD; see the QEMU tree) | `patches/qemu-9.2.2-aero.patch` applies to the official tarball |
| qemu-vmvga (VMware SVGA/SVGA3D device) | GPL-2.0-or-later | included in the same patch; upstream: https://github.com/qemus/qemu-vmvga |
| DXVK | zlib/libpng | `patches/dxvk-macos.patch`, pinned commit in TECHNICAL.md |
| dxbc-spirv | zlib/libpng | `patches/dxbc-spirv-dref.patch` |
| libdisplay-info | MIT | `patches/libdisplay-info-macos.patch` |
| MoltenVK, Vulkan loader | Apache-2.0 | installed by Homebrew (`molten-vk`, `vulkan-loader`) |
| SDL2 / sdl2-compat | zlib | installed by Homebrew |
| glib, pixman, gnutls, libpng, jpeg-turbo, zstd, libslirp, libusb | their own licenses | installed by Homebrew |
| WSI shim (`src/wsishim/`) | GPL-2.0-or-later | written for this project |
| Guest resize agent (`src/guest-agent/`) | GPL-2.0-or-later | written for this project |
| Guest scripts (`guest/`) | GPL-2.0-or-later | written for this project |

| SPICE guest tools 0.141 (spice-vdagent, virtio-serial driver) | GPL-2.0+ / BSD-3-Clause | downloaded from spice-space.org by `Setup.command`, hash-pinned |

## Not included, on purpose

- **The VMware SVGA 3D display driver** (`vm3dmp.sys`, `vm3dum*.dll`, `vm3d.inf`).
  It is VMware's proprietary software. `Setup.command` downloads the official
  VMware Tools 10.3.10 package from VMware's own server and extracts only the
  display driver, on your machine. VMware Tools itself is never installed in
  the guest.
- **Windows.** You supply your own licensed installation media.

The reverse engineering documented in TECHNICAL.md section 2.6 was done for
interoperability: to make a resolution change work without VMware Tools. It
describes an interface; it copies no code.
