# Virtual Switch - Report

A multithreaded software Ethernet switch in C (hosts are TCP connections; frames are the 17-byte-header frames of the specification). Build, run, options, the configuration file and
every file / shared-memory name are in `README.md`. Everything below describes the code as committed.

## 1. Threads

```
                         +--------------------- main ----------------------+
                         | start-up, then waits on shutdown_cond; then the   |
                         | shutdown sequence (stop, wake, join, free)        |
                         +---------------------------------------------------+
  TCP listener                                                          UNIX socket /tmp/vswitch_<port>.sock
      |                                                                          |
  [accept] --token from free_ports--> port i  ==  [receiver i] ---> forward ---> [sender j] --> host j      (one pair per UP port)
      |        (sem_timedwait)                       reads frames,    (learn,       dequeues (WRR + aging +
      |                                              learns, looks     lookup,      token), writes to its
      |                                              up, enqueues;     enqueue      own socket
      |                                              never blocks      to q[j])
      |
  [reaper]  tears ports down (the ONLY thread that joins rx/tx, closes the fd, flushes the port's MACs)
  [aging]   removes MAC entries older than mac_aging_seconds (pthread_cond_timedwait, absolute deadline)
  [token timer]  adds tokens to the rate-limit buckets every 100 ms (same style of wait)
  [signal]  sigwait loop: SIGINT/SIGTERM -> shutdown, SIGHUP -> reload, SIGUSR1 -> MAC dump (all signals blocked everywhere else)
  [admin]   serves swctl on the UNIX socket, one client at a time (poll on socket + eventfd)
  [logger]  writes log records from a bounded queue to the log file (the last thread to stop)
```

| thread | count | job |
|---|---|---|
| main | 1 | start-up; waits on the shutdown condition; runs the shutdown sequence |
| accept | 1 | accepts connections; waits (`sem_timedwait`, `port_wait_timeout`) for a free port, else sends "switch full" |
| receiver | 1 per UP port | reads frames, learns the source, looks up the destination, enqueues; never writes to another socket and never blocks on a queue (a full queue is a counted drop) |
| sender | 1 per UP port | takes the next frame by weighted round robin with aging, waits for a rate-limit token, writes to its own socket |
| reaper | 1 | the only thread that tears a port down; joins the port's threads with no lock held |
| aging | 1 | expires MAC entries |
| token timer | 1 | refills the token buckets |
| signal | 1 | `sigwait` for SIGINT, SIGTERM, SIGHUP, SIGUSR1 |
| admin | 1 | serves `swctl` |
| logger | 1 | writes the log |

The helper threads never `sleep()`; they wait on a condition with an absolute deadline and wake at once when the shutdown starts (a source check, `tests/static_checks.py`, enforces this).
With `--pair` a second process (switch B) has the same threads, and port 0 of each switch is served by an ordinary receiver / sender pair on a pipe (woken by an eventfd, because a pipe cannot be
`shutdown()`). The supervisor (Part 20) is a separate single-threaded process.

## 2. Locks and the data each protects

| lock | protects |
|---|---|
| `sw.ports_lock` (rwlock, writer-preferring) | state, VLAN, trunk flag, descriptors of every port and the transitions between states; read by receivers, written by accept, reaper and admin |
| `port[i].q.lock` (+ condvars) | the three priority rings of one output queue, the token bucket, `closed`; one queue lock at a time |
| bucket locks (256, robust, process-shared) | the hash chain of one bucket, its entries and their LRU links; one held at a time |
| `pool_lock` (MAC table) | the free list, the `used` flags and the entry count |
| frame `ref_lock` | the reference count of one frame (every frame has its own); the last unreference frees it |
| `sw.reaper_lock` (+ cond) | the ring of port numbers waiting for teardown |
| `sw.config_lock` | the running configuration; leaf: copied out (`config_snapshot`), then released |
| `sw.shutdown_lock` (+ cond) | `shutdown_requested`, the predicate of every shutdown wait; leaf |
| logger queue lock (+ cond) | the ring of log records and the stop flag |
| stats mutex (in shared memory, `PTHREAD_PROCESS_SHARED`, robust) | the per-port counters read by `swctl`; always the last lock; `swctl` takes it too, and a dead holder is recovered (`EOWNERDEAD`) |
| `free_ports` semaphore | the number of free ports (value + the token held by the accept thread = number of FREE ports); not a lock: its only blocking wait, `sem_timedwait`, holds no lock |
| `sw.port_down_lock` (+ cond) | per-port count of completed teardowns, for `swctl shutdown` to wait on; leaf |

The two bonus strategies of the MAC table (Part 21, `-DMAC_LOCK=GLOBAL|RWLOCK`) replace the bucket locks by one mutex / one rwlock; the order below is the same.

## 3. The lock order and why it prevents deadlock

`ports_lock` -> `q.lock` -> bucket lock -> `pool_lock` -> `reaper_lock` -> frame `ref_lock` -> logger lock -> stats mutex (always last, and `log_msg` is never called while it is held).
Leaf locks (`config_lock`, `shutdown_lock`, `port_down_lock`) are taken alone. **No lock is held across blocking I/O, `pthread_join`, `sem_timedwait` or a wait on another lock's behalf.**

A deadlock needs a cycle of threads each waiting for a lock the next one holds. Every path that holds two locks takes them in the order above, so a wait always goes from a lower to a
higher rank and no cycle can close. The rest of the argument is that nobody waits for long while holding a lock: the receivers hold only the `ports_lock` *read* lock and one short leaf lock
at a time and never block (a full queue is a drop); the lock is writer-preferring, so the writers (accept, reaper, admin) are not starved by a stream of readers; the reaper joins threads with
no lock held and takes the write lock only for the last step; the admin thread requests a teardown under the write lock, releases it, then waits on a leaf condition. Full details and the
locks of every Part 16-21 operation: `notes/locks.md`. The order is exercised by `reconfig_stress.sh` (500 random admin operations plus `SIGHUP` / `SIGUSR1` under traffic) under TSan and valgrind.

## 4. One real bug: a teardown that processed the socket backlog (notes/bugs.md item 31)

* **Symptom.** Under valgrind, `swctl shutdown <port>` of a port whose host was flooding answered `ERR timeout waiting for the port to shut down` (the 5 s limit), and `SIGINT` with flooding
  hosts took 12 s (3000-frame backlog) to 34 s (30000 frames) to finish. The log showed thousands of `RX` lines *after* `shutdown: started`.
* **How it was found.** `tests/reconfig_stress.sh` run with `VARIANT=valgrind`. A dedicated test (`tests/test_backlog.py`) then measured it without valgrind too: **23902 frames were forwarded
  after the teardown was requested** in the normal build.
* **Cause.** `shutdown(fd, SHUT_RDWR)` wakes a blocked `read` but does not discard the frames already in the kernel's receive buffer. The receiver went on reading and forwarding that whole backlog;
  the slower the switch (valgrind, TSan) the longer the reaper's `pthread_join` waited.
* **Fix.** `port_t.closing`, an `_Atomic bool` set when the UP -> CLOSING transition is won (`request_down_locked`) and cleared in `port_reset`. The receiver tests it at the top of its loop and again after
  each frame it has read (that frame is dropped), so it stops at once.
* **Check.** `test_backlog.py` (normal, TSan, valgrind): `SIGINT` with four flooders exits quickly with at most about 40 frames per port forwarded after the request, and `swctl shutdown` of a flooding
  port answers `OK` well inside 5 s. Before the fix 1 failure (normal) and 3 (valgrind); after it 21 checks, 0 failures in all three variants.

(A second real defect, found by TSan in Part 16: a data race in `admin_up`, which read `p->state` after releasing `ports_lock`; fixed by copying it under the lock: item 28.)

## 5. Completed items

**Tier 1 (Parts 1-8):** the wire protocol with partial reads / writes, mid-frame disconnect, SIGPIPE ignored, EPIPE handled; per-port bounded queues with a sender thread, drop-and-count, a receiver that never
blocks; reference-counted shared broadcast frames; the concurrent MAC table (striped bucket locks, aging thread, "MAC moved" log, K limit with LRU, port security); the free-port semaphore with `sem_timedwait` and
the "switch full" reply; the signal thread and clean `SIGINT` shutdown. Checkpoint of Part 8 (tag `tier2` marks the later Tier 2 checkpoint).

**Tier 2 (Parts 9-18):** the asynchronous logger thread; the configuration file, `SIGHUP` reload and `SIGUSR1` MAC dump; QoS (three priority queues, weighted round robin 5:3:1, aging against starvation);
rate limiting (token bucket, timer thread); VLANs and the trunk port; shared-memory counters with a process-shared robust mutex; the admin socket and `swctl` (all commands, `stats --watch`);
live reconfiguration (`shutdown`, `up`, `vlan`, `clear mac`); port mirroring; the checkpoint with the 19 integration scripts, the hostile-host simulation and the source checks.

**Tier 3 / bonus (Parts 19-21):** two switches joined by a two-pipe trunk with cross-switch learning (`--pair`); the supervisor with a MAC table that survives a crash in shared memory (`--persist-mac`); the benchmark
of global vs striped vs rwlock locking with a graph and a one-page report (`bench/report.pdf`).

**Verification:** `make test`, `make test-tsan` and `make test-valgrind` all pass on a clean tree (see `docs/Parts_16_to_20_Completion_Report.md` section 10 and `docs/Part22_Final_Check.md`).
Known limits: the roadmap v2 PDF was not available (v1 used); `test_host.py` (Part 2) and `test_qos.c` (Part 11) are load-sensitive and can fail spuriously on a busy 2-core machine (items 36 and 38).
