/*
 * stats.c - per-port counters in a heap array, protected by one mutex.
 * Lock order: the stats mutex is always taken last. Never call log_msg while
 * holding it (this file never does).
 */
#include "stats.h"

#include "common.h"

#include <errno.h>
#include <stdlib.h>

static pthread_mutex_t stats_lock = PTHREAD_MUTEX_INITIALIZER;
static unsigned long long *counts;     /* [port * STAT_KINDS + kind] */
static int nports;                     /* number of ports in the array (max_port + 1) */

int stats_init(int max_port)
{
    if (max_port < 0)
        return EINVAL;
    unsigned long long *p = calloc((size_t)(max_port + 1) * STAT_KINDS, sizeof *p);
    if (p == NULL)
        return ENOMEM;

    CHECK_PT(pthread_mutex_lock(&stats_lock));
    free(counts);
    counts = p;
    nports = max_port + 1;
    CHECK_PT(pthread_mutex_unlock(&stats_lock));
    return 0;
}

void stats_destroy(void)
{
    CHECK_PT(pthread_mutex_lock(&stats_lock));
    free(counts);
    counts = NULL;
    nports = 0;
    CHECK_PT(pthread_mutex_unlock(&stats_lock));
}

static bool valid(int port, enum stat_kind kind)
{
    return counts != NULL && port >= 0 && port < nports && (int)kind >= 0 && kind < STAT_KINDS;
}

unsigned long long stats_inc(int port, enum stat_kind kind)
{
    unsigned long long v = 0;

    CHECK_PT(pthread_mutex_lock(&stats_lock));
    if (valid(port, kind))
        v = ++counts[port * STAT_KINDS + kind];
    CHECK_PT(pthread_mutex_unlock(&stats_lock));
    return v;
}

unsigned long long stats_get(int port, enum stat_kind kind)
{
    unsigned long long v = 0;

    CHECK_PT(pthread_mutex_lock(&stats_lock));
    if (valid(port, kind))
        v = counts[port * STAT_KINDS + kind];
    CHECK_PT(pthread_mutex_unlock(&stats_lock));
    return v;
}
