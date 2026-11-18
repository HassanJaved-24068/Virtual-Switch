/*
 * queue.h - the bounded output queue of one port.
 *
 * Three circular buffers of frame pointers ("classes": 0 high, 1 normal, 2 low) protected by ONE
 * mutex; the sender thread of the port sleeps on a condition variable while all are empty.
 *
 * Part 4 used one class. Part 11 (QoS) activates all three:
 *   - a frame goes to the ring of its priority (prio > 2 is treated as 2 = low);
 *   - every ring has room for `cap` frames; fullness is checked PER CLASS: a full low ring drops low
 *     frames only;
 *   - outq_pop picks the next frame with sched_pick(): weighted round robin (weights from the
 *     configuration, 5:3:1 by default) plus an aging override so that a low-priority frame is never
 *     starved: the head of the low (then normal) ring that has waited longer than prio_aging_ms is sent next.
 *
 * Part 12 (rate limiting) adds a token bucket to the same pop path: rate (frames per second, 0 = unlimited)
 * and tokens_milli (one frame costs 1000). A timer thread adds tokens every TOKEN_TICK_MS (outq_refill);
 * outq_pop sleeps on the condition variable - it does not spin - until a whole token is there.
 */
#ifndef QUEUE_H
#define QUEUE_H

#include <pthread.h>
#include <stdbool.h>
#include <time.h>

#include "frame.h"

#define OUTQ_CLASSES 3
#define OUTQ_HIGH 0            /* priority 0 */
#define OUTQ_NORMAL 1          /* priority 1 */
#define OUTQ_LOW 2             /* priority 2 and above */

typedef struct {
    frame_t *f;
    struct timespec enq_time;  /* when it was queued (monotonic clock); used by the aging override */
} qentry_t;

typedef struct {
    pthread_mutex_t lock;
    pthread_cond_t cond;       /* monotonic clock */
    qentry_t *ring[OUTQ_CLASSES];
    int head[OUTQ_CLASSES], tail[OUTQ_CLASSES], count[OUTQ_CLASSES];
    int credit[OUTQ_CLASSES];  /* weighted round robin: sends left for each class in this round */
    int cap;                   /* frames per class */
    bool closing;
    int rate;                  /* frames per second; 0 = unlimited (no token check) */
    long tokens_milli;         /* 1000 = one frame; never more than rate * 1000 (a burst of one second) */
} outq_t;

#define TOKEN_TICK_MS 100          /* period of the token timer thread */
#define TOKEN_COST 1000L           /* milli-tokens per frame */
#define MAX_RATE 1000000           /* largest allowed rate (frames/s); keeps rate * 1000 far inside a long */

/* The ring that a frame priority maps to: 0 -> 0, 1 -> 1, anything above 2 -> 2. */
int outq_class_of(unsigned prio);

/*
 * Scheduler settings, shared by every queue: the weights (high, medium, low; each >= 1) and the aging
 * time in milliseconds (>= 1). Thread-safe (atomics, no lock); a change takes effect at the next credit
 * reset / the next pop. Values below 1 are raised to 1. The defaults are 5,3,1 and 200 ms.
 */
void outq_set_sched(const int weights[OUTQ_CLASSES], long aging_ms);
void outq_get_sched(int weights[OUTQ_CLASSES], long *aging_ms);

/* Create a queue with room for cap frames PER CLASS. Returns 0, or an errno value. */
int outq_init(outq_t *q, int cap);

/*
 * Add a frame to the ring of its priority. NEVER blocks. Returns true if queued (the queue now holds
 * the caller's reference), false if THAT class is full or the queue is closing (the caller keeps its
 * reference and must drop it).
 */
bool outq_try_push(outq_t *q, frame_t *f);

/*
 * Take the next frame as chosen by sched_pick (see queue.c); sleeps while the queue is empty or (with a rate)
 * while there is no whole token; a frame sent costs one token. Returns
 * true and sets *f (the caller now owns that reference), or returns false when the queue is closing.
 */
bool outq_pop(outq_t *q, frame_t **f);

/*
 * Set the rate (frames per second; 0 = unlimited; values above MAX_RATE are cut to it). Takes effect at
 * once: the bucket is reset to its new cap (rate * 1000, a burst of one second) and a waiting sender is woken.
 */
void outq_set_rate(outq_t *q, int rate);

/* Add `ms` milliseconds' worth of tokens (rate * ms), capped at rate * 1000; wakes the sender when a whole
 * token is available. Does nothing for an unlimited queue. Called by the token timer thread. */
void outq_refill(outq_t *q, long ms);

/* Mark the queue closing and wake the sleeping sender. */
void outq_close(outq_t *q);

/* frame_unref() every frame still in the queue (all classes) and empty it. */
void outq_drain(outq_t *q);

/* Free the memory of a drained queue. Nobody may use the queue afterwards. */
void outq_destroy(outq_t *q);

#endif /* QUEUE_H */
