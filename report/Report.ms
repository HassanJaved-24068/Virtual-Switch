.\" Virtual Switch - report (2-3 pages). Build: report/build.sh (groff -ms | pic | tbl -> ps2pdf)
.\" page setup (no ms macros are installed: the few macros used are defined here)
.pl 11i
.po 0.55i
.ll 7.4i
.ps 8.6
.vs 10.2p
.de HD
'sp 0.45i
..
.de FT
'bp
..
.wh 0 HD
.wh -0.45i FT
.de LP
.br
.in 0
.sp 0.3v
..
.de BU
.br
.sp 0.2v
.in 1.4m
.ti -1.4m
\(bu\h'0.7m'\c
..
.de B
\fB\\$1\fP\\$2
..
.de H1
.br
.in 0
.sp 0.55v
.ne 4
.ps 10
.vs 11.5p
\fB\\$1\fP
.br
.ps 8.6
.vs 10.2p
..
.ce
.ps 14
.vs 16p
\fBVirtual Switch \(em Report\fP
.br
.ps 8.6
.vs 10.2p
.ce
Operating Systems, Fall 2026 \(en multithreaded software network switch in C (Linux, pthreads, POSIX shared memory)
.sp 0.2v
.LP
Build and run instructions, every option and the configuration file are in \f(CWREADME.md\fP. This report describes the code as submitted; every result stated here was
produced by a test run listed in section 6 (the official assignment PDF was not available on the development machine, the implementation roadmap derived from it was the specification).
.H1 "1. Threads"
.PS 7.2
boxht = 0.50; boxwid = 0.98
H1: box "host i" "(TCP client)"
arrow right 0.2
R: box "receiver i" "1 per UP port"
arrow right 0.2
F: box "forward" "learn \(bu lookup" "(runs in receiver)"
arrow right 0.2
Q: box "output queue j" "3 priority rings"
arrow right 0.2
S: box "sender j" "1 per UP port"
arrow right 0.2
H2: box "host j" "(TCP client)"
A: box "accept" "1 thread" wid 1.0 with .nw at H1.sw + (0,-0.3)
RP: box "reaper" "1 thread" wid 1.0 with .w at A.e + (0.12,0)
AG: box "aging" "1 thread" wid 1.0 with .w at RP.e + (0.12,0)
TT: box "token timer" "1 thread" wid 1.0 with .w at AG.e + (0.12,0)
SG: box "signal" "1 thread" wid 1.0 with .w at TT.e + (0.12,0)
AD: box "admin" "1 thread" wid 1.0 with .w at SG.e + (0.12,0)
LG: box "logger" "1 thread" wid 1.0 with .w at AD.e + (0.12,0)
"main (1 thread): start-up, then waits on shutdown_cond; then stops, wakes and joins every thread" at A.sw + (0,-0.2) ljust
.PE
.TS
center box tab(|);
lw(0.8i) lw(0.55i) lw(5.45i).
\fBthread\fP|\fBcount\fP|\fBjob\fP
_
main|1|T{
start-up (strict argument parsing, shared memory, threads); waits on \f(CWshutdown_cond\fP; runs the shutdown sequence (stop, wake, join, free)
T}
accept|1|T{
\f(CWaccept()\fP, takes a token from the \f(CWfree_ports\fP semaphore (\f(CWsem_timedwait\fP, \f(CWport_wait_timeout\fP), starts a port or answers "switch full"
T}
receiver|1/UP port|T{
reads frames (partial reads, EOF mid-frame), learns the source MAC, looks up the destination, enqueues; never writes to another socket, never blocks on a queue (a full ring is a counted drop)
T}
sender|1/UP port|T{
takes the next frame by weighted round robin (5:3:1) with aging, waits for a rate-limit token, writes to its own socket only
T}
reaper|1|T{
the only thread that tears a port down: joins its receiver and sender (no lock held), closes the fd, flushes its MAC entries, frees the slot
T}
aging|1|T{
removes MAC entries older than \f(CWmac_aging_seconds\fP (absolute-deadline \f(CWpthread_cond_timedwait\fP on \f(CWshutdown_cond\fP; never \f(CWsleep\fP)
T}
token timer|1|T{
adds tokens to every rate-limit bucket every 100 ms (same kind of wait)
T}
signal|1|T{
\f(CWsigwait\fP loop: SIGINT/SIGTERM \(-> shutdown, SIGHUP \(-> reload configuration, SIGUSR1 \(-> MAC dump; every other thread blocks all signals
T}
admin|1|T{
serves \f(CWswctl\fP on \f(CW/tmp/vswitch_<port>.sock\fP, one client at a time (\f(CWpoll\fP on the socket and an eventfd, so shutdown never waits for a client)
T}
logger|1|T{
writes records from a bounded queue (1024) to the log file; drops and counts when full; the last thread to stop
T}
.TE
.LP
Bonus: with \f(CW--pair\fP a second process (switch B) has the same threads; port 0 of each switch is an ordinary receiver/sender pair on a pipe, woken by an eventfd (a pipe cannot be
\f(CWshutdown()\fP). The supervisor is a separate single-threaded process (\f(CWpoll\fP on a \f(CWsignalfd\fP).
.H1 "2. Locks and the data each protects"
.TS
center box tab(|);
lw(1.7i) lw(4.9i).
\fBlock\fP|\fBprotects\fP
_
T{
\f(CWsw.ports_lock\fP (rwlock, writer-preferring)
T}|T{
state, VLAN, trunk flag and descriptors of every port and the transitions between states (read: receivers, token timer, \f(CWshow ports\fP; write: accept, reaper, admin)
T}
T{
\f(CWport[i].q.lock\fP (+ condvars)
T}|T{
the three priority rings of one output queue, its token bucket, \f(CWclosed\fP; one queue lock at a time
T}
T{
bucket locks (256, robust, process-shared)
T}|T{
the hash chain of one MAC bucket, its entries and LRU links; one at a time
T}
T{
\f(CWpool_lock\fP (MAC table)
T}|T{
the free list, the \f(CWused\fP flags and the entry count
T}
T{
\f(CWsw.reaper_lock\fP (+ cond)
T}|T{
the ring of port numbers waiting for teardown
T}
T{
frame \f(CWref_lock\fP
T}|T{
the reference count of ONE frame (each frame has its own); the last \f(CWframe_unref\fP frees it
T}
T{
logger queue lock (+ cond)
T}|T{
the ring of log records and the stop flag
T}
T{
stats mutex (shared memory, \f(CWPTHREAD_PROCESS_SHARED\fP, robust)
T}|T{
the per-port counters read by \f(CWswctl\fP (which locks it too; a dead holder gives \f(CWEOWNERDEAD\fP and is recovered)
T}
T{
\f(CWsw.config_lock\fP
T}|T{
the running configuration (copied out, then released)
T}
T{
\f(CWsw.shutdown_lock\fP (+ cond)
T}|T{
\f(CWshutdown_requested\fP, the predicate of every shutdown wait
T}
T{
\f(CWsw.port_down_lock\fP (+ cond)
T}|T{
per-port count of completed teardowns, for \f(CWswctl shutdown\fP to wait on
T}
T{
\f(CWfree_ports\fP semaphore
T}|T{
the number of free ports (value + the token held by accept = number of FREE ports); not a lock
T}
.TE
.H1 "3. Lock order and why it prevents deadlock"
.LP
.B "ports_lock \(-> q.lock \(-> bucket lock \(-> pool_lock \(-> reaper_lock \(-> frame ref_lock \(-> logger lock \(-> stats mutex (always last; log_msg is never called while it is held)."
\f(CWconfig_lock\fP, \f(CWshutdown_lock\fP and \f(CWport_down_lock\fP are leaves taken alone. No lock is held across blocking I/O, \f(CWpthread_join\fP, \f(CWsem_timedwait\fP or a wait on
another lock's behalf; at most one queue lock and one bucket lock are held at a time.
A deadlock needs a cycle of waits. Every path that holds two locks takes them in the order above, so a wait always goes from a lower to a higher rank and no cycle can close. Nobody waits long
while holding a lock either: receivers hold only the \f(CWports_lock\fP read lock and one short leaf lock and never block (a full queue is a drop); the writers (accept, reaper, admin) are not starved
because the rwlock prefers writers; the reaper joins with no lock held and takes the write lock only for the last step; the admin thread requests a teardown under the write lock, releases it, and then
waits on a leaf condition with a 5 s limit. The details of every operation are in \f(CWnotes/locks.md\fP; the order is exercised by \f(CWreconfig_stress.sh\fP (500 random admin operations plus SIGHUP/SIGUSR1 under
traffic) under TSan and valgrind.
.H1 "4. One real bug: a teardown that processed the socket backlog (notes/bugs.md, item 31)"
.BU
\fBSymptom.\fP Under valgrind, \f(CWswctl shutdown <port>\fP of a flooding host answered \f(CWERR timeout waiting for the port to shut down\fP (5 s limit) and SIGINT with flooding hosts took 12 s (3000-frame backlog) to 34 s
(30000 frames); the log showed thousands of \f(CWRX\fP lines after \f(CWshutdown: started\fP.
.BU
\fBHow it was found.\fP \f(CWreconfig_stress.sh\fP with \f(CWVARIANT=valgrind\fP. A dedicated test, \f(CWtests/test_backlog.py\fP, then measured it without valgrind: 23902 frames were forwarded after the teardown was requested.
.BU
\fBCause.\fP \f(CWshutdown(fd, SHUT_RDWR)\fP wakes a blocked \f(CWread\fP but does not discard the frames already in the kernel receive buffer; the receiver kept reading and forwarding the whole backlog and the reaper's \f(CWpthread_join\fP waited for it.
.BU
\fBFix.\fP \f(CWport_t.closing\fP, an atomic flag set when the UP \(-> CLOSING transition is won and cleared in \f(CWport_reset\fP; the receiver tests it at the top of its loop and after each frame read (that frame is dropped).
Result: at most about 40 frames per port are forwarded after the request; \f(CWtest_backlog.py\fP 21 checks, 0 failures in the normal, TSan and valgrind variants (before: failures in both normal and valgrind).
.LP
(Another real defect, found by TSan: a data race in \f(CWadmin_up\fP, which read \f(CWp->state\fP after releasing \f(CWports_lock\fP; fixed by copying the state under the lock, item 28.)
.H1 "5. Completed items"
.LP
\fBTier 1 (Parts 1\(en8):\fP the 17-byte wire protocol (network byte order, partial reads/writes, mid-frame disconnect, SIGPIPE ignored, EPIPE handled); bounded per-port queues with a sender thread (drop and count, receiver never blocks);
reference-counted shared broadcast frames; the concurrent MAC table (striped bucket locks, aging thread, "MAC moved" log, K limit with LRU, port security); the free-port semaphore with \f(CWsem_timedwait\fP and the "switch full" reply;
the signal thread and clean SIGINT shutdown.
.LP
\fBTier 2 (Parts 9\(en18):\fP asynchronous logger thread; configuration file, SIGHUP reload and SIGUSR1 MAC dump; QoS (three priority queues, weighted round robin 5:3:1, aging against starvation); rate limiting (token bucket, timer thread);
VLANs and the trunk port; shared-memory counters with a process-shared robust mutex; admin socket and \f(CWswctl\fP (all commands, \f(CWstats --watch\fP); live reconfiguration (\f(CWshutdown\fP, \f(CWup\fP, \f(CWvlan\fP, \f(CWclear mac\fP);
port mirroring; the checkpoint with 19 integration scripts, the hostile-host simulation and the source checks (no \f(CWsleep\fP, no ignored system call result).
.LP
\fBTier 3 / bonus (Parts 19\(en21):\fP two switches joined by a two-pipe trunk with cross-switch learning (\f(CW--pair\fP); the supervisor with a MAC table that survives a crash in shared memory (\f(CW--persist-mac\fP);
the benchmark of global vs striped vs rwlock MAC table locking with a graph and a one-page report (\f(CWbench/report.pdf\fP; on the 2-core test machine striped locking scales and the others do not in the table alone, the three are
indistinguishable in the whole switch).
.H1 "6. Verification (what was actually run)"
.LP
The submission archive (\f(CWmake dist\fP, commit \f(CWb86fa52\fP) was unpacked into an empty directory and, there, in this order: \f(CWmake\fP with \f(CW-std=c11 -Wall -Wextra -Werror -g -O0 -D_GNU_SOURCE -pthread\fP (0 warnings),
\f(CWmake clean\fP (no object file or program left), \f(CWmake\fP again, \f(CWmake tsan\fP, \f(CWmake test\fP (unit and regression suites of all parts, including their ThreadSanitizer and valgrind unit runs, plus 19 integration scripts),
\f(CWmake test-tsan\fP (19 of 19 integration scripts under ThreadSanitizer) and \f(CWmake test-valgrind\fP (19 of 19 under valgrind with full leak check): every command exited 0 and every suite reported "All tests passed". Details: \f(CWdocs/Final_Submission_Check.md\fP.
Known limits: no official public tests were supplied (\f(CWforward.sh\fP and \f(CWbroadcast.sh\fP are our own); \f(CW--pair\fP has one link; \f(CW--persist-mac\fP needs the striped table and cannot be combined with \f(CW--pair\fP; the benchmark ran on a 2-core machine at \f(CW-O0\fP.
