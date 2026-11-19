# Locks of the Virtual Switch: the order, what each lock protects (roadmap 2.6, Part 16)

## The global lock order

Take locks only in this order (a lower number first). A thread that holds a lock of a higher number never takes a lock of a lower number.

| # | lock | kind |
|---|---|---|
| 1 | `sw.ports_lock` | `pthread_rwlock_t`, writer-preferring (`PTHREAD_RWLOCK_PREFER_WRITER_NONRECURSIVE_NP`) |
| 2 | `port[i].q.lock` | mutex; at most one held at a time (if two are ever needed: lowest port number first) |
| 3 | MAC bucket locks (`bucket[b].lock`) | robust mutexes; lowest bucket first; the normal path holds only one |
| 4 | `sw.reaper_lock` | mutex |
| 5 | frame `ref_lock` | mutex of the frame pool / reference counts |
| 6 | logger queue lock | mutex |
| 7 | stats mutex (in shared memory) | `PTHREAD_PROCESS_SHARED`, robust; ALWAYS LAST; `log_msg` is never called while it is held |

Leaf rules (locks that are never held together with another lock, or only as the very last one):
`config_lock` is taken alone: the configuration is copied out (`config_snapshot`) and the lock released. `shutdown_lock` and `port_down_lock`
are leaves: each is held alone, `request_shutdown()` releases `shutdown_lock` before it takes `port_down_lock`. The `free_ports` semaphore is
not a lock (its operations are non-blocking where a lock is held; the only blocking wait, `sem_timedwait`, holds no lock).
**No lock is held across blocking I/O, `pthread_join`, `sem_timedwait` or a condition wait on another lock's behalf.**

## What each lock protects

| lock | protects | notes |
|---|---|---|
| `sw.ports_lock` | `port[i].state`, `final_state`, `vlan`, `vlan_admin`, `trunk`, `rfd/wfd`, `rx_started`, the transitions of a port; the port slots while a port is started or reset | read: the receivers (`forward_frame`), `show ports`, the token timer; write: `port_start`, every state change (`request_down_locked`, `admin_shutdown/up/vlan`), the last step of a teardown |
| `port[i].q.lock` (+ `q.cond`) | the three priority rings of one output queue, the token bucket (`tokens_milli`, `rate`), `closed` | one queue lock at a time; the sender waits on `q.cond` (no other lock held) |
| MAC bucket lock | the hash chain of one bucket, its entries, the LRU/free-list links that belong to it | robust: the owner may die in Part 20 (`EOWNERDEAD` -> `pthread_mutex_consistent`) |
| `sw.reaper_lock` (+ `reaper_cond`) | the ring of port ids waiting for teardown, `reaper_stop` | taken while `ports_lock` is held (rank 4 after rank 1) when a port is queued; the reaper itself holds it only to pop |
| frame `ref_lock` | the reference count of the frames (`frame_ref` / `frame_unref`), the frame pool | last but one: a frame is unreferenced while queue locks were just released |
| logger queue lock | the ring of log records, the stop flag | `log_msg` takes it; it may be called while any lock of rank 1-5 is held, never with rank 7 |
| stats mutex | the counters and the port fields of the shared segment (`/vswitch_stats_P`) | process-shared and robust; swctl locks it too; always the last lock taken |
| `sw.config_lock` | `sw.cfg`, and the atomics copied from it at the same time | leaf; copied out, then released |
| `sw.shutdown_lock` (+ `shutdown_cond`) | `shutdown_requested` (THE predicate) | leaf; every wait tests the predicate in a loop; `shutting_down` is only a lock-free hint |
| `sw.port_down_lock` (+ `port_down_cond`) | `port_down_gen[]`: teardowns completed per port | leaf; the reaper bumps it, `admin_shutdown` waits on it (Part 16); `request_shutdown()` broadcasts it so that no admin command delays the shutdown |
| `free_ports` semaphore | the number of free port tokens | invariant: value + the token held by the accept thread = number of FREE ports |

## Why the order prevents deadlock

* Every path that needs two locks takes them in the order above, so a cycle (A waits for B while B waits for A) cannot form.
* The receivers hold only the `ports_lock` READ lock and short leaf locks (one queue lock, one bucket lock); they never block while holding them: a full
  queue is a drop, not a wait. So a writer waiting for `ports_lock` is never blocked for long, and the lock is writer-preferring, so a steady stream of
  readers cannot starve it either (the admin thread, the reaper and the accept thread are writers).
* The admin thread and the reaper NEVER hold a lock while they join a thread or wait: `admin_shutdown` requests the teardown under the write lock,
  releases it, then waits on `port_down_cond` (a leaf lock) with a 5 s limit; the reaper joins the port's threads with no lock held and only then takes
  the write lock for the last step.
* `config_lock`, `shutdown_lock` and `port_down_lock` are leaves: nothing is acquired while they are held.

## The Part 16 operations and their locks

| operation | locks, in order |
|---|---|
| `shutdown` of an UP port | write `ports_lock` (UP -> CLOSING, queue the port: + `reaper_lock`), release; wait on `port_down_lock` (leaf) with no other lock; read `ports_lock` to look at the state |
| `shutdown` of a FREE port | write `ports_lock`: FREE -> DISABLED, `sem_trywait` (non-blocking), `stats` (last), release |
| `up` | write `ports_lock`: DISABLED -> FREE, `stats`, release; then `sem_post` (no lock) |
| `vlan` | write `ports_lock` (+ `config_lock` as a leaf, + `stats`), release; then `mac_flush_port` (bucket locks, one at a time, no other lock) |
| `clear mac` | `mac_clear`: the bucket locks one at a time, ascending, no other lock |
| teardown (reaper) | no lock for the joins; `mac_flush_port` (bucket locks); write `ports_lock` for the reset and the state; `port_down_lock` (leaf) for the notification |
| `mirror <x> <s>` / `mirror <x> off` | write `ports_lock`: both states checked, `mirror_to` stored (atomic), `config_lock` (leaf) for the trunk test, `stats` (last), release |
| mirroring in `forward_frame` | under the `ports_lock` READ lock the receiver reads `mirror_to` (atomic), checks that the sniffer is UP and takes one queue lock at a time (`outq_try_push`, never blocks); the drop is counted with `stats` (last) |
| clearing the references (reaper, last step of a teardown) | the same write-locked step that changes the state of the port: every `mirror_to` equal to the port is set to 0, so nothing can queue to a port that is not UP |

## Part 19: port 0 (the pipe trunk to the peer switch) adds no lock

Port 0 is an ordinary `port_t` (index 0 of the static array): the same `ports_lock`, queue lock, reaper, `stats` lock and `port_down_lock` protect the same data in the same
order. What is new is only how its two threads are woken: `shutdown(fd)` does not work on a pipe, so the reaper writes the port's eventfd (`wake_fd`, no lock needed) and the receiver
and sender, which wait in `poll()` on `{pipe, wake_fd}` with no lock held, return. `port_attach_pipe` (startup, before accept) takes the `ports_lock` write lock exactly like
`port_start`. Port 0 is never in the free-port semaphore, so `sem_post` / `sem_trywait` are never called for it. The fork happens before any thread or lock is created, so no lock
is ever copied into the child in a held state.
