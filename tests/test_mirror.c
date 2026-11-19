/*
 * test_mirror.c - unit tests of port mirroring (Part 17): forward_frame and admin_mirror with real port structures but no sockets and no threads of the
 * switch (the output queues are inspected). Also built with ThreadSanitizer and run under valgrind.
 *
 * Layout: ports 1 and 3 in VLAN 10, ports 2 and 4 in VLAN 20, port 5 the trunk, port 6 the sniffer in VLAN 99 (so that a mirrored copy can only get there
 * through the mirroring: the VLAN filter would refuse it).
 */
#include "forward.h"
#include "frame.h"
#include "log.h"
#include "mactable.h"
#include "port.h"
#include "stats.h"

#include <stdatomic.h>
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

#define NPORTS 6
#define QCAP 64
static void *region;

static void mk(mac_t m, unsigned n)
{
    m[0] = 0x02; m[1] = 0; m[2] = 0; m[3] = 0;
    m[4] = (uint8_t)(n >> 8);
    m[5] = (uint8_t)n;
}

static void setup(void)
{
    switch_init(NPORTS);
    CHECK_PT(stats_init(MAX_PORTS));
    atomic_store(&sw.max_macs_per_port, 1000000);
    sw.num_ports = NPORTS;
    size_t size = mac_region_size(256, 64);
    region = malloc(size);
    CHECK(region == NULL ? -1 : 0);
    CHECK_PT(mac_init(region, 256, 64));
    static const int vlans[NPORTS + 1] = {0, 10, 20, 10, 20, 1, 99};
    for (int i = 1; i <= NPORTS; i++) {
        sw.ports[i].state = PORT_UP;
        sw.ports[i].vlan = (uint16_t)vlans[i];
        sw.ports[i].trunk = i == 5;
        CHECK_PT(outq_init(&sw.ports[i].q, QCAP));
    }
}

static void cleanup(void)
{
    for (int i = 1; i <= NPORTS; i++) {
        sw.ports[i].state = PORT_FREE;
        outq_close(&sw.ports[i].q);
        outq_drain(&sw.ports[i].q);
        outq_destroy(&sw.ports[i].q);
    }
    mac_destroy();
    free(region);
    region = NULL;
    stats_destroy();
}

static frame_t *mkframe(unsigned src, unsigned dst, bool bcast, uint16_t vlan, uint8_t prio)
{
    frame_t *f = frame_new();

    CHECK(f == NULL ? -1 : 0);
    mk(f->src, src);
    if (bcast)
        memset(f->dst, 0xFF, MAC_LEN);
    else
        mk(f->dst, dst);
    f->vlan = vlan;
    f->prio = prio;
    f->len = 0;
    memset(f->wire, 0, HDR_LEN);
    f->wire[FRAME_OFF_PRIO] = prio;
    memcpy(f->wire + FRAME_OFF_SRC, f->src, MAC_LEN);
    memcpy(f->wire + FRAME_OFF_DST, f->dst, MAC_LEN);
    f->wire[FRAME_OFF_VLAN] = (uint8_t)(vlan >> 8);
    f->wire[FRAME_OFF_VLAN + 1] = (uint8_t)vlan;
    return f;
}

/* Send a frame as `port` (source MAC = 0x100 + port unless given) and release the receiver's reference. */
static void send_from(int port, unsigned dst, bool bcast, uint16_t vlan)
{
    frame_t *f = mkframe(0x100u + (unsigned)port, dst, bcast, vlan, 1);

    (void)forward_frame(&sw.ports[port], f);
    frame_unref(f);
}

static int queued(int port)
{
    outq_t *q = &sw.ports[port].q;

    CHECK_PT(pthread_mutex_lock(&q->lock));
    int n = q->count[0] + q->count[1] + q->count[2];
    CHECK_PT(pthread_mutex_unlock(&q->lock));
    return n;
}

static frame_t *take(int port)
{
    outq_t *q = &sw.ports[port].q;
    frame_t *f = NULL;

    if (queued(port) > 0)
        EXPECT(outq_pop(q, &f));
    return f;
}

static void empty_all(void)
{
    for (int i = 1; i <= NPORTS; i++) {
        frame_t *f;

        while ((f = take(i)) != NULL)
            frame_unref(f);
    }
}

#define MIRRORED(p) stats_get((p), STAT_MIRROR)
#define DROPPED(p) stats_get((p), STAT_DROP)

/* ---- 1. validation ---- */
static void test_validation(void)
{
    printf("admin_mirror: x != s, the sniffer is not the trunk, both ports must be UP; off is idempotent\n");
    setup();
    EXPECT(admin_mirror(1, 1) != NULL);
    EXPECT(admin_mirror(1, 5) != NULL && strstr(admin_mirror(1, 5), "trunk") != NULL);   /* s is the trunk */
    EXPECT(atomic_load(&sw.ports[1].mirror_to) == 0);
    sw.ports[1].state = PORT_FREE;
    EXPECT(admin_mirror(1, 6) != NULL && strstr(admin_mirror(1, 6), "mirrored port") != NULL);   /* x has no host */
    sw.ports[1].state = PORT_UP;
    sw.ports[6].state = PORT_DISABLED;
    EXPECT(admin_mirror(1, 6) != NULL && strstr(admin_mirror(1, 6), "sniffer port has no host") != NULL);
    sw.ports[6].state = PORT_UP;
    EXPECT(atomic_load(&sw.ports[1].mirror_to) == 0);
    EXPECT(admin_mirror(1, 6) == NULL);
    EXPECT(atomic_load(&sw.ports[1].mirror_to) == 6);
    int st, vl, mr;
    EXPECT(stats_get_port(1, &st, &vl, &mr) == 0 && mr == 6);                          /* the shared copy for show ports / stats */
    EXPECT(admin_mirror(5, 6) == NULL);                                                /* the trunk may be mirrored */
    EXPECT(admin_mirror(1, 3) == NULL && atomic_load(&sw.ports[1].mirror_to) == 3);    /* a new target replaces the old one */
    EXPECT(admin_mirror_off(1) == NULL && atomic_load(&sw.ports[1].mirror_to) == 0);
    EXPECT(admin_mirror_off(1) == NULL);                                               /* already off: OK */
    EXPECT(stats_get_port(1, &st, &vl, &mr) == 0 && mr == 0);
    cleanup();
}

/* ---- 2. direction (a): frames received on x ---- */
static void test_ingress(void)
{
    printf("(a) a frame received on the mirrored port also goes to the sniffer: the same frame (no copy), the VLAN filter is ignored, mirrored counts on x\n");
    long base = frame_live_count();
    setup();
    EXPECT(admin_mirror(1, 6) == NULL);
    send_from(1, 0x777, false, 10);                      /* unknown unicast: flooded to VLAN 10 (port 3) and the trunk (5) */
    EXPECT(queued(3) == 1 && queued(5) == 1 && queued(2) == 0 && queued(4) == 0);
    EXPECT(queued(6) == 1);                              /* the sniffer is in VLAN 99: only the mirroring can put it here */
    EXPECT(MIRRORED(1) == 1 && DROPPED(6) == 0);
    EXPECT(frame_live_count() == base + 1);              /* ONE frame shared by the three queues: the mirror is not a copy */
    frame_t *a = take(3), *b = take(6);
    EXPECT(a != NULL && a == b);                         /* the very same frame object */
    EXPECT(a != NULL && a->vlan == 10);                  /* a mirrored copy keeps the frame's VLAN (10), whatever the sniffer's port VLAN is */
    if (a != NULL)
        frame_unref(a);
    if (b != NULL)
        frame_unref(b);                                  /* two queues, two references to the SAME frame: both are released (and a failure is a FAIL line, not a crash) */
    empty_all();
    EXPECT(frame_live_count() == base);
    send_from(1, 0x778, true, 10);                       /* a broadcast too, and a frame whose destination is behind port 1 itself is still received on 1 */
    EXPECT(queued(6) == 1 && MIRRORED(1) == 2);
    empty_all();
    cleanup();
    EXPECT(frame_live_count() == base);
}

/* ---- 3. direction (b): frames queued to x ---- */
static void test_egress(void)
{
    printf("(b) a frame successfully queued to the mirrored port also goes to the sniffer\n");
    long base = frame_live_count();
    setup();
    send_from(1, 0x777, false, 10);                      /* port 1 learns its MAC (0x101) in VLAN 10 */
    empty_all();
    EXPECT(admin_mirror(1, 6) == NULL);
    send_from(3, 0x101, false, 10);                      /* 3 -> 1, a known unicast: queued to port 1 only */
    EXPECT(queued(1) == 1 && queued(3) == 0 && queued(5) == 0);
    EXPECT(queued(6) == 1 && MIRRORED(1) == 1);          /* the copy of what port 1 is sent */
    EXPECT(frame_live_count() == base + 1);
    empty_all();
    send_from(2, 0x101, false, 20);                      /* from another VLAN: not delivered to port 1 (other VLAN), so nothing to mirror for port 1 */
    EXPECT(queued(1) == 0 && queued(6) == 0 && MIRRORED(1) == 1);
    empty_all();
    cleanup();
    EXPECT(frame_live_count() == base);
}

/* ---- 4. never back to the ingress port ---- */
static void test_not_ingress(void)
{
    printf("the sniffer's own frames are not mirrored back to it; a copy is never sent to the frame's ingress port\n");
    setup();
    sw.ports[6].vlan = 10;                               /* the sniffer is in the VLAN now: its broadcast reaches port 1 normally */
    EXPECT(admin_mirror(1, 6) == NULL);
    send_from(6, 0, true, 10);                           /* frame from the sniffer, flooded to 1, 3, 5 */
    EXPECT(queued(1) == 1 && queued(3) == 1 && queued(5) == 1);
    EXPECT(queued(6) == 0 && MIRRORED(1) == 0);          /* queued to port 1, whose mirror target is the frame's own ingress port: skipped */
    empty_all();
    cleanup();
}

/* ---- 5. a full sniffer queue ---- */
static void test_sniffer_full(void)
{
    printf("a full queue of the sniffer drops the copies and counts them on the SNIFFER; the mirrored port still delivers everything\n");
    long base = frame_live_count();
    setup();
    EXPECT(admin_mirror(1, 6) == NULL);
    for (int i = 0; i < 100; i++)
        send_from(1, 0x777, false, 10);                  /* 100 unknown unicasts from port 1 */
    EXPECT(queued(6) == QCAP);                           /* the ring of the sniffer is full ... */
    EXPECT(MIRRORED(1) == QCAP);                         /* ... 64 copies were counted as mirrored on port 1 ... */
    EXPECT(DROPPED(6) == 100 - QCAP);                    /* ... and 36 were dropped, on the sniffer's counter */
    EXPECT(queued(3) == QCAP && DROPPED(3) == 100 - QCAP);   /* port 3 has the same capacity: its own drops are its own business */
    EXPECT(DROPPED(1) == 0);                             /* the mirrored port itself lost nothing */
    empty_all();
    cleanup();
    EXPECT(frame_live_count() == base);                  /* every reference of a dropped copy was released */
}

/* ---- 6. the sniffer is not UP / bad target ---- */
static void test_sniffer_state(void)
{
    printf("no copy to a sniffer that is not UP, nor to an invalid target; mirror off stops the copies\n");
    setup();
    EXPECT(admin_mirror(1, 6) == NULL);
    sw.ports[6].state = PORT_CLOSING;                    /* the reaper is tearing it down: nothing may be queued to it */
    send_from(1, 0x777, false, 10);
    EXPECT(queued(6) == 0 && MIRRORED(1) == 0 && DROPPED(6) == 0);
    sw.ports[6].state = PORT_UP;
    atomic_store(&sw.ports[1].mirror_to, 99);            /* a stale or damaged value is ignored */
    send_from(1, 0x777, false, 10);
    EXPECT(queued(6) == 0 && MIRRORED(1) == 0);
    atomic_store(&sw.ports[1].mirror_to, 0);
    empty_all();
    EXPECT(admin_mirror(1, 6) == NULL);
    send_from(1, 0x777, false, 10);
    EXPECT(queued(6) == 1 && MIRRORED(1) == 1);
    EXPECT(admin_mirror_off(1) == NULL);
    empty_all();
    send_from(1, 0x777, false, 10);
    EXPECT(queued(6) == 0 && MIRRORED(1) == 1);          /* off: no more copies, the counter keeps its value */
    empty_all();
    cleanup();
}

/* ---- 7. two mirrored ports, one sniffer ---- */
static void test_two_ports(void)
{
    printf("two mirrored ports share one sniffer: one copy per mirrored port the frame is queued to\n");
    setup();
    EXPECT(admin_mirror(1, 6) == NULL && admin_mirror(3, 6) == NULL);
    send_from(5, 0, true, 10);                           /* trunk, VLAN 10: flooded to ports 1 and 3 */
    EXPECT(queued(1) == 1 && queued(3) == 1);
    EXPECT(queued(6) == 2 && MIRRORED(1) == 1 && MIRRORED(3) == 1);
    empty_all();
    send_from(4, 0, true, 20);                           /* VLAN 20: port 2 and the trunk, no mirrored port */
    EXPECT(queued(6) == 0);
    empty_all();
    cleanup();
}

/* ---- 8. a sniffer in the mirrored VLAN gets a flooded frame twice (documented) ---- */
static void test_no_dedup(void)
{
    printf("no de-duplication (the roadmap's algorithm): a sniffer that is itself in the VLAN gets a flooded frame normally and as a copy\n");
    setup();
    sw.ports[6].vlan = 10;
    EXPECT(admin_mirror(1, 6) == NULL);
    send_from(1, 0x777, false, 10);
    EXPECT(queued(6) == 2 && MIRRORED(1) == 1);          /* the normal flood copy + the mirrored one */
    empty_all();
    cleanup();
}

/* ---- 9. threads: forwarding while the mirror is switched on and off ---- */
static atomic_bool stop_flag;

static void *forwarder(void *arg)
{
    const int port = (int)(intptr_t)arg;
    unsigned n = 0;

    while (!atomic_load(&stop_flag)) {
        send_from(port, 0x700 + (n % 7), (n % 3) == 0, port == 2 ? 20 : 10);
        n++;
        if ((n & 63) == 0)
            usleep(200);
    }
    return NULL;
}

static void test_threads(void)
{
    printf("3 threads forward while the main thread switches the mirror on and off 2000 times and empties the queues: no race, no leak\n");
    long base = frame_live_count();
    pthread_t th[3];

    setup();
    atomic_store(&stop_flag, false);
    for (int i = 0; i < 3; i++)
        CHECK_PT(pthread_create(&th[i], NULL, forwarder, (void *)(intptr_t)(i + 1)));
    for (int i = 0; i < 2000; i++) {
        if (i % 2 == 0)
            (void)admin_mirror((i % 3) + 1, 6);
        else
            (void)admin_mirror_off((i % 3) + 1);
        for (int p = 1; p <= NPORTS; p++) {
            frame_t *f;

            if (queued(p) > 0 && outq_pop(&sw.ports[p].q, &f))
                frame_unref(f);
        }
    }
    atomic_store(&stop_flag, true);
    for (int i = 0; i < 3; i++)
        CHECK_PT(pthread_join(th[i], NULL));
    empty_all();
    EXPECT(frame_live_count() == base);
    cleanup();
    EXPECT(frame_live_count() == base);
}

int main(void)
{
    char path[64];

    snprintf(path, sizeof path, "/tmp/test_mirror.%d.log", (int)getpid());
    CHECK_PT(log_start(path));                           /* admin_mirror logs: keep the test output clean */
    test_validation();
    test_ingress();
    test_egress();
    test_not_ingress();
    test_sniffer_full();
    test_sniffer_state();
    test_two_ports();
    test_no_dedup();
    test_threads();
    log_stop();
    unlink(path);
    printf("%d checks, %d failures\n%s\n", checks, failures, failures ? "FAILED" : "ALL PASSED");
    return failures ? 1 : 0;
}
