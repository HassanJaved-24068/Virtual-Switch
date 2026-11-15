/*
 * test_frame.c - unit tests for frame.c (Part 2).
 * Raw wire bytes in the tests are built by hand (not with frame_pack) so
 * that the tests do not just check the code against itself.
 */
#include "frame.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>

static int checks = 0, failures = 0;

#define EXPECT(cond)                                                       \
    do {                                                                   \
        checks++;                                                          \
        if (!(cond)) {                                                     \
            failures++;                                                    \
            printf("  FAIL line %d: %s\n", __LINE__, #cond);               \
        }                                                                  \
    } while (0)

/* ---- helpers ---- */

static void make_pair(int fds[2])
{
    CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, fds));
}

/* Hand-made header bytes: len (big endian), src, dst, vlan (big endian), prio. */
static void raw_hdr(uint8_t out[HDR_LEN], unsigned len, const mac_t src,
                    const mac_t dst, unsigned vlan, unsigned prio)
{
    out[0] = (uint8_t)(len >> 8);
    out[1] = (uint8_t)(len & 0xFF);
    memcpy(out + 2, src, 6);
    memcpy(out + 8, dst, 6);
    out[14] = (uint8_t)(vlan >> 8);
    out[15] = (uint8_t)(vlan & 0xFF);
    out[16] = (uint8_t)prio;
}

static const mac_t SRC = {0x02, 0, 0, 0, 0, 0x0a};
static const mac_t DST = {0x02, 0, 0, 0, 0, 0x0b};

struct writer_args {
    int fd;
    const uint8_t *data;
    size_t len;
    size_t chunk;           /* bytes per write() call */
    int close_after;
};

/* Thread: writes data in small chunks, with a tiny pause between them. */
static void *chunk_writer(void *p)
{
    struct writer_args *a = p;
    for (size_t off = 0; off < a->len; off += a->chunk) {
        size_t n = a->len - off < a->chunk ? a->len - off : a->chunk;
        if (write_full(a->fd, a->data + off, n) < 0)
            break;
        struct timespec ts = {0, 200000L};          /* 0.2 ms: let the reader run */
        nanosleep(&ts, NULL);
    }
    if (a->close_after)
        close(a->fd);
    return NULL;
}

static void write_all_close(int fd, const void *buf, size_t n)
{
    CHECK(write_full(fd, buf, n));
    CHECK(close(fd));
}

/* ---- pack / unpack ---- */

static void test_pack_unpack(void)
{
    printf("frame_pack / frame_unpack\n");

    /* exact layout, checked byte by byte against the spec */
    frame_hdr_t h = {.len = 5, .vlan = 0x0123, .prio = 2};
    memcpy(h.src, SRC, 6);
    memcpy(h.dst, DST, 6);
    uint8_t out[HDR_LEN + 8];
    memset(out, 0xEE, sizeof out);
    int n = frame_pack(&h, (const uint8_t *)"hello", out);
    EXPECT(n == HDR_LEN + 5);
    EXPECT(out[0] == 0x00 && out[1] == 0x05);                 /* offset 0: length */
    EXPECT(memcmp(out + 2, SRC, 6) == 0);                     /* offset 2: src */
    EXPECT(memcmp(out + 8, DST, 6) == 0);                     /* offset 8: dst */
    EXPECT(out[14] == 0x01 && out[15] == 0x23);               /* offset 14: vlan */
    EXPECT(out[16] == 2);                                     /* offset 16: prio */
    EXPECT(memcmp(out + 17, "hello", 5) == 0);                /* offset 17: payload */
    EXPECT(out[22] == 0xEE && out[23] == 0xEE);               /* nothing written past the end */

    /* header only: exactly 17 bytes */
    h.len = 0;
    memset(out, 0xEE, sizeof out);
    EXPECT(frame_pack(&h, NULL, out) == 17);
    EXPECT(HDR_LEN == 17);
    EXPECT(out[17] == 0xEE);

    /* byte order: 0x0102 must be 01 02 on the wire (and 0x0201 -> 02 01) */
    uint8_t payload[MAX_PAYLOAD];
    memset(payload, 'p', sizeof payload);
    h.len = 0x0102;
    h.vlan = 0x0A0B;
    uint8_t big[HDR_LEN + MAX_PAYLOAD];
    EXPECT(frame_pack(&h, payload, big) == HDR_LEN + 0x0102);
    EXPECT(big[0] == 0x01 && big[1] == 0x02);
    EXPECT(big[14] == 0x0A && big[15] == 0x0B);

    /* unpack of hand-made bytes */
    uint8_t raw[HDR_LEN];
    raw_hdr(raw, 0x0102, SRC, DST, 4094, 1);
    frame_hdr_t u;
    frame_unpack(raw, &u);
    EXPECT(u.len == 0x0102 && u.vlan == 4094 && u.prio == 1);
    EXPECT(memcmp(u.src, SRC, 6) == 0 && memcmp(u.dst, DST, 6) == 0);

    /* round trip over many field values, including the limits */
    const unsigned lens[] = {0, 1, 2, 255, 256, 258, 1499, 1500};
    const unsigned vlans[] = {1, 2, 255, 256, 1000, 4093, 4094};
    int ok = 1;
    for (size_t i = 0; i < sizeof lens / sizeof lens[0]; i++)
        for (size_t j = 0; j < sizeof vlans / sizeof vlans[0]; j++)
            for (unsigned prio = 0; prio <= 2; prio++) {
                frame_hdr_t a = {.len = (uint16_t)lens[i], .vlan = (uint16_t)vlans[j],
                                 .prio = (uint8_t)prio};
                memcpy(a.src, SRC, 6);
                memset(a.dst, 0xFF, 6);
                a.src[5] = (uint8_t)(lens[i] + j);
                for (unsigned k = 0; k < lens[i]; k++)
                    payload[k] = (uint8_t)(k * 7 + i);
                uint8_t buf[HDR_LEN + MAX_PAYLOAD];
                frame_hdr_t b;
                if (frame_pack(&a, payload, buf) != (int)(HDR_LEN + lens[i])) { ok = 0; continue; }
                frame_unpack(buf, &b);
                if (b.len != a.len || b.vlan != a.vlan || b.prio != a.prio ||
                    memcmp(b.src, a.src, 6) || memcmp(b.dst, a.dst, 6) ||
                    memcmp(buf + HDR_LEN, payload, lens[i]))
                    ok = 0;
            }
    EXPECT(ok);

    /* invalid input to pack */
    h.len = 1501;
    errno = 0;
    EXPECT(frame_pack(&h, payload, big) == -1 && errno == EMSGSIZE);
    h.len = 0xFFFF;
    EXPECT(frame_pack(&h, payload, big) == -1);
    h.len = 4;
    errno = 0;
    EXPECT(frame_pack(&h, NULL, big) == -1 && errno == EINVAL);
}

/* ---- read_full ---- */

static void test_read_full_basic(void)
{
    printf("read_full: basic cases\n");
    int fds[2];
    uint8_t buf[64];

    make_pair(fds);
    CHECK(write_full(fds[1], "abcdef", 6));
    EXPECT(read_full(fds[0], buf, 6) == 6 && memcmp(buf, "abcdef", 6) == 0);
    EXPECT(read_full(fds[0], buf, 0) == 0);                    /* n == 0: nothing to do */

    /* EOF in the middle: returns how many bytes it did get */
    CHECK(write_full(fds[1], "12345", 5));
    CHECK(close(fds[1]));
    EXPECT(read_full(fds[0], buf, 10) == 5 && memcmp(buf, "12345", 5) == 0);
    /* EOF at the start: 0 */
    EXPECT(read_full(fds[0], buf, 10) == 0);
    EXPECT(read_full(fds[0], buf, 10) == 0);                   /* stays at EOF */
    close(fds[0]);

    /* error: errno is kept */
    errno = 0;
    EXPECT(read_full(-1, buf, 4) == -1 && errno == EBADF);
}

static void test_read_full_chunks(void)
{
    printf("read_full: 1-byte chunks, and 1.5 frames in one write\n");
    int fds[2];
    uint8_t f1[HDR_LEN + 10], f2[HDR_LEN + 20];
    raw_hdr(f1, 10, SRC, DST, 1, 1);
    memcpy(f1 + HDR_LEN, "0123456789", 10);
    raw_hdr(f2, 20, DST, SRC, 7, 0);
    memcpy(f2 + HDR_LEN, "abcdefghijklmnopqrst", 20);

    /* (a) one frame, written one byte at a time */
    make_pair(fds);
    struct writer_args wa = {fds[1], f1, sizeof f1, 1, 0};
    pthread_t th;
    CHECK_PT(pthread_create(&th, NULL, chunk_writer, &wa));
    uint8_t got[sizeof f1];
    EXPECT(read_full(fds[0], got, sizeof got) == (ssize_t)sizeof got);
    EXPECT(memcmp(got, f1, sizeof f1) == 0);
    CHECK_PT(pthread_join(th, NULL));
    close(fds[0]);
    close(fds[1]);

    /* (b) 1.5 frames in one write, the rest later: reader rebuilds both */
    uint8_t stream[sizeof f1 + sizeof f2];
    memcpy(stream, f1, sizeof f1);
    memcpy(stream + sizeof f1, f2, sizeof f2);
    size_t first = sizeof f1 + sizeof f2 / 2;        /* frame 1 + half of frame 2 */
    make_pair(fds);
    CHECK(write_full(fds[1], stream, first));
    uint8_t r1[sizeof f1], r2[sizeof f2];
    EXPECT(read_full(fds[0], r1, sizeof r1) == (ssize_t)sizeof r1);
    EXPECT(memcmp(r1, f1, sizeof f1) == 0);
    struct writer_args wb = {fds[1], stream + first, sizeof stream - first, 5, 0};
    CHECK_PT(pthread_create(&th, NULL, chunk_writer, &wb));
    EXPECT(read_full(fds[0], r2, sizeof r2) == (ssize_t)sizeof r2);
    EXPECT(memcmp(r2, f2, sizeof f2) == 0);
    CHECK_PT(pthread_join(th, NULL));
    close(fds[0]);
    close(fds[1]);
}

/* ---- EINTR handling ---- */

static volatile sig_atomic_t alarms;
static void on_alarm(int sig) { (void)sig; alarms++; }

static void start_timer(long ms)
{
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_alarm;               /* no SA_RESTART: read/write get EINTR */
    CHECK(sigaction(SIGALRM, &sa, NULL));
    struct itimerval it = {{0, ms * 1000}, {0, ms * 1000}};
    CHECK(setitimer(ITIMER_REAL, &it, NULL));
}

static void stop_timer(void)
{
    struct itimerval it = {{0, 0}, {0, 0}};
    CHECK(setitimer(ITIMER_REAL, &it, NULL));
}

static void test_eintr(void)
{
    printf("read_full / write_full retry on EINTR\n");
    int fds[2];
    uint8_t buf[16];

    /* read_full: signals arrive every 20 ms while it waits; the data comes at 200 ms */
    make_pair(fds);
    pid_t pid = fork();
    CHECK(pid);
    if (pid == 0) {
        close(fds[0]);
        usleep(200000);
        write_full(fds[1], "0123456789abcdef", 16);
        _exit(0);
    }
    close(fds[1]);
    alarms = 0;
    start_timer(20);
    ssize_t r = read_full(fds[0], buf, 16);
    stop_timer();
    EXPECT(r == 16 && memcmp(buf, "0123456789abcdef", 16) == 0);
    EXPECT(alarms >= 2);                      /* the read really was interrupted */
    close(fds[0]);
    waitpid(pid, NULL, 0);

    /* write_full: 4 MB does not fit in the socket buffer, so write() blocks,
     * gets interrupted, and may write only part. The reader starts late. */
    const size_t total = 4u << 20;
    uint8_t *data = malloc(total);
    CHECK(data == NULL ? -1 : 0);
    for (size_t i = 0; i < total; i++)
        data[i] = (uint8_t)(i * 31 + (i >> 8));
    make_pair(fds);
    pid = fork();
    CHECK(pid);
    if (pid == 0) {
        close(fds[1]);
        usleep(300000);
        uint8_t *in = malloc(total);
        if (in == NULL || read_full(fds[0], in, total) != (ssize_t)total ||
            memcmp(in, data, total) != 0)
            _exit(1);
        _exit(0);
    }
    close(fds[0]);
    alarms = 0;
    start_timer(20);
    int wr = write_full(fds[1], data, total);
    stop_timer();
    EXPECT(wr == 0);
    EXPECT(alarms >= 2);
    close(fds[1]);
    int st = 0;
    waitpid(pid, &st, 0);
    EXPECT(WIFEXITED(st) && WEXITSTATUS(st) == 0);        /* all bytes arrived intact */
    free(data);
}

/* ---- write_full errors ---- */

/* Loopback TCP connection; the server side closes with a reset (RST). */
static int tcp_pair(int *client, int *server)
{
    int ls = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in a;
    socklen_t al = sizeof a;
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (ls < 0 || bind(ls, (struct sockaddr *)&a, sizeof a) < 0 ||
        listen(ls, 1) < 0 || getsockname(ls, (struct sockaddr *)&a, &al) < 0)
        return -1;
    *client = socket(AF_INET, SOCK_STREAM, 0);
    if (*client < 0 || connect(*client, (struct sockaddr *)&a, sizeof a) < 0)
        return -1;
    *server = accept(ls, NULL, NULL);
    close(ls);
    return *server < 0 ? -1 : 0;
}

static void reset_peer(int server_fd)
{
    struct linger lg = {1, 0};               /* close() sends RST, not FIN */
    CHECK(setsockopt(server_fd, SOL_SOCKET, SO_LINGER, &lg, sizeof lg));
    CHECK(close(server_fd));
}

static void test_write_errors(void)
{
    printf("write_full: errors\n");
    int fds[2];

    EXPECT(write_full(-1, "x", 1) == -1 && errno == EBADF);

    make_pair(fds);
    EXPECT(write_full(fds[1], "x", 0) == 0);              /* nothing to write */
    close(fds[0]);                                        /* peer gone */
    errno = 0;
    EXPECT(write_full(fds[1], "hello", 5) == -1 && errno == EPIPE);   /* SIGPIPE is ignored */
    close(fds[1]);

    int c, s;
    CHECK(tcp_pair(&c, &s));
    reset_peer(s);
    usleep(50000);
    errno = 0;
    int rc = write_full(c, "hello", 5);
    EXPECT(rc == -1 && (errno == ECONNRESET || errno == EPIPE));
    errno = 0;
    rc = write_full(c, "hello", 5);                       /* second try: broken pipe */
    EXPECT(rc == -1 && errno == EPIPE);
    close(c);
}

/* ---- frame_recv ---- */

static void test_recv_ok(void)
{
    printf("frame_recv: good frames\n");
    int fds[2];
    uint8_t buf[HDR_LEN + MAX_PAYLOAD];
    uint8_t pay[MAX_PAYLOAD];
    frame_hdr_t h;

    /* normal frame */
    make_pair(fds);
    raw_hdr(buf, 5, SRC, DST, 10, 2);
    memcpy(buf + HDR_LEN, "hello", 5);
    CHECK(write_full(fds[1], buf, HDR_LEN + 5));
    memset(pay, 0, sizeof pay);
    EXPECT(frame_recv(fds[0], &h, pay) == FR_OK);
    EXPECT(h.len == 5 && h.vlan == 10 && h.prio == 2);
    EXPECT(memcmp(h.src, SRC, 6) == 0 && memcmp(h.dst, DST, 6) == 0);
    EXPECT(memcmp(pay, "hello", 5) == 0);

    /* zero-length payload is valid, and is not mistaken for EOF */
    raw_hdr(buf, 0, DST, SRC, 4094, 0);
    CHECK(write_full(fds[1], buf, HDR_LEN));
    EXPECT(frame_recv(fds[0], &h, pay) == FR_OK);
    EXPECT(h.len == 0 && h.vlan == 4094 && h.prio == 0);

    /* largest payload, 1500 */
    raw_hdr(buf, 1500, SRC, DST, 1, 1);
    for (int i = 0; i < 1500; i++)
        buf[HDR_LEN + i] = (uint8_t)i;
    CHECK(write_full(fds[1], buf, HDR_LEN + 1500));
    EXPECT(frame_recv(fds[0], &h, pay) == FR_OK && h.len == 1500);
    EXPECT(memcmp(pay, buf + HDR_LEN, 1500) == 0);

    /* byte order: the wire bytes 01 02 mean length 258 */
    raw_hdr(buf, 0x0102, SRC, DST, 1, 1);
    memset(buf + HDR_LEN, 'z', 0x0102);
    CHECK(write_full(fds[1], buf, HDR_LEN + 0x0102));
    EXPECT(frame_recv(fds[0], &h, pay) == FR_OK && h.len == 258);

    /* three frames in ONE write: three separate frame_recv calls */
    uint8_t three[3 * (HDR_LEN + 4)];
    for (int i = 0; i < 3; i++) {
        raw_hdr(three + i * (HDR_LEN + 4), 4, SRC, DST, (unsigned)(i + 1), 1);
        memcpy(three + i * (HDR_LEN + 4) + HDR_LEN, "ab_", 3);
        three[i * (HDR_LEN + 4) + HDR_LEN + 3] = (uint8_t)('0' + i);
    }
    CHECK(write_full(fds[1], three, sizeof three));
    for (int i = 0; i < 3; i++) {
        EXPECT(frame_recv(fds[0], &h, pay) == FR_OK);
        EXPECT(h.vlan == (unsigned)(i + 1) && h.len == 4 && pay[3] == '0' + i);
    }
    close(fds[0]);
    close(fds[1]);

    /* a frame written one byte at a time */
    make_pair(fds);
    raw_hdr(buf, 12, SRC, DST, 33, 1);
    memcpy(buf + HDR_LEN, "slow-writer!", 12);
    struct writer_args wa = {fds[1], buf, HDR_LEN + 12, 1, 0};
    pthread_t th;
    CHECK_PT(pthread_create(&th, NULL, chunk_writer, &wa));
    EXPECT(frame_recv(fds[0], &h, pay) == FR_OK && h.len == 12 && h.vlan == 33);
    EXPECT(memcmp(pay, "slow-writer!", 12) == 0);
    CHECK_PT(pthread_join(th, NULL));
    close(fds[0]);
    close(fds[1]);
}

static void test_recv_failures(void)
{
    printf("frame_recv: EOF, truncation, protocol and I/O errors\n");
    int fds[2];
    uint8_t buf[HDR_LEN + 64];
    uint8_t pay[MAX_PAYLOAD];
    frame_hdr_t h;

    /* clean EOF before any byte */
    make_pair(fds);
    close(fds[1]);
    EXPECT(frame_recv(fds[0], &h, pay) == FR_EOF);
    EXPECT(frame_recv(fds[0], &h, pay) == FR_EOF);
    close(fds[0]);

    /* clean EOF right after a complete frame */
    make_pair(fds);
    raw_hdr(buf, 3, SRC, DST, 1, 1);
    memcpy(buf + HDR_LEN, "abc", 3);
    write_all_close(fds[1], buf, HDR_LEN + 3);
    EXPECT(frame_recv(fds[0], &h, pay) == FR_OK);
    EXPECT(frame_recv(fds[0], &h, pay) == FR_EOF);
    close(fds[0]);

    /* truncated header: 1, 2, 9 and 16 bytes */
    const size_t cuts[] = {1, 2, 9, 16};
    for (size_t i = 0; i < sizeof cuts / sizeof cuts[0]; i++) {
        make_pair(fds);
        raw_hdr(buf, 4, SRC, DST, 1, 1);
        write_all_close(fds[1], buf, cuts[i]);
        EXPECT(frame_recv(fds[0], &h, pay) == FR_TRUNC_HDR);
        close(fds[0]);
    }

    /* truncated payload: header says 10, only 0, 1 and 9 bytes follow */
    const size_t have[] = {0, 1, 9};
    for (size_t i = 0; i < sizeof have / sizeof have[0]; i++) {
        make_pair(fds);
        raw_hdr(buf, 10, SRC, DST, 1, 1);
        memset(buf + HDR_LEN, 'q', 10);
        write_all_close(fds[1], buf, HDR_LEN + have[i]);
        EXPECT(frame_recv(fds[0], &h, pay) == FR_TRUNC_PAYLOAD);
        close(fds[0]);
    }

    /* protocol violation: length 1501, 2000, 0xFFFF. Payload is not read. */
    const unsigned bad[] = {1501, 2000, 0xFFFF};
    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        make_pair(fds);
        raw_hdr(buf, bad[i], SRC, DST, 1, 1);
        CHECK(write_full(fds[1], buf, HDR_LEN));
        EXPECT(frame_recv(fds[0], &h, pay) == FR_PROTO);
        EXPECT(h.len == bad[i]);
        close(fds[0]);
        close(fds[1]);
    }
    /* 1500 is still fine (boundary) - checked in test_recv_ok; here: 1501 + data */
    make_pair(fds);
    uint8_t oversize[HDR_LEN + 1501];
    raw_hdr(oversize, 1501, SRC, DST, 1, 1);
    memset(oversize + HDR_LEN, 'x', 1501);
    CHECK(write_full(fds[1], oversize, sizeof oversize));
    EXPECT(frame_recv(fds[0], &h, pay) == FR_PROTO);
    close(fds[0]);
    close(fds[1]);

    /* I/O error: bad descriptor, errno kept */
    errno = 0;
    EXPECT(frame_recv(-1, &h, pay) == FR_ERR && errno == EBADF);

    /* I/O error: connection reset by the peer */
    int c, s;
    CHECK(tcp_pair(&c, &s));
    reset_peer(s);
    usleep(50000);
    errno = 0;
    EXPECT(frame_recv(c, &h, pay) == FR_ERR && errno == ECONNRESET);
    close(c);

    /* return values are the documented numbers */
    EXPECT(FR_OK == 1 && FR_EOF == 0 && FR_ERR == -1 && FR_TRUNC_HDR == -2 &&
           FR_TRUNC_PAYLOAD == -3 && FR_PROTO == -4);
    EXPECT(strlen(frame_rc_str(FR_OK)) > 0 && strlen(frame_rc_str(FR_PROTO)) > 0);
}

/* ---- frame_send ---- */

static void test_send(void)
{
    printf("frame_send\n");
    int fds[2];
    uint8_t expect[HDR_LEN + 5], got[HDR_LEN + 5];
    uint8_t pay[MAX_PAYLOAD];
    frame_hdr_t h = {.len = 5, .vlan = 77, .prio = 1};
    frame_hdr_t r;
    memcpy(h.src, SRC, 6);
    memcpy(h.dst, DST, 6);

    make_pair(fds);
    EXPECT(frame_send(fds[0], &h, (const uint8_t *)"hello") == 0);
    raw_hdr(expect, 5, SRC, DST, 77, 1);
    memcpy(expect + HDR_LEN, "hello", 5);
    EXPECT(read_full(fds[1], got, sizeof got) == (ssize_t)sizeof got);
    EXPECT(memcmp(got, expect, sizeof got) == 0);

    /* send then recv across the pair, incl. zero length and max length */
    uint8_t big[MAX_PAYLOAD];
    for (int i = 0; i < MAX_PAYLOAD; i++)
        big[i] = (uint8_t)(i ^ 0x5a);
    h.len = 1500;
    EXPECT(frame_send(fds[0], &h, big) == 0);
    EXPECT(frame_recv(fds[1], &r, pay) == FR_OK && r.len == 1500 && memcmp(pay, big, 1500) == 0);
    h.len = 0;
    EXPECT(frame_send(fds[0], &h, NULL) == 0);
    EXPECT(frame_recv(fds[1], &r, pay) == FR_OK && r.len == 0 && r.vlan == 77);

    /* too long: nothing is sent */
    h.len = 1501;
    errno = 0;
    EXPECT(frame_send(fds[0], &h, big) == -1 && errno == EMSGSIZE);
    close(fds[0]);
    EXPECT(frame_recv(fds[1], &r, pay) == FR_EOF);          /* nothing was sent */
    close(fds[1]);

    /* peer closed: error is passed up */
    make_pair(fds);
    close(fds[1]);
    h.len = 5;
    errno = 0;
    EXPECT(frame_send(fds[0], &h, (const uint8_t *)"hello") == -1 && errno == EPIPE);
    close(fds[0]);
}

int main(void)
{
    signal(SIGPIPE, SIG_IGN);       /* the program under test does this too */

    test_pack_unpack();
    test_read_full_basic();
    test_read_full_chunks();
    test_eintr();
    test_write_errors();
    test_recv_ok();
    test_recv_failures();
    test_send();

    printf("\n%d checks, %d failures\n", checks, failures);
    printf(failures == 0 ? "ALL PASSED\n" : "SOME FAILED\n");
    return failures == 0 ? 0 : 1;
}
