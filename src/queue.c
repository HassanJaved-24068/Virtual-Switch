/*
 * queue.c - bounded output queue with three priority classes (Part 4 queue, Part 11 scheduler).
 *
 * Lock order: the queue lock is taken after sw.ports_lock and before the
 * frame ref_lock (outq_drain calls frame_unref while holding it). The scheduler
 * settings are atomics, so sched_pick takes no other lock.
 */
#include "queue.h"

#include "common.h"

#include <errno.h>
#include <stdatomic.h>
#include <stdlib.h>

/* ---- scheduler settings shared by all queues (set from the configuration) ---- */
static _Atomic int sched_weight[OUTQ_CLASSES] = {DEFAULT_WRR_HIGH, DEFAULT_WRR_MEDIUM, DEFAULT_WRR_LOW};
static _Atomic long sched_aging_ms = DEFAULT_PRIO_AGING_MS;

int outq_class_of(unsigned prio)
{
    return prio > OUTQ_LOW ? OUTQ_LOW : (int)prio;
}

void outq_set_sched(const int weights[OUTQ_CLASSES], long aging_ms)
{
    for (int c = 0; c < OUTQ_CLASSES; c++)
        atomic_store(&sched_weight[c], weights[c] < 1 ? 1 : weights[c]);   /* never 0: sched_pick must end */
    atomic_store(&sched_aging_ms, aging_ms < 1 ? 1 : aging_ms);
}

void outq_get_sched(int weights[OUTQ_CLASSES], long *aging_ms)
{
    for (int c = 0; c < OUTQ_CLASSES; c++)
        weights[c] = atomic_load(&sched_weight[c]);
    if (aging_ms != NULL)
        *aging_ms = atomic_load(&sched_aging_ms);
}

static void reset_credit(outq_t *q)
{
    for (int c = 0; c < OUTQ_CLASSES; c++)
        q->credit[c] = atomic_load(&sched_weight[c]);
}

static void free_rings(outq_t *q)
{
    for (int c = 0; c < OUTQ_CLASSES; c++) {
        free(q->ring[c]);
        q->ring[c] = NULL;
    }
}

int outq_init(outq_t *q, int cap)
{
    if (cap <= 0)
        return EINVAL;
    memset(q, 0, sizeof *q);

    for (int c = 0; c < OUTQ_CLASSES; c++) {
        q->ring[c] = calloc((size_t)cap, sizeof(qentry_t));
        if (q->ring[c] == NULL) {
            free_rings(q);
            return ENOMEM;
        }
    }
    q->cap = cap;
    reset_credit(q);

    int rc = pthread_mutex_init(&q->lock, NULL);
    if (rc != 0) {
        free_rings(q);
        return rc;
    }
    rc = cond_init_mono(&q->cond);
    if (rc != 0) {
        pthread_mutex_destroy(&q->lock);
        free_rings(q);
        return rc;
    }
    return 0;
}

bool outq_try_push(outq_t *q, frame_t *f)
{
    const int c = outq_class_of(f->prio);          /* the ring of this frame's priority */
    bool ok = false;

    CHECK_PT(pthread_mutex_lock(&q->lock));
    if (!q->closing && q->count[c] < q->cap) {     /* fullness is checked per class */
        q->ring[c][q->tail[c]].f = f;
        q->ring[c][q->tail[c]].enq_time = now_mono();
        q->tail[c] = (q->tail[c] + 1) % q->cap;
        q->count[c]++;
        CHECK_PT(pthread_cond_signal(&q->cond));
        ok = true;
    }
    CHECK_PT(pthread_mutex_unlock(&q->lock));
    return ok;                      /* full or closing: return at once, never wait */
}

/*
 * Choose the class of the next frame. The caller holds q->lock and the total count is > 0.
 *   1. Aging override: if the head of the LOW ring (then of the NORMAL ring) has waited more than
 *      prio_aging_ms, pick it. (This does not use up any credit.)
 *   2. Otherwise pick the first class c in the order 0, 1, 2 with count[c] > 0 and credit[c] > 0 and
 *      use up one credit of it.
 *   3. If no class qualifies, reset the credits to the weights and repeat step 2. Some ring is not
 *      empty and every weight is >= 1, so the second pass always finds one.
 * With weights 5,3,1 and all classes busy one round sends H H H H H N N N L.
 */
static int sched_pick(outq_t *q)
{
    struct timespec now = now_mono();
    const long aging = atomic_load(&sched_aging_ms);

    for (int c = OUTQ_LOW; c >= OUTQ_NORMAL; c--)
        if (q->count[c] > 0 && ts_diff_ms(now, q->ring[c][q->head[c]].enq_time) > aging)
            return c;

    for (;;) {
        for (int c = 0; c < OUTQ_CLASSES; c++) {
            if (q->count[c] > 0 && q->credit[c] > 0) {
                q->credit[c]--;
                return c;
            }
        }
        reset_credit(q);
    }
}

bool outq_pop(outq_t *q, frame_t **f)
{
    CHECK_PT(pthread_mutex_lock(&q->lock));
    while (q->count[0] + q->count[1] + q->count[2] == 0 && !q->closing)   /* always while, never if */
        CHECK_PT(pthread_cond_wait(&q->cond, &q->lock));
    if (q->closing) {
        CHECK_PT(pthread_mutex_unlock(&q->lock));
        return false;
    }
    const int c = sched_pick(q);
    *f = q->ring[c][q->head[c]].f;
    q->ring[c][q->head[c]].f = NULL;
    q->head[c] = (q->head[c] + 1) % q->cap;
    q->count[c]--;
    CHECK_PT(pthread_mutex_unlock(&q->lock));
    return true;
}

void outq_close(outq_t *q)
{
    CHECK_PT(pthread_mutex_lock(&q->lock));
    q->closing = true;
    CHECK_PT(pthread_cond_broadcast(&q->cond));
    CHECK_PT(pthread_mutex_unlock(&q->lock));
}

void outq_drain(outq_t *q)
{
    CHECK_PT(pthread_mutex_lock(&q->lock));
    for (int c = 0; c < OUTQ_CLASSES; c++) {
        while (q->count[c] > 0) {
            frame_unref(q->ring[c][q->head[c]].f);
            q->ring[c][q->head[c]].f = NULL;
            q->head[c] = (q->head[c] + 1) % q->cap;
            q->count[c]--;
        }
    }
    CHECK_PT(pthread_mutex_unlock(&q->lock));
}

void outq_destroy(outq_t *q)
{
    CHECK_PT(pthread_cond_destroy(&q->cond));
    CHECK_PT(pthread_mutex_destroy(&q->lock));
    free_rings(q);
}
