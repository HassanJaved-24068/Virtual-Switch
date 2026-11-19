/*
 * swctl.c - the admin client of the switch (Part 15).
 *
 *   ./swctl [-p tcp_port] <command>          default TCP port 9000
 *
 *   show mac | show ports | clear mac | shutdown <port> | up <port> | vlan <port> <vlan> | mirror <port> <sniffer> | mirror <port> off
 *       one line to the admin socket /tmp/vswitch_<tcp_port>.sock; the reply lines are printed, the last line is OK or ERR <reason>
 *   stats
 *   stats --watch
 *       do NOT use the socket: swctl maps the shared-memory counters /vswitch_stats_P, locks the process-shared mutex, copies the counters, unlocks and
 *       prints a table. --watch repeats it at every whole second (clock_nanosleep with an absolute time) and ends on SIGINT.
 *
 * Exit status: 0 = OK, 1 = ERR (or a usage / system error), 2 = the switch is not running (no socket, connection refused, no segment).
 *
 * The mapping: the control page (magic, version, mutex) is mapped read-write because locking a mutex writes to it; the data area (the counters) is
 * mapped PROT_READ only, so swctl cannot change a counter even by mistake.
 */
#include "common.h"

#include <ctype.h>
#include "admin.h"
#include "stats.h"

#include <fcntl.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#define EXIT_OK 0
#define EXIT_ERR 1
#define EXIT_NOT_RUNNING 2
#define DEFAULT_TCP_PORT 9000

/* close / munmap with the result checked: a failure is reported on stderr (nothing else can be done at this point). */
static void xclose(int fd)
{
    if (close(fd) < 0)
        fprintf(stderr, "swctl: close failed: %s\n", strerror(errno));
}

static void xmunmap(void *p, size_t n)
{
    if (munmap(p, n) < 0)
        fprintf(stderr, "swctl: munmap failed: %s\n", strerror(errno));
}

static void usage(void)
{
    fprintf(stderr,
            "usage: swctl [-p tcp_port] <command>\n"
            "  show mac | show ports | clear mac | stats | stats --watch\n"
            "  shutdown <port> | up <port> | vlan <port> <vlan> | mirror <port> <sniffer_port> | mirror <port> off\n"
            "  (default tcp_port %d)\n", DEFAULT_TCP_PORT);
}

/* ------------------------------------------------------------------ */
/* Commands for the admin socket                                       */
/* ------------------------------------------------------------------ */

static int send_command(int tcp_port, const char *line)
{
    struct sockaddr_un addr;
    char path[sizeof addr.sun_path];

    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    admin_path(tcp_port, path, sizeof path);
    snprintf(addr.sun_path, sizeof addr.sun_path, "%s", path);

    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        fprintf(stderr, "swctl: socket: %s\n", strerror(errno));
        return EXIT_ERR;
    }
    if (connect(fd, (struct sockaddr *)&addr, sizeof addr) < 0) {
        int e = errno;
        xclose(fd);
        if (e == ENOENT || e == ECONNREFUSED) {
            fprintf(stderr, "swctl: switch not running (nothing listens on %s)\n", path);
            return EXIT_NOT_RUNNING;
        }
        fprintf(stderr, "swctl: cannot connect to %s: %s\n", path, strerror(e));
        return EXIT_ERR;
    }
    struct timeval tv = { 10, 0 };                    /* a stuck switch must not hang swctl for ever */
    (void)setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    (void)setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);

    char req[ADMIN_MAX_LINE + 2];
    int n = snprintf(req, sizeof req, "%s\n", line);
    for (int off = 0; off < n;) {
        ssize_t w = send(fd, req + off, (size_t)(n - off), MSG_NOSIGNAL);
        if (w < 0) {
            if (errno == EINTR)
                continue;
            fprintf(stderr, "swctl: cannot send the command: %s\n", strerror(errno));
            xclose(fd);
            return EXIT_ERR;
        }
        off += (int)w;
    }

    /* Read the whole reply (the switch closes when it is done). */
    size_t len = 0, cap = 4096;
    char *buf = malloc(cap);
    if (buf == NULL) {
        xclose(fd);
        fprintf(stderr, "swctl: out of memory\n");
        return EXIT_ERR;
    }
    for (;;) {
        if (len + 1024 > cap) {
            char *q = realloc(buf, cap * 2);
            if (q == NULL) {
                fprintf(stderr, "swctl: out of memory\n");
                free(buf);
                xclose(fd);
                return EXIT_ERR;
            }
            buf = q;
            cap *= 2;
        }
        ssize_t r = recv(fd, buf + len, cap - len - 1, 0);
        if (r < 0 && errno == EINTR)
            continue;
        if (r < 0) {
            fprintf(stderr, "swctl: reading the reply: %s\n", strerror(errno));
            free(buf);
            xclose(fd);
            return EXIT_ERR;
        }
        if (r == 0)
            break;
        len += (size_t)r;
    }
    xclose(fd);
    buf[len] = '\0';

    /* The last line is the status; everything before it is the answer. */
    while (len > 0 && buf[len - 1] == '\n')
        buf[--len] = '\0';
    char *last = strrchr(buf, '\n');
    char *status = last ? last + 1 : buf;
    int rc;
    if (strcmp(status, "OK") == 0) {
        if (last) {
            *last = '\0';
            printf("%s\n", buf);
        }
        rc = EXIT_OK;
    } else if (strncmp(status, "ERR", 3) == 0 && (status[3] == '\0' || status[3] == ' ')) {
        fprintf(stderr, "swctl: %s\n", status);
        rc = EXIT_ERR;
    } else {
        fprintf(stderr, "swctl: no OK or ERR from the switch (it closed the connection)\n");
        rc = EXIT_ERR;
    }
    free(buf);
    return rc;
}

/* ------------------------------------------------------------------ */
/* stats: read the shared memory                                       */
/* ------------------------------------------------------------------ */

typedef struct {
    stats_ctl_t *ctl;                 /* read-write: the mutex */
    const stats_data_t *data;         /* read only */
    size_t page, len;
} segment_t;

/* Open and map the segment. Returns 0, EXIT_NOT_RUNNING (no segment) or EXIT_ERR; prints the reason. */
static int segment_open(segment_t *s, int tcp_port)
{
    char name[64];
    s->page = stats_page_size();
    s->len = stats_shm_size(s->page);
    if (s->len == 0) {
        fprintf(stderr, "swctl: unusable page size\n");
        return EXIT_ERR;
    }
    /* The second switch of a --pair (Part 19) has a segment named by its TCP port; every other switch uses the fixed name. */
    snprintf(name, sizeof name, STATS_SHM_PAIR_FMT, tcp_port);
    int fd = shm_open(name, O_RDWR, 0);               /* O_RDWR: a mutex cannot be locked through a read-only mapping */
    if (fd < 0 && errno == ENOENT) {
        snprintf(name, sizeof name, "%s", STATS_SHM_NAME);
        fd = shm_open(name, O_RDWR, 0);
    }
    if (fd < 0) {
        if (errno == ENOENT) {
            fprintf(stderr, "swctl: switch not running (no shared-memory segment %s)\n", STATS_SHM_NAME);
            return EXIT_NOT_RUNNING;
        }
        fprintf(stderr, "swctl: cannot open %s: %s\n", name, strerror(errno));
        return EXIT_ERR;
    }
    struct stat st;
    if (fstat(fd, &st) < 0 || (size_t)st.st_size != s->len) {
        fprintf(stderr, "swctl: %s has the wrong size (%ld bytes, expected %zu)\n", name, (long)st.st_size, s->len);
        xclose(fd);
        return EXIT_ERR;
    }
    void *c = mmap(NULL, s->page, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    void *d = mmap(NULL, s->len - s->page, PROT_READ, MAP_SHARED, fd, (off_t)s->page);
    int e = errno;
    xclose(fd);
    if (c == MAP_FAILED || d == MAP_FAILED) {
        fprintf(stderr, "swctl: cannot map %s: %s\n", name, strerror(e));
        if (c != MAP_FAILED)
            xmunmap(c, s->page);
        if (d != MAP_FAILED)
            xmunmap(d, s->len - s->page);
        return EXIT_ERR;
    }
    s->ctl = c;
    s->data = d;
    return 0;
}

static void segment_close(segment_t *s)
{
    xmunmap(s->ctl, s->page);
    xmunmap((void *)s->data, s->len - s->page);
}

static const char *state_name(int st)
{
    static const char *names[] = { "FREE", "UP", "CLOSING", "DISABLED" };
    return st >= 0 && st < 4 ? names[st] : "?";
}

/* One snapshot: validate, lock, copy, unlock, print. Returns 0, EXIT_NOT_RUNNING (the switch stopped) or EXIT_ERR. */
static int print_stats(const segment_t *s)
{
    stats_port_t copy[MAX_PORTS + 1];
    int nports;

    int rc = stats_validate(s->ctl);
    if (rc == EINVAL) {                               /* magic 0: shut down (or not yet initialised) */
        fprintf(stderr, "swctl: switch not running (the shared counters are not valid)\n");
        return EXIT_NOT_RUNNING;
    }
    if (rc != 0) {
        fprintf(stderr, "swctl: the shared counters have another version (%u, expected %u)\n", s->ctl->version, STATS_VERSION);
        return EXIT_ERR;
    }
    rc = stats_lock(&s->ctl->lock);                   /* handles a dead previous owner (EOWNERDEAD) */
    if (rc != 0) {
        fprintf(stderr, "swctl: cannot lock the shared counters: %s\n", strerror(rc));
        return EXIT_ERR;
    }
    nports = s->ctl->num_ports;
    if (nports >= 0 && nports <= MAX_PORTS)
        memcpy(copy, s->data->port, (size_t)(nports + 1) * sizeof copy[0]);
    stats_unlock(&s->ctl->lock);
    if (nports < 0 || nports > MAX_PORTS) {
        fprintf(stderr, "swctl: the shared counters are damaged (num_ports = %d)\n", nports);
        return EXIT_ERR;
    }

    printf("%-4s %-8s %-4s %12s %12s %12s %12s\n", "PORT", "STATE", "VLAN", "IN", "OUT", "DROPPED", "MIRRORED");
    for (int i = copy[0].state != 0 ? 0 : 1; i <= nports; i++)    /* Part 19: row 0 = the pipe trunk of a --pair (shown once it has been attached) */
        printf("%-4d %-8s %-4d %12llu %12llu %12llu %12llu\n", i, state_name(copy[i].state), copy[i].vlan,
               (unsigned long long)copy[i].in, (unsigned long long)copy[i].out,
               (unsigned long long)copy[i].dropped, (unsigned long long)copy[i].mirrored);
    return 0;
}

static volatile sig_atomic_t stop_watch;

static void on_sigint(int sig)
{
    (void)sig;
    stop_watch = 1;
}

static int cmd_stats(bool watch, int tcp_port)
{
    segment_t seg;
    int rc = segment_open(&seg, tcp_port);
    if (rc != 0)
        return rc;

    if (!watch) {
        rc = print_stats(&seg);
        segment_close(&seg);
        return rc;
    }

    /* A handler without SA_RESTART: it only sets a flag, and it interrupts the clock_nanosleep below. (swctl is a plain program: the
     * "no signal handlers" rule is the switch's.) */
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_sigint;
    sigemptyset(&sa.sa_mask);
    CHECK(sigaction(SIGINT, &sa, NULL));
    CHECK(sigaction(SIGTERM, &sa, NULL));

    struct timespec next = now_mono();
    rc = 0;
    while (!stop_watch) {
        fputs("\033[H\033[2J", stdout);               /* cursor home + clear screen */
        rc = print_stats(&seg);
        if (fflush(stdout) != 0) {                    /* stdout is gone (a closed pipe): end the watch */
            fprintf(stderr, "swctl: cannot write to stdout: %s\n", strerror(errno));
            rc = EXIT_ERR;
            break;
        }
        if (rc != 0)
            break;
        next.tv_sec += 1;                             /* the next WHOLE second of the monotonic clock: no drift, no busy loop */
        next.tv_nsec = 0;
        while (!stop_watch) {
            int e = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
            if (e == 0)
                break;
            if (e != EINTR) {
                fprintf(stderr, "swctl: clock_nanosleep: %s\n", strerror(e));
                rc = EXIT_ERR;
                stop_watch = 1;
            }
        }
    }
    segment_close(&seg);
    return rc;
}

/* ------------------------------------------------------------------ */

int main(int argc, char **argv)
{
    int tcp_port = DEFAULT_TCP_PORT;
    int i = 1;

    if (i < argc && strcmp(argv[i], "-p") == 0) {
        long v;
        if (i + 1 >= argc) {
            usage();
            return EXIT_ERR;
        }
        if (parse_uint(argv[i + 1], 1, 65535, &v) < 0) {
            fprintf(stderr, "swctl: bad TCP port '%s' (1-65535)\n", argv[i + 1]);
            return EXIT_ERR;
        }
        tcp_port = (int)v;
        i += 2;
    }
    if (i >= argc) {
        usage();
        return EXIT_ERR;
    }

    if (strcmp(argv[i], "stats") == 0) {
        if (i + 1 == argc)
            return cmd_stats(false, tcp_port);
        if (i + 2 == argc && strcmp(argv[i + 1], "--watch") == 0)
            return cmd_stats(true, tcp_port);
        fprintf(stderr, "swctl: usage: stats [--watch]\n");
        return EXIT_ERR;
    }

    /* Everything else is one line for the switch (it validates it). */
    char line[ADMIN_MAX_LINE + 1];
    size_t len = 0;
    line[0] = '\0';
    for (; i < argc; i++) {
        size_t n = strlen(argv[i]);
        /* The words of the command are joined by single spaces and the switch splits the line at white space again: an argument that is empty or begins or ends with white
         * (" 1", "1 ") would silently become the word "1", so it is refused here, like a sign in a number. White space INSIDE an argument is allowed: `swctl 'show ports'` (one
         * argument) has always worked and still does. */
        if (n == 0 || isspace((unsigned char)argv[i][0]) || isspace((unsigned char)argv[i][n - 1])) {
            fprintf(stderr, "swctl: bad argument '%s': it must not be empty or begin or end with white space\n", argv[i]);
            return EXIT_ERR;
        }
        if (len + n + 2 > ADMIN_MAX_LINE) {
            fprintf(stderr, "swctl: the command is too long (maximum %d bytes)\n", ADMIN_MAX_LINE - 1);
            return EXIT_ERR;
        }
        len += (size_t)snprintf(line + len, sizeof line - len, "%s%s", len ? " " : "", argv[i]);
    }
    return send_command(tcp_port, line);
}
