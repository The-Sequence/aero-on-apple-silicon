/* aero-probe: live view of what Windows is doing, streamed to QEMU.
 *
 * Runs inside the guest (as administrator / SYSTEM).  Opens two real-time
 * ETW sessions -- the NT kernel logger for driver and process events, and a
 * private session for the graphics, Direct3D 9, desktop compositor, audio
 * and Plug and Play providers -- decodes each event's name with TDH and sends
 * a line per event to the host over the VMware backdoor ("log aero-probe:").
 * QEMU prints those lines in its log; the control screen's Live log shows them.
 *
 * Errors and warnings go out immediately.  Chatty informational events are
 * counted and summarised every 2 seconds, so a 60 fps desktop does not flood
 * the backdoor.
 *
 * Build (on the Mac):
 *   x86_64-w64-mingw32-gcc -O2 -municode -o aero-probe.exe aero-probe.c \
 *       -ltdh -ladvapi32 -lole32
 */
#define INITGUID
#include <windows.h>
#include <evntrace.h>
#include <evntcons.h>
#include <tdh.h>
#include <stdio.h>
#include <stdarg.h>
#include <wchar.h>

/* ------------------------------------------------------------ backdoor RPC */
#define BDOOR_MAGIC 0x564D5868u
#define BDOOR_PORT  0x5658
#define BDOOR_CMD_MESSAGE 30u
enum { MSG_OPEN, MSG_SENDSIZE, MSG_SENDPAYLOAD, MSG_RECVSIZE,
       MSG_RECVPAYLOAD, MSG_RECVSTATUS, MSG_CLOSE };
#define MSG_SUCCESS 0x0001u

typedef struct { unsigned eax, ebx, ecx, edx, esi, edi; } regs_t;
typedef struct { unsigned id, cookie_lo, cookie_hi; int open; } chan_t;

static void bdoor(regs_t *r)
{
    __asm__ __volatile__("inl %%dx, %%eax"
        : "+a"(r->eax), "+b"(r->ebx), "+c"(r->ecx), "+d"(r->edx),
          "+S"(r->esi), "+D"(r->edi) :: "memory");
}

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

static int rpc_send(chan_t *c, const char *req)
{
    regs_t r;
    unsigned len = (unsigned)strlen(req), i, size, k;
    if (!msg_call(c, MSG_SENDSIZE, len, NULL)) return 0;
    for (i = 0; i < len; i += 4) {
        unsigned w = 0;
        for (k = 0; k < 4 && i + k < len; k++) w |= (unsigned)(unsigned char)req[i + k] << (k * 8);
        if (!msg_call(c, MSG_SENDPAYLOAD, w, NULL)) return 0;
    }
    if (!msg_call(c, MSG_RECVSIZE, 0, &r)) return 0;
    size = r.ebx;
    for (i = 0; i < size; i += 4)
        if (!msg_call(c, MSG_RECVPAYLOAD, 0, &r)) return 0;
    msg_call(c, MSG_RECVSTATUS, 0, NULL);
    return 1;
}

static CRITICAL_SECTION g_send_lock;
static chan_t g_chan;
static FILE *g_file;

/* One line to the host (and to C:\aero-probe.log). */
static void out(const char *fmt, ...)
{
    char line[600] = "log aero-probe: ";
    size_t pre = strlen(line);
    va_list ap;
    va_start(ap, fmt);
    _vsnprintf(line + pre, sizeof(line) - pre - 1, fmt, ap);
    va_end(ap);
    line[sizeof(line) - 1] = 0;

    EnterCriticalSection(&g_send_lock);
    if (g_file) { fprintf(g_file, "%s\n", line + pre); fflush(g_file); }
    if (!g_chan.open) chan_open(&g_chan);
    if (g_chan.open && !rpc_send(&g_chan, line)) {
        chan_close(&g_chan);
        if (chan_open(&g_chan)) rpc_send(&g_chan, line);
    }
    LeaveCriticalSection(&g_send_lock);
}

/* ------------------------------------------------------------ providers */
DEFINE_GUID(DXGKRNL,  0x802ec45a, 0x1e99, 0x4b83, 0x99, 0x20, 0x87, 0xc9, 0x82, 0x77, 0xba, 0x9d);
DEFINE_GUID(D3D9P,    0x783aca0a, 0x790e, 0x4d7f, 0x84, 0x51, 0xaa, 0x85, 0x05, 0x11, 0xc6, 0xb9);
DEFINE_GUID(DWMCORE,  0x9e9bba3c, 0x2e38, 0x40cb, 0x99, 0xf4, 0x9e, 0x82, 0x81, 0x42, 0x51, 0x64);
DEFINE_GUID(AUDIOP,   0xae4bd3be, 0xf36f, 0x45b6, 0x8d, 0x21, 0xbd, 0xd6, 0xfb, 0x83, 0x28, 0x53);
DEFINE_GUID(KPNP,     0x9c205a39, 0x1250, 0x487d, 0xab, 0xd7, 0xe8, 0x31, 0xc6, 0x29, 0x05, 0x39);

static const struct { const GUID *guid; const char *name; UCHAR level; } providers[] = {
    { &DXGKRNL, "DxgKrnl", TRACE_LEVEL_VERBOSE },
    { &D3D9P,   "D3D9",    TRACE_LEVEL_VERBOSE },
    { &DWMCORE, "DWM",     TRACE_LEVEL_WARNING },
    { &AUDIOP,  "Audio",   TRACE_LEVEL_INFORMATION },
    { &KPNP,    "PnP",     TRACE_LEVEL_INFORMATION },
};

/* ------------------------------------------------------------ summaries */
#define MAX_KEYS 256
static struct { char key[160]; unsigned count; } g_counts[MAX_KEYS];
static CRITICAL_SECTION g_count_lock;

static void count_event(const char *key)
{
    int i;
    EnterCriticalSection(&g_count_lock);
    for (i = 0; i < MAX_KEYS && g_counts[i].key[0]; i++)
        if (!strcmp(g_counts[i].key, key)) { g_counts[i].count++; goto done; }
    if (i < MAX_KEYS) { strncpy(g_counts[i].key, key, sizeof(g_counts[i].key) - 1); g_counts[i].count = 1; }
done:
    LeaveCriticalSection(&g_count_lock);
}

static DWORD WINAPI summary_thread(LPVOID unused)
{
    (void)unused;
    for (;;) {
        char buf[560];
        size_t n = 0;
        int i;
        Sleep(2000);
        EnterCriticalSection(&g_count_lock);
        buf[0] = 0;
        for (i = 0; i < MAX_KEYS && g_counts[i].key[0]; i++) {
            if (!g_counts[i].count) continue;
            n += _snprintf(buf + n, sizeof(buf) - n, "%s%s x%u", n ? ", " : "", g_counts[i].key, g_counts[i].count);
            g_counts[i].count = 0;
            if (n > sizeof(buf) - 170) { buf[sizeof(buf) - 1] = 0; out("2s: %s", buf); n = 0; buf[0] = 0; }
        }
        LeaveCriticalSection(&g_count_lock);
        if (n) out("2s: %s", buf);
    }
    return 0;
}

/* ------------------------------------------------------------ decoding */
static void wtoa(const WCHAR *w, char *a, size_t cap)
{
    size_t i = 0;
    if (!w) { a[0] = 0; return; }
    for (; w[i] && i + 1 < cap; i++) a[i] = (w[i] < 128) ? (char)w[i] : '?';
    a[i] = 0;
}

static const char *prov_name(const GUID *g)
{
    size_t i;
    for (i = 0; i < sizeof(providers) / sizeof(providers[0]); i++)
        if (IsEqualGUID(g, providers[i].guid)) return providers[i].name;
    return NULL;
}

/* Best-effort: read a string property (e.g. "FileName") from an event. */
static int get_string_prop(PEVENT_RECORD ev, const WCHAR *name, char *outbuf, size_t cap, int ansi)
{
    PROPERTY_DATA_DESCRIPTOR d;
    BYTE buf[1024];
    ULONG size = 0;
    d.PropertyName = (ULONGLONG)(ULONG_PTR)name;
    d.ArrayIndex = ULONG_MAX;
    d.Reserved = 0;
    if (TdhGetPropertySize(ev, 0, NULL, 1, &d, &size) != ERROR_SUCCESS || size == 0 || size > sizeof(buf))
        return 0;
    if (TdhGetProperty(ev, 0, NULL, 1, &d, size, buf) != ERROR_SUCCESS)
        return 0;
    if (ansi) {
        size_t n = size < cap ? size : cap - 1;
        memcpy(outbuf, buf, n);
        outbuf[n] = 0;
    } else {
        wtoa((const WCHAR *)buf, outbuf, cap);
    }
    return 1;
}

static void WINAPI on_event(PEVENT_RECORD ev)
{
    BYTE infobuf[8192];
    PTRACE_EVENT_INFO info = (PTRACE_EVENT_INFO)infobuf;
    ULONG size = sizeof(infobuf);
    char task[96] = "", opcode[64] = "", prov[64] = "", key[160];
    const char *pn = prov_name(&ev->EventHeader.ProviderId);
    UCHAR level = ev->EventHeader.EventDescriptor.Level;

    if (TdhGetEventInformation(ev, 0, NULL, info, &size) == ERROR_SUCCESS) {
        if (info->TaskNameOffset)   wtoa((WCHAR *)(infobuf + info->TaskNameOffset), task, sizeof(task));
        if (info->OpcodeNameOffset) wtoa((WCHAR *)(infobuf + info->OpcodeNameOffset), opcode, sizeof(opcode));
        if (!pn && info->ProviderNameOffset) wtoa((WCHAR *)(infobuf + info->ProviderNameOffset), prov, sizeof(prov));
    }
    if (pn) strncpy(prov, pn, sizeof(prov) - 1);
    if (!prov[0]) {
        const GUID *g = &ev->EventHeader.ProviderId;
        _snprintf(prov, sizeof(prov), "{%08lx-%04x}", g->Data1, g->Data2);
    }
    if (!task[0]) _snprintf(task, sizeof(task), "id%u", ev->EventHeader.EventDescriptor.Id);

    /* Kernel image loads: report drivers (System process) and new modules. */
    if (!strcmp(task, "Image") && !strcmp(opcode, "Load")) {
        char file[300];
        if (get_string_prop(ev, L"FileName", file, sizeof(file), 0))
            out("load pid=%lu %s", ev->EventHeader.ProcessId, file);
        return;
    }
    if (!strcmp(task, "Process") && !strcmp(opcode, "Start")) {
        char image[128];
        if (get_string_prop(ev, L"ImageFileName", image, sizeof(image), 1))
            out("process start pid=%lu %s", ev->EventHeader.ProcessId, image);
        return;
    }

    if (!strcmp(task, "Image") || (!strcmp(task, "Process") && strcmp(opcode, "End")))
        return;   /* unloads and start-of-session rundowns */

    _snprintf(key, sizeof(key), "%s/%s%s%s", prov, task, opcode[0] ? "/" : "", opcode);
    key[sizeof(key) - 1] = 0;
    if (level && level <= TRACE_LEVEL_WARNING)
        out("%s %s pid=%lu", level <= TRACE_LEVEL_ERROR ? "ERROR" : "warn", key, ev->EventHeader.ProcessId);
    else
        count_event(key);
}


/* ------------------------------------------------------------ wait chains
 * "aero-probe.exe waits <name.exe>": for every thread of that process, ask
 * Windows' Wait Chain Traversal what it is blocked on and who owns it. */
#include <wct.h>
#ifndef WCT_MAX_NODE_COUNT
#define WCT_MAX_NODE_COUNT 16
#endif
#ifndef WCTP_GETINFO_ALL_FLAGS
#define WCTP_GETINFO_ALL_FLAGS 0x7 /* out-of-proc, COM, critical sections */
#endif
#include <tlhelp32.h>

static const char *wct_type(WCT_OBJECT_TYPE t)
{
    switch (t) {
    case WctCriticalSectionType: return "CriticalSection";
    case WctSendMessageType:     return "SendMessage";
    case WctMutexType:           return "Mutex";
    case WctAlpcType:            return "ALPC";
    case WctComType:             return "COM";
    case WctThreadWaitType:      return "ThreadWait";
    case WctProcessWaitType:     return "ProcessWait";
    case WctThreadType:          return "Thread";
    case WctComActivationType:   return "ComActivation";
    case WctUnknownType:         return "Unknown";
    default:                     return "?";
    }
}

static int dump_waits(const WCHAR *exe)
{
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS | TH32CS_SNAPTHREAD, 0);
    PROCESSENTRY32W pe = { sizeof(pe) };
    THREADENTRY32 te = { sizeof(te) };
    HWCT wct = OpenThreadWaitChainSession(0, NULL);
    DWORD pids[16]; int npids = 0, i;

    if (snap == INVALID_HANDLE_VALUE || !wct) { out("waits: cannot open snapshot/WCT (%lu)", GetLastError()); return 1; }
    for (BOOL ok = Process32FirstW(snap, &pe); ok; ok = Process32NextW(snap, &pe))
        if (!_wcsicmp(pe.szExeFile, exe) && npids < 16) pids[npids++] = pe.th32ProcessID;
    if (!npids) { out("waits: %ls is not running", exe); return 1; }
    for (BOOL ok = Thread32First(snap, &te); ok; ok = Thread32Next(snap, &te)) {
        for (i = 0; i < npids; i++) if (te.th32OwnerProcessID == pids[i]) break;
        if (i == npids) continue;
        WAITCHAIN_NODE_INFO nodes[WCT_MAX_NODE_COUNT];
        DWORD count = WCT_MAX_NODE_COUNT; BOOL cycle = FALSE;
        if (!GetThreadWaitChain(wct, 0, WCTP_GETINFO_ALL_FLAGS, te.th32ThreadID, &count, nodes, &cycle)) {
            out("waits: pid %lu tid %lu: error %lu", te.th32OwnerProcessID, te.th32ThreadID, GetLastError());
            continue;
        }
        char line[520]; size_t n = 0; DWORD k;
        n += _snprintf(line + n, sizeof(line) - n, "waits: pid %lu tid %lu%s:", te.th32OwnerProcessID,
                       te.th32ThreadID, cycle ? " DEADLOCK" : "");
        for (k = 0; k < count && n < sizeof(line) - 80; k++) {
            WAITCHAIN_NODE_INFO *nd = &nodes[k];
            if (nd->ObjectType == WctThreadType) {
                n += _snprintf(line + n, sizeof(line) - n, " -> thread %lu(pid %lu,%s,wait %lums)",
                               nd->ThreadObject.ThreadId, nd->ThreadObject.ProcessId,
                               nd->ObjectStatus == WctStatusBlocked ? "blocked" :
                               nd->ObjectStatus == WctStatusRunning ? "running" : "other",
                               nd->ThreadObject.WaitTime);
            } else {
                char nm[80]; wtoa(nd->LockObject.ObjectName, nm, sizeof(nm));
                n += _snprintf(line + n, sizeof(line) - n, " -> %s%s%s", wct_type(nd->ObjectType),
                               nm[0] ? " " : "", nm);
            }
        }
        line[sizeof(line) - 1] = 0;
        out("%s", line);
    }
    CloseThreadWaitChainSession(wct);
    CloseHandle(snap);
    return 0;
}

/* ------------------------------------------------------------ sessions */
static TRACEHANDLE start_session(const WCHAR *name, ULONG enable_flags, int kernel)
{
    BYTE buf[sizeof(EVENT_TRACE_PROPERTIES) + 512];
    PEVENT_TRACE_PROPERTIES p = (PEVENT_TRACE_PROPERTIES)buf;
    TRACEHANDLE h = 0;
    ULONG rc;

    memset(buf, 0, sizeof(buf));
    p->Wnode.BufferSize = sizeof(buf);
    p->Wnode.Flags = WNODE_FLAG_TRACED_GUID;
    p->Wnode.ClientContext = 1;            /* QPC timestamps */
    if (kernel) p->Wnode.Guid = SystemTraceControlGuid;
    p->LogFileMode = EVENT_TRACE_REAL_TIME_MODE;
    p->EnableFlags = enable_flags;
    p->BufferSize = 64;
    p->MinimumBuffers = 8;
    p->FlushTimer = 1;
    p->LoggerNameOffset = sizeof(EVENT_TRACE_PROPERTIES);

    /* A stale session from a previous run would make StartTrace fail. */
    ControlTraceW(0, name, p, EVENT_TRACE_CONTROL_STOP);
    memset(buf, 0, sizeof(buf));
    p->Wnode.BufferSize = sizeof(buf);
    p->Wnode.Flags = WNODE_FLAG_TRACED_GUID;
    p->Wnode.ClientContext = 1;
    if (kernel) p->Wnode.Guid = SystemTraceControlGuid;
    p->LogFileMode = EVENT_TRACE_REAL_TIME_MODE;
    p->EnableFlags = enable_flags;
    p->BufferSize = 64;
    p->MinimumBuffers = 8;
    p->FlushTimer = 1;
    p->LoggerNameOffset = sizeof(EVENT_TRACE_PROPERTIES);

    rc = StartTraceW(&h, name, p);
    if (rc != ERROR_SUCCESS) {
        out("could not start session %ls (error %lu - run as administrator)", name, rc);
        return 0;
    }
    return h;
}

typedef struct { const WCHAR *name; } consume_arg;

static DWORD WINAPI consume_thread(LPVOID arg)
{
    EVENT_TRACE_LOGFILEW lf;
    TRACEHANDLE th;
    memset(&lf, 0, sizeof(lf));
    lf.LoggerName = (LPWSTR)((consume_arg *)arg)->name;
    lf.ProcessTraceMode = PROCESS_TRACE_MODE_REAL_TIME | PROCESS_TRACE_MODE_EVENT_RECORD;
    lf.EventRecordCallback = on_event;
    th = OpenTraceW(&lf);
    if (th == (TRACEHANDLE)INVALID_HANDLE_VALUE) {
        out("could not open %ls (error %lu)", lf.LoggerName, GetLastError());
        return 1;
    }
    ProcessTrace(&th, 1, NULL, NULL);   /* blocks while the session runs */
    CloseTrace(th);
    return 0;
}

int wmain(int argc, WCHAR **argv)
{
    static consume_arg k = { KERNEL_LOGGER_NAMEW }, a = { L"AeroProbe" };
    TRACEHANDLE ks, as;
    size_t i;

    InitializeCriticalSection(&g_send_lock);
    InitializeCriticalSection(&g_count_lock);
    g_file = _wfopen(L"C:\\aero-probe.log", L"a");
    if (argc >= 3 && !_wcsicmp(argv[1], L"waits"))
        return dump_waits(argv[2]);
    out("start v3");

    ks = start_session(KERNEL_LOGGER_NAMEW, EVENT_TRACE_FLAG_IMAGE_LOAD | EVENT_TRACE_FLAG_PROCESS, 1);
    as = start_session(L"AeroProbe", 0, 0);
    if (as) {
        for (i = 0; i < sizeof(providers) / sizeof(providers[0]); i++) {
            ULONG rc = EnableTraceEx2(as, providers[i].guid, EVENT_CONTROL_CODE_ENABLE_PROVIDER,
                                      providers[i].level, ~0ULL, 0, 0, NULL);
            out("provider %s %s", providers[i].name, rc == ERROR_SUCCESS ? "on" : "unavailable");
        }
    }
    CreateThread(NULL, 0, summary_thread, NULL, 0, NULL);
    if (ks) CreateThread(NULL, 0, consume_thread, &k, 0, NULL);
    if (as) consume_thread(&a);
    else Sleep(INFINITE);
    return 0;
}
