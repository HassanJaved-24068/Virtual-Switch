/*
 * port.h - the switch, its port table and the port states (roadmap 2.4).
 *
 * Part 3 accepted hosts and read their frames. Part 4 added the output queue
 * and sender thread of each port. Part 5 adds the port lifecycle: a reaper
 * thread that tears ports down, and the free-port semaphore.
 */
#ifndef PORT_H
#define PORT_H

#include <pthread.h>
#include <semaphore.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>

#include "common.h"
#include "config.h"
#include "queue.h"

/*
 * Port states.
 *   FREE      enabled, no host attached
 *   UP        host attached, receiver and sender threads running
 *   CLOSING   teardown in progress (the reaper is cleaning up)
 *   DISABLED  shut down by admin / security (needs an admin "up"; Parts 6 / 16)
 * Allowed transitions:
 *   FREE -> UP          a host was accepted           (port_start)
 *   UP -> CLOSING       anyone, only one wins         (port_request_down)
 *   CLOSING -> FREE or DISABLED                       (the reaper)
 * DISABLED -> FREE and FREE -> DISABLED belong to the admin commands (Part 16).
 */
enum port_state {
    PORT_FREE,
    PORT_UP,
    PORT_CLOSING,
    PORT_DISABLED
};

typedef struct port {
    int id;                    /* 1..num_ports (slot 0 is reserved for the bonus trunk) */
    enum port_state state;     /* protected by sw.ports_lock */
    int rfd, wfd;              /* socket (rfd == wfd for a TCP host), -1 if none */
    uint16_t vlan;
    uint16_t vlan_admin;       /* Part 16: VLAN set by "swctl vlan" (0 = none). Survives teardown, so a free port keeps it for the next host; a
                                * configuration reload that changes this port's VLAN clears it. Protected by sw.ports_lock. */
    bool trunk;
    _Atomic bool closing;      /* Part 18: set (under the write lock) when the teardown is requested, read lock-free by the receiver: it stops reading and forwarding
                                * at once instead of working through the frames already queued in the socket (shutdown(fd) does not discard them) */
    _Atomic int mirror_to;     /* Part 17: the sniffer port that gets a copy of this port's traffic, 0 = off (changed under the write lock) */
    _Atomic int mac_count;     /* used from Part 6 */
    _Atomic unsigned long bad_prio;  /* Part 11: frames received with a priority above 2 (queued as low) */
    pthread_t rx;              /* receiver thread: reads frames and forwards them */
    pthread_t tx;              /* sender thread: writes queued frames to the host */
    bool rx_started;           /* is rx a real thread that must be joined? */
    outq_t q;                  /* output queue (valid from UP until teardown) */
    enum port_state final_state;  /* FREE or DISABLED: what the reaper sets at the end */
} port_t;

typedef struct {
    pthread_rwlock_t ports_lock;
    port_t ports[MAX_PORTS + 1];   /* static array: slots are never freed or moved */
    int num_ports;

    int listen_fd;                 /* non-blocking listening socket */
    int wake_efd;                  /* eventfd: request_shutdown() writes it to wake the accept thread */

    /* Free-port semaphore. Invariant: its value + the tokens held by the accept
     * thread = the number of FREE ports. */
    sem_t free_ports;
    _Atomic long port_wait_sec;    /* port_wait_timeout: how long a host waits for a port (reloadable) */

    /* Reaper queue: FIFO of port ids waiting for teardown (lock order rank 4). */
    pthread_mutex_t reaper_lock;
    pthread_cond_t reaper_cond;
    int reaper_q[MAX_PORTS + 1];   /* ring buffer; a port is queued at most once at a time */
    int reaper_head, reaper_count;
    bool reaper_stop;              /* set by reaper_finish(): finish the queue, then exit */
    pthread_t reaper;

    /* "Teardown finished" notification (a leaf lock, never held with another lock). */
    pthread_mutex_t port_down_lock;
    pthread_cond_t port_down_cond;           /* monotonic clock */
    unsigned long port_down_gen[MAX_PORTS + 1];  /* teardowns completed per port */

    /* Shutdown (Part 7). shutdown_requested is THE predicate: it is protected by
     * shutdown_lock and every wait on shutdown_cond tests it in a loop.
     * shutting_down is only a lock-free hint for loops that must not take the lock
     * (the accept thread); it is set together with shutdown_requested. */
    bool shutdown_requested;
    _Atomic bool shutting_down;
    pthread_mutex_t shutdown_lock;
    pthread_cond_t shutdown_cond;  /* monotonic clock; timed waits wake early on shutdown */

    /*
     * Configuration (Part 10). `cfg` is the live configuration; it is protected by config_lock, a LEAF lock:
     * readers copy what they need (config_snapshot) and release it at once; nobody does I/O, logging or takes
     * another lock while holding it. The settings that the hot paths read for every frame or every cycle are
     * also published as atomics below; a reload writes cfg and these together under config_lock.
     */
    pthread_mutex_t config_lock;
    config_t cfg;
    int mac_table_size;            /* K          (startup only) */
    int mac_buckets;               /* power of two, derived from K */
    int queue_capacity;            /* frames per priority class (startup only) */
    _Atomic long mac_aging_sec;    /* T          (reloadable) */
    _Atomic int max_macs_per_port; /* M          (reloadable) */
    void *mac_region;              /* memory of the MAC table (heap now, shared memory in Part 20) */
    pthread_t aging;               /* aging thread */

    const char *config_path;       /* from -c; NULL = no file (the defaults) */
} switch_t;

extern switch_t sw;

/* Set up the rwlock, mutex, condition variable and the port slots. Startup only. */
void switch_init(int num_ports);

/*
 * Destroy every lock, condition variable and the semaphore created by
 * switch_init() and close wake_efd and listen_fd if still open. Call it once,
 * after every thread that could use them has been joined.
 */
void switch_destroy(void);

/*
 * Wait up to ms milliseconds on shutdown_cond (returns early if shutdown was
 * requested). Used instead of sleep() when a loop must pause.
 */
void switch_wait_ms(long ms);

/* Reset the per-connection fields of a slot (never the slot itself). */
void port_reset(port_t *p);

/*
 * Return the lowest-numbered FREE port (1..num_ports), or NULL if none.
 * The caller must hold sw.ports_lock for writing.
 */
port_t *port_alloc(void);

/*
 * Result of port_start().
 */
enum port_start_rc {
    PS_OK,             /* the host now owns a port (UP); threads are running */
    PS_NONE,           /* no FREE port found; nothing was changed */
    PS_FAILED,         /* could not start; nothing was changed, the caller still owns fd and its token */
    PS_FAILED_HANDED   /* the port was handed to the reaper to clean up (fd and token included) */
};

/*
 * Give a connected socket to the lowest FREE port: set up the queue, start the
 * sender and the receiver, and set the port UP. Called by the accept thread
 * after it got a token from sw.free_ports. *port_id gets the port number
 * (also for PS_FAILED_HANDED), *err an errno-style code on failure.
 */
enum port_start_rc port_start(int fd, int *port_id, int *err);

/*
 * Ask for the teardown of a port: UP -> CLOSING (under the write lock), remember
 * final_state (PORT_FREE or PORT_DISABLED), queue the port for the reaper.
 * Only the first caller wins. Returns true if this call started the teardown.
 * Call it WITHOUT holding sw.ports_lock.
 */
bool port_request_down(port_t *p, enum port_state final_state);

/* Reaper thread (the only code that finishes a teardown). */
void *reaper_main(void *arg);

/* Let the reaper finish the ports already queued, then exit, and join it. */
void reaper_finish(void);

/*
 * Teardown notification for admin commands (Part 16): port_down_count() gives the
 * number of teardowns finished for a port; port_wait_down() waits until that
 * number is different from `since`, the timeout ms ends, or the switch is shutting
 * down (request_shutdown() wakes the waiters). Returns true if a teardown finished.
 */
unsigned long port_down_count(int id);
bool port_wait_down(int id, unsigned long since, long timeout_ms);

/* Receiver thread: arg is a port_t *. */
void *receiver_main(void *arg);

/* Sender thread: arg is a port_t *. Pops frames from p->q and writes them. */
void *sender_main(void *arg);

/*
 * Rate limiting (Part 12). The token timer thread wakes every TOKEN_TICK_MS (absolute deadlines on
 * shutdown_cond: no drift, and it ends at once on shutdown) and gives every UP port's queue its tokens,
 * one port at a time in ascending order (ports_lock read, then the queue lock).
 */
void *token_timer_main(void *arg);

/*
 * VLANs (Part 13). port_set_vlan: take the ports_lock write lock, set the VLAN of the port, release it, THEN
 * mac_flush_port() so that the entries learned in the old VLAN vanish (no lock is held while flushing). Used by SIGHUP
 * (and by swctl later). The trunk port keeps the VLAN of every frame; its own `vlan` field is not used.
 * vlan_eligible: may a frame of VLAN v be sent out of `dst`? (the caller holds ports_lock; the port must also be UP)
 */
void port_set_vlan(port_t *p, uint16_t vlan);
bool port_vlan_ok(const port_t *dst, uint16_t vlan);

/* Apply a new rate to port `id` if it is UP (a port that starts later reads the configuration itself). */
void port_apply_rate(int id, int rate);

/*
 * Live reconfiguration (Part 16), called by the admin thread. Each returns NULL on success or a static text that
 * becomes "ERR <text>". None of them holds a lock while it waits or joins; the teardown itself is the reaper's.
 *
 *   admin_shutdown  UP: port_request_down(DISABLED), then wait up to ADMIN_DOWN_TIMEOUT_MS until the reaper has finished (state DISABLED).
 *                   FREE: under the write lock set DISABLED and take one token (sem_trywait; EAGAIN means an accept is in progress,
 *                   which then finds no slot: port_start answers PS_NONE and the token is dropped, so the Part 5 invariant holds).
 *                   DISABLED or CLOSING: an error.
 *   admin_up        DISABLED -> FREE under the write lock, then sem_post. Any other state: an error.
 *   admin_vlan      any state (a free port keeps the VLAN for its next host); the trunk is refused. A connected port moves at once and
 *                   its MAC entries of the old VLAN are flushed.
 *   admin_clear_mac mac_clear(): buckets one at a time, ascending; returns how many entries were removed.
 */

/*
 * Port mirroring (Part 17). admin_mirror(x, s): every frame received on port x and every frame successfully queued to port x is also queued to the
 * sniffer port s (forward.c, mirror_copy: the same shared frame, no copy, VLAN filtering ignored, never back to the frame's own ingress port; there is no
 * de-duplication: a sniffer that also got the frame normally receives it twice). The copy goes into s's own bounded queue, so a slow sniffer only drops on s
 * and never slows x.
 * Both ports must be UP (the mirroring lives as long as both hosts do) and x != s; s must not be the trunk. When x or s goes down the reaper clears every
 * reference to it under the write lock, in the same step that changes the state. admin_mirror_off(x) switches it off (OK if it was off).
 * Both return NULL on success or a static error text.
 */
const char *admin_mirror(int x, int s);
const char *admin_mirror_off(int x);

#define ADMIN_DOWN_TIMEOUT_MS 5000
const char *admin_shutdown(int id);
const char *admin_up(int id);
const char *admin_vlan(int id, int vlan);
int admin_clear_mac(void);

#endif /* PORT_H */
