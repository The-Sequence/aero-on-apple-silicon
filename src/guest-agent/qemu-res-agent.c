/* qemu-res-agent: auto-resize agent for the vmvga QEMU VM, without VMware Tools.
 * Polls the VMware backdoor guestinfo key "guestinfo.qemu.resolution"
 * ("W H seq", set by QEMU when the host window is resized) and switches the
 * primary display to exactly W x H if the driver accepts it, else to the
 * closest listed mode (v3). */
#include <winsock2.h>
#include <windows.h>
#include <mmsystem.h>
#include <stdarg.h>

/* No C runtime (v4.1): runs on Vista/7 without the Universal CRT.  Only
 * kernel32 + user32; the compiler may still emit memset/memcpy calls. */
void *memset(void *d, int c, size_t n)
{
    unsigned char *p = d;
    while (n--) *p++ = (unsigned char)c;
    return d;
}

void *memcpy(void *d, const void *s, size_t n)
{
    unsigned char *p = d;
    const unsigned char *q = s;
    while (n--) *p++ = *q++;
    return d;
}

static unsigned str_len(const char *s)
{
    unsigned n = 0;
    while (s[n]) n++;
    return n;
}

/* "W H SEQ" -> 1 if three unsigned numbers were read */
static int parse3(const char *s, unsigned *a, unsigned *b, unsigned *c)
{
    unsigned *v[3] = { a, b, c };
    int i;
    for (i = 0; i < 3; i++) {
        unsigned x = 0;
        int any = 0;
        while (*s == ' ') s++;
        while (*s >= '0' && *s <= '9') { x = x * 10 + (unsigned)(*s - '0'); s++; any = 1; }
        if (!any) return 0;
        *v[i] = x;
    }
    return 1;
}

#define BDOOR_MAGIC 0x564D5868u
#define BDOOR_PORT  0x5658
#define BDOOR_CMD_MESSAGE 30u
enum { MSG_OPEN, MSG_SENDSIZE, MSG_SENDPAYLOAD, MSG_RECVSIZE,
       MSG_RECVPAYLOAD, MSG_RECVSTATUS, MSG_CLOSE };
#define MSG_SUCCESS 0x0001u

typedef struct { unsigned eax, ebx, ecx, edx, esi, edi; } regs_t;

static void bdoor(regs_t *r)
{
    __asm__ __volatile__("inl %%dx, %%eax"
        : "+a"(r->eax), "+b"(r->ebx), "+c"(r->ecx), "+d"(r->edx),
          "+S"(r->esi), "+D"(r->edi) :: "memory");
}

typedef struct { unsigned id, cookie_lo, cookie_hi; int open; } chan_t;

static int msg_call(chan_t *c, unsigned sub, unsigned ebx, regs_t *out)
{
    regs_t r = { BDOOR_MAGIC, ebx, BDOOR_CMD_MESSAGE | (sub << 16),
                 BDOOR_PORT | (c->id << 16), c->cookie_hi, c->cookie_lo };
    bdoor(&r);
    if (out) *out = r;
    return ((r.ecx >> 16) & MSG_SUCCESS) != 0;
}

static int chan_open(chan_t *c)
{
    regs_t r;
    memset(c, 0, sizeof(*c));
    if (!msg_call(c, MSG_OPEN, 0x49435052u | 0x80000000u, &r)) return 0;
    c->id = r.edx >> 16; c->cookie_hi = r.esi; c->cookie_lo = r.edi;
    c->open = 1;
    return 1;
}

static void chan_close(chan_t *c)
{
    if (c->open) msg_call(c, MSG_CLOSE, 0, NULL);
    c->open = 0;
}

/* Send a request and return the reply in buf (NUL-terminated). */
static int rpc(chan_t *c, const char *req, char *buf, unsigned cap)
{
    regs_t r;
    unsigned len = str_len(req), i, size;
    if (!msg_call(c, MSG_SENDSIZE, len, NULL)) return 0;
    for (i = 0; i < len; i += 4) {
        unsigned w = 0, k;
        for (k = 0; k < 4 && i + k < len; k++) w |= (unsigned)(unsigned char)req[i + k] << (k * 8);
        if (!msg_call(c, MSG_SENDPAYLOAD, w, NULL)) return 0;
    }
    if (!msg_call(c, MSG_RECVSIZE, 0, &r)) return 0;
    size = r.ebx;
    if (size >= cap) size = cap - 1;
    for (i = 0; i < size; i += 4) {
        unsigned k;
        if (!msg_call(c, MSG_RECVPAYLOAD, 0, &r)) return 0;
        for (k = 0; k < 4 && i + k < size; k++) buf[i + k] = (char)(r.ebx >> (k * 8));
    }
    buf[size] = 0;
    msg_call(c, MSG_RECVSTATUS, 0, NULL);
    return 1;
}

/* v3: log to C:\qemu-res-agent.log (mode list at start, every decision). */
static void agent_log(const char *fmt, ...)
{
    static char line[1100];
    HANDLE f = CreateFileA("C:\\qemu-res-agent.log", FILE_APPEND_DATA,
                           FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                           OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    va_list ap;
    SYSTEMTIME t;
    DWORD n, done;
    if (f == INVALID_HANDLE_VALUE) return;
    GetLocalTime(&t);
    n = (DWORD)wsprintfA(line, "%02u:%02u:%02u ", t.wHour, t.wMinute, t.wSecond);
    va_start(ap, fmt); n += (DWORD)wvsprintfA(line + n, fmt, ap); va_end(ap);
    line[n++] = '\r'; line[n++] = '\n';
    WriteFile(f, line, n, &done, NULL);
    CloseHandle(f);
}

static void log_modes(void)
{
    static char line[1000];
    DEVMODEA dm;
    DWORD i;
    unsigned n = 0;
    line[0] = 0;
    for (i = 0;; i++) {
        memset(&dm, 0, sizeof(dm)); dm.dmSize = sizeof(dm);
        if (!EnumDisplaySettingsA(NULL, i, &dm)) break;
        if (dm.dmBitsPerPel != 32) continue;
        if (n + 24 < sizeof(line))
            n += (unsigned)wsprintfA(line + n, " %lux%lu",
                                     dm.dmPelsWidth, dm.dmPelsHeight);
    }
    agent_log("modes:%s", line);
}

/* v4: ask the VMware WDDM driver (vm3dmp 8.16) to add an exact mode, the
 * way VMware Tools does: driver-private D3DKMTEscape, command 6 = topology
 * {count, {source id, width, height, refresh mHz, bpp}...}.  Reverse
 * engineered from vm3dmp.sys (escape dispatcher -> topology handler, which
 * validates 24000..120000 mHz and 8/16/32 bpp, then updates/hot-plugs the
 * monitor so the OS re-reads its mode list). */
typedef struct { HDC hDc; UINT hAdapter; LUID AdapterLuid; UINT VidPnSourceId; } KMT_OPENHDC;
typedef struct { UINT hAdapter; UINT hDevice; UINT Type; UINT Flags;
                 void *pPrivateDriverData; UINT PrivateDriverDataSize; UINT hContext; } KMT_ESCAPE;
typedef struct { UINT hAdapter; } KMT_CLOSE;
typedef LONG (APIENTRY *PFN_OPENHDC)(KMT_OPENHDC *);
typedef LONG (APIENTRY *PFN_ESCAPE)(const KMT_ESCAPE *);
typedef LONG (APIENTRY *PFN_CLOSE)(const KMT_CLOSE *);

static LONG vmw_add_mode(unsigned w, unsigned h)
{
    HMODULE gdi = LoadLibraryA("gdi32.dll");
    PFN_OPENHDC open_hdc = gdi ? (PFN_OPENHDC)GetProcAddress(gdi, "D3DKMTOpenAdapterFromHdc") : NULL;
    PFN_ESCAPE esc = gdi ? (PFN_ESCAPE)GetProcAddress(gdi, "D3DKMTEscape") : NULL;
    PFN_CLOSE close_ad = gdi ? (PFN_CLOSE)GetProcAddress(gdi, "D3DKMTCloseAdapter") : NULL;
    KMT_OPENHDC oa;
    KMT_ESCAPE e;
    KMT_CLOSE ca;
    UINT pkt[8];
    LONG st;

    if (!open_hdc || !esc || !close_ad) return -1;
    {   /* the escape layout is vm3dmp-specific: never send it elsewhere */
        DISPLAY_DEVICEA dd;
        const char *want = "VMware SVGA 3D";
        unsigned k = 0, j;
        int ok = 0;
        memset(&dd, 0, sizeof(dd)); dd.cb = sizeof(dd);
        if (EnumDisplayDevicesA(NULL, 0, &dd, 0)) {
            for (k = 0; dd.DeviceString[k] && !ok; k++) {
                for (j = 0; want[j] && dd.DeviceString[k + j] == want[j]; j++) {}
                ok = want[j] == 0;
            }
        }
        if (!ok) { agent_log("escape: adapter '%s' is not VMware SVGA 3D, skipped", dd.DeviceString); return -1; }
    }
    memset(&oa, 0, sizeof(oa));
    oa.hDc = GetDC(NULL);
    st = open_hdc(&oa);
    ReleaseDC(NULL, oa.hDc);
    if (st != 0) { agent_log("escape: open adapter failed 0x%08lx", st); return st; }

    pkt[0] = 6;                 /* command: set topology */
    pkt[1] = 4 + 20;            /* payload bytes after this header */
    pkt[2] = 1;                 /* one monitor */
    pkt[3] = oa.VidPnSourceId;  /* monitor / source id */
    pkt[4] = w;
    pkt[5] = h;
    pkt[6] = 60000;             /* refresh, mHz */
    pkt[7] = 32;                /* bpp */
    memset(&e, 0, sizeof(e));
    e.hAdapter = oa.hAdapter;
    e.Type = 0;                 /* D3DKMT_ESCAPE_DRIVERPRIVATE */
    e.pPrivateDriverData = pkt;
    e.PrivateDriverDataSize = sizeof(pkt);
    st = esc(&e);
    agent_log("escape: topology %ux%u source=%u -> 0x%08lx", w, h, oa.VidPnSourceId, st);
    ca.hAdapter = oa.hAdapter;
    close_ad(&ca);
    return st;
}

static LONG apply_mode(DEVMODEA *dm, DWORD flags)
{
    dm->dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_BITSPERPEL;
    dm->dmBitsPerPel = 32;
    return ChangeDisplaySettingsExA(NULL, dm, NULL, flags, NULL);
}

/* 1) Try the exact window size: the driver may accept a mode it does not
 *    list.  2) Otherwise the listed mode closest to the window: aspect ratio
 *    first, then size, never more than 25% bigger (QEMU scales to fit). */
static void set_best_mode(unsigned want_w, unsigned want_h)
{
    DEVMODEA dm, best, cur;
    DWORD i;
    double want_aspect = (double)want_w / want_h, best_score = 1e18;
    int found = 0;
    LONG r;

    memset(&cur, 0, sizeof(cur)); cur.dmSize = sizeof(cur);
    EnumDisplaySettingsA(NULL, ENUM_CURRENT_SETTINGS, &cur);
    if (cur.dmPelsWidth == want_w && cur.dmPelsHeight == want_h) {
        agent_log("want %ux%u: already current", want_w, want_h);
        return;
    }

    memset(&dm, 0, sizeof(dm)); dm.dmSize = sizeof(dm);
    dm.dmPelsWidth = want_w; dm.dmPelsHeight = want_h;
    r = apply_mode(&dm, CDS_TEST);
    if (r == DISP_CHANGE_SUCCESSFUL) {
        r = apply_mode(&dm, CDS_UPDATEREGISTRY);
        agent_log("want %ux%u: exact -> %ld", want_w, want_h, r);
        if (r == DISP_CHANGE_SUCCESSFUL) return;
    } else {
        int tries;
        agent_log("want %ux%u: exact mode not listed (%ld), asking driver", want_w, want_h, r);
        if (vmw_add_mode(want_w, want_h) == 0) {
            for (tries = 0; tries < 12; tries++) {       /* OS re-reads modes async */
                Sleep(250);
                memset(&dm, 0, sizeof(dm)); dm.dmSize = sizeof(dm);
                dm.dmPelsWidth = want_w; dm.dmPelsHeight = want_h;
                if (apply_mode(&dm, CDS_TEST) == DISP_CHANGE_SUCCESSFUL) {
                    r = apply_mode(&dm, CDS_UPDATEREGISTRY);
                    agent_log("want %ux%u: exact after escape (try %d) -> %ld",
                              want_w, want_h, tries, r);
                    if (r == DISP_CHANGE_SUCCESSFUL) return;
                    break;
                }
            }
            memset(&cur, 0, sizeof(cur)); cur.dmSize = sizeof(cur);
            EnumDisplaySettingsA(NULL, ENUM_CURRENT_SETTINGS, &cur);
            if (cur.dmPelsWidth == want_w && cur.dmPelsHeight == want_h) {
                agent_log("want %ux%u: driver switched to it itself", want_w, want_h);
                return;
            }
            agent_log("want %ux%u: still not accepted, falling back", want_w, want_h);
        }
    }

    memset(&best, 0, sizeof(best));
    for (i = 0;; i++) {
        double aspect, da, sw, sh, score;
        memset(&dm, 0, sizeof(dm)); dm.dmSize = sizeof(dm);
        if (!EnumDisplaySettingsA(NULL, i, &dm)) break;
        if (dm.dmBitsPerPel != 32 || dm.dmPelsWidth < 640 || dm.dmPelsHeight < 480) continue;
        if (dm.dmPelsWidth > want_w * 1.25 || dm.dmPelsHeight > want_h * 1.25) continue;
        aspect = (double)dm.dmPelsWidth / dm.dmPelsHeight;
        da = aspect / want_aspect; if (da < 1) da = 1 / da;          /* >= 1 */
        sw = (double)dm.dmPelsWidth / want_w; if (sw < 1) sw = 1 / sw;
        sh = (double)dm.dmPelsHeight / want_h; if (sh < 1) sh = 1 / sh;
        score = (da - 1) * 4.0 + (sw - 1) + (sh - 1);
        if (score < best_score) { best_score = score; best = dm; found = 1; }
    }
    if (!found) { agent_log("want %ux%u: no usable mode", want_w, want_h); return; }
    if (best.dmPelsWidth == cur.dmPelsWidth && best.dmPelsHeight == cur.dmPelsHeight) {
        agent_log("want %ux%u: best listed %lux%lu already current",
                  want_w, want_h, best.dmPelsWidth, best.dmPelsHeight);
        return;
    }
    r = apply_mode(&best, CDS_UPDATEREGISTRY);
    agent_log("want %ux%u: best listed %lux%lu -> %ld", want_w, want_h,
              best.dmPelsWidth, best.dmPelsHeight, r);
}


/* ------------------------------------------------------------------ v5
 * Health reports.  Written with "info-set guestinfo.aero.<key> <value>";
 * QEMU logs a line whenever a value changes, and the setup screen on the
 * Mac reads them.  Everything is loaded on demand from system DLLs, so a
 * missing component just reports "n/a" instead of stopping the agent. */
static void report(const char *key, const char *value)
{
    char req[300];
    char reply[64];
    chan_t ch;
    wsprintfA(req, "info-set guestinfo.aero.%s %s", key, value);
    if (chan_open(&ch)) {
        rpc(&ch, req, reply, sizeof(reply));
        chan_close(&ch);
    }
}

static void check_dwm(void)
{
    typedef HRESULT (WINAPI *PFN)(BOOL *);
    HMODULE m = LoadLibraryA("dwmapi.dll");
    PFN f = m ? (PFN)GetProcAddress(m, "DwmIsCompositionEnabled") : NULL;
    BOOL on = FALSE;
    if (!f) { report("aero", "n/a"); return; }
    if (f(&on) != 0) { report("aero", "error"); return; }
    report("aero", on ? "on" : "off");
}

static void check_audio(void)
{
    typedef UINT (WINAPI *PFN_NUM)(void);
    typedef MMRESULT (WINAPI *PFN_CAPS)(UINT_PTR, LPWAVEOUTCAPSA, UINT);
    HMODULE m = LoadLibraryA("winmm.dll");
    PFN_NUM num = m ? (PFN_NUM)GetProcAddress(m, "waveOutGetNumDevs") : NULL;
    PFN_CAPS caps = m ? (PFN_CAPS)GetProcAddress(m, "waveOutGetDevCapsA") : NULL;
    WAVEOUTCAPSA wc;
    char v[64];
    UINT n;
    if (!num) { report("audio", "n/a"); return; }
    n = num();
    if (n == 0) { report("audio", "none"); return; }
    memset(&wc, 0, sizeof(wc));
    if (caps && caps(0, &wc, sizeof(wc)) == MMSYSERR_NOERROR)
        wsprintfA(v, "%u %s", n, wc.szPname);
    else
        wsprintfA(v, "%u", n);
    report("audio", v);
}

/* Internet: resolve a Microsoft connectivity-check host and open a TCP
 * connection to it on port 80, with a short timeout. */
static void check_net(void)
{
    typedef int (WSAAPI *PFN_STARTUP)(WORD, LPWSADATA);
    typedef struct hostent *(WSAAPI *PFN_GHBN)(const char *);
    typedef SOCKET (WSAAPI *PFN_SOCKET)(int, int, int);
    typedef int (WSAAPI *PFN_IOCTL)(SOCKET, long, u_long *);
    typedef int (WSAAPI *PFN_CONNECT)(SOCKET, const struct sockaddr *, int);
    typedef int (WSAAPI *PFN_SELECT)(int, fd_set *, fd_set *, fd_set *, const struct timeval *);
    typedef int (WSAAPI *PFN_GETOPT)(SOCKET, int, int, char *, int *);
    typedef int (WSAAPI *PFN_CLOSE)(SOCKET);
    typedef u_short (WSAAPI *PFN_HTONS)(u_short);
    HMODULE m = LoadLibraryA("ws2_32.dll");
    PFN_STARTUP startup; PFN_GHBN ghbn; PFN_SOCKET sock; PFN_IOCTL ioctl; PFN_CONNECT conn;
    PFN_SELECT sel; PFN_GETOPT getopt; PFN_CLOSE closes; PFN_HTONS hs;
    WSADATA wd;
    struct hostent *he;
    struct sockaddr_in sa;
    fd_set wr;
    struct timeval tv;
    SOCKET s;
    u_long nb = 1;
    int err = 0, len = sizeof(err), r;
    if (!m) { report("net", "n/a"); return; }
    startup = (PFN_STARTUP)GetProcAddress(m, "WSAStartup");
    ghbn = (PFN_GHBN)GetProcAddress(m, "gethostbyname");
    sock = (PFN_SOCKET)GetProcAddress(m, "socket");
    ioctl = (PFN_IOCTL)GetProcAddress(m, "ioctlsocket");
    conn = (PFN_CONNECT)GetProcAddress(m, "connect");
    sel = (PFN_SELECT)GetProcAddress(m, "select");
    getopt = (PFN_GETOPT)GetProcAddress(m, "getsockopt");
    closes = (PFN_CLOSE)GetProcAddress(m, "closesocket");
    hs = (PFN_HTONS)GetProcAddress(m, "htons");
    if (!startup || !ghbn || !sock || !ioctl || !conn || !sel || !getopt || !closes || !hs ||
        startup(MAKEWORD(2, 2), &wd) != 0) { report("net", "n/a"); return; }
    he = ghbn("www.msftncsi.com");
    if (!he || !he->h_addr_list || !he->h_addr_list[0]) { report("net", "no-dns"); return; }
    s = sock(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) { report("net", "no-socket"); return; }
    ioctl(s, FIONBIO, &nb);
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_port = hs(80);
    memcpy(&sa.sin_addr, he->h_addr_list[0], 4);
    conn(s, (struct sockaddr *)&sa, sizeof(sa));
    wr.fd_count = 1;              /* one socket: no FD_ISSET needed */
    wr.fd_array[0] = s;
    tv.tv_sec = 5; tv.tv_usec = 0;
    r = sel(0, NULL, &wr, NULL, &tv);
    if (r == 1 && getopt(s, SOL_SOCKET, SO_ERROR, (char *)&err, &len) == 0 && err == 0)
        report("net", "ok");
    else
        report("net", r == 0 ? "timeout" : "no-connect");
    closes(s);
}

static void check_display(void)
{
    DISPLAY_DEVICEA dd;
    DEVMODEA dm;
    char v[64];
    memset(&dd, 0, sizeof(dd)); dd.cb = sizeof(dd);
    if (EnumDisplayDevicesA(NULL, 0, &dd, 0)) report("gpu", dd.DeviceString);
    memset(&dm, 0, sizeof(dm)); dm.dmSize = sizeof(dm);
    if (EnumDisplaySettingsA(NULL, ENUM_CURRENT_SETTINGS, &dm)) {
        wsprintfA(v, "%lux%lu", dm.dmPelsWidth, dm.dmPelsHeight);
        report("res", v);
    }
}

static void check_all(void)
{
    check_display();
    check_dwm();
    check_audio();
    check_net();
}

int WINAPI WinMain(HINSTANCE a, HINSTANCE b, LPSTR c, int d)
{
    char buf[256];
    unsigned last_seq = 0;
    HANDLE one = CreateMutexA(NULL, TRUE, "Local\\qemu-res-agent");
    (void)a; (void)b; (void)c; (void)d;
    if (GetLastError() == ERROR_ALREADY_EXISTS) return 0;   /* one per session */
    agent_log("qemu-res-agent v5 started");
    log_modes();
    report("agent", "v5");
    check_all();

    for (;;) {
        static DWORD last_check = 0;
        if (GetTickCount() - last_check > 20000) {     /* every 20 s */
            last_check = GetTickCount();
            check_all();
        }
        chan_t ch;
        if (chan_open(&ch)) {
            if (rpc(&ch, "info-get guestinfo.qemu.resolution", buf, sizeof(buf)) &&
                buf[0] == '1' && buf[1] == ' ') {
                unsigned w = 0, h = 0, seq = 0;
                if (parse3(buf + 2, &w, &h, &seq) &&
                    seq != last_seq && w >= 640 && h >= 480) {
                    last_seq = seq;
                    set_best_mode(w, h);
                    check_display();
                }
            }
            chan_close(&ch);
        }
        Sleep(500);
    }
    (void)one;
    return 0;
}

/* Entry point without the C runtime. */
void WINAPI AgentStartup(void)
{
    ExitProcess((UINT)WinMain(GetModuleHandleA(NULL), NULL, GetCommandLineA(), SW_SHOWDEFAULT));
}
