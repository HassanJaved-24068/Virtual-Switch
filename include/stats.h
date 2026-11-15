/*
 * stats.h - per-port counters.
 * Part 4: a heap array protected by one mutex. Part 14 moves it into
 * shared memory; the API stays the same.
 */
#ifndef STATS_H
#define STATS_H

enum stat_kind {
    STAT_IN,       /* frames received from the host on this port */
    STAT_OUT,      /* frames written to the host on this port */
    STAT_DROP,     /* frames dropped because this port's queue was full */
    STAT_MIRROR,   /* mirrored copies (used from Part 17) */
    STAT_KINDS
};

/* Allocate counters for ports 0..max_port. Returns 0, or an errno value. */
int stats_init(int max_port);

/* Free the counters. */
void stats_destroy(void);

/* Add 1 to a counter and return its new value (0 if port/kind is invalid). */
unsigned long long stats_inc(int port, enum stat_kind kind);

/* Read a counter (0 if port/kind is invalid). */
unsigned long long stats_get(int port, enum stat_kind kind);

#endif /* STATS_H */
