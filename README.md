# Virtual Switch

A multithreaded software network switch for Linux, written in C. Hosts connect over TCP; the switch learns
which MAC address is behind which port, forwards frames only where they have to go, floods unknown
destinations and broadcasts, and manages its ports, MAC table, logging, signals and configuration.

This README describes the switch as of **Part 10** (configuration file, `SIGHUP` reload, `SIGUSR1` MAC dump).
Features of later parts (priority queues, rate limiting, VLAN switching, the admin socket, shared-memory
statistics, port mirroring) are not implemented yet; where the configuration file already accepts their keys
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
| `queue_capacity` | frames per output queue (per priority class) | 1 ... 65536 | 64 | no: "ignored until restart" |
| `port_wait_timeout` | seconds a host waits for a free port before "switch full" | 0 ... 3600 | 5 | yes, applies to the next waiting host |
| `wrr_weights` | weighted round robin weights `high,medium,low`, exactly three numbers | each 1 ... 1000 | 5,3,1 | yes (*) |
| `prio_aging_ms` | aging time of low priority frames (Part 11) | 1 ... 60000 | 200 | yes (*) |
| `rate_limit` | frames per second for every port, 0 = unlimited (Part 12) | 0 ... 1000000 | 0 | yes (*) |
| `trunk_port` | trunk port number, 0 = none (Part 13) | 0 ... `num_ports` | 0 | no: "ignored until restart" (*) |
| `log_file` | the log file (opened with `O_APPEND`) | a file name | `switch.log` | no: "ignored until restart" |
| `mac_dump_file` | where `SIGUSR1` writes the MAC table (opened with `"w"`) | a file name | `mac_dump.txt` | yes |
| `port.N.vlan` | VLAN of port N (Part 13) | N = 1 ... `num_ports`; 1 ... 4094 | not set (VLAN 1) | yes (*) |
| `port.N.rate` | frames per second of port N, overrides `rate_limit`; 0 = unlimited (Part 12) | N = 1 ... `num_ports`; 0 ... 1000000 | not set | yes (*) |

(*) **These settings are validated, kept in the live configuration and logged when they change, but they have
no effect yet**: the priority queues (Part 11), rate limiting (Part 12) and VLAN switching (Part 13) are not
implemented. The log says so ("stored; ... not applied until Part N") instead of pretending.

A key that the file does not mention has its default. The file is checked completely: an unknown key, a
value that is not a plain decimal number, a value out of range, a duplicate key, a line without `=`, a missing
value, a port number outside `1..num_ports` or a line that is too long makes the **whole file invalid**, and
the message lists every bad line, for example

```
switch: configuration file 'v.conf': line 2: unknown key 'bogus'; line 4: wrr_weights needs exactly 3 weights: high,medium,low (2 errors)
```

An environment variable `VSWITCH_LOG_FILE`, if set, overrides `log_file` (the tests use it to give every
switch its own log file).

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
./build/test_config             # the configuration parser (after `make build/test_config`)
```

See `docs/PartN_Explanation.txt` for the design and the test results of each part and `notes/bugs.md` for the bugs found.
