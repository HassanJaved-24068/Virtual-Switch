"""Independent black-box verification harness for the Virtual Switch (raw sockets, no use of the project's own test helpers)."""
import os, sys, time, socket, struct, subprocess, signal, tempfile, shutil, random, traceback, json, re, select, threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BCAST = "ff:ff:ff:ff:ff:ff"
_port = [random.randint(21000, 30000)]

def next_port():
    _port[0] += 3
    return _port[0]

def mac_b(m): return bytes(int(x, 16) for x in m.split(":"))
def mac_s(b): return ":".join("%02x" % x for x in b)
def M(n): return "02:00:00:00:%02x:%02x" % (n >> 8, n & 255)

def pack(src, dst, vlan=1, prio=1, payload=b"", length=None):
    if isinstance(payload, str): payload = payload.encode()
    if length is None: length = len(payload)
    return struct.pack("!H", length) + mac_b(src) + mac_b(dst) + struct.pack("!HB", vlan, prio) + payload

# ---------------- results ----------------
RESULTS = []          # failures + env-limits
ALL = []              # (id, status)
class Skip(Exception): pass
CUR = {}

def ck(cond, what, expected=None, actual=None, component=None, cause=None, logs=None):
    """record a check; a failed check marks the current test failed (test keeps running)."""
    CUR["n"] = CUR.get("n", 0) + 1
    if cond: return True
    CUR.setdefault("fails", []).append(dict(what=what, expected=None if expected is None else str(expected),
        actual=None if actual is None else str(actual), component=component, cause=cause, logs=(logs or "")[-1800:]))
    return False

TESTS = []
def test(tid, desc, verifies=None, timeout=120):
    def deco(fn):
        TESTS.append((tid, desc, verifies or desc, timeout, fn)); return fn
    return deco

class TO(Exception): pass
def _alarm(sig, frm): raise TO()

def run_tests(filt=None, out="results.json"):
    signal.signal(signal.SIGALRM, _alarm)
    res = []
    for tid, desc, ver, tmo, fn in TESTS:
        if filt and not any(tid.startswith(f) for f in filt): continue
        CUR.clear(); t0 = time.time(); status = "pass"; err = None
        signal.alarm(tmo)
        try:
            fn()
        except Skip as e: status = "env"; err = str(e)
        except TO: status = "fail"; err = "TEST TIMEOUT after %ds (hang)" % tmo; CUR.setdefault("fails", []).append(dict(what="hang/timeout", expected="finish in %ds" % tmo, actual="did not finish", component=None, cause=None, logs=""))
        except Exception:
            status = "fail"; err = traceback.format_exc()[-1500:]
            CUR.setdefault("fails", []).append(dict(what="exception in test (switch behaviour or harness)", expected="no exception", actual=err, component=None, cause=None, logs=""))
        finally:
            signal.alarm(0)
            cleanup_all()
        if CUR.get("fails"): status = "fail"
        r = dict(id=tid, desc=desc, verifies=ver, status=status, checks=CUR.get("n", 0), fails=CUR.get("fails", []), note=err if status == "env" else None, secs=round(time.time() - t0, 1))
        res.append(r)
        print("%-9s %-5s %3d checks %5.1fs  %s" % (tid, status.upper(), r["checks"], r["secs"], desc), flush=True)
        for f in r["fails"]:
            print("      !! %s | expected=%s | actual=%s" % (f["what"], (f["expected"] or "")[:120], (f["actual"] or "")[:200]), flush=True)
        with open(out, "w") as fh: json.dump(res, fh, indent=1)
    return res

# ---------------- process management ----------------
LIVE = []
def cleanup_all():
    for o in LIVE[:]:
        try: o.kill()
        except Exception: pass
    LIVE.clear()

class Switch:
    def __init__(self, nports=4, conf=None, binary="switch", extra=(), env=None, pre=(), port=None, confname="v.conf", ulimit_n=None, args=None):
        self.port = port or next_port()
        self.dir = tempfile.mkdtemp(prefix="vt.")
        self.logf = os.path.join(self.dir, "sw.log")
        self.cmd = list(pre) + [os.path.join(ROOT, binary)]
        self.cmd += args if args is not None else [str(self.port), str(nports)]
        if conf is not None:
            self.conffile = os.path.join(self.dir, confname)
            with open(self.conffile, "w") as f: f.write(conf)
            self.cmd += ["-c", self.conffile]
        self.cmd += list(extra)
        e = dict(os.environ); e["VSWITCH_LOG_FILE"] = self.logf
        if env:
            for k, v in env.items():
                if v is None: e.pop(k, None)
                else: e[k] = v
        self.env = e; self.ulimit_n = ulimit_n; self.hosts = []; self.p = None
        LIVE.append(self)
    def start(self, wait=True, timeout=10):
        pre = None
        if self.ulimit_n:
            import resource
            n = self.ulimit_n
            pre = lambda: resource.setrlimit(resource.RLIMIT_NOFILE, (n, n))
        self.errf = open(os.path.join(self.dir, "stderr"), "w+")
        self.p = subprocess.Popen(self.cmd, stdout=self.errf, stderr=subprocess.STDOUT, env=self.env, cwd=self.dir, preexec_fn=pre)
        if wait:
            end = time.time() + timeout
            while time.time() < end:
                if self.p.poll() is not None: return self
                if "listening" in self.log(): break
                time.sleep(0.02)
            else: ck(False, "switch did not start listening", "listening log line", self.log())
        return self
    def log(self):
        try: return open(self.logf, errors="replace").read()
        except Exception: return ""
    def err(self):
        try: self.errf.flush(); return open(self.errf.name, errors="replace").read()
        except Exception: return ""
    def alive(self): return self.p is not None and self.p.poll() is None
    def wait_log(self, text, n=1, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            if self.log().count(text) >= n: return True
            time.sleep(0.02)
        return False
    def sig(self, s): self.p.send_signal(s)
    def stop(self, s=signal.SIGINT, timeout=10):
        if not self.alive(): return self.p.returncode if self.p else None
        self.p.send_signal(s)
        try: self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            self.p.kill(); self.p.wait(); return "HANG"
        return self.p.returncode
    def kill(self):
        for h in self.hosts: h.close()
        if self.p and self.p.poll() is None:
            self.p.kill()
            try: self.p.wait(5)
            except Exception: pass
        try: self.errf.close()
        except Exception: pass
        shutil.rmtree(self.dir, ignore_errors=True)
    def ctl(self, *a, timeout=10, port=None):
        r = subprocess.run([os.path.join(ROOT, "swctl"), "-p", str(port or self.port)] + [str(x) for x in a], capture_output=True, text=True, timeout=timeout, cwd=self.dir)
        return r.returncode, r.stdout + r.stderr
    def host(self, mac, **kw):
        h = RawHost(self.port, mac, **kw); self.hosts.append(h); return h
    def threads(self): return len(os.listdir("/proc/%d/task" % self.p.pid))
    def nfds(self): return len(os.listdir("/proc/%d/fd" % self.p.pid))
    def rss_kb(self):
        for l in open("/proc/%d/status" % self.p.pid):
            if l.startswith("VmRSS"): return int(l.split()[1])
    def cpu_ticks(self):
        f = open("/proc/%d/stat" % self.p.pid).read().rsplit(")", 1)[1].split()
        return int(f[11]) + int(f[12])
    def wait_ports(self, state, ports, timeout=6):
        end = time.time() + timeout
        while time.time() < end:
            rc, out = self.ctl("show", "ports")
            st = {}
            for l in out.splitlines():
                p = l.split()
                if len(p) >= 2 and p[0].isdigit(): st[int(p[0])] = p[1]
            if all(st.get(p) == state for p in ports): return True
            time.sleep(0.05)
        return False
    def mac_table(self):
        rc, out = self.ctl("show", "mac")
        rows = []
        for l in out.splitlines():
            p = l.split()
            if len(p) >= 4 and p[0].isdigit(): rows.append((int(p[0]), p[1], int(p[2]), int(p[3])))
        return rows
    def stats(self):
        rc, out = self.ctl("stats"); d = {}
        for l in out.splitlines():
            p = l.split()
            if len(p) >= 7 and p[0].isdigit(): d[int(p[0])] = dict(state=p[1], vlan=int(p[2]), inn=int(p[3]), out=int(p[4]), drop=int(p[5]), mir=int(p[6]))
        return d

class RawHost:
    def __init__(self, port, mac, connect=True, ip="127.0.0.1", rcvbuf=None):
        self.mac = mac; self.s = None; self.buf = b""
        if connect:
            self.s = socket.socket()
            if rcvbuf: self.s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
            self.s.settimeout(10); self.s.connect((ip, port))
        self.rx = []
    def send(self, dst, vlan=1, prio=1, payload=b"x", src=None, length=None):
        self.s.sendall(pack(src or self.mac, dst, vlan, prio, payload, length))
    def raw(self, b): self.s.sendall(b)
    def _fill(self, n, deadline):
        if self.s is None: return None
        while len(self.buf) < n:
            t = deadline - time.time()
            if t <= 0: return False
            self.s.settimeout(t)
            try: d = self.s.recv(65536)
            except socket.timeout: return False
            except (ConnectionResetError, OSError): d = b""
            if not d: return None
            self.buf += d
        return True
    def recv(self, timeout=2.0):
        """returns dict frame, None on timeout, 'EOF' on close"""
        dl = time.time() + timeout
        r = self._fill(17, dl)
        if r is None: return "EOF"
        if not r: return None
        ln = struct.unpack("!H", self.buf[:2])[0]
        r = self._fill(17 + ln, dl)
        if r is None: return "EOF"
        if not r: return None
        raw = self.buf[:17 + ln]; self.buf = self.buf[17 + ln:]
        return dict(len=ln, src=mac_s(raw[2:8]), dst=mac_s(raw[8:14]), vlan=struct.unpack("!H", raw[14:16])[0], prio=raw[16], data=raw[17:], raw=raw)
    def drain(self, quiet=0.4, maxt=10):
        out = []; end = time.time() + maxt
        while time.time() < end:
            f = self.recv(quiet)
            if f is None or f == "EOF": break
            out.append(f)
        return out
    def expect(self, timeout=2.0, **kw):
        f = self.recv(timeout)
        if not isinstance(f, dict): return None
        return f
    def got_data(self, data, timeout=1.5):
        end = time.time() + timeout
        while time.time() < end:
            f = self.recv(max(0.05, end - time.time()))
            if f == "EOF": return False
            if isinstance(f, dict) and f["data"] == (data.encode() if isinstance(data, str) else data): return True
            if f is None: break
        return False
    def eof(self, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            f = self.recv(max(0.05, end - time.time()))
            if f == "EOF": return True
        return False
    def close(self):
        try:
            if self.s: self.s.close()
        except Exception: pass
        self.s = None

def settle(sw, n, timeout=6):
    """wait until n 'host connected' log lines"""
    return sw.wait_log("host connected", n, timeout)

def hosts_up(sw, n):
    """connect n hosts sequentially (port i gets host i) and wait for each connect line"""
    hs = []
    for i in range(1, n + 1):
        h = sw.host(M(i)); hs.append(h)
        if not settle(sw, i): ck(False, "host %d connect not logged" % i, "log line", sw.log())
    return hs

def sh(cmd, timeout=120, cwd=None, env=None):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, cwd=cwd or ROOT, env=env)
    return r.returncode, r.stdout + r.stderr

def san_clean(sw):
    t = sw.err()
    t = t.replace("ERROR SUMMARY: 0 errors from 0 contexts", "")
    return not re.search(r"ThreadSanitizer|WARNING: |ERROR: |==\d+== (Invalid|Conditional|Use of|Syscall)|definitely lost|indirectly lost|possibly lost|still reachable|SUMMARY: ", t)
