#!/usr/bin/env python3
"""Aero on Apple Silicon - the all-in-one control screen, in the style of a DOS utility.

Sets up tools, creates and installs VMs, runs them, and controls them while
they run (keys such as Ctrl+Alt+Del, discs, screenshots, pause, shut down),
with a live checklist of what to do next and health reports from inside
Windows.  VMs run as background processes: there is no second Terminal
window to keep open, and the VM window's close button is disabled.

Shares vms/<name>.conf, Setup.command and build/run-vm.sh with the plain
wizard (build/wizard.sh).  Written for the Python 3.9 that ships with Apple's
command-line tools, standard library only.

Exit status 3 asks START HERE.command to fall back to the plain wizard.
"""

import curses
import glob
import hashlib
import json
import locale
import os
import re
import select
import shlex
import shutil
import threading
import socket
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CACHE = os.environ.get("AERO_CACHE") or os.path.expanduser("~/Library/Caches/AeroOnAppleSilicon")
VMS = os.path.join(ROOT, "vms")
LOGS = os.path.join(ROOT, "logs")
QEMU_IMG = os.path.join(ROOT, "runtime", "bin", "qemu-img")
GUEST_TOOLS_ISO = os.path.join(ROOT, "guest-tools.iso")
VMTOOLS_FILE = "VMware-tools-windows-10.3.10-12406962.iso"
VMTOOLS_SHA256 = "edb889e6cce11aeb568dbf471cee1b3dc26ca72bc671b660ad4872911edbf6da"
BREW_PKGS = ["glib", "pixman", "sdl2-compat", "gnutls", "libpng", "jpeg-turbo", "zstd",
             "libslirp", "libusb", "molten-vk", "vulkan-loader", "p7zip"]
GB = 1024 ** 3
PLAIN = 3                # exit code: use the plain wizard instead
MIN_H, MIN_W = 24, 80
MIN_DISK_GB = 30         # Windows 7 x64 + updates + page file + hibernation file
MAX_SAFE_CORES = 4       # more vCPUs barely speed up TCG and make installs flakier

STAGES = ["new", "installed", "check", "ready"]
STAGE_TEXT = {"new": "not installed", "installed": "needs drivers", "check": "needs checks",
              "ready": "ready"}
STEP_NAMES = ["Tools ready", "Install Windows", "Graphics driver, clipboard and tools",
              "Check sound, internet and resizing", "Ready to use"]


# =====================================================================
# Files and state (the same rules as build/wizard.sh)
# =====================================================================

def runtime_tag():
    try:
        with open(os.path.join(ROOT, "Setup.command")) as f:
            m = re.search(r'^RUNTIME_TAG="(.*)"', f.read(), re.M)
            return m.group(1) if m else ""
    except OSError:
        return ""


def human(n):
    n = n or 0
    return "%.1f GB" % (n / GB) if n >= GB else "%d MB" % (n // (1024 * 1024))


def path_size(p):
    if not p or not os.path.exists(p):
        return 0
    if os.path.isfile(p):
        return os.stat(p).st_blocks * 512
    total = 0
    for dirpath, _, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_blocks * 512
            except OSError:
                pass
    return total


def sha_ok(path, want):
    if not os.path.isfile(path):
        return False
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest() == want


def conf_read(path):
    d = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.rstrip("\n")
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    d.setdefault(k, v)
    except OSError:
        pass
    return d


def conf_write(path, d):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for k, v in d.items():
            if not k.startswith("_"):
                f.write("%s=%s\n" % (k, v))
    os.replace(tmp, path)


def conf_set(path, key, value):
    d = conf_read(path)
    d[key] = str(value)
    conf_write(path, d)


def vm_running(disk):
    if not disk:
        return False
    r = subprocess.run(["pgrep", "-f", "--", "[q]emu-system.*file=" + disk], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    return r.returncode == 0


def disk_virtual(disk):
    if not (os.path.isfile(disk) and os.access(QEMU_IMG, os.X_OK)):
        return 0
    try:
        out = subprocess.run([QEMU_IMG, "info", "--output=json", "-U", disk], capture_output=True,
                             text=True, timeout=20).stdout
        return int(json.loads(out).get("virtual-size", 0))
    except Exception:
        return 0


def slug(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")


def clean_path(p):
    p = p.strip().strip('"').strip("'")
    return re.sub(r"\\(.)", r"\1", p)


def safe_rm(path):
    """Delete a file or folder, but only inside this project or the cache."""
    if not path or not (os.path.exists(path) or os.path.islink(path)):
        return True
    real = os.path.realpath(path)
    root = os.path.realpath(ROOT)
    cache = os.path.realpath(CACHE)
    if not (real.startswith(root + os.sep) or real == cache or real.startswith(cache + os.sep)):
        return False
    if os.path.isdir(real) and not os.path.islink(path):
        shutil.rmtree(real, ignore_errors=True)
    else:
        os.remove(path)
    return True


def adopt_stray_disks():
    os.makedirs(VMS, exist_ok=True)
    for disk in glob.glob(os.path.join(VMS, "*.qcow2")):
        conf = disk[:-6] + ".conf"
        if os.path.exists(conf):
            continue
        base = os.path.basename(disk)[:-6]
        conf_write(conf, {
            "VM_NAME": base, "DISK": disk,
            "GUEST": "vista" if "vista" in base.lower() else "win7",
            "VM_CPUS": "4", "MEM": "4096", "DISK_SIZE": "40G",
            "STAGE": "installed" if path_size(disk) > 3 * GB else "new",
        })


def list_vms():
    adopt_stray_disks()
    vms = []
    for conf in sorted(glob.glob(os.path.join(VMS, "*.conf"))):
        d = conf_read(conf)
        d["_conf"] = conf
        vms.append(d)
    return vms


def vm_slug(vm):
    return os.path.basename(vm["_conf"])[:-5]


def tools_status():
    """-> (all_ok, [(ok, text)])"""
    items = []
    try:
        installed = set(subprocess.run(["brew", "list", "--formula", "-1"], capture_output=True,
                                       text=True, timeout=60).stdout.split())
    except Exception:
        installed = set()
    missing = [p for p in BREW_PKGS if p not in installed]
    items.append((not missing, "Homebrew packages" if not missing
                  else "%d Homebrew package(s) to install" % len(missing)))
    tag = runtime_tag()
    ver = ""
    try:
        with open(os.path.join(ROOT, "runtime", ".version")) as f:
            ver = f.read().strip()
    except OSError:
        pass
    rt_ok = os.access(os.path.join(ROOT, "runtime", "bin", "qemu-system-x86_64"), os.X_OK) and ver == tag
    items.append((rt_ok, "Runtime %s" % tag if rt_ok else "Runtime %s not installed" % tag))
    drv_ok = os.path.isfile(os.path.join(CACHE, "driver-10.3.10", "vm3d.inf"))
    items.append((drv_ok, "Display driver" if drv_ok else "Display driver not extracted"))
    clip_ok = os.path.isfile(os.path.join(CACHE, "clipboard-0.141", "vdagent", "64", "vdservice.exe"))
    items.append((clip_ok, "Clipboard" if clip_ok else "Clipboard parts not downloaded"))
    disc_ok = os.path.isfile(GUEST_TOOLS_ISO)
    if disc_ok:
        built = os.path.getmtime(GUEST_TOOLS_ISO)
        for dirpath, _, files in os.walk(os.path.join(ROOT, "guest")):
            if any(os.path.getmtime(os.path.join(dirpath, f)) > built for f in files):
                disc_ok = False
    items.append((disc_ok, "Guest tools disc" if disc_ok else "Guest tools disc to build"))
    return all(ok for ok, _ in items), items


def mac_cores():
    try:
        return int(subprocess.run(["sysctl", "-n", "hw.perflevel0.logicalcpu"], capture_output=True,
                                  text=True).stdout.strip())
    except ValueError:
        return os.cpu_count() or 4


def mac_mem_mb():
    try:
        return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                                  text=True).stdout.strip()) // (1024 * 1024)
    except ValueError:
        return 8192


# =====================================================================
# Running VMs: launch, control socket (QMP), live session state
# =====================================================================

def qmp_sock(disk):
    # The same name build/run-vm.sh uses: md5 of the disk path.
    return "/tmp/aero-%s.sock" % hashlib.md5(disk.encode()).hexdigest()[:12]


def vm_env(vm, mode):
    log = os.path.join(LOGS, "%s-%s.log" % (vm_slug(vm), mode))
    guest = vm.get("GUEST", "win7")
    env = {
        "MODE": mode, "GUEST": guest, "DISK": vm.get("DISK", ""),
        "DISK_SIZE": vm.get("DISK_SIZE", "40G"), "VM_CPUS": vm.get("VM_CPUS", "4"),
        "MEM": vm.get("MEM", "4096"), "ISO": vm.get("ISO", ""), "HOST_LOG": log,
        "QMP_SOCK": qmp_sock(vm.get("DISK", "")),
        "AUDIO_DEVICE": vm.get("AUDIO_DEVICE", "usb" if guest == "vista" else "hda"),
        "NIC": vm.get("NIC", "e1000"), "CLIPBOARD": vm.get("CLIPBOARD", "on"),
    }
    return env, log


def launch_vm(vm, mode):
    """Start the VM as a background process (its own SDL window, no Terminal).
    It keeps running if this screen is closed."""
    env, log = vm_env(vm, mode)
    os.makedirs(LOGS, exist_ok=True)
    # Keep the previous run's log as .prev, so it is never mistaken for this run.
    if os.path.exists(log):
        os.replace(log, log + ".prev")
    full = dict(os.environ)
    full.update(env)
    # run-vm.sh's own messages (before QEMU starts its log) go to <log>.launch
    with open(log + ".launch", "w") as out:
        subprocess.Popen(["bash", os.path.join(ROOT, "build", "run-vm.sh")], cwd=ROOT, env=full,
                         stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         start_new_session=True)
    return log


SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values, width, top=None):
    """Last `width` samples as a one-line bar graph."""
    vals = list(values)[-width:]
    top = top or max(vals + [1e-9])
    out = "".join(SPARK[min(7, int(v / top * 7.999))] if v > 0 else " " for v in vals)
    return out.rjust(width)


def rate_text(bps):
    for unit in ("B/s", "KB/s", "MB/s"):
        if bps < 1024 or unit == "MB/s":
            return "%.0f %s" % (bps, unit) if unit == "B/s" else "%.1f %s" % (bps, unit)
        bps /= 1024.0


class Stats:
    """Samples the VM's host CPU, the Mac's GPU, the VM's network traffic and
    memory once a second on a background thread (nettop takes ~0.3 s)."""
    N = 120

    def __init__(self, disk, log=None, mem_mb=0):
        self.disk, self.log, self.mem_mb = disk, log, mem_mb
        self.cpu, self.gpu, self.net = [], [], []
        self.net_peak = 1024 * 1024
        self.now = {"cpu": 0.0, "gpu": 0.0, "vgpu": 0.0, "net": 0.0, "mem": 0, "down": 0.0, "up": 0.0}
        self.ncpu = os.cpu_count() or 1
        self._last_net = None
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop = True

    def _pid(self):
        try:
            # Never put the disk path in this helper's own command line:
            # run-vm.sh's one-copy check would mistake it for a running VM.
            for pid in subprocess.run(["pgrep", "qemu-system"], capture_output=True, text=True,
                                      timeout=3).stdout.split():
                cmd = subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True, text=True,
                                     timeout=3).stdout
                if "file=" + self.disk in cmd:
                    return pid
            return None
        except Exception:
            return None

    def _sample(self):
        pid = self._pid()
        cpu = mem = 0
        if pid:
            try:
                f = subprocess.run(["ps", "-o", "%cpu=,rss=", "-p", pid], capture_output=True, text=True,
                                   timeout=3).stdout.split()
                cpu, mem = float(f[0]) / self.ncpu, int(f[1]) * 1024
            except Exception:
                pass
        gpu = 0.0
        try:
            m = re.search(r'"Device Utilization %"=(\d+)', subprocess.run(
                ["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"], capture_output=True, text=True,
                timeout=3).stdout)
            gpu = float(m.group(1)) if m else 0.0
        except Exception:
            pass
        down = up = 0.0
        if pid:
            try:
                rows = subprocess.run(["nettop", "-P", "-L", "1", "-x", "-J", "bytes_in,bytes_out", "-p", pid],
                                      capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
                f = rows[-1].split(",")
                bi, bo, t = int(f[1]), int(f[2]), time.time()
                if self._last_net:
                    dt = max(0.2, t - self._last_net[2])
                    down, up = max(0, bi - self._last_net[0]) / dt, max(0, bo - self._last_net[1]) / dt
                self._last_net = (bi, bo, t)
            except Exception:
                pass
        self.net_peak = max(self.net_peak, down + up)
        self.now = {"cpu": cpu, "gpu": gpu, "vgpu": self._vgpu(), "net": down + up, "mem": mem,
                    "down": down, "up": up}
        for lst, v in ((self.cpu, cpu), (self.gpu, gpu), (self.net, down + up)):
            lst.append(v)
            del lst[:-self.N]

    def _vgpu(self):
        """Virtual graphics card busy time: QEMU logs, about once a second, how
        long it spent processing the card's command queue (VMVGA-PROFILE-2D)."""
        if not self.log:
            return 0.0
        try:
            with open(self.log, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 32768))
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            return 0.0
        for line in reversed(tail.splitlines()):
            if line.startswith("VMVGA-PROFILE-2D "):
                v = dict(kv.split("=", 1) for kv in line.split()[1:] if "=" in kv)
                try:
                    return min(100.0, int(v["fifo-us"]) / (int(v["interval-ms"]) * 10.0))
                except (KeyError, ValueError, ZeroDivisionError):
                    return 0.0
        return 0.0

    def _run(self):
        while not self._stop:
            t = time.time()
            self._sample()
            time.sleep(max(0.1, 1.0 - (time.time() - t)))


class QMP:
    """Minimal QEMU Machine Protocol client over the VM's control socket."""

    def __init__(self, path):
        self.path = path
        self.sock = None
        self.buf = b""
        self.events = []

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.buf = b""

    def _read_msg(self, timeout):
        end = time.time() + timeout
        while b"\n" not in self.buf:
            left = end - time.time()
            if left <= 0 or not self.sock:
                return None
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            except OSError:
                self.close()
                return None
            if not chunk:
                self.close()
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        try:
            return json.loads(line)
        except ValueError:
            return {}

    def connect(self):
        if self.sock:
            return True
        if not os.path.exists(self.path):
            return False
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.5)
            s.connect(self.path)
        except OSError:
            return False
        self.sock = s
        greet = self._read_msg(1.5)
        if not greet or "QMP" not in greet:
            self.close()
            return False
        if self.cmd("qmp_capabilities") is None:
            self.close()
            return False
        return True

    def cmd(self, name, **args):
        if not self.sock and not self.connect():
            return None
        msg = {"execute": name}
        if args:
            msg["arguments"] = args
        try:
            self.sock.sendall((json.dumps(msg) + "\n").encode())
        except OSError:
            self.close()
            return None
        while True:
            r = self._read_msg(10)
            if r is None:
                return None
            if "event" in r:
                self.events.append(r)
                continue
            if "return" in r or "error" in r:
                return r

    def ok(self, name, **args):
        r = self.cmd(name, **args)
        return r is not None and "return" in r

    def poll(self):
        if not self.sock:
            return
        try:
            self.sock.setblocking(False)
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    self.close()
                    break
                self.buf += chunk
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            self.close()
        if self.sock:
            self.sock.setblocking(True)
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if "event" in r:
                self.events.append(r)


class Session:
    """What is known about one running (or last run) VM: restarts, shutdown,
    health reports from the guest agent, graphics activity."""

    def __init__(self, vm, mode, log):
        self.disk = vm.get("DISK", "")
        self.mode = mode
        self.log = log
        self.qmp = QMP(qmp_sock(self.disk))
        self.t0 = time.time()
        self.started = False
        self.running = False
        self.paused = False
        self.resets = 0
        self.guest_shutdown = False
        self.health = {}             # from AERO-GUEST lines: agent, res, aero, audio, net, gpu
        self.agent_at = None         # time the agent first reported
        self.reset_after_agent = False
        self.driver_loaded = False   # adapter is no longer "Standard VGA"
        self.reset_after_driver = False
        self.draw = False            # the 3D driver drew through the Mac GPU
        self.res_requests = []
        self.res_ok = False          # Windows followed a window-size change
        self.log_pos = 0
        self.log_ino = None
        # Start reading the log from its current end for a fresh session, but
        # read it all when attaching to a VM that is already running.
        self.update()

    def update(self):
        self.running = vm_running(self.disk)
        if self.running:
            self.started = True
            self.qmp.connect()
        self.qmp.poll()
        for ev in self.qmp.events:
            e = ev.get("event")
            if e == "RESET":
                self.resets += 1
                if self.agent_at is not None:
                    self.reset_after_agent = True
                if self.driver_loaded:
                    self.reset_after_driver = True
            elif e == "SHUTDOWN":
                if ev.get("data", {}).get("guest"):
                    self.guest_shutdown = True
            elif e == "STOP":
                self.paused = True
            elif e == "RESUME":
                self.paused = False
        self.qmp.events = []
        self.read_log()
        return self.running

    def read_log(self):
        try:
            st = os.stat(self.log)
        except OSError:
            return
        if self.log_ino != st.st_ino or st.st_size < self.log_pos:
            self.log_ino, self.log_pos = st.st_ino, 0
        if st.st_size == self.log_pos:
            return
        try:
            with open(self.log, "rb") as f:
                f.seek(self.log_pos)
                data = f.read()
        except OSError:
            return
        self.log_pos += len(data)
        for line in data.decode("utf-8", "replace").splitlines():
            if line.startswith("AERO-GUEST "):
                key, _, value = line[len("AERO-GUEST "):].partition("=")
                key = key[len("aero."):] if key.startswith("aero.") else key
                self.health[key] = value.strip()
                if key == "agent" and self.agent_at is None:
                    self.agent_at = time.time()
                if key == "gpu" and self.health[key] and "standard vga" not in self.health[key].lower():
                    # The VMware driver (or its renamed GPU label) is running:
                    # this is the moment the VM window resizes itself.
                    self.driver_loaded = True
                if key == "res" and self.res_requests:
                    want = self.res_requests[-1]
                    if self.health[key] == want:
                        self.res_ok = True
            elif "VMVGA-DYNAMIC-RES request" in line:
                self.res_requests.append(line.split()[-1])
            elif line.startswith("VMVGA-PROFILE "):
                m = re.search(r"\bdraw9=(\d+)", line)
                if m and int(m.group(1)) > 0:
                    self.draw = True

    def elapsed(self):
        return int(time.time() - self.t0)

    def ctrl(self, name, **args):
        if not self.qmp.sock:
            self.qmp.connect()
        return self.qmp.ok(name, **args)

    def keys(self, *qcodes):
        return self.ctrl("send-key", keys=[{"type": "qcode", "data": k} for k in qcodes])


def health_text(h, key):
    """Human reading of one health value -> (ok|None, text)."""
    v = h.get(key)
    if key == "aero":
        if v is None:
            return None, "Aero: waiting for Windows"
        return (v == "on"), {"on": "Aero glass is on", "off": "Aero glass is OFF"}.get(v, "Aero: %s" % v)
    if key == "audio":
        if v is None:
            return None, "Sound: waiting for Windows"
        if v in ("none", "n/a"):
            return False, "Sound: no sound device in Windows"
        return True, "Sound: %s" % (v.split(" ", 1)[1] if " " in v else "device found")
    if key == "net":
        if v is None:
            return None, "Internet: checking"
        return (v == "ok"), {"ok": "Internet works", "no-dns": "Internet: no DNS (no network?)",
                             "timeout": "Internet: no answer", "no-connect": "Internet: cannot connect",
                             "n/a": "Internet: cannot check"}.get(v, "Internet: %s" % v)
    return None, "%s: %s" % (key, v)


# =====================================================================
# Drawing
# =====================================================================

C_DESK, C_MENU, C_MENU_HOT, C_TITLE, C_WIN, C_SEL, C_BTN, C_BTN_HOT, C_SHADOW, \
    C_DLG, C_DLG_TITLE, C_DLG_HOT, C_OK, C_TODO, C_BAD, C_INPUT, C_CURSOR, C_BAR, C_DLG_BOX, C_OK_DLG = range(1, 21)


def init_colors():
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    B, W, K, C, R, Y, G = (curses.COLOR_BLUE, curses.COLOR_WHITE, curses.COLOR_BLACK, curses.COLOR_CYAN,
                           curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_GREEN)
    pairs = {
        C_DESK: (W, B), C_MENU: (K, W), C_MENU_HOT: (R, W), C_TITLE: (W, K), C_WIN: (W, B),
        C_SEL: (W, K), C_BTN: (K, W), C_BTN_HOT: (R, W), C_SHADOW: (K, K), C_DLG: (K, C),
        C_DLG_TITLE: (K, W), C_DLG_HOT: (R, C), C_OK: (G, B), C_TODO: (Y, B), C_BAD: (R, B),
        C_INPUT: (W, K), C_CURSOR: (K, Y), C_BAR: (W, B), C_DLG_BOX: (R, C), C_OK_DLG: (G, C),
    }
    for n, (fg, bg) in pairs.items():
        curses.init_pair(n, fg, bg)


class UI:
    def __init__(self, scr):
        self.scr = scr
        self.mouse = None          # (y, x) of the text-mode mouse cursor
        self.hits = []             # [(y0, x0, y1, x1, id)] clickable areas
        self.last_click = (0.0, None)

    # -- primitives ---------------------------------------------------
    def put(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        if x < 0:
            text, x = text[-x:], 0
        text = text[: max(0, w - x - (1 if y == h - 1 else 0))]
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def fill(self, y, x, hgt, wid, attr, ch=" "):
        for r in range(hgt):
            self.put(y + r, x, ch * wid, attr)

    def box(self, y, x, hgt, wid, attr, double=False, title=None, title_attr=None):
        tl, tr, bl, br, hz, vt = ("╔", "╗", "╚", "╝", "═", "║") if double else ("┌", "┐", "└", "┘", "─", "│")
        self.put(y, x, tl + hz * (wid - 2) + tr, attr)
        for r in range(1, hgt - 1):
            self.put(y + r, x, vt, attr)
            self.put(y + r, x + wid - 1, vt, attr)
        self.put(y + hgt - 1, x, bl + hz * (wid - 2) + br, attr)
        if title:
            t = " %s " % title
            self.put(y, x + (wid - len(t)) // 2, t, title_attr if title_attr is not None else attr)

    def shadow(self, y, x, hgt, wid):
        self.fill(y + 1, x + wid, hgt, 2, curses.color_pair(C_SHADOW))
        self.fill(y + hgt, x + 2, 1, wid, curses.color_pair(C_SHADOW))

    def hot_text(self, y, x, label, attr, hot_attr):
        """'&Scan' draws S in the hotkey colour."""
        i = label.find("&")
        clean = label.replace("&", "")
        self.put(y, x, clean, attr)
        if i >= 0:
            self.put(y, x + i, clean[i], hot_attr)
        return clean

    def button(self, y, x, label, bid, focused=False, width=None):
        clean = label.replace("&", "")
        width = width or len(clean) + 4
        self.fill(y, x, 1, width, curses.color_pair(C_BTN))
        cx = x + (width - len(clean)) // 2
        self.hot_text(y, cx, label, curses.color_pair(C_BTN), curses.color_pair(C_BTN_HOT))
        if focused:
            self.put(y, x, "►", curses.color_pair(C_BTN) | curses.A_BOLD)
            self.put(y, x + width - 1, "◄", curses.color_pair(C_BTN) | curses.A_BOLD)
        self.put(y, x + width, "▄", curses.color_pair(C_SHADOW))
        self.put(y + 1, x + 1, "▀" * width, curses.color_pair(C_SHADOW))
        self.hits.append((y, x, y, x + width - 1, bid))

    def progress(self, y, x, width, pct, attr):
        self.put(y, x, "0%", attr)
        bar_w = width - 23          # "0% " + bar + " 100%  " + "100% Complete"
        filled = int(bar_w * max(0, min(100, pct)) / 100)
        self.put(y, x + 3, "█" * filled + "░" * (bar_w - filled), attr)
        self.put(y, x + 4 + bar_w, "100%", attr)
        self.put(y, x + 9 + bar_w, "%3d%% Complete" % pct, attr | curses.A_BOLD)

    def draw_cursor(self):
        if self.mouse is None:
            return
        y, x = self.mouse
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and 0 <= x < w - (1 if y == h - 1 else 0):
            try:
                self.scr.addstr(y, x, " ", curses.color_pair(C_CURSOR))
            except curses.error:
                pass

    def hit(self, y, x):
        for (y0, x0, y1, x1, hid) in reversed(self.hits):
            if y0 <= y <= y1 and x0 <= x <= x1:
                return hid
        return None

    # -- input ----------------------------------------------------------
    ESCAPES = {"[A": curses.KEY_UP, "[B": curses.KEY_DOWN, "[C": curses.KEY_RIGHT, "[D": curses.KEY_LEFT,
               "OA": curses.KEY_UP, "OB": curses.KEY_DOWN, "OC": curses.KEY_RIGHT, "OD": curses.KEY_LEFT,
               "[H": curses.KEY_HOME, "[F": curses.KEY_END, "OH": curses.KEY_HOME, "OF": curses.KEY_END,
               "[Z": curses.KEY_BTAB, "[21~": curses.KEY_F10, "[5~": curses.KEY_PPAGE, "[6~": curses.KEY_NPAGE,
               "[3~": curses.KEY_DC}

    def escape_sequence(self):
        """Decode arrow/function keys curses left undecoded (terminals that
        ignore keypad mode send ESC [ C instead of ESC O C). Lone Esc -> 27."""
        seq = ""
        self.scr.timeout(15)
        while len(seq) < 5:
            try:
                c = self.scr.get_wch()
            except curses.error:
                break
            if not isinstance(c, str):
                break
            seq += c
            if seq in self.ESCAPES:
                return self.ESCAPES[seq]
            if not (seq[0] in "[O"):
                break
        return 27

    def get_event(self, timeout_ms=250):
        """-> ('key', code) | ('click', id, double) | ('none',) | ('resize',)"""
        self.scr.timeout(timeout_ms)
        try:
            k = self.scr.get_wch()
        except curses.error:
            return ("none",)
        if isinstance(k, str) and len(k) == 1 and (ord(k) < 32 or ord(k) == 127):
            k = 10 if k == "\r" else ord(k)          # Tab 9, Enter 10, Esc 27, Backspace 127
        if k == 27:
            k = self.escape_sequence()
        if k == curses.KEY_RESIZE:
            return ("resize",)
        if k == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except curses.error:
                return ("none",)
            self.mouse = (my, mx)
            if bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED):
                hid = self.hit(my, mx)
                now = time.time()
                double = bool(bstate & curses.BUTTON1_DOUBLE_CLICKED) or (
                    hid is not None and self.last_click[1] == hid and now - self.last_click[0] < 0.45)
                self.last_click = (now, hid)
                return ("click", hid, double)
            if bstate & curses.BUTTON4_PRESSED:
                return ("key", curses.KEY_UP)
            if hasattr(curses, "BUTTON5_PRESSED") and bstate & curses.BUTTON5_PRESSED:
                return ("key", curses.KEY_DOWN)
            return ("move",)
        return ("key", k)


def centered(ui, hgt, wid):
    h, w = ui.scr.getmaxyx()
    return max(1, (h - hgt) // 2), max(0, (w - wid) // 2)


# =====================================================================
# Dialogs
# =====================================================================

def wrap(text, width):
    out = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            if len(line) + len(word) + (1 if line else 0) > width:
                out.append(line)
                line = word
            else:
                line = (line + " " + word) if line else word
        out.append(line)
    return out


class Form:
    """A modal dialog: text, input fields, radio groups, checkboxes, buttons.

    items: ("text", str) | ("input", key, label, default) |
           ("radio", key, [options], default_index) | ("check", key, label, default)
    Returns (button_label or None for Esc, values).
    """

    def __init__(self, ui, title, items, buttons, width=60, default=0, background=None):
        self.ui, self.title, self.items, self.buttons = ui, title, items, buttons
        self.width = min(width, ui.scr.getmaxyx()[1] - 4)
        self.values = {}
        self.background = background
        for it in items:
            if it[0] == "input":
                self.values[it[1]] = it[3]
            elif it[0] == "radio":
                self.values[it[1]] = it[3]
            elif it[0] == "check":
                self.values[it[1]] = it[3]
        # focus order: fields, then buttons
        self.focusables = [i for i, it in enumerate(items) if it[0] in ("input", "radio", "check")]
        self.focusables += [("btn", b) for b in range(len(buttons))]
        first_btn = len(self.focusables) - len(buttons)
        has_fields = any(it[0] in ("input", "radio", "check") for it in items)
        self.focus = 0 if has_fields else first_btn + default
        self.default_btn = default

    def lines(self):
        inner = self.width - 6
        rows = []
        for i, it in enumerate(self.items):
            if it[0] == "text":
                for ln in wrap(it[1], inner):
                    rows.append(("text", ln, i))
            elif it[0] == "input":
                rows.append(("label", it[2], i))
                rows.append(("input", it[1], i))
            elif it[0] == "radio":
                for oi, opt in enumerate(it[2]):
                    rows.append(("radio", (it[1], oi, opt), i))
            elif it[0] == "check":
                rows.append(("check", it, i))
        return rows

    def draw(self):
        ui = self.ui
        if self.background:
            self.background()
        rows = self.lines()
        hgt = len(rows) + 6
        y, x = centered(ui, hgt, self.width)
        dlg = curses.color_pair(C_DLG)
        ui.fill(y, x, hgt, self.width, dlg)
        ui.fill(y, x, 1, self.width, curses.color_pair(C_DLG_TITLE))
        ui.put(y, x + (self.width - len(self.title)) // 2, self.title, curses.color_pair(C_DLG_TITLE))
        ui.put(y, x + 1, "[■]", curses.color_pair(C_DLG_TITLE))
        ui.hits.append((y, x + 1, y, x + 3, ("btn", None)))
        ui.shadow(y, x, hgt, self.width)
        cursor_at = None
        r = y + 2
        for kind, data, idx in rows:
            focused = self.focusables and self.focusables[self.focus] == idx
            if kind == "text":
                ui.put(r, x + 3, data, dlg)
            elif kind == "label":
                ui.put(r, x + 3, data, dlg | curses.A_BOLD)
            elif kind == "input":
                val = self.values[data]
                fw = self.width - 6
                shown = val[-(fw - 1):]
                ui.fill(r, x + 3, 1, fw, curses.color_pair(C_INPUT))
                ui.put(r, x + 3, shown, curses.color_pair(C_INPUT))
                ui.hits.append((r, x + 3, r, x + 3 + fw, ("field", idx)))
                if focused:
                    cursor_at = (r, x + 3 + len(shown))
            elif kind == "radio":
                key, oi, opt = data
                mark = "(•)" if self.values[key] == oi else "( )"
                attr = dlg | (curses.A_BOLD if focused and self.values[key] == oi else 0)
                ui.put(r, x + 3, "%s %s" % (mark, opt), attr)
                ui.hits.append((r, x + 3, r, x + 7 + len(opt), ("radio", idx, oi)))
            elif kind == "check":
                it = data
                mark = "[X]" if self.values[it[1]] else "[ ]"
                ui.put(r, x + 3, "%s %s" % (mark, it[2]), dlg | (curses.A_BOLD if focused else 0))
                ui.hits.append((r, x + 3, r, x + 7 + len(it[2]), ("check", idx)))
            r += 1
        # buttons
        widths = [len(b.replace("&", "")) + 4 for b in self.buttons]
        total = sum(widths) + 3 * (len(widths) - 1)
        bx = x + (self.width - total) // 2
        by = y + hgt - 2
        for bi, b in enumerate(self.buttons):
            focused = self.focusables[self.focus] == ("btn", bi)
            ui.button(by, bx, b, ("btn", bi), focused, widths[bi])
            bx += widths[bi] + 3
        if cursor_at:
            curses.curs_set(1)
            ui.draw_cursor()
            ui.scr.move(*cursor_at)
        else:
            curses.curs_set(0)
            ui.draw_cursor()

    def run(self):
        ui = self.ui
        while True:
            ui.hits = []
            ui.scr.erase()
            self.draw()
            ui.scr.refresh()
            ev = ui.get_event(500)
            if ev[0] == "click":
                hid = ev[1]
                if hid is None:
                    continue
                if hid == ("btn", None):
                    curses.curs_set(0)
                    return None, self.values
                if hid[0] == "btn":
                    curses.curs_set(0)
                    return self.buttons[hid[1]].replace("&", ""), self.values
                if hid[0] == "field":
                    self.focus = self.focusables.index(hid[1])
                elif hid[0] == "radio":
                    self.values[self.items[hid[1]][1]] = hid[2]
                    self.focus = self.focusables.index(hid[1])
                elif hid[0] == "check":
                    key = self.items[hid[1]][1]
                    self.values[key] = not self.values[key]
                    self.focus = self.focusables.index(hid[1])
                continue
            if ev[0] != "key":
                continue
            k = ev[1]
            cur = self.focusables[self.focus] if self.focusables else None
            item = self.items[cur] if isinstance(cur, int) else None
            if k == 27:
                curses.curs_set(0)
                return None, self.values
            if k in (9, curses.KEY_DOWN) and not (item and item[0] == "radio" and k == curses.KEY_DOWN):
                self.focus = (self.focus + 1) % len(self.focusables)
                continue
            if k in (curses.KEY_BTAB, curses.KEY_UP) and not (item and item[0] == "radio" and k == curses.KEY_UP):
                self.focus = (self.focus - 1) % len(self.focusables)
                continue
            if isinstance(cur, tuple):          # a button
                if k in (curses.KEY_LEFT,):
                    self.focus = max(len(self.focusables) - len(self.buttons), self.focus - 1)
                elif k in (curses.KEY_RIGHT,):
                    self.focus = min(len(self.focusables) - 1, self.focus + 1)
                elif k in (10, 13, " ", curses.KEY_ENTER):
                    curses.curs_set(0)
                    return self.buttons[cur[1]].replace("&", ""), self.values
                elif isinstance(k, str):
                    for bi, b in enumerate(self.buttons):
                        i = b.find("&")
                        if i >= 0 and b[i + 1].lower() == k.lower():
                            curses.curs_set(0)
                            return b.replace("&", ""), self.values
                continue
            if item[0] == "input":
                key = item[1]
                if k in (10, 13, curses.KEY_ENTER):
                    curses.curs_set(0)
                    return self.buttons[self.default_btn].replace("&", ""), self.values
                if k in (curses.KEY_BACKSPACE, 127, 8):
                    self.values[key] = self.values[key][:-1]
                elif k == 21:                          # Ctrl-U clears
                    self.values[key] = ""
                elif isinstance(k, str) and k.isprintable():
                    self.values[key] += k
            elif isinstance(k, str) and item[0] != "input" and any(
                    b.find("&") >= 0 and b[b.find("&") + 1].lower() == k.lower() for b in self.buttons):
                b = next(b for b in self.buttons if b.find("&") >= 0 and b[b.find("&") + 1].lower() == k.lower())
                curses.curs_set(0)
                return b.replace("&", ""), self.values
            elif item[0] == "radio":
                key, n = item[1], len(item[2])
                if k in (curses.KEY_UP, curses.KEY_LEFT):
                    self.values[key] = (self.values[key] - 1) % n
                elif k in (curses.KEY_DOWN, curses.KEY_RIGHT):
                    self.values[key] = (self.values[key] + 1) % n
                elif k in (10, 13, curses.KEY_ENTER):
                    curses.curs_set(0)
                    return self.buttons[self.default_btn].replace("&", ""), self.values
            elif item[0] == "check":
                if k in (" ", "x", "X"):
                    self.values[item[1]] = not self.values[item[1]]
                elif k in (10, 13, curses.KEY_ENTER):
                    curses.curs_set(0)
                    return self.buttons[self.default_btn].replace("&", ""), self.values


def message(ui, title, text, buttons=("&OK",), default=0, width=62, background=None):
    b, _ = Form(ui, title, [("text", text)], list(buttons), width=width, default=default,
                background=background).run()
    return b



# =====================================================================
# The application
# =====================================================================

KEYS_MENU = [
    ("Ctrl+Alt+&Del", ("ctrl", "alt", "delete")),
    ("&Task Manager (Ctrl+Shift+Esc)", ("ctrl", "shift", "esc")),
    ("&Windows key (Start menu)", ("meta_l",)),
    ("&Alt+Tab", ("alt", "tab")),
    ("Alt+F&4 (close window)", ("alt", "f4")),
    ("&Print Screen", ("print",)),
]


class App:
    def __init__(self, ui):
        self.ui = ui
        self.sel = 0
        self.focus = 0               # 0 = list, 1.. = buttons
        self.menu_open = None
        self.menu_sel = 0
        self.status_msg = ""
        self.sessions = {}           # disk -> Session
        self.running = {}            # disk -> bool, refreshed each loop
        self.menus = [
            ("&VMs", [("&Continue setup", "continue"), ("&Start / open panel", "start"),
                      ("&New VM...", "new"), ("&Edit settings...", "edit"),
                      ("S&napshots...", "snapshots"), ("Show in &Finder", "finder")]),
            ("&Machine", [(label, "key:" + "+".join(k)) for label, k in KEYS_MENU] + [
                ("-", None),
                ("Insert &guest tools disc", "cd_tools"), ("Insert an &ISO...", "cd_iso"),
                ("&Eject disc", "cd_eject"), ("Scree&nshot to Desktop", "shot"),
                ("-", None),
                ("Pa&use / resume", "pause"), ("Shut d&own Windows", "powerdown"),
                ("&Restart (hard reset)...", "reset"), ("&Force stop...", "force")]),
            ("&Tools", [("&Set up / check tools", "tools"), ("&Rebuild guest tools disc", "rebuild"),
                        ("Use my own VMware &ISO...", "own_iso"), ("Re-&download everything...", "redownload"),
                        ("Open &logs folder", "logs"), ("&Plain text mode", "plain")]),
            ("&Remove", [("&Delete selected VM...", "delete"), ("Downloaded &cache...", "rm_cache"),
                         ("&Runtime...", "rm_runtime"), ("Guest tools &disc", "rm_disc"),
                         ("&Everything except VMs...", "rm_all")]),
            ("&Help", [("&Keys and mouse", "help"), ("&What each step does", "steps_help"),
                       ("&About", "about")]),
            ("Ctrl+Alt+Del", "key:ctrl+alt+delete"),      # a direct action on the bar
        ]
        self.refresh_state(check_tools=True)

    # -- state ------------------------------------------------------------
    def refresh_state(self, check_tools=False):
        self.vms = list_vms()
        if check_tools or not hasattr(self, "tools"):
            self.tools_ok, self.tools = tools_status()
        self.sel = max(0, min(self.sel, len(self.vms)))
        self.running = {vm.get("DISK", ""): vm_running(vm.get("DISK", "")) for vm in self.vms}
        try:
            st = os.statvfs(ROOT)
            self.free = st.f_bavail * st.f_frsize
        except OSError:
            self.free = 0

    def selected_vm(self):
        return self.vms[self.sel] if self.sel < len(self.vms) else None

    def is_running(self, vm):
        return bool(vm) and self.running.get(vm.get("DISK", ""), False)

    def session(self, vm, mode=None, fresh=False):
        """The live session for a VM, attaching to an already-running one."""
        disk = vm.get("DISK", "")
        s = self.sessions.get(disk)
        if s is None or fresh:
            if mode is None:
                logs = sorted(glob.glob(os.path.join(LOGS, vm_slug(vm) + "-*.log")), key=os.path.getmtime)
                log = logs[-1] if logs else os.path.join(LOGS, vm_slug(vm) + "-run.log")
                mode = re.sub(r"^.*-(\w+)\.log$", r"\1", log) if logs else "run"
            else:
                _, log = vm_env(vm, mode)
            s = Session(vm, mode, log)
            self.sessions[disk] = s
        return s

    def step_index(self, vm):
        """Which of the five steps a VM is on (0-based), and whether tools are ready."""
        if vm is None:
            return 0 if not self.tools_ok else 1
        stage = vm.get("STAGE", "new")
        return {"new": 1, "installed": 2, "check": 3, "ready": 4}.get(stage, 1)

    # -- main screen -------------------------------------------------------
    def buttons(self):
        vm = self.selected_vm()
        if vm is None:
            return [("&New VM", "new"), ("&Tools", "tools"), ("&Quit", "quit")]
        if self.is_running(vm):
            return [("&Open panel", "start"), ("Ctrl+Alt+&Del", "key:ctrl+alt+delete"),
                    ("&Shut down", "powerdown"), ("&Force stop", "force"), ("&Quit", "quit")]
        stage = vm.get("STAGE", "new")
        first = ("&Start", "start") if stage == "ready" else ("&Continue", "continue")
        return [first, ("&New VM", "new"), ("&Settings", "edit"), ("&Delete...", "delete"), ("&Quit", "quit")]

    def draw_menubar(self):
        ui = self.ui
        h, w = ui.scr.getmaxyx()
        ui.fill(0, 0, 1, w, curses.color_pair(C_TITLE))
        title = "Aero on Apple Silicon"
        ui.put(0, (w - len(title)) // 2, title, curses.color_pair(C_TITLE) | curses.A_BOLD)
        ui.fill(1, 0, 1, w, curses.color_pair(C_MENU))
        mx = 2
        self.menu_x = []
        for mi, (label, _) in enumerate(self.menus):
            direct = isinstance(self.menus[mi][1], str)
            if direct:                               # right-aligned action button
                mx = max(mx, w - len(label) - 4)
            open_ = self.menu_open == mi
            attr = curses.color_pair(C_SEL) if open_ else curses.color_pair(C_MENU)
            hot = curses.color_pair(C_SEL) | curses.A_BOLD if open_ else curses.color_pair(C_MENU_HOT)
            if direct:
                attr = curses.color_pair(C_BTN_HOT) | curses.A_BOLD | curses.A_REVERSE
                hot = attr
            clean = ui.hot_text(1, mx + 1, label, attr, hot)
            ui.put(1, mx, " ", attr)
            ui.put(1, mx + 1 + len(clean), " ", attr)
            ui.hits.append((1, mx, 1, mx + len(clean) + 1, ("menu", mi)))
            self.menu_x.append(mx)
            mx += len(clean) + 4

    def draw_main(self):
        ui = self.ui
        h, w = ui.scr.getmaxyx()
        ui.fill(0, 0, h, w, curses.color_pair(C_DESK), "░")
        self.draw_menubar()

        wy, wx, wh, ww = 3, 2, h - 6, w - 6
        ui.fill(wy, wx, wh, ww, curses.color_pair(C_WIN))
        ui.box(wy, wx, wh, ww, curses.color_pair(C_WIN) | curses.A_BOLD, double=True,
               title="Virtual Machines", title_attr=curses.color_pair(C_DLG_TITLE))
        ui.shadow(wy, wx, wh, ww)

        # VM list
        lx, ly, lw = wx + 3, wy + 2, ww - 26
        lh = max(3, min(len(self.vms) + 1, wh - 17))
        ui.box(ly, lx, lh + 2, lw, curses.color_pair(C_WIN))
        rows = []
        for vm in self.vms:
            name = vm.get("VM_NAME", "?")
            guest = "Vista" if vm.get("GUEST") == "vista" else "Win 7"
            stage = vm.get("STAGE", "new")
            flag = STAGE_TEXT.get(stage, stage)
            disk = vm.get("DISK", "")
            if self.is_running(vm):
                s = self.sessions.get(disk)
                flag = "PAUSED" if s and s.paused else "RUNNING"
            elif not os.path.isfile(disk) and stage != "new":
                flag = "disk missing!"
            rows.append(" ▣ %-20.20s %-6s %-14s %8s" % (name, guest, flag, human(path_size(disk))))
        rows.append(" ✚ Create a new VM...")
        top = max(0, self.sel - lh + 1)
        for i, text in enumerate(rows[top:top + lh]):
            ri = top + i
            attr = curses.color_pair(C_SEL) | curses.A_BOLD if ri == self.sel else curses.color_pair(C_WIN)
            ui.fill(ly + 1 + i, lx + 1, 1, lw - 2, attr)
            ui.put(ly + 1 + i, lx + 1, text[: lw - 2], attr)
            ui.hits.append((ly + 1 + i, lx + 1, ly + 1 + i, lx + lw - 2, ("row", ri)))

        # buttons
        bx = wx + ww - 19
        for bi, (label, act) in enumerate(self.buttons()):
            ui.button(wy + 3 + bi * 2, bx, label, ("act", act), self.focus == bi + 1, width=15)

        # steps for the selected VM
        vm = self.selected_vm()
        sy = ly + lh + 3
        cur = self.step_index(vm)
        head = "Steps for %s:" % vm.get("VM_NAME") if vm else "Steps:"
        ui.put(sy, lx, head, curses.color_pair(C_WIN) | curses.A_BOLD)
        for i, name in enumerate(STEP_NAMES):
            if i < cur or (i == 4 and cur == 4):
                mark, pair = "✓", C_OK
            elif i == cur:
                mark, pair = "►", C_TODO
            else:
                mark, pair = "·", C_WIN
            ui.put(sy + 1 + i, lx + 2, mark, curses.color_pair(pair) | curses.A_BOLD)
            ui.put(sy + 1 + i, lx + 4, "%d  %s" % (i + 1, name),
                   curses.color_pair(C_WIN) | (curses.A_BOLD if i == cur else 0))

        # right of the steps: tools, or live health of the selected VM
        hx = lx + 42
        if vm and self.is_running(vm):
            s = self.session(vm)
            s.update()
            ui.put(sy, hx, "Running now:", curses.color_pair(C_WIN) | curses.A_BOLD)
            lines = self.health_lines(s)
        else:
            ui.put(sy, hx, "Tools:", curses.color_pair(C_WIN) | curses.A_BOLD)
            lines = [(ok, t) for ok, t in self.tools]
        for i, (ok, text) in enumerate(lines[:6]):
            mark, pair = ("✓", C_OK) if ok else (("•", C_TODO) if ok is None else ("✗", C_BAD))
            ui.put(sy + 1 + i, hx + 2, mark, curses.color_pair(pair) | curses.A_BOLD)
            ui.put(sy + 1 + i, hx + 4, text[: ww - (hx - wx) - 24], curses.color_pair(C_WIN))
        ui.put(sy + 7, lx, "Free disk space: %s" % human(self.free),
               curses.color_pair(C_WIN) | (0 if self.free > 25 * GB else curses.A_BOLD))

        pct = (cur * 25) if vm else (25 if self.tools_ok else 0)
        ui.progress(wy + wh - 2, wx + 3, ww - 6, min(100, pct), curses.color_pair(C_BAR))

        ui.fill(h - 1, 0, 1, w, curses.color_pair(C_MENU))
        hint = self.status_msg or "F10 Menu   ↑↓ Choose   Enter Continue   Tab Buttons   Mouse works too   Q Quit"
        ui.put(h - 1, 1, hint[: w - 2], curses.color_pair(C_MENU))
        if self.menu_open is not None:
            self.draw_menu()

    def health_lines(self, s):
        out = []
        if s.paused:
            out.append((None, "Paused - Machine > Pause / resume"))
        if s.health.get("agent"):
            for key in ("aero", "audio", "net"):
                out.append(health_text(s.health, key))
            if s.res_ok:
                out.append((True, "Resizing follows the window"))
            elif s.health.get("res"):
                out.append((None, "Resolution %s" % s.health["res"]))
        else:
            out.append((None, "Windows is running (%d:%02d)" % divmod(s.elapsed(), 60)))
            out.append((None, "Restarts seen: %d" % s.resets))
            if s.draw:
                out.append((True, "3D graphics driver active"))
        return out

    def draw_menu(self):
        ui = self.ui
        label, entries = self.menus[self.menu_open]
        x = self.menu_x[self.menu_open]
        width = max(len(e[0]) for e in entries) + 4
        y = 2
        ui.fill(y, x, len(entries) + 2, width, curses.color_pair(C_MENU))
        ui.box(y, x, len(entries) + 2, width, curses.color_pair(C_MENU))
        ui.shadow(y, x, len(entries) + 2, width)
        for i, (text, act) in enumerate(entries):
            if text == "-":
                ui.put(y + 1 + i, x, "├" + "─" * (width - 2) + "┤", curses.color_pair(C_MENU))
                continue
            sel = i == self.menu_sel
            attr = curses.color_pair(C_SEL) if sel else curses.color_pair(C_MENU)
            hot = curses.color_pair(C_SEL) | curses.A_BOLD if sel else curses.color_pair(C_MENU_HOT)
            ui.fill(y + 1 + i, x + 1, 1, width - 2, attr)
            ui.hot_text(y + 1 + i, x + 2, text, attr, hot)
            ui.hits.append((y + 1 + i, x + 1, y + 1 + i, x + width - 2, ("item", act)))

    def redraw(self):
        self.ui.hits = []
        self.ui.scr.erase()
        self.draw_main()

    def bg(self):
        saved = self.ui.hits
        self.ui.hits = []
        mo = self.menu_open
        self.menu_open = None
        self.draw_main()
        self.menu_open = mo
        self.ui.hits = saved

    def msg(self, title, text, buttons=("&OK",), default=0, width=62):
        return message(self.ui, title, text, buttons, default, width, background=self.bg)

    def form(self, title, items, buttons, width=62, default=0):
        return Form(self.ui, title, items, list(buttons), width, default, background=self.bg).run()

    # -- main loop -----------------------------------------------------------
    def run(self, start_conf=None):
        ui = self.ui
        if start_conf:
            for i, vm in enumerate(self.vms):
                if os.path.realpath(vm["_conf"]) == os.path.realpath(start_conf):
                    self.sel = i
                    self.do("start")
                    self.refresh_state()
        last_refresh = 0.0
        while True:
            if time.time() - last_refresh > 3:
                self.refresh_state()
                last_refresh = time.time()
            self.redraw()
            curses.curs_set(0)
            ui.draw_cursor()
            ui.scr.refresh()
            ev = ui.get_event(1000)
            if ev[0] in ("none", "move"):
                continue
            if ev[0] == "resize":
                h, w = ui.scr.getmaxyx()
                if (h < MIN_H or w < MIN_W) and self.too_small() == "quit":
                    return 0
                continue
            action = None
            if ev[0] == "click":
                hid = ev[1]
                if self.menu_open is not None and (hid is None or hid[0] not in ("item", "menu")):
                    self.menu_open = None
                    continue
                if hid is None:
                    continue
                if hid[0] == "menu":
                    entries = self.menus[hid[1]][1]
                    if isinstance(entries, str):
                        self.menu_open = None
                        action = entries
                    else:
                        self.menu_open = None if self.menu_open == hid[1] else hid[1]
                        self.menu_sel = 0
                elif hid[0] == "item":
                    self.menu_open = None
                    action = hid[1]
                elif hid[0] == "row":
                    self.sel = hid[1]
                    self.focus = 0
                    if ev[2]:
                        action = self.default_action()
                elif hid[0] == "act":
                    action = hid[1]
            else:
                action = self.key(ev[1])
            if action == "quit":
                if any(self.running.values()) and self.msg(
                        "VMs still running", "A VM is still running. It keeps running after you quit; open "
                        "START HERE again to control it.", ("&Quit anyway", "&Stay")) != "Quit anyway":
                    continue
                return 0
            if action == "plain":
                return PLAIN
            if action:
                self.status_msg = ""
                self.do(action)
                self.refresh_state(check_tools=action in ("tools", "rebuild", "redownload", "own_iso",
                                                          "rm_cache", "rm_runtime", "rm_disc", "rm_all"))
                last_refresh = time.time()

    def default_action(self):
        vm = self.selected_vm()
        if vm is None:
            return "new"
        if self.is_running(vm) or vm.get("STAGE") == "ready":
            return "start"
        return "continue"

    def menu_entries(self):
        e = self.menus[self.menu_open][1]
        return e if isinstance(e, list) else []

    def key(self, k):
        if self.menu_open is not None:
            entries = self.menu_entries()
            n = len(entries)

            def step(d):
                i = self.menu_sel
                for _ in range(n):
                    i = (i + d) % n
                    if entries[i][0] != "-":
                        return i
                return self.menu_sel
            if k == curses.KEY_UP:
                self.menu_sel = step(-1)
            elif k == curses.KEY_DOWN:
                self.menu_sel = step(1)
            elif k in (curses.KEY_LEFT, curses.KEY_RIGHT):
                d = -1 if k == curses.KEY_LEFT else 1
                i = self.menu_open
                for _ in range(len(self.menus)):
                    i = (i + d) % len(self.menus)
                    if isinstance(self.menus[i][1], list):
                        break
                self.menu_open, self.menu_sel = i, 0
            elif k in (27, curses.KEY_F10):
                self.menu_open = None
            elif k in (10, 13, curses.KEY_ENTER):
                act = entries[self.menu_sel][1]
                self.menu_open = None
                return act
            elif isinstance(k, str):
                for text, act in entries:
                    i = text.find("&")
                    if i >= 0 and text[i + 1].lower() == k.lower():
                        self.menu_open = None
                        return act
            return None
        if k in (curses.KEY_F10, 27):
            self.menu_open, self.menu_sel = 0, 0
            return None
        if k == curses.KEY_UP:
            self.sel = max(0, self.sel - 1)
            self.focus = 0
        elif k == curses.KEY_DOWN:
            self.sel = min(len(self.vms), self.sel + 1)
            self.focus = 0
        elif k == 9:
            self.focus = (self.focus + 1) % (len(self.buttons()) + 1)
        elif k == curses.KEY_BTAB:
            self.focus = (self.focus - 1) % (len(self.buttons()) + 1)
        elif k in (10, 13, curses.KEY_ENTER):
            if self.focus == 0:
                return self.default_action()
            return self.buttons()[self.focus - 1][1]
        elif isinstance(k, str):
            if k.lower() == "q":
                return "quit"
            for label, act in self.buttons():
                i = label.find("&")
                if i >= 0 and label[i + 1].lower() == k.lower():
                    return act
            # The highlighted letter of a menu title opens that menu.
            for mi, (title, entries) in enumerate(self.menus):
                i = title.find("&")
                if i >= 0 and title[i + 1].lower() == k.lower():
                    if isinstance(entries, list):
                        self.menu_open, self.menu_sel = mi, 0
                        return None
                    return entries
        return None

    def too_small(self):
        ui = self.ui
        while True:
            h, w = ui.scr.getmaxyx()
            if h >= MIN_H and w >= MIN_W:
                return None
            ui.scr.erase()
            ui.put(0, 0, "Please make this window bigger (at least %dx%d, now %dx%d). Q quits." % (MIN_W, MIN_H, w, h))
            ui.scr.refresh()
            ev = ui.get_event(500)
            if ev[0] == "key" and ev[1] in ("q", "Q"):
                return "quit"

    # -- dispatch --------------------------------------------------------------
    MACHINE = ("key", "cd_tools", "cd_iso", "cd_eject", "shot", "pause", "powerdown", "reset", "force")

    def do(self, action):
        vm = self.selected_vm()
        base = action.split(":")[0]
        needs_vm = ("continue", "start", "delete", "finder", "edit", "snapshots") + self.MACHINE
        if base in needs_vm and vm is None:
            self.msg("No VM selected", "Select a VM in the list first, or create a new one.")
            return
        if base in self.MACHINE and not self.is_running(vm):
            self.msg("Not running", "%s is not running. Start it first (VMs > Start)." % vm.get("VM_NAME"))
            return
        # Tools are only needed to launch a VM, not to open the panel of a
        # VM that is already running.
        if base in ("continue", "start", "new") and not self.tools_ok and not (vm and self.is_running(vm)
                                                                              and base != "new"):
            if self.msg("Tools needed", "Some tools are missing. Set them up now? Only what is not "
                        "already on this Mac is downloaded.", ("&Set up", "&Cancel")) != "Set up":
                return
            if not self.setup_tools():
                return
        if base == "key":
            self.send_keys(vm, action.split(":", 1)[1].split("+"))
            return
        getattr(self, "act_" + base)()

    # -- machine actions (running VM) --------------------------------------------------
    def send_keys(self, vm, keys):
        s = self.session(vm)
        if s.keys(*keys):
            self.status_msg = "Sent %s to %s." % ("+".join(k.replace("meta_l", "Windows").title() for k in keys),
                                                  vm.get("VM_NAME"))
        else:
            self.msg("Could not send", "The VM did not answer. Is it still starting?")

    def act_cd_tools(self):
        if not os.path.isfile(GUEST_TOOLS_ISO):
            self.msg("No disc", "The guest tools disc is not built yet. Tools > Set up / check tools.")
            return
        self.insert_disc(GUEST_TOOLS_ISO)

    def act_cd_iso(self):
        b, v = self.form("Insert an ISO", [("text", "Drag an .iso file into this window, or type its path."),
                                          ("input", "p", "ISO:", "")], ("&Insert", "Cancel"), width=72)
        if b != "Insert":
            return
        p = clean_path(v["p"])
        if not (os.path.isfile(p) and p.lower().endswith(".iso")):
            self.msg("Not an ISO", "That is not an .iso file.")
            return
        self.insert_disc(p)

    def insert_disc(self, path):
        vm = self.selected_vm()
        s = self.session(vm)
        if s.ctrl("blockdev-change-medium", id="cdrom", filename=path, format="raw"):
            self.status_msg = "Inserted %s. Open Computer in Windows to see it." % os.path.basename(path)
        else:
            self.msg("Could not insert", "The VM did not accept the disc. Try ejecting first.")

    def act_cd_eject(self):
        s = self.session(self.selected_vm())
        self.status_msg = "Disc ejected." if s.ctrl("eject", id="cdrom", force=True) else "Could not eject."

    def act_shot(self):
        vm = self.selected_vm()
        s = self.session(vm)
        tmp = "/tmp/aero-shot-%d.ppm" % os.getpid()
        name = time.strftime("%s %Y-%m-%d at %H.%M.%S.png") % vm.get("VM_NAME", "VM")
        dest = os.path.join(os.path.expanduser("~/Desktop"), name)
        if not s.ctrl("screendump", filename=tmp):
            self.msg("Screenshot failed", "The VM did not answer.")
            return
        time.sleep(0.3)
        r = subprocess.run(["sips", "-s", "format", "png", tmp, "--out", dest], capture_output=True)
        try:
            os.remove(tmp)
        except OSError:
            pass
        self.status_msg = ("Screenshot saved to your Desktop: %s" % name) if r.returncode == 0 else "Screenshot failed."

    def act_pause(self):
        s = self.session(self.selected_vm())
        s.update()
        if s.paused:
            ok = s.ctrl("cont")
            self.status_msg = "Resumed." if ok else "Could not resume."
        else:
            ok = s.ctrl("stop")
            self.status_msg = "Paused. Machine > Pause / resume to continue." if ok else "Could not pause."
        s.update()

    def act_powerdown(self):
        s = self.session(self.selected_vm())
        if s.ctrl("system_powerdown"):
            self.status_msg = "Asked Windows to shut down (like pressing the power button)."
        else:
            self.msg("No answer", "The VM did not answer.")

    def act_reset(self):
        vm = self.selected_vm()
        if self.msg("Hard reset?", "Restart %s immediately, like pressing a reset button? Unsaved work in "
                    "Windows is lost. Prefer Start > Restart inside Windows." % vm.get("VM_NAME"),
                    ("&Cancel", "&Reset"), default=0) == "Reset":
            self.session(vm).ctrl("system_reset")

    def act_force(self):
        vm = self.selected_vm()
        if self.msg("Force stop?", "Turn %s off immediately, like pulling the plug? Unsaved work is lost, and "
                    "doing this during an install ruins the install. Prefer Shut down." % vm.get("VM_NAME"),
                    ("&Cancel", "&Force stop"), default=0) == "Force stop":
            s = self.session(vm)
            if not s.ctrl("quit"):
                subprocess.run(["pkill", "-f", "--", "[q]emu-system.*file=" + vm.get("DISK", "")])
            self.status_msg = "%s was stopped." % vm.get("VM_NAME")

    # -- VM actions ------------------------------------------------------------------
    def act_continue(self):
        self.continue_vm(self.selected_vm())

    def act_start(self):
        vm = self.selected_vm()
        if self.is_running(vm):
            self.monitor(vm, self.session(vm).mode)
            return
        stage = vm.get("STAGE", "new")
        if stage != "ready":
            b = self.msg("Not set up yet", "%s is not fully set up (%s). Continue its setup instead?"
                         % (vm.get("VM_NAME"), STAGE_TEXT.get(stage, stage)),
                         ("&Continue setup", "&Start anyway", "Cancel"))
            if b == "Continue setup":
                self.continue_vm(vm)
            if b != "Start anyway":
                return
        if not self.preflight(vm):
            return
        self.start_and_watch(vm, "run")

    def preflight(self, vm):
        """Checks before any VM start."""
        disk = vm.get("DISK", "")
        if self.free < 5 * GB and self.msg("Low disk space", "Only %s free on this Mac. Windows may fail or "
                                          "corrupt its disk when the Mac runs out of space. Start anyway?"
                                          % human(self.free), ("&Cancel", "&Start anyway")) != "Start anyway":
            return False
        others = [v.get("VM_NAME") for v in self.vms if v is not vm and self.is_running(v)]
        if others and self.msg("Another VM is running", "%s is already running. Two emulated PCs at once are "
                               "both very slow. Start anyway?" % ", ".join(others),
                               ("&Cancel", "&Start anyway")) != "Start anyway":
            return False
        if vm.get("STAGE", "new") != "new" and not os.path.isfile(disk):
            self.msg("Disk missing", "This VM's disk file is missing:\n%s" % disk)
            return False
        return True

    def start_and_watch(self, vm, mode):
        launch_vm(vm, mode)
        s = self.session(vm, mode, fresh=True)
        return self.monitor(vm, mode, s, launched=True)

    # -- the live panel ---------------------------------------------------------------
    def steps_for(self, vm, s):
        """-> (title, [(label, state)], instruction). state: done / now / todo / fail"""
        used = path_size(vm.get("DISK", ""))
        mode = s.mode
        h = s.health
        if mode == "install":
            done = [used > GB or s.resets > 0, s.resets >= 1, s.resets >= 2, s.guest_shutdown, s.guest_shutdown]
            labels = ["Start the installer: language, Install now, Custom, pick the disk",
                      "Windows copies and expands files (the longest part)",
                      "It restarts and completes the installation by itself",
                      "Set up Windows: user name, password, time zone",
                      "At the desktop: Start > Shut down"]
            texts = ["In the VM window: pick your language, click Install now, accept the licence, choose "
                     "Custom (advanced), select the unallocated disk and click Next.",
                     "Nothing to do - Windows is copying files. This takes a while under emulation "
                     "(often 20-40 minutes). Leave both windows open.",
                     "Nothing to do - Windows restarts on its own, maybe more than once. Do not press "
                     "any key at 'Press any key to boot from CD'.",
                     "Answer the Set Up Windows questions: user name, password (optional), product key "
                     "(you can skip it), updates, time zone, network (choose Home).",
                     "When you see the Windows desktop, click Start and then Shut down. This panel "
                     "moves on by itself when Windows has shut down."]
            title = "Step 2 of 5: Install Windows"
        elif mode == "setup":
            agent = s.agent_at is not None
            driver = s.driver_loaded or s.draw
            aero = h.get("aero") == "on"
            done = [s.elapsed() > 45 or agent or s.resets > 0,
                    agent,
                    driver,
                    s.reset_after_driver or aero,
                    aero,
                    s.guest_shutdown]
            labels = ["Windows starts",
                      "Open Computer > AEROTOOLS disc > double-click SETUP.CMD",
                      "The graphics driver loads - the VM window resizes itself",
                      "Run WinSAT (SETUP offers it), then Start > Restart",
                      "After the restart: Aero glass is on",
                      "Then Start > Shut down"]
            texts = ["Wait for the Windows desktop.",
                     "In Windows: click Start > Computer, double-click the AEROTOOLS CD drive, then "
                     "double-click SETUP.CMD and click Yes. It installs the display driver, clipboard "
                     "sharing and the helper that reports back to this screen.",
                     "Let SETUP.CMD finish. When the driver starts, the VM window resizes itself. If it has "
                     "not after SETUP says Finished, restart Windows once (Start > Restart).",
                     "The driver is running. Windows only turns Aero on after it has rated the new driver: "
                     "answer Y when SETUP offers WinSAT (or Control Panel > Performance Information and "
                     "Tools > Rate this computer). Then click Start > Restart.",
                     "Wait for Windows to come back up. If the glass is still off, right-click the desktop > "
                     "Personalize and pick 'Windows 7' under Aero Themes.",
                     "Click Start and then Shut down. The next step checks sound, internet and resizing."]
            title = "Step 3 of 5: Graphics driver, clipboard and tools"
        elif mode == "check":
            agent = s.agent_at is not None
            done = [agent, h.get("aero") == "on", h.get("audio", "none") not in ("none", "n/a"),
                    h.get("net") == "ok", s.res_ok]
            labels = ["Windows starts and the helper reports in",
                      "Aero glass is on",
                      "Sound device works",
                      "Internet works",
                      "Resizing: drag a corner of the VM window"]
            texts = ["Wait for the Windows desktop. The helper installed by SETUP.CMD reports sound, "
                     "internet and graphics to this screen.",
                     "Aero is off. Right-click the desktop > Personalize > pick 'Windows 7' under Aero Themes.",
                     "Windows reports no sound device. Try VMs > Edit settings > Audio: the other option.",
                     "Waiting for internet. If it stays red, try VMs > Edit settings > Network: the other card.",
                     "Drag a corner of the VM window to a new size. Windows should switch to the same size "
                     "within a few seconds."]
            title = "Step 4 of 5: Check sound, internet and resizing"
        else:
            done, labels, texts = [], [], []
            title = "%s is running" % vm.get("VM_NAME")
        steps, instruction, seen_now = [], "", False
        for i, lab in enumerate(labels):
            if done[i]:
                steps.append((lab, "done"))
            elif not seen_now:
                steps.append((lab, "now"))
                instruction = texts[i]
                seen_now = True
            else:
                steps.append((lab, "todo"))
        if mode == "check" and all(done):
            instruction = ("Everything checks out. Try copying text on your Mac and pasting it into Notepad "
                           "to see clipboard sharing. Keep using Windows, or shut it down.")
        if mode == "run":
            instruction = ("Use Windows normally. Shut down from Windows' Start menu when you are done; "
                           "Machine menu has Ctrl+Alt+Del, discs, screenshots and more.")
        return title, steps, instruction

    def monitor(self, vm, mode, s=None, launched=False):
        """Live panel while the VM runs. -> 'closed' | 'back' | 'early'
        launched: this panel just started the VM (only then can an early exit
        mean it failed to start)."""
        ui = self.ui
        s = s or self.session(vm, mode)
        s.mode = mode
        name = vm.get("VM_NAME", "VM")
        t_launch = time.time()
        spin = "|/-\\"
        tick = 0
        buttons = [("Ctrl+Alt+&Del", "cad"), ("&Shut down", "down"), ("&Restart", "reset"),
                   ("&Force stop", "force"), ("&Log", "log"), ("&Back", "back")]
        buttons.insert(4, ("&Graphs", "graphs"))
        focus = 6
        stats = Stats(vm.get("DISK", ""), s.log, int(re.sub(r"\D", "", str(vm.get("MEM", "0"))) or 0))
        show_log = "graphs" if mode == "run" else "steps"   # which view fills the top of the panel
        try:
            return self._monitor_loop(vm, mode, s, launched, name, t_launch, spin, tick, buttons, focus,
                                      show_log, stats)
        finally:
            stats.stop()

    def _gauge_values(self, stats):
        n = stats.now
        mem_pct = min(100.0, n["mem"] * 100.0 / (stats.mem_mb * 1048576.0)) if stats.mem_mb and n["mem"] else 0.0
        return [("VM CPU", n["cpu"], "%.0f%%" % n["cpu"]),
                ("VM GPU", n["vgpu"], "%.0f%%" % n["vgpu"]),
                ("Mac GPU", n["gpu"], "%.0f%%" % n["gpu"]),
                ("Network", n["net"] * 100.0 / stats.net_peak, rate_text(n["net"])),
                ("Memory", mem_pct, human(n["mem"]) if n["mem"] else "-")]

    def _stats_line(self, stats):
        return "   ".join("%s %s" % (lab, txt) for lab, _, txt in self._gauge_values(stats))

    def _gauges(self, stats, y, x, dw):
        """DOOM-installer style meters, standing upright: a double-lined panel
        each, a 0-100 ruler and a bar that fills from the bottom. -> height"""
        ui = self.ui
        panel = curses.color_pair(C_DLG)
        vals = self._gauge_values(stats)
        gap = 2
        gw = min(17, (dw - 6 - gap * (len(vals) - 1)) // len(vals))
        gh = 13
        x0 = x + (dw - (gw * len(vals) + gap * (len(vals) - 1))) // 2
        levels = 9                                   # rows of bar; labels every other row
        for gi, (lab, pct, txt) in enumerate(vals):
            gx = x0 + gi * (gw + gap)
            pct = max(0.0, min(100.0, pct))
            ui.fill(y, gx, gh, gw, panel)
            ui.put(y, gx, "╔" + "═" * (gw - 2) + "╗", panel)
            for r in range(1, gh - 1):
                ui.put(y + r, gx, "║", panel)
                ui.put(y + r, gx + gw - 1, "║", panel)
            ui.put(y + gh - 1, gx, "╚" + "═" * (gw - 2) + "╝", panel)
            ui.put(y + 1, gx + (gw - len(txt)) // 2, txt, panel | curses.A_BOLD)
            bar_w = gw - 9
            filled = pct / 100.0 * levels                # in rows, from the bottom
            for i in range(levels):
                row = y + 2 + i
                lvl = levels - 1 - i                      # 0 = bottom row
                if i % 2 == 0:
                    ui.put(row, gx + 2, "%3d" % (100 - i * 12.5), panel)
                    ui.put(row, gx + 5, "┤", panel)
                else:
                    ui.put(row, gx + 5, "│", panel)
                part = filled - lvl
                if part >= 1:
                    cell = "█" * bar_w
                elif part > 0:
                    cell = SPARK[min(7, int(part * 8))] * bar_w
                else:
                    cell = "·" * bar_w if i % 2 == 0 else " " * bar_w
                ui.put(row, gx + 7, cell, panel | (curses.A_BOLD if part > 0 else 0))
            name = "% " + lab
            ui.put(y + gh - 2, gx + (gw - len(name)) // 2, name, panel)
            ui.shadow(y, gx, gh, gw)
        return gh

    def _log_rows(self, s, height):
        try:
            with open(s.log, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 65536))
                lines = f.read().decode("utf-8", "replace").splitlines()[1:]
        except OSError:
            return ["(no log yet)"]
        keep = [l for l in lines if not l.startswith(("VMVGA-PROFILE", "vmport-rpc"))]
        return keep[-height:] or ["(nothing new)"]

    def _monitor_loop(self, vm, mode, s, launched, name, t_launch, spin, tick, buttons, focus, show_log, stats):
        ui = self.ui
        while True:
            running = s.update()
            if not running and (s.started or time.time() - t_launch > 25):
                break
            # draw
            ui.hits = []
            ui.scr.erase()
            self.bg()
            h, w = ui.scr.getmaxyx()
            dw, dh = min(96, w - 4), min(28, h - 3)
            y, x = centered(ui, dh, dw)
            dlg = curses.color_pair(C_DLG)
            ui.fill(y, x, dh, dw, dlg)
            title, steps, instruction = self.steps_for(vm, s)
            ui.fill(y, x, 1, dw, curses.color_pair(C_DLG_TITLE))
            ui.put(y, x + (dw - len(title)) // 2, title, curses.color_pair(C_DLG_TITLE))
            ui.shadow(y, x, dh, dw)
            r = y + 2
            if show_log == "graphs":
                steps = []
                r += self._gauges(stats, r, x, dw) + 1
            elif show_log == "log":
                steps = []
                lh = dh - 15
                ui.box(r, x + 2, lh + 2, dw - 4, curses.color_pair(C_DLG_BOX), title="Live log (L to go back)",
                       title_attr=curses.color_pair(C_DLG_BOX) | curses.A_BOLD)
                for i, ln in enumerate(self._log_rows(s, lh)):
                    ui.put(r + 1 + i, x + 4, ln[: dw - 8], dlg)
                r += lh + 3
            for lab, state in steps:
                mark, attr = {"done": ("✓", curses.color_pair(C_OK_DLG) | curses.A_BOLD),
                              "now": ("►", dlg | curses.A_BOLD),
                              "todo": ("·", dlg)}[state]
                ui.put(r, x + 3, mark, attr)
                ui.put(r, x + 5, lab[: dw - 8], dlg | (curses.A_BOLD if state == "now" else 0))
                r += 1
            if steps:
                r += 1
            if show_log == "steps":
                ui.box(r, x + 2, 5, dw - 4, curses.color_pair(C_DLG_BOX), title="What to do now",
                   title_attr=curses.color_pair(C_DLG_BOX) | curses.A_BOLD)
                for i, ln in enumerate(wrap(instruction, dw - 10)[:3]):
                    ui.put(r + 1 + i, x + 4, ln, dlg | curses.A_BOLD)
                r += 6
            el = s.elapsed()
            info = "%s  %s   Time %d:%02d   Disk %s   Restarts %d" % (
                spin[tick % 4], "PAUSED" if s.paused else ("running" if running else "starting"),
                el // 60, el % 60, human(path_size(vm.get("DISK", ""))), s.resets)
            ui.put(r, x + 3, info[: dw - 6], dlg)
            hl = self.health_lines(s) if s.health.get("agent") else []
            if hl:
                txt = "   ".join(("✓ " if ok else ("✗ " if ok is False else "• ")) + t for ok, t in hl)
                ui.put(r + 1, x + 3, txt[: dw - 6], dlg)
            if show_log != "graphs":
                ui.put(y + dh - 5, x + 3, self._stats_line(stats)[: dw - 6], dlg | curses.A_BOLD)
            ui.put(y + dh - 3, x + 3, ("Keep this open; shut down from Windows' Start menu. Tip: a smaller VM window "
                                       "hangs less.")[: dw - 6], dlg)
            bx = x + 3
            for bi, (lab, bid) in enumerate(buttons):
                wid = len(lab.replace("&", "")) + 4
                ui.button(y + dh - 1 - 0, bx, lab, ("mon", bid), focus == bi, wid)
                bx += wid + 2
            ui.draw_cursor()
            ui.scr.refresh()
            tick += 1
            ev = ui.get_event(1000)
            act = None
            if ev[0] == "click" and ev[1] and ev[1][0] == "mon":
                act = ev[1][1]
            elif ev[0] == "key":
                k = ev[1]
                if k == 9:
                    focus = (focus + 1) % len(buttons)
                elif k == curses.KEY_BTAB:
                    focus = (focus - 1) % len(buttons)
                elif k in (10, 13, curses.KEY_ENTER):
                    act = buttons[focus][1]
                elif k == 27:
                    act = "back"
                elif isinstance(k, str):
                    for lab, bid in buttons:
                        i = lab.find("&")
                        if i >= 0 and lab[i + 1].lower() == k.lower():
                            act = bid
                    if act is None:
                        # A menu title's letter (Machine, Tools...): leave this panel
                        # with that menu open. The VM keeps running.
                        for mi, (title, entries) in enumerate(self.menus):
                            i = title.find("&")
                            if i >= 0 and title[i + 1].lower() == k.lower() and isinstance(entries, list):
                                self.menu_open, self.menu_sel = mi, 0
                                act = "back"
            if act == "back":
                self.status_msg = "%s keeps running. Select it and press Open panel to come back." % name
                return "back"
            if act in ("log", "graphs"):
                show_log = "steps" if show_log == act else act
            elif act == "reset":
                if self.msg("Restart?", "Restart %s right now, like pressing a PC's reset button? Unsaved work in "
                            "Windows is lost. (A normal restart is Start > Restart in Windows.)" % name,
                            ("&Cancel", "&Restart"), default=0) == "Restart":
                    s.ctrl("system_reset")
            elif act == "cad":
                s.keys("ctrl", "alt", "delete")
            elif act == "down":
                s.ctrl("system_powerdown")
            elif act == "force":
                if self.msg("Force stop?", "Turn %s off immediately, like pulling the plug? During an install "
                            "this ruins the install." % name, ("&Cancel", "&Force stop"), default=0) == "Force stop":
                    if not s.ctrl("quit"):
                        subprocess.run(["pkill", "-f", "--", "[q]emu-system.*file=" + vm.get("DISK", "")])
            # stage progress that does not need the VM to stop
            if mode == "check" and s.agent_at and s.health.get("aero") == "on" and s.res_ok and \
                    s.health.get("net") == "ok" and s.health.get("audio", "none") not in ("none", "n/a"):
                if conf_read(vm["_conf"]).get("STAGE") == "check":
                    conf_set(vm["_conf"], "STAGE", "ready")
                    make_launcher(dict(conf_read(vm["_conf"]), _conf=vm["_conf"]))
        s.update()
        if launched and (not s.started or time.time() - t_launch < 45):
            tail = ""
            try:
                with open(s.log, errors="replace") as f:
                    tail = "\n".join(l for l in f.read().splitlines()[-8:] if not l.startswith("VMVGA-PROFILE"))
            except OSError:
                pass
            if not tail.strip():
                try:
                    with open(s.log + ".launch", errors="replace") as f:
                        tail = "\n".join(l for l in f.read().splitlines()
                                         if l.strip() and not l.startswith("Press Return"))[-1500:]
                except OSError:
                    pass
            self.msg("The VM did not start", "It closed almost immediately. The end of its log:\n\n" + tail, width=78)
            return "early"
        return "closed"

    # -- setup stages ---------------------------------------------------------------
    def continue_vm(self, vm):
        conf, disk, name = vm["_conf"], vm.get("DISK", ""), vm.get("VM_NAME", "VM")
        if self.is_running(vm):
            self.monitor(vm, self.session(vm).mode)
            return
        stage = vm.get("STAGE", "new")
        if not os.path.isfile(disk) and stage not in ("new", ""):
            b, v = self.form("Disk missing", [
                ("text", "%s's disk file is gone:\n%s" % (name, disk)),
                ("radio", "c", ["I moved it - let me point to it", "Start over (install Windows again)",
                                "Remove it from the list"], 0)], ("&OK", "Cancel"), width=70)
            if b != "OK":
                return
            if v["c"] == 0:
                b2, v2 = self.form("Where is the disk?", [("text", "Drag the .qcow2 file here."),
                                                          ("input", "p", "File:", "")], ("&Use it", "Cancel"), width=70)
                p = clean_path(v2["p"]) if b2 == "Use it" else ""
                if not (p.endswith(".qcow2") and os.path.isfile(p)):
                    self.msg("Not a disk", "That is not a .qcow2 disk file.")
                    return
                conf_set(conf, "DISK", p)
            elif v["c"] == 1:
                conf_set(conf, "STAGE", "new")
            else:
                if self.msg("Remove?", "Remove %s from the list?" % name, ("&Cancel", "&Remove")) == "Remove":
                    safe_rm(conf)
                return
        while True:
            vm = conf_read(conf)
            vm["_conf"] = conf
            stage = vm.get("STAGE", "new")
            if stage in ("new", ""):
                if not self.stage_install(vm):
                    return
            elif stage == "installed":
                if not self.stage_drivers(vm):
                    return
            elif stage == "check":
                if not self.stage_check(vm):
                    return
            elif stage == "ready":
                r = make_launcher(vm)
                note = {"created": "A launcher is now on your Desktop: %s.command - it opens this screen "
                                   "and starts the VM." % name,
                        "already": "Its Desktop launcher is %s.command." % name,
                        "taken": "(A different %s.command is on your Desktop, so no launcher was made.)" % name}[r]
                b = self.msg("%s is ready" % name, note, ("&Start it", "Driver setup &again", "&Close"))
                if b == "Start it":
                    self.act_start()
                elif b == "Driver setup again":
                    conf_set(conf, "STAGE", "installed")
                    continue
                return
            else:
                conf_set(conf, "STAGE", "new")

    def choose_iso(self, vm):
        label = "Windows Vista" if vm.get("GUEST") == "vista" else "Windows 7"
        prev = vm.get("ISO", "")
        if prev and os.path.isfile(prev):
            b, v = self.form("Windows installer", [("text", "Last time you used:\n%s" % prev),
                                                   ("radio", "c", ["Use it again", "Use a different ISO"], 0)],
                             ("&OK", "Cancel"), width=70)
            if b != "OK":
                return False
            if v["c"] == 0:
                return True
        err = "The ISO used last time is gone (an unplugged drive?)." if prev and not os.path.isfile(prev) else ""
        while True:
            items = [("text", "Drag your %s installation ISO into this window." % label)]
            if err:
                items.append(("text", "! " + err))
            items.append(("input", "p", "ISO:", ""))
            b, v = self.form("Windows installer", items, ("&OK", "Cancel"), width=72)
            if b != "OK":
                return False
            f = clean_path(v["p"])
            if not os.path.isfile(f):
                err = "Not a file: %s" % f
                continue
            if not f.lower().endswith(".iso"):
                err = "That is not an .iso file."
                continue
            if os.path.basename(f) in (VMTOOLS_FILE, "guest-tools.iso"):
                err = "That is the driver disc, not Windows."
                continue
            size = os.path.getsize(f)
            if size < 1500 * 1024 * 1024:
                err = "That file is only %s - Windows installers are 2.5 GB or more." % human(size)
                continue
            low = os.path.basename(f).lower()
            other = ("vista" in low and label == "Windows 7") or \
                (re.search(r"win(dows)?[ _.-]*7", low) and label == "Windows Vista")
            if other and self.msg("Check the version", "The file name suggests the other Windows version than "
                                  "this VM (%s). Use it anyway?" % label, ("&No", "&Yes")) != "Yes":
                continue
            conf_set(vm["_conf"], "ISO", f)
            vm["ISO"] = f
            return True

    def stage_install(self, vm):
        conf, disk, name = vm["_conf"], vm.get("DISK", ""), vm.get("VM_NAME", "VM")
        want = int(re.sub(r"\D", "", vm.get("DISK_SIZE", "40")) or 40)
        if want < MIN_DISK_GB:
            b = self.msg("Disk too small", "%s is set to a %d GB disk. Windows 7 with updates needs more than "
                         "20 GB plus its page file, and a too-small disk makes Setup fail with \"The computer "
                         "restarted unexpectedly\". Use 40 GB instead? (It only uses what it needs.)" % (name, want),
                         ("&Use 40 GB", "&Keep %d GB" % want))
            if b == "Use 40 GB":
                conf_set(conf, "DISK_SIZE", "40G")
                vm["DISK_SIZE"] = "40G"
                if os.path.isfile(disk) and path_size(disk) < 3 * GB:
                    safe_rm(disk)
        if os.path.isfile(disk):
            if disk_virtual(disk) < GB:
                safe_rm(disk)
            elif path_size(disk) > 3 * GB:
                b, v = self.form("Install Windows", [
                    ("text", "%s's disk already holds %s - Windows may already be installed." % (name, human(path_size(disk)))),
                    ("radio", "c", ["Windows is installed - move on to the drivers",
                                    "Boot the installer again (continue or repair)",
                                    "Erase this disk and install from scratch"], 0)], ("&OK", "Cancel"), width=70)
                if b != "OK":
                    return False
                if v["c"] == 0:
                    conf_set(conf, "STAGE", "installed")
                    return True
                if v["c"] == 2:
                    if self.msg("Erase?", "Really erase %s's disk? Everything on it is lost." % name,
                                ("&Cancel", "&Erase"), default=0) != "Erase":
                        return False
                    safe_rm(disk)
        if not self.choose_iso(vm):
            return False
        if self.free < 25 * GB and self.msg("Low disk space", "Only %s is free on this Mac; a Windows install "
                                           "needs 15-25 GB. Continue anyway?" % human(self.free),
                                           ("&Cancel", "&Continue")) != "Continue":
            return False
        if self.msg("Step 2 of 5: Install Windows",
                    "A VM window opens with the Windows installer. This screen shows each step and what to do.\n\n"
                    "- Keep THIS screen open until Windows has shut down.\n"
                    "- The VM window's red close button is off on purpose. Always shut Windows down from its "
                    "Start menu.\n"
                    "- Click inside the VM window to use it; press Ctrl+Alt+G to get the mouse back.\n"
                    "- Your Mac is kept awake while the install runs.",
                    ("&Start installer", "Cancel"), width=72) != "Start installer":
            return False
        if not self.preflight(vm):
            return False
        r = self.start_and_watch(dict(conf_read(conf), _conf=conf), "install")
        if r == "back":
            return False
        if r != "closed":
            return False
        s = self.sessions.get(disk)
        used = path_size(disk)
        clean = s is not None and s.guest_shutdown
        if used < 3 * GB:
            if self.msg("Installed?", "The disk only holds %s, which is too little for an installed Windows. "
                        "Did Windows finish installing anyway?" % human(used), ("&No", "&Yes")) != "Yes":
                return False
        elif not clean:
            if self.msg("Installed?", "The VM stopped without Windows shutting itself down. The disk holds %s. "
                        "Did Windows finish installing and reach the desktop?" % human(used),
                        ("&No", "&Yes")) != "Yes":
                return False
        elif self.msg("Installed?", "Windows shut down. The disk now holds %s. Did Windows reach the desktop?"
                      % human(used), ("&Yes", "&No")) != "Yes":
            return False
        conf_set(conf, "STAGE", "installed")
        return True

    def stage_drivers(self, vm):
        conf, name = vm["_conf"], vm.get("VM_NAME", "VM")
        if self.msg("Step 3 of 5: Graphics driver, clipboard and tools",
                    "Windows starts with a disc called AEROTOOLS in its CD drive. This screen tells you what to "
                    "do and ticks each step off as it happens.\n\n"
                    "In short: open the disc, run SETUP.CMD, restart Windows once, then shut it down.\n"
                    "It installs the display driver, clipboard sharing, sound, and a small helper that reports "
                    "back here. It does NOT install VMware Tools.",
                    ("&Start VM", "Cancel"), width=72) != "Start VM":
            return False
        if not self.preflight(vm):
            return False
        r = self.start_and_watch(vm, "setup")
        if r != "closed":
            return False
        s = self.sessions.get(vm.get("DISK", ""))
        if s and s.agent_at is not None:
            conf_set(conf, "STAGE", "check")
            if s.health.get("aero") == "on":
                self.msg("Step 3 done", "SETUP.CMD ran, the graphics driver is active and Aero is on.")
            elif s.driver_loaded or s.draw:
                self.msg("Step 3 done", "SETUP.CMD ran and the graphics driver is active, but Aero was not on "
                         "yet. If you have not run WinSAT: Control Panel > Performance Information and Tools > "
                         "Rate this computer, then restart. The next step checks Aero again.", width=70)
            else:
                self.msg("Step 3 done", "SETUP.CMD ran. The graphics driver loads once Windows restarts; the next "
                         "step checks it.")
            return True
        b = self.msg("SETUP.CMD", "The helper from SETUP.CMD never reported in, so it looks like SETUP.CMD did not "
                     "run (or the disc was not opened). What happened?",
                     ("&Run this step again", "It &did run", "&Stop here"))
        if b == "It did run":
            conf_set(conf, "STAGE", "check")
            return True
        return b == "Run this step again"

    def stage_check(self, vm):
        conf, name = vm["_conf"], vm.get("VM_NAME", "VM")
        b = self.msg("Step 4 of 5: Check sound, internet and resizing",
                    "Windows starts normally. The helper inside Windows reports what works, and this screen ticks "
                    "it off: Aero glass, sound, internet, and resizing (drag a corner of the VM window).\n\n"
                    "When everything is ticked, this step is done - keep using Windows or shut it down.",
                    ("&Start VM", "&Skip checks", "Cancel"), width=72)
        if b == "Skip checks":
            conf_set(conf, "STAGE", "ready")
            return True
        if b != "Start VM":
            return False
        if not self.preflight(vm):
            return False
        r = self.start_and_watch(vm, "check")
        if conf_read(conf).get("STAGE") == "ready":
            return True
        if r != "closed":
            return False
        s = self.sessions.get(vm.get("DISK", ""))
        problems = []
        if s:
            for key in ("aero", "audio", "net"):
                ok, text = health_text(s.health, key)
                if ok is False:
                    problems.append(text)
            if not s.res_ok:
                problems.append("Resizing was not confirmed (drag a corner of the VM window next time)")
        b = self.msg("Checks", ("Not everything was confirmed:\n- " + "\n- ".join(problems)) if problems
                     else "Everything was confirmed.",
                     ("&Mark as ready anyway", "Check &again later") if problems else ("&OK",))
        if not problems or b == "Mark as ready anyway":
            conf_set(conf, "STAGE", "ready")
            return True
        return False

    # -- other actions -------------------------------------------------------------------
    def act_new(self):
        total_cores, total_mem = mac_cores(), mac_mem_mb()
        vals = {"guest": 0, "name": "Windows 7", "size": "40", "cores": str(min(4, total_cores)),
                "mem": "4096" if total_mem >= 16384 else "2048"}
        while True:
            b, v = self.form("New VM", [
                ("radio", "guest", ["Windows 7", "Windows Vista"], vals["guest"]),
                ("input", "name", "Name:", vals["name"]),
                ("input", "size", "Disk size in GB (it only uses what it needs, 40 recommended):", vals["size"]),
                ("input", "cores", "CPU cores (this Mac has %d fast cores, 4 recommended):" % total_cores, vals["cores"]),
                ("input", "mem", "Memory in MB (this Mac has %d MB):" % total_mem, vals["mem"]),
            ], ("&Create", "Cancel"), width=70)
            if b != "Create":
                return
            vals = v
            if vals["name"] in ("Windows 7", "Windows Vista"):
                vals["name"] = ["Windows 7", "Windows Vista"][vals["guest"]]
            problems = []
            name = vals["name"].strip()
            s = slug(name)
            if not s:
                problems.append("The name needs at least one letter or number.")
            size = re.sub(r"(?i)\s*g(b)?$", "", vals["size"].strip())
            if not size.isdigit() or int(size) < MIN_DISK_GB:
                problems.append("Disk size must be at least %d GB. Windows 7 with updates plus its page "
                                "file needs more than 20 GB." % MIN_DISK_GB)
            cores = vals["cores"].strip()
            if not cores.isdigit() or not 1 <= int(cores) <= total_cores:
                problems.append("CPU cores must be between 1 and %d." % total_cores)
            mem = vals["mem"].strip()
            if not mem.isdigit() or not 1024 <= int(mem) <= total_mem - 4096:
                problems.append("Memory must be between 1024 and %d MB." % (total_mem - 4096))
            conf = os.path.join(VMS, s + ".conf") if s else ""
            if s and (os.path.exists(conf) or os.path.exists(os.path.join(VMS, s + ".qcow2"))):
                if self.msg("Name in use", "There is already a VM called \"%s\"." % name,
                            ("&Continue that one", "&Pick another name")) == "Continue that one":
                    self.refresh_state()
                    for i, vm in enumerate(self.vms):
                        if vm["_conf"] == conf:
                            self.sel = i
                            self.continue_vm(vm)
                    return
                continue
            if problems:
                self.msg("Please check", "\n".join(problems))
                continue
            if int(cores) > MAX_SAFE_CORES and self.msg(
                    "Many cores", "%s cores is more than this setup has been tested with. Under emulation, more "
                    "than %d cores barely speeds Windows up and makes the installer more likely to fail. Use %d?"
                    % (cores, MAX_SAFE_CORES, MAX_SAFE_CORES),
                    ("&Use %d" % MAX_SAFE_CORES, "&Keep %s" % cores)) != "Keep %s" % cores:
                cores = str(MAX_SAFE_CORES)
            os.makedirs(VMS, exist_ok=True)
            guest = ["win7", "vista"][vals["guest"]]
            conf_write(conf, {"VM_NAME": name, "GUEST": guest,
                              "DISK": os.path.join(VMS, s + ".qcow2"), "DISK_SIZE": size + "G",
                              "VM_CPUS": cores, "MEM": mem, "STAGE": "new",
                              "AUDIO_DEVICE": "usb" if guest == "vista" else "hda", "NIC": "e1000",
                              "CLIPBOARD": "on"})
            self.refresh_state()
            for i, vm in enumerate(self.vms):
                if vm["_conf"] == conf:
                    self.sel = i
                    self.continue_vm(vm)
            return

    def act_edit(self):
        vm = self.selected_vm()
        conf, disk, name = vm["_conf"], vm.get("DISK", ""), vm.get("VM_NAME", "VM")
        if self.is_running(vm):
            self.msg("Running", "Shut %s down before changing its settings." % name)
            return
        total_cores, total_mem = mac_cores(), mac_mem_mb()
        cur_gb = int(re.sub(r"\D", "", vm.get("DISK_SIZE", "40")) or 40)
        if os.path.isfile(disk):
            cur_gb = max(cur_gb, disk_virtual(disk) // GB)
        guest = vm.get("GUEST", "win7")
        audio = vm.get("AUDIO_DEVICE", "usb" if guest == "vista" else "hda")
        nic = vm.get("NIC", "e1000")
        b, v = self.form("Settings for %s" % name, [
            ("input", "cores", "CPU cores (1-%d, 4 recommended):" % total_cores, vm.get("VM_CPUS", "4")),
            ("input", "mem", "Memory in MB (1024-%d):" % (total_mem - 4096), vm.get("MEM", "4096")),
            ("input", "size", "Disk size in GB (can only grow, now %d):" % cur_gb, str(cur_gb)),
            ("text", "Sound device:"),
            ("radio", "audio", ["HD Audio (best for Windows 7)", "USB audio (best for Vista)", "No sound"],
             {"hda": 0, "usb": 1, "none": 2}.get(audio, 0)),
            ("text", "Network card:"),
            ("radio", "nic", ["Intel PRO/1000 (recommended)", "Realtek RTL8139"], 0 if nic == "e1000" else 1),
            ("check", "clip", "Clipboard sharing with the Mac", vm.get("CLIPBOARD", "on") == "on"),
        ], ("&Save", "Cancel"), width=66)
        if b != "Save":
            return
        problems = []
        cores, mem = v["cores"].strip(), v["mem"].strip()
        size = re.sub(r"(?i)\s*g(b)?$", "", v["size"].strip())
        if not cores.isdigit() or not 1 <= int(cores) <= total_cores:
            problems.append("CPU cores must be between 1 and %d." % total_cores)
        if not mem.isdigit() or not 1024 <= int(mem) <= total_mem - 4096:
            problems.append("Memory must be between 1024 and %d MB." % (total_mem - 4096))
        if not size.isdigit() or int(size) < cur_gb:
            problems.append("The disk can only grow (at least %d GB)." % cur_gb)
        if problems:
            self.msg("Please check", "\n".join(problems))
            return
        conf_set(conf, "VM_CPUS", cores)
        conf_set(conf, "MEM", mem)
        conf_set(conf, "DISK_SIZE", size + "G")
        conf_set(conf, "AUDIO_DEVICE", ["hda", "usb", "none"][v["audio"]])
        conf_set(conf, "NIC", ["e1000", "rtl8139"][v["nic"]])
        conf_set(conf, "CLIPBOARD", "on" if v["clip"] else "off")
        note = ""
        if int(size) > cur_gb and os.path.isfile(disk):
            r = subprocess.run([QEMU_IMG, "resize", disk, size + "G"], capture_output=True, text=True)
            if r.returncode != 0:
                self.msg("Resize failed", r.stderr.strip()[-300:] or "qemu-img resize failed.")
                return
            if vm.get("STAGE", "new") != "new":
                note = ("\n\nWindows sees the extra space as unallocated. To use it: Start > right-click Computer > "
                        "Manage > Disk Management, right-click C: > Extend Volume.")
        self.msg("Saved", "Settings for %s saved.%s" % (name, note))

    def act_snapshots(self):
        vm = self.selected_vm()
        disk, name = vm.get("DISK", ""), vm.get("VM_NAME", "VM")
        if not os.path.isfile(disk):
            self.msg("No disk", "%s has no disk yet." % name)
            return
        if self.is_running(vm):
            self.msg("Running", "Shut %s down first - snapshots are taken and restored while it is off." % name)
            return
        while True:
            out = subprocess.run([QEMU_IMG, "snapshot", "-l", disk], capture_output=True, text=True).stdout
            snaps = []
            for line in out.splitlines():
                m = re.match(r"^\s*(\d+)\s+(.+?)\s+\S+\s+[\d-]+\s+[\d:]+", line)
                if m and not line.startswith("ID"):
                    snaps.append(m.group(2).strip())
            items = [("text", "Snapshots save %s's disk as it is now, so you can go back to it later." % name)]
            if snaps:
                items.append(("radio", "s", snaps, 0))
            else:
                items.append(("text", "(No snapshots yet.)"))
            b, v = self.form("Snapshots of %s" % name, items,
                             ("&Take new", "&Restore", "&Delete", "&Close") if snaps else ("&Take new", "&Close"),
                             width=66)
            if b in (None, "Close"):
                return
            if b == "Take new":
                b2, v2 = self.form("Take a snapshot", [("input", "n", "Name:", time.strftime("Snapshot %Y-%m-%d %H.%M"))],
                                   ("&Take", "Cancel"))
                if b2 == "Take" and v2["n"].strip():
                    r = subprocess.run([QEMU_IMG, "snapshot", "-c", v2["n"].strip(), disk], capture_output=True, text=True)
                    self.status_msg = "Snapshot taken." if r.returncode == 0 else "Snapshot failed: " + r.stderr.strip()[-80:]
                continue
            snap = snaps[v["s"]]
            if b == "Restore":
                if self.msg("Restore?", "Put %s's disk back to \"%s\"? Everything changed since then is lost."
                            % (name, snap), ("&Cancel", "&Restore"), default=0) == "Restore":
                    r = subprocess.run([QEMU_IMG, "snapshot", "-a", snap, disk], capture_output=True, text=True)
                    self.msg("Restore", "Restored." if r.returncode == 0 else r.stderr.strip()[-200:])
            elif b == "Delete":
                if self.msg("Delete?", "Delete the snapshot \"%s\"?" % snap, ("&Cancel", "&Delete"), default=0) == "Delete":
                    subprocess.run([QEMU_IMG, "snapshot", "-d", snap, disk], capture_output=True)

    def act_delete(self):
        vm = self.selected_vm()
        name, disk = vm.get("VM_NAME", "?"), vm.get("DISK", "")
        if self.is_running(vm):
            self.msg("Running", "%s is running. Shut Windows down first, then delete it." % name)
            return
        logs = glob.glob(os.path.join(LOGS, vm_slug(vm) + "-*.log"))
        launcher = os.path.expanduser("~/Desktop/%s.command" % name)
        ours = False
        if os.path.isfile(launcher):
            with open(launcher, errors="replace") as f:
                body = f.read()
            ours = "Created by Aero on Apple Silicon" in body and (vm["_conf"] in body or disk in body)
        items = [("text", "Choose what to delete:")]
        if os.path.isfile(disk):
            items.append(("check", "disk", "Disk image - Windows and all its files (%s)" % human(path_size(disk)), True))
        items.append(("check", "conf", "Its settings (removes it from the list)", True))
        if logs:
            items.append(("check", "logs", "Its log files (%s)" % human(sum(path_size(p) for p in logs)), True))
        if ours:
            items.append(("check", "launcher", "Its Desktop launcher", True))
        b, v = self.form("Delete %s" % name, items, ("&Delete...", "Cancel"), width=66, default=1)
        if b != "Delete...":
            return
        what = {"disk": "the disk image", "conf": "the settings", "logs": "the logs", "launcher": "the launcher"}
        chosen = [k for k in what if v.get(k)]
        if not chosen:
            return
        if self.msg("Are you sure?", "Permanently delete %s of \"%s\"? This cannot be undone."
                    % (", ".join(what[k] for k in chosen), name), ("&Cancel", "&Delete"), default=0) != "Delete":
            return
        if v.get("disk"):
            safe_rm(disk)
            if not v.get("conf"):
                conf_set(vm["_conf"], "STAGE", "new")
        if v.get("logs"):
            for p in logs:
                safe_rm(p)
        if v.get("launcher") and ours:
            os.remove(launcher)
        if v.get("conf"):
            safe_rm(vm["_conf"])
        self.status_msg = "Deleted: %s." % ", ".join(what[k] for k in chosen)

    def act_finder(self):
        subprocess.run(["open", "-R", self.selected_vm().get("DISK") or VMS])

    def act_logs(self):
        os.makedirs(LOGS, exist_ok=True)
        subprocess.run(["open", LOGS])

    def act_tools(self):
        if self.tools_ok:
            b = self.msg("Tools", "Everything is already in place. Nothing needs downloading.",
                         ("&OK", "&Rebuild disc", "Re-&download"))
            if b == "Rebuild disc":
                self.act_rebuild()
            elif b == "Re-download":
                self.act_redownload()
            return
        self.setup_tools()

    def act_rebuild(self):
        safe_rm(GUEST_TOOLS_ISO)
        self.setup_tools()

    def act_redownload(self):
        if self.msg("Download again?", "Delete the downloaded files (%s) and the runtime, then fetch them again? "
                    "Your VMs are not touched." % human(path_size(CACHE)),
                    ("&Cancel", "&Download again"), default=0) != "Download again":
            return
        if any(self.running.values()):
            self.msg("VMs running", "Shut down the running VMs first - they use the runtime.")
            return
        for p in (CACHE, os.path.join(ROOT, "runtime"), GUEST_TOOLS_ISO):
            safe_rm(p)
        self.setup_tools()

    def act_own_iso(self):
        err = ""
        while True:
            items = [("text", "Drag your %s into this window, or type its path." % VMTOOLS_FILE)]
            if err:
                items.append(("text", "! " + err))
            items.append(("input", "path", "File:", ""))
            b, v = self.form("Use my own VMware Tools ISO", items, ("&Use it", "Cancel"), width=72)
            if b != "Use it":
                return
            p = clean_path(v["path"])
            if not os.path.isfile(p):
                err = "That is not a file."
                continue
            self.busy("Checking the file...")
            if not sha_ok(p, VMTOOLS_SHA256):
                err = "That is not VMware Tools 10.3.10 (build 12406962)."
                continue
            os.makedirs(CACHE, exist_ok=True)
            dest = os.path.join(CACHE, VMTOOLS_FILE)
            if subprocess.run(["cp", "-c", p, dest]).returncode != 0:
                shutil.copyfile(p, dest)
            self.msg("Done", "Using your copy. It will not be downloaded.")
            self.setup_tools()
            return

    def act_rm_cache(self):
        if self.msg("Remove downloads", "Delete the downloaded files (%s)? They are fetched again the next time "
                    "tools are set up." % human(path_size(CACHE)), ("&Cancel", "&Delete"), default=0) == "Delete":
            safe_rm(CACHE)
            self.status_msg = "Downloads removed."

    def act_rm_runtime(self):
        if any(self.running.values()):
            self.msg("VMs running", "Shut down the running VMs first - they use the runtime.")
            return
        if self.msg("Remove runtime", "Delete the runtime (%s)? VMs cannot start until tools are set up again."
                    % human(path_size(os.path.join(ROOT, "runtime"))), ("&Cancel", "&Delete"), default=0) == "Delete":
            safe_rm(os.path.join(ROOT, "runtime"))
            self.status_msg = "Runtime removed."

    def act_rm_disc(self):
        safe_rm(GUEST_TOOLS_ISO)
        self.status_msg = "Guest tools disc removed (it is rebuilt when needed)."

    def act_rm_all(self):
        if any(self.running.values()):
            self.msg("VMs running", "Shut down the running VMs first.")
            return
        paths = [CACHE, os.path.join(ROOT, "runtime"), GUEST_TOOLS_ISO, os.path.join(ROOT, "downloads"), LOGS]
        if self.msg("Remove everything except VMs", "Delete downloads, runtime, guest tools disc and logs (%s)? "
                    "Your VMs are kept." % human(sum(path_size(p) for p in paths)),
                    ("&Cancel", "&Delete"), default=0) != "Delete":
            return
        for p in paths:
            safe_rm(p)
        os.makedirs(LOGS, exist_ok=True)
        self.status_msg = "Removed. Your VMs are untouched."

    def act_help(self):
        self.msg("Keys and mouse",
                 "Mouse: click anything; double-click a VM to continue with it.\n"
                 "F10 or Esc: menu bar. Arrows move, Enter chooses, red letters are shortcuts.\n"
                 "Tab: move between buttons. Q: quit (VMs keep running).\n"
                 "Dragging a file into this window types its path into a text box.\n"
                 "In the VM window: click to use it, Ctrl+Alt+G gives the mouse back.\n"
                 "Ctrl+Alt+Del for Windows: the red button top right, or the Machine menu.", width=74)

    def act_steps_help(self):
        self.msg("The five steps",
                 "1 Tools: QEMU, the display driver and clipboard parts are downloaded once.\n"
                 "2 Install Windows from your ISO. Ends when Windows shuts down from its desktop.\n"
                 "3 Drivers: run SETUP.CMD from the AEROTOOLS disc, restart once, shut down.\n"
                 "4 Checks: Windows reports Aero, sound and internet; you drag the window to test resizing.\n"
                 "5 Ready: start the VM from here or from its Desktop launcher.", width=76)

    def act_about(self):
        self.msg("About",
                 "Aero on Apple Silicon\n\n"
                 "Windows 7 and Vista with Aero on Apple Silicon, 3D rendered by the Mac's GPU through QEMU, "
                 "DXVK and MoltenVK.\n\n"
                 "Made by Kai with Claude (Anthropic).\n"
                 "Kai on GitHub: @The-Sequence  https://github.com/The-Sequence\n"
                 "Project: https://github.com/The-Sequence/aero-on-apple-silicon\n\n"
                 "Built on QEMU, qemu-vmvga, DXVK, MoltenVK, SDL, "
                 "SPICE and VMware's SVGA 3D driver.", width=64)

    # -- helpers --------------------------------------------------------------
    def busy(self, text):
        self.redraw()
        y, x = centered(self.ui, 3, len(text) + 8)
        self.ui.fill(y, x, 3, len(text) + 8, curses.color_pair(C_DLG))
        self.ui.put(y + 1, x + 4, text, curses.color_pair(C_DLG))
        self.ui.shadow(y, x, 3, len(text) + 8)
        self.ui.scr.refresh()

    def setup_tools(self):
        """Run Setup.command --auto with a live progress window. -> success"""
        ui = self.ui
        proc = subprocess.Popen(["bash", os.path.join(ROOT, "Setup.command"), "--auto"], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        lines, stage, buf, eof = [], 0, b"", False
        stages = ["Checking this Mac", "Installing dependencies", "Runtime", "VMware SVGA 3D", "Clipboard",
                  "Building guest-tools"]
        ansi = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
        while True:
            r = []
            if not eof:
                r, _, _ = select.select([proc.stdout], [], [], 0.2)
            if r:
                chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    # End of output: select() keeps reporting EOF as readable,
                    # so stop polling the pipe or this loop never ends.
                    eof, r = True, []
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                else:
                    buf += chunk
                    parts = re.split(rb"[\r\n]", buf)
                    buf = parts.pop()
                    for p in parts:
                        t = ansi.sub("", p.decode("utf-8", "replace")).rstrip()
                        if not t:
                            continue
                        for si, name in enumerate(stages):
                            if name in t:
                                stage = max(stage, si + 1)
                        lines.append(t)
            done = proc.poll() is not None and eof
            ui.hits = []
            ui.scr.erase()
            self.bg()
            h, w = ui.scr.getmaxyx()
            dh, dw = min(18, h - 4), min(76, w - 4)
            y, x = centered(ui, dh, dw)
            ui.fill(y, x, dh, dw, curses.color_pair(C_DLG))
            ui.fill(y, x, 1, dw, curses.color_pair(C_DLG_TITLE))
            t = "Setting up tools"
            ui.put(y, x + (dw - len(t)) // 2, t, curses.color_pair(C_DLG_TITLE))
            ui.shadow(y, x, dh, dw)
            ui.box(y + 1, x + 2, dh - 5, dw - 4, curses.color_pair(C_DLG))
            for i, t in enumerate(lines[-(dh - 7):]):
                ui.put(y + 2 + i, x + 4, t[: dw - 8], curses.color_pair(C_DLG))
            pct = 100 if (done and proc.returncode == 0) else int(stage * 100 / (len(stages) + 1))
            ui.put(y + dh - 3, x + 3, "0%", curses.color_pair(C_DLG))
            bw = dw - 22
            fill = int(bw * pct / 100)
            ui.put(y + dh - 3, x + 6, "█" * fill + "░" * (bw - fill), curses.color_pair(C_DLG))
            ui.put(y + dh - 3, x + 7 + bw, "%3d%% Complete" % pct, curses.color_pair(C_DLG) | curses.A_BOLD)
            ui.draw_cursor()
            ui.scr.refresh()
            if not r:
                ui.get_event(1)
            if done:
                break
        ok = proc.returncode == 0
        self.tools_ok, self.tools = tools_status()
        if ok:
            self.msg("Tools ready", "Everything is in place.")
        else:
            self.msg("Setup did not finish", "Last messages:\n" + "\n".join(lines[-6:]), width=74)
        return ok and self.tools_ok


def make_launcher(vm):
    """A Desktop .command that opens this screen and starts the VM."""
    name = vm.get("VM_NAME", "VM")
    path = os.path.expanduser("~/Desktop/%s.command" % name)
    conf = vm["_conf"]
    if os.path.exists(path):
        with open(path, errors="replace") as f:
            body = f.read()
        if "Created by Aero on Apple Silicon" not in body:
            return "taken"
        if conf in body and "--start" in body:
            return "already"
    with open(path, "w") as f:
        f.write("#!/bin/bash\n# Opens Aero on Apple Silicon and starts \"%s\". Created by Aero on Apple Silicon.\n" % name)
        f.write("cd %s || { echo \"The project folder has moved.\"; read -r -p \"Press Return.\"; exit 1; }\n"
                % shlex.quote(ROOT))
        f.write("exec ./START\\ HERE.command --start %s\n" % shlex.quote(conf))
    os.chmod(path, 0o755)
    return "created"


# =====================================================================

def main(scr, start_conf=None):
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    curses.mouseinterval(0)
    sys.stdout.write("\033[?1003h")     # report mouse movement for the text-mode cursor
    sys.stdout.flush()
    init_colors()
    curses.curs_set(0)
    ui = UI(scr)
    h, w = scr.getmaxyx()
    if h < MIN_H or w < MIN_W:
        return PLAIN
    ui.fill(0, 0, h, w, curses.color_pair(C_DESK), "░")
    msg = " Checking what is already here... "
    ui.put(h // 2, (w - len(msg)) // 2, msg, curses.color_pair(C_DLG) | curses.A_BOLD)
    scr.refresh()
    return App(ui).run(start_conf)


def run():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return PLAIN
    if shutil.which("brew") is None:
        print("Homebrew is required: https://brew.sh")
        return PLAIN
    start_conf = None
    if len(sys.argv) >= 3 and sys.argv[1] == "--start":
        start_conf = sys.argv[2]
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    try:
        return curses.wrapper(main, start_conf)
    except KeyboardInterrupt:
        return 0
    finally:
        sys.stdout.write("\033[?1003l\033[?1000l")
        sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(run())
