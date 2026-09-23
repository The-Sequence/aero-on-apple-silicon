/*
 * libvmsvga3d_wsi - macOS delivery shim for qemu-vmvga's DXVK WSI backend.
 *
 * WHY THIS EXISTS
 * ---------------
 * DXVK Native insists on a window-system integration even when the caller only
 * wants an offscreen renderer. Its SDL2 backend dlopen()s a library by name and
 * dlsym()s a fixed set of 16 symbols. qemu-vmvga already implements all 16 as
 * ordinary C functions (vmsvga3d_dxvk_sdl_*); the hard part is only *delivery* -
 * getting DXVK's dlsym to find them.
 *
 * Upstream solves this on Linux by synthesizing a read-only ELF object in a
 * sealed memfd whose absolute symbols point back at those functions. That is
 * ELF-specific and does not port to Mach-O.
 *
 * On macOS we take a simpler route that is possible because we also build DXVK:
 * DXVK is patched to look for "libvmsvga3d_wsi.dylib" instead of SDL2, and this
 * ordinary dylib exports the 16 names. QEMU dlopens it first and hands over its
 * function pointers via vmsvga3d_wsi_register(); the exported SDL_* symbols are
 * thin forwarders through that table.
 *
 * Net effect is identical, with no runtime code generation, and no collision
 * with the real SDL2 that QEMU itself uses for -display sdl.
 */
#ifndef VMSVGA3D_WSI_SHIM_H
#define VMSVGA3D_WSI_SHIM_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int32_t  format;
    int32_t  w;
    int32_t  h;
    int32_t  refresh_rate;
    void    *driverdata;
} VMSVGA3DDxvkSdlDisplayMode;

typedef struct {
    int32_t x, y, w, h;
} VMSVGA3DDxvkSdlRect;

/* Mirrors the implementations in hw/display/vmware_vga_dxvk_wsi.c. */
typedef struct {
    uint32_t size;   /* sizeof(this struct), for forward compatibility */
    VMSVGA3DDxvkSdlDisplayMode *(*get_closest_display_mode)(
        int, const VMSVGA3DDxvkSdlDisplayMode *, VMSVGA3DDxvkSdlDisplayMode *);
    int  (*get_current_display_mode)(int, VMSVGA3DDxvkSdlDisplayMode *);
    int  (*get_desktop_display_mode)(int, VMSVGA3DDxvkSdlDisplayMode *);
    int  (*get_display_bounds)(int, VMSVGA3DDxvkSdlRect *);
    int  (*get_display_mode)(int, int, VMSVGA3DDxvkSdlDisplayMode *);
    const char *(*get_error)(void);
    int  (*get_num_video_displays)(void);
    int  (*get_window_display_index)(void *);
    int  (*set_window_display_mode)(void *, const VMSVGA3DDxvkSdlDisplayMode *);
    int  (*set_window_fullscreen)(void *, uint32_t);
    uint32_t (*get_window_flags)(void *);
    void (*get_window_size)(void *, int *, int *);
    void (*set_window_size)(void *, int, int);
    int  (*vulkan_load_library)(const char *);
    int  (*vulkan_get_instance_extensions)(void *, unsigned int *, const char **);
    int  (*vulkan_create_surface)(void *, void *, void *);
} VMSVGA3DWsiTable;

/* Called by QEMU immediately after dlopen, before DXVK is initialized.
 * Returns 0 on success, -1 if the table is NULL or a size mismatch. */
int vmsvga3d_wsi_register(const VMSVGA3DWsiTable *table);

#ifdef __cplusplus
}
#endif
#endif
