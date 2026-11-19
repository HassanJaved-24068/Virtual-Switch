/*
 * stats.h - per-port counters in POSIX shared memory (Part 14).
 *
 * Part 4 kept the counters in a heap array behind one mutex. Part 14 keeps the same API (stats_init, stats_inc,
 * stats_get, stats_destroy) but the counters now live in the shared-memory object /vswitch_stats_P, so that another
 * process (swctl, Part 15) can read them while the switch runs.
 *
 * Layout of the segment (two page-aligned areas; PAGE = sysconf(_SC_PAGESIZE), 4096 on x86-64):
 *
 *   offset 0     stats_ctl_t   magic, version, num_ports, the process-shared ROBUST mutex   (one whole page)
 *   offset PAGE  stats_data_t  port[MAX_PORTS + 1]: in, out, dropped, mirrored, state, vlan, mirror_to
 *   total size   PAGE + round_up(sizeof(stats_data_t), PAGE)
 *
 * Lock order: the stats mutex is the LAST lock of the project (it may be taken while any other lock is held, but
 * nothing else is taken while it is held; this file never calls log_msg).
 */
#ifndef STATS_H
#define STATS_H

#include <pthread.h>
#include <stddef.h>
#include <stdint.h>

#include "common.h"

#define STATS_SHM_NAME "/vswitch_stats_P"
#define STATS_MAGIC    0x56535453u      /* "VSTS" */
#define STATS_VERSION  1u

enum stat_kind {
    STAT_IN,       /* frames received from the host on this port */
    STAT_OUT,      /* frames written to the host on this port */
    STAT_DROP,     /* frames dropped because this port's queue was full */
    STAT_MIRROR,   /* mirrored copies queued for the sniffer of this port (Part 17) */
    STAT_KINDS
};

/* ---- the layout of the shared segment (also used by readers: swctl, the tests) ---- */

typedef struct {
    uint32_t magic;                /* STATS_MAGIC, written LAST (release store) after everything is initialised */
    uint32_t version;              /* STATS_VERSION */
    int num_ports;                 /* highest valid port number given to stats_init (counters exist for 0..num_ports) */
    pthread_mutex_t lock;          /* PTHREAD_PROCESS_SHARED + PTHREAD_MUTEX_ROBUST; the switch initialises it */
} stats_ctl_t;                     /* followed by padding up to exactly one page (the data area starts at PAGE) */

typedef struct {
    uint64_t in, out, dropped, mirrored;
    int state;                     /* enum port_state, copied in whenever it changes */
    int vlan;                      /* VLAN of the port */
    int mirror_to;                 /* sniffer port, 0 = none */
} stats_port_t;

typedef struct {
    stats_port_t port[MAX_PORTS + 1];
} stats_data_t;

_Static_assert(sizeof(stats_ctl_t) <= 4096, "the control area must fit in the smallest page");

/* The system page size (sysconf), 0 if it is unusable. */
size_t stats_page_size(void);

/* Total size of the segment for a page size: page + round_up(sizeof(stats_data_t), page). 0 if page is not a
 * power of two or is smaller than the control structure. */
size_t stats_shm_size(size_t page);

/* Reader-side check before the data is trusted: 0 if magic and version are right, EINVAL for a wrong magic (not
 * initialised yet, or already shut down), EPROTO for another version. The magic is read with acquire semantics. */
int stats_validate(const stats_ctl_t *ctl);

/* Lock / unlock the shared mutex the way every user must (the switch and readers in other processes):
 * stats_lock returns 0 with the lock held. If the previous owner died holding it (EOWNERDEAD) the mutex is made
 * consistent again (pthread_mutex_consistent) and 0 is returned: the data are single-word counters, so they
 * are always usable. Any other error is returned as it is and the lock is NOT held. */
int stats_lock(pthread_mutex_t *m);
void stats_unlock(pthread_mutex_t *m);

/* ---- the API of Part 4 (unchanged) ---- */

/* Create the shared segment /vswitch_stats_P and the counters for ports 0..max_port (0 <= max_port <= MAX_PORTS).
 * A stale segment from a crashed run is unlinked and recreated. Returns 0, or an errno value (nothing is left
 * behind on failure). */
int stats_init(int max_port);

/* Same, with another segment name (tests that must not touch the real one). */
int stats_init_named(const char *name, int max_port);

/* Unlink the segment (so nobody can open it any more), destroy the mutex, unmap. Safe to call twice. A process that
 * mapped it earlier keeps its mapping until it exits; it sees the magic cleared. */
void stats_destroy(void);

/* Add 1 to a counter and return its new value (0 if port/kind is invalid or the counters are not initialised). */
unsigned long long stats_inc(int port, enum stat_kind kind);

/* Read a counter (0 if port/kind is invalid). */
unsigned long long stats_get(int port, enum stat_kind kind);

/* ---- fields copied in whenever a port changes (for "swctl show ports", Part 15); 0 or EINVAL ---- */
int stats_set_state(int port, int state);
int stats_set_vlan(int port, int vlan);
int stats_set_mirror(int port, int mirror_to);

/* Read all three fields of a port under the lock; 0 or EINVAL. */
int stats_get_port(int port, int *state, int *vlan, int *mirror_to);

#endif /* STATS_H */
