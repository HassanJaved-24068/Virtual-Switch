/*
 * test_macshm.c - the MAC table in shared memory and its recovery after a crash (Part 20).
 *
 * A "switch" is a forked child: it attaches the table (mac_attach_shm), learns, and then dies like a crashed switch: _exit() while it holds a bucket lock (and the pool lock),
 * and optionally after it has damaged the table the way a crash in the middle of an update could (a free list that points into the chains, a wrong count, a cycle, an
 * out-of-range index, an entry in the wrong bucket, a port that does not exist). The parent attaches the same object and must recover: no deadlock on the dead
 * owner's locks (EOWNERDEAD -> pthread_mutex_consistent), the entry count and the free list rebuilt by a scan, every port's mac_count rebuilt, the damaged entries
 * dropped, and mac_check_counts() must be happy. Run normally, under ThreadSanitizer and under valgrind.
 */
#include "mactable.h"
#include "port.h"

#include <fcntl.h>
#include <signal.h>
#include <stdatomic.h>
#include <sys/mman.h>
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

#define K 16
#define NB 4
static char NAME[64];

static void fresh_switch(void)
{
    switch_init(8);
    atomic_store(&sw.max_macs_per_port, 1000000);
}

static void drop_switch(void)
{
    switch_destroy();
}

static void mac_of(mac_t m, int i)
{
    static const uint8_t base[MAC_LEN] = {0x02, 0, 0, 0, 0, 0};
    memcpy(m, base, MAC_LEN);
    m[5] = (uint8_t)i;
}

/* The mapping of the shared object, to look inside and to damage it. */
static void *map_it(size_t *len)
{
    int fd = shm_open(NAME, O_RDWR, 0);
    if (fd < 0)
        return NULL;
    *len = mac_region_size(K, NB);
    void *m = mmap(NULL, *len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    CHECK(close(fd));
    return m == MAP_FAILED ? NULL : m;
}

enum damage { NONE, LOCKED_ONLY, WRONG_COUNT_AND_FREELIST, CYCLE, BAD_INDEX, WRONG_BUCKET, BAD_PORT };

/* The child: a switch that learns 6 MACs on ports 1..3 and dies holding locks. Never returns. */
static void crashing_switch(enum damage d)
{
    void *r;
    size_t len;
    int rec;

    fresh_switch();
    if (mac_attach_shm(NAME, K, NB, &r, &len, &rec) != 0)
        _exit(10);
    for (int i = 0; i < 6; i++) {
        mac_t m;
        mac_of(m, i);
        if (mac_learn(1, m, 1 + i % 3) != LEARN_OK)
            _exit(11);
    }
    mactable_t *t = r;
    mac_bucket_t *bk = (mac_bucket_t *)(t + 1);
    mac_entry_t *en = (mac_entry_t *)(bk + NB);

    /* find a used entry and the bucket that holds it */
    int victim = -1, vb = -1;
    for (int b = 0; b < NB && victim < 0; b++)
        if (bk[b].first >= 0) {
            victim = bk[b].first;
            vb = b;
        }
    if (victim < 0)
        _exit(12);

    switch (d) {
    case NONE:
        break;
    case LOCKED_ONLY:
        break;
    case WRONG_COUNT_AND_FREELIST:
        t->count = 1234;
        t->free_head = victim;                           /* the free list now runs into a chained entry */
        break;
    case CYCLE:
        en[victim].next = victim;
        break;
    case BAD_INDEX:
        en[victim].next = K + 100;
        break;
    case WRONG_BUCKET:
        en[victim].vlan ^= 0x0005;                       /* it no longer hashes to its bucket (any other bucket, or the same one by chance: pick a value that moves it) */
        for (int v = 2; v < 4000 && en[victim].vlan == 1; v++)
            en[victim].vlan = (uint16_t)v;
        break;
    case BAD_PORT:
        en[victim].port = 60;                            /* a port this switch does not have (8) */
        break;
    }
    /* die holding the lock of that bucket and the pool lock, like a thread killed in the middle of an update */
    int rc = pthread_mutex_lock(&bk[vb].lock);
    if (rc != 0)
        _exit(13);
    rc = pthread_mutex_lock(&t->pool_lock);
    if (rc != 0)
        _exit(14);
    _exit(0);
}

static void run_child(enum damage d)
{
    pid_t pid = fork();
    CHECK(pid);
    if (pid == 0)
        crashing_switch(d);
    int st = 0;
    CHECK(waitpid(pid, &st, 0));
    EXPECT(WIFEXITED(st) && WEXITSTATUS(st) == 0);
}

static int entries_on(int port)
{
    return atomic_load(&sw.ports[port].mac_count);
}

static void test_scenario(const char *what, enum damage d, int expect_kept, int expect_dropped_somewhere)
{
    printf("%s\n", what);
    EXPECT(shm_unlink(NAME) == 0 || errno == ENOENT);
    run_child(d);

    fresh_switch();                                      /* the new switch: ports start with mac_count 0 */
    alarm(20);                                           /* a lock held by a dead owner must not hang us */
    void *r = NULL;
    size_t len = 0;
    int rec = -2;
    int rc = mac_attach_shm(NAME, K, NB, &r, &len, &rec);
    alarm(0);
    EXPECT(rc == 0);
    if (rc != 0)
        return;
    if (expect_dropped_somewhere)
        EXPECT(rec >= 0 && rec < 6);
    else
        EXPECT(rec == expect_kept);
    if (!expect_dropped_somewhere)
        EXPECT(mac_size() == 6);
    char why[200] = "";
    int cc = mac_check_counts(why, sizeof why);
    if (cc != 0)
        printf("    mac_check_counts: %s\n", why);
    EXPECT(cc == 0);
    EXPECT(mac_size() == rec);
    int total = 0;
    for (int p = 0; p <= 8; p++)
        total += entries_on(p);
    EXPECT(total == rec);                                /* every port's mac_count was rebuilt from the entries */

    /* the table works: lookups, new entries, removal */
    int found = 0;
    for (int i = 0; i < 6; i++) {
        mac_t m;
        mac_of(m, i);
        if (mac_lookup(1, m) >= 0)
            found++;
    }
    EXPECT(found == rec);
    for (int i = 10; i < 16; i++) {                      /* the pool must have its free entries back: 10 are free, 6 more fit */
        mac_t m;
        mac_of(m, i);
        EXPECT(mac_learn(1, m, 2) == LEARN_OK);
    }
    EXPECT(mac_check_counts(why, sizeof why) == 0);
    EXPECT(mac_flush_port(2) >= 6);
    EXPECT(mac_check_counts(why, sizeof why) == 0);
    mac_detach();
    EXPECT(munmap(r, len) == 0);
    drop_switch();
}

static void test_fresh_and_reuse(void)
{
    printf("a table is created, reused by the next attach, and recreated when its size or header does not fit\n");
    EXPECT(shm_unlink(NAME) == 0 || errno == ENOENT);
    fresh_switch();
    void *r;
    size_t len;
    int rec = -2;
    EXPECT(mac_attach_shm(NAME, K, NB, &r, &len, &rec) == 0);
    EXPECT(rec == -1);                                   /* nothing was there: a fresh table */
    mac_t m;
    mac_of(m, 1);
    EXPECT(mac_learn(7, m, 3) == LEARN_OK);
    mac_detach();                                        /* a clean exit: no mutex is destroyed */
    EXPECT(munmap(r, len) == 0);
    drop_switch();

    fresh_switch();
    EXPECT(mac_attach_shm(NAME, K, NB, &r, &len, &rec) == 0);
    EXPECT(rec == 1);                                    /* the entry was found again */
    EXPECT(mac_lookup(7, m) == 3);
    EXPECT(entries_on(3) == 1);
    mac_detach();
    EXPECT(munmap(r, len) == 0);
    drop_switch();

    fresh_switch();                                      /* another size: the old table is not trusted, the object is resized */
    EXPECT(mac_attach_shm(NAME, K * 2, NB, &r, &len, &rec) == 0);
    EXPECT(rec == -1);
    EXPECT(mac_size() == 0);
    mac_detach();
    EXPECT(munmap(r, len) == 0);
    drop_switch();

    fresh_switch();                                      /* the right size but a wrong magic (a table that was being built when the process died) */
    EXPECT(mac_attach_shm(NAME, K, NB, &r, &len, &rec) == 0);
    mac_detach();
    EXPECT(munmap(r, len) == 0);
    size_t l2;
    mactable_t *t = map_it(&l2);
    EXPECT(t != NULL);
    t->magic = 0;
    EXPECT(munmap(t, l2) == 0);
    EXPECT(mac_attach_shm(NAME, K, NB, &r, &len, &rec) == 0);
    EXPECT(rec == -1);
    mac_detach();
    EXPECT(munmap(r, len) == 0);
    drop_switch();

    EXPECT(mac_attach_shm("no-slash", K, NB, &r, &len, &rec) != 0);   /* bad names and sizes are reported, nothing is left mapped */
    EXPECT(mac_attach_shm(NAME, 0, NB, &r, &len, &rec) == EINVAL);
    EXPECT(shm_unlink(NAME) == 0);
}

int main(void)
{
    snprintf(NAME, sizeof NAME, "/vswitch_test_macshm_%d", (int)getpid());
    test_fresh_and_reuse();
    test_scenario("a switch dies holding a bucket lock and the pool lock (EOWNERDEAD): all 6 entries are recovered", LOCKED_ONLY, 6, 0);
    test_scenario("... with a wrong entry count and a free list that runs into the chains: count and free list are rebuilt", WRONG_COUNT_AND_FREELIST, 6, 0);
    test_scenario("... with a cycle in a chain: the chain is cut, the table is consistent", CYCLE, 0, 1);
    test_scenario("... with a chain index out of range: the chain is cut", BAD_INDEX, 0, 1);
    test_scenario("... with an entry in the wrong bucket: it is dropped", WRONG_BUCKET, 0, 1);
    test_scenario("... with an entry of a port that does not exist: it is dropped", BAD_PORT, 0, 1);
    EXPECT(shm_unlink(NAME) == 0 || errno == ENOENT);
    printf("%d checks, %d failures\n", checks, failures);
    printf(failures == 0 ? "ALL PASSED\n" : "FAILED\n");
    return failures == 0 ? 0 : 1;
}
