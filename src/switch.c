/*
 * switch.c - the switch program: argument parsing, listening socket and the
 * accept thread.
 *
 *   ./switch <tcp_port> <num_ports> [-c <config>]
 *
 * Part 3: accept hosts, give each one a port and a receiver thread.
 * Part 4: every port also gets an output queue and a sender thread, and
 * frames are flooded to all other UP ports.
 * Part 5: a reaper thread tears ports down so they can be reused, and a host
 * waits (up to port_wait_timeout) for a free port or gets "switch full".
 * Part 6: a MAC table (learning, aging, LRU, port security) and an aging thread.
 *
 * Part 7: a signal thread (sigwait) turns SIGINT/SIGTERM into request_shutdown();
 * main waits for it and then runs the shutdown sequence (see shutdown_switch()).
 * Part 9: log_msg() queues its lines for one logger thread that writes the log file;
 * it starts before the first log line and is the last thread to stop.
 */
#include "common.h"
#include "frame.h"
#include "log.h"
#include "mactable.h"
#include "port.h"
#include "sig.h"
#include "stats.h"

#include <netinet/in.h>
#include <poll.h>
#include <semaphore.h>
#include <signal.h>
#include <sys/eventfd.h>
#include <sys/socket.h>
#include <unistd.h>

#define LISTEN_BACKLOG 64
#define RETRY_WAIT_MS 100     /* pause after an accept/poll problem (no busy loop) */

/* ------------------------------------------------------------------ */
/* Start-up                                                            */
/* ------------------------------------------------------------------ */

static void usage(const char *prog)
{
    fprintf(stderr, "usage: %s <tcp_port 1-65535> <num_ports 1-%d> [-c <config>]\n",
            prog, MAX_PORTS);
}

/* Parse a whole string as a decimal long in [lo, hi]. Returns 0 or -1. */
static int parse_range(const char *s, long lo, long hi, long *out)
{
    char *end;

    if (s == NULL || *s == '\0')
        return -1;
    errno = 0;
    long v = strtol(s, &end, 10);
    if (errno != 0 || *end != '\0' || v < lo || v > hi)
        return -1;
    *out = v;
    return 0;
}

/* Non-blocking TCP listening socket on INADDR_ANY. Exits on failure (startup). */
static int make_listener(int port)
{
    struct sockaddr_in addr;
    int one = 1;
    int fd;

    CHECK(fd = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0));
    CHECK(setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one));

    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons((uint16_t)port);
    if (bind(fd, (struct sockaddr *)&addr, sizeof addr) < 0) {
        fprintf(stderr, "switch: bind to port %d failed: %s\n", port, strerror(errno));
        exit(EXIT_FAILURE);
    }
    if (listen(fd, LISTEN_BACKLOG) < 0) {
        fprintf(stderr, "switch: listen failed: %s\n", strerror(errno));
        exit(EXIT_FAILURE);
    }
    return fd;
}

/* ------------------------------------------------------------------ */
/* Accept thread                                                       */
/* ------------------------------------------------------------------ */

/*
 * The fixed "switch full" reply (roadmap Part 5): one frame from
 * 00:00:00:00:00:00 to FF:FF:FF:FF:FF:FF, vlan 1, priority 0, payload
 * "switch full". Written with the normal frame code; errors are ignored
 * because the host may already be gone.
 */
static void send_switch_full(int fd)
{
    static const char text[] = "switch full";
    frame_hdr_t h;

    memset(h.src, 0x00, MAC_LEN);
    memset(h.dst, 0xFF, MAC_LEN);
    h.vlan = 1;
    h.prio = 0;
    h.len = sizeof text - 1;
    (void)frame_send(fd, &h, (const uint8_t *)text);
}

static void close_fd(int fd)
{
    if (close(fd) < 0)
        log_msg(LOG_WARN, "close of a rejected connection failed: %s", strerror(errno));
}

/*
 * Take one token from the free-port semaphore, waiting up to sw.port_wait_sec
 * seconds. Returns 0 with the token, or an errno value (ETIMEDOUT = no port
 * became free in time). The deadline uses CLOCK_REALTIME because
 * sem_timedwait requires it.
 */
static int wait_for_token(void)
{
    struct timespec deadline;

    /* Fast path: a port is free, no waiting and no deadline needed. */
    for (;;) {
        if (sem_trywait(&sw.free_ports) == 0)
            return 0;
        if (errno == EAGAIN)
            break;
        if (errno != EINTR)
            return errno;
    }

    log_msg(LOG_INFO, "no free port: host waits up to %ld s for one", sw.port_wait_sec);
    deadline = deadline_real(sw.port_wait_sec);
    for (;;) {
        if (sem_timedwait(&sw.free_ports, &deadline) == 0)
            return 0;
        if (errno != EINTR)
            return errno;                               /* ETIMEDOUT or a real error */
        /* EINTR: wait again, with the same deadline */
    }
}

/*
 * A new host: wait for a free port (at most port_wait_sec), then start it,
 * or answer "switch full". One host at a time: the accept thread does not
 * accept the next connection before this is finished.
 */
static void admit_host(int fd)
{
    int rc = wait_for_token();

    /* Shutdown wins over everything else. This also covers the host that waited in
     * sem_timedwait(): shutdown_switch() posts the semaphore to wake us, and the token
     * we may have received is not a real free port. Do not allocate a port; the
     * semaphore is destroyed later, so the token need not be returned. */
    if (atomic_load(&sw.shutting_down)) {
        log_msg(LOG_INFO, "shutting down: closing a connection that was waiting for a port");
        close_fd(fd);
        return;
    }
    if (rc == ETIMEDOUT) {
        log_msg(LOG_WARN, "no free port after %ld s: sending SWITCH FULL", sw.port_wait_sec);
        send_switch_full(fd);
        close_fd(fd);
        return;
    }
    if (rc != 0) {
        log_msg(LOG_ERR, "waiting for a free port failed: %s", strerror(rc));
        close_fd(fd);
        return;
    }

    /* We hold a token. Find the lowest FREE port and start it. */
    int id, err;
    switch (port_start(fd, &id, &err)) {
    case PS_OK:
        log_msg(LOG_INFO, "port %d: host connected", id);
        break;
    case PS_NONE:
        /* The token had no FREE port behind it (an admin disabled one meanwhile).
         * Treat it as full and do NOT post the token back. */
        log_msg(LOG_WARN, "no FREE port found for a token: sending SWITCH FULL");
        send_switch_full(fd);
        close_fd(fd);
        break;
    case PS_FAILED:
        log_msg(LOG_ERR, "port %d: cannot start the port threads: %s", id, strerror(err));
        close_fd(fd);
        if (sem_post(&sw.free_ports) < 0)                 /* the port is FREE again: return the token */
            log_msg(LOG_ERR, "sem_post failed: %s", strerror(errno));
        break;
    case PS_FAILED_HANDED:
        log_msg(LOG_ERR, "port %d: cannot start the receiver thread: %s", id, strerror(err));
        break;                                            /* the reaper closes fd and returns the token */
    }
}

/* Errors that only mean "this one connection is gone / try again". */
static bool accept_error_is_transient(int e)
{
    return e == EINTR || e == EAGAIN || e == EWOULDBLOCK || e == ECONNABORTED ||
           e == EPROTO || e == ENOPROTOOPT || e == EHOSTDOWN || e == ENONET ||
           e == EHOSTUNREACH || e == EOPNOTSUPP || e == ENETUNREACH || e == ENETDOWN;
}

static void *accept_main(void *arg)
{
    bool resource_problem = false;        /* true while EMFILE etc. continues (log once) */
    int rc;

    (void)arg;
    rc = pthread_setname_np(pthread_self(), "accept");
    if (rc != 0)
        log_msg(LOG_WARN, "cannot set thread name: %s", strerror(rc));

    while (!atomic_load(&sw.shutting_down)) {
        struct pollfd pfd[2] = {
            {.fd = sw.listen_fd, .events = POLLIN},
            {.fd = sw.wake_efd, .events = POLLIN},
        };

        if (poll(pfd, 2, -1) < 0) {
            if (errno == EINTR)
                continue;
            log_msg(LOG_ERR, "poll failed: %s", strerror(errno));
            switch_wait_ms(RETRY_WAIT_MS);                /* never spin */
            continue;
        }

        if (pfd[1].revents & POLLIN) {
            /* request_shutdown() writes to wake_efd to stop this thread. If it ever
             * fires without shutdown, drain it so poll() blocks again. */
            if (atomic_load(&sw.shutting_down))
                break;
            uint64_t counter;
            if (read(sw.wake_efd, &counter, sizeof counter) < 0 && errno != EAGAIN && errno != EINTR)
                log_msg(LOG_WARN, "reading wake eventfd failed: %s", strerror(errno));
        }

        if (!(pfd[0].revents & POLLIN)) {
            if (pfd[0].revents & (POLLERR | POLLHUP | POLLNVAL)) {
                log_msg(LOG_ERR, "listening socket problem (poll events 0x%x)", pfd[0].revents);
                switch_wait_ms(RETRY_WAIT_MS);
            }
            continue;
        }

        /* The listener is non-blocking; the accepted socket is blocking
         * (no SOCK_NONBLOCK here), which is what frame_recv expects. */
        int fd = accept4(sw.listen_fd, NULL, NULL, SOCK_CLOEXEC);
        if (fd >= 0) {
            resource_problem = false;
            admit_host(fd);
            continue;
        }

        int e = errno;
        if (accept_error_is_transient(e)) {
            /* EAGAIN: the connection vanished between poll() and accept4(). */
            if (e == ECONNABORTED)
                log_msg(LOG_INFO, "accept: connection aborted before it was accepted");
            continue;
        }
        if (e == EMFILE || e == ENFILE || e == ENOBUFS || e == ENOMEM) {
            if (!resource_problem)
                log_msg(LOG_WARN, "accept failed: %s (retrying every %d ms)", strerror(e),
                        RETRY_WAIT_MS);
            resource_problem = true;
            switch_wait_ms(RETRY_WAIT_MS);                /* the connection stays queued */
            continue;
        }
        log_msg(LOG_ERR, "accept failed: %s", strerror(e));
        switch_wait_ms(RETRY_WAIT_MS);
    }
    return NULL;
}

/* ------------------------------------------------------------------ */
/* Start-up and shutdown                                               */
/* ------------------------------------------------------------------ */

/* Which threads exist (and must be joined). Set only after a successful pthread_create. */
typedef struct {
    bool sig, reaper, aging, accept;
    pthread_t sig_thread, accept_thread;
} started_t;

/* Ask for the teardown of every UP port. The reaper does the actual work. */
static void request_down_all_ports(void)
{
    int up[MAX_PORTS + 1];
    int n = 0;

    /* Find the UP ports under the write lock, release it, THEN call
     * port_request_down(): that function takes ports_lock itself. A port that
     * leaves UP in between is already CLOSING and queued; port_request_down()
     * then simply returns false for it. */
    CHECK_PT(pthread_rwlock_wrlock(&sw.ports_lock));
    for (int i = 1; i <= sw.num_ports; i++)
        if (sw.ports[i].state == PORT_UP)
            up[n++] = i;
    CHECK_PT(pthread_rwlock_unlock(&sw.ports_lock));

    for (int i = 0; i < n; i++)
        (void)port_request_down(&sw.ports[up[i]], PORT_FREE);
    log_msg(LOG_INFO, "shutdown: teardown requested for %d connected port(s)", n);
}

/*
 * The shutdown sequence (also used when start-up fails half way: `st` says what
 * exists). Every step is skipped if its thread was never started, and errors are
 * logged without abandoning the rest.
 */
static void shutdown_switch(const started_t *st)
{
    log_msg(LOG_INFO, "shutdown: started");

    /* 1. The signal thread (it ended itself after SIGINT/SIGTERM). */
    if (st->sig)
        signal_stop_join(st->sig_thread);

    /* 2. Stop accept: the eventfd wakes poll(); the semaphore wakes a
     *    sem_timedwait() of a host that waits for a port. request_shutdown() is
     *    idempotent; calling it here makes the start-up failure path work too. */
    if (st->accept) {
        request_shutdown();
        if (sem_post(&sw.free_ports) < 0)
            log_msg(LOG_ERR, "shutdown: sem_post failed: %s", strerror(errno));
        int rc = pthread_join(st->accept_thread, NULL);
        if (rc != 0)
            log_msg(LOG_ERR, "shutdown: join of the accept thread failed: %s", strerror(rc));
    }
    /* The accept thread is gone: nobody uses listen_fd any more. Close it now so
     * new connections are refused while the ports are torn down. */
    if (sw.listen_fd >= 0) {
        if (close(sw.listen_fd) < 0)
            log_msg(LOG_ERR, "shutdown: close of the listening socket failed: %s", strerror(errno));
        sw.listen_fd = -1;
    }
    /* (3. The admin thread belongs to Part 15.) */

    /* 4. Every UP port goes to the reaper. */
    if (st->reaper) {
        if (st->accept)
            request_down_all_ports();      /* no port can be UP unless accept ran */
        /* 5.+6. The reaper finishes everything queued (also ports that were already
         *       CLOSING), then exits, and is joined. */
        reaper_finish();
    }

    /* 7. The aging thread (woken by the shutdown broadcast). */
    if (st->aging) {
        request_shutdown();                /* start-up failure path: make sure it was requested */
        int rc = pthread_join(sw.aging, NULL);
        if (rc != 0)
            log_msg(LOG_ERR, "shutdown: join of the aging thread failed: %s", strerror(rc));
    }

    /* 8-13. No thread is left: destroy things, each exactly once. */
    mac_destroy();
    free(sw.mac_region);
    sw.mac_region = NULL;
    switch_destroy();                      /* semaphore, locks, condition variables, listen_fd, wake_efd */
    stats_destroy();
}

int main(int argc, char **argv)
{
    long tcp_port, num_ports;
    const char *config = NULL;
    started_t st;
    int rc;

    memset(&st, 0, sizeof st);

    /* Before ANY thread exists: ignore SIGPIPE and block the signals that the
     * signal thread will collect with sigwait(). */
    rc = signal_setup();
    if (rc != 0) {
        fprintf(stderr, "switch: cannot set up signal handling: %s\n", strerror(rc));
        return EXIT_FAILURE;
    }

    if ((argc != 3 && argc != 5) ||
        (argc == 5 && strcmp(argv[3], "-c") != 0)) {
        usage(argv[0]);
        return EXIT_FAILURE;
    }
    if (parse_range(argv[1], 1, 65535, &tcp_port) < 0) {
        fprintf(stderr, "switch: bad TCP port '%s' (must be 1-65535)\n", argv[1]);
        return EXIT_FAILURE;
    }
    if (parse_range(argv[2], 1, MAX_PORTS, &num_ports) < 0) {
        fprintf(stderr, "switch: bad number of ports '%s' (must be 1-%d)\n", argv[2], MAX_PORTS);
        return EXIT_FAILURE;
    }
    if (argc == 5)
        config = argv[4];

    switch_init((int)num_ports);
    rc = stats_init(MAX_PORTS);
    if (rc != 0) {
        fprintf(stderr, "switch: cannot allocate counters: %s\n", strerror(rc));
        return EXIT_FAILURE;
    }
    sw.config_path = config;
    sw.listen_fd = make_listener((int)tcp_port);
    CHECK(sw.wake_efd = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK));

    /* The logger: from here on log_msg() is queued and written to the log file by its own thread.
     * (Part 10 takes the file name from the config; until then switch.log, or $VSWITCH_LOG_FILE.) */
    const char *log_path = getenv("VSWITCH_LOG_FILE");
    if (log_path == NULL || *log_path == '\0')
        log_path = DEFAULT_LOG_FILE;
    rc = log_start(log_path);
    if (rc != 0) {
        fprintf(stderr, "switch: cannot open the log file '%s': %s\n", log_path, strerror(rc));
        return EXIT_FAILURE;
    }

    log_msg(LOG_INFO, "switch listening on TCP port %ld with %ld ports", tcp_port, num_ports);
    if (config != NULL)
        log_msg(LOG_INFO, "config file: %s (not used until Part 10)", config);

    /* The MAC table lives in one region (heap now; shared memory in the bonus Part 20). */
    size_t mac_size = mac_region_size(sw.mac_table_size, sw.mac_buckets);
    sw.mac_region = malloc(mac_size);
    if (sw.mac_region == NULL) {
        fprintf(stderr, "switch: cannot allocate the MAC table\n");
        log_stop();
        return EXIT_FAILURE;
    }
    rc = mac_init(sw.mac_region, sw.mac_table_size, sw.mac_buckets);
    if (rc != 0) {
        fprintf(stderr, "switch: cannot initialise the MAC table: %s\n", strerror(rc));
        log_stop();
        return EXIT_FAILURE;
    }
    log_msg(LOG_INFO, "MAC table: %d entries, %d buckets, aging %lds, at most %d MACs per port",
            sw.mac_table_size, sw.mac_buckets, atomic_load(&sw.mac_aging_sec),
            atomic_load(&sw.max_macs_per_port));

    /*
     * Threads. The signal thread comes first so that a SIGINT during start-up is
     * handled (a signal that arrives before it exists stays pending: it is blocked).
     * If a thread cannot be created, the ones already running are stopped by the
     * same shutdown sequence and the exit status is 1.
     */
    const char *failed = NULL;

    rc = signal_start(&st.sig_thread);
    if (rc == 0) {
        st.sig = true;
        rc = pthread_create(&sw.reaper, NULL, reaper_main, NULL);  /* before any host can connect */
        if (rc == 0) {
            st.reaper = true;
            rc = pthread_create(&sw.aging, NULL, aging_main, NULL);
            if (rc == 0) {
                st.aging = true;
                rc = pthread_create(&st.accept_thread, NULL, accept_main, NULL);
                if (rc == 0)
                    st.accept = true;
                else
                    failed = "accept";
            } else {
                failed = "aging";
            }
        } else {
            failed = "reaper";
        }
    } else {
        failed = "signal";
    }

    if (failed != NULL) {
        fprintf(stderr, "switch: cannot start the %s thread: %s\n", failed, strerror(rc));
        request_shutdown();
        shutdown_switch(&st);
        log_stop();                        /* the last thread: the messages above are written first */
        return EXIT_FAILURE;
    }

    /* Wait for the shutdown request. The predicate is tested under the lock before
     * every wait, so a request made before we get here is not missed. */
    CHECK_PT(pthread_mutex_lock(&sw.shutdown_lock));
    while (!sw.shutdown_requested)
        CHECK_PT(pthread_cond_wait(&sw.shutdown_cond, &sw.shutdown_lock));
    CHECK_PT(pthread_mutex_unlock(&sw.shutdown_lock));

    shutdown_switch(&st);
    log_msg(LOG_INFO, "shutdown: complete");
    log_stop();                            /* the logger is the LAST thread to stop: it writes everything above */
    return EXIT_SUCCESS;
}
