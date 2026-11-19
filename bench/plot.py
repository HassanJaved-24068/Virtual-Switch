#!/usr/bin/env python3
"""
plot.py - turns the raw numbers of bench/run.sh into results.csv (medians), throughput.png (the graph) and report.pdf (one page).

    python3 bench/plot.py [directory]        directory = where run.sh wrote raw_mac.csv, raw_net.csv and machine.txt (default: bench)

The roadmap names matplotlib or gnuplot; neither is installed on the machine of this project, so the graph is drawn with PIL (Pillow), which is. Nothing else is needed, and the
output depends only on the CSV files: the same CSV files always give the same picture. The explanation in the report is generated from the numbers (every sentence that
compares the variants is computed, not written in advance), so it cannot disagree with the graph.
"""
import csv, os, statistics, sys, textwrap
from collections import defaultdict

from PIL import Image, ImageDraw, ImageFont

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)))
VARIANTS = ["global", "striped", "rwlock"]
COLORS = {"global": (213, 94, 0), "striped": (0, 114, 178), "rwlock": (0, 158, 115)}       # colour-blind safe (Okabe-Ito)
LABEL = {"global": "global lock (one mutex)", "striped": "striped locks (256 mutexes)", "rwlock": "rwlock (one pthread_rwlock_t)"}
INK, GRID, MUTED, BG = (30, 30, 30), (222, 222, 222), (110, 110, 110), (255, 255, 255)


def font(size, bold=False):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else ""),
                 "/usr/share/fonts/truetype/liberation/LiberationSans-%s.ttf" % ("Bold" if bold else "Regular")):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def read_raw(name, xcol, ycol):
    """{variant: {x: [values of the runs]}}"""
    data = defaultdict(lambda: defaultdict(list))
    with open(os.path.join(OUT, name)) as f:
        for row in csv.DictReader(f):
            data[row["variant"]][int(row[xcol])].append(float(row[ycol]))
    return data


def summary(data):
    """{variant: {x: (median, min, max, runs)}}"""
    return {v: {x: (statistics.median(r), min(r), max(r), len(r)) for x, r in sorted(xs.items())} for v, xs in data.items()}


def human(v):
    for unit, div in (("M", 1e6), ("k", 1e3)):
        if abs(v) >= div:
            return "%.2f%s" % (v / div, unit)
    return "%.0f" % v


def nice_max(v):
    for p in range(-3, 12):                                # the smallest "nice" number (1, 2, 2.5, 5 x 10^p) that is not below v
        for base in (1, 2, 2.5, 5, 10):
            if base * 10 ** p >= v:
                return base * 10 ** p
    return v


# ---- the chart --------------------------------------------------------------------------------------------------------------------------------------------------------------

def draw_panel(d, box, title, xlabel, ylabel, series, xs, scale, unit):
    x0, y0, x1, y1 = box                                   # the plot area (inside the axes)
    ymax = nice_max(max(s[x][2] for s in series.values() for x in xs if x in s) / scale) * 1.0
    f_t, f_l, f_s = font(26, True), font(20), font(17)
    d.text(((x0 + x1) // 2, y0 - 70), title, fill=INK, font=f_t, anchor="mm")
    for i in range(6):                                     # horizontal grid and y labels
        v = ymax * i / 5
        y = y1 - (y1 - y0) * i / 5
        d.line([(x0, y), (x1, y)], fill=GRID, width=1)
        d.text((x0 - 12, y), ("%g" % v), fill=MUTED, font=f_s, anchor="rm")
    d.line([(x0, y0), (x0, y1)], fill=INK, width=2)
    d.line([(x0, y1), (x1, y1)], fill=INK, width=2)
    step = (x1 - x0) / len(xs)
    px = {x: x0 + step * (i + 0.5) for i, x in enumerate(xs)}
    for x in xs:
        d.line([(px[x], y1), (px[x], y1 + 7)], fill=INK, width=2)
        d.text((px[x], y1 + 22), str(x), fill=MUTED, font=f_s, anchor="mm")
    d.text(((x0 + x1) // 2, y1 + 58), xlabel, fill=INK, font=f_l, anchor="mm")
    label = Image.new("RGBA", (420, 30), (255, 255, 255, 0))
    ImageDraw.Draw(label).text((210, 15), ylabel, fill=INK, font=f_l, anchor="mm")
    d._image.paste(label.rotate(90, expand=True), (x0 - 92, (y0 + y1) // 2 - 210), label.rotate(90, expand=True))

    def py(v):
        return y1 - (y1 - y0) * (v / scale) / ymax

    ends = []
    for k, v in enumerate(VARIANTS):
        s = series.get(v)
        if not s:
            continue
        col = COLORS[v]
        pts = [(px[x], py(s[x][0])) for x in xs if x in s]
        for x in xs:                                       # the spread of the runs (min to max)
            if x in s:
                d.line([(px[x], py(s[x][1])), (px[x], py(s[x][2]))], fill=col, width=3)
                for yy in (s[x][1], s[x][2]):
                    d.line([(px[x] - 6, py(yy)), (px[x] + 6, py(yy))], fill=col, width=3)
        d.line(pts, fill=col, width=4, joint="curve")
        for (cx, cy) in pts:                               # a different marker per variant (not colour alone)
            if k == 0:
                d.ellipse([cx - 8, cy - 8, cx + 8, cy + 8], fill=col, outline=BG, width=2)
            elif k == 1:
                d.rectangle([cx - 8, cy - 8, cx + 8, cy + 8], fill=col, outline=BG, width=2)
            else:
                d.polygon([(cx, cy - 10), (cx + 9, cy + 7), (cx - 9, cy + 7)], fill=col, outline=BG)
        if xs[-1] in s:
            ends.append((pts[-1][1], pts[-1][0], human(s[xs[-1]][0]), col))
    ends.sort()                                            # the value labels at the end of the lines must not overlap: keep them at least 22 px apart
    for i in range(1, len(ends)):
        if ends[i][0] - ends[i - 1][0] < 22:
            ends[i] = (ends[i - 1][0] + 22, ends[i][1], ends[i][2], ends[i][3])
    for ly, lx, text, col in ends:
        d.text((lx + 16, ly), text, fill=col, font=f_s, anchor="lm")


def draw_legend(d, x, y):
    f = font(20)
    for k, v in enumerate(VARIANTS):
        cx = x + k * 520
        col = COLORS[v]
        d.line([(cx, y), (cx + 46, y)], fill=col, width=4)
        mx = cx + 23
        if k == 0:
            d.ellipse([mx - 8, y - 8, mx + 8, y + 8], fill=col)
        elif k == 1:
            d.rectangle([mx - 8, y - 8, mx + 8, y + 8], fill=col)
        else:
            d.polygon([(mx, y - 10), (mx + 9, y + 7), (mx - 9, y + 7)], fill=col)
        d.text((cx + 58, y), LABEL[v], fill=INK, font=f, anchor="lm")


def make_chart(mac, net, path):
    W, H = 1700, 700
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d._image = img
    draw_legend(d, 60, 38)
    mx = sorted({x for s in mac.values() for x in s})
    nx = sorted({x for s in net.values() for x in s})
    draw_panel(d, (170, 150, 760, 560), "MAC table alone (in-process)", "threads", "operations per second (millions)", mac, mx, 1e6, "M")
    draw_panel(d, (1000, 150, 1590, 560), "Whole switch (loopback TCP)", "hosts", "frames received per second (thousands)", net, nx, 1e3, "k")
    d.text((W // 2, H - 24), "median of the runs, bars = lowest to highest run", fill=MUTED, font=font(17), anchor="mm")
    img.save(path)
    return img


# ---- the text of the report ---------------------------------------------------------------------------------------------------------------------------------------------

def med(s, v, x):
    return s[v][x][0]


def explanation(mac, net, cores):
    mx, nx = sorted({x for s in mac.values() for x in s}), sorted({x for s in net.values() for x in s})
    t1, tn = mx[0], mx[-1]
    out = []
    g1, s1, r1 = (med(mac, v, t1) for v in VARIANTS)
    out.append("In-process, %d thread: global %s, striped %s, rwlock %s operations/s. With a single thread there is no contention, so this is the bare cost of taking and "
               "releasing the lock; the three differ by at most %.0f %%." % (t1, human(g1), human(s1), human(r1), 100 * (max(g1, s1, r1) - min(g1, s1, r1)) / max(g1, s1, r1)))
    gn, sn, rn = (med(mac, v, tn) for v in VARIANTS)
    rel = {v: [med(mac, v, x) / med(mac, v, t1) for x in mx[1:]] for v in VARIANTS}
    out.append("In-process, %d to %d threads on %d core%s: global %s -> %s, striped %s -> %s, rwlock %s -> %s operations/s. Relative to its own 1-thread value, over all thread counts "
               "above 1: global %.0f-%.0f %%, striped %.0f-%.0f %%, rwlock %.0f-%.0f %%. At %d threads striped is %.1f x the global lock and %.1f x the rwlock."
               % (t1, tn, cores, "" if cores == 1 else "s", human(g1), human(gn), human(s1), human(sn), human(r1), human(rn),
                  100 * min(rel["global"]), 100 * max(rel["global"]), 100 * min(rel["striped"]), 100 * max(rel["striped"]), 100 * min(rel["rwlock"]), 100 * max(rel["rwlock"]),
                  tn, sn / gn, sn / rn))
    out.append("Why (the usual explanation; the cache-line effects themselves were not measured separately): the global mutex serialises every operation, and with more than one "
               "thread every acquisition moves the lock's cache line from one core to another and blocked threads sleep and wake up, so more threads %s. Striped locking gives each "
               "of the 256 buckets its own mutex: operations on different buckets do not meet and the lock lines are not shared by all threads, so the throughput %s."
               % ("do not add throughput, they cost it" if max(rel["global"]) < 1 else "add little", "grows with the threads until the %d core%s are used up" % (cores, "" if cores == 1 else "s")
                  if min(rel["striped"]) > 1.05 else "does not collapse"))
    if max(rel["global"]) > 1.2 * min(rel["global"]) and rel["global"][-1] > rel["global"][0]:
        out.append("Note: the global lock recovers somewhat when there are many more threads than cores (%d threads: %.0f %% of its 1-thread value, against %.0f %% at %d threads); "
                   "oversubscribed threads are descheduled while others run, so fewer of them fight for the lock at the same instant. That is a property of this machine, not a virtue of the lock."
                   % (tn, 100 * rel["global"][-1], 100 * rel["global"][0], mx[1]))
    if rn < gn:
        out.append("The rwlock did NOT do what is usually expected of it: although lookups (90 %) can share the read lock, every reader still performs an atomic update of the "
                   "same lock word (one cache line bounced between the cores), and every learn (10 %) takes the write lock, which waits for the readers and blocks all new ones. "
                   "Here that made it slower than the plain mutex. A read-mostly workload with this few writers is not enough to make ONE rwlock scale.")
    elif rn > gn:
        out.append("The rwlock beats the global mutex because lookups share the read lock, but it stays below striped locking: all readers still update the same lock word and "
                   "every learn blocks everybody.")
    else:
        out.append("The rwlock and the global mutex measured the same.")
    ns = [(x, med(net, "global", x), med(net, "striped", x), med(net, "rwlock", x)) for x in nx]
    spread = max((max(a, b, c) - min(a, b, c)) / max(a, b, c) for _, a, b, c in ns)
    runspread = max((s[x][2] - s[x][1]) / s[x][0] for s in net.values() for x in s)
    out.append("Whole switch: with %s hosts the three variants deliver %s" % ("/".join(str(x) for x in nx), "; ".join("%s / %s / %s" % (human(a), human(b), human(c)) for _, a, b, c in ns)) +
               " frames/s (global / striped / rwlock). The largest difference between the variants at any point is %.0f %%, the runs of ONE variant differ by up to %.0f %%. %s"
               % (100 * spread, 100 * runspread,
                  "The differences between the variants are therefore within the run-to-run noise: in the whole switch the MAC table lock is not the bottleneck. One delivered frame "
                  "takes about %.0f us of the whole machine (receiver thread, log line, two socket system calls, sender thread; all built with -O0), one table operation about %.0f ns "
                  "(%.1f %% of that, once or twice per frame); the micro-benchmark isolates the lock, the network benchmark shows how little of the whole it is."
                  % (1e6 / ns[0][2], 1e9 / g1, 100 * (1e9 / g1) / (1e9 / ns[0][2]) * 2) if spread <= max(0.10, runspread)
                  else "The variants differ by more than the noise; see the table."))
    if tn > cores or max(nx) + 1 > cores:
        out.append("The machine has only %d core%s: points above %d thread%s / hosts are oversubscribed (the threads share the cores), which lowers every curve and is the same for the three "
                   "variants; the load generator runs on the same cores as the switch." % (cores, "" if cores == 1 else "s", cores, "" if cores == 1 else "s"))
    return out


# ---- the page --------------------------------------------------------------------------------------------------------------------------------------------------------------

def wrap(d, text, f, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= width:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def make_report(chart, mac, net, machine, paras, path, fs):
    W, H = 1240, 1754                                      # A4 at 150 dpi
    lh = fs + 5
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    M = 80
    y = 60
    d.text((M, y), "MAC table locking: global lock vs striped locks vs rwlock", fill=INK, font=font(32, True))
    y += 48
    d.text((M, y), "Virtual Switch, Part 21 benchmark", fill=MUTED, font=font(20))
    y += 46
    f_h, f_s = font(fs + 6, True), font(fs)
    d.text((M, y), "Machine and method", fill=INK, font=f_h)
    y += lh + 12
    for line in machine:
        for l in wrap(d, line, f_s, W - 2 * M):
            d.text((M, y), l, fill=INK, font=f_s)
            y += lh
    y += 8
    for l in wrap(d, "Method: the same source file mactable.c is built three times with -DMAC_LOCK=GLOBAL | STRIPED | RWLOCK; everything is built with -O0, as the project requires. "
                     "'MAC table alone': bench_mac, T threads share one table (1024 entries, 256 buckets, 512 keys) and do 90 % lookups / 10 % learns for the time given. "
                     "'Whole switch': bench opens H connections to the switch, warms up with one broadcast per host (so every MAC is learned) and then every host sends unicast frames "
                     "to random other hosts as fast as it can; the number is the frames RECEIVED per second. Random choices use fixed seeds. Each point is run 3 times, the median is shown.",
                  f_s, W - 2 * M):
        d.text((M, y), l, fill=INK, font=f_s)
        y += lh
    y += 14
    cw = W - 2 * M
    ch = int(chart.height * cw / chart.width)
    img.paste(chart.resize((cw, ch), Image.LANCZOS), (M, y))
    y += ch + 14

    def table(title, s, xs, xname):
        nonlocal y
        d.text((M, y), title, fill=INK, font=f_h)
        y += lh + 10
        cols = [M, M + 250] + [M + 250 + 150 * (i + 1) for i in range(len(xs) - 1)]
        d.text((M, y), "%s:" % xname, fill=MUTED, font=f_s)
        for i, x in enumerate(xs):
            d.text((M + 250 + 150 * i, y), str(x), fill=MUTED, font=f_s)
        y += lh + 2
        for v in VARIANTS:
            d.text((M, y), v, fill=COLORS[v], font=font(fs, True))
            for i, x in enumerate(xs):
                d.text((M + 250 + 150 * i, y), human(s[v][x][0]) if x in s[v] else "-", fill=INK, font=f_s)
            y += lh
        y += 8

    mx = sorted({x for s in mac.values() for x in s})
    nx = sorted({x for s in net.values() for x in s})
    table("Operations per second (median), MAC table alone", mac, mx, "threads")
    table("Frames received per second (median), whole switch", net, nx, "hosts")
    d.text((M, y), "Explanation", fill=INK, font=f_h)
    y += lh + 10
    for p in paras:
        for l in wrap(d, p, f_s, W - 2 * M):
            d.text((M, y), l, fill=INK, font=f_s)
            y += lh
        y += 8
    y += 6
    for l in wrap(d, "Reproduce: bench/run.sh (builds with make bench, writes bench/raw_mac.csv, bench/raw_net.csv, then runs this script). The numbers depend on the machine and on what else "
                     "runs on it; the seeds, the method and the raw CSV files are in the repository.", f_s, W - 2 * M):
        d.text((M, y), l, fill=MUTED, font=f_s)
        y += lh
    if y > H - 40:
        return False
    img.save(path, "PDF", resolution=150.0)
    return True


def main():
    mac = summary(read_raw("raw_mac.csv", "threads", "ops_per_sec"))
    net = summary(read_raw("raw_net.csv", "hosts", "frames_per_sec"))
    missing = [v for v in VARIANTS if v not in mac or v not in net]
    if missing:
        sys.exit("plot.py: no data for %s" % ", ".join(missing))
    with open(os.path.join(OUT, "results.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["benchmark", "variant", "x", "median", "min", "max", "runs"])
        for name, s in (("mac_ops_per_sec", mac), ("net_frames_per_sec", net)):
            for v in VARIANTS:
                for x, (m, lo, hi, n) in s[v].items():
                    w.writerow([name, v, x, "%.0f" % m, "%.0f" % lo, "%.0f" % hi, n])
    with open(os.path.join(OUT, "machine.txt")) as f:
        machine = [l.rstrip("\n") for l in f if l.strip()]
    cores = 1
    for l in machine:
        if l.startswith("cores:"):
            cores = int(l.split()[1])
    chart = make_chart(mac, net, os.path.join(OUT, "throughput.png"))
    paras = explanation(mac, net, cores)
    for fs in (17, 16, 15, 14, 13, 12):                    # the report is ONE page: the largest font that fits
        if make_report(chart, mac, net, machine, paras, os.path.join(OUT, "report.pdf"), fs):
            break
    else:
        sys.exit("plot.py: the report does not fit on one page")
    print("plot.py: wrote results.csv, throughput.png, report.pdf in %s" % OUT)


if __name__ == "__main__":
    main()
