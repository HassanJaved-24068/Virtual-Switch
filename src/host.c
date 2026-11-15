/*
 * host.c - the test host.
 *
 *   ./host <ip> <port> <mac> [options]
 *
 * Two threads:
 *   - main thread: reads commands from stdin and sends frames
 *   - receiver thread: reads frames from the socket and prints them
 *
 * Options: --noread  --partial  --flood N --to <mac>  --die-midframe  --quiet
 * Exit codes: 0 normal, 1 error, 2 "switch full".
 */
#include "common.h"
#include "frame.h"

#include <arpa/inet.h>
#include <signal.h>
#include <stdatomic.h>
#include <sys/socket.h>
#include <unistd.h>

#define EXIT_FULL 2

/* Settings from the command line. */
static struct {
    bool noread, partial, die_midframe, quiet, flood;
    long flood_count;
    mac_t me;        /* our own MAC: used as src of every frame */
    mac_t flood_to;
} opt;

static int sock = -1;
static _Atomic unsigned long rx_count;       /* frames received (for --quiet) */
static pthread_mutex_t finish_lock = PTHREAD_MUTEX_INITIALIZER;

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

/*
 * Leave the program. Only the first caller gets through (the lock is never
 * released), so the receiver thread and the main thread cannot both finish.
 * _exit() is used so no other thread can get in the way during exit.
 */
static void host_finish(int code)
{
    CHECK_PT(pthread_mutex_lock(&finish_lock));
    if (opt.quiet)
        printf("RX_TOTAL %lu\n", (unsigned long)rx_count);
    fflush(stdout);
    fflush(stderr);
    _exit(code);
}

static void usage(const char *prog)
{
    fprintf(stderr,
            "usage: %s <ip> <port> <mac> [--noread] [--partial] [--quiet]\n"
            "          [--flood N --to <mac>] [--die-midframe]\n"
            "stdin commands: send <dst|bcast> <text>\n"
            "                sendx <dst|bcast> <vlan> <prio> <text>\n"
            "                quit\n", prog);
}

/* Parse a whole string as a long in [lo, hi]. Returns 0 or -1. */
static int parse_long(const char *s, long lo, long hi, long *out)
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

/* "bcast" or a MAC string -> 6 bytes. Returns 0 or -1. */
static int parse_dst(const char *s, mac_t out)
{
    if (s != NULL && strcmp(s, "bcast") == 0) {
        memset(out, 0xFF, MAC_LEN);
        return 0;
    }
    return mac_parse(s, out);
}

/* ------------------------------------------------------------------ */
/* Sending                                                             */
/* ------------------------------------------------------------------ */

static void sleep_1ms(void)
{
    /* Only used by the --partial test option, not for synchronisation. */
    struct timespec ts = {0, 1000000L};
    nanosleep(&ts, NULL);
}

/* Build and send one frame from our MAC. Returns 0 or -1 (errno set). */
static int send_frame(const mac_t dst, uint16_t vlan, uint8_t prio,
                      const uint8_t *payload, size_t len)
{
    frame_hdr_t h;

    h.len = (uint16_t)len;
    memcpy(h.src, opt.me, MAC_LEN);
    memcpy(h.dst, dst, MAC_LEN);
    h.vlan = vlan;
    h.prio = prio;

    if (!opt.partial)
        return frame_send(sock, &h, payload);

    /* --partial: one byte at a time, 1 ms apart. */
    uint8_t buf[HDR_LEN + MAX_PAYLOAD];
    int total = frame_pack(&h, payload, buf);
    if (total < 0)
        return -1;
    for (int i = 0; i < total; i++) {
        if (write_full(sock, &buf[i], 1) < 0)
            return -1;
        sleep_1ms();
    }
    return 0;
}

/* Send and exit with code 1 if the connection is broken. */
static void send_or_die(const mac_t dst, uint16_t vlan, uint8_t prio,
                        const char *text)
{
    size_t len = strlen(text);

    if (send_frame(dst, vlan, prio, (const uint8_t *)text, len) < 0) {
        fprintf(stderr, "host: send failed: %s\n", strerror(errno));
        host_finish(1);
    }
}

/* --die-midframe: write half a frame, then exit. */
static void die_midframe(void)
{
    static const char text[] = "half-frame-payload";   /* 18 bytes */
    uint8_t buf[HDR_LEN + MAX_PAYLOAD];
    frame_hdr_t h;

    h.len = sizeof text - 1;
    memcpy(h.src, opt.me, MAC_LEN);
    memset(h.dst, 0xFF, MAC_LEN);
    h.vlan = DEFAULT_VLAN;
    h.prio = 1;
    int total = frame_pack(&h, (const uint8_t *)text, buf);
    if (total < 0) {
        fprintf(stderr, "host: pack failed\n");
        host_finish(1);
    }
    if (write_full(sock, buf, (size_t)total / 2) < 0) {
        fprintf(stderr, "host: write failed: %s\n", strerror(errno));
        host_finish(1);
    }
    CHECK(close(sock));
    exit(0);
}

/* ------------------------------------------------------------------ */
/* Receiving                                                           */
/* ------------------------------------------------------------------ */

/* The fixed "switch full" reply frame (see roadmap Part 5). */
static bool is_switch_full(const frame_hdr_t *h, const uint8_t *data)
{
    static const mac_t zero = {0, 0, 0, 0, 0, 0};
    static const char msg[] = "switch full";

    return memcmp(h->src, zero, MAC_LEN) == 0 && mac_is_broadcast(h->dst) &&
           h->vlan == 1 && h->prio == 0 && h->len == sizeof msg - 1 &&
           memcmp(data, msg, sizeof msg - 1) == 0;
}

/* Print one RX line. Unprintable bytes become \xNN, backslash becomes \\. */
static void print_rx(const frame_hdr_t *h, const uint8_t *data)
{
    char line[MAX_PAYLOAD * 4 + 200];
    char src[MAC_STR_LEN], dst[MAC_STR_LEN];
    size_t n;

    mac_format(h->src, src, sizeof src);
    mac_format(h->dst, dst, sizeof dst);
    n = (size_t)snprintf(line, sizeof line,
                         "RX src=%s dst=%s vlan=%u prio=%u len=%u data=",
                         src, dst, h->vlan, h->prio, h->len);
    for (unsigned i = 0; i < h->len; i++) {
        uint8_t c = data[i];
        if (c == '\\')
            n += (size_t)snprintf(line + n, sizeof line - n, "\\\\");
        else if (c >= 0x20 && c < 0x7f)
            line[n++] = (char)c;
        else
            n += (size_t)snprintf(line + n, sizeof line - n, "\\x%02x", c);
    }
    line[n++] = '\n';
    fwrite(line, 1, n, stdout);
    fflush(stdout);                 /* tests read our output line by line */
}

static void *receiver_main(void *arg)
{
    uint8_t data[MAX_PAYLOAD];
    frame_hdr_t h;

    (void)arg;
    for (;;) {
        enum frame_rc rc = frame_recv(sock, &h, data);

        if (rc == FR_OK) {
            if (is_switch_full(&h, data)) {
                printf("SWITCH FULL\n");
                host_finish(EXIT_FULL);
            }
            atomic_fetch_add(&rx_count, 1);
            if (!opt.quiet)
                print_rx(&h, data);
        } else if (rc == FR_EOF) {
            fprintf(stderr, "host: connection closed by switch\n");
            host_finish(0);
        } else {
            fprintf(stderr, "host: receive failed: %s%s%s\n", frame_rc_str(rc),
                    rc == FR_ERR ? ": " : "",
                    rc == FR_ERR ? strerror(errno) : "");
            host_finish(1);
        }
    }
    return NULL;
}

/* ------------------------------------------------------------------ */
/* stdin commands                                                      */
/* ------------------------------------------------------------------ */

/*
 * Split off the next word of *p. The word ends at the first space; that one
 * space is skipped, so the rest of the line (*p) keeps any further spaces.
 * Returns NULL if there is no word.
 */
static char *next_word(char **p)
{
    char *s = *p;

    if (s == NULL)
        return NULL;
    while (*s == ' ')
        s++;
    if (*s == '\0') {
        *p = NULL;
        return NULL;
    }
    char *end = strchr(s, ' ');
    if (end != NULL) {
        *end = '\0';
        *p = end + 1;
    } else {
        *p = NULL;
    }
    return s;
}

/* The rest of the line is the text; no text means an empty payload. */
static int check_text(const char *text)
{
    if (strlen(text) > MAX_PAYLOAD) {
        fprintf(stderr, "host: text too long (max %d bytes)\n", MAX_PAYLOAD);
        return -1;
    }
    return 0;
}

static void cmd_send(char *rest)
{
    char *dst_s = next_word(&rest);
    mac_t dst;

    if (parse_dst(dst_s, dst) < 0) {
        fprintf(stderr, "host: usage: send <dst_mac|bcast> <text>\n");
        return;
    }
    const char *text = rest != NULL ? rest : "";
    if (check_text(text) < 0)
        return;
    send_or_die(dst, DEFAULT_VLAN, 1, text);
}

static void cmd_sendx(char *rest)
{
    char *dst_s = next_word(&rest);
    char *vlan_s = next_word(&rest);
    char *prio_s = next_word(&rest);
    mac_t dst;
    long vlan, prio;

    if (parse_dst(dst_s, dst) < 0 || parse_long(vlan_s, 1, 4094, &vlan) < 0 ||
        parse_long(prio_s, 0, 2, &prio) < 0) {
        fprintf(stderr, "host: usage: sendx <dst_mac|bcast> <vlan 1-4094> "
                        "<prio 0-2> <text>\n");
        return;
    }
    const char *text = rest != NULL ? rest : "";
    if (check_text(text) < 0)
        return;
    send_or_die(dst, (uint16_t)vlan, (uint8_t)prio, text);
}

static void command_loop(void)
{
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;

    while ((n = getline(&line, &cap, stdin)) > 0) {
        while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r'))
            line[--n] = '\0';

        char *rest = line;
        char *cmd = next_word(&rest);

        if (cmd == NULL)
            continue;                           /* blank line */
        if (strcmp(cmd, "quit") == 0)
            break;
        else if (strcmp(cmd, "send") == 0)
            cmd_send(rest);
        else if (strcmp(cmd, "sendx") == 0)
            cmd_sendx(rest);
        else
            fprintf(stderr, "host: unknown command '%s'\n", cmd);
    }
    free(line);                                 /* quit or end of stdin */
}

/* ------------------------------------------------------------------ */
/* Start-up                                                            */
/* ------------------------------------------------------------------ */

static void parse_args(int argc, char **argv, struct in_addr *ip, long *port)
{
    bool have_to = false;

    if (argc < 4) {
        usage(argv[0]);
        exit(1);
    }
    if (inet_pton(AF_INET, argv[1], ip) != 1) {
        fprintf(stderr, "host: bad IP address '%s'\n", argv[1]);
        exit(1);
    }
    if (parse_long(argv[2], 1, 65535, port) < 0) {
        fprintf(stderr, "host: bad port '%s' (1-65535)\n", argv[2]);
        exit(1);
    }
    if (mac_parse(argv[3], opt.me) < 0) {
        fprintf(stderr, "host: bad MAC '%s'\n", argv[3]);
        exit(1);
    }

    for (int i = 4; i < argc; i++) {
        if (strcmp(argv[i], "--noread") == 0) {
            opt.noread = true;
        } else if (strcmp(argv[i], "--partial") == 0) {
            opt.partial = true;
        } else if (strcmp(argv[i], "--quiet") == 0) {
            opt.quiet = true;
        } else if (strcmp(argv[i], "--die-midframe") == 0) {
            opt.die_midframe = true;
        } else if (strcmp(argv[i], "--flood") == 0) {
            if (i + 1 >= argc || parse_long(argv[i + 1], 0, 1000000000L, &opt.flood_count) < 0) {
                fprintf(stderr, "host: --flood needs a number N >= 0\n");
                exit(1);
            }
            opt.flood = true;
            i++;
        } else if (strcmp(argv[i], "--to") == 0) {
            if (i + 1 >= argc || parse_dst(argv[i + 1], opt.flood_to) < 0) {
                fprintf(stderr, "host: --to needs a MAC address (or bcast)\n");
                exit(1);
            }
            have_to = true;
            i++;
        } else {
            fprintf(stderr, "host: unknown option '%s'\n", argv[i]);
            usage(argv[0]);
            exit(1);
        }
    }
    if (opt.flood != have_to) {
        fprintf(stderr, "host: --flood and --to must be used together\n");
        exit(1);
    }
}

int main(int argc, char **argv)
{
    struct in_addr ip;
    long port;
    struct sigaction sa;

    /* A write to a dead peer must give EPIPE, not kill the process. */
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = SIG_IGN;
    CHECK(sigaction(SIGPIPE, &sa, NULL));

    parse_args(argc, argv, &ip, &port);

    CHECK(sock = socket(AF_INET, SOCK_STREAM, 0));
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)port);
    addr.sin_addr = ip;
    if (connect(sock, (struct sockaddr *)&addr, sizeof addr) < 0) {
        fprintf(stderr, "host: connect to %s:%ld failed: %s\n", argv[1], port,
                strerror(errno));
        return 1;
    }

    if (opt.die_midframe)
        die_midframe();

    if (!opt.noread) {
        pthread_t rx;
        CHECK_PT(pthread_create(&rx, NULL, receiver_main, NULL));
        CHECK_PT(pthread_detach(rx));
    }

    if (opt.flood) {
        char text[32];
        for (long i = 0; i < opt.flood_count; i++) {
            snprintf(text, sizeof text, "flood %ld", i);
            send_or_die(opt.flood_to, DEFAULT_VLAN, 1, text);
        }
    }

    command_loop();         /* returns on quit or on end of stdin */
    host_finish(0);
    return 0;               /* not reached */
}
