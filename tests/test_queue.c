/*
 * test_queue.c - unit tests for queue.c and stats.c (Part 4).
 */
#include "queue.h"
#include "stats.h"

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

static frame_t *mk(uint16_t tag)
{
    frame_t *f = frame_new();
    CHECK(f == NULL ? -1 : 0);
    f->len = tag;
    return f;
}

static int refs(frame_t *f)
{
    CHECK_PT(pthread_mutex_lock(&f->ref_lock));
    int c = f->refcount;
    CHECK_PT(pthread_mutex_unlock(&f->ref_lock));
    return c;
}

/* ---- queue: single thread ---- */

static void test_fifo_and_full(void)
{
    printf("queue: FIFO order, full queue, wrap-around\n");
    long base = frame_live_count();
    outq_t q;
    EXPECT(outq_init(&q, 4) == 0);

    frame_t *f[10], *out;
    for (int i = 0; i < 10; i++)
        f[i] = mk((uint16_t)i);

    for (int i = 0; i < 4; i++)
        EXPECT(outq_try_push(&q, f[i]));
    EXPECT(!outq_try_push(&q, f[4]));                 /* full: false at once, no waiting */
    EXPECT(!outq_try_push(&q, f[4]));
    EXPECT(q.count[OUTQ_NORMAL] == 4);

    EXPECT(outq_pop(&q, &out) && out == f[0]);
    EXPECT(outq_try_push(&q, f[4]));                  /* one slot free again (tail wraps) */
    EXPECT(!outq_try_push(&q, f[5]));
    for (int i = 1; i <= 4; i++)
        EXPECT(outq_pop(&q, &out) && out == f[i]);    /* oldest first */
    EXPECT(q.count[OUTQ_NORMAL] == 0);

    /* many wrap-arounds */
    int ok = 1;
    for (int round = 0; round < 1000; round++) {
        frame_t *a = mk((uint16_t)round);
        if (!outq_try_push(&q, a) || !outq_pop(&q, &out) || out != a)
            ok = 0;
        frame_unref(a);
    }
    EXPECT(ok);

    /* the queue only stores the pointer: ownership of the reference moves to it */
    frame_ref(f[5]);                                  /* the sender's reference */
    EXPECT(outq_try_push(&q, f[5]));
    EXPECT(refs(f[5]) == 2);
    EXPECT(outq_pop(&q, &out) && out == f[5]);
    frame_unref(out);                                 /* the reference that was in the queue */
    EXPECT(refs(f[5]) == 1);

    for (int i = 0; i < 10; i++)
        frame_unref(f[i]);
    outq_destroy(&q);
    EXPECT(frame_live_count() == base);
}

static void test_init_errors(void)
{
    printf("queue: init errors and capacity 1\n");
    outq_t q;
    EXPECT(outq_init(&q, 0) == EINVAL);
    EXPECT(outq_init(&q, -5) == EINVAL);
    EXPECT(outq_init(&q, 1) == 0);
    frame_t *a = mk(1), *b = mk(2), *out;
    EXPECT(outq_try_push(&q, a));
    EXPECT(!outq_try_push(&q, b));
    EXPECT(outq_pop(&q, &out) && out == a);
    EXPECT(outq_try_push(&q, b));
    EXPECT(outq_pop(&q, &out) && out == b);
    frame_unref(a);
    frame_unref(b);
    outq_destroy(&q);
}

static void test_close_and_drain(void)
{
    printf("queue: close and drain\n");
    long base = frame_live_count();
    outq_t q;
    EXPECT(outq_init(&q, 8) == 0);
    frame_t *a = mk(1), *b = mk(2), *c = mk(3), *out;

    /* the queue holds the only reference of a, b: after drain they are freed */
    EXPECT(outq_try_push(&q, a));
    EXPECT(outq_try_push(&q, b));
    frame_ref(c);                                      /* c is also held by "us" */
    EXPECT(outq_try_push(&q, c));
    EXPECT(refs(c) == 2);
    EXPECT(frame_live_count() == base + 3);

    outq_close(&q);
    frame_t *late = mk(9);
    EXPECT(!outq_try_push(&q, late));                  /* a closing queue accepts nothing */
    frame_unref(late);                                 /* so the caller still owns it */
    EXPECT(!outq_pop(&q, &out));                       /* closing: pop returns CLOSED at once */
    EXPECT(q.count[OUTQ_NORMAL] == 3);                 /* frames are still there until drain */

    outq_drain(&q);
    EXPECT(q.count[OUTQ_NORMAL] == 0);
    EXPECT(refs(c) == 1);                              /* drain dropped the queue's reference only */
    EXPECT(frame_live_count() == base + 1);            /* a and b were freed; c remains */
    frame_unref(c);
    EXPECT(frame_live_count() == base);
    outq_drain(&q);                                    /* draining an empty queue is fine */
    outq_destroy(&q);
}

/* ---- queue: threads ---- */

static outq_t shared_q;
static frame_t *popped;
static int popped_ok = -1;

static void *popper(void *arg)
{
    (void)arg;
    popped_ok = outq_pop(&shared_q, &popped) ? 1 : 0;
    return NULL;
}

static void test_blocking_pop(void)
{
    printf("queue: pop sleeps until a frame arrives / until close\n");
    EXPECT(outq_init(&shared_q, 4) == 0);
    pthread_t th;
    popped = NULL;
    popped_ok = -1;
    CHECK_PT(pthread_create(&th, NULL, popper, NULL));
    usleep(150000);                                    /* test code: give it time to go to sleep */
    EXPECT(popped_ok == -1);                           /* still waiting */
    frame_t *f = mk(7);
    EXPECT(outq_try_push(&shared_q, f));
    CHECK_PT(pthread_join(th, NULL));
    EXPECT(popped_ok == 1 && popped == f);
    frame_unref(f);

    /* a sleeping pop is woken by close */
    popped_ok = -1;
    CHECK_PT(pthread_create(&th, NULL, popper, NULL));
    usleep(150000);
    EXPECT(popped_ok == -1);
    outq_close(&shared_q);
    CHECK_PT(pthread_join(th, NULL));
    EXPECT(popped_ok == 0);
    outq_destroy(&shared_q);
}

#define PRODUCERS 4
#define PER_PRODUCER 20000
static outq_t mq;
static unsigned long pushed[PRODUCERS], full[PRODUCERS];
static unsigned long got;

static void *producer(void *arg)
{
    int id = (int)(intptr_t)arg;
    for (int i = 0; i < PER_PRODUCER; i++) {
        frame_t *f = mk((uint16_t)id);
        if (outq_try_push(&mq, f))
            pushed[id]++;
        else {
            frame_unref(f);
            full[id]++;
        }
    }
    return NULL;
}

static void *drainer(void *arg)
{
    (void)arg;
    frame_t *f;
    while (outq_pop(&mq, &f)) {
        got++;
        frame_unref(f);
    }
    return NULL;
}

static void test_many_threads(void)
{
    printf("queue: 4 producers, 1 consumer, small queue\n");
    long base = frame_live_count();
    EXPECT(outq_init(&mq, 16) == 0);
    pthread_t pr[PRODUCERS], co;
    CHECK_PT(pthread_create(&co, NULL, drainer, NULL));
    for (int i = 0; i < PRODUCERS; i++)
        CHECK_PT(pthread_create(&pr[i], NULL, producer, (void *)(intptr_t)i));
    for (int i = 0; i < PRODUCERS; i++)
        CHECK_PT(pthread_join(pr[i], NULL));
    for (;;) {                                         /* wait for the consumer to catch up */
        CHECK_PT(pthread_mutex_lock(&mq.lock));
        int c = mq.count[OUTQ_NORMAL];
        CHECK_PT(pthread_mutex_unlock(&mq.lock));
        if (c == 0)
            break;
        usleep(1000);
    }
    outq_close(&mq);
    CHECK_PT(pthread_join(co, NULL));
    outq_drain(&mq);
    outq_destroy(&mq);

    unsigned long total_pushed = 0, total_full = 0;
    for (int i = 0; i < PRODUCERS; i++) {
        total_pushed += pushed[i];
        total_full += full[i];
    }
    EXPECT(total_pushed + total_full == (unsigned long)PRODUCERS * PER_PRODUCER);
    EXPECT(got <= total_pushed);
    EXPECT(total_pushed > 0);
    EXPECT(frame_live_count() == base);
}

/* ---- stats ---- */

static void *stat_worker(void *arg)
{
    int port = (int)(intptr_t)arg;
    for (int i = 0; i < 10000; i++) {
        stats_inc(port, STAT_IN);
        stats_inc(port, STAT_OUT);
        stats_inc(3, STAT_DROP);                       /* all threads on the same counter */
    }
    return NULL;
}

static void test_stats(void)
{
    printf("stats: counters per port and kind\n");
    EXPECT(stats_inc(1, STAT_IN) == 0);                /* not initialised: ignored */
    EXPECT(stats_init(-1) == EINVAL);
    EXPECT(stats_init(8) == 0);
    EXPECT(stats_get(1, STAT_IN) == 0);
    EXPECT(stats_inc(1, STAT_IN) == 1);                /* returns the new value */
    EXPECT(stats_inc(1, STAT_IN) == 2);
    EXPECT(stats_inc(1, STAT_OUT) == 1);
    EXPECT(stats_inc(2, STAT_DROP) == 1);
    EXPECT(stats_inc(8, STAT_MIRROR) == 1);            /* highest valid port */
    EXPECT(stats_get(1, STAT_IN) == 2 && stats_get(1, STAT_OUT) == 1 && stats_get(1, STAT_DROP) == 0);
    EXPECT(stats_get(2, STAT_DROP) == 1 && stats_get(2, STAT_IN) == 0);
    EXPECT(stats_inc(9, STAT_IN) == 0);                /* port out of range */
    EXPECT(stats_inc(-1, STAT_IN) == 0);
    EXPECT(stats_inc(1, STAT_KINDS) == 0);             /* kind out of range */
    EXPECT(stats_get(99, STAT_IN) == 0);

    pthread_t th[8];
    for (intptr_t i = 0; i < 8; i++)
        CHECK_PT(pthread_create(&th[i], NULL, stat_worker, (void *)(i + 1)));
    for (int i = 0; i < 8; i++)
        CHECK_PT(pthread_join(th[i], NULL));
    int ok = 1;
    for (int p = 1; p <= 8; p++) {
        unsigned long long in = stats_get(p, STAT_IN), outc = stats_get(p, STAT_OUT);
        if (p == 1 ? (in != 10002 || outc != 10001) : (in != 10000 || outc != 10000))
            ok = 0;
    }
    EXPECT(ok);
    EXPECT(stats_get(3, STAT_DROP) == 80000);          /* 8 threads x 10000, no lost updates */
    stats_destroy();
    EXPECT(stats_get(1, STAT_IN) == 0);
}

int main(void)
{
    test_fifo_and_full();
    test_init_errors();
    test_close_and_drain();
    test_blocking_pop();
    test_many_threads();
    test_stats();
    printf("\n%d checks, %d failures\n", checks, failures);
    printf(failures == 0 ? "ALL PASSED\n" : "SOME FAILED\n");
    return failures == 0 ? 0 : 1;
}
