/*
 * port.c - the switch/port table and the receiver thread.
 */
#include "port.h"

#include "forward.h"
#include "frame.h"
#include "log.h"
#include "mactable.h"
#include "reload.h"
#include "stats.h"

#include <string.h>
#include <sys/socket.h>
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

    /* One token per port: every port starts FREE. */
    CHECK(sem_init(&sw.free_ports, 0, (unsigned)num_ports));
    /* The live configuration starts as the defaults; main() replaces it with the file's (config_install). */
    CHECK_PT(pthread_mutex_init(&sw.config_lock, NULL));
    config_defaults(&sw.cfg, num_ports);
    atomic_init(&sw.port_wait_sec, sw.cfg.port_wait_sec);
    sw.mac_table_size = sw.cfg.mac_table_size;
    sw.mac_buckets = DEFAULT_MAC_BUCKETS;
    sw.queue_capacity = sw.cfg.queue_capacity;
    atomic_init(&sw.mac_aging_sec, sw.cfg.mac_aging_sec);
    atomic_init(&sw.max_macs_per_port, sw.cfg.max_macs_per_port);

    CHECK_PT(pthread_mutex_init(&sw.reaper_lock, NULL));
    CHECK_PT(cond_init_mono(&sw.reaper_cond));
    CHECK_PT(pthread_mutex_init(&sw.port_down_lock, NULL));
    CHECK_PT(cond_init_mono(&sw.port_down_cond));

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
    atomic_store(&p->mirror_to, 0);          /* atomic: other threads may read these */
    atomic_store(&p->mac_count, 0);
    atomic_store(&p->bad_prio, 0);
}

void switch_wait_ms(long ms)
{
    struct timespec deadline = deadline_mono(ms);
    int rc = 0;

    CHECK_PT(pthread_mutex_lock(&sw.shutdown_lock));
    while (rc == 0 && !sw.shutdown_requested)
        rc = pthread_cond_timedwait(&sw.shutdown_cond, &sw.shutdown_lock, &deadline);
    CHECK_PT(pthread_mutex_unlock(&sw.shutdown_lock));
    if (rc != 0 && rc != ETIMEDOUT) {
        log_msg(LOG_ERR, "pthread_cond_timedwait failed: %s", strerror(rc));
    }
}

/* Report a failed cleanup call and carry on with the remaining cleanup. */
static void destroy_failed(const char *what, int rc)
{
    if (rc != 0)
        log_msg(LOG_ERR, "cleanup: %s failed: %s", what, strerror(rc));
}

void switch_destroy(void)
{
    destroy_failed("destroy port_down_cond", pthread_cond_destroy(&sw.port_down_cond));
    destroy_failed("destroy port_down_lock", pthread_mutex_destroy(&sw.port_down_lock));
    destroy_failed("destroy reaper_cond", pthread_cond_destroy(&sw.reaper_cond));
    destroy_failed("destroy reaper_lock", pthread_mutex_destroy(&sw.reaper_lock));
    destroy_failed("destroy free_ports", sem_destroy(&sw.free_ports) < 0 ? errno : 0);
    destroy_failed("destroy config_lock", pthread_mutex_destroy(&sw.config_lock));
    destroy_failed("destroy shutdown_cond", pthread_cond_destroy(&sw.shutdown_cond));
    destroy_failed("destroy shutdown_lock", pthread_mutex_destroy(&sw.shutdown_lock));
    destroy_failed("destroy ports_lock", pthread_rwlock_destroy(&sw.ports_lock));
    if (sw.listen_fd >= 0) {
        destroy_failed("close listen_fd", close(sw.listen_fd) < 0 ? errno : 0);
        sw.listen_fd = -1;
    }
    if (sw.wake_efd >= 0) {
        destroy_failed("close wake_efd", close(sw.wake_efd) < 0 ? errno : 0);
        sw.wake_efd = -1;
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
/* Starting a port                                                     */
/* ------------------------------------------------------------------ */

static bool request_down_locked(port_t *p, enum port_state final_state);

enum port_start_rc port_start(int fd, int *port_id, int *err)
{
    enum port_start_rc result = PS_NONE;

    *port_id = 0;
    *err = 0;

    CHECK_PT(pthread_rwlock_wrlock(&sw.ports_lock));
    port_t *p = port_alloc();                            /* lowest FREE port */
    if (p != NULL) {
        *port_id = p->id;
        result = PS_FAILED;
        port_reset(p);
        p->rfd = fd;
        p->wfd = fd;
        p->rx_started = false;

        *err = outq_init(&p->q, sw.queue_capacity);
        if (*err == 0) {
            config_t cfg;

            config_snapshot(&cfg);                       /* config_lock is a leaf: fine under ports_lock */
            outq_set_rate(&p->q, config_port_rate(&cfg, p->id));   /* before the sender exists */
            p->vlan = (uint16_t)config_port_vlan(&cfg, p->id);     /* Part 13: VLAN and trunk role, fixed here */
            p->trunk = cfg.trunk_port == p->id;
            if (p->q.rate > 0)
                log_msg(LOG_INFO, "port %d: rate limit %d frames/s", p->id, p->q.rate);
            *err = pthread_create(&p->tx, NULL, sender_main, p);
            if (*err != 0) {
                outq_destroy(&p->q);                     /* nothing is running yet */
                port_reset(p);
            } else {
                p->state = PORT_UP;                      /* FREE -> UP */
                *err = pthread_create(&p->rx, NULL, receiver_main, p);
                if (*err == 0) {
                    p->rx_started = true;
                    result = PS_OK;
                } else {
                    /* The sender is already running. Let the reaper clean up (it
                     * joins only threads that exist); it also closes fd and gives
                     * the token back. We must not join here: no join under a lock. */
                    request_down_locked(p, PORT_FREE);
                    result = PS_FAILED_HANDED;
                }
            }
        } else {
            port_reset(p);
        }
    }
    CHECK_PT(pthread_rwlock_unlock(&sw.ports_lock));
    return result;
}

/* ------------------------------------------------------------------ */
/* Teardown request and the reaper                                     */
/* ------------------------------------------------------------------ */

/* Caller holds the write lock. Only the first caller finds the port UP. */
static bool request_down_locked(port_t *p, enum port_state final_state)
{
    if (p->state != PORT_UP)
        return false;                                    /* somebody else won (or not up) */

    p->state = PORT_CLOSING;                             /* UP -> CLOSING */
    p->final_state = final_state;

    /* Lock order: ports_lock (1) first, then the reaper lock (4). */
    CHECK_PT(pthread_mutex_lock(&sw.reaper_lock));
    sw.reaper_q[(sw.reaper_head + sw.reaper_count) % (MAX_PORTS + 1)] = p->id;
    sw.reaper_count++;
    CHECK_PT(pthread_cond_signal(&sw.reaper_cond));
    CHECK_PT(pthread_mutex_unlock(&sw.reaper_lock));
    return true;
}

bool port_request_down(port_t *p, enum port_state final_state)
{
    bool won;

    CHECK_PT(pthread_rwlock_wrlock(&sw.ports_lock));
    won = request_down_locked(p, final_state);
    CHECK_PT(pthread_rwlock_unlock(&sw.ports_lock));
    return won;
}

/*
 * The teardown of one port, in the order the roadmap gives. Only the reaper
 * runs this, and only for a port that is CLOSING.
 */
static void teardown_port(port_t *p)
{
    const int id = p->id;
    const int fd = p->rfd;
    const enum port_state final_state = p->final_state;
    int rc;

    /* 1. The state is already CLOSING: forward_frame skips this port, so
     *    nothing new is queued for it. */

    /* 2. Wake a sender that is waiting for a frame. */
    outq_close(&p->q);

    /* 3. Wake a receiver blocked in read() and a sender blocked in write()
     *    to a frozen host. */
    if (shutdown(fd, SHUT_RDWR) < 0 && errno != ENOTCONN)
        log_msg(LOG_WARN, "port %d: shutdown failed: %s", id, strerror(errno));

    /* 4. Join both threads. No lock is held here. */
    if (p->rx_started) {
        rc = pthread_join(p->rx, NULL);
        if (rc != 0)
            log_msg(LOG_ERR, "port %d: join of receiver failed: %s", id, strerror(rc));
    }
    rc = pthread_join(p->tx, NULL);
    if (rc != 0)
        log_msg(LOG_ERR, "port %d: join of sender failed: %s", id, strerror(rc));

    /* 5. Release the frames still queued, then free the queue itself. */
    outq_drain(&p->q);
    outq_destroy(&p->q);

    /* Part 11: say how many frames of this host had a priority above 2 (the first one was logged when it came). */
    if (atomic_load(&p->bad_prio) != 0)
        log_msg(LOG_INFO, "port %d: bad_prio: %lu frame%s with a priority above 2 were queued as low priority", id,
                atomic_load(&p->bad_prio), atomic_load(&p->bad_prio) == 1 ? "" : "s");

    /* 6. Forget the MAC addresses learned on this port. Its receiver is gone, so
     *    nothing can learn on it any more; no lock is held. */
    mac_flush_port(id);

    /* 7. Close the socket, only now that both threads are gone, so the
     *    descriptor number cannot be reused while a thread could still use it. */
    if (close(fd) < 0)
        log_msg(LOG_WARN, "port %d: close failed: %s", id, strerror(errno));

    /* 8. Reset the slot and set the final state. The state is set BEFORE the
     *    token is posted: a host that gets the token must find the port FREE. */
    CHECK_PT(pthread_rwlock_wrlock(&sw.ports_lock));
    port_reset(p);
    p->rx_started = false;
    p->state = final_state;
    CHECK_PT(pthread_rwlock_unlock(&sw.ports_lock));

    /* 9. A FREE port gives its token back. A DISABLED port keeps it (it
     *    stays out of circulation until an admin brings the port up). */
    if (final_state == PORT_FREE && sem_post(&sw.free_ports) < 0)
        log_msg(LOG_ERR, "port %d: sem_post failed: %s", id, strerror(errno));

    log_msg(LOG_INFO, "port %d: teardown complete, port is %s", id,
            final_state == PORT_FREE ? "FREE" : "DISABLED");

    /* Tell whoever waits for this teardown (admin commands, Part 16). */
    CHECK_PT(pthread_mutex_lock(&sw.port_down_lock));
    sw.port_down_gen[id]++;
    CHECK_PT(pthread_cond_broadcast(&sw.port_down_cond));
    CHECK_PT(pthread_mutex_unlock(&sw.port_down_lock));
}

void *reaper_main(void *arg)
{
    int rc;

    (void)arg;
    rc = pthread_setname_np(pthread_self(), "reaper");
    if (rc != 0)
        log_msg(LOG_WARN, "cannot set reaper thread name: %s", strerror(rc));

    for (;;) {
        CHECK_PT(pthread_mutex_lock(&sw.reaper_lock));
        while (sw.reaper_count == 0 && !sw.reaper_stop)          /* while, never if */
            CHECK_PT(pthread_cond_wait(&sw.reaper_cond, &sw.reaper_lock));
        if (sw.reaper_count == 0) {                              /* stop requested, queue empty */
            CHECK_PT(pthread_mutex_unlock(&sw.reaper_lock));
            break;
        }
        int id = sw.reaper_q[sw.reaper_head];
        sw.reaper_head = (sw.reaper_head + 1) % (MAX_PORTS + 1);
        sw.reaper_count--;
        CHECK_PT(pthread_mutex_unlock(&sw.reaper_lock));

        teardown_port(&sw.ports[id]);                            /* no lock held */
    }
    return NULL;
}

void reaper_finish(void)
{
    CHECK_PT(pthread_mutex_lock(&sw.reaper_lock));
    sw.reaper_stop = true;
    CHECK_PT(pthread_cond_broadcast(&sw.reaper_cond));
    CHECK_PT(pthread_mutex_unlock(&sw.reaper_lock));
    CHECK_PT(pthread_join(sw.reaper, NULL));
}

unsigned long port_down_count(int id)
{
    unsigned long n = 0;

    if (id < 0 || id > MAX_PORTS)
        return 0;
    CHECK_PT(pthread_mutex_lock(&sw.port_down_lock));
    n = sw.port_down_gen[id];
    CHECK_PT(pthread_mutex_unlock(&sw.port_down_lock));
    return n;
}

bool port_wait_down(int id, unsigned long since, long timeout_ms)
{
    struct timespec deadline = deadline_mono(timeout_ms);
    bool done;
    int rc = 0;

    if (id < 0 || id > MAX_PORTS)
        return false;
    CHECK_PT(pthread_mutex_lock(&sw.port_down_lock));
    while (sw.port_down_gen[id] == since && rc == 0)
        rc = pthread_cond_timedwait(&sw.port_down_cond, &sw.port_down_lock, &deadline);
    done = (sw.port_down_gen[id] != since);
    CHECK_PT(pthread_mutex_unlock(&sw.port_down_lock));
    if (rc != 0 && rc != ETIMEDOUT)
        log_msg(LOG_ERR, "pthread_cond_timedwait failed: %s", strerror(rc));
    return done;
}

/* ------------------------------------------------------------------ */
/* Receiver thread                                                     */
/* ------------------------------------------------------------------ */

/*
 * Reads frames, counts them, logs them and floods them to the other ports.
 * It never writes to a socket and never waits for queue space. When the host
 * is gone or sends something invalid it asks for the teardown and returns:
 * it never closes or resets its own port (the reaper does) and never joins
 * anybody.
 */
void *receiver_main(void *arg)
{
    port_t *p = arg;
    char name[16];
    int rc;

    snprintf(name, sizeof name, "rx-port%d", p->id);
    rc = pthread_setname_np(pthread_self(), name);     /* so `ps -T` shows it */
    if (rc != 0)
        log_msg(LOG_WARN, "port %d: cannot set thread name: %s", p->id, strerror(rc));

    for (;;) {
        frame_t *f = frame_new();
        if (f == NULL) {
            log_msg(LOG_ERR, "port %d: out of memory for a frame, closing the host", p->id);
            port_request_down(p, PORT_FREE);
            return NULL;
        }

        enum frame_rc fr = frame_recv_f(p->rfd, f);    /* reads straight into f->wire */

        if (fr == FR_OK) {
            char src[MAC_STR_LEN], dst[MAC_STR_LEN];

            stats_inc(p->id, STAT_IN);
            mac_format(f->src, src, sizeof src);
            mac_format(f->dst, dst, sizeof dst);
            log_msg(LOG_INFO, "port %d: RX src=%s dst=%s vlan=%u prio=%u len=%u",
                    p->id, src, dst, f->vlan, f->prio, f->len);
            enum learn_rc lr = forward_frame(p, f);    /* returns with no lock held */
            frame_unref(f);                            /* the receiver's own reference */
            if (lr == LEARN_VIOLATION) {
                /* Port security: this port has learned more than M MACs. The frame was
                 * dropped. We ask for the shutdown HERE, after forward_frame released
                 * ports_lock: asking inside it would need the write lock while this thread
                 * holds the read lock (self-deadlock). */
                log_msg(LOG_WARN, "port %d shut down: MAC flooding", p->id);
                port_request_down(p, PORT_DISABLED);
                return NULL;
            }
            continue;
        }

        /* The reason, as text. Only the thread that starts the teardown logs it:
         * if the sender already failed, our EOF is just a result of that. */
        char why[96];
        enum log_level level = LOG_WARN;
        switch (fr) {
        case FR_EOF:
            snprintf(why, sizeof why, "host disconnected");
            level = LOG_INFO;
            break;
        case FR_ERR:
            snprintf(why, sizeof why, "read error: %s", strerror(errno));
            break;
        case FR_TRUNC_HDR:
            snprintf(why, sizeof why, "truncated frame (header cut short), discarded");
            break;
        case FR_TRUNC_PAYLOAD:
            snprintf(why, sizeof why, "truncated frame (payload cut short), discarded");
            break;
        case FR_PROTO:
            snprintf(why, sizeof why, "protocol violation: length %u > %d", f->len, MAX_PAYLOAD);
            break;
        default:
            snprintf(why, sizeof why, "unexpected frame_recv result %d", (int)fr);
            level = LOG_ERR;
            break;
        }
        frame_unref(f);                                /* never queued: this frees it */

        if (port_request_down(p, PORT_FREE))           /* UP -> CLOSING, the reaper does the rest */
            log_msg(level, "port %d: %s", p->id, why);
        return NULL;
    }
}

/* ------------------------------------------------------------------ */
/* Sender thread                                                       */
/* ------------------------------------------------------------------ */

/*
 * Takes frames from this port's queue and writes them to the host. Only this
 * thread writes to its socket, and no lock is held while it does. On a write
 * error it asks for the teardown (it never tears down or joins anything
 * itself). When the queue is closed it simply returns.
 */
void *sender_main(void *arg)
{
    port_t *p = arg;
    const int fd = p->wfd;           /* set before this thread was created */
    char name[16];
    frame_t *f;
    int rc;

    snprintf(name, sizeof name, "tx-port%d", p->id);
    rc = pthread_setname_np(pthread_self(), name);
    if (rc != 0)
        log_msg(LOG_WARN, "port %d: cannot set sender thread name: %s", p->id, strerror(rc));

    while (outq_pop(&p->q, &f)) {
        int wr = write_full(fd, f->wire, (size_t)HDR_LEN + f->len);
        int err = errno;

        frame_unref(f);              /* our reference from the queue */
        if (wr < 0) {
            if (port_request_down(p, PORT_FREE))
                log_msg(LOG_WARN, "port %d: write failed: %s", p->id, strerror(err));
            break;
        }
        stats_inc(p->id, STAT_OUT);
    }
    return NULL;
}

/* ------------------------------------------------------------------ */
/* Rate limiting (Part 12)                                             */
/* ------------------------------------------------------------------ */

void port_apply_rate(int id, int rate)
{
    if (id < 1 || id > sw.num_ports)
        return;
    CHECK_PT(pthread_rwlock_rdlock(&sw.ports_lock));
    if (sw.ports[id].state == PORT_UP)                   /* the queue exists from UP until teardown */
        outq_set_rate(&sw.ports[id].q, rate);
    CHECK_PT(pthread_rwlock_unlock(&sw.ports_lock));
}

void *token_timer_main(void *arg)
{
    struct timespec deadline = deadline_mono(TOKEN_TICK_MS);
    int rc;

    (void)arg;
    rc = pthread_setname_np(pthread_self(), "token-timer");
    if (rc != 0)
        log_msg(LOG_WARN, "cannot set token timer thread name: %s", strerror(rc));

    for (;;) {
        /* Sleep until the absolute deadline; shutdown ends the wait early. No sleep(), no polling. */
        CHECK_PT(pthread_mutex_lock(&sw.shutdown_lock));
        rc = 0;
        while (rc == 0 && !sw.shutdown_requested)
            rc = pthread_cond_timedwait(&sw.shutdown_cond, &sw.shutdown_lock, &deadline);
        bool stop = sw.shutdown_requested;
        CHECK_PT(pthread_mutex_unlock(&sw.shutdown_lock));

        if (rc != 0 && rc != ETIMEDOUT)
            log_msg(LOG_ERR, "token timer: pthread_cond_timedwait failed: %s", strerror(rc));
        if (stop)
            break;

        /* The deadlines advance by exactly one period (no drift). If we are late (a loaded machine) every
         * missed period still counts, so the long-run rate stays right; after a long stop (a suspended
         * machine) we do not catch up more than one second: the buckets cannot hold more anyway. */
        const struct timespec now = now_mono();
        long ticks = 0;

        while (ts_diff_ms(deadline, now) <= 0 && ticks < 1000 / TOKEN_TICK_MS) {
            ticks++;
            deadline = ts_add_ms(deadline, TOKEN_TICK_MS);
        }
        if (ts_diff_ms(deadline, now) <= 0)
            deadline = deadline_mono(TOKEN_TICK_MS);
        if (ticks == 0)
            continue;                                   /* woken early: nothing is due */

        for (int i = 1; i <= sw.num_ports; i++) {        /* one queue at a time, ascending */
            CHECK_PT(pthread_rwlock_rdlock(&sw.ports_lock));
            if (sw.ports[i].state == PORT_UP)
                outq_refill(&sw.ports[i].q, ticks * TOKEN_TICK_MS);
            CHECK_PT(pthread_rwlock_unlock(&sw.ports_lock));
        }
    }
    return NULL;
}

/* ------------------------------------------------------------------ */
/* VLANs (Part 13)                                                     */
/* ------------------------------------------------------------------ */

bool port_vlan_ok(const port_t *dst, uint16_t vlan)
{
    return dst->trunk || dst->vlan == vlan;
}

void port_set_vlan(port_t *p, uint16_t vlan)
{
    CHECK_PT(pthread_rwlock_wrlock(&sw.ports_lock));
    p->vlan = vlan;
    CHECK_PT(pthread_rwlock_unlock(&sw.ports_lock));
    (void)mac_flush_port(p->id);             /* after the unlock: old-VLAN entries of this port vanish */
}
