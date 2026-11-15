/*
 * port.c - the switch/port table and the receiver thread.
 */
#include "port.h"

#include "frame.h"
#include "log.h"

#include <string.h>
#include <unistd.h>

switch_t sw;

void switch_init(int num_ports)
{
    pthread_rwlockattr_t attr;

    memset(&sw, 0, sizeof sw);
    sw.num_ports = num_ports;
    sw.listen_fd = -1;
    sw.wake_efd = -1;

    /* Writer-preferring, so a steady stream of readers cannot starve a writer. */
    CHECK_PT(pthread_rwlockattr_init(&attr));
    CHECK_PT(pthread_rwlockattr_setkind_np(&attr, PTHREAD_RWLOCK_PREFER_WRITER_NONRECURSIVE_NP));
    CHECK_PT(pthread_rwlock_init(&sw.ports_lock, &attr));
    CHECK_PT(pthread_rwlockattr_destroy(&attr));

    CHECK_PT(pthread_mutex_init(&sw.shutdown_lock, NULL));
    CHECK_PT(cond_init_mono(&sw.shutdown_cond));
    atomic_init(&sw.shutting_down, false);

    for (int i = 0; i <= MAX_PORTS; i++) {
        sw.ports[i].id = i;
        sw.ports[i].state = PORT_FREE;
        port_reset(&sw.ports[i]);
    }
}

void port_reset(port_t *p)
{
    p->rfd = -1;
    p->wfd = -1;
    p->vlan = DEFAULT_VLAN;
    p->trunk = false;
    atomic_init(&p->mirror_to, 0);
    atomic_init(&p->mac_count, 0);
}

void switch_wait_ms(long ms)
{
    struct timespec deadline = deadline_mono(ms);
    int rc = 0;

    CHECK_PT(pthread_mutex_lock(&sw.shutdown_lock));
    while (rc == 0 && !atomic_load(&sw.shutting_down))
        rc = pthread_cond_timedwait(&sw.shutdown_cond, &sw.shutdown_lock, &deadline);
    CHECK_PT(pthread_mutex_unlock(&sw.shutdown_lock));
    if (rc != 0 && rc != ETIMEDOUT) {
        log_msg(LOG_ERR, "pthread_cond_timedwait failed: %s", strerror(rc));
    }
}

port_t *port_alloc(void)
{
    for (int i = 1; i <= sw.num_ports; i++)
        if (sw.ports[i].state == PORT_FREE)
            return &sw.ports[i];
    return NULL;
}

/* ------------------------------------------------------------------ */
/* Receiver thread                                                     */
/* ------------------------------------------------------------------ */

/*
 * Part 3 behaviour: read frames and log them. When the host leaves or
 * something goes wrong, close the socket and stop. Part 5 replaces this
 * ending with the reaper (port_request_down); the port stays marked UP
 * until then.
 */
void *receiver_main(void *arg)
{
    port_t *p = arg;
    uint8_t payload[MAX_PAYLOAD];
    frame_hdr_t h;
    char name[16];
    int rc;

    snprintf(name, sizeof name, "rx-port%d", p->id);
    rc = pthread_setname_np(pthread_self(), name);     /* so `ps -T` shows it */
    if (rc != 0)
        log_msg(LOG_WARN, "port %d: cannot set thread name: %s", p->id, strerror(rc));

    for (;;) {
        enum frame_rc fr = frame_recv(p->rfd, &h, payload);

        if (fr == FR_OK) {
            char src[MAC_STR_LEN], dst[MAC_STR_LEN];
            mac_format(h.src, src, sizeof src);
            mac_format(h.dst, dst, sizeof dst);
            log_msg(LOG_INFO, "port %d: RX src=%s dst=%s vlan=%u prio=%u len=%u",
                    p->id, src, dst, h.vlan, h.prio, h.len);
            continue;
        }

        switch (fr) {
        case FR_EOF:
            log_msg(LOG_INFO, "port %d: host disconnected", p->id);
            break;
        case FR_ERR:
            log_msg(LOG_WARN, "port %d: read error: %s", p->id, strerror(errno));
            break;
        case FR_TRUNC_HDR:
            log_msg(LOG_WARN, "port %d: truncated frame (header cut short), discarded", p->id);
            break;
        case FR_TRUNC_PAYLOAD:
            log_msg(LOG_WARN, "port %d: truncated frame (payload cut short), discarded", p->id);
            break;
        case FR_PROTO:
            log_msg(LOG_WARN, "port %d: protocol violation: length %u > %d", p->id,
                    h.len, MAX_PAYLOAD);
            break;
        default:
            log_msg(LOG_ERR, "port %d: unexpected frame_recv result %d", p->id, (int)fr);
            break;
        }
        break;                                  /* any non-OK result ends this host */
    }

    /* Close the socket. Only this thread uses it in Part 3. */
    CHECK_PT(pthread_rwlock_wrlock(&sw.ports_lock));
    int fd = p->rfd;
    p->rfd = -1;
    p->wfd = -1;
    CHECK_PT(pthread_rwlock_unlock(&sw.ports_lock));
    if (fd >= 0 && close(fd) < 0)
        log_msg(LOG_WARN, "port %d: close failed: %s", p->id, strerror(errno));
    return NULL;
}
