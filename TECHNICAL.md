# TECHNICAL.md — how this works, what was wrong, and what is still open

**Authorship:** the code and the analysis described here were written by **Claude** (Anthropic's AI assistant), directed and tested by Kai ([@The-Sequence](https://github.com/The-Sequence)) on real VMs. Project: <https://github.com/The-Sequence/aero-on-apple-silicon>. A ChatGPT (Codex) session contributed the coverage-gated present path (`VMVGA_COVERAGE_GATED_PRESENT`) and the `DXVK_MVK_WAIT_IDLE_ON_RESOURCE_DESTROY` and `DXVK_FORCE_FULL_RENDER_AREA` flags. See the README for the full split of who did what, and for credits to QEMU, qemu-vmvga, DXVK, MoltenVK, Apple, VMware and SDL, whose work this is built on.

This is the engineering record. It is written for people who want to fix the remaining bugs or reuse the findings. Everything below was measured on the machine in README.md unless it says otherwise. Where a claim came from a single run, it says so.

Line numbers refer to the patched QEMU 9.2.2 tree produced by `patches/qemu-9.2.2-aero.patch`.

---

## 1. Architecture

```
Windows app  ->  D3D9  ->  VMware UMD (vm3dum64.dll)
                            |
                            v  SVGA3D commands in a FIFO / command buffers
                        vm3dmp.sys (WDDM 1.0 miniport)
                            |
============================ guest / host boundary =====================
                            |
                    QEMU "vmvga" device
                      hw/display/vmware_vga*.c
                            |
                            v  translated to D3D9 calls
                    DXVK (dxvk-native, built as .dylib)
                            |
                            v  Vulkan
                        MoltenVK  ->  Metal  ->  your Mac GPU
```

Key points:

- **The guest driver is VMware's own**, version 8.16.1.24, a **WDDM 1.0** driver. WDDM 1.0 has no GDI hardware acceleration, so all 2D window drawing is done by the guest CPU. That matters for the performance work below.
- **The device is `qemu-vmvga`**, a much more complete VMware SVGA II implementation than QEMU's stock `vmware-svga`. Its 3D path drives DXVK directly.
- **Only the legacy SVGA3D ("vGPU9", D3D9-class) path is used.** That is what the Windows 7 VMware driver speaks. vGPU10 exists in the tree but is not the working path here.
- **Presentation is a readback.** `BLIT_SURFACE_TO_SCREEN` copies the composed frame back to the CPU into QEMU's console surface, which QEMU's SDL renderer then draws. There is no zero-copy path (see "Dead ends").
- **Threads:** guest vCPUs (MTTCG, one host thread each), QEMU's main loop (which runs FIFO and command-buffer bottom halves, display refresh, and all of the DXVK calls), and DXVK's own CS thread.
- **Build layout note:** only `vmware_vga.c` is a real translation unit. It `#include`s `vmware_vga_gmr.c` and `vmware_vga_3d.c`, which in turn include the DXVK, WSI, vGPU9/10/11 and state fragments. Registering them separately with meson does not work.

### 1.1 macOS port of the device

Upstream's DXVK delivery is Linux-only: it synthesizes an ELF object in a sealed `memfd` whose absolute symbols point at its SDL replacement functions. On macOS we instead:

- ship a small Mach-O dylib, `libvmsvga3d_wsi.dylib` (`src/wsishim/`), which exports the same 16 `SDL_*` names as thin forwarders;
- have QEMU hand it a function table at load time (`vmsvga3d_wsi_register`);
- patch DXVK to `dlopen` that name instead of SDL2, so it never collides with the real SDL2 that QEMU already uses for `-display sdl`.

Library lookup is done at runtime by `vmsvga3d_macos_lib()` in `vmware_vga_dxvk_wsi.c`: `$VMVGA_LIBDIR`, then `<exe dir>/../lib`, then `<exe dir>`. Absolute paths compiled into the binary would make the release non-relocatable.

Also note: QEMU's macOS build is code-signed, so SIP strips `DYLD_LIBRARY_PATH`. `dlopen` of a bare leaf name will not find Homebrew libraries; the Vulkan loader is therefore tried by leaf name first and then by absolute Homebrew paths.

---

## 2. The fixes

Each is: symptom, root cause, fix, and how it was verified.

### 2.1 Aero froze after 1–3 frames — GMR 0

**Symptom.** Aero composited one to three frames, then the desktop stopped updating. The guest stayed fully responsive: input, audio and applications all continued. That observation is what disproved every "the guest is hung" theory.

**Root cause.** Windows 7's VMware driver sends `DEFINE_GMR2(id=0, pages=0)`, then a sparse `REMAP_GMR2(id=0, offsetPages=16381, pages=3)`, and puts its **query-result ring** at `0:0x03ffd008` — inside those last three pages of a 16384-page aperture. QEMU treated a zero-page define as destruction and rejected the later remap, so every query-completion write failed. DWM exhausted its small outstanding-query pool and stopped submitting composition work.

**Fix** (`hw/display/vmware_vga_gmr.c`): recreate **only GMR 0** on that otherwise-unmapped sparse remap, sized to the remap end (16381 + 3 = 16384 pages).

**Verified:** 3,767 successful query publications and 2,996 successful screen blits with zero failures in the validation run.

**Caveat, deliberately narrow.** VMware's published headers describe `REMAP_GMR2` as modifying an *existing* GMR, and VirtualBox frees a GMR on a zero-page define. This is a compatibility quirk for this driver, not a general relaxation.

### 2.2 The whole desktop rendered in greyscale — a dead shader branch

The hardest bug in the project, and the most reusable finding.

**Symptom.** Aero worked but everything was greyscale. Measured output was exactly `(src.r, src.r, src.r, ·)`.

**Root cause.** `dxbc-spirv` emits a **runtime-branched shadow-sampling path for every sampled texture**, guarded by a `LegacySamplerState` flag. The SPIR-V is correct, and for a colour texture the branch is never taken:

```
%30  = OpTypeImage %float 2D 0 0 0 1 Unknown   ; Depth = 0, an ordinary image
%98  = OpImageSampleDrefImplicitLod %float ... ; shadow path, never executed
%127 = OpImageSampleImplicitLod %v4float ...   ; the live path
```

But **Metal's texture type is static.** SPIRV-Cross promotes any image used by an `OpImageSampleDref*` instruction to `depth2d<float>`, so MoltenVK generated:

```metal
float4 sampleTexture_0(float4 texCoord, depth2d<float> s0_2d, ...)
return float4(s0_2d.sample(sampler_heap[_102], _112));
```

`depth2d::sample()` returns a **scalar**, and `float4(scalar)` broadcasts it. A dead branch poisoned the live one through Metal's static typing.

**Fix** (`patches/dxbc-spirv-dref.patch`, `sm3/sm3_resources.cpp`, `ResourceMap::emitSampleColorOrDref()`): skip the Dref branch. D3D9 shadow sampling needs a depth-format texture, which DXVK's `IsDepthFormat()` never reports for colour formats, so the branch was unreachable for this workload. `DXBC_SPV_EMIT_DREF=1` restores the original behaviour.

**Known limitation.** D3D9 hardware shadow mapping (INTZ/DF16/DF24 sampled with comparison) does not work in this build. Aero does not use it. **The correct upstream fix is to give the Dref path its own image descriptor** so the colour image stays a plain `texture2d`. This will affect any D3D9 title on MoltenVK, and is worth reporting upstream.

**Method that found it, after many wasted VM boots:** take the VM out of the loop. `dxbc-spirv` ships `dxbc_compiler`, which compiles captured D3D9 bytecode straight to SPIR-V on the host, and `MVK_CONFIG_SHADER_DUMP_DIR` dumps MoltenVK's generated MSL. That turned a multi-boot guessing game into a seconds-long loop.

### 2.3 `VK_ERROR_DEVICE_LOST` and WinSAT crashes — three separate MoltenVK lifetime bugs

**Symptom.** `kIOGPUCommandBufferCallbackErrorInvalidResource` → `VK_ERROR_DEVICE_LOST`, usually in WinSAT's media-decoding stage, and on Vista after about 100 seconds.

**Root causes and fixes** (all in `patches/dxvk-macos.patch`):

1. **Memory freed while Metal still used it.** The Metal debug layer pinned it to a 32 MiB DXVK chunk freed by `freeEmptyChunksInPool` while a submitted `MTLCommandBuffer` still referenced it. `DXVK_CHUNK_FREE_GRACE` postpones the real `vkFreeMemory` (`dxvk_memory.cpp`); `DXVK_HOLD_EMPTY_CHUNKS` never frees empty chunks at all.
2. **MoltenVK's timeline semaphore signals too early.** It is an `MTLSharedEvent` signalled by the GPU *inside* the command buffer, before completion, and with residency sets (macOS 15+) MoltenVK skips `retainedReferences`. DXVK therefore believed a submission was done and released resources early. `DXVK_MVK_COMPLETION_FENCE` attaches a `VkFence` (which MoltenVK signals from Metal's completed handler) to each command list's final submit and waits on it before releasing anything (`dxvk_cmdlist.{h,cpp}`, `dxvk_queue.cpp`).
3. **The one that actually made WinSAT pass: dynamic vertex strides.** An unbound D3D9 vertex stream was backed by a 0-byte, 0-stride dummy under a dynamic-stride pipeline, and MoltenVK left Metal buffer slot 29 with a static binding, so the GPU fetched through a garbage stride. `DXVK_MVK_STATIC_VERTEX_STRIDES` never uses dynamic strides and gives missing streams the full 64 KiB zero dummy (`dxvk_context.cpp`, `dxvk_graphics.cpp`, `dxvk_graphics_state.h`). The debug layer had named it exactly: *"providing MTLAttributeStrideStatic to attributeStride when stride at buffer-layout index 29 is dynamic"*.

Each flag logs one `info:` line on first use, so a host log proves which were active.

**Debug recipe that found all three:**
```
MVK_CONFIG_LOG_LEVEL=2 METAL_DEVICE_WRAPPER_TYPE=1 MTL_DEBUG_LAYER=1 MTL_DEBUG_LAYER_ERROR_MODE=nslog
```

**Result:** the full WinSAT assessment completes — base 5.4 (Processor 5.4, Memory 7.9, Graphics 6.0, Gaming 6.0, Disk 7.6). `winsat dwm` went from 0.00 to over 5000 MB/s.

### 2.4 WinSAT killed the display driver — the backdoor called from ring 3

**Symptom.** `Faulting module vm3dum64.dll, exception 0xc0000096 (STATUS_PRIVILEGED_INSTRUCTION), offset 0x2800f`.

**Root cause.** Disassembly at that offset is VMware's backdoor stub:

```
18002800c: movq (%rax), %rax      ; eax = magic 'VMXh'
18002800f: inl  %dx, %eax         ; <-- faults
```

VMware's **user-mode** display driver calls the hypervisor backdoor from ring 3. Real VMware permits this. QEMU's `helper_check_io()` consults the guest TSS I/O permission bitmap, which Windows sets to deny the port to user mode, so it raised #GP.

**Fix** (`target/i386/tcg/sysemu/seg_helper.c`, `target/i386/cpu.h`, `hw/i386/vmport.c`): permit port **0x5658 only**, and **only when the vmport device is present**. This changes the emulated guest CPU's privilege model only. It grants the guest no additional access to the host.

### 2.5 Garbled Aero window borders

**Symptom.** Whenever a D3D application (Photo Viewer, Media Center, a browser) ran alongside DWM, **every** window's frame broke: dark edges, a light box behind the title, blocky caption buttons. Intermittent, which made bisection unreliable.

**Root cause.** The incremental D3D9 state tracking misses some state once another context has touched the native device. Inputs were all intact: no dropped draws, no viewport or scissor failures.

**Fix** (`vmware_vga_vgpu9.c`): `VMVGA_ARTIFACT_FULL_REPLAY=1` replays the native D3D9 state before every draw. `VMVGA_REPLAY_NORESET=1` narrows this to replay mask 30 (targets + fixed function + textures + shaders, **no** device reset), which was correct in 3 of 3 runs and avoids the expensive reset.

`VMVGA_REPLAY_MASK` takes an explicit bitmask: 1 device reset, 2 render targets, 4 fixed function, 8 textures and samplers, 16 shaders.

**Still not root-caused.** The narrow fix is to find *which* state the incremental path misses. Single-category masks were unreliable because the bug is intermittent; do not trust a single run.

**Earlier, separate border bug, now fixed:** `BLIT_SURFACE_TO_SCREEN` copied only DWM's clip rects to the screen, leaving pixels outside them stale from earlier blits. `VMVGA_BLIT_FULL=1` presents the whole source rect. Surface dumps (`VMVGA_DUMP_FILE`) proved the GPU composition itself was correct at both copy and blit time.

### 2.6 Dynamic resolution without VMware Tools

Two halves.

**Host.** `vmsvga_ui_info()` in `vmware_vga.c` hooks QEMU's UI resize notification, and publishes the wanted size as `guestinfo.qemu.resolution = "W H seq"` through the VMware backdoor (`hw/i386/vmport.c`), plus a TCLO `Resolution_Set`. `VMVGA_DYNAMIC_RES=1` enables it; `VMVGA_DYNAMIC_RES_MAX=WxH` optionally caps it (there is no cap by default).

**Guest.** `src/guest-agent/qemu-res-agent.c`, a small agent with no C runtime dependency (it uses only kernel32 and user32, so it runs on Vista and 7 without the Universal CRT). It polls that key over the RPCI backdoor and switches the display mode.

The interesting part is **how it gets an exact, non-standard resolution**. The VMware driver only lists standard modes, so a 1800×1130 window could not be matched, and QEMU had to scale the picture — which is what made text blurry. VMware Tools solves this with a private driver call, so that call was reverse-engineered from `vm3dmp.sys` 8.16.1.24:

- Escape dispatcher at `0x1400059e0`. `DXGKARG_ESCAPE` private data is `{uint32 command, uint32 payloadSize}`, with `payloadSize + 8 <= PrivateDriverDataSize`.
- **Command 6 sets the display topology.** Handler at `0x14001b8ac`. Payload: `{uint32 count, entries[count]}`, `payloadSize == 4 + 20*count`, each entry `{uint32 sourceId (<8), uint32 width, uint32 height, uint32 refresh_mHz, uint32 bpp}`.
- Validator at `0x1400158e0`: refresh must be in 24000–120000 mHz, bpp in {8, 16, 32}, and width × height × bytes-per-pixel must fit the reported VRAM.
- The driver then either updates the active monitor or hot-plugs it, so Windows re-reads the mode list and the exact mode becomes available. `ChangeDisplaySettingsEx` then succeeds.
- The driver also **persists topology** in its PnP driver key (`...\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\000N`) as `ActiveMonitors` and `VidPNSource<N>{Flags,Width,Height,X,Y}`, read back through `IoOpenDeviceRegistryKey` + `ZwQueryValueKey`.

The agent sends that escape via `D3DKMTOpenAdapterFromHdc` + `D3DKMTEscape` (type `DRIVERPRIVATE`), then retries `ChangeDisplaySettingsEx` for up to 3 seconds while Windows re-enumerates. It refuses to send the escape unless the adapter string is "VMware SVGA 3D", because the packet layout is specific to this driver. It logs everything to `C:\qemu-res-agent.log`.

### 2.7 Sharp output on a Retina display

Two host-side problems made text blurry:

1. QEMU's SDL window was not high-DPI aware, so macOS scaled a 1× backing store up by 2×.
2. SDL's default texture filter is nearest, so when the guest resolution didn't exactly match the window, whole pixel columns were dropped out of glyphs.

**Fix** (`ui/sdl2.c`, `ui/sdl2-2d.c`): request `SDL_WINDOW_ALLOW_HIGHDPI` (disable with `VMVGA_HIDPI=0`), and choose the texture scale mode per frame — nearest at an integer scale (a guest pixel becomes a sharp 2×2 block on Retina), linear otherwise. `VMVGA_SCALE_FILTER=nearest|linear` forces one.

Note this project runs on sdl2-compat over SDL3. Its logical-size path converts mouse coordinates for you, including pixel density, which is why mouse mapping stays correct.

### 2.8 Audio

Two separate causes, both fixed:

- **Crackle:** CoreAudio's default buffering (~46 ms) is far too small for a TCG guest that stalls. The launcher passes a much larger `out.buffer-length` and 8 buffers.
- **Audio and video playing too fast:** QEMU's `audio/coreaudio.m` `audioDeviceIOProc` copied the *requested* frame size instead of `outOutputData->mBuffers[0].mDataByteSize`, draining guest audio faster than it played. Patched to size each callback from `mDataByteSize` and pad underruns with silence. The HDA timer FIFO was also enlarged from 8 to 32 KiB (`hw/audio/hda-codec.c`), because under TCG a late callback dropped a full FIFO while the guest's DMA position had already advanced, making players skip ahead.
- Device choice: `hda-output`, not `hda-duplex` (the duplex codec wants a capture voice CoreAudio cannot create here). Vista's in-box HD Audio driver lacks MSI and was unreliable, so Vista defaults to `usb-audio`, which Vista supports in-box.

### 2.9 Buffer uploads in place

DWM's dynamic vertex buffer was evicted and recreated **every frame**, because buffer DMA was CPU-only and `sync_surface_from_cpu` evicted buffers. Uploading in place with DISCARD semantics (`vmsvga3d_dxvk_surface_upload_buffer`) brought that to zero evictions. `VMVGA_BUFFER_EVICT=1` restores the old behaviour.

### 2.10 The GPU name

`guest/gpuname/` installs a startup task that rewrites the adapter strings (`DriverDesc`, `HardwareInformation.AdapterString`, `HardwareInformation.ChipType`, `FriendlyName`, `DeviceDesc`, `Mfg`) under the display class key, the `Control\Video` keys and the PCI enum key, so Device Manager, dxdiag, `EnumDisplayDevices` and WinSAT report the host GPU name. It must run at every startup because the VMware miniport rewrites those values when it starts.

**This is cosmetic only.** It changes the displayed name, not what renders. The rendering genuinely runs on the Mac GPU through DXVK and Metal. `UNDOGPU.CMD` removes it.

---

## 3. Measuring

The device emits profile lines once a second, independent of the tracing controls, from the display refresh. Enable nothing; they are always on.

| Line | Contents |
|---|---|
| `VMVGA-PROFILE` | pipeline setups, shader replays, linkage, D3D9 and DX draws |
| `VMVGA-PROFILE-IO` | present blits, context switches, screen poll/submit/quiesce, command buffers |
| `VMVGA-PROFILE-PATH` | per-path timings (D3D9 vs D3D11 polls, syncs, handoffs, retire) |
| `VMVGA-PROFILE-2D` | FIFO runs and time, surface DMA count/pixels/time, screen blits, 2D GMRFB blits, display refresh count/time/worst gap, draw count and time, forced replays |
| `VMVGA-PROFILE-CMD` | the six most expensive SVGA3D command ids in the last second, as `cmd<id>=<µs>/<count>` |

**Before trusting any counter, find its increment site.** Dead counters (`draw9`, `present`, the vGPU10 retire counters at various times) cost this project days.

Other tools:

- `VMVGA_TRACE_FILE=<path>` — tracing only while that file exists. Full `VMVGA_DEBUG=on` writes ~280 KB/s and makes the guest look hung.
- `VMVGA_DUMP_FILE=<trigger>` — dumps surface copy source/destination and blit source as PPM/PGM.
- `EXTRA_QEMU_ARGS="-snapshot -qmp unix:/tmp/q.sock,server=on,wait=off"` — throwaway test boots driven over QMP.

### 3.1 What the numbers say today (3DMark06, Windows 7)

- ~10,000 draw calls per second.
- QEMU spends **400–590 ms of every second** processing guest commands, on the main loop thread.
- Of that second: draws ≈ **15 ms** (about 1.5 µs each, including the forced state replay), surface DMA ≈ 0.4 ms, screen blits ≈ 7.5 ms.
- **So roughly 95% of the command-processing time is in some other command type, not yet identified.** `VMVGA-PROFILE-CMD` was added to name it; the measurement has not been taken yet.
- The GPU is mostly idle. This is a host CPU bottleneck in command translation, plus the emulated guest CPU.

Ordinary desktop use is nowhere near that: host-side 2D work is under 10% of one core, and the display refresh runs every 30–40 ms as expected.

---

## 4. Open problems

If you want to help, these are the live ones.

1. **The unidentified per-command cost above.** Run 3DMark06 and read `VMVGA-PROFILE-CMD`. The suspects are the high-frequency state commands: `SETRENDERSTATE` (1003), `SETTEXTURESTATE` (1004), `SET_SHADER_CONST` (1017). If it is one of those, the fix is to batch or to skip redundant state instead of calling into DXVK per command.
2. **3DMark06 `D3DERR_DEVICELOST`.** Still reproducible on some runs even with the TDR timeout at 60 s.
3. **Aero border replay.** `VMVGA_REPLAY_MASK` 30 every draw is a blunt instrument; find the missing state. Beware: the underlying bug is intermittent, so a single clean run proves nothing.
4. **Welcome-screen slowness.** Traced to Explorer decoding and scaling the JPEG wallpaper (bottom-up DIB rows) on the emulated CPU while DWM composes. A solid-colour desktop background avoids it. A real fix needs faster guest CPU or a cheaper path.
5. **Resolution changes stall the display for 2–3 seconds** (visible as `refresh-max-us` around 2–3 million in `VMVGA-PROFILE-2D`). Likely device or swapchain recreation on the main thread.
6. **GDI hardware acceleration.** The driver reads host configuration through `info-get guestinfo.svga.wddm.*` and has a key called `svga.wddm.enableFakeGDIHW`. In `vm3dmp.sys` it sets bit 7 of the adapter capability word at `adapter+0xdd4`, and it is only consulted when bits 0, 8 and 10 of that word are already set and the driver-started flag at `0x1400350f8` is non-zero; its default comes from a global that looks like an "OS is Windows 8 or newer" check. If this really enables WDDM 1.1 GDI acceleration, 2D window drawing could move off the emulated CPU. **Unproven and untested.**
7. **A render thread** for SVGA3D command processing, so the guest and host overlap instead of serializing on QEMU's main loop.
8. **vGPU10** (DirectX 10/11) support.
9. **Video surfaces.** `VMSVGA3DD3D9ResourceCaps` has `supports_uyvy`, `supports_yuy2` and `supports_a8b8g8r8`, but only `supports_intz` was ever assigned, so YUV surfaces were rejected and video rendered green. `VMVGA_D3D9_EXTRA_CAPS=1` reports them when DXVK supports them; the `CheckDeviceFormat` plumbing already exists in `vmware_vga_dxvk.c`. Worth a fresh look, along with caching the results at device init.

---

## 5. Dead ends — please don't re-litigate these

Each of these was tested and disproven, usually expensively.

- **Zero-copy present.** Presenting DXVK's swapchain straight into a borderless child NSWindow over QEMU's SDL window. It works, but the geometry never reliably matched QEMU's own view (windowed, resized and full screen), and mouse mapping follows QEMU's viewport. Two macOS traps: SDL's Metal view resets `drawableSize` to the view bounds on every layout, and `CAMetalLayer.contentsGravity` is ignored or overridden. Shelved.
- **"Smart replay"** (only replaying state after a non-draw command). Measured saving: nothing, because the replay costs ~1.5 µs per draw. It also appeared in a run that ended in `DEVICE_LOST`. Available as `VMVGA_REPLAY_SMART=1`, off by default.
- **`MVK_CONFIG_USE_METAL_ARGUMENT_BUFFERS=0`** actively breaks everything: DXVK's descriptor model requires Metal 3 argument buffers, and without them every pipeline fails to compile (permanent black screen).
- **The robustness2 feature flags were never disabled.** The third argument to `ENABLE_FEATURE` / `ENABLE_EXT_FEATURE` is `require`, not `enable`, and MoltenVK advertises the extension anyway.
- **DXVK's D3D9 format table and swizzle packing are correct.** A8R8G8B8's `Swizzle` defaults to explicit IDENTITY; the only `{R,R,R,ONE}` entries are L8 and L16.
- **Three wrong greyscale theories**: the `def`/alpha-constant theory, the masked-write (`mov r0.w, c0.x`) theory, and the PS8-versus-PS9 difference theory.
- **Installing VMware Tools.** It makes the login screen crawl. Only the display driver is wanted.
- **Apple TSO** for x86 memory ordering: it needs a private entitlement, and QEMU closed the request as wontfix.
- **Rosetta for Windows guests:** Rosetta's VM support is Linux userspace only.
- **HVF / hardware virtualization:** Apple Silicon cannot virtualize x86. TCG is the only option, and `-accel tcg` plus `-display sdl` and `-M pc` are required.
- **Video decode through SVGA3D commands 1076–1078:** those ids are `DEAD8`/`DEAD9`/`DEAD10`, removed commands.
- **HPET off** made no measurable boot-time difference (44.0 s either way).

---

## 6. Every switch

Host, set in the launcher environment.

| Variable | Default | Effect |
|---|---|---|
| `VMVGA_DYNAMIC_RES` | 1 | publish host window size to the guest agent |
| `VMVGA_DYNAMIC_RES_MAX` | none | cap the requested resolution, keeping aspect |
| `VMVGA_HIDPI` | 1 | high-DPI (Retina) SDL window |
| `VMVGA_SCALE_FILTER` | auto | `nearest` or `linear` to force the scaler |
| `VMVGA_ARTIFACT_FULL_REPLAY` | 1 | replay D3D9 state before every draw (border fix) |
| `VMVGA_REPLAY_NORESET` | 1 | as above, without the device reset (mask 30) |
| `VMVGA_REPLAY_MASK` | — | explicit replay mask (1/2/4/8/16) |
| `VMVGA_REPLAY_SMART` | 0 | only force a replay after a non-draw command |
| `VMVGA_X8_AS_A8` | 1 | back X8R8G8B8 surfaces with A8R8G8B8 |
| `VMVGA_DMA_PARTIAL_UPLOAD` | 1 | upload only written boxes into resident surfaces |
| `VMVGA_BLIT_FULL` | 0 | present the whole source rect, ignoring clip rects |
| `VMVGA_COVERAGE_GATED_PRESENT` | 0 | hold the last complete frame until damage covers the screen |
| `VMVGA_BUFFER_EVICT` | 0 | old evict-and-recreate buffer upload path |
| `VMVGA_UPLOAD_FRESH_STAGING` | 0 | a private staging surface per upload |
| `VMVGA_PRESERVE_ON_RECREATE`, `VMVGA_CLEAR_PRESERVE` | 1 | keep surface contents across recreation and clears |
| `VMVGA_D3D9_EXTRA_CAPS` | 1 | report UYVY/YUY2/A8B8G8R8 when DXVK supports them |
| `VMVGA_ZERO_COPY` | 0 | shelved zero-copy present |
| `VMVGA_LIBDIR` | — | where the DXVK dylibs and the WSI shim live |
| `DXVK_CHUNK_FREE_GRACE`, `DXVK_HOLD_EMPTY_CHUNKS` | 1 | memory lifetime fixes |
| `DXVK_MVK_COMPLETION_FENCE` | 1 | wait for Metal completion before releasing resources |
| `DXVK_MVK_STATIC_VERTEX_STRIDES` | 1 | never use dynamic vertex strides |
| `DXVK_FORCE_DEPTH_LOAD`, `DXVK_FORCE_COLOR_LOAD`, `DXVK_NO_COLOR_STORE_DISCARD` | 1 | never discard attachment contents DWM relies on |
| `DXVK_FORCE_FULL_RENDER_AREA` | 0 | one full tile render area per pass |
| `DXVK_MVK_WAIT_IDLE_ON_RESOURCE_DESTROY` | 0 | flush and wait before retiring resources (slow) |
| `DXBC_SPV_EMIT_DREF` | 0 | restore the original Dref shader path (greyscale) |

Guest: `C:\qemu-res-agent.log` (resize agent), `C:\aero-setup.log` (setup), TDR timeouts at `HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers\Tdr{Delay,DdiDelay}` = 60.

---

## 7. Reproducing the build

`build/build-from-source.sh` does all of it. The pinned upstream commits are:

- QEMU **9.2.2** release tarball, plus `patches/qemu-9.2.2-aero.patch` (this includes the qemu-vmvga device sources and every change described here).
- DXVK at **`40e01640396d03f9fddeb697f5ee00ae893ecb75`**, plus `patches/dxvk-macos.patch`.
- dxbc-spirv at **`bf14419e5fa7eacb817b7b632f03cb61d61bbad7`**, plus `patches/dxbc-spirv-dref.patch`.
- DXVK is built with `meson --buildtype release -Dnative_sdl2=enabled -Dnative_glfw=disabled`.

The guest agent builds with mingw-w64 and no C runtime:

```
x86_64-w64-mingw32-gcc -O2 -s -Wall -mwindows -nostdlib -ffreestanding \
  -fno-tree-loop-distribute-patterns -e AgentStartup \
  -o qemu-res-agent.exe qemu-res-agent.c -lkernel32 -luser32 -lgcc
```

---

## 8. The control screen (build/aero_tui.py)

- **VMs run detached** (`subprocess.Popen(..., start_new_session=True)` on `build/run-vm.sh`), so there is no second Terminal window and a VM survives the control screen being closed. `-display sdl,window-close=off` disables the VM window's close button; `caffeinate -i -m -w <qemu pid>` keeps the Mac awake while it runs.
- **Control socket.** Every VM gets a QMP socket at `/tmp/aero-<md5(disk path)[:12]>.sock` (short, because macOS limits Unix socket paths to 104 bytes). The screen uses it for `send-key` (Ctrl+Alt+Del and others), `blockdev-change-medium` / `eject` on the always-present CD device `id=cdrom`, `screendump`, `stop` / `cont`, `system_powerdown`, `system_reset` and `quit`, and reads the `RESET`, `SHUTDOWN` (with `guest: true`), `STOP` and `RESUME` events to track install progress (restarts) and clean shutdowns.
- **Reports from inside Windows.** The guest helper (`src/guest-agent/`, v5) sends `info-set guestinfo.aero.<key> <value>` over the VMware backdoor RPC every 20 s. `hw/i386/vmport.c` logs a line `AERO-GUEST aero.<key>=<value>` to the host log whenever a value changes. Keys: `agent` (version), `res` (current resolution), `aero` (DwmIsCompositionEnabled: on/off), `audio` (waveOutGetNumDevs + first device name, or `none`), `net` (DNS lookup of www.msftncsi.com + TCP connect to port 80: `ok`, `no-dns`, `timeout`, `no-connect`), `gpu` (adapter string). All APIs are loaded on demand, so the helper still needs only kernel32 and user32 and runs on Vista and 7 without any C runtime.
- **Resizing is confirmed** when a `VMVGA-DYNAMIC-RES request WxH` in the log is followed by the guest reporting `res=WxH`. **3D is confirmed** by non-zero `draw9=` in the device's profile lines.
- **Stages** in `vms/<name>.conf`: `new` -> `installed` -> `check` -> `ready`. The plain wizard treats `check` as `ready`.
- **Guest hardware defaults** (per VM, changeable in the settings): network `e1000` (Intel PRO/1000, in-box on Vista and 7 in both bitnesses; `rtl8139` remains an option), sound `hda` on 7 and `usb` on Vista, clipboard on (`virtio-serial-pci` + `qemu-vdagent`).
- **Snapshots** are qcow2 internal snapshots (`qemu-img snapshot -c/-a/-d/-l`), only while the VM is off - live VM state is not saved, because the 3D device state cannot be migrated.

---

## 9. Credits

This work stands on:

- **QEMU** and its contributors, including **Andrzej Zaborowski**, who wrote the original VMware SVGA II device.
- **qemu-vmvga** (the **qemus** project, with earlier work by **Christopher Eric Lentocha**) — the SVGA3D device and its DXVK 3D path. Sections 2 and 3 are fixes and measurements on top of that device, not a replacement for it.
- **DXVK** by **Philip Rebohle (doitsujin)** and contributors, plus **dxbc-spirv**.
- **MoltenVK** by **Bill Hollings / The Brenwill Workshop** and the **Khronos Group**, plus **SPIRV-Cross** — and its debug output, which is what identified two of the three device-lost bugs.
- **Apple** — Metal and the Apple Silicon GPU.
- **VMware** (now Broadcom) — the SVGA3D interface and the WDDM driver analysed in section 2.6. That driver is downloaded from VMware and is not redistributed here.
- **SDL** / sdl2-compat.
- **OpenAI's ChatGPT / Codex**, for the contributions noted at the top.

The `vm3dmp.sys` analysis in section 2.6 is clean-room reverse engineering of an interface for interoperability, done by disassembling the driver the user already has installed. No VMware code is included in this repository.
