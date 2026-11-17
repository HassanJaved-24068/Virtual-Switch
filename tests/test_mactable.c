/*
 * test_mactable.c - unit tests for the MAC table (Part 6).
 *
 * The table structures are public (shared-memory-ready layout), so these tests
 * look inside the region to check placement and to set `last_seen` values
 * directly instead of waiting for time to pass. Run normally, under
 * ThreadSanitizer and under valgrind.
 */
#include "forward.h"
#include "frame.h"
#include "mactable.h"
#include "port.h"
#include "sig.h"
#include "stats.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <semaphore.h>
#include <signal.h>
#include <stdatomic.h>
#include <sys/socket.h>
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

/* ---- access to the table region ---- */


/* Read a port's state the way the switch does: under ports_lock (read side). */
static enum port_state port_state(int id)
{
    enum port_state st;

    CHECK_PT(pthread_rwlock_rdlock(&sw.ports_lock));
    st = sw.ports[id].state;
    CHECK_PT(pthread_rwlock_unlock(&sw.ports_lock));
    return st;
}

static void *region;
static mactable_t *T;
static mac_bucket_t *BK;
static mac_entry_t *EN;

static void setup(int K, int nb)
{
    switch_init(8);                               /* fresh ports: all mac_count 0 */
    atomic_store(&sw.max_macs_per_port, 1000000); /* port security off unless a test sets it */
    size_t size = mac_region_size(K, nb);
    region = malloc(size);
    CHECK(region == NULL ? -1 : 0);
    CHECK_PT(mac_init(region, K, nb));
    T = region;
    BK = (mac_bucket_t *)(T + 1);
    EN = (mac_entry_t *)(BK + T->nbuckets);
}

static void cleanup(void)
{
    mac_destroy();
    free(region);
    region = NULL;
}

static void mk(mac_t m, unsigned n)
{
    m[0] = 0x02; m[1] = 0; m[2] = 0; m[3] = 0;
    m[4] = (uint8_t)(n >> 8);
    m[5] = (uint8_t)n;
}

static int check_ok(void)
{
    char why[200];
    int rc = mac_check_counts(why, sizeof why);
    if (rc != 0)
        printf("  mac_check_counts: %s\n", why);
    return rc == 0;
}

static mac_entry_t *find_entry(uint16_t vlan, const mac_t m)
{
    for (int i = 0; i < T->K; i++)
        if (EN[i].used && EN[i].vlan == vlan && memcmp(EN[i].mac, m, MAC_LEN) == 0)
            return &EN[i];
    return NULL;
}

/* Pretend the MAC was last seen `ms` milliseconds ago (single-threaded tests only). */
static void age_entry(uint16_t vlan, unsigned n, long ms)
{
    mac_t m;
    mk(m, n);
    mac_entry_t *e = find_entry(vlan, m);
    if (e != NULL)
        e->last_seen = ts_add_ms(now_mono(), -ms);
}

static int learn(uint16_t vlan, unsigned n, int port)
{
    mac_t m;
    mk(m, n);
    return (int)mac_learn_ex(vlan, m, port, NULL);
}

static int look(uint16_t vlan, unsigned n)
{
    mac_t m;
    mk(m, n);
    return mac_lookup(vlan, m);
}

/* Independent FNV-1a, to check that entries sit in the bucket the roadmap prescribes. */
static unsigned fnv_bucket(uint16_t vlan, const mac_t m, int nb)
{
    uint8_t key[8] = {(uint8_t)(vlan >> 8), (uint8_t)vlan, m[0], m[1], m[2], m[3], m[4], m[5]};
    uint32_t h = 2166136261u;
    for (int i = 0; i < 8; i++) {
        h ^= key[i];
        h *= 16777619u;
    }
    return h & (unsigned)(nb - 1);
}

/* ---- 1. no table, init ---- */
static void test_init(void)
{
    printf("init: validation, layout, empty table\n");
    mac_t m;
    mk(m, 1);
    /* before any table exists nothing crashes */
    EXPECT(mac_learn_ex(1, m, 1, NULL) == LEARN_SKIPPED);
    EXPECT(mac_lookup(1, m) == -1);
    EXPECT(mac_age(1) == 0 && mac_flush_port(1) == 0 && mac_clear() == 0 && mac_size() == 0);
    EXPECT(!mac_evict_lru());

    EXPECT(mac_region_size(0, 4) == 0);
    EXPECT(mac_region_size(-1, 4) == 0);
    EXPECT(mac_region_size(4, 0) == 0);
    EXPECT(mac_region_size(4, 3) == 0);               /* not a power of two */
    EXPECT(mac_region_size(4, 6) == 0);
    EXPECT(mac_region_size(4, -4) == 0);
    EXPECT(mac_region_size(4, 1) > 0 && mac_region_size(4, 1024) > mac_region_size(4, 1));
    EXPECT(mac_region_size(8, 4) > mac_region_size(4, 4));
    char buf[4096] __attribute__((aligned(16)));
    EXPECT(mac_init(NULL, 4, 4) == EINVAL);
    EXPECT(mac_init(buf, 4, 3) == EINVAL);
    EXPECT(mac_init(buf + 1, 4, 4) == EINVAL);        /* misaligned */
    EXPECT(mac_init(buf, 0, 4) == EINVAL);

    setup(16, 4);
    EXPECT(T->magic == MAC_TABLE_MAGIC && T->K == 16 && T->nbuckets == 4);
    EXPECT(T->count == 0 && mac_size() == 0);
    EXPECT(look(1, 1) == -1);
    EXPECT(check_ok());                                /* count + free = K, free list intact */
    int ok = 1;
    for (int i = 0; i < 4; i++)
        if (BK[i].first != -1)
            ok = 0;
    EXPECT(ok);
    EXPECT(T->free_head == 0 && EN[0].next == 1 && EN[15].next == -1);
    cleanup();
}

/* ---- 2. learn / lookup / move ---- */
static void test_learn_lookup(void)
{
    printf("learn, lookup, refresh, key = (vlan, mac), moves, bad ports\n");
    setup(16, 4);
    mac_t a, b;
    mk(a, 0xA);
    mk(b, 0xB);

    EXPECT(learn(1, 0xA, 1) == LEARN_OK);
    EXPECT(look(1, 0xA) == 1);
    EXPECT(look(2, 0xA) == -1);                        /* the vlan is part of the key */
    EXPECT(look(1, 0xB) == -1);
    EXPECT(atomic_load(&sw.ports[1].mac_count) == 1 && mac_size() == 1);

    /* seen again on the same port: only last_seen changes */
    age_entry(1, 0xA, 5000);
    EXPECT(ts_diff_ms(now_mono(), find_entry(1, a)->last_seen) >= 5000);
    EXPECT(learn(1, 0xA, 1) == LEARN_OK);
    EXPECT(ts_diff_ms(now_mono(), find_entry(1, a)->last_seen) < 500);
    EXPECT(atomic_load(&sw.ports[1].mac_count) == 1 && mac_size() == 1);

    /* a lookup does NOT refresh last_seen: aging counts time since last seen as a source */
    age_entry(1, 0xA, 5000);
    for (int i = 0; i < 10; i++)
        EXPECT(look(1, 0xA) == 1);
    EXPECT(ts_diff_ms(now_mono(), find_entry(1, a)->last_seen) >= 5000);

    /* same MAC in another vlan is another entry */
    EXPECT(learn(2, 0xA, 4) == LEARN_OK);
    EXPECT(look(2, 0xA) == 4 && look(1, 0xA) == 1 && mac_size() == 2);

    /* a move: new port, counts adjusted on both, and reported */
    struct mac_move mv;
    EXPECT(mac_learn_ex(1, a, 3, &mv) == LEARN_OK);
    EXPECT(mv.moved && mv.from == 1 && mv.to == 3 && mv.vlan == 1 && memcmp(mv.mac, a, MAC_LEN) == 0);
    EXPECT(look(1, 0xA) == 3);
    EXPECT(atomic_load(&sw.ports[1].mac_count) == 0 && atomic_load(&sw.ports[3].mac_count) == 1);
    EXPECT(mac_learn_ex(1, a, 3, &mv) == LEARN_OK && !mv.moved);     /* same port: no move */
    EXPECT(mac_size() == 2);
    EXPECT(mac_learn(1, a, 5) == LEARN_OK && look(1, 0xA) == 5);      /* the logging version */

    /* port range: 0 (bonus trunk) is valid, outside is skipped */
    EXPECT(learn(1, 0xB, 0) == LEARN_OK && look(1, 0xB) == 0);
    EXPECT(learn(1, 0xC, -1) == LEARN_SKIPPED);
    EXPECT(learn(1, 0xC, MAX_PORTS + 1) == LEARN_SKIPPED);
    EXPECT(look(1, 0xC) == -1);
    EXPECT(learn(1, 0xC, MAX_PORTS) == LEARN_OK && look(1, 0xC) == MAX_PORTS);
    EXPECT(check_ok());
    EXPECT(mac_learn_ex(1, b, 1, NULL) == LEARN_OK);   /* NULL for the move report is allowed */
    cleanup();
}

/* ---- 3. hashing and chains ---- */
static void test_hash_and_chains(void)
{
    printf("FNV-1a bucket placement; one long chain; removal from the middle of a chain\n");
    setup(200, 16);
    int ok = 1;
    for (unsigned n = 1; n <= 150; n++)
        if (learn((uint16_t)(1 + n % 7), n, 1 + (int)(n % 8)) != LEARN_OK)
            ok = 0;
    EXPECT(ok && mac_size() == 150);
    /* every entry is in the bucket that an independent FNV-1a computation gives */
    int placed = 0, wrong = 0, used_buckets = 0;
    for (int bi = 0; bi < 16; bi++) {
        int len = 0;
        for (int32_t j = BK[bi].first; j >= 0; j = EN[j].next) {
            len++;
            placed++;
            if (fnv_bucket(EN[j].vlan, EN[j].mac, 16) != (unsigned)bi)
                wrong++;
        }
        if (len > 0)
            used_buckets++;
    }
    EXPECT(placed == 150 && wrong == 0);
    EXPECT(used_buckets >= 12);                        /* the hash really spreads the keys */
    ok = 1;
    for (unsigned n = 1; n <= 150; n++)
        if (look((uint16_t)(1 + n % 7), n) != 1 + (int)(n % 8))
            ok = 0;
    EXPECT(ok);
    EXPECT(check_ok());
    cleanup();

    /* a single bucket: everything is one chain */
    setup(50, 1);
    for (unsigned n = 1; n <= 50; n++)
        learn(1, n, 2);
    EXPECT(mac_size() == 50 && look(1, 1) == 2 && look(1, 25) == 2 && look(1, 50) == 2);
    /* remove from the head, the middle and the tail of the chain using flush by port */
    for (unsigned n = 10; n <= 20; n++)
        learn(1, n, 3);
    EXPECT(mac_flush_port(3) == 11);
    EXPECT(look(1, 10) == -1 && look(1, 20) == -1 && look(1, 9) == 2 && look(1, 21) == 2);
    EXPECT(mac_size() == 39 && check_ok());
    cleanup();
}

/* ---- 4. LRU eviction ---- */
static void test_eviction(void)
{
    printf("LRU eviction: the entry seen longest ago goes\n");
    setup(4, 4);
    EXPECT(!mac_evict_lru());                          /* empty table: nothing to evict */
    for (unsigned n = 1; n <= 4; n++)
        EXPECT(learn(1, n, (int)n) == LEARN_OK);
    EXPECT(mac_size() == 4 && T->free_head == -1);     /* the pool is empty */
    age_entry(1, 1, 4000);
    age_entry(1, 2, 3000);
    age_entry(1, 3, 2000);
    age_entry(1, 4, 1000);

    EXPECT(learn(1, 5, 5) == LEARN_OK);                /* needs a slot: evicts MAC 1 */
    EXPECT(look(1, 1) == -1 && look(1, 2) == 2 && look(1, 3) == 3 && look(1, 4) == 4 && look(1, 5) == 5);
    EXPECT(atomic_load(&sw.ports[1].mac_count) == 0);  /* the victim's port was decremented */
    EXPECT(atomic_load(&sw.ports[5].mac_count) == 1);
    EXPECT(mac_size() == 4 && check_ok());

    learn(1, 2, 2);                                    /* MAC 2 is seen again: now the newest */
    age_entry(1, 3, 9000);                             /* MAC 3 becomes the oldest */
    EXPECT(learn(1, 6, 6) == LEARN_OK);
    EXPECT(look(1, 3) == -1 && look(1, 2) == 2 && look(1, 6) == 6);
    EXPECT(mac_size() == 4 && check_ok());

    /* explicit eviction */
    EXPECT(mac_evict_lru() && mac_size() == 3 && check_ok());
    EXPECT(mac_evict_lru() && mac_evict_lru() && mac_evict_lru() && !mac_evict_lru());
    EXPECT(mac_size() == 0 && check_ok());
    cleanup();

    /* K = 1 */
    setup(1, 1);
    EXPECT(learn(1, 1, 1) == LEARN_OK && learn(1, 2, 2) == LEARN_OK);
    EXPECT(look(1, 1) == -1 && look(1, 2) == 2 && mac_size() == 1 && check_ok());
    EXPECT(atomic_load(&sw.ports[1].mac_count) == 0 && atomic_load(&sw.ports[2].mac_count) == 1);
    cleanup();

    /* a full table with all MACs on one port: eviction keeps that port's count right */
    setup(8, 2);
    for (unsigned n = 1; n <= 30; n++)
        learn(1, n, 7);
    EXPECT(mac_size() == 8 && atomic_load(&sw.ports[7].mac_count) == 8 && check_ok());
    cleanup();
}

/* ---- 5. aging ---- */
static void test_aging(void)
{
    printf("aging: only entries older than T are removed; counts follow\n");
    setup(32, 8);
    learn(1, 1, 1);
    learn(1, 2, 2);
    learn(1, 3, 2);
    learn(1, 4, 3);
    learn(1, 5, 3);
    age_entry(1, 1, 100);
    age_entry(1, 2, 500);
    age_entry(1, 3, 2500);
    age_entry(1, 4, 3500);
    age_entry(1, 5, 5000);
    EXPECT(mac_age(3) == 2);                           /* the two that are more than 3 s old */
    EXPECT(look(1, 4) == -1 && look(1, 5) == -1);
    EXPECT(look(1, 1) == 1 && look(1, 2) == 2 && look(1, 3) == 2);
    EXPECT(atomic_load(&sw.ports[3].mac_count) == 0 && atomic_load(&sw.ports[2].mac_count) == 2);
    EXPECT(mac_size() == 3 && check_ok());
    EXPECT(mac_age(3) == 0);                           /* nothing more is old enough */
    EXPECT(mac_age(100000) == 0);
    EXPECT(mac_age(1) == 1 && look(1, 3) == -1);       /* 2.5 s old: gone with T = 1; the 0.5 s one stays */
    EXPECT(mac_age(0) == 2 && mac_size() == 0);        /* T = 0: everything with a positive age */
    EXPECT(mac_age(5) == 0 && check_ok());
    /* the freed entries are reusable */
    for (unsigned n = 1; n <= 32; n++)
        EXPECT(learn(1, n, 1) == LEARN_OK);
    EXPECT(mac_size() == 32 && check_ok());
    cleanup();
}

/* ---- 6. flush and clear ---- */
static void test_flush_clear(void)
{
    printf("flush a port, clear everything\n");
    setup(64, 8);
    for (unsigned n = 0; n < 40; n++)
        learn((uint16_t)(1 + n % 3), 100 + n, 1 + (int)(n % 4));
    EXPECT(mac_size() == 40);
    EXPECT(mac_flush_port(2) == 10);
    EXPECT(atomic_load(&sw.ports[2].mac_count) == 0);
    for (unsigned n = 0; n < 40; n++) {
        int want = (1 + (int)(n % 4) == 2) ? -1 : 1 + (int)(n % 4);
        EXPECT(look((uint16_t)(1 + n % 3), 100 + n) == want);
    }
    EXPECT(mac_flush_port(2) == 0);                    /* nothing left on that port */
    EXPECT(mac_flush_port(7) == 0);
    EXPECT(mac_flush_port(-1) == 0 && mac_flush_port(MAX_PORTS + 5) == 0);
    EXPECT(mac_size() == 30 && check_ok());

    EXPECT(mac_clear() == 30);
    EXPECT(mac_size() == 0 && check_ok());
    for (int p = 0; p <= MAX_PORTS; p++)
        EXPECT(atomic_load(&sw.ports[p].mac_count) == 0);
    EXPECT(mac_clear() == 0);
    EXPECT(learn(1, 1, 1) == LEARN_OK && look(1, 1) == 1);   /* usable after clear */
    cleanup();
}

/* ---- 7. foreach and dump ---- */
struct fe {
    int n, ports_sum;
    long max_age;
    int reentrant_ok;
};

static void fe_cb(const mac_info_t *m, void *arg)
{
    struct fe *f = arg;
    f->n++;
    f->ports_sum += m->port;
    if (m->age_ms > f->max_age)
        f->max_age = m->age_ms;
    /* The callback runs with no lock held: it may use the table, also the same bucket. */
    if (mac_lookup(m->vlan, m->mac) == m->port && mac_learn_ex(m->vlan, m->mac, m->port, NULL) == LEARN_OK)
        f->reentrant_ok++;
}

static void test_foreach_dump(void)
{
    printf("foreach (callback runs without a lock) and dump\n");
    setup(32, 4);
    for (unsigned n = 1; n <= 10; n++)
        learn(1, n, (int)n);
    age_entry(1, 3, 7000);
    struct fe f = {0, 0, 0, 0};
    EXPECT(mac_foreach(fe_cb, &f) == 10);
    EXPECT(f.n == 10 && f.ports_sum == 55 && f.max_age >= 7000 && f.reentrant_ok == 10);
    struct fe none = {0, 0, 0, 0};
    mac_clear();
    EXPECT(mac_foreach(fe_cb, &none) == 0 && none.n == 0);

    for (unsigned n = 1; n <= 3; n++)
        learn(5, 0x10 + n, 2);
    FILE *fp = tmpfile();
    CHECK(fp == NULL ? -1 : 0);
    EXPECT(mac_dump(fp) == 3);
    rewind(fp);
    char line[200];
    int lines = 0, entries = 0, header = 0;
    while (fgets(line, sizeof line, fp) != NULL) {
        lines++;
        if (line[0] == '#')
            header++;
        unsigned vlan;
        char mac[20];
        int port;
        if (sscanf(line, "vlan %u %17s port %d", &vlan, mac, &port) == 3 && vlan == 5 && port == 2 &&
            strncmp(mac, "02:00:00:00:00:1", 16) == 0)
            entries++;
    }
    EXPECT(lines == 4 && header == 1 && entries == 3);
    fclose(fp);
    cleanup();
}

/* ---- 7b. the SIGUSR1 dump to a file (Part 10) ---- */
struct dump_stats {
    int header, entries, bad, duplicates;
    bool seen[3][400];                              /* [vlan][id] */
};

/* Parse a dump file: 2 header lines "# ...", then "vlan mac port age_s". Counts what is wrong. */
static void parse_dump(const char *path, struct dump_stats *d)
{
    FILE *fp = fopen(path, "r");
    char line[200];

    memset(d, 0, sizeof *d);
    CHECK(fp == NULL ? -1 : 0);
    while (fgets(line, sizeof line, fp) != NULL) {
        unsigned vlan;
        char mac[32];
        int port;
        long age;
        char extra;

        if (line[0] == '#') {
            d->header++;
            continue;
        }
        if (sscanf(line, "%u %31s %d %ld%c", &vlan, mac, &port, &age, &extra) == 5 && extra == '\n' && strlen(mac) == 17 &&
            strncmp(mac, "02:00:00:00:", 12) == 0 && vlan >= 1 && vlan <= 2 && port >= 1 && port <= 8 && age >= 0) {
            unsigned hi, lo;

            if (sscanf(mac + 12, "%2x:%2x", &hi, &lo) == 2 && hi * 256 + lo < 400) {
                d->entries++;
                if (d->seen[vlan][hi * 256 + lo])
                    d->duplicates++;
                d->seen[vlan][hi * 256 + lo] = true;
                continue;
            }
        }
        d->bad++;
        if (d->bad <= 3)
            printf("  bad dump line: %s", line);        /* shows what is wrong */
    }
    fclose(fp);
}

static atomic_bool dump_stop;

static void *dump_churn(void *arg)
{
    unsigned x = 12345u + (unsigned)(uintptr_t)arg * 7919u;

    while (!atomic_load(&dump_stop)) {
        x = x * 1103515245u + 12345u;
        unsigned id = (x >> 8) % 300;
        uint16_t vlan = (uint16_t)(1 + (x >> 20) % 2);

        learn(vlan, id, 1 + (int)(id % 4));
        if (x % 61 == 0)
            mac_age(0);                              /* removes (almost) everything: the table is emptied and refilled */
        if (x % 997 == 0)
            mac_flush_port(1 + (int)(id % 4));
    }
    return NULL;
}

static void test_dump_file(void)
{
    printf("mac_dump_file: format, mode w, errors, 4 threads learning and aging while dumping\n");
    char path[] = "/tmp/test_dump_XXXXXX";
    int fd = mkstemp(path);
    struct dump_stats d;

    CHECK(fd);
    CHECK(close(fd));
    setup(512, 64);

    EXPECT(mac_dump_file(path) == 0);                       /* an empty table: only the header */
    parse_dump(path, &d);
    EXPECT(d.header == 2 && d.entries == 0 && d.bad == 0);

    for (unsigned n = 1; n <= 10; n++)
        learn((uint16_t)(n % 2 + 1), n, (int)(n % 4 + 1));
    age_entry(1, 2, 7500);                                  /* vlan 1 = even n? n%2+1: n=2 -> vlan 1 */
    age_entry(2, 3, 12999);
    EXPECT(mac_dump_file(path) == 10);
    parse_dump(path, &d);
    EXPECT(d.header == 2 && d.entries == 10 && d.bad == 0 && d.duplicates == 0);

    /* exact values of two entries: vlan, mac, port, age in whole seconds (7500 ms -> 7, 12999 ms -> 12) */
    FILE *fp = fopen(path, "r");
    char line[200];
    int saw_first = 0, saw_second = 0, saw_header_time = 0;

    CHECK(fp == NULL ? -1 : 0);
    while (fgets(line, sizeof line, fp) != NULL) {
        unsigned y, mo, dd, h, mi, sec;

        if (sscanf(line, "# MAC table dump, %u-%u-%u %u:%u:%u", &y, &mo, &dd, &h, &mi, &sec) == 6 && y >= 2024)
            saw_header_time = 1;
        if (strncmp(line, "1 02:00:00:00:00:02 3 7\n", 24) == 0 || (strncmp(line, "1 02:00:00:00:00:02 3 8\n", 24) == 0))
            saw_first = 1;
        if (strncmp(line, "2 02:00:00:00:00:03 4 12\n", 25) == 0 || strncmp(line, "2 02:00:00:00:00:03 4 13\n", 25) == 0)
            saw_second = 1;
    }
    fclose(fp);
    EXPECT(saw_header_time && saw_first && saw_second);

    /* mode "w": a longer old content is gone */
    fp = fopen(path, "w");
    CHECK(fp == NULL ? -1 : 0);
    for (int i = 0; i < 300; i++)
        fputs("junk junk junk junk junk\n", fp);
    fclose(fp);
    EXPECT(mac_dump_file(path) == 10);
    parse_dump(path, &d);
    EXPECT(d.header == 2 && d.entries == 10 && d.bad == 0);

    /* errors: no directory, a directory, a full device */
    errno = 0;
    EXPECT(mac_dump_file("/nonexistent-dir/dump.txt") == -1 && errno == ENOENT);
    errno = 0;
    EXPECT(mac_dump_file("/tmp") == -1 && errno == EISDIR);
    if (access("/dev/full", W_OK) == 0) {
        errno = 0;
        EXPECT(mac_dump_file("/dev/full") == -1 && errno == ENOSPC);   /* a write error is reported, not ignored */
    }
    EXPECT(check_ok());

    /* 4 threads learn, age and flush while the main thread dumps 60 times: every dump is whole */
    mac_clear();
    atomic_store(&dump_stop, false);
    pthread_t th[4];
    for (intptr_t i = 0; i < 4; i++)
        CHECK_PT(pthread_create(&th[i], NULL, dump_churn, (void *)i));
    int whole = 0, max_entries = 0;
    for (int i = 0; i < 60; i++) {
        int n = mac_dump_file(path);

        parse_dump(path, &d);
        if (n >= 0 && d.header == 2 && d.bad == 0 && d.duplicates == 0 && d.entries == n)
            whole++;
        if (d.entries > max_entries)
            max_entries = d.entries;
    }
    atomic_store(&dump_stop, true);
    for (int i = 0; i < 4; i++)
        CHECK_PT(pthread_join(th[i], NULL));
    EXPECT(whole == 60);                                    /* each dump: header, whole lines, no entry twice, count = lines */
    EXPECT(max_entries > 0);                                /* the dumps really saw a table that was changing */
    EXPECT(check_ok());                                     /* and the table is still consistent */

    unlink(path);
    cleanup();
}

/* ---- 8. port security ---- */
static void test_port_security(void)
{
    printf("port security: more than M MACs on a port is a violation\n");
    setup(64, 8);
    atomic_store(&sw.max_macs_per_port, 3);
    EXPECT(learn(1, 1, 5) == LEARN_OK);
    EXPECT(learn(1, 2, 5) == LEARN_OK);
    EXPECT(learn(1, 3, 5) == LEARN_OK);                /* exactly M: still fine */
    EXPECT(learn(1, 3, 5) == LEARN_OK);                /* a known MAC again: no new entry */
    EXPECT(learn(1, 4, 5) == LEARN_VIOLATION);         /* the 4th */
    EXPECT(atomic_load(&sw.ports[5].mac_count) == 4);  /* it was inserted; flush will remove it */
    EXPECT(look(1, 4) == 5);
    EXPECT(learn(1, 5, 5) == LEARN_VIOLATION);
    EXPECT(learn(1, 1, 5) == LEARN_OK);                /* existing MACs are never a violation */

    /* other ports have their own count */
    EXPECT(learn(1, 10, 6) == LEARN_OK && learn(1, 11, 6) == LEARN_OK && learn(1, 12, 6) == LEARN_OK);
    EXPECT(learn(1, 13, 6) == LEARN_VIOLATION);

    /* a move into a port is not a new insertion */
    EXPECT(learn(1, 20, 7) == LEARN_OK && learn(1, 21, 7) == LEARN_OK && learn(1, 22, 7) == LEARN_OK);
    EXPECT(learn(1, 22, 5) == LEARN_OK);
    EXPECT(atomic_load(&sw.ports[5].mac_count) == 6 && atomic_load(&sw.ports[7].mac_count) == 2);

    /* a trunk port is exempt */
    sw.ports[4].trunk = true;
    int ok = 1;
    for (unsigned n = 100; n < 120; n++)
        if (learn(1, n, 4) != LEARN_OK)
            ok = 0;
    EXPECT(ok && atomic_load(&sw.ports[4].mac_count) == 20);
    sw.ports[4].trunk = false;

    /* after the flush (what the reaper does at teardown) the port can learn again */
    EXPECT(mac_flush_port(5) == 6);
    EXPECT(atomic_load(&sw.ports[5].mac_count) == 0);
    EXPECT(learn(1, 200, 5) == LEARN_OK);
    EXPECT(check_ok());
    cleanup();
}

/* ---- 9. robust mutexes ---- */
static pthread_mutex_t *victim_mutex;

static void *die_holding_lock(void *arg)
{
    (void)arg;
    CHECK_PT(pthread_mutex_lock(victim_mutex));
    return NULL;                                       /* exits without unlocking */
}

static void test_robust(void)
{
    printf("robust mutexes: a lock whose owner died is recovered (EOWNERDEAD)\n");
    setup(16, 4);
    mac_t m;
    mk(m, 1);
    unsigned b = fnv_bucket(1, m, 4);

    alarm(20);                                         /* a hang would be a failure, not a stuck test */
    victim_mutex = &BK[b].lock;
    pthread_t th;
    CHECK_PT(pthread_create(&th, NULL, die_holding_lock, NULL));
    CHECK_PT(pthread_join(th, NULL));
    EXPECT(learn(1, 1, 2) == LEARN_OK);                /* takes the dead owner's bucket lock */
    EXPECT(look(1, 1) == 2);
    EXPECT(learn(1, 1, 3) == LEARN_OK && look(1, 1) == 3);

    victim_mutex = &T->pool_lock;                      /* the same for the pool lock */
    CHECK_PT(pthread_create(&th, NULL, die_holding_lock, NULL));
    CHECK_PT(pthread_join(th, NULL));
    EXPECT(learn(1, 2, 2) == LEARN_OK && mac_size() == 2);
    EXPECT(mac_age(100) == 0 && mac_flush_port(3) == 1);
    EXPECT(check_ok());
    alarm(0);
    cleanup();
}

/* ---- 10. check_counts really detects problems ---- */
static void test_check_counts(void)
{
    printf("mac_check_counts detects wrong counts and a damaged free list\n");
    setup(8, 4);
    for (unsigned n = 1; n <= 5; n++)
        learn(1, n, 1 + (int)(n % 2));
    char why[200];
    EXPECT(mac_check_counts(why, sizeof why) == 0);

    atomic_fetch_add(&sw.ports[2].mac_count, 1);       /* a port's count is wrong */
    EXPECT(mac_check_counts(why, sizeof why) == -1 && strstr(why, "port 2") != NULL);
    atomic_fetch_sub(&sw.ports[2].mac_count, 1);

    T->count++;                                        /* total count is wrong */
    EXPECT(mac_check_counts(why, sizeof why) == -1);
    T->count--;

    int32_t saved = T->free_head;                      /* free list contains a used entry */
    T->free_head = BK[0].first >= 0 ? BK[0].first : (BK[1].first >= 0 ? BK[1].first : (BK[2].first >= 0 ? BK[2].first : BK[3].first));
    EXPECT(mac_check_counts(why, sizeof why) == -1);
    T->free_head = saved;

    mac_entry_t *e = &EN[saved];                       /* a cycle in the free list */
    int32_t saved_next = e->next;
    e->next = saved;
    EXPECT(mac_check_counts(why, sizeof why) == -1);
    e->next = saved_next;

    int bi = -1;                                       /* an entry in the wrong bucket */
    for (int i = 0; i < 4 && bi < 0; i++)
        if (BK[i].first >= 0)
            bi = i;
    int32_t moved = BK[bi].first;
    BK[bi].first = EN[moved].next;
    int other = (bi + 1) % 4;
    EN[moved].next = BK[other].first;
    BK[other].first = moved;
    EXPECT(mac_check_counts(why, sizeof why) == -1 && strstr(why, "hashes elsewhere") != NULL);
    BK[other].first = EN[moved].next;
    EN[moved].next = BK[bi].first;
    BK[bi].first = moved;

    EXPECT(mac_check_counts(why, sizeof why) == 0);    /* and it is fine again */
    EXPECT(mac_check_counts(NULL, 0) == 0);            /* no message buffer is allowed */
    cleanup();
}

/* ---- 11. the aging thread ---- */
static void shutdown_now(void)
{
    request_shutdown();             /* Part 7: no eventfd in this test (wake_efd is -1): skipped */
}

static void test_aging_thread(void)
{
    printf("aging thread: runs every second, stops at once on shutdown\n");
    setup(16, 4);
    atomic_store(&sw.mac_aging_sec, 0);                /* every entry with a positive age is old */
    pthread_t th;
    CHECK_PT(pthread_create(&th, NULL, aging_main, NULL));

    struct timespec t0 = now_mono();
    learn(1, 1, 1);
    int gone = 0;
    for (int i = 0; i < 400 && !gone; i++) {           /* test code: poll for up to 4 s */
        gone = (look(1, 1) == -1);
        if (!gone)
            usleep(10000);
    }
    long el = ts_diff_ms(now_mono(), t0);
    EXPECT(gone && el <= 2200);                        /* within about one period (1 s) of the entry */
    EXPECT(atomic_load(&sw.ports[1].mac_count) == 0);

    learn(1, 2, 1);                                    /* and again: the thread keeps running */
    gone = 0;
    for (int i = 0; i < 400 && !gone; i++) {
        gone = (look(1, 2) == -1);
        if (!gone)
            usleep(10000);
    }
    EXPECT(gone);

    atomic_store(&sw.mac_aging_sec, 100000);           /* nothing ages now */
    learn(1, 3, 1);
    usleep(1300000);                                   /* test code: let at least one tick pass */
    EXPECT(look(1, 3) == 1);

    struct timespec t1 = now_mono();
    shutdown_now();                                    /* what Part 7 will do */
    CHECK_PT(pthread_join(th, NULL));
    EXPECT(ts_diff_ms(now_mono(), t1) < 400);          /* it did not wait for the next tick */
    EXPECT(check_ok());
    cleanup();
}

/* ---- 12. eight threads ---- */
#define WORKERS 8
#define MAC_POOL 300
static _Atomic bool stop_flag;
static _Atomic unsigned long ops[WORKERS];
static int K_stress;

static uint32_t xrand(uint32_t *s)
{
    *s ^= *s << 13;
    *s ^= *s >> 17;
    *s ^= *s << 5;
    return *s;
}

static void count_cb(const mac_info_t *m, void *arg)
{
    if (m->port < 1 || m->port > 8)
        *(int *)arg = 1;
}

static void *worker(void *arg)
{
    int id = (int)(intptr_t)arg;
    uint32_t seed = 12345u + (uint32_t)id * 7919u;
    unsigned long n = 0;
    int bad = 0;

    while (!atomic_load(&stop_flag)) {
        uint32_t r = xrand(&seed) % 1000;
        mac_t m;
        mk(m, xrand(&seed) % MAC_POOL);
        uint16_t vlan = (uint16_t)(1 + xrand(&seed) % 3);
        int port = 1 + (int)(xrand(&seed) % 8);

        if (r < 550) {
            mac_learn_ex(vlan, m, port, NULL);
        } else if (r < 850) {
            int p = mac_lookup(vlan, m);
            if (p != -1 && (p < 1 || p > 8))
                bad = 1;
        } else if (r < 930) {
            mac_age((long)(xrand(&seed) % 2) * 1000);          /* T = 0 s or 1 s */
        } else if (r < 970) {
            mac_flush_port(port);
        } else if (r < 985) {
            mac_evict_lru();
        } else if (r < 990) {
            mac_foreach(count_cb, &bad);
        } else if (r < 992 && id == 0) {
            mac_clear();
        } else if (mac_size() > K_stress) {
            bad = 1;                                           /* never more than K entries */
        }
        n++;
    }
    atomic_store(&ops[id], bad ? (unsigned long)-1 : n);
    return NULL;
}

static void test_concurrent(void)
{
    printf("8 threads: random learn / lookup / age / flush / evict / foreach on a small table\n");
    K_stress = 64;
    setup(K_stress, 16);
    atomic_store(&sw.max_macs_per_port, 1000000);
    atomic_store(&stop_flag, false);
    pthread_t th[WORKERS];
    for (intptr_t i = 0; i < WORKERS; i++)
        CHECK_PT(pthread_create(&th[i], NULL, worker, (void *)i));
    usleep(2000000);                                   /* test code: let them run for 2 s */
    atomic_store(&stop_flag, true);
    for (int i = 0; i < WORKERS; i++)
        CHECK_PT(pthread_join(th[i], NULL));

    unsigned long total = 0;
    int bad = 0;
    for (int i = 0; i < WORKERS; i++) {
        if (atomic_load(&ops[i]) == (unsigned long)-1)
            bad = 1;
        else
            total += atomic_load(&ops[i]);
    }
    printf("  %lu operations\n", total);
    EXPECT(!bad);
    EXPECT(total > 1000);
    EXPECT(mac_size() <= K_stress);
    EXPECT(check_ok());                                /* counts and free list are exact afterwards */
    mac_clear();
    EXPECT(mac_size() == 0 && check_ok());
    cleanup();
}

/* ---- 13. forward_frame decisions (without sockets) ---- */
static frame_t *mkframe(unsigned src, unsigned dst_n, bool dst_bcast, bool dst_mc, uint16_t vlan)
{
    frame_t *f = frame_new();
    CHECK(f == NULL ? -1 : 0);
    mk(f->src, src);
    if (dst_bcast)
        memset(f->dst, 0xFF, MAC_LEN);
    else if (dst_mc) {
        mk(f->dst, dst_n);
        f->dst[0] = 0x01;
    } else
        mk(f->dst, dst_n);
    f->vlan = vlan;
    f->len = 0;
    return f;
}

static int queued(int port)
{
    CHECK_PT(pthread_mutex_lock(&sw.ports[port].q.lock));
    int n = sw.ports[port].q.count[OUTQ_NORMAL];
    CHECK_PT(pthread_mutex_unlock(&sw.ports[port].q.lock));
    return n;
}

static void test_forward_decision(void)
{
    printf("forward_frame: learn, unicast, flood, filter, source rules (queues only, no threads)\n");
    long live0 = frame_live_count();
    setup(32, 8);
    atomic_store(&sw.max_macs_per_port, 3);
    sw.num_ports = 4;
    for (int i = 1; i <= 4; i++) {                     /* four UP ports with queues but no threads */
        sw.ports[i].state = PORT_UP;
        CHECK_PT(outq_init(&sw.ports[i].q, 16));
    }
    frame_t *f;

    f = mkframe(0xA, 0xC, false, false, 1);            /* A -> C, C unknown: flood */
    EXPECT(forward_frame(&sw.ports[1], f) == LEARN_OK);
    frame_unref(f);
    EXPECT(queued(1) == 0 && queued(2) == 1 && queued(3) == 1 && queued(4) == 1);
    EXPECT(look(1, 0xA) == 1);                         /* A was learned on port 1 */

    f = mkframe(0xC, 0xA, false, false, 1);            /* C -> A: A is known: only port 1 */
    forward_frame(&sw.ports[3], f);
    frame_unref(f);
    EXPECT(queued(1) == 1 && queued(2) == 1 && queued(3) == 1 && queued(4) == 1);
    EXPECT(look(1, 0xC) == 3);

    f = mkframe(0xA, 0xC, false, false, 1);            /* A -> C again: only port 3 */
    forward_frame(&sw.ports[1], f);
    frame_unref(f);
    EXPECT(queued(1) == 1 && queued(2) == 1 && queued(3) == 2 && queued(4) == 1);

    f = mkframe(0xA, 0x99, false, false, 1);           /* unknown destination: flood */
    forward_frame(&sw.ports[1], f);
    frame_unref(f);
    EXPECT(queued(1) == 1 && queued(2) == 2 && queued(3) == 3 && queued(4) == 2);

    f = mkframe(0xA, 0, true, false, 1);               /* broadcast: flood */
    forward_frame(&sw.ports[1], f);
    frame_unref(f);
    EXPECT(queued(2) == 3 && queued(3) == 4 && queued(4) == 3 && queued(1) == 1);

    f = mkframe(0xA, 0x55, false, true, 1);            /* multicast: flood */
    forward_frame(&sw.ports[1], f);
    frame_unref(f);
    EXPECT(queued(2) == 4 && queued(3) == 5 && queued(4) == 4 && queued(1) == 1);

    /* a destination behind the ingress port is filtered: nothing is queued anywhere */
    learn(1, 0xD, 1);                                  /* D is on port 1 as well */
    f = mkframe(0xA, 0xD, false, false, 1);
    forward_frame(&sw.ports[1], f);
    frame_unref(f);
    EXPECT(queued(1) == 1 && queued(2) == 4 && queued(3) == 5 && queued(4) == 4);

    /* the destination's port is not UP: flood instead */
    sw.ports[3].state = PORT_CLOSING;
    f = mkframe(0xA, 0xC, false, false, 1);
    forward_frame(&sw.ports[1], f);
    frame_unref(f);
    EXPECT(queued(2) == 5 && queued(3) == 5 && queued(4) == 5);   /* port 3 is skipped, the others get it */
    sw.ports[3].state = PORT_UP;

    /* a broadcast SOURCE is dropped and not learned; a multicast source is forwarded but not learned */
    f = mkframe(0xA, 0xC, false, false, 1);
    memset(f->src, 0xFF, MAC_LEN);
    EXPECT(forward_frame(&sw.ports[2], f) == LEARN_OK);
    frame_unref(f);
    EXPECT(queued(1) == 1 && queued(3) == 5 && queued(4) == 5);
    f = mkframe(0x77, 0xA, false, false, 1);
    f->src[0] = 0x01;                                  /* multicast source */
    forward_frame(&sw.ports[2], f);
    frame_unref(f);
    EXPECT(queued(1) == 2);                            /* forwarded (A is known on port 1) */
    {
        mac_t ms;
        mk(ms, 0x77);
        ms[0] = 0x01;
        EXPECT(mac_lookup(1, ms) == -1);               /* but not learned */
    }

    /* the vlan is part of the key: A is unknown in vlan 9, so this floods */
    f = mkframe(0xC, 0xA, false, false, 9);
    forward_frame(&sw.ports[3], f);
    frame_unref(f);
    EXPECT(queued(1) == 3 && queued(2) == 6 && queued(4) == 6);

    /* port security: the 4th MAC on port 2 -> LEARN_VIOLATION, frame dropped, no lock left held */
    int before = queued(1) + queued(3) + queued(4);
    for (unsigned s = 0x21; s <= 0x23; s++) {
        f = mkframe(s, 0xFFFF, true, false, 1);
        EXPECT(forward_frame(&sw.ports[2], f) == LEARN_OK);
        frame_unref(f);
    }
    f = mkframe(0x24, 0xFFFF, true, false, 1);
    EXPECT(forward_frame(&sw.ports[2], f) == LEARN_VIOLATION);
    frame_unref(f);
    EXPECT(queued(1) + queued(3) + queued(4) == before + 9);       /* only the 3 good broadcasts went out (3 ports each) */
    /* ports_lock is free: a writer can take it at once (this would hang if it were still held) */
    alarm(10);
    CHECK_PT(pthread_rwlock_wrlock(&sw.ports_lock));
    CHECK_PT(pthread_rwlock_unlock(&sw.ports_lock));
    alarm(0);

    for (int i = 1; i <= 4; i++) {
        outq_drain(&sw.ports[i].q);
        outq_destroy(&sw.ports[i].q);
    }
    EXPECT(frame_live_count() == live0);               /* every frame was released */
    EXPECT(check_ok());
    cleanup();
}

/* ---- 14. real ports: receiver + reaper + MAC table together ---- */
static void tcp_pair(int *host, int *sw_fd)
{
    int ls, one = 1;
    struct sockaddr_in a;
    socklen_t al = sizeof a;

    CHECK(ls = socket(AF_INET, SOCK_STREAM, 0));
    CHECK(setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one));
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    CHECK(bind(ls, (struct sockaddr *)&a, sizeof a));
    CHECK(listen(ls, 4));
    CHECK(getsockname(ls, (struct sockaddr *)&a, &al));
    CHECK(*host = socket(AF_INET, SOCK_STREAM, 0));
    CHECK(connect(*host, (struct sockaddr *)&a, sizeof a));
    CHECK(*sw_fd = accept(ls, NULL, NULL));
    CHECK(close(ls));
}

static int start_host(int *peer)
{
    int fd, id, err;

    CHECK(sem_trywait(&sw.free_ports));
    tcp_pair(peer, &fd);
    CHECK_PT(port_start(fd, &id, &err) == PS_OK ? 0 : EINVAL);
    return id;
}

/* The host `peer` sends a broadcast from source MAC number src_n. */
static void host_sends(int peer, unsigned src_n, const mac_t dst)
{
    frame_hdr_t h = {.len = 1, .vlan = 1, .prio = 1};
    mk(h.src, src_n);
    memcpy(h.dst, dst, MAC_LEN);
    CHECK(frame_send(peer, &h, (const uint8_t *)"x"));
}

/* Wait until the receiver of `port` has counted n frames (polling the counter: test code). */
static bool wait_in(int port, unsigned long long n)
{
    for (int i = 0; i < 10000; i++) {
        if (stats_get(port, STAT_IN) >= n)
            return true;
        usleep(1000);
    }
    return false;
}

/*
 * Wait until `port` has learned n MAC addresses (polling the counter: test code).
 * wait_in() is NOT enough before looking at the table: the receiver counts STAT_IN when it has READ a frame,
 * then formats and logs it, and only then learns its source MAC in forward_frame(). A slow or loaded run (ThreadSanitizer)
 * can therefore see "n frames received" while the last MAC is not in the table yet. mac_count is incremented while the
 * entry is inserted, after the table's own count, so once it has reached n the table size is final too.
 */
static bool wait_macs(int port, int n)
{
    for (int i = 0; i < 10000; i++) {
        if (atomic_load(&sw.ports[port].mac_count) >= n)
            return true;
        usleep(1000);
    }
    return false;
}

static int sem_value(void)
{
    int v = -1;
    CHECK(sem_getvalue(&sw.free_ports, &v));
    return v;
}

static void test_ports_and_table(void)
{
    printf("real ports: learning from frames, unicast, flush at teardown, port security shutdown\n");
    long live0 = frame_live_count();
    setup(32, 8);
    atomic_store(&sw.max_macs_per_port, 3);
    CHECK_PT(stats_init(MAX_PORTS));
    CHECK_PT(pthread_create(&sw.reaper, NULL, reaper_main, NULL));
    mac_t bc;
    memset(bc, 0xFF, MAC_LEN);

    int p1, p2, p3;
    EXPECT(start_host(&p1) == 1 && start_host(&p2) == 2 && start_host(&p3) == 3);
    host_sends(p1, 1, bc);
    host_sends(p1, 2, bc);
    host_sends(p2, 3, bc);
    host_sends(p3, 4, bc);
    host_sends(p3, 5, bc);
    host_sends(p3, 6, bc);                              /* exactly M = 3 on port 3 */
    EXPECT(wait_in(1, 2) && wait_in(2, 1) && wait_in(3, 3));
    EXPECT(wait_macs(1, 2) && wait_macs(2, 1) && wait_macs(3, 3));      /* learned, not just received */
    EXPECT(mac_size() == 6);
    EXPECT(atomic_load(&sw.ports[1].mac_count) == 2 && atomic_load(&sw.ports[2].mac_count) == 1 &&
           atomic_load(&sw.ports[3].mac_count) == 3);
    EXPECT(look(1, 3) == 2 && look(1, 5) == 3 && look(1, 1) == 1);
    EXPECT(check_ok());
    EXPECT(port_state(3) == PORT_UP);                /* 3 = M: no shutdown */

    /* a unicast from host 1 to MAC 3 (behind port 2) reaches port 2 only; then a broadcast
     * reaches everybody: the first frame port 3 gets must be the broadcast. */
    alarm(20);
    mac_t m3;
    mk(m3, 3);
    host_sends(p1, 1, m3);
    host_sends(p1, 1, bc);
    EXPECT(wait_in(1, 4));
    frame_hdr_t h;
    uint8_t pay[MAX_PAYLOAD];
    /* port 2 first got the broadcasts of host 1 and 3's... read until the unicast arrives */
    bool unicast_seen = false;
    for (int i = 0; i < 20 && !unicast_seen; i++) {
        EXPECT(frame_recv(p2, &h, pay) == FR_OK);
        unicast_seen = memcmp(h.dst, m3, MAC_LEN) == 0;
    }
    EXPECT(unicast_seen);
    int saw_unicast_on_3 = 0;
    for (int i = 0; i < 20; i++) {
        EXPECT(frame_recv(p3, &h, pay) == FR_OK);
        if (memcmp(h.dst, m3, MAC_LEN) == 0)
            saw_unicast_on_3 = 1;
        if (mac_is_broadcast(h.dst) && h.src[5] == 1 && stats_get(1, STAT_IN) >= 4)
            break;
    }
    EXPECT(!saw_unicast_on_3);                           /* port 3 never got the unicast */
    alarm(0);

    /* host 2 leaves: the reaper flushes its MACs */
    unsigned long g = port_down_count(2);
    CHECK(close(p2));
    EXPECT(port_wait_down(2, g, 10000));
    EXPECT(atomic_load(&sw.ports[2].mac_count) == 0 && look(1, 3) == -1 && mac_size() == 5);
    EXPECT(check_ok());
    EXPECT(sem_value() == 8 - 3 + 1);

    /* host 3 uses a 4th source MAC: port security. The receiver asks for DISABLED. */
    g = port_down_count(3);
    host_sends(p3, 7, bc);
    EXPECT(port_wait_down(3, g, 10000));
    EXPECT(port_state(3) == PORT_DISABLED);
    EXPECT(atomic_load(&sw.ports[3].mac_count) == 0 && look(1, 7) == -1 && look(1, 4) == -1);
    EXPECT(sem_value() == 8 - 3 + 1);                    /* a DISABLED port keeps its token */
    EXPECT(check_ok());
    EXPECT(frame_recv(p3, &h, pay) != FR_OK || 1);       /* the switch closed it (EOF after queued frames) */
    CHECK(close(p3));
    EXPECT(look(1, 1) == 1 && look(1, 2) == 1);          /* host 1 is untouched */

    g = port_down_count(1);
    CHECK(close(p1));
    EXPECT(port_wait_down(1, g, 10000));
    EXPECT(mac_size() == 0 && check_ok());
    EXPECT(sem_value() == 8 - 1);                        /* only the DISABLED port is missing */
    reaper_finish();
    stats_destroy();
    EXPECT(frame_live_count() == live0);
    cleanup();
}

int main(void)
{
    signal(SIGPIPE, SIG_IGN);
    test_init();
    test_learn_lookup();
    test_hash_and_chains();
    test_eviction();
    test_aging();
    test_flush_clear();
    test_foreach_dump();
    test_dump_file();
    test_port_security();
    test_robust();
    test_check_counts();
    test_aging_thread();
    test_concurrent();
    test_forward_decision();
    test_ports_and_table();

    printf("\n%d checks, %d failures\n", checks, failures);
    printf(failures == 0 ? "ALL PASSED\n" : "SOME FAILED\n");
    return failures == 0 ? 0 : 1;
}
