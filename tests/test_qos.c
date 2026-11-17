/*
 * test_qos.c - unit tests of the priority queue scheduler (Part 11): three rings, weighted round
 * robin (5:3:1), aging against starvation. Also built with ThreadSanitizer and run under valgrind.
 *
 * A frame is a frame_t whose prio chooses the ring and whose len carries a tag, so that the order of
 * pops can be checked. The "frozen consumer" tests push everything first and pop afterwards.
 */
#include "queue.h"

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
#define NEVER_AGE 1000000000L              /* aging off: ms */

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

static double now_s(void)
{
    struct timespec ts = now_mono();

    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

/*
 * Pop one frame: returns its class, stores its tag, frees it. -1 if the queue is closing; -2 if the queue is
 * EMPTY (a test must fail then, not hang: outq_pop would sleep for ever; pop_class_wait is the blocking one).
 */
static int pop_class_wait(outq_t *q, int *tag)
{
    frame_t *f;

    if (!outq_pop(q, &f))
        return -1;
    int c = outq_class_of(f->prio);

    if (tag != NULL)
        *tag = f->len;
    frame_unref(f);
    return c;
}

static int pop_class(outq_t *q, int *tag)
{
    CHECK_PT(pthread_mutex_lock(&q->lock));
    int empty = q->count[0] + q->count[1] + q->count[2] == 0 && !q->closing;
    CHECK_PT(pthread_mutex_unlock(&q->lock));
    return empty ? -2 : pop_class_wait(q, tag);
}

/* ---- the reference model: the algorithm of the roadmap, written down independently ---- */
struct model {
    int count[3], credit[3], w[3];
};

static void model_init(struct model *m, const int w[3])
{
    memset(m, 0, sizeof *m);
    for (int c = 0; c < 3; c++)
        m->w[c] = m->credit[c] = w[c];
}

static int model_pick(struct model *m)
{
    for (;;) {
        for (int c = 0; c < 3; c++)
            if (m->count[c] > 0 && m->credit[c] > 0) {
                m->credit[c]--;
                m->count[c]--;
                return c;
            }
        for (int c = 0; c < 3; c++)
            m->credit[c] = m->w[c];
    }
}

/* ---- 1. classes ---- */
static void test_classes(void)
{
    printf("priority -> class: 0, 1, 2; everything above 2 is low\n");
    EXPECT(outq_class_of(0) == OUTQ_HIGH && outq_class_of(1) == OUTQ_NORMAL && outq_class_of(2) == OUTQ_LOW);
    EXPECT(outq_class_of(3) == OUTQ_LOW && outq_class_of(7) == OUTQ_LOW && outq_class_of(255) == OUTQ_LOW);

    long base = frame_live_count();
    outq_t q;
    EXPECT(outq_init(&q, 8) == 0);
    int prios[] = {0, 1, 2, 3, 255, 1, 0, 9};
    for (size_t i = 0; i < sizeof prios / sizeof prios[0]; i++)
        EXPECT(outq_try_push(&q, mkf(prios[i], (uint16_t)i)));
    EXPECT(q.count[OUTQ_HIGH] == 2 && q.count[OUTQ_NORMAL] == 2 && q.count[OUTQ_LOW] == 4);   /* 2, 3, 255, 9 are low */
    outq_close(&q);
    outq_drain(&q);
    EXPECT(q.count[0] + q.count[1] + q.count[2] == 0);
    outq_destroy(&q);
    EXPECT(frame_live_count() == base);                 /* every frame of every ring was freed by the drain */
}

/* ---- 2. scheduler settings ---- */
static void test_settings(void)
{
    printf("scheduler settings: set, get, values below 1 are raised to 1\n");
    int w[3];
    long aging;

    outq_set_sched(W531, 200);
    outq_get_sched(w, &aging);
    EXPECT(w[0] == 5 && w[1] == 3 && w[2] == 1 && aging == 200);
    const int odd[3] = {0, -4, 7};
    outq_set_sched(odd, 0);
    outq_get_sched(w, &aging);
    EXPECT(w[0] == 1 && w[1] == 1 && w[2] == 7 && aging == 1);   /* never 0: sched_pick must always end */
    outq_get_sched(w, NULL);                                      /* the aging pointer is optional */
    outq_set_sched(W531, 200);
}

/* ---- 3. a full class does not block the others ---- */
static void test_per_class_fullness(void)
{
    printf("fullness is per class: a full low ring drops low frames only\n");
    long base = frame_live_count();
    outq_t q;
    frame_t *spare[3];

    EXPECT(outq_init(&q, 3) == 0);
    for (int i = 0; i < 3; i++)
        EXPECT(outq_try_push(&q, mkf(2, (uint16_t)i)));        /* the low ring is full */
    frame_t *l4 = mkf(2, 99);
    EXPECT(!outq_try_push(&q, l4));                            /* a 4th low frame: refused at once */
    frame_unref(l4);                                           /* the caller keeps its reference */
    EXPECT(outq_try_push(&q, mkf(0, 100)) && outq_try_push(&q, mkf(1, 101)));    /* high and normal still accept */
    for (int i = 0; i < 2; i++)
        EXPECT(outq_try_push(&q, mkf(0, (uint16_t)(110 + i))));
    EXPECT(q.count[OUTQ_HIGH] == 3 && q.count[OUTQ_NORMAL] == 1 && q.count[OUTQ_LOW] == 3);
    spare[0] = mkf(0, 120);
    EXPECT(!outq_try_push(&q, spare[0]));                      /* now high is full as well */
    frame_unref(spare[0]);
    spare[1] = mkf(1, 121);
    EXPECT(outq_try_push(&q, spare[1]));                       /* normal is not */
    EXPECT(q.count[OUTQ_NORMAL] == 2);
    /* popping one low frame frees exactly one low slot */
    int tag;
    outq_set_sched(W531, NEVER_AGE);
    q.credit[0] = q.credit[1] = 0;                             /* make the scheduler take the low class first */
    q.credit[2] = 1;
    EXPECT(pop_class(&q, &tag) == OUTQ_LOW && tag == 0);
    spare[2] = mkf(2, 130);
    EXPECT(outq_try_push(&q, spare[2]));                       /* a low slot is free again */
    outq_close(&q);
    frame_t *c1 = mkf(1, 140);
    EXPECT(!outq_try_push(&q, c1));                            /* a closing queue accepts nothing */
    frame_unref(c1);
    outq_drain(&q);
    outq_destroy(&q);
    outq_set_sched(W531, 200);
    EXPECT(frame_live_count() == base);
}

/* ---- 4. the 5:3:1 pattern with a frozen consumer ---- */
static void test_ratio(void)
{
    printf("frozen consumer: all rings full, the dequeue order is 5:3:1 (H H H H H N N N L)\n");
    long base = frame_live_count();
    outq_t q;

    outq_set_sched(W531, NEVER_AGE);
    EXPECT(outq_init(&q, 64) == 0);
    for (int i = 0; i < 64; i++) {                             /* fill all three rings before the consumer runs */
        EXPECT(outq_try_push(&q, mkf(0, (uint16_t)(1000 + i))));
        EXPECT(outq_try_push(&q, mkf(1, (uint16_t)(2000 + i))));
        EXPECT(outq_try_push(&q, mkf(2, (uint16_t)(3000 + i))));
    }
    EXPECT(q.count[0] == 64 && q.count[1] == 64 && q.count[2] == 64);

    int seq[192], tags[192];
    for (int i = 0; i < 192; i++)
        seq[i] = pop_class(&q, &tags[i]);

    /* the exact pattern of the first rounds (hard-coded, not computed) */
    static const int round9[9] = {0, 0, 0, 0, 0, 1, 1, 1, 2};
    int exact = 1;
    for (int i = 0; i < 12 * 9; i++)                           /* 12 full rounds: the high ring lasts 12.8 rounds */
        if (seq[i] != round9[i % 9])
            exact = 0;
    EXPECT(exact);
    int h = 0, n = 0, l = 0;
    for (int i = 0; i < 27; i++)
        if (seq[i] == 0)
            h++;
        else if (seq[i] == 1)
            n++;
        else
            l++;
    EXPECT(h == 15 && n == 9 && l == 3);                       /* 5 : 3 : 1 in every round */

    /* the whole sequence, against the reference model; and every class is FIFO */
    struct model m;
    model_init(&m, W531);
    m.count[0] = m.count[1] = m.count[2] = 64;
    int same = 1, fifo = 1, next[3] = {0, 0, 0};
    for (int i = 0; i < 192; i++) {
        if (model_pick(&m) != seq[i])
            same = 0;
        if (tags[i] % 1000 != next[seq[i]]++)
            fifo = 0;
    }
    EXPECT(same && fifo);
    EXPECT(q.count[0] == 0 && q.count[1] == 0 && q.count[2] == 0 && next[0] == 64 && next[1] == 64 && next[2] == 64);
    outq_close(&q);
    outq_drain(&q);
    outq_destroy(&q);
    outq_set_sched(W531, 200);
    EXPECT(frame_live_count() == base);
}

/* ---- 5. other weights and other mixes ---- */
static void run_pattern(const int w[3], const int fill[3], const char *what)
{
    long base = frame_live_count();
    outq_t q;
    int total = fill[0] + fill[1] + fill[2];

    outq_set_sched(w, NEVER_AGE);
    EXPECT(outq_init(&q, 64) == 0);
    for (int c = 0; c < 3; c++)
        for (int i = 0; i < fill[c]; i++)
            EXPECT(outq_try_push(&q, mkf(c, (uint16_t)i)));
    struct model m;
    model_init(&m, w);
    memcpy(m.count, fill, sizeof m.count);
    int same = 1;
    for (int i = 0; i < total; i++)
        if (pop_class(&q, NULL) != model_pick(&m))
            same = 0;
    if (!same)
        printf("  (pattern differs: %s)\n", what);
    EXPECT(same);
    outq_close(&q);
    outq_drain(&q);
    outq_destroy(&q);
    EXPECT(frame_live_count() == base);
}

static void test_other_mixes(void)
{
    printf("other weights and mixes: 1:1:1, 7:1:1, 2:2:2, only two classes busy, one class busy\n");
    static const int w111[3] = {1, 1, 1}, w711[3] = {7, 1, 1}, w222[3] = {2, 2, 2}, w1000[3] = {1000, 1, 1};
    static const int full[3] = {40, 40, 40}, hl[3] = {40, 0, 40}, nl[3] = {0, 40, 40}, h_only[3] = {40, 0, 0};
    static const int l_only[3] = {0, 0, 40}, skew[3] = {3, 50, 7};

    run_pattern(W531, full, "5:3:1 busy");
    run_pattern(w111, full, "1:1:1");
    run_pattern(w711, full, "7:1:1");
    run_pattern(w222, full, "2:2:2");
    run_pattern(w1000, full, "1000:1:1");
    run_pattern(W531, hl, "H and L");
    run_pattern(W531, nl, "N and L");
    run_pattern(W531, h_only, "H only");
    run_pattern(W531, l_only, "L only");
    run_pattern(W531, skew, "few H, many N");
    outq_set_sched(W531, 200);

    /* the nl case exactly: with no high frames the order is N N N L N N N L ... */
    outq_t q;
    EXPECT(outq_init(&q, 64) == 0);
    outq_set_sched(W531, NEVER_AGE);
    for (int i = 0; i < 12; i++) {
        EXPECT(outq_try_push(&q, mkf(1, (uint16_t)i)));
        EXPECT(outq_try_push(&q, mkf(2, (uint16_t)i)));
    }
    int ok = 1;
    for (int i = 0; i < 16; i++)
        if (pop_class(&q, NULL) != ((i % 4) == 3 ? 2 : 1))
            ok = 0;
    EXPECT(ok);
    outq_close(&q);
    outq_drain(&q);
    outq_destroy(&q);
    outq_set_sched(W531, 200);
}

/* ---- 6. random pushes and pops against the model ---- */
static void test_random(void)
{
    printf("20,000 random pushes and pops give the same order as the reference model\n");
    long base = frame_live_count();
    outq_t q;
    struct model m;
    unsigned x = 424242u;
    int same = 1, popped = 0;

    outq_set_sched(W531, NEVER_AGE);
    EXPECT(outq_init(&q, 16) == 0);
    model_init(&m, W531);
    for (int i = 0; i < 20000; i++) {
        x = x * 1103515245u + 12345u;
        int c = (int)((x >> 16) % 3);
        if (((x >> 8) & 3) != 0) {                              /* push (3 of 4) */
            frame_t *f = mkf(c, (uint16_t)i);
            if (outq_try_push(&q, f)) {
                m.count[c]++;
            } else {
                frame_unref(f);
                if (m.count[c] != 16)                           /* refused only when that class is full */
                    same = 0;
            }
        } else if (m.count[0] + m.count[1] + m.count[2] > 0) {  /* pop */
            if (pop_class(&q, NULL) != model_pick(&m))
                same = 0;
            popped++;
        }
    }
    EXPECT(same && popped > 1000);
    outq_close(&q);
    outq_drain(&q);
    outq_destroy(&q);
    outq_set_sched(W531, 200);
    EXPECT(frame_live_count() == base);
}

/* ---- 7. changing the weights while queues are in use ---- */
static void test_weights_change(void)
{
    printf("new weights apply at the next credit reset (the rest of the round uses the old credits)\n");
    long base = frame_live_count();
    outq_t q;
    static const int w111[3] = {1, 1, 1};

    outq_set_sched(W531, NEVER_AGE);
    EXPECT(outq_init(&q, 64) == 0);
    for (int i = 0; i < 20; i++)
        for (int c = 0; c < 3; c++)
            EXPECT(outq_try_push(&q, mkf(c, (uint16_t)i)));
    EXPECT(pop_class(&q, NULL) == 0 && pop_class(&q, NULL) == 0);       /* H H: credits are now 3, 3, 1 */
    outq_set_sched(w111, NEVER_AGE);                                    /* like a SIGHUP in the middle of a round */
    int seq[16], ok = 1;
    for (int i = 0; i < 16; i++)
        seq[i] = pop_class(&q, NULL);
    /* old credits first: H H H N N N L; then the new weights: H N L H N L ... */
    static const int want[16] = {0, 0, 0, 1, 1, 1, 2, 0, 1, 2, 0, 1, 2, 0, 1, 2};
    for (int i = 0; i < 16; i++)
        if (seq[i] != want[i])
            ok = 0;
    EXPECT(ok);
    outq_close(&q);
    outq_drain(&q);
    outq_destroy(&q);
    outq_set_sched(W531, 200);
    EXPECT(frame_live_count() == base);
}

/* ---- 8. aging ---- */
static void test_aging(void)
{
    printf("aging: the head of the low (then the normal) ring that waited more than prio_aging_ms goes first\n");
    long base = frame_live_count();
    outq_t q;
    static const int w1000[3] = {1000, 1, 1};

    /* low frame older than the limit beats high frames although the weights say 1000:1:1 */
    outq_set_sched(w1000, 60);
    EXPECT(outq_init(&q, 16) == 0);
    EXPECT(outq_try_push(&q, mkf(2, 500)));
    sleep_ms(120);                                               /* it is now older than 60 ms */
    for (int i = 0; i < 4; i++)
        EXPECT(outq_try_push(&q, mkf(0, (uint16_t)i)));
    int before[3] = {q.credit[0], q.credit[1], q.credit[2]}, tag = -1;
    EXPECT(pop_class(&q, &tag) == OUTQ_LOW && tag == 500);       /* the aged low frame first */
    EXPECT(q.credit[0] == before[0] && q.credit[1] == before[1] && q.credit[2] == before[2]);   /* no credit used */
    EXPECT(pop_class(&q, &tag) == OUTQ_HIGH && tag == 0);        /* then the weights again */
    outq_close(&q);
    outq_drain(&q);
    outq_destroy(&q);

    /* both older than the limit: low first, then normal, then the rest by weight */
    EXPECT(outq_init(&q, 16) == 0);
    EXPECT(outq_try_push(&q, mkf(1, 600)));
    EXPECT(outq_try_push(&q, mkf(2, 700)));
    sleep_ms(120);
    EXPECT(outq_try_push(&q, mkf(0, 1)));
    EXPECT(pop_class(&q, &tag) == OUTQ_LOW && tag == 700);
    EXPECT(pop_class(&q, &tag) == OUTQ_NORMAL && tag == 600);
    EXPECT(pop_class(&q, &tag) == OUTQ_HIGH && tag == 1);
    outq_close(&q);
    outq_drain(&q);
    outq_destroy(&q);

    /* young frames are not overtaken: with a long aging time the weights decide */
    outq_set_sched(w1000, NEVER_AGE);
    EXPECT(outq_init(&q, 16) == 0);
    EXPECT(outq_try_push(&q, mkf(2, 800)));
    sleep_ms(120);
    EXPECT(outq_try_push(&q, mkf(0, 2)));
    EXPECT(pop_class(&q, &tag) == OUTQ_HIGH && tag == 2);
    EXPECT(pop_class(&q, &tag) == OUTQ_LOW && tag == 800);
    outq_close(&q);
    outq_drain(&q);
    outq_destroy(&q);
    outq_set_sched(W531, 200);
    EXPECT(frame_live_count() == base);
}

/* ---- 9. continuous high traffic: a low frame is not starved ---- */
struct hi_ctx {
    outq_t *q;
    atomic_bool stop;
};

static void *hi_producer(void *arg)
{
    struct hi_ctx *c = arg;

    while (!atomic_load(&c->stop)) {
        frame_t *f = mkf(0, 1);

        if (!outq_try_push(c->q, f))
            frame_unref(f);
        sleep_ms(1);                                  /* keep the high ring non-empty: far faster than the consumer */
    }
    return NULL;
}

/* A consumer that needs ~3 ms per frame ("one write"). Returns the time until the low frame (tag 777) left, or -1. */
static double starvation_run(long aging_ms, double limit_s)
{
    static const int w1000[3] = {1000, 1, 1};
    outq_t q;
    struct hi_ctx ctx;
    pthread_t th;
    double t0, got = -1;

    outq_set_sched(w1000, aging_ms);
    CHECK_PT(outq_init(&q, 64));
    ctx.q = &q;
    atomic_init(&ctx.stop, false);
    CHECK_PT(pthread_create(&th, NULL, hi_producer, &ctx));
    sleep_ms(50);                                     /* the high ring has frames now */
    t0 = now_s();
    CHECK_PT(outq_try_push(&q, mkf(2, 777)) ? 0 : EINVAL);
    while (now_s() - t0 < limit_s) {
        int tag = 0;

        if (pop_class(&q, &tag) == OUTQ_LOW && tag == 777) {
            got = now_s() - t0;
            break;
        }
        sleep_ms(3);                                  /* the write to the host */
    }
    atomic_store(&ctx.stop, true);
    CHECK_PT(pthread_join(th, NULL));
    outq_close(&q);
    outq_drain(&q);
    outq_destroy(&q);
    return got;
}

static void test_starvation(void)
{
    printf("continuous high traffic: with 1000:1:1 and aging 200 ms a low frame still leaves within about 200 ms + one write\n");
    long base = frame_live_count();
    double t = starvation_run(200, 3.0);

    printf("  low frame left after %.0f ms\n", t * 1000);
    EXPECT(t >= 0 && t < 0.200 + 0.003 + 0.4);       /* aging time + one write + scheduling slack (slow variants) */
    EXPECT(t >= 0.15);                               /* and not earlier than the aging time: it did not jump the queue by weight */
    /* control: without aging the weights alone would make it wait for ~1000 high frames (3+ s) */
    double c = starvation_run(NEVER_AGE, 1.2);

    printf("  control (no aging): %s\n", c < 0 ? "not sent within 1.2 s" : "sent");
    EXPECT(c < 0);
    outq_set_sched(W531, 200);
    EXPECT(frame_live_count() == base);
}

/* ---- 10. four producers, one consumer ---- */
#define PRODUCERS 4
#define PER_PRODUCER 20000                                /* the sequence number fits the 16-bit tag */

struct prod {
    outq_t *q;
    int id;
    unsigned long accepted;
};

static void *producer(void *arg)
{
    struct prod *p = arg;

    for (int i = 0; i < PER_PRODUCER; i++) {
        /* producer 3 sends bad priorities (7): they go to the low ring */
        frame_t *f = mkf(p->id == 3 ? 7 : p->id, (uint16_t)i);

        f->src[0] = (uint8_t)p->id;                       /* who sent it; the tag is its sequence number */
        if (outq_try_push(p->q, f))
            p->accepted++;
        else
            frame_unref(f);                               /* queue full: the caller keeps its reference */
        if (i % 64 == 0)
            sched_yield();
    }
    return NULL;
}

struct consumer {
    outq_t *q;
    unsigned long received[PRODUCERS], order_bad, unknown;
    int last[PRODUCERS];
};

static void *consumer(void *arg)
{
    struct consumer *c = arg;
    frame_t *f;

    for (int i = 0; i < PRODUCERS; i++)
        c->last[i] = -1;
    while (outq_pop(c->q, &f)) {                          /* blocks while the queue is empty */
        int id = f->src[0], seq = f->len;

        if (id >= PRODUCERS) {
            c->unknown++;
        } else {
            c->received[id]++;
            if (seq <= c->last[id])
                c->order_bad++;                           /* a producer's frames must not overtake each other */
            c->last[id] = seq;
        }
        frame_unref(f);
    }
    return NULL;
}

static void test_concurrent(void)
{
    printf("%d producers (incl. bad priorities) and one consumer: nothing lost, nothing twice, FIFO per producer\n", PRODUCERS);
    long base = frame_live_count();
    outq_t q;
    pthread_t th[PRODUCERS], cth;
    struct prod p[PRODUCERS];
    struct consumer c;

    memset(&c, 0, sizeof c);
    outq_set_sched(W531, 50);                              /* aging is active here too */
    EXPECT(outq_init(&q, 32) == 0);
    c.q = &q;
    CHECK_PT(pthread_create(&cth, NULL, consumer, &c));
    for (int i = 0; i < PRODUCERS; i++) {
        p[i].q = &q;
        p[i].id = i;
        p[i].accepted = 0;
        CHECK_PT(pthread_create(&th[i], NULL, producer, &p[i]));
    }
    for (int i = 0; i < PRODUCERS; i++)
        CHECK_PT(pthread_join(th[i], NULL));
    for (;;) {                                             /* wait until the consumer has emptied the queue */
        pthread_mutex_lock(&q.lock);
        int pending = q.count[0] + q.count[1] + q.count[2];
        pthread_mutex_unlock(&q.lock);
        if (pending == 0)
            break;
        sleep_ms(1);
    }
    outq_close(&q);                                        /* the consumer's pop returns false now */
    CHECK_PT(pthread_join(cth, NULL));

    unsigned long total_accepted = 0, total_received = 0;
    for (int i = 0; i < PRODUCERS; i++) {
        total_accepted += p[i].accepted;
        total_received += c.received[i];
        EXPECT(c.received[i] == p[i].accepted);            /* every accepted frame came out exactly once */
    }
    printf("  accepted %lu, received %lu\n", total_accepted, total_received);
    EXPECT(total_accepted > 1000 && total_received == total_accepted);
    EXPECT(c.order_bad == 0 && c.unknown == 0);
    outq_drain(&q);
    outq_destroy(&q);
    outq_set_sched(W531, 200);
    EXPECT(frame_live_count() == base);                    /* no frame was leaked or freed twice */
}

/* ---- 11. blocking pop wakes up for any class ---- */
static void *late_pusher(void *arg)
{
    outq_t *q = arg;

    sleep_ms(80);
    if (!outq_try_push(q, mkf(2, 4242)))
        printf("  late push refused\n");
    return NULL;
}

static void test_blocking_pop(void)
{
    printf("pop sleeps on an empty queue and wakes up for a frame of any class (also a low one)\n");
    long base = frame_live_count();
    outq_t q;
    pthread_t th;
    int tag = 0;

    EXPECT(outq_init(&q, 4) == 0);
    CHECK_PT(pthread_create(&th, NULL, late_pusher, &q));
    double t0 = now_s();
    EXPECT(pop_class_wait(&q, &tag) == OUTQ_LOW && tag == 4242);     /* blocks until the late push */
    EXPECT(now_s() - t0 >= 0.05);                            /* it really slept */
    CHECK_PT(pthread_join(th, NULL));
    outq_close(&q);
    EXPECT(pop_class(&q, &tag) == -1);                       /* closing: pop returns false */
    outq_destroy(&q);
    EXPECT(frame_live_count() == base);
}

int main(void)
{
    alarm(300);                       /* backstop: a hang must kill the test (SIGALRM), not block the suite */
    test_classes();
    test_settings();
    test_per_class_fullness();
    test_ratio();
    test_other_mixes();
    test_random();
    test_weights_change();
    test_aging();
    test_starvation();
    test_concurrent();
    test_blocking_pop();
    printf("\n%d checks, %d failures\n", checks, failures);
    printf(failures == 0 ? "ALL PASSED\n" : "SOME FAILED\n");
    return failures == 0 ? 0 : 1;
}
