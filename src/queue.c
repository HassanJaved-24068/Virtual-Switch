/*
 * queue.c - bounded circular output queue (one class in Part 4).
 *
 * Lock order: the queue lock is taken after sw.ports_lock and before the
 * frame ref_lock (outq_drain calls frame_unref while holding it).
 */
#include "queue.h"

#include "common.h"

#include <errno.h>
#include <stdlib.h>

int outq_init(outq_t *q, int cap)
{
    if (cap <= 0)
        return EINVAL;
    memset(q, 0, sizeof *q);

    q->ring[OUTQ_NORMAL] = calloc((size_t)cap, sizeof(qentry_t));
    if (q->ring[OUTQ_NORMAL] == NULL)
        return ENOMEM;
    q->cap = cap;

    int rc = pthread_mutex_init(&q->lock, NULL);
    if (rc != 0) {
        free(q->ring[OUTQ_NORMAL]);
        q->ring[OUTQ_NORMAL] = NULL;
        return rc;
    }
    rc = cond_init_mono(&q->cond);
    if (rc != 0) {
        pthread_mutex_destroy(&q->lock);
        free(q->ring[OUTQ_NORMAL]);
        q->ring[OUTQ_NORMAL] = NULL;
        return rc;
    }
    return 0;
}

bool outq_try_push(outq_t *q, frame_t *f)
{
    const int c = OUTQ_NORMAL;
    bool ok = false;

    CHECK_PT(pthread_mutex_lock(&q->lock));
    if (!q->closing && q->count[c] < q->cap) {
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

bool outq_pop(outq_t *q, frame_t **f)
{
    const int c = OUTQ_NORMAL;

    CHECK_PT(pthread_mutex_lock(&q->lock));
    while (q->count[c] == 0 && !q->closing)          /* always while, never if */
        CHECK_PT(pthread_cond_wait(&q->cond, &q->lock));
    if (q->closing) {
        CHECK_PT(pthread_mutex_unlock(&q->lock));
        return false;
    }
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
    const int c = OUTQ_NORMAL;

    CHECK_PT(pthread_mutex_lock(&q->lock));
    while (q->count[c] > 0) {
        frame_unref(q->ring[c][q->head[c]].f);
        q->ring[c][q->head[c]].f = NULL;
        q->head[c] = (q->head[c] + 1) % q->cap;
        q->count[c]--;
    }
    CHECK_PT(pthread_mutex_unlock(&q->lock));
}

void outq_destroy(outq_t *q)
{
    CHECK_PT(pthread_cond_destroy(&q->cond));
    CHECK_PT(pthread_mutex_destroy(&q->lock));
    free(q->ring[OUTQ_NORMAL]);
    q->ring[OUTQ_NORMAL] = NULL;
}
