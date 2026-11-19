# Virtual Switch

A multithreaded software network switch for Linux, written in C. Hosts connect over TCP; the switch learns
which MAC address is behind which port, forwards frames only where they have to go, floods unknown
destinations and broadcasts, and manages its ports, MAC table, logging, signals and configuration.

This README describes the switch as of **Part 17** (port mirroring; before it Part 16, live reconfiguration by `swctl shutdown|up|vlan|clear mac`; before it Part 15, the admin socket and `swctl`; before it the per-port counters in POSIX shared memory of Part 14, VLANs and the trunk port of
Part 13, rate limiting of Part 12, the priority queues of Part 11 and the configuration file, `SIGHUP` reload and `SIGUSR1` MAC dump of Part 10).
Features of later parts (the bonus items of Tier 3) are not implemented yet; where the configuration file already accepts their keys
this is said explicitly below.

## Build

```
make            # switch, host, swctl (strict flags: -std=c11 -Wall -Wextra -Werror -g -O0 -D_GNU_SOURCE -pthread)
make tsan       # the same with ThreadSanitizer: switch_tsan, host_tsan (+ switch_small_tsan for the tests)
make clean
make test       # the whole test suite (about 8 minutes); see "Tests"
```

## Run

```
./switch <tcp_port> <num_ports> [-c vswitch.conf]
./host   <switch_ip> <tcp_port> <mac> [--noread] [--partial] [--quiet] [--flood N --to <mac>] [--die-midframe]
```

* `tcp_port` 1-65535, `num_ports` 1-64. Hosts take the lowest free port; when all ports are taken a new host
  waits up to `port_wait_timeout` seconds and then receives the "switch full" frame (the `host` program prints
  `SWITCH FULL` and exits with status 2).
* `-c vswitch.conf` names a configuration file (see below). Without it the defaults are used.
* A bad command line, an unreadable configuration file or **any** bad line in it is a start-up error: the
  switch prints the reason (with the line numbers) on stderr and exits with status 1 before it listens.
* `Ctrl+C` (`SIGINT`) or `SIGTERM` shuts the switch down cleanly (exit status 0).

The host program reads commands from stdin: `send <dst_mac|bcast> <text>`, `sendx <dst_mac|bcast> <vlan> <prio> <text>`, `quit`.

## Configuration file

Lines are `key = value`. `#` starts a comment (anywhere in a line, so a file name cannot contain `#`). Blank
lines, spaces and tabs around keys and values and DOS line ends are ignored. Keys are case sensitive. A key
may appear once. Lines are limited to 510 characters.

```
# vswitch.conf
mac_aging_seconds = 300      # reloadable
mac_table_size    = 1024     # startup only
max_macs_per_port = 16       # reloadable
queue_capacity    = 64       # startup only (per priority class)
port_wait_timeout = 5        # reloadable
wrr_weights       = 5,3,1    # reloadable
prio_aging_ms     = 200      # reloadable
rate_limit        = 0        # frames/s for every port, 0 = unlimited; reloadable
trunk_port        = 0        # 0 = none; startup only
log_file          = switch.log
mac_dump_file     = mac_dump.txt
port.3.vlan       = 20       # reloadable
port.3.rate       = 100      # reloadable, overrides rate_limit
```

| key | meaning | allowed values | default | changed by `SIGHUP`? |
|---|---|---|---|---|
| `mac_aging_seconds` | a MAC entry not seen for this long is removed (the aging thread scans every second) | 1 ... 86400 | 300 | yes, takes effect at the next scan |
| `mac_table_size` | number of MAC table entries (K); buckets = 256, or the largest power of two <= K for a small table | 1 ... 65536 | 1024 | no: "ignored until restart" |
| `max_macs_per_port` | port security: a port that learns more MACs than this is shut down (state DISABLED) | 1 ... 65536 | 16 | yes, applies to the next learned MAC |
| `queue_capacity` | frames per output queue, **per priority class** (a port has three queues) | 1 ... 65536 | 64 | no: "ignored until restart" |
| `port_wait_timeout` | seconds a host waits for a free port before "switch full" | 0 ... 3600 | 5 | yes, applies to the next waiting host |
| `wrr_weights` | weighted round robin weights `high,medium,low`, exactly three numbers | each 1 ... 1000 | 5,3,1 | yes, applies at the next scheduling round |
| `prio_aging_ms` | a frame at the head of the low (then the normal) queue that has waited longer than this is sent next | 1 ... 60000 | 200 | yes, applies to the next frame picked |
| `rate_limit` | frames per second for every port, 0 = unlimited (Part 12) | 0 ... 1000000 | 0 | yes, applies at once |
| `trunk_port` | trunk port number, 0 = none (Part 13) | 0 ... `num_ports` | 0 | no: "ignored until restart" |
| `log_file` | the log file (opened with `O_APPEND`) | a file name | `switch.log` | no: "ignored until restart" |
| `mac_dump_file` | where `SIGUSR1` writes the MAC table (opened with `"w"`) | a file name | `mac_dump.txt` | yes |
| `port.N.vlan` | VLAN of port N (Part 13) | N = 1 ... `num_ports`; 1 ... 4094 | not set (VLAN 1) | yes, applies at once |
| `port.N.rate` | frames per second of port N, overrides `rate_limit`; 0 = unlimited (Part 12) | N = 1 ... `num_ports`; 0 ... 1000000 | not set | yes, applies at once |

A key that the file does not mention has its default. The file is checked completely: an unknown key, a
value that is not a plain decimal number, a value out of range, a duplicate key, a line without `=`, a missing
value, a port number outside `1..num_ports` or a line that is too long makes the **whole file invalid**, and
the message lists every bad line, for example

```
switch: configuration file 'v.conf': line 2: unknown key 'bogus'; line 4: wrr_weights needs exactly 3 weights: high,medium,low (2 errors)
```

An environment variable `VSWITCH_LOG_FILE`, if set, overrides `log_file` (the tests use it to give every
switch its own log file).

## Priorities (QoS)

Every frame carries a priority (`0` high, `1` normal, `2` low; the `host` command `sendx <dst> <vlan> <prio> <text>`
sets it, `send` uses 1). Each port has **three output queues**, one per priority, each with room for `queue_capacity`
frames. A frame goes to the queue of its priority; a priority above 2 is not valid and is queued as low priority
(the frame itself is forwarded unchanged), counted per port, with **one** `bad_prio` log line per port and a count
when the port closes.

The sender of a port takes the next frame by **weighted round robin** with the weights `wrr_weights` (default 5,3,1:
with all three queues busy one round sends H H H H H N N N L), plus an **aging override** against starvation: if the
frame at the head of the low queue (then of the normal queue) has waited longer than `prio_aging_ms`, it is sent
next, so a low-priority frame is still sent within about `prio_aging_ms` plus one write even under continuous
high-priority traffic. Fullness is checked per class: a full low queue drops low frames only (counted, logged as
`output queue full`); high and normal frames of the same port are still accepted. The receiver never blocks.

## Rate limiting

`rate_limit` (every port) and `port.N.rate` (one port, overrides `rate_limit`) set the most frames per second that the
**sender** of a port writes to its host; 0 = unlimited. It is a token bucket per port: a frame costs 1000 milli-tokens, the
bucket holds at most `rate * 1000` (a burst of one second, so a host that was idle may receive `rate` frames at once), and a
**token timer thread** adds `rate * 100` every 100 ms (absolute deadlines, no drift; it also counts periods it was late).
The sender of a limited port sleeps on a condition variable until a whole token is there: no polling, so the switch is
near 0 % CPU while a port is throttled. Frames that wait for a token fill the three rings of the port (Part 11); when a
ring is full the receiver of the *other* port drops the frame (counted, logged as `output queue full`): **throttling never
blocks a receiver** and never slows other ports. A reload (`SIGHUP`) applies a changed rate to the connected hosts at
once (the bucket is reset to the new cap); ports whose rate did not change keep their tokens. A host that connects later
gets the rate from the configuration (`port N: rate limit R frames/s` is logged). The priorities of Part 11 are unchanged: a token
is spent on whichever frame the scheduler picks.

## VLANs and the trunk port

Every port is an **access port** in one VLAN (`port.N.vlan`, default 1) except the **trunk port** (`trunk_port`), which carries every
VLAN. The VLAN of a frame is fixed once, when the switch receives it:

* from an access port the frame's VLAN is the port's VLAN: the switch overwrites the VLAN field (`f->vlan` and the two wire bytes 14-15),
  whatever the host wrote;
* from the trunk the frame keeps its VLAN; one outside 1...4094 is dropped, counted (`port N: trunk frame with invalid VLAN V dropped`).

A frame is sent only to ports that are UP, are not the ingress port and are the trunk or in the frame's VLAN; this holds for flooding and for
unicast. The MAC table is keyed by (VLAN, MAC), so a MAC learned in another VLAN is never found: unicast to it floods inside the sender's VLAN and
is never delivered across VLANs. The frame is shared by all output queues (one copy), which is correct because the VLAN is already final: the trunk
host sees the VLAN of the frame, an access host sees its own. The trunk is exempt from `max_macs_per_port`. `SIGHUP` moves a port to its new VLAN at
once (`port_set_vlan`: VLAN set under the write lock, then the port's MAC entries are flushed); a host that connects later gets the VLAN from the
configuration. `trunk_port` changes only at start-up.

## Counters in shared memory

The per-port counters live in the POSIX shared-memory object **`/vswitch_stats_P`** (`ls -l /dev/shm` shows `vswitch_stats_P` while the
switch runs and not after it stopped), so that another process can read them. `swctl stats` reads them (Part 15).

| offset | structure | content |
|---|---|---|
| 0 | `stats_ctl_t` (padded to one page) | `uint32_t magic` (`0x56535453`, written last), `uint32_t version` (1), `int num_ports`, `pthread_mutex_t lock` |
| `PAGE` | `stats_data_t` | `port[MAX_PORTS + 1]`, each `{uint64_t in, out, dropped, mirrored; int state, vlan, mirror_to;}` |

`PAGE` is `sysconf(_SC_PAGESIZE)` (4096 on x86-64); the object is `PAGE + round_up(sizeof(stats_data_t), PAGE)` bytes (8192), mode **0600** (same user
only). `in` counts frames received from the host of the port, `out` frames written to it, `dropped` frames lost because its queue was full (and
invalid trunk frames), `mirrored` counts the mirrored copies queued for a sniffer (Part 17, counted on the mirrored port). `state` (0 FREE, 1 UP, 2 CLOSING, 3 DISABLED), `vlan` and `mirror_to` are copied in whenever
the port changes, for `swctl show ports` (Part 15).

The lock is a **process-shared, robust** mutex that only the switch initialises, before it writes the magic. Every counter update takes it (it is the last
lock of the global lock order). If a process dies holding it, the next locker gets `EOWNERDEAD`, calls `pthread_mutex_consistent` and goes on, so a
crashed reader never wedges the switch. A reader must check `magic` and `version` (`stats_validate`) and lock with `stats_lock`.

Start-up creates the object with `O_CREAT | O_EXCL` (a stale one left by a crash is unlinked first), sizes it with `ftruncate`, maps it with `mmap` and
publishes the magic last; any failure removes the object again. A switch that cannot start (TCP port in use, bad file) never touches the object of a
running switch. Shutdown: `shm_unlink` first (nobody can open it any more), the magic is cleared, the mutex destroyed, then `munmap`; a process that mapped the
object earlier keeps its memory until it exits and sees `magic == 0`. A switch killed with `SIGKILL` leaves the object behind; the next start replaces it.
The name is fixed, so one switch per machine and user owns it: a second switch that does start replaces the first one's object (the first keeps
working on its own mapping but is no longer visible).

## Admin socket and `swctl` (Part 15)

An **admin thread** inside the switch listens on the UNIX-domain stream socket **`/tmp/vswitch_<tcp_port>.sock`** (mode 0600; a stale path is
unlinked at start-up; `ss -xlp` shows it; it is removed at shutdown). It serves one client at a time. **Protocol:** the client sends one line ending in
`\n` (at most 256 bytes including the newline, 2 s in total to send it); the switch replies with text lines, the last one `OK` or `ERR <reason>`, and
closes. The wait for a client, for the request and for the reply is a `poll()` on the socket and on `wake_efd`, so `SIGINT` ends the thread at once even while
a client is silent; no lock is held during socket I/O.

```
./swctl [-p tcp_port] <command>          # default tcp_port 9000
  show mac                  the MAC table (vlan, mac, port, age), sorted
  show ports                state, VLAN, trunk, number of MACs, mirror target of every port
  clear mac                 empty the MAC table (unknown destinations flood until relearned)
  stats                     per-port counters (in, out, dropped, mirrored) from the shared memory
  stats --watch             the same every whole second, until SIGINT
  shutdown <port> | up <port> | vlan <port> <vlan> | mirror <port> <sniffer> | mirror <port> off
```

Exit status: **0** `OK`, **1** `ERR` (also bad arguments and system errors), **2** the switch is not running (connect fails with `ENOENT` or
`ECONNREFUSED`, or no shared-memory object: message `switch not running`).

* **Done:** `show mac`, `show ports`, `clear mac`, `stats`, `stats --watch` (Part 15) and `shutdown`, `up`, `vlan` (Part 16, below).
* **Port mirroring (Part 17):** `mirror <port> <sniffer>` sends a copy of **all** traffic of `<port>` to `<sniffer>`: every frame received on the port and every frame
  queued to it (both directions). The copy is the same shared frame (no copy of the data), the VLAN filter does not apply to it (the sniffer may be in its own VLAN) and it is
  never sent back to the frame's own ingress port. Both ports must have a host (UP), the ports must differ and the sniffer must not be the trunk. `mirror <port> off` stops it (OK
  also if it was off). The copy goes into the sniffer's own bounded queue: a sniffer that is slow or never reads only loses copies (counted as `dropped` on the sniffer's port); it never
  slows the mirrored port. `swctl stats` shows `MIRRORED` (copies queued, counted on the mirrored port) and `show ports` the target in the `MIRROR` column. Mirroring ends by itself
  (and is logged) when the mirrored host or the sniffer goes away, also by `swctl shutdown`; a new host on those ports is not mirrored. There is no de-duplication: a sniffer that
  is itself in the VLAN of the mirrored traffic gets a flooded frame twice (normally and as a copy); give it its own VLAN. Frames that the switch drops before forwarding (broadcast
  source, invalid trunk VLAN, port-security violation) are not mirrored.
* `./host <ip> <port> <mac> --sniff` is a plain host that only prints (`RX src=... dst=... vlan=... data=...` per frame): it sends nothing, reads no stdin, and ends when the switch
  closes the connection or on a signal.

**Live reconfiguration (Part 16), safe under traffic:**
* `shutdown <port>`: an UP port is torn down by the reaper (the host is disconnected, its MAC entries are flushed) and ends **DISABLED**; the command answers
  `OK` only after that (at most 5 s, else `ERR timeout ...`). A FREE port becomes DISABLED at once and leaves the free-port semaphore. A DISABLED or CLOSING port
  answers `ERR` (only one of several simultaneous shutdowns, or of a shutdown and a host disconnect, wins the UP -> CLOSING transition).
* `up <port>`: DISABLED -> FREE (and back into the semaphore); any other state is an `ERR`. Port security starts again from 0.
* `vlan <port> <vlan>`: any state. A connected port moves at once and its MAC entries of the old VLAN are flushed; a FREE or DISABLED port keeps the VLAN for its next
  host. The trunk is refused. A configuration reload that changes `port.N.vlan` replaces what `swctl` set; an unrelated reload keeps it.
* `clear mac`: the table is emptied while the receivers keep running; their next frames flood and relearn.
The locks involved and why this cannot deadlock are in `notes/locks.md`; the design is in `docs/Part16_Explanation.txt`.

`stats` does not use the socket. `swctl` opens `/vswitch_stats_P` with `O_RDWR` and makes **two** mappings of it: the control page (offset 0, `PROT_READ|PROT_WRITE`:
locking a mutex writes to it) and the data area (offset `PAGE`, `PROT_READ`: the counters cannot be changed by `swctl`, a write is a `SIGSEGV`). It checks
magic and version, locks the robust mutex (`EOWNERDEAD` -> `pthread_mutex_consistent`), copies the counters, unlocks, and only then prints.
`show ports` shows the VLAN and role of the configuration for a port that has no host yet (the switch applies them when a host connects).

## Signals

| signal | effect |
|---|---|
| `SIGINT`, `SIGTERM` | clean shutdown: every thread is stopped and joined, every port torn down, all resources released, exit status 0 |
| `SIGHUP` | reload the configuration file given with `-c` (see below) |
| `SIGUSR1` | write the MAC table to `mac_dump_file` |

The signals are blocked in every thread and collected by one dedicated thread with `sigwait()`; there are no
signal handlers.

### Reloading: `kill -HUP <pid>`

The file is read into a temporary configuration first.

* If it cannot be read or any line is bad, the log shows an error and **nothing changes**:
  `[ERR] reload failed, line 3: unknown key 'bogus' (1 error); the previous configuration stays in force`.
* Otherwise the reloadable settings are replaced, every change is logged
  (`reload: mac_aging_seconds 300 -> 5 ...`), settings that only take effect at start-up and differ from the
  running ones are reported as `... ignored until restart`, and a summary line ends the reload:
  `configuration reloaded from 'vswitch.conf': 2 settings changed (mac aging 5s)`.
* Keys that the file no longer mentions go back to their defaults (the file describes the whole configuration).
* A switch started without `-c` has no file: `SIGHUP` only logs that there is nothing to reload.
* Signals of one kind that arrive while the previous one is being handled are merged by the kernel; the last
  state of the file wins.

### Dumping the MAC table: `kill -USR1 <pid>`

`mac_dump_file` is created or truncated (mode `"w"`) and contains

```
# MAC table dump, 2026-11-17 19:20:16
# vlan mac port age_s
1 02:00:00:00:00:10 1 12
100 02:00:00:00:00:11 2 0
```

one line per entry: VLAN, MAC, port and the age in whole seconds since the MAC was last seen. The table is
copied one bucket at a time (a bucket's lock is held only while its entries are copied; no lock is held while
writing the file), so the dump is correct for every entry but is not one atomic snapshot of a table that is
changing under traffic. The log reports the number of entries or the error
(`[ERR] SIGUSR1: cannot write the MAC table to '...': Permission denied`).

## Log

`switch.log` (or `log_file`), one line per event: `YYYY-MM-DD HH:MM:SS.mmm [INFO|WARN|ERR] message`, with
connects, disconnects, MAC moves, port security events, reloads and dumps. The log is written by a separate
logger thread from a bounded queue of 1024 records; when a flood produces lines faster than the file can take
them, lines are **dropped** and counted (`N log messages dropped`) so that logging never slows the switch down.

## Tests

```
make test                       # ./tests/run_all.sh: the unit and regression tests of all parts + the ten integration scripts
VARIANT=valgrind ./tests/run_all.sh    # the integration scripts with the switch under valgrind
VARIANT=tsan ./tests/run_all.sh        # ... with ThreadSanitizer
python3 tests/test_config_signals.py [tsan|valgrind]   # configuration, SIGHUP, SIGUSR1 (Part 10)
./build/test_qos                # the priority queue scheduler (after `make build/test_qos`)
python3 tests/test_qos.py [tsan|valgrind]              # priorities through a congested port (Part 11)
./build/test_rate               # the token bucket (after `make build/test_rate`)
python3 tests/test_rate.py [tsan|valgrind]             # rate limiting with the real switch (Part 12)
./build/test_vlan               # VLAN forwarding and the trunk, no sockets (after `make build/test_vlan`)
python3 tests/test_vlan.py [tsan|valgrind]             # VLANs with the real switch (Part 13)
./build/test_stats              # shared-memory counters, layout, robust mutex, stale segments (after `make build/test_stats`)
python3 tests/test_shm.py [tsan|valgrind]              # the segment of the real switch: counters, fields, shutdown (Part 14)
./build/test_admin              # the admin command parser (after `make build/test_admin`)
./build/test_swctl              # swctl against a segment: read-only data mapping, robust mutex (after `make build/test_swctl`)
python3 tests/test_admin.py [tsan|valgrind]            # admin socket and swctl with the real switch (Part 15)
python3 tests/test_reconfig.py [tsan|valgrind]         # shutdown / up / vlan / clear mac with the real switch (Part 16)
./build/test_mirror                                    # port mirroring without sockets (after `make build/test_mirror`)
python3 tests/test_mirror.py [tsan|valgrind]           # port mirroring with the real switch and sniffers (Part 17)
./tests/reconfig_stress.sh                             # 500 random admin operations + SIGHUP/SIGUSR1 every 0.2 s under traffic (Part 16; VARIANT=tsan|valgrind too)
./build/test_config             # the configuration parser (after `make build/test_config`)
```

See `docs/PartN_Explanation.txt` for the design and the test results of each part and `notes/bugs.md` for the bugs found.
