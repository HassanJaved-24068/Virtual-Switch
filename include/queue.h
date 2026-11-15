/*
 * queue.h - the bounded output queue of one port.
 *
 * A circular buffer of frame pointers protected by a mutex; the sender
 * thread of the port sleeps on a condition variable while it is empty.
 * Part 4 uses ONE class (index 1, "normal"). The arrays have three entries
 * so that Part 11 can switch on the three priority classes without
 * changing the structure.
 */
#ifndef QUEUE_H
#define QUEUE_H

#include <pthread.h>
#include <stdbool.h>
#include <time.h>

#include "frame.h"

#define OUTQ_CLASSES 3
#define OUTQ_NORMAL 1          /* the only class used in Part 4 */

typedef struct {
    frame_t *f;
    struct timespec enq_time;  /* when it was queued (monotonic clock); used by Part 11 aging */
} qentry_t;

typedef struct {
    pthread_mutex_t lock;
    pthread_cond_t cond;       /* monotonic clock */
    qentry_t *ring[OUTQ_CLASSES];
    int head[OUTQ_CLASSES], tail[OUTQ_CLASSES], count[OUTQ_CLASSES];
    int cap;
    bool closing;
} outq_t;

/* Create a queue with room for cap frames. Returns 0, or an errno value. */
int outq_init(outq_t *q, int cap);

/*
 * Add a frame. NEVER blocks. Returns true if queued (the queue now holds
 * the caller's reference), false if the queue is full or closing (the
 * caller keeps its reference and must drop it).
 */
bool outq_try_push(outq_t *q, frame_t *f);

/*
 * Take the oldest frame; sleeps while the queue is empty. Returns true and
 * sets *f (the caller now owns that reference), or returns false when the
 * queue is closing.
 */
bool outq_pop(outq_t *q, frame_t **f);

/* Mark the queue closing and wake the sleeping sender. */
void outq_close(outq_t *q);

/* frame_unref() every frame still in the queue and empty it. */
void outq_drain(outq_t *q);

/* Free the memory of a drained queue. Nobody may use the queue afterwards. */
void outq_destroy(outq_t *q);

#endif /* QUEUE_H */
