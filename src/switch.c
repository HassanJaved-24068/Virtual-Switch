/*
 * switch.c - the switch program: argument parsing, listening socket and the
 * accept thread.
 *
 *   ./switch <tcp_port> <num_ports> [-c <config>] [--pair <tcp_port_b> [--peer-config <config_b>]] [--persist-mac]
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
#include "admin.h"
#include "common.h"
#include "frame.h"
#include "log.h"
#include "mactable.h"
#include "port.h"
#include "reload.h"
#include "sig.h"
#include "stats.h"

#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <semaphore.h>
#include <signal.h>
#include <sys/eventfd.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

#define LISTEN_BACKLOG 64
#define RETRY_WAIT_MS 100     /* pause after an accept/poll problem (no busy loop) */

/* ------------------------------------------------------------------ */
/* Start-up                                                            */
/* ------------------------------------------------------------------ */

static void usage(const char *prog)
{
    fprintf(stderr, "usage: %s <tcp_port 1-65535> <num_ports 1-%d> [-c <config>] [--pair <tcp_port_b> [--peer-config <config_b>]] [--persist-mac]\n",
            prog, MAX_PORTS);
}

/* Parse a whole string as a decimal number in [lo, hi]: digits only, see parse_uint() (a sign or white space is refused, unlike strtol). Returns 0 or -1. */
static int parse_range(const char *s, long lo, long hi, long *out)
{
    return parse_uint(s, (unsigned long)lo, (unsigned long)hi, out);
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
    bool sig, reaper, aging, timer, accept, admin, link;     /* link: port 0 (Part 19) was attached */
    pthread_t sig_thread, accept_thread, timer_thread, admin_thread;
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
    for (int i = 0; i <= sw.num_ports; i++)              /* i = 0: the pipe trunk of a --pair (Part 19), FREE and skipped otherwise */
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
    /* 3. The admin thread (Part 15): it leaves its loop at once, even while it waits for a silent client; then the socket is closed and its
     *    path unlinked. It uses the MAC table and the ports, which still exist. */
    admin_close(st->admin, st->admin_thread);

    /* 4. Every UP port goes to the reaper. */
    if (st->reaper) {
        if (st->accept || st->link)
            request_down_all_ports();      /* no port can be UP unless accept ran (or port 0 was attached) */
        /* 5.+6. The reaper finishes everything queued (also ports that were already
         *       CLOSING), then exits, and is joined. */
        reaper_finish();
    }

    /* 7. The token timer thread (Part 12) and the aging thread: both wake on the shutdown broadcast. */
    if (st->timer) {
        request_shutdown();
        int rc = pthread_join(st->timer_thread, NULL);
        if (rc != 0)
            log_msg(LOG_ERR, "shutdown: join of the token timer thread failed: %s", strerror(rc));
    }
    if (st->aging) {
        request_shutdown();                /* start-up failure path: make sure it was requested */
        int rc = pthread_join(sw.aging, NULL);
        if (rc != 0)
            log_msg(LOG_ERR, "shutdown: join of the aging thread failed: %s", strerror(rc));
    }

    /* 8-13. No thread is left: destroy things, each exactly once. */
    if (sw.mac_persist) {                  /* Part 20: the table stays in shared memory for the next switch; its mutexes must NOT be destroyed */
        mac_detach();
        if (munmap(sw.mac_region, sw.mac_region_len) < 0)
            log_msg(LOG_ERR, "shutdown: munmap of the shared MAC table failed: %s", strerror(errno));
    } else {
        mac_destroy();
        free(sw.mac_region);
    }
    sw.mac_region = NULL;
    switch_destroy();                      /* semaphore, locks, condition variables, listen_fd, wake_efd */
    stats_destroy();
}

/*
 * Part 19: --pair. Two pipes, then fork() BEFORE any thread exists (a thread would not survive the fork, and the child must start with the clean state
 * and the signal set-up of main). The child becomes switch B (its own TCP port, admin socket, shared counters and log file, all named by its TCP port);
 * the parent stays switch A. Each side closes the two pipe ends it does not use right away, so that EOF and EPIPE work when the peer dies. A writes pipe 1
 * and reads pipe 2; B the opposite. Returns the child's pid in the parent (*link_r / *link_w = its ends), 0 in the child, -1 on error (reported).
 */
static pid_t make_pair(int *link_r, int *link_w)
{
    int p1[2], p2[2];                                    /* [0] read end, [1] write end */

    if (pipe2(p1, O_CLOEXEC) < 0 || pipe2(p2, O_CLOEXEC) < 0) {
        fprintf(stderr, "switch: cannot create the pipes for --pair: %s\n", strerror(errno));
        return -1;
    }
    const pid_t parent = getpid();
    const pid_t pid = fork();
    if (pid < 0) {
        fprintf(stderr, "switch: fork failed: %s\n", strerror(errno));
        return -1;
    }
    if (pid == 0) {                                      /* switch B: reads pipe 1, writes pipe 2 */
        if (close(p1[1]) < 0 || close(p2[0]) < 0) {
            fprintf(stderr, "switch: close of the unused pipe ends failed: %s\n", strerror(errno));
            _exit(EXIT_FAILURE);
        }
        *link_r = p1[0];
        *link_w = p2[1];
        /* If A dies without telling us (kill -9), B stops too instead of living on as an orphan. The signal is blocked and taken by the signal thread. */
        if (prctl(PR_SET_PDEATHSIG, SIGTERM) < 0 || getppid() != parent) {
            fprintf(stderr, "switch: the parent switch is gone\n");
            _exit(EXIT_FAILURE);
        }
        return 0;
    }
    if (close(p1[0]) < 0 || close(p2[1]) < 0)            /* switch A: writes pipe 1, reads pipe 2 */
        fprintf(stderr, "switch: close of the unused pipe ends failed: %s\n", strerror(errno));
    *link_r = p2[0];
    *link_w = p1[1];
    return pid;
}

/* The log file of switch B: the path of A's log (the file of the configuration, or VSWITCH_LOG_FILE) with ".<tcp_port_b>" appended. */
static void peer_log_path(char *out, size_t len, const char *base, long tcp_port_b)
{
    snprintf(out, len, "%s.%ld", base, tcp_port_b);
}

/*
 * Switch A, after its own shutdown: stop switch B and wait for it (Part 19). B normally has SIGINT from the terminal already; SIGTERM makes it shut down
 * even if it did not. The wait is a poll() on a pidfd with a time limit (no sleeping, no spinning): a peer that does not exit in PEER_EXIT_WAIT_MS (stopped
 * with SIGSTOP, hung) is killed, so that A can never wait for ever. Then waitpid() reaps it and its fate is logged.
 */
#define PEER_EXIT_WAIT_MS 15000

static void reap_peer(pid_t peer)
{
    int status = 0;

    if (kill(peer, SIGTERM) < 0 && errno != ESRCH)
        log_msg(LOG_WARN, "shutdown: cannot signal the peer switch (pid %ld): %s", (long)peer, strerror(errno));

    int pidfd = (int)syscall(SYS_pidfd_open, peer, 0);
    if (pidfd >= 0) {
        struct pollfd pfd = {.fd = pidfd, .events = POLLIN};
        int n;

        do
            n = poll(&pfd, 1, PEER_EXIT_WAIT_MS);
        while (n < 0 && errno == EINTR);
        if (n == 0) {
            log_msg(LOG_WARN, "shutdown: the peer switch (pid %ld) did not exit within %d ms: killing it", (long)peer, PEER_EXIT_WAIT_MS);
            if (kill(peer, SIGKILL) < 0 && errno != ESRCH)
                log_msg(LOG_ERR, "shutdown: cannot kill the peer switch: %s", strerror(errno));
        } else if (n < 0) {
            log_msg(LOG_ERR, "shutdown: poll on the peer switch failed: %s", strerror(errno));
        }
        if (close(pidfd) < 0)
            log_msg(LOG_WARN, "shutdown: close of the pidfd failed: %s", strerror(errno));
    } else {
        log_msg(LOG_WARN, "shutdown: pidfd_open failed (%s): waiting for the peer switch without a time limit", strerror(errno));
    }
    while (waitpid(peer, &status, 0) < 0) {
        if (errno == EINTR)
            continue;
        log_msg(LOG_ERR, "shutdown: waitpid of the peer switch failed: %s", strerror(errno));
        return;
    }
    if (WIFEXITED(status))
        log_msg(LOG_INFO, "shutdown: the peer switch (pid %ld) exited with status %d", (long)peer, WEXITSTATUS(status));
    else if (WIFSIGNALED(status))
        log_msg(LOG_INFO, "shutdown: the peer switch (pid %ld) was killed by signal %d", (long)peer, WTERMSIG(status));
}

int main(int argc, char **argv)
{
    long tcp_port, num_ports, tcp_port_b = 0;
    const char *config = NULL, *peer_config = NULL;
    started_t st;
    int rc;
    pid_t peer = -1;                       /* switch A: the pid of B (Part 19) */
    int link_r = -1, link_w = -1;          /* the pipe ends of port 0 (Part 19) */
    bool pair = false, persist_mac = false;

    memset(&st, 0, sizeof st);

    /* Before ANY thread exists: ignore SIGPIPE and block the signals that the
     * signal thread will collect with sigwait(). */
    rc = signal_setup();
    if (rc != 0) {
        fprintf(stderr, "switch: cannot set up signal handling: %s\n", strerror(rc));
        return EXIT_FAILURE;
    }

    if (argc < 3) {
        usage(argv[0]);
        return EXIT_FAILURE;
    }
    for (int i = 3; i < argc; i += 2) {    /* options, each with one value (except the flag --persist-mac) */
        if (strcmp(argv[i], "--persist-mac") == 0 && !persist_mac) {
            persist_mac = true;
            i--;                           /* a flag: the loop adds 2, the next option starts one further */
            continue;
        }
        if (i + 1 >= argc) {
            usage(argv[0]);
            return EXIT_FAILURE;
        }
        if (strcmp(argv[i], "-c") == 0 && config == NULL) {
            config = argv[i + 1];
        } else if (strcmp(argv[i], "--peer-config") == 0 && peer_config == NULL) {
            peer_config = argv[i + 1];
        } else if (strcmp(argv[i], "--pair") == 0 && !pair) {
            if (parse_range(argv[i + 1], 1, 65535, &tcp_port_b) < 0) {
                fprintf(stderr, "switch: bad TCP port '%s' for --pair (must be 1-65535)\n", argv[i + 1]);
                return EXIT_FAILURE;
            }
            pair = true;
        } else {
            usage(argv[0]);
            return EXIT_FAILURE;
        }
    }
    if (peer_config != NULL && !pair) {
        fprintf(stderr, "switch: --peer-config needs --pair\n");
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
    if (persist_mac && pair) {
        fprintf(stderr, "switch: --persist-mac cannot be combined with --pair\n");
        return EXIT_FAILURE;
    }
    if (pair && tcp_port_b == tcp_port) {
        fprintf(stderr, "switch: the two switches of a --pair need different TCP ports\n");
        return EXIT_FAILURE;
    }

    /*
     * The configuration (Part 10): the defaults, then the file given with -c. A file with ANY bad line is a
     * start-up error (exit status 1, every bad line is named); nothing has been created yet.
     */
    config_t cfg;
    config_error_t cerr;

    config_defaults(&cfg, (int)num_ports);
    if (config != NULL && config_load(config, &cfg, &cerr) != 0) {
        fprintf(stderr, "switch: configuration file '%s': %s (%d error%s)\n", config, cerr.msg, cerr.count,
                cerr.count == 1 ? "" : "s");
        return EXIT_FAILURE;
    }
    config_t cfg_b;                        /* switch B's configuration (Part 19), checked here so that a bad file stops BOTH before anything is created */

    config_defaults(&cfg_b, (int)num_ports);
    if (peer_config != NULL && config_load(peer_config, &cfg_b, &cerr) != 0) {
        fprintf(stderr, "switch: configuration file '%s': %s (%d error%s)\n", peer_config, cerr.msg, cerr.count,
                cerr.count == 1 ? "" : "s");
        return EXIT_FAILURE;
    }

    if (pair) {
        peer = make_pair(&link_r, &link_w);
        if (peer < 0)
            return EXIT_FAILURE;
        if (peer == 0) {                   /* switch B: from here on a switch of its own. No thread exists yet; the signal set-up is done again exactly as in main */
            rc = signal_setup();
            if (rc != 0) {
                fprintf(stderr, "switch: cannot set up signal handling: %s\n", strerror(rc));
                return EXIT_FAILURE;
            }
            tcp_port = tcp_port_b;
            config = peer_config;
            cfg = cfg_b;
        }
    }

    switch_init((int)num_ports);
    config_install(&cfg);                  /* the live configuration; no other thread exists yet */
    sw.config_path = config;
    sw.listen_fd = make_listener((int)tcp_port);
    CHECK(sw.wake_efd = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK));

    /* The logger: from here on log_msg() is queued and written to the log file by its own thread.
     * The file is log_file of the configuration (default switch.log); the environment variable
     * VSWITCH_LOG_FILE, if set, overrides it (the tests give every switch its own file that way). */
    const char *log_path = getenv("VSWITCH_LOG_FILE");
    char log_path_b[CONFIG_PATH_LEN + 16];
    if (log_path == NULL || *log_path == '\0')
        log_path = cfg.log_file;
    if (pair && peer == 0) {               /* switch B: its own log file, named by its TCP port (A's name + ".<port>") */
        peer_log_path(log_path_b, sizeof log_path_b, log_path, tcp_port_b);
        log_path = log_path_b;
    }
    sw.paired = pair;
    rc = log_start(log_path);
    if (rc != 0) {
        fprintf(stderr, "switch: cannot open the log file '%s': %s\n", log_path, strerror(rc));
        return EXIT_FAILURE;
    }

    /* The shared counters come AFTER everything that can fail for an outside reason (the TCP port is in use...):
     * a switch that cannot start must not replace, or remove, the segment of one that is running. */
    char stats_name[64];
    if (pair && peer == 0)
        snprintf(stats_name, sizeof stats_name, STATS_SHM_PAIR_FMT, (int)tcp_port_b);   /* B: named by its TCP port; A keeps the fixed name */
    else
        snprintf(stats_name, sizeof stats_name, "%s", STATS_SHM_NAME);
    rc = stats_init_named(stats_name, (int)num_ports);   /* num_ports of the header = the ports of this switch (swctl stats shows exactly these) */
    if (rc != 0) {
        fprintf(stderr, "switch: cannot create the shared counters %s: %s\n", stats_name, strerror(rc));
        log_stop();
        return EXIT_FAILURE;
    }
    for (int i = 1; i <= sw.num_ports; i++) {      /* the fields that "swctl show ports" will read (Part 15) */
        stats_set_state(i, sw.ports[i].state);
        stats_set_vlan(i, sw.ports[i].vlan);
        stats_set_mirror(i, atomic_load(&sw.ports[i].mirror_to));
    }

    /* The admin socket (Part 15), also after everything that can fail for an outside reason: the TCP port is ours (the listener exists), so an
     * old /tmp/vswitch_<port>.sock is stale and is unlinked. The thread that serves it starts later, when the MAC table exists. */
    rc = admin_open((int)tcp_port);
    if (rc != 0) {
        fprintf(stderr, "switch: cannot create the admin socket for TCP port %ld: %s\n", tcp_port, strerror(rc));
        log_stop();
        stats_destroy();
        return EXIT_FAILURE;
    }

    log_msg(LOG_INFO, "switch listening on TCP port %ld with %ld ports", tcp_port, num_ports);
    if (config != NULL)
        log_msg(LOG_INFO, "configuration file: %s (SIGHUP reloads it)", config);
    else
        log_msg(LOG_INFO, "no configuration file: using the defaults");

    /* The MAC table lives in one region: the heap, or with --persist-mac (Part 20) the shared-memory object /vswitch_mac_<tcp_port>, which a restarted
     * switch finds again (recovered below). */
    sw.mac_persist = persist_mac;
    int recovered = -1;
    if (persist_mac) {
        char mac_name[64];

        snprintf(mac_name, sizeof mac_name, MAC_SHM_FMT, (int)tcp_port);
        rc = mac_attach_shm(mac_name, sw.mac_table_size, sw.mac_buckets, &sw.mac_region, &sw.mac_region_len, &recovered);
        if (rc != 0)
            fprintf(stderr, "switch: cannot attach the shared MAC table %s: %s\n", mac_name, strerror(rc));
    } else {
        sw.mac_region = malloc(mac_region_size(sw.mac_table_size, sw.mac_buckets));
        if (sw.mac_region == NULL) {
            fprintf(stderr, "switch: cannot allocate the MAC table\n");
            rc = ENOMEM;
        } else {
            rc = mac_init(sw.mac_region, sw.mac_table_size, sw.mac_buckets);
            if (rc != 0)
                fprintf(stderr, "switch: cannot initialise the MAC table: %s\n", strerror(rc));
        }
    }
    if (rc != 0) {
        admin_close(false, st.admin_thread);
        log_stop();
        stats_destroy();
        return EXIT_FAILURE;
    }
    log_msg(LOG_INFO, "MAC table: %d entries, %d buckets, aging %lds, at most %d MACs per port%s",
            sw.mac_table_size, sw.mac_buckets, atomic_load(&sw.mac_aging_sec),
            atomic_load(&sw.max_macs_per_port), persist_mac ? " (in shared memory, --persist-mac)" : "");
    if (persist_mac) {
        if (recovered >= 0)
            log_msg(LOG_INFO, "MAC table recovered: %d entries", recovered);
        else
            log_msg(LOG_INFO, "MAC table: no usable shared table found, a new one was created");
    }

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
                rc = pthread_create(&st.timer_thread, NULL, token_timer_main, NULL);
                if (rc == 0) {
                    st.timer = true;
                    rc = admin_start(&st.admin_thread);
                    if (rc == 0) {
                        st.admin = true;
                        rc = pthread_create(&st.accept_thread, NULL, accept_main, NULL);
                        if (rc == 0)
                            st.accept = true;
                        else
                            failed = "accept";
                    } else {
                        failed = "admin";
                    }
                } else {
                    failed = "token timer";
                }
            } else {
                failed = "aging";
            }
        } else {
            failed = "reaper";
        }
    } else {
        failed = "signal";
    }

    if (failed == NULL && pair) {          /* Part 19: the pipes become port 0, the trunk to the peer */
        rc = port_attach_pipe(link_r, link_w);
        st.link = true;                    /* on a failure after the first thread was started the reaper owns the pipe ends */
        if (rc == 0)
            log_msg(LOG_INFO, "port 0: trunk to the peer switch attached (%s, peer TCP port %ld)",
                    peer == 0 ? "this is switch B" : "this is switch A", peer == 0 ? tcp_port : tcp_port_b);
        else
            failed = "peer link";
    }

    if (failed != NULL) {
        fprintf(stderr, "switch: cannot start the %s thread: %s\n", failed, strerror(rc));
        request_shutdown();
        shutdown_switch(&st);
        if (peer > 0)
            reap_peer(peer);
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
    if (peer > 0)
        reap_peer(peer);                   /* switch A: wait for B before exiting (Part 19) */
    log_msg(LOG_INFO, "shutdown: complete");
    log_stop();                            /* the logger is the LAST thread to stop: it writes everything above */
    return EXIT_SUCCESS;
}
