/*
 * libvmsvga3d_wsi - macOS delivery shim. See vmsvga3d_wsi_shim.h for rationale.
 *
 * Every exported SDL_* symbol is a thin forwarder through a table QEMU
 * registers at load time. If QEMU has not registered (or a given entry is
 * NULL) the forwarders fail safely rather than jumping through a null pointer:
 * DXVK treats a NULL/-1 return as "unsupported" and reports it.
 */
#include "vmsvga3d_wsi_shim.h"
#include <string.h>
#include <stddef.h>

static VMSVGA3DWsiTable g_tbl;
static int g_ready;

int vmsvga3d_wsi_register(const VMSVGA3DWsiTable *table)
{
    if (table == NULL || table->size != sizeof(VMSVGA3DWsiTable)) {
        g_ready = 0;
        return -1;
    }
    memcpy(&g_tbl, table, sizeof(g_tbl));
    g_ready = 1;
    return 0;
}

#define HAVE(fn) (g_ready && g_tbl.fn != NULL)

/* ---- display enumeration ------------------------------------------------ */

VMSVGA3DDxvkSdlDisplayMode *SDL_GetClosestDisplayMode(
    int display_index, const VMSVGA3DDxvkSdlDisplayMode *wanted,
    VMSVGA3DDxvkSdlDisplayMode *closest)
{
    if (!HAVE(get_closest_display_mode)) return NULL;
    return g_tbl.get_closest_display_mode(display_index, wanted, closest);
}

int SDL_GetCurrentDisplayMode(int i, VMSVGA3DDxvkSdlDisplayMode *m)
{
    return HAVE(get_current_display_mode) ? g_tbl.get_current_display_mode(i, m) : -1;
}

int SDL_GetDesktopDisplayMode(int i, VMSVGA3DDxvkSdlDisplayMode *m)
{
    return HAVE(get_desktop_display_mode) ? g_tbl.get_desktop_display_mode(i, m) : -1;
}

int SDL_GetDisplayBounds(int i, VMSVGA3DDxvkSdlRect *r)
{
    return HAVE(get_display_bounds) ? g_tbl.get_display_bounds(i, r) : -1;
}

int SDL_GetDisplayMode(int i, int mode_index, VMSVGA3DDxvkSdlDisplayMode *m)
{
    return HAVE(get_display_mode) ? g_tbl.get_display_mode(i, mode_index, m) : -1;
}

int SDL_GetNumVideoDisplays(void)
{
    return HAVE(get_num_video_displays) ? g_tbl.get_num_video_displays() : 0;
}

/* ---- window ------------------------------------------------------------- */

int SDL_GetWindowDisplayIndex(void *w)
{
    return HAVE(get_window_display_index) ? g_tbl.get_window_display_index(w) : -1;
}

int SDL_SetWindowDisplayMode(void *w, const VMSVGA3DDxvkSdlDisplayMode *m)
{
    return HAVE(set_window_display_mode) ? g_tbl.set_window_display_mode(w, m) : -1;
}

int SDL_SetWindowFullscreen(void *w, uint32_t flags)
{
    return HAVE(set_window_fullscreen) ? g_tbl.set_window_fullscreen(w, flags) : -1;
}

uint32_t SDL_GetWindowFlags(void *w)
{
    return HAVE(get_window_flags) ? g_tbl.get_window_flags(w) : 0u;
}

void SDL_GetWindowSize(void *w, int *width, int *height)
{
    if (HAVE(get_window_size)) {
        g_tbl.get_window_size(w, width, height);
    } else {
        if (width)  *width  = 0;
        if (height) *height = 0;
    }
}

void SDL_SetWindowSize(void *w, int width, int height)
{
    if (HAVE(set_window_size)) g_tbl.set_window_size(w, width, height);
}

/* ---- vulkan ------------------------------------------------------------- */

int SDL_Vulkan_LoadLibrary(const char *path)
{
    return HAVE(vulkan_load_library) ? g_tbl.vulkan_load_library(path) : -1;
}

int SDL_Vulkan_GetInstanceExtensions(void *w, unsigned int *count,
                                     const char **names)
{
    return HAVE(vulkan_get_instance_extensions)
         ? g_tbl.vulkan_get_instance_extensions(w, count, names) : 0;
}

int SDL_Vulkan_CreateSurface(void *w, void *instance, void *surface)
{
    return HAVE(vulkan_create_surface)
         ? g_tbl.vulkan_create_surface(w, instance, surface) : 0;
}

/* ---- error -------------------------------------------------------------- */

const char *SDL_GetError(void)
{
    if (HAVE(get_error)) return g_tbl.get_error();
    return g_ready ? "" : "vmsvga3d WSI shim: QEMU has not registered its table";
}
