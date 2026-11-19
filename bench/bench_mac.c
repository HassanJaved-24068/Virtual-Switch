/*
 * bench_mac.c - the in-process micro-benchmark of the MAC table locking (Part 21).
 *
 *   ./bench/bench_mac_<variant> <threads> <seconds> [seed]        variant = global | striped | rwlock
 *
 * It links mactable.c directly (built with -DMAC_LOCK=GLOBAL | STRIPED | RWLOCK) and takes the network, the sockets and the logging out of the picture, so that what is
 * measured is the lock: T threads share ONE table (the defaults of the switch: 1024 entries, 256 buckets) that holds NKEYS MACs, and for <seconds> each thread does a
 * 90 % / 10 % mix of mac_lookup and mac_learn_ex on random keys. A learn of a key that is already there only refreshes it (the key always goes to the same port, so there
 * are no "moved" log lines). The threads start together; the result is the total number of operations per second.
 *
 * The random keys come from xorshift64 generators with fixed seeds (the optional seed, default 12345, plus the thread number): the same arguments give the same sequence.
 *
 * Output, one line:  bench_mac variant=V threads=T seconds=S ops=N ops_per_sec=F
 */
#include "common.h"
#include "mactable.h"
#include "port.h"

#include <stdatomic.h>

#ifndef BENCH_VARIANT
#define BENCH_VARIANT "striped"
#endif

#define NKEYS 512                  /* distinct MACs, all in VLAN 1: fits in the table, so a learn never evicts */
#define LEARN_PERCENT 10
#define NPORTS 8
#define MAX_THREADS 256

static pthread_barrier_t start_barrier;
static _Atomic bool stop_flag;

typedef struct {
    int idx;
    uint64_t rng;
    unsigned long long ops;
    unsigned long long bad;        /* answers that were wrong (a lookup that found another port than the one the key was learned on) */
    pthread_t th;
} worker_t;

static worker_t workers[MAX_THREADS];

static uint64_t xorshift(uint64_t *s)
{
    uint64_t x = *s;

    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    *s = x;
    return x;
}

static void key_mac(mac_t m, unsigned k)
{
    m[0] = 0x02;
    m[1] = 0xbe;
    m[2] = 0;
    m[3] = 0;
    m[4] = (uint8_t)(k >> 8);
    m[5] = (uint8_t)(k & 0xff);
}

static int key_port(unsigned k)
{
    return 1 + (int)(k % NPORTS);
}

static void *worker_main(void *arg)
{
    worker_t *w = arg;
    unsigned long long ops = 0, bad = 0;

    int rc = pthread_barrier_wait(&start_barrier);
    if (rc != 0 && rc != PTHREAD_BARRIER_SERIAL_THREAD)
        return NULL;
    while (!atomic_load_explicit(&stop_flag, memory_order_relaxed)) {
        for (int i = 0; i < 256; i++) {                  /* the flag is looked at every 256 operations */
            const uint64_t r = xorshift(&w->rng);
            const unsigned k = (unsigned)((r >> 8) % NKEYS);
            mac_t m;

            key_mac(m, k);
            if ((r % 100) < LEARN_PERCENT) {
                struct mac_move mv;

                if (mac_learn_ex(1, m, key_port(k), &mv) == LEARN_VIOLATION)
                    bad++;
            } else {
                if (mac_lookup(1, m) != key_port(k))
                    bad++;
            }
            ops++;
        }
    }
    w->ops = ops;
    w->bad = bad;
    return NULL;
}

int main(int argc, char **argv)
{
    if (argc != 3 && argc != 4) {
        fprintf(stderr, "usage: %s <threads 1-%d> <seconds> [seed]\n", argv[0], MAX_THREADS);
        return EXIT_FAILURE;
    }
    char *end;
    long t = strtol(argv[1], &end, 10);
    if (*end != '\0' || t < 1 || t > MAX_THREADS)
        return fprintf(stderr, "bench_mac: threads must be 1-%d\n", MAX_THREADS), EXIT_FAILURE;
    double seconds = strtod(argv[2], &end);
    if (*end != '\0' || seconds <= 0)
        return fprintf(stderr, "bench_mac: bad seconds\n"), EXIT_FAILURE;
    unsigned long long seed = 12345;
    if (argc == 4) {
        seed = strtoull(argv[3], &end, 10);
        if (*end != '\0')
            return fprintf(stderr, "bench_mac: bad seed\n"), EXIT_FAILURE;
    }

    switch_init(NPORTS);                                  /* the port table the MAC table reports to (mac_count) */
    atomic_store(&sw.max_macs_per_port, 1 << 30);         /* port security is not what is measured */
    const size_t size = mac_region_size(DEFAULT_MAC_TABLE_SIZE, DEFAULT_MAC_BUCKETS);
    void *region = malloc(size);
    if (region == NULL)
        return fprintf(stderr, "bench_mac: out of memory\n"), EXIT_FAILURE;
    CHECK_PT(mac_init(region, DEFAULT_MAC_TABLE_SIZE, DEFAULT_MAC_BUCKETS));
    for (unsigned k = 0; k < NKEYS; k++) {                /* every key is known from the start: the lookups find something */
        mac_t m;

        key_mac(m, k);
        if (mac_learn(1, m, key_port(k)) != LEARN_OK)
            return fprintf(stderr, "bench_mac: cannot fill the table\n"), EXIT_FAILURE;
    }

    CHECK_PT(pthread_barrier_init(&start_barrier, NULL, (unsigned)t + 1));
    for (int i = 0; i < t; i++) {
        workers[i].idx = i;
        workers[i].rng = (seed + 1) * 0x9E3779B97F4A7C15ull + (uint64_t)i * 0xD1B54A32D192ED03ull;
        if (workers[i].rng == 0)
            workers[i].rng = 1;
        CHECK_PT(pthread_create(&workers[i].th, NULL, worker_main, &workers[i]));
    }
    int rc = pthread_barrier_wait(&start_barrier);
    if (rc != 0 && rc != PTHREAD_BARRIER_SERIAL_THREAD)
        return fprintf(stderr, "bench_mac: barrier failed\n"), EXIT_FAILURE;
    const struct timespec t0 = now_mono();
    const struct timespec deadline = ts_add_ms(t0, (long)(seconds * 1000.0));
    while (ts_diff_ms(deadline, now_mono()) > 0) {
        struct timespec nap = {0, 2000000};
        nanosleep(&nap, NULL);
    }
    atomic_store(&stop_flag, true);
    const struct timespec t1 = now_mono();
    unsigned long long ops = 0, bad = 0;
    for (int i = 0; i < t; i++) {
        CHECK_PT(pthread_join(workers[i].th, NULL));
        ops += workers[i].ops;
        bad += workers[i].bad;
    }
    const double elapsed = (double)ts_diff_ms(t1, t0) / 1000.0;

    char why[200] = "";
    if (mac_check_counts(why, sizeof why) != 0 || bad != 0) {      /* the table must still be consistent and every answer right */
        fprintf(stderr, "bench_mac: the table is damaged or an answer was wrong: %s (%llu wrong answers)\n", why, bad);
        return EXIT_FAILURE;
    }
    printf("bench_mac variant=%s threads=%ld seconds=%.2f ops=%llu ops_per_sec=%.0f\n", BENCH_VARIANT, t, elapsed, ops, elapsed > 0 ? (double)ops / elapsed : 0.0);
    mac_destroy();
    free(region);
    switch_destroy();
    return EXIT_SUCCESS;
}
