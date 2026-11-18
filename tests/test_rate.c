/*
 * test_rate.c - unit tests of the token bucket of the output queue (Part 12). Also built with
 * ThreadSanitizer and run under valgrind.
 *
 * The timer thread of the switch is replaced here by direct calls of outq_refill(), so every test is
 * deterministic: the amount of "time" that passes is chosen by the test, not measured. The only waiting is
 * for a negative fact ("the sender is still blocked after 300 ms"), which cannot make a correct queue fail.
 */
#include "queue.h"

#include <limits.h>
#include <pthread.h>
#include <semaphore.h>
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

static const int W531[OUTQ_CLASSES] = {5, 3, 1};

static frame_t *mkf(int prio, uint16_t tag)
{
    frame_t *f = frame_new();

    CHECK(f == NULL ? -1 : 0);
    f->prio = (uint8_t)prio;
    f->len = tag;
    return f;
}

static void sleep_ms(long ms)
{
    struct timespec ts = {ms / 1000, (ms % 1000) * 1000000L};

    nanosleep(&ts, NULL);
}

static long tokens_of(outq_t *q)
{
    CHECK_PT(pthread_mutex_lock(&q->lock));
    long t = q->tokens_milli;
    CHECK_PT(pthread_mutex_unlock(&q->lock));
    return t;
}

static int total_of(outq_t *q)
{
    CHECK_PT(pthread_mutex_lock(&q->lock));
    int n = q->count[0] + q->count[1] + q->count[2];
    CHECK_PT(pthread_mutex_unlock(&q->lock));
    return n;
}

/* ---- a sender thread that pops ONE frame (or sees the queue closing) ---- */
typedef struct {
    outq_t *q;
    pthread_t th;
    sem_t done;
    bool returned;      /* outq_pop returned */
    bool got;           /* ... with a frame */
    int tag;
} popper_t;

static void *popper_main(void *arg)
{
    popper_t *p = arg;
    frame_t *f = NULL;

    p->got = outq_pop(p->q, &f);
    if (p->got) {
        p->tag = f->len;
        frame_unref(f);
    }
    p->returned = true;
    CHECK(sem_post(&p->done));
    return NULL;
}

static void popper_start(popper_t *p, outq_t *q)
{
    memset(p, 0, sizeof *p);
    p->q = q;
    CHECK(sem_init(&p->done, 0, 0));
    CHECK_PT(pthread_create(&p->th, NULL, popper_main, p));
}

/* Wait up to ms for the popper to return; true if it did. */
static bool popper_wait(popper_t *p, long ms)
{
    struct timespec until;

    CHECK(clock_gettime(CLOCK_REALTIME, &until));
    until.tv_sec += ms / 1000;
    until.tv_nsec += (ms % 1000) * 1000000L;
    if (until.tv_nsec >= 1000000000L) {
        until.tv_sec++;
        until.tv_nsec -= 1000000000L;
    }
    while (sem_timedwait(&p->done, &until) < 0) {
        if (errno == EINTR)
            continue;
        return false;                               /* ETIMEDOUT */
    }
    return true;
}

static void popper_end(popper_t *p)
{
    CHECK_PT(pthread_join(p->th, NULL));
    CHECK(sem_destroy(&p->done));
}

/* CPU time used so far by the popper thread, in ms. */
static double popper_cpu_ms(popper_t *p)
{
    clockid_t cid;
    struct timespec ts;

    CHECK_PT(pthread_getcpuclockid(p->th, &cid));
    CHECK(clock_gettime(cid, &ts));
    return (double)ts.tv_sec * 1000.0 + (double)ts.tv_nsec / 1e6;
}

/* Pop one frame without ever sleeping: -1 if the queue has none or no whole token (a test fails, never hangs). */
static int pop_now(outq_t *q)
{
    CHECK_PT(pthread_mutex_lock(&q->lock));
    int blocked = q->count[0] + q->count[1] + q->count[2] == 0 || (q->rate > 0 && q->tokens_milli < TOKEN_COST);
    CHECK_PT(pthread_mutex_unlock(&q->lock));
    if (blocked)
        return -1;
    frame_t *f;
    if (!outq_pop(q, &f))
        return -2;
    int c = outq_class_of(f->prio);
    frame_unref(f);
    return c;
}

static void fill(outq_t *q, int n, int prio)
{
    for (int i = 0; i < n; i++)
        EXPECT(outq_try_push(q, mkf(prio, (uint16_t)i)));
}

/* ---- 1. rate 0 = unlimited ---- */
static void test_unlimited(void)
{
    printf("rate 0: no token check, nothing is subtracted, refill does nothing\n");
    long base = frame_live_count();
    outq_t q;

    EXPECT(outq_init(&q, 64) == 0);
    EXPECT(q.rate == 0);
    fill(&q, 40, 1);
    int n = 0;
    while (pop_now(&q) >= 0)
        n++;
    EXPECT(n == 40);                                  /* all 40 at once, with no token at all */
    EXPECT(tokens_of(&q) == 0);
    outq_refill(&q, 100);
    EXPECT(tokens_of(&q) == 0);                       /* unlimited: refill is a no-op */
    outq_set_rate(&q, 0);
    EXPECT(q.rate == 0 && tokens_of(&q) == 0);
    outq_close(&q);
    outq_drain(&q);
    outq_destroy(&q);
    EXPECT(frame_live_count() == base);
}

/* ---- 2. outq_set_rate ---- */
static void test_set_rate(void)
{
    printf("outq_set_rate: full bucket (rate * 1000), clamps, negative = unlimited\n");
    outq_t q;

    EXPECT(outq_init(&q, 8) == 0);
    outq_set_rate(&q, 50);
    EXPECT(q.rate == 50 && tokens_of(&q) == 50000);
    outq_set_rate(&q, 1);
    EXPECT(q.rate == 1 && tokens_of(&q) == 1000);                 /* the smallest rate: a burst of one frame */
    outq_set_rate(&q, -5);
    EXPECT(q.rate == 0);                                          /* a negative rate is "unlimited", not a huge one */
    outq_set_rate(&q, MAX_RATE + 1);
    EXPECT(q.rate == MAX_RATE && tokens_of(&q) == (long)MAX_RATE * 1000);
    outq_set_rate(&q, INT_MAX);
    EXPECT(q.rate == MAX_RATE);                                   /* cut, no overflow */
    outq_set_rate(&q, 0);
    EXPECT(q.rate == 0);
    outq_close(&q);
    outq_destroy(&q);
}

/* ---- 3. a burst, then blocked, then one frame per whole token ---- */
static void test_burst_and_block(void)
{
    printf("burst of rate frames, then the sender blocks without spinning; whole tokens release frames one by one\n");
    long base = frame_live_count();
    outq_t q;

    EXPECT(outq_init(&q, 64) == 0);
    outq_set_rate(&q, 10);                            /* 10 frames/s: 10000 milli-tokens, a 1 s burst */
    fill(&q, 14, 1);
    for (int i = 0; i < 10; i++)
        EXPECT(pop_now(&q) == OUTQ_NORMAL);           /* the burst */
    EXPECT(tokens_of(&q) == 0);
    EXPECT(pop_now(&q) == -1);                        /* 4 frames are queued but there is no token */
    EXPECT(total_of(&q) == 4);

    popper_t p;
    popper_start(&p, &q);
    EXPECT(!popper_wait(&p, 300));                    /* blocked, although frames wait */
    double cpu = popper_cpu_ms(&p);
    EXPECT(cpu < 50.0);                               /* it sleeps: a spinning thread would have used ~300 ms */
    printf("    sender blocked for 300 ms, cpu used by it: %.2f ms\n", cpu);

    outq_refill(&q, 50);                              /* 10 * 50 = 500 milli-tokens: half a frame */
    EXPECT(tokens_of(&q) == 500);
    EXPECT(!popper_wait(&p, 200));                    /* half a token is not enough */
    outq_refill(&q, 50);                              /* 1000: exactly one frame */
    EXPECT(popper_wait(&p, 5000) && p.got);
    popper_end(&p);
    EXPECT(tokens_of(&q) == 0);                       /* the frame cost 1000 */
    EXPECT(total_of(&q) == 3);

    outq_refill(&q, 300);                             /* 3000: three frames */
    EXPECT(tokens_of(&q) == 3000);
    for (int i = 0; i < 3; i++)
        EXPECT(pop_now(&q) == OUTQ_NORMAL);
    EXPECT(total_of(&q) == 0 && tokens_of(&q) == 0);

    outq_close(&q);
    outq_drain(&q);
    outq_destroy(&q);
    EXPECT(frame_live_count() == base);
}

/* ---- 3b. the wake-up comes from the refill, not from the safety-net timeout ---- */
static void test_wakeup_by_refill(void)
{
    printf("a refill wakes the sender at once (the 2-tick timeout of the wait is only a safety net)\n");
    long base = frame_live_count();
    outq_t q;

    EXPECT(outq_init(&q, 8) == 0);
    outq_set_rate(&q, 10);
    fill(&q, 4, 1);
    for (int i = 0; i < 10 && total_of(&q) > 0; i++)
        (void)pop_now(&q);                            /* use up the burst: at most 4 frames queued, all taken */
    fill(&q, 4, 1);
    CHECK_PT(pthread_mutex_lock(&q.lock));
    q.tokens_milli = 0;
    CHECK_PT(pthread_mutex_unlock(&q.lock));
    for (int trial = 0; trial < 3; trial++) {
        popper_t p;

        popper_start(&p, &q);
        sleep_ms(30);                                 /* it is waiting; its safety-net timeout ends ~170 ms from now */
        outq_refill(&q, 100);                         /* 1000 milli-tokens: one frame */
        EXPECT(popper_wait(&p, 120) && p.got);        /* the broadcast: far sooner than the timeout would */
        popper_end(&p);
    }
    outq_close(&q);
    outq_drain(&q);
    outq_destroy(&q);
    EXPECT(frame_live_count() == base);
}

/* ---- 4. the cap ---- */
static void test_cap(void)
{
    printf("the bucket never holds more than rate * 1000 (a burst of one second); no overflow\n");
    outq_t q;

    EXPECT(outq_init(&q, 8) == 0);
    outq_set_rate(&q, 7);
    outq_refill(&q, 100);                             /* already full */
    EXPECT(tokens_of(&q) == 7000);
    outq_refill(&q, 1000000);                         /* 1000 s at once */
    EXPECT(tokens_of(&q) == 7000);

    outq_set_rate(&q, MAX_RATE);
    outq_refill(&q, 1000000000L);                     /* rate * ms = 1e15: still inside a long */
    EXPECT(tokens_of(&q) == (long)MAX_RATE * 1000);
    outq_refill(&q, LONG_MAX / MAX_RATE);             /* the largest product that fits */
    EXPECT(tokens_of(&q) == (long)MAX_RATE * 1000);
    outq_refill(&q, 0);                               /* nothing, or a negative time, adds nothing */
    outq_refill(&q, -100);
    EXPECT(tokens_of(&q) == (long)MAX_RATE * 1000);

    outq_set_rate(&q, 3);                             /* empty it by sending, then check partial refill and cap */
    fill(&q, 3, 0);
    for (int i = 0; i < 3; i++)
        EXPECT(pop_now(&q) == OUTQ_HIGH);
    EXPECT(tokens_of(&q) == 0);
    outq_refill(&q, 100);
    outq_refill(&q, 100);
    EXPECT(tokens_of(&q) == 600);                     /* 3 * 100 per tick */
    outq_refill(&q, 5000);
    EXPECT(tokens_of(&q) == 3000);                    /* capped */
    outq_close(&q);
    outq_destroy(&q);
}

/* ---- 5. a rate change takes effect at once ---- */
static void test_rate_change(void)
{
    printf("a rate change: bucket reset to the new cap, a blocked sender is released by rate 0\n");
    long base = frame_live_count();
    outq_t q;

    EXPECT(outq_init(&q, 16) == 0);
    outq_set_rate(&q, 2);
    fill(&q, 6, 1);
    EXPECT(pop_now(&q) == OUTQ_NORMAL && pop_now(&q) == OUTQ_NORMAL);
    EXPECT(pop_now(&q) == -1);                        /* the 2-frame burst is used up */

    popper_t p;
    popper_start(&p, &q);
    EXPECT(!popper_wait(&p, 200));
    outq_set_rate(&q, 5);                             /* new cap 5000: the sender goes on at once */
    EXPECT(popper_wait(&p, 5000) && p.got);
    popper_end(&p);
    EXPECT(tokens_of(&q) == 4000);                    /* reset to 5000, minus the frame just sent */
    EXPECT(total_of(&q) == 3);

    for (int i = 0; i < 3; i++)
        EXPECT(pop_now(&q) == OUTQ_NORMAL);           /* 3 of the 4 tokens left */
    EXPECT(tokens_of(&q) == 1000);
    fill(&q, 4, 1);
    EXPECT(pop_now(&q) == OUTQ_NORMAL);               /* the last token */
    EXPECT(pop_now(&q) == -1);                        /* and now it is blocked again */

    popper_start(&p, &q);
    EXPECT(!popper_wait(&p, 200));
    outq_set_rate(&q, 0);                             /* unlimited: the waiting sender is released */
    EXPECT(popper_wait(&p, 5000) && p.got);
    popper_end(&p);
    int n = 0;
    while (pop_now(&q) >= 0)
        n++;
    EXPECT(n == 2);                                   /* the rest of the frames, with no token */

    fill(&q, 3, 1);
    outq_set_rate(&q, 1);                             /* limited again: a fresh one-frame bucket */
    EXPECT(pop_now(&q) == OUTQ_NORMAL && pop_now(&q) == -1);
    outq_close(&q);
    outq_drain(&q);
    outq_destroy(&q);
    EXPECT(frame_live_count() == base);
}

/* ---- 6. close wakes a sender that waits for tokens ---- */
static void test_close_while_throttled(void)
{
    printf("outq_close wakes a sender that is waiting for a token\n");
    long base = frame_live_count();
    outq_t q;

    EXPECT(outq_init(&q, 8) == 0);
    outq_set_rate(&q, 1);
    fill(&q, 3, 2);
    EXPECT(pop_now(&q) == OUTQ_LOW);
    popper_t p;
    popper_start(&p, &q);
    EXPECT(!popper_wait(&p, 200));
    outq_close(&q);
    EXPECT(popper_wait(&p, 5000));
    EXPECT(!p.got);                                   /* false: the queue is closing; the frames stay for the drain */
    popper_end(&p);
    EXPECT(total_of(&q) == 2);
    outq_drain(&q);
    EXPECT(frame_live_count() == base);               /* the two frames that never got a token were released */
    outq_destroy(&q);
}

/* ---- 7. the receiver side never blocks ---- */
static void test_push_never_blocks(void)
{
    printf("while the sender waits for tokens the rings fill and further pushes fail at once (the receiver drops)\n");
    long base = frame_live_count();
    outq_t q;

    EXPECT(outq_init(&q, 4) == 0);
    outq_set_rate(&q, 1);
    fill(&q, 1, 1);
    EXPECT(pop_now(&q) == OUTQ_NORMAL);               /* the burst is spent */
    popper_t p;
    popper_start(&p, &q);
    fill(&q, 4, 1);                                   /* the ring takes cap frames ... */
    frame_t *f = mkf(1, 99);
    double t0 = (double)now_mono().tv_sec + (double)now_mono().tv_nsec / 1e9;
    EXPECT(!outq_try_push(&q, f));                    /* ... and the next one fails without waiting */
    struct timespec t1 = now_mono();
    EXPECT((double)t1.tv_sec + (double)t1.tv_nsec / 1e9 - t0 < 0.5);
    frame_unref(f);
    EXPECT(!popper_wait(&p, 150));                    /* still waiting for its token */
    outq_close(&q);
    EXPECT(popper_wait(&p, 5000));
    popper_end(&p);
    outq_drain(&q);
    outq_destroy(&q);
    EXPECT(frame_live_count() == base);
}

/* ---- 8. the scheduler is unchanged ---- */
static void test_scheduler_with_rate(void)
{
    printf("rate limiting does not change the order: 5:3:1 weighted round robin and priorities\n");
    long base = frame_live_count();
    outq_t q;

    outq_set_sched(W531, 1000000000L);                /* aging off */
    EXPECT(outq_init(&q, 16) == 0);
    outq_set_rate(&q, 100);
    fill(&q, 5, 0);
    fill(&q, 3, 1);
    fill(&q, 1, 2);
    int order[9];
    for (int i = 0; i < 9; i++)
        order[i] = pop_now(&q);
    static const int want[9] = {0, 0, 0, 0, 0, 1, 1, 1, 2};
    EXPECT(memcmp(order, want, sizeof want) == 0);    /* H H H H H N N N L */
    EXPECT(tokens_of(&q) == 100000 - 9000);           /* nine frames, nine tokens */
    outq_close(&q);
    outq_drain(&q);
    outq_destroy(&q);
    EXPECT(frame_live_count() == base);
}

/* ---- 9. concurrent: producers, one rate limited sender, a refill thread (the timer) ---- */
#define PRODUCERS 4
#define PER_PRODUCER 60

typedef struct {
    outq_t *q;
    _Atomic long popped;
    _Atomic long bad;                    /* a token count outside 0..cap seen by the sender */
    sem_t all_popped;
    long want;
} conc_t;

static void *conc_producer(void *arg)
{
    conc_t *c = arg;
    int sent = 0;

    while (sent < PER_PRODUCER) {
        frame_t *f = mkf(1, (uint16_t)sent);

        if (outq_try_push(c->q, f)) {
            sent++;
        } else {
            frame_unref(f);              /* not queued: the caller keeps (and drops) its reference */
            sched_yield();               /* a full ring: the sender frees a place when a token arrives */
        }
    }
    return NULL;
}

static void *conc_sender(void *arg)
{
    conc_t *c = arg;
    frame_t *f;

    while (outq_pop(c->q, &f)) {
        frame_unref(f);
        long t = tokens_of(c->q);
        if (t < 0 || t > (long)c->q->rate * 1000)
            atomic_fetch_add(&c->bad, 1);
        if (atomic_fetch_add(&c->popped, 1) + 1 == c->want)
            CHECK(sem_post(&c->all_popped));
    }
    return NULL;
}

static void *conc_timer(void *arg)
{
    conc_t *c = arg;

    while (atomic_load(&c->popped) < c->want) {
        outq_refill(c->q, TOKEN_TICK_MS);
        sleep_ms(2);                     /* the "timer": real time does not matter for the result */
    }
    return NULL;
}

static void test_concurrent(void)
{
    printf("4 producers + a rate limited sender + a refill thread: every frame sent exactly once, bucket always in range\n");
    long base = frame_live_count();
    outq_t q;
    conc_t c;
    pthread_t prod[PRODUCERS], snd, tmr;

    EXPECT(outq_init(&q, 32) == 0);
    outq_set_rate(&q, 50);
    memset(&c, 0, sizeof c);
    c.q = &q;
    c.want = PRODUCERS * PER_PRODUCER;
    CHECK(sem_init(&c.all_popped, 0, 0));
    CHECK_PT(pthread_create(&snd, NULL, conc_sender, &c));
    CHECK_PT(pthread_create(&tmr, NULL, conc_timer, &c));
    for (int i = 0; i < PRODUCERS; i++)
        CHECK_PT(pthread_create(&prod[i], NULL, conc_producer, &c));
    for (int i = 0; i < PRODUCERS; i++)
        CHECK_PT(pthread_join(prod[i], NULL));

    struct timespec until;
    CHECK(clock_gettime(CLOCK_REALTIME, &until));
    until.tv_sec += 60;
    int done = 0;
    while (!done) {
        if (sem_timedwait(&c.all_popped, &until) == 0)
            done = 1;
        else if (errno != EINTR)
            break;
    }
    EXPECT(done);
    outq_close(&q);
    CHECK_PT(pthread_join(snd, NULL));
    if (!done)
        atomic_store(&c.popped, c.want);              /* let the timer thread end */
    CHECK_PT(pthread_join(tmr, NULL));
    EXPECT(atomic_load(&c.popped) == c.want);
    EXPECT(atomic_load(&c.bad) == 0);
    outq_drain(&q);
    outq_destroy(&q);
    CHECK(sem_destroy(&c.all_popped));
    EXPECT(frame_live_count() == base);
}

int main(void)
{
    alarm(180);                          /* a bug must not hang the suite */
    test_unlimited();
    test_set_rate();
    test_burst_and_block();
    test_wakeup_by_refill();
    test_cap();
    test_rate_change();
    test_close_while_throttled();
    test_push_never_blocks();
    test_scheduler_with_rate();
    test_concurrent();
    printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
