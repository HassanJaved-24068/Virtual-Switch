/*
 * bench.c - the network load generator of the benchmark (Part 21).
 *
 *   ./bench/bench <ip> <tcp_port> <hosts> <seconds> <payload> [seed]
 *
 * It opens H connections to a running switch (the switch needs at least H ports). Each connection is a host with a unique MAC, a SENDER thread and a READER thread:
 *   1. warm-up: every host broadcasts, so that the switch learns every MAC (the frames of the measurement are then unicast, not flooded). The switch accepts the
 *      connections one at a time, so a broadcast sent early can miss a host that has no port yet: the broadcasts are REPEATED until every host has received a broadcast
 *      of every other host. Only then does the measurement start; broadcasts are never counted;
 *   2. measurement: for <seconds> every sender sends unicast frames of <payload> bytes to a random OTHER host (as fast as the socket takes them: no pacing), every reader counts
 *      the frames it receives;
 *   3. the result is the number of frames RECEIVED by all hosts during the window, per second.
 * The random choices come from xorshift64 generators with fixed seeds (the optional seed, default 12345, plus the host number): the same arguments give the same sequence of
 * destinations. With one host nothing can be delivered (there is no peer): use at least 2.
 *
 * Output, one line:  bench hosts=H seconds=S payload=P sent=N received=M frames_per_sec=F
 */
#include "common.h"
#include "frame.h"

#include <arpa/inet.h>
#include <ctype.h>
#include <limits.h>
#include <math.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <sys/socket.h>
#include <unistd.h>

#include <stdatomic.h>

#define MAX_HOSTS 64
#define WARMUP_TIMEOUT_SEC 20

typedef struct {
    int idx;
    int fd;
    mac_t mac;
    pthread_t sender, reader;
    uint64_t rng;
    _Atomic unsigned long long sent;
    _Atomic unsigned long long received;
} host_t;

static host_t hosts[MAX_HOSTS];
static int nhosts, payload_len;
static pthread_barrier_t go_barrier;             /* the measurement starts: the senders and main pass it together */
static _Atomic bool stop_all;
static _Atomic bool heard[MAX_HOSTS][MAX_HOSTS]; /* heard[r][s]: host r has received a broadcast of host s (warm-up) */

static uint64_t xorshift(uint64_t *s)
{
    uint64_t x = *s;

    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    *s = x;
    return x;
}

static void die(const char *what)
{
    fprintf(stderr, "bench: %s: %s\n", what, strerror(errno));
    exit(EXIT_FAILURE);
}

static void *reader_main(void *arg)
{
    host_t *h = arg;
    uint8_t buf[HDR_LEN + MAX_PAYLOAD];

    for (;;) {
        ssize_t r = read_full(h->fd, buf, HDR_LEN);
        if (r != HDR_LEN)
            return NULL;                          /* EOF or error: the run is over */
        frame_hdr_t hdr;
        frame_unpack(buf, &hdr);
        if (hdr.len > MAX_PAYLOAD)
            return NULL;
        if (hdr.len > 0 && read_full(h->fd, buf + HDR_LEN, hdr.len) != (ssize_t)hdr.len)
            return NULL;
        if (mac_is_broadcast(hdr.dst)) {              /* a warm-up broadcast: remember who it came from, never count it */
            const int from = hdr.src[4] << 8 | hdr.src[5];
            if (from < nhosts)
                atomic_store(&heard[h->idx][from], true);
            continue;
        }
        atomic_fetch_add(&h->received, 1);
    }
}

static void *sender_main(void *arg)
{
    host_t *h = arg;
    uint8_t wire[HDR_LEN + MAX_PAYLOAD];
    frame_hdr_t hdr;

    memcpy(hdr.src, h->mac, MAC_LEN);
    memset(hdr.dst, 0xFF, MAC_LEN);
    hdr.vlan = 1;
    hdr.prio = 1;
    hdr.len = (uint16_t)payload_len;
    uint8_t payload[MAX_PAYLOAD];
    memset(payload, 'x', sizeof payload);
    if (frame_pack(&hdr, payload, wire) < 0)
        die("frame_pack");

    int rc = pthread_barrier_wait(&go_barrier);                          /* the warm-up is main's job; the senders wait for the start */
    if (rc != 0 && rc != PTHREAD_BARRIER_SERIAL_THREAD)
        return NULL;

    while (!atomic_load(&stop_all)) {
        int j = (int)(xorshift(&h->rng) % (uint64_t)(nhosts - 1));
        if (j >= h->idx)
            j++;                                  /* any host but myself */
        memcpy(wire + 8, hosts[j].mac, MAC_LEN);  /* offset 8: the destination MAC in the 17-byte header */
        if (write_full(h->fd, wire, HDR_LEN + (size_t)payload_len) < 0)
            return NULL;
        atomic_fetch_add(&h->sent, 1);
    }
    return NULL;
}

static unsigned long long sum_received(void)
{
    unsigned long long n = 0;

    for (int i = 0; i < nhosts; i++)
        n += atomic_load(&hosts[i].received);
    return n;
}

static unsigned long long sum_sent(void)
{
    unsigned long long n = 0;

    for (int i = 0; i < nhosts; i++)
        n += atomic_load(&hosts[i].sent);
    return n;
}

int main(int argc, char **argv)
{
    if (argc != 6 && argc != 7) {
        fprintf(stderr, "usage: %s <ip> <tcp_port> <hosts 2-%d> <seconds> <payload 0-%d> [seed]\n", argv[0], MAX_HOSTS, MAX_PAYLOAD);
        return EXIT_FAILURE;
    }
    char *end;
    long port, h, pl;
    if (parse_uint(argv[2], 1, 65535, &port) < 0)
        return fprintf(stderr, "bench: bad port\n"), EXIT_FAILURE;
    if (parse_uint(argv[3], 2, MAX_HOSTS, &h) < 0)
        return fprintf(stderr, "bench: hosts must be 2-%d\n", MAX_HOSTS), EXIT_FAILURE;
    errno = 0;
    double seconds = isdigit((unsigned char)argv[4][0]) ? strtod(argv[4], &end) : 0;    /* a digit first: no sign, no white space, no "nan" / "inf" */
    if (!isdigit((unsigned char)argv[4][0]) || *end != '\0' || errno != 0 || !isfinite(seconds) || seconds <= 0 || seconds > 86400)
        return fprintf(stderr, "bench: bad seconds\n"), EXIT_FAILURE;
    if (parse_uint(argv[5], 0, MAX_PAYLOAD, &pl) < 0)
        return fprintf(stderr, "bench: payload must be 0-%d\n", MAX_PAYLOAD), EXIT_FAILURE;
    unsigned long long seed = 12345;
    if (argc == 7) {
        long sd;

        if (parse_uint(argv[6], 0, LONG_MAX, &sd) < 0)
            return fprintf(stderr, "bench: bad seed\n"), EXIT_FAILURE;
        seed = (unsigned long long)sd;
    }
    nhosts = (int)h;
    payload_len = (int)pl;
    struct sigaction ign;                                                /* a write to a closed connection (at the end of the run) must fail with EPIPE, not kill us */
    memset(&ign, 0, sizeof ign);
    ign.sa_handler = SIG_IGN;
    if (sigemptyset(&ign.sa_mask) < 0 || sigaction(SIGPIPE, &ign, NULL) < 0)
        die("sigaction");

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, argv[1], &addr.sin_addr) != 1)
        return fprintf(stderr, "bench: bad IP address '%s'\n", argv[1]), EXIT_FAILURE;

    for (int i = 0; i < nhosts; i++) {                                   /* connect one after the other: host i gets port i + 1 */
        host_t *ho = &hosts[i];
        int one = 1;

        ho->idx = i;
        ho->rng = (seed + 1) * 0x9E3779B97F4A7C15ull + (uint64_t)i * 0xD1B54A32D192ED03ull;
        if (ho->rng == 0)
            ho->rng = 1;
        const uint8_t m[MAC_LEN] = {0x02, 0xbe, 0x00, 0x00, (uint8_t)(i >> 8), (uint8_t)(i & 0xff)};
        memcpy(ho->mac, m, MAC_LEN);
        ho->fd = socket(AF_INET, SOCK_STREAM, 0);
        if (ho->fd < 0)
            die("socket");
        if (setsockopt(ho->fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one) < 0)
            die("setsockopt TCP_NODELAY");
        if (connect(ho->fd, (struct sockaddr *)&addr, sizeof addr) < 0)
            die("connect");
    }
    CHECK_PT(pthread_barrier_init(&go_barrier, NULL, (unsigned)nhosts + 1));
    for (int i = 0; i < nhosts; i++) {
        CHECK_PT(pthread_create(&hosts[i].reader, NULL, reader_main, &hosts[i]));
        CHECK_PT(pthread_create(&hosts[i].sender, NULL, sender_main, &hosts[i]));
    }

    /* Warm-up (before the senders are released, so main is the only writer): repeat a broadcast of every host that is not yet known to all the others, until every host has
     * received a broadcast of every other host. By then the switch has learned every MAC. */
    uint8_t bwire[HDR_LEN + MAX_PAYLOAD];
    struct timespec t0 = now_mono();
    for (;;) {
        bool all = true;

        for (int s_ = 0; s_ < nhosts; s_++) {
            bool known = true;

            for (int r_ = 0; r_ < nhosts; r_++)
                if (r_ != s_ && !atomic_load(&heard[r_][s_]))
                    known = false;
            if (known)
                continue;
            all = false;
            frame_hdr_t bh;
            memcpy(bh.src, hosts[s_].mac, MAC_LEN);
            memset(bh.dst, 0xFF, MAC_LEN);
            bh.vlan = 1;
            bh.prio = 1;
            bh.len = 1;
            const uint8_t one_byte = 'w';
            if (frame_pack(&bh, &one_byte, bwire) < 0 || write_full(hosts[s_].fd, bwire, HDR_LEN + 1) < 0)
                die("warm-up send");
        }
        if (all)
            break;
        if (ts_diff_ms(now_mono(), t0) > WARMUP_TIMEOUT_SEC * 1000L) {
            fprintf(stderr, "bench: warm-up timeout: some hosts never heard some others (frames dropped by the switch?)\n");
            break;                                                      /* measure anyway */
        }
        struct timespec nap = {0, 20000000};
        nanosleep(&nap, NULL);
    }
    int rc;

    const unsigned long long rx0 = sum_received(), tx0 = sum_sent();
    t0 = now_mono();
    rc = pthread_barrier_wait(&go_barrier);                             /* go: the senders start now */
    if (rc != 0 && rc != PTHREAD_BARRIER_SERIAL_THREAD)
        die("barrier");
    struct timespec deadline = ts_add_ms(t0, (long)(seconds * 1000.0));
    while (ts_diff_ms(deadline, now_mono()) > 0) {
        struct timespec nap = {0, 2000000};
        nanosleep(&nap, NULL);
    }
    const unsigned long long rx1 = sum_received(), tx1 = sum_sent();
    const struct timespec t1 = now_mono();
    atomic_store(&stop_all, true);

    const double elapsed = (double)ts_diff_ms(t1, t0) / 1000.0;
    printf("bench hosts=%d seconds=%.2f payload=%d sent=%llu received=%llu frames_per_sec=%.0f\n", nhosts, elapsed, payload_len, tx1 - tx0, rx1 - rx0,
           elapsed > 0 ? (double)(rx1 - rx0) / elapsed : 0.0);
    fflush(stdout);

    for (int i = 0; i < nhosts; i++)                                    /* the end of the run: shutdown wakes every blocked reader / sender */
        if (shutdown(hosts[i].fd, SHUT_RDWR) < 0 && errno != ENOTCONN)
            fprintf(stderr, "bench: shutdown: %s\n", strerror(errno));
    for (int i = 0; i < nhosts; i++) {
        CHECK_PT(pthread_join(hosts[i].sender, NULL));
        CHECK_PT(pthread_join(hosts[i].reader, NULL));
        if (close(hosts[i].fd) < 0)
            fprintf(stderr, "bench: close: %s\n", strerror(errno));
    }
    return EXIT_SUCCESS;
}
