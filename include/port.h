/*
 * port.h - the switch, its port table and the port states (roadmap 2.4).
 *
 * Part 3 only has what is needed to accept hosts and read their frames.
 * Later parts add: sender thread and output queue (Part 4), the reaper and
 * the free-port semaphore (Part 5), and so on.
 */
#ifndef PORT_H
#define PORT_H

#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>

#include "common.h"

/*
 * Port states.
 *   FREE      enabled, no host attached
 *   UP        host attached, receiver thread running
 *   CLOSING   teardown in progress            (used from Part 5)
 *   DISABLED  shut down by admin / security   (used from Part 6 / 16)
 * Part 3 only makes the transition FREE -> UP.
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
    bool trunk;
    _Atomic int mirror_to;     /* used from Part 17 */
    _Atomic int mac_count;     /* used from Part 6 */
    pthread_t rx;              /* receiver thread */
} port_t;

typedef struct {
    pthread_rwlock_t ports_lock;
    port_t ports[MAX_PORTS + 1];   /* static array: slots are never freed or moved */
    int num_ports;

    int listen_fd;                 /* non-blocking listening socket */
    int wake_efd;                  /* eventfd that wakes the accept thread (written from Part 7) */

    _Atomic bool shutting_down;    /* set from Part 7 */
    pthread_mutex_t shutdown_lock;
    pthread_cond_t shutdown_cond;  /* monotonic clock; timed waits wake early on shutdown */

    const char *config_path;       /* from -c, parsed in Part 10 */
} switch_t;

extern switch_t sw;

/* Set up the rwlock, mutex, condition variable and the port slots. Startup only. */
void switch_init(int num_ports);

/*
 * Wait up to ms milliseconds on shutdown_cond (returns early if shutting_down
 * is set). Used instead of sleep() when a loop must pause.
 */
void switch_wait_ms(long ms);

/* Reset the per-connection fields of a slot (never the slot itself). */
void port_reset(port_t *p);

/*
 * Return the lowest-numbered FREE port (1..num_ports), or NULL if none.
 * The caller must hold sw.ports_lock for writing.
 */
port_t *port_alloc(void);

/* Receiver thread: arg is a port_t *. */
void *receiver_main(void *arg);

#endif /* PORT_H */
