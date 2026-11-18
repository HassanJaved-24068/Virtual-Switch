/*
 * test_vlan.c - unit tests of VLAN switching and the trunk port (Part 13): forward_frame with real port structures
 * but no sockets and no threads (the output queues are inspected). Also built with ThreadSanitizer and run under
 * valgrind.
 *
 * Layout used by most tests (the roadmap's completion criterion): ports 1 and 3 in VLAN 10, ports 2 and 4 in
 * VLAN 20, port 5 the trunk, port 6 an access port in the default VLAN 1.
 */
#include "forward.h"
#include "frame.h"
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
    static const int vlans[NPORTS + 1] = {0, 10, 20, 10, 20, 1, 1};
    for (int i = 1; i <= NPORTS; i++) {
        sw.ports[i].state = PORT_UP;
        sw.ports[i].vlan = (uint16_t)vlans[i];
        sw.ports[i].trunk = i == 5;
        CHECK_PT(outq_init(&sw.ports[i].q, 64));
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
    memset(f->wire, 0, HDR_LEN);                               /* the whole header: length 0, ... */
    f->wire[FRAME_OFF_PRIO] = prio;
    memcpy(f->wire + FRAME_OFF_SRC, f->src, MAC_LEN);          /* the wire image as the receiver would have read it */
    memcpy(f->wire + FRAME_OFF_DST, f->dst, MAC_LEN);
    f->wire[FRAME_OFF_VLAN] = (uint8_t)(vlan >> 8);
    f->wire[FRAME_OFF_VLAN + 1] = (uint8_t)vlan;
    return f;
}

/* Send a frame as `port` and release the receiver's reference (the queues keep theirs). */
static enum learn_rc send_from(int port, unsigned src, unsigned dst, bool bcast, uint16_t vlan)
{
    frame_t *f = mkframe(src, dst, bcast, vlan, 1);
    enum learn_rc rc = forward_frame(&sw.ports[port], f);

    frame_unref(f);
    return rc;
}

static int queued(int port)
{
    outq_t *q = &sw.ports[port].q;

    CHECK_PT(pthread_mutex_lock(&q->lock));
    int n = q->count[0] + q->count[1] + q->count[2];
    CHECK_PT(pthread_mutex_unlock(&q->lock));
    return n;
}

/* A bit mask of the ports (bit i = port i) that have at least one frame queued. */
static unsigned who(void)
{
    unsigned m = 0;

    for (int i = 1; i <= NPORTS; i++)
        if (queued(i) > 0)
            m |= 1u << i;
    return m;
}
#define PORTS(...) ports_mask((int[]){__VA_ARGS__, 0})
static unsigned ports_mask(const int *l)
{
    unsigned m = 0;

    for (; *l; l++)
        m |= 1u << *l;
    return m;
}

/* Take the first frame out of a queue (the caller owns the reference); NULL if none. */
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

static int look(uint16_t vlan, unsigned n)
{
    mac_t m;

    mk(m, n);
    return mac_lookup(vlan, m);
}

static int check_ok(void)
{
    char why[200];
    int rc = mac_check_counts(why, sizeof why);

    if (rc != 0)
        printf("  mac_check_counts: %s\n", why);
    return rc == 0;
}

/* ---- 1. the roadmap's criterion ---- */
static void test_broadcast_and_trunk(void)
{
    printf("broadcast from port 1 (VLAN 10) reaches 3 and the trunk only; the trunk sees vlan 10; trunk frame vlan 20 -> 2 and 4\n");
    long base = frame_live_count();
    setup();

    EXPECT(send_from(1, 0xA1, 0, true, 77) == LEARN_OK);      /* the host wrote vlan 77: an access port ignores it */
    EXPECT(who() == PORTS(3, 5));
    frame_t *f3 = take(3), *f5 = take(5);
    EXPECT(f3 != NULL && f5 != NULL && f3 == f5);              /* one shared frame: the broadcast is still zero-copy */
    if (f5 != NULL) {
        EXPECT(f5->vlan == 10);                                /* the trunk host sees vlan=10 */
        EXPECT(f5->wire[FRAME_OFF_VLAN] == 0 && f5->wire[FRAME_OFF_VLAN + 1] == 10);   /* ... also in the bytes that are written */
        EXPECT(f5->refcount == 2 || f3 != f5);                 /* two references: one per queue, both taken out by us */
    }
    if (f3 != NULL)
        frame_unref(f3);
    if (f5 != NULL)
        frame_unref(f5);
    EXPECT(look(10, 0xA1) == 1 && look(77, 0xA1) == -1);       /* learned in VLAN 10, not in the host's 77 */

    send_from(5, 0xE1, 0, true, 20);                           /* injected on the trunk with vlan 20 */
    EXPECT(who() == PORTS(2, 4));
    f3 = take(2);
    EXPECT(f3 != NULL && f3->vlan == 20 && f3->wire[FRAME_OFF_VLAN + 1] == 20);   /* the trunk keeps the frame's VLAN */
    if (f3 != NULL)
        frame_unref(f3);
    empty_all();
    EXPECT(look(20, 0xE1) == 5);                               /* learned on the trunk in VLAN 20 */

    send_from(2, 0xB2, 0, true, 1);                            /* VLAN 20 access: 4 and the trunk (not 1, 3, 6) */
    EXPECT(who() == PORTS(4, 5));
    empty_all();
    send_from(6, 0xC6, 0, true, 20);                           /* the default VLAN 1: only the trunk */
    EXPECT(who() == PORTS(5));
    f5 = take(5);
    EXPECT(f5 != NULL && f5->vlan == 1);
    if (f5 != NULL)
        frame_unref(f5);
    empty_all();

    EXPECT(check_ok());
    cleanup();
    EXPECT(frame_live_count() == base);
}

/* ---- 2. unicast ---- */
static void test_unicast(void)
{
    printf("unicast is keyed by (VLAN, MAC): never delivered across VLANs; in the VLAN only the owner gets it\n");
    long base = frame_live_count();
    setup();

    send_from(1, 0xA1, 0, true, 1);                            /* A on port 1 (VLAN 10) */
    send_from(3, 0xA3, 0, true, 1);                            /* A3 on port 3 (VLAN 10) */
    send_from(2, 0xB2, 0, true, 1);                            /* B on port 2 (VLAN 20) */
    send_from(4, 0xB4, 0, true, 1);
    send_from(5, 0xE1, 0, true, 10);                           /* a MAC behind the trunk in VLAN 10 */
    empty_all();

    send_from(3, 0xA3, 0xA1, false, 1);                        /* same VLAN, known: only the owner */
    EXPECT(who() == PORTS(1));
    empty_all();

    send_from(2, 0xB2, 0xA1, false, 1);                        /* VLAN 20 -> a MAC that lives in VLAN 10: not found, flood in VLAN 20 */
    EXPECT(who() == PORTS(4, 5));
    EXPECT(queued(1) == 0 && queued(3) == 0);                  /* never delivered across VLANs */
    empty_all();

    send_from(1, 0xA1, 0xE1, false, 1);                        /* VLAN 10 -> the MAC behind the trunk: only the trunk */
    EXPECT(who() == PORTS(5));
    empty_all();

    send_from(5, 0xE1, 0xA1, false, 10);                       /* trunk -> port 1 in VLAN 10: only port 1 */
    EXPECT(who() == PORTS(1));
    empty_all();
    send_from(5, 0xE1, 0xA1, false, 20);                       /* trunk, vlan 20 -> the MAC of VLAN 10: not found: flood in 20 */
    EXPECT(who() == PORTS(2, 4));
    empty_all();

    send_from(1, 0xA1, 0xB2, false, 1);                        /* port 1 -> a MAC of VLAN 20: unknown in VLAN 10: flood in 10 */
    EXPECT(who() == PORTS(3, 5));                              /* port 2 never sees it */
    empty_all();

    send_from(1, 0xA1, 0xA1, false, 1);                        /* to its own MAC: filtered */
    EXPECT(who() == 0);

    EXPECT(look(10, 0xA1) == 1 && look(20, 0xB2) == 2 && look(10, 0xE1) == 5);
    EXPECT(check_ok());
    cleanup();
    EXPECT(frame_live_count() == base);
}

/* ---- 3. the same MAC in two VLANs ---- */
static void test_same_mac_two_vlans(void)
{
    printf("the same MAC in two VLANs: two independent entries; a destination whose port is not UP is flooded in its VLAN\n");
    long base = frame_live_count();
    setup();

    send_from(1, 0xAA, 0, true, 1);                            /* 0xAA in VLAN 10 on port 1 */
    send_from(2, 0xAA, 0, true, 1);                            /* the same MAC in VLAN 20 on port 2 */
    empty_all();
    EXPECT(look(10, 0xAA) == 1 && look(20, 0xAA) == 2 && mac_size() == 2);
    send_from(3, 0xA3, 0xAA, false, 1);                        /* VLAN 10: port 1 */
    EXPECT(who() == PORTS(1));
    empty_all();
    send_from(4, 0xB4, 0xAA, false, 1);                        /* VLAN 20: port 2 */
    EXPECT(who() == PORTS(2));
    empty_all();

    sw.ports[1].state = PORT_CLOSING;                          /* the owner leaves: flood inside the VLAN */
    send_from(3, 0xA3, 0xAA, false, 1);
    EXPECT(who() == PORTS(5));                                 /* the trunk (port 1 is not UP, 3 is the ingress) */
    empty_all();
    sw.ports[1].state = PORT_UP;
    EXPECT(check_ok());
    cleanup();
    EXPECT(frame_live_count() == base);
}

/* ---- 4. invalid VLANs ---- */
static void test_invalid_vlan(void)
{
    printf("trunk: a VLAN outside 1..4094 is dropped, counted and not learned; 1 and 4094 are valid; access ports ignore the field\n");
    long base = frame_live_count();
    setup();

    const unsigned bad[] = {0, 4095, 5000, 65535};
    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        EXPECT(send_from(5, 0xE0 + (unsigned)i, 0, true, (uint16_t)bad[i]) == LEARN_OK);
        EXPECT(who() == 0);                                    /* nobody got it */
        EXPECT(look((uint16_t)bad[i], 0xE0 + (unsigned)i) == -1);   /* and it was not learned */
    }
    EXPECT(stats_get(5, STAT_DROP) == 4);                      /* counted on the trunk, one per frame */
    EXPECT(mac_size() == 0);

    send_from(5, 0xE9, 0, true, 1);                            /* the smallest valid VLAN: only port 5's VLAN-1 neighbours */
    EXPECT(who() == PORTS(6));
    empty_all();
    send_from(5, 0xEA, 0, true, 4094);                         /* the largest valid VLAN: no access port is in it */
    EXPECT(who() == 0);
    EXPECT(look(4094, 0xEA) == 5);                             /* ... but the MAC is learned in it */
    EXPECT(stats_get(5, STAT_DROP) == 4);

    send_from(1, 0xA1, 0, true, 0);                            /* an access port with vlan 0: rewritten, not dropped */
    EXPECT(who() == PORTS(3, 5));
    empty_all();
    send_from(1, 0xA1, 0, true, 65535);
    EXPECT(who() == PORTS(3, 5));
    EXPECT(stats_get(1, STAT_DROP) == 0);
    empty_all();

    EXPECT(check_ok());
    cleanup();
    EXPECT(frame_live_count() == base);
}

/* ---- 5. port security ---- */
static void test_trunk_exempt(void)
{
    printf("port security: the trunk may learn more than max_macs_per_port MACs, an access port may not\n");
    long base = frame_live_count();
    setup();
    atomic_store(&sw.max_macs_per_port, 3);

    for (unsigned i = 0; i < 12; i++)
        EXPECT(send_from(5, 0x300 + i, 0, true, 10) == LEARN_OK);   /* 12 MACs behind the trunk: all fine */
    EXPECT(look(10, 0x300) == 5 && look(10, 0x30B) == 5);
    empty_all();
    for (unsigned i = 0; i < 3; i++)
        EXPECT(send_from(1, 0x100 + i, 0, true, 1) == LEARN_OK);
    EXPECT(send_from(1, 0x103, 0, true, 1) == LEARN_VIOLATION); /* the 4th on an access port: violation */
    empty_all();
    EXPECT(check_ok());
    cleanup();
    EXPECT(frame_live_count() == base);
}

/* ---- 6. port_set_vlan ---- */
static void test_set_vlan(void)
{
    printf("port_set_vlan: the new VLAN applies at once and the port's old MAC entries are flushed\n");
    long base = frame_live_count();
    setup();

    send_from(3, 0xA3, 0, true, 1);                            /* 0xA3 on port 3 (VLAN 10) */
    send_from(1, 0xA1, 0, true, 1);
    empty_all();
    EXPECT(look(10, 0xA3) == 3 && mac_size() == 2);
    port_set_vlan(&sw.ports[3], 20);
    EXPECT(sw.ports[3].vlan == 20);
    EXPECT(look(10, 0xA3) == -1 && mac_size() == 1);           /* the entry of the old VLAN vanished, port 1's stayed */
    EXPECT(look(10, 0xA1) == 1);

    send_from(1, 0xA1, 0, true, 1);                            /* VLAN 10: 3 no longer gets it */
    EXPECT(who() == PORTS(5));
    empty_all();
    send_from(2, 0xB2, 0, true, 1);                            /* VLAN 20: 3 gets it now */
    EXPECT(who() == PORTS(3, 4, 5));
    empty_all();
    send_from(3, 0xA3, 0xB2, false, 1);                        /* port 3 learned in VLAN 20 now; B2 lives there: unicast */
    EXPECT(who() == PORTS(2));
    EXPECT(look(20, 0xA3) == 3);
    empty_all();

    /* The short window inside port_set_vlan (VLAN set, entries not yet flushed): a stale entry must not attract frames of
     * another VLAN. Simulated by changing the field only: port 1 is in VLAN 10 and its entry says so; "move" port 1. */
    sw.ports[1].vlan = 20;
    sw.ports[6].vlan = 10;                                     /* somebody else in VLAN 10 */
    send_from(5, 0xE1, 0xA1, false, 10);                       /* trunk, VLAN 10 -> the entry (10, A1) -> port 1, which is in VLAN 20 now */
    EXPECT(queued(1) == 0);                                    /* not sent to port 1 ... */
    EXPECT(who() == PORTS(6));                                 /* ... but flooded inside VLAN 10 (not lost) */
    empty_all();
    sw.ports[1].vlan = 10;
    sw.ports[6].vlan = 1;

    port_set_vlan(&sw.ports[3], 20);                           /* the same VLAN again: still flushes (and is harmless) */
    EXPECT(look(20, 0xA3) == -1 && sw.ports[3].vlan == 20);
    port_set_vlan(&sw.ports[3], 4094);                         /* the largest VLAN */
    EXPECT(sw.ports[3].vlan == 4094);
    port_set_vlan(&sw.ports[3], 10);
    EXPECT(sw.ports[3].vlan == 10 && check_ok());
    cleanup();
    EXPECT(frame_live_count() == base);
}

/* ---- 7. priorities and payload bytes are untouched ---- */
static void test_other_fields(void)
{
    printf("only the VLAN field is rewritten: addresses, priority and the other wire bytes stay\n");
    long base = frame_live_count();
    setup();

    frame_t *f = mkframe(0xA1, 0, true, 33, 2);
    uint8_t before[HDR_LEN];
    memcpy(before, f->wire, HDR_LEN);
    forward_frame(&sw.ports[1], f);
    frame_unref(f);
    frame_t *g = take(3);
    EXPECT(g != NULL);
    if (g != NULL) {
        EXPECT(g->prio == 2 && g->wire[FRAME_OFF_PRIO] == before[FRAME_OFF_PRIO]);
        EXPECT(memcmp(g->wire, before, FRAME_OFF_VLAN) == 0);       /* length, src and dst bytes are the same */
        EXPECT(g->wire[FRAME_OFF_VLAN + 1] == 10);
        frame_unref(g);
    }
    empty_all();
    cleanup();
    EXPECT(frame_live_count() == base);
}

/* ---- 8. concurrency: traffic while the VLAN of a port changes ---- */
static _Atomic bool stop_flag;

static void *sender(void *arg)
{
    const int port = (int)(long)arg;

    for (unsigned i = 0; !atomic_load(&stop_flag); i++) {
        frame_t *f = mkframe(0x500 + (unsigned)port * 16 + (i % 8), 0, true, (uint16_t)(1 + i % 30), 1);

        (void)forward_frame(&sw.ports[port], f);
        frame_unref(f);
        if (i % 16 == 0) {                         /* keep the queues from filling up: the "senders" are not running */
            outq_drain(&sw.ports[1 + (int)(i / 16) % NPORTS].q);   /* locked, never blocks (several threads do this) */
        }
    }
    return NULL;
}

static void *flipper(void *arg)
{
    (void)arg;
    for (int i = 0; i < 400; i++)
        port_set_vlan(&sw.ports[3], (uint16_t)(i % 2 ? 10 : 20));
    return NULL;
}

static void test_concurrent(void)
{
    printf("4 threads forward while another changes the VLAN of port 3 400 times: no race, table consistent, no leak\n");
    long base = frame_live_count();
    setup();
    pthread_t s[3], fl;

    atomic_store(&stop_flag, false);
    for (long i = 0; i < 3; i++)
        CHECK_PT(pthread_create(&s[i], NULL, sender, (void *)(i == 0 ? 1L : i == 1 ? 2L : 5L)));
    CHECK_PT(pthread_create(&fl, NULL, flipper, NULL));
    CHECK_PT(pthread_join(fl, NULL));
    atomic_store(&stop_flag, true);
    for (int i = 0; i < 3; i++)
        CHECK_PT(pthread_join(s[i], NULL));
    empty_all();
    EXPECT(check_ok());
    cleanup();
    EXPECT(frame_live_count() == base);
}

int main(void)
{
    alarm(180);
    test_broadcast_and_trunk();
    test_unicast();
    test_same_mac_two_vlans();
    test_invalid_vlan();
    test_trunk_exempt();
    test_set_vlan();
    test_other_fields();
    test_concurrent();
    printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
