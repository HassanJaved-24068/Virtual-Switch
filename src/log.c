/*
 * log.c - synchronous logger: one mutex, one fprintf to stderr.
 */
#include "log.h"
#include "common.h"

#include <stdarg.h>

static pthread_mutex_t log_lock = PTHREAD_MUTEX_INITIALIZER;

static const char *level_name(enum log_level level)
{
    switch (level) {
    case LOG_INFO: return "INFO";
    case LOG_WARN: return "WARN";
    case LOG_ERR:  return "ERR";
    }
    return "?";
}

void log_msg(enum log_level level, const char *fmt, ...)
{
    char msg[1024];
    char when[32] = "unknown-time";
    struct timespec ts;
    struct tm tm;
    va_list ap;

    /* Format the message first, outside the lock. */
    va_start(ap, fmt);
    vsnprintf(msg, sizeof msg, fmt, ap);
    va_end(ap);

    /* Log timestamps use real (wall-clock) time. */
    if (clock_gettime(CLOCK_REALTIME, &ts) == 0 && localtime_r(&ts.tv_sec, &tm) != NULL)
        strftime(when, sizeof when, "%Y-%m-%d %H:%M:%S", &tm);

    int rc = pthread_mutex_lock(&log_lock);
    if (rc != 0) {                      /* cannot happen; do not lose the message */
        fprintf(stderr, "log: mutex lock failed: %s\n", strerror(rc));
        return;
    }
    fprintf(stderr, "%s.%03ld [%s] %s\n", when, ts.tv_nsec / 1000000L,
            level_name(level), msg);
    fflush(stderr);
    pthread_mutex_unlock(&log_lock);
}
