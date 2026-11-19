/*
 * admin.c - the admin socket and its thread (Part 15). See admin.h for the protocol.
 *
 * One thread, one client at a time. Everything that can block has a bound: the request line must arrive within ADMIN_TIMEOUT_MS IN TOTAL (a client that
 * sends one byte every 1.9 s does not hold the thread), the reply is sent with a send timeout, and the wait for a client is a poll() on the socket AND the
 * switch's wake_efd (written by request_shutdown()), so admin_close() ends the thread at once whatever it is doing.
 */
#include "admin.h"

#include "common.h"
#include "log.h"
#include "mactable.h"
#include "port.h"
#include "reload.h"
#include "sig.h"

#include <ctype.h>
#include <fcntl.h>
#include <poll.h>
#include <stdarg.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

static int admin_fd = -1;                 /* listening socket (non-blocking) */
static char admin_sock_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
static bool path_bound;                   /* the path exists because of us */

/* ------------------------------------------------------------------ */
/* Parsing                                                             */
/* ------------------------------------------------------------------ */

static void set_err(char *err, size_t n, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(err, n, fmt, ap);
    va_end(ap);
}

/* A decimal number made only of digits (no sign, no spaces), at most 6 of them. */
static bool parse_num(const char *s, long *out)
{
    if (s == NULL || *s == '\0' || strlen(s) > 6)
        return false;
    long v = 0;
    for (const char *p = s; *p; p++) {
        if (!isdigit((unsigned char)*p))
            return false;
        v = v * 10 + (*p - '0');
    }
    *out = v;
    return true;
}

static int parse_port(const char *tok, int num_ports, int *out, char *err, size_t errlen)
{
    long v;
    if (!parse_num(tok, &v)) {
        set_err(err, errlen, "bad port number '%s'", tok);
        return -1;
    }
    if (v < 1 || v > num_ports) {
        set_err(err, errlen, "port %ld out of range (1-%d)", v, num_ports);
        return -1;
    }
    *out = (int)v;
    return 0;
}

int admin_parse(const char *line, int num_ports, admin_cmd_t *cmd, char *err, size_t errlen)
{
    char copy[ADMIN_MAX_LINE + 1];
    char *tok[6];
    int n = 0;

    if (line == NULL || strlen(line) > ADMIN_MAX_LINE) {
        set_err(err, errlen, "line too long");
        return -1;
    }
    snprintf(copy, sizeof copy, "%s", line);
    for (char *p = copy; *p; p++)
        if (!isprint((unsigned char)*p) && *p != '\t') {
            set_err(err, errlen, "bad character in the command");
            return -1;
        }
    for (char *p = strtok(copy, " \t"); p != NULL; p = strtok(NULL, " \t")) {
        if (n == 6) {
            set_err(err, errlen, "too many arguments");
            return -1;
        }
        tok[n++] = p;
    }
    if (n == 0) {
        set_err(err, errlen, "empty command");
        return -1;
    }
    memset(cmd, 0, sizeof *cmd);

    const char *c = tok[0];
    if (strcmp(c, "show") == 0) {
        if (n != 2) {
            set_err(err, errlen, n < 2 ? "usage: show mac|ports" : "too many arguments");
            return -1;
        }
        if (strcmp(tok[1], "mac") == 0) {
            cmd->kind = CMD_SHOW_MAC;
            return 0;
        }
        if (strcmp(tok[1], "ports") == 0) {
            cmd->kind = CMD_SHOW_PORTS;
            return 0;
        }
        set_err(err, errlen, "unknown show target '%.20s' (mac or ports)", tok[1]);
        return -1;
    }
    if (strcmp(c, "clear") == 0) {
        if (n == 2 && strcmp(tok[1], "mac") == 0) {
            cmd->kind = CMD_CLEAR_MAC;
            return 0;
        }
        set_err(err, errlen, n == 2 ? "unknown clear target '%.20s' (mac)" : "usage: clear mac", tok[1]);
        return -1;
    }
    if (strcmp(c, "shutdown") == 0 || strcmp(c, "up") == 0) {
        if (n != 2) {
            set_err(err, errlen, "usage: %s <port>", c);
            return -1;
        }
        cmd->kind = c[0] == 's' ? CMD_SHUTDOWN : CMD_UP;
        return parse_port(tok[1], num_ports, &cmd->port, err, errlen);
    }
    if (strcmp(c, "vlan") == 0) {
        long v;
        if (n != 3) {
            set_err(err, errlen, "usage: vlan <port> <vlan>");
            return -1;
        }
        if (parse_port(tok[1], num_ports, &cmd->port, err, errlen) < 0)
            return -1;
        if (!parse_num(tok[2], &v)) {
            set_err(err, errlen, "bad VLAN '%.20s'", tok[2]);
            return -1;
        }
        if (v < 1 || v > 4094) {
            set_err(err, errlen, "VLAN %ld out of range (1-4094)", v);
            return -1;
        }
        cmd->kind = CMD_VLAN;
        cmd->arg = (int)v;
        return 0;
    }
    if (strcmp(c, "mirror") == 0) {
        if (n != 3) {
            set_err(err, errlen, "usage: mirror <port> <sniffer_port>|off");
            return -1;
        }
        if (parse_port(tok[1], num_ports, &cmd->port, err, errlen) < 0)
            return -1;
        if (strcmp(tok[2], "off") == 0) {
            cmd->kind = CMD_MIRROR_OFF;
            return 0;
        }
        if (parse_port(tok[2], num_ports, &cmd->arg, err, errlen) < 0)
            return -1;
        if (cmd->arg == cmd->port) {
            set_err(err, errlen, "a port cannot mirror to itself");
            return -1;
        }
        cmd->kind = CMD_MIRROR;
        return 0;
    }
    set_err(err, errlen, "unknown command '%.20s'", c);
    return -1;
}

/* ------------------------------------------------------------------ */
/* Replies                                                             */
/* ------------------------------------------------------------------ */

/* A growing text buffer: the whole reply is built first, so an error leaves no half answer. */
typedef struct {
    char *p;
    size_t len, cap;
    bool oom;
} obuf_t;

static void ob_printf(obuf_t *b, const char *fmt, ...)
{
    va_list ap;

    if (b->oom)
        return;
    for (;;) {
        size_t room = b->cap - b->len;
        va_start(ap, fmt);
        int n = vsnprintf(b->p ? b->p + b->len : NULL, room, fmt, ap);
        va_end(ap);
        if (n < 0) {
            b->oom = true;
            return;
        }
        if ((size_t)n < room) {
            b->len += (size_t)n;
            return;
        }
        size_t ncap = b->cap ? b->cap * 2 : 4096;
        while (ncap - b->len <= (size_t)n)
            ncap *= 2;
        char *q = realloc(b->p, ncap);
        if (q == NULL) {
            b->oom = true;
            return;
        }
        b->p = q;
        b->cap = ncap;
    }
}

typedef struct {
    mac_info_t *v;
    size_t n, cap;
    bool oom;
} maclist_t;

static void collect_mac(const mac_info_t *info, void *arg)
{
    maclist_t *l = arg;

    if (l->oom)
        return;
    if (l->n == l->cap) {
        size_t ncap = l->cap ? l->cap * 2 : 256;
        mac_info_t *q = realloc(l->v, ncap * sizeof *q);
        if (q == NULL) {
            l->oom = true;
            return;
        }
        l->v = q;
        l->cap = ncap;
    }
    l->v[l->n++] = *info;
}

static int cmp_mac(const void *a, const void *b)
{
    const mac_info_t *x = a, *y = b;
    if (x->vlan != y->vlan)
        return x->vlan < y->vlan ? -1 : 1;
    return memcmp(x->mac, y->mac, MAC_LEN);
}

static const char *state_name(enum port_state s)
{
    switch (s) {
    case PORT_FREE:     return "FREE";
    case PORT_UP:       return "UP";
    case PORT_CLOSING:  return "CLOSING";
    case PORT_DISABLED: return "DISABLED";
    }
    return "?";
}

/* show mac: "# vlan mac port age_s" then one line per entry, sorted by (vlan, mac) - the format of the SIGUSR1 dump. */
static const char *do_show_mac(obuf_t *out)
{
    maclist_t l = { 0 };

    if (mac_foreach(collect_mac, &l) < 0 || l.oom) {
        free(l.v);
        return "out of memory";
    }
    qsort(l.v, l.n, sizeof *l.v, cmp_mac);
    ob_printf(out, "# vlan mac port age_s\n");
    for (size_t i = 0; i < l.n; i++) {
        char m[MAC_STR_LEN];
        mac_format(l.v[i].mac, m, sizeof m);
        ob_printf(out, "%u %s %d %ld\n", (unsigned)l.v[i].vlan, m, l.v[i].port, l.v[i].age_ms / 1000);
    }
    free(l.v);
    return NULL;
}

/* show ports: a table, read under the ports_lock (read) so that every row is consistent; mac_count and mirror_to are atomics. */
static const char *do_show_ports(obuf_t *out)
{
    struct { enum port_state st; unsigned vlan; bool trunk; int macs, mirror; } row[MAX_PORTS + 1];

    config_t cfg;
    config_snapshot(&cfg);                                  /* config_lock is a leaf; a copy, used after it is released */

    CHECK_PT(pthread_rwlock_rdlock(&sw.ports_lock));
    for (int i = 1; i <= sw.num_ports; i++) {
        const port_t *p = &sw.ports[i];
        row[i].st = p->state;
        row[i].macs = atomic_load(&p->mac_count);
        row[i].mirror = atomic_load(&p->mirror_to);
        if (p->state == PORT_UP || p->state == PORT_CLOSING) {
            row[i].vlan = p->vlan;                          /* what the connected host really has (SIGHUP and Part 16 change it) */
            row[i].trunk = p->trunk;
        } else {                                            /* no host: the VLAN and role the next host will get (port.c applies them on connect) */
            row[i].vlan = p->vlan_admin ? p->vlan_admin : (unsigned)config_port_vlan(&cfg, i);   /* Part 16: swctl vlan wins */
            row[i].trunk = cfg.trunk_port == i;
        }
    }
    CHECK_PT(pthread_rwlock_unlock(&sw.ports_lock));

    ob_printf(out, "PORT STATE    VLAN TRUNK MACS MIRROR\n");
    for (int i = 1; i <= sw.num_ports; i++) {
        char vlan[8], mirror[8];
        if (row[i].trunk)
            snprintf(vlan, sizeof vlan, "-");               /* the trunk carries every VLAN */
        else
            snprintf(vlan, sizeof vlan, "%u", row[i].vlan);
        if (row[i].mirror > 0)
            snprintf(mirror, sizeof mirror, "%d", row[i].mirror);
        else
            snprintf(mirror, sizeof mirror, "-");
        ob_printf(out, "%-4d %-8s %-4s %-5s %-4d %s\n", i, state_name(row[i].st), vlan, row[i].trunk ? "yes" : "no", row[i].macs, mirror);
    }
    return NULL;
}

/* Run a parsed command. Returns NULL for OK (the text is in out) or the reason of an ERR. */
static const char *execute(const admin_cmd_t *c, obuf_t *out, int *cleared)
{
    switch (c->kind) {
    case CMD_SHOW_MAC:
        return do_show_mac(out);
    case CMD_SHOW_PORTS:
        return do_show_ports(out);
    case CMD_CLEAR_MAC:
        *cleared = admin_clear_mac();
        ob_printf(out, "cleared %d MAC entr%s\n", *cleared, *cleared == 1 ? "y" : "ies");
        return NULL;
    case CMD_SHUTDOWN: {
        const char *why = admin_shutdown(c->port);
        if (why == NULL)
            ob_printf(out, "port %d disabled\n", c->port);
        return why;
    }
    case CMD_UP: {
        const char *why = admin_up(c->port);
        if (why == NULL)
            ob_printf(out, "port %d is FREE again\n", c->port);
        return why;
    }
    case CMD_VLAN: {
        const char *why = admin_vlan(c->port, c->arg);
        if (why == NULL)
            ob_printf(out, "port %d: VLAN %d\n", c->port, c->arg);
        return why;
    }
    case CMD_MIRROR:
        /* Validation that needs the switch's state (the roadmap, Part 17: the sniffer is not the trunk). */
        config_t cfg;
        config_snapshot(&cfg);
        bool trunk = cfg.trunk_port == c->arg;
        CHECK_PT(pthread_rwlock_rdlock(&sw.ports_lock));
        trunk = trunk || sw.ports[c->arg].trunk;
        CHECK_PT(pthread_rwlock_unlock(&sw.ports_lock));
        if (trunk)
            return "the sniffer port is the trunk";
        return "not implemented yet (Part 17)";
    case CMD_MIRROR_OFF:
        return "not implemented yet (Part 17)";
    }
    return "internal error";
}

/* ------------------------------------------------------------------ */
/* One client                                                          */
/* ------------------------------------------------------------------ */

/* Printable copy of a request for the log (the client may send anything). */
static void sanitize(const char *s, char *out, size_t n)
{
    size_t i = 0;
    for (; s[i] && i + 1 < n; i++)
        out[i] = isprint((unsigned char)s[i]) ? s[i] : '?';
    out[i] = '\0';
}

enum read_rc { READ_OK, READ_TIMEOUT, READ_TOO_LONG, READ_BAD, READ_CLOSED, READ_STOP };

/* Read the request line: up to '\n', at most ADMIN_MAX_LINE bytes, ADMIN_TIMEOUT_MS in total. The line is returned without '\n' (and without a '\r'). */
static enum read_rc read_line(int fd, char *line)
{
    size_t len = 0;
    struct timespec deadline = deadline_mono(ADMIN_TIMEOUT_MS);

    for (;;) {
        long left = ts_diff_ms(deadline, now_mono());
        if (left <= 0)
            return READ_TIMEOUT;
        struct pollfd p[2] = { { fd, POLLIN, 0 }, { sw.wake_efd, POLLIN, 0 } };
        int r = poll(p, 2, (int)left);
        if (r < 0) {
            if (errno == EINTR)
                continue;
            return READ_CLOSED;
        }
        if (p[1].revents)
            return READ_STOP;                         /* shutdown */
        if (r == 0)
            return READ_TIMEOUT;
        char buf[ADMIN_MAX_LINE + 1];
        ssize_t n = recv(fd, buf, sizeof buf, MSG_DONTWAIT);
        if (n < 0) {
            if (errno == EINTR || errno == EAGAIN)
                continue;
            return READ_CLOSED;
        }
        if (n == 0)
            return READ_CLOSED;
        for (ssize_t i = 0; i < n; i++) {
            if (buf[i] == '\0')                           /* would end the C string early: "show mac\0junk" must not pass as "show mac" */
                return READ_BAD;
            if (buf[i] == '\n') {
                if (len > 0 && line[len - 1] == '\r')
                    len--;
                line[len] = '\0';
                return READ_OK;
            }
            if (len >= ADMIN_MAX_LINE - 1)            /* the '\n' would be byte number ADMIN_MAX_LINE + 1 */
                return READ_TOO_LONG;
            line[len++] = buf[i];
        }
    }
}

/* Send everything, ADMIN_TIMEOUT_MS in total (also SO_SNDTIMEO on the socket). The wait is a poll() on the client AND wake_efd: a client that never
 * reads cannot delay the shutdown. Returns false if the reply was not sent completely. */
static bool send_all(int fd, const char *p, size_t n)
{
    struct timespec deadline = deadline_mono(ADMIN_TIMEOUT_MS);

    while (n > 0) {
        ssize_t w = send(fd, p, n, MSG_NOSIGNAL | MSG_DONTWAIT);
        if (w > 0) {
            p += w;
            n -= (size_t)w;
            continue;
        }
        if (w < 0 && errno == EINTR)
            continue;
        if (w < 0 && errno != EAGAIN && errno != EWOULDBLOCK)
            return false;                                 /* EPIPE, ECONNRESET... */
        long left = ts_diff_ms(deadline, now_mono());
        if (left <= 0)
            return false;
        struct pollfd pf[2] = { { fd, POLLOUT, 0 }, { sw.wake_efd, POLLIN, 0 } };
        int r = poll(pf, 2, (int)left);
        if (r < 0 && errno != EINTR)
            return false;
        if (r > 0 && pf[1].revents)
            return false;                                 /* shutdown */
    }
    return true;
}

static void reply_err(int fd, const char *reason)
{
    char b[256];
    int n = snprintf(b, sizeof b, "ERR %s\n", reason);
    send_all(fd, b, (size_t)(n < (int)sizeof b ? n : (int)sizeof b - 1));
}

static void serve_one(int fd)
{
    struct timeval tv = { ADMIN_TIMEOUT_MS / 1000, (ADMIN_TIMEOUT_MS % 1000) * 1000 };
    char line[ADMIN_MAX_LINE + 1];
    char err[128], shown[96];
    admin_cmd_t cmd;
    struct ucred cred;
    socklen_t cl = sizeof cred;

    (void)setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    (void)setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);

    /* Only the user who runs the switch may give it commands (the socket file is 0600 too). */
    if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &cred, &cl) == 0 && cred.uid != geteuid()) {
        log_msg(LOG_WARN, "admin: connection from uid %d refused", (int)cred.uid);
        reply_err(fd, "permission denied");
        return;
    }

    switch (read_line(fd, line)) {
    case READ_OK:
        break;
    case READ_TIMEOUT:
        log_msg(LOG_WARN, "admin: client sent no complete line within %d ms", ADMIN_TIMEOUT_MS);
        reply_err(fd, "timeout waiting for the command line");
        return;
    case READ_TOO_LONG:
        log_msg(LOG_WARN, "admin: request line longer than %d bytes refused", ADMIN_MAX_LINE);
        reply_err(fd, "line too long (maximum 256 bytes)");
        return;
    case READ_BAD:
        log_msg(LOG_WARN, "admin: request with a NUL byte refused");
        reply_err(fd, "bad character in the command");
        return;
    case READ_CLOSED:
    case READ_STOP:
        return;
    }

    sanitize(line, shown, sizeof shown);
    if (admin_parse(line, sw.num_ports, &cmd, err, sizeof err) < 0) {
        log_msg(LOG_WARN, "admin: bad command '%s': %s", shown, err);
        reply_err(fd, err);
        return;
    }

    obuf_t out = { 0 };
    int cleared = 0;
    const char *why = execute(&cmd, &out, &cleared);
    if (why == NULL && out.oom)
        why = "out of memory";
    if (why != NULL) {
        log_msg(LOG_INFO, "admin: '%s' -> ERR %s", shown, why);
        reply_err(fd, why);
    } else {
        if (cmd.kind == CMD_CLEAR_MAC)
            log_msg(LOG_INFO, "admin: clear mac: %d entr%s removed", cleared, cleared == 1 ? "y" : "ies");
        else
            log_msg(LOG_INFO, "admin: '%s' -> OK", shown);
        send_all(fd, out.p ? out.p : "", out.len);
        send_all(fd, "OK\n", 3);
    }
    free(out.p);
}

/* Unread bytes of the client would make close() reset the connection and could destroy the reply (an ERR for a request that was too long, for
 * example): say "no more from me" and read away what the client already sent (a bounded amount, non-blocking). */
static void serve(int fd)
{
    char junk[4096];

    serve_one(fd);
    (void)shutdown(fd, SHUT_WR);
    for (int i = 0; i < 256 && recv(fd, junk, sizeof junk, MSG_DONTWAIT) > 0; i++)
        ;
}

/* ------------------------------------------------------------------ */
/* Socket and thread                                                   */
/* ------------------------------------------------------------------ */

int admin_open(int tcp_port)
{
    struct sockaddr_un addr;
    int rc;

    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    admin_path(tcp_port, admin_sock_path, sizeof admin_sock_path);
    snprintf(addr.sun_path, sizeof addr.sun_path, "%s", admin_sock_path);

    admin_fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (admin_fd < 0)
        return errno;
    /* A path left by a crashed switch (or anything else) is stale: only one switch can own this TCP port, and the TCP listener already exists. */
    if (unlink(admin_sock_path) < 0 && errno != ENOENT) {
        rc = errno;
        goto fail;
    }
    if (bind(admin_fd, (struct sockaddr *)&addr, sizeof addr) < 0) {
        rc = errno;
        goto fail;
    }
    path_bound = true;
    if (chmod(admin_sock_path, 0600) < 0 || listen(admin_fd, 8) < 0) {
        rc = errno;
        goto fail;
    }
    return 0;

fail:
    admin_close(false, (pthread_t)0);
    return rc;
}

static void *admin_main(void *arg)
{
    (void)arg;
    int nrc = pthread_setname_np(pthread_self(), "admin");
    if (nrc != 0)
        log_msg(LOG_WARN, "cannot set thread name: %s", strerror(nrc));
    for (;;) {
        struct pollfd p[2] = { { admin_fd, POLLIN, 0 }, { sw.wake_efd, POLLIN, 0 } };
        int r = poll(p, 2, -1);
        if (r < 0) {
            if (errno != EINTR)
                switch_wait_ms(100);
            continue;
        }
        if (p[1].revents)
            break;                                    /* admin_close(): shutdown */
        if (p[0].revents & (POLLERR | POLLHUP | POLLNVAL)) {
            log_msg(LOG_ERR, "admin: the listening socket is broken; the admin thread stops");
            break;
        }
        if (p[0].revents & POLLIN) {
            int c = accept4(admin_fd, NULL, NULL, SOCK_CLOEXEC);
            if (c < 0) {
                if (errno != EINTR && errno != EAGAIN && errno != ECONNABORTED) {
                    log_msg(LOG_ERR, "admin: accept failed: %s", strerror(errno));
                    switch_wait_ms(100);              /* no busy loop (EMFILE...) */
                }
                continue;
            }
            serve(c);
            close(c);
        }
    }
    return NULL;
}

int admin_start(pthread_t *thread)
{
    return pthread_create(thread, NULL, admin_main, NULL);
}

void admin_close(bool started, pthread_t thread)
{
    if (started) {
        request_shutdown();                           /* idempotent: writes sw.wake_efd, which the admin thread polls (it is never drained) */
        if (admin_fd >= 0)
            (void)shutdown(admin_fd, SHUT_RDWR);      /* breaks an accept() that may be waiting */
        int rc = pthread_join(thread, NULL);
        if (rc != 0)
            log_msg(LOG_ERR, "admin: join of the admin thread failed: %s", strerror(rc));
    }
    if (admin_fd >= 0) {
        close(admin_fd);
        admin_fd = -1;
    }
    if (path_bound) {
        (void)unlink(admin_sock_path);
        path_bound = false;
    }
}
