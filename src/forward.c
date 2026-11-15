/*
 * forward.c - forwarding decision. Part 4: flooding.
 */
#include "forward.h"

#include "log.h"
#include "stats.h"

#include <string.h>

/* Log a "queue full" message for the 1st drop and then every 1000th. */
#define DROP_LOG_EVERY 1000

void forward_frame(port_t *in_port, frame_t *f)
{
    unsigned long long drops[MAX_PORTS + 1];     /* counts worth logging, 0 = nothing */

    memset(drops, 0, sizeof drops);

    /* The read lock keeps ports from being torn down while we use their queues. */
    CHECK_PT(pthread_rwlock_rdlock(&sw.ports_lock));
    for (int i = 1; i <= sw.num_ports; i++) {
        port_t *dst = &sw.ports[i];

        if (dst == in_port || dst->state != PORT_UP)
            continue;

        frame_ref(f);                            /* one reference for the queue */
        if (!outq_try_push(&dst->q, f)) {
            frame_unref(f);                      /* queue full: take it back */
            unsigned long long n = stats_inc(i, STAT_DROP);
            if (n == 1 || n % DROP_LOG_EVERY == 0)
                drops[i] = n;
        }
    }
    CHECK_PT(pthread_rwlock_unlock(&sw.ports_lock));

    /* Logging writes to stderr, so it is done after the lock is released. */
    for (int i = 1; i <= sw.num_ports; i++)
        if (drops[i] != 0)
            log_msg(LOG_WARN, "port %d: output queue full, %llu frames dropped so far", i, drops[i]);
}
