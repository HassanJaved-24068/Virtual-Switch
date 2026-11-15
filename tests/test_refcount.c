/*
 * test_refcount.c - frame_ref / frame_unref with 8 threads (Part 4).
 *
 * Meant to be run normally, under valgrind and under ThreadSanitizer:
 * a use-after-free, a double free or a data race on the count shows up there.
 * frame_live_count() must be 0 at the end (every frame freed exactly once).
 */
#include "frame.h"
#include "queue.h"

#include <unistd.h>

#define THREADS 8

static int checks = 0, failures = 0;

#define EXPECT(cond)                                                       \
    do {                                                                   \
        checks++;                                                          \
        if (!(cond)) {                                                     \
            failures++;                                                    \
            printf("  FAIL line %d: %s\n", __LINE__, #cond);               \
        }                                                                  \
    } while (0)

static int count_of(frame_t *f)
{
    CHECK_PT(pthread_mutex_lock(&f->ref_lock));
    int c = f->refcount;
    CHECK_PT(pthread_mutex_unlock(&f->ref_lock));
    return c;
}

/* ---- 1. basics ---- */
static void test_basic(void)
{
    printf("frame_new / ref / unref: single thread\n");
    long base = frame_live_count();
    frame_t *f = frame_new();
    EXPECT(f != NULL);
    EXPECT(count_of(f) == 1);                       /* the creator's reference */
    EXPECT(frame_live_count() == base + 1);
    frame_ref(f);
    frame_ref(f);
    EXPECT(count_of(f) == 3);
    frame_unref(f);
    EXPECT(count_of(f) == 2);
    EXPECT(frame_live_count() == base + 1);         /* still alive */
    frame_unref(f);
    EXPECT(frame_live_count() == base + 1);
    frame_unref(f);                                 /* last one: freed */
    EXPECT(frame_live_count() == base);
}

/* ---- 2. 8 threads each taking and dropping references to ONE frame ---- */
static frame_t *shared;
#define ROUNDS 50000

static void *ref_worker(void *arg)
{
    (void)arg;
    for (int i = 0; i < ROUNDS; i++) {
        frame_ref(shared);
        frame_ref(shared);
        frame_unref(shared);
        frame_unref(shared);
    }
    return NULL;
}

static void test_threads_ref_unref(void)
{
    printf("8 threads: ref/unref on one shared frame\n");
    long base = frame_live_count();
    shared = frame_new();
    EXPECT(shared != NULL);
    pthread_t th[THREADS];
    for (int i = 0; i < THREADS; i++)
        CHECK_PT(pthread_create(&th[i], NULL, ref_worker, NULL));
    for (int i = 0; i < THREADS; i++)
        CHECK_PT(pthread_join(th[i], NULL));
    EXPECT(count_of(shared) == 1);                  /* every ref was matched by an unref */
    EXPECT(frame_live_count() == base + 1);
    frame_unref(shared);
    EXPECT(frame_live_count() == base);
}

/* ---- 3. last-one-frees: 8 threads each hold a reference and drop it ---- */
#define FRAMES 20000
static frame_t *volatile slot[FRAMES];

static void *drop_worker(void *arg)
{
    (void)arg;
    for (int i = 0; i < FRAMES; i++)
        frame_unref(slot[i]);
    return NULL;
}

static void test_last_one_frees(void)
{
    printf("8 threads drop the last references of %d frames (who frees is a race)\n", FRAMES);
    long base = frame_live_count();
    for (int i = 0; i < FRAMES; i++) {
        slot[i] = frame_new();
        CHECK(slot[i] == NULL ? -1 : 0);
        for (int t = 1; t < THREADS; t++)
            frame_ref(slot[i]);                     /* count = 8: one per thread */
    }
    EXPECT(frame_live_count() == base + FRAMES);
    pthread_t th[THREADS];
    for (int i = 0; i < THREADS; i++)
        CHECK_PT(pthread_create(&th[i], NULL, drop_worker, NULL));
    for (int i = 0; i < THREADS; i++)
        CHECK_PT(pthread_join(th[i], NULL));
    EXPECT(frame_live_count() == base);             /* each freed exactly once, no leak */
}

/* ---- 4. the real pattern: 1 receiver floods 8 queues, 8 senders consume ---- */
#define QUEUES 8
#define SENT 30000

static outq_t qs[QUEUES];
static unsigned long consumed[QUEUES];
static unsigned long accepted[QUEUES], dropped[QUEUES];

static void *consumer(void *arg)
{
    int i = (int)(intptr_t)arg;
    frame_t *f;
    while (outq_pop(&qs[i], &f)) {
        if (f->len != (uint16_t)(f->vlan + 1) || f->wire[HDR_LEN - 1] != f->prio)
            consumed[i] = (unsigned long)-1000000;      /* contents damaged */
        consumed[i]++;
        frame_unref(f);
    }
    return NULL;
}

static void test_flood_pattern(void)
{
    printf("1 receiver floods %d queues of 8 slots, %d frames, 8 consumers\n", QUEUES, SENT);
    long base = frame_live_count();
    pthread_t th[QUEUES];
    for (int i = 0; i < QUEUES; i++) {
        EXPECT(outq_init(&qs[i], 8) == 0);
        CHECK_PT(pthread_create(&th[i], NULL, consumer, (void *)(intptr_t)i));
    }
    for (int n = 0; n < SENT; n++) {
        frame_t *f = frame_new();
        CHECK(f == NULL ? -1 : 0);
        f->vlan = (uint16_t)n;
        f->len = (uint16_t)(n + 1);
        f->prio = (uint8_t)n;
        f->wire[HDR_LEN - 1] = (uint8_t)n;
        for (int i = 0; i < QUEUES; i++) {
            frame_ref(f);                           /* before the push */
            if (outq_try_push(&qs[i], f))
                accepted[i]++;
            else {
                frame_unref(f);                     /* push failed: give it back at once */
                dropped[i]++;
            }
        }
        frame_unref(f);                             /* the receiver's own reference */
    }
    /* wait until the queues are empty, then stop the consumers */
    for (int i = 0; i < QUEUES; i++) {
        for (;;) {
            CHECK_PT(pthread_mutex_lock(&qs[i].lock));
            int c = qs[i].count[OUTQ_NORMAL];
            CHECK_PT(pthread_mutex_unlock(&qs[i].lock));
            if (c == 0)
                break;
            usleep(1000);                           /* test code only */
        }
        outq_close(&qs[i]);
    }
    for (int i = 0; i < QUEUES; i++)
        CHECK_PT(pthread_join(th[i], NULL));
    int ok = 1;
    for (int i = 0; i < QUEUES; i++) {
        outq_drain(&qs[i]);
        outq_destroy(&qs[i]);
        if (accepted[i] + dropped[i] != SENT)
            ok = 0;
        /* a frame popped right when close() came may be left in the queue and
         * drained instead of consumed, so consumed <= accepted */
        if (consumed[i] > accepted[i])
            ok = 0;
    }
    EXPECT(ok);
    EXPECT(frame_live_count() == base);             /* nothing leaked, nothing freed twice */
}

int main(void)
{
    test_basic();
    test_threads_ref_unref();
    test_last_one_frees();
    test_flood_pattern();
    printf("\n%d checks, %d failures\n", checks, failures);
    printf(failures == 0 ? "ALL PASSED\n" : "SOME FAILED\n");
    return failures == 0 ? 0 : 1;
}
