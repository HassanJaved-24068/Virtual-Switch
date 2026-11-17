/*
 * log.c - the logger (Part 9): a bounded ring of log records, one mutex, one condition
 * variable and one logger thread that writes them to the log file.
 *
 *   log_msg  : vsnprintf into a local buffer (no lock), CLOCK_REALTIME timestamp, lock,
 *              copy in if there is space, otherwise count a drop, signal, unlock. Never waits.
 *   logger   : waits while the ring is empty and not stopping, pops a batch, unlocks, writes
 *              the lines, fflush; reports "N log messages dropped" if some were dropped.
 *   log_stop : set stopping, broadcast; the logger drains the ring, exits and is joined.
 *
 * When no logger runs, log_msg() writes synchronously to stderr (the Part 1 behaviour).
 */
#include "log.h"
#include "common.h"

#include <fcntl.h>
#include <stdarg.h>
#include <unistd.h>

#define LOG_BATCH 64             /* records popped per lock round */

typedef struct {
    struct timespec ts;          /* CLOCK_REALTIME, taken by the caller of log_msg */
    int level;
    char text[LOG_TEXT_LEN];
} log_rec_t;

/* ---- shared state: everything below is protected by qlock ---- */
static pthread_mutex_t qlock = PTHREAD_MUTEX_INITIALIZER;     /* lock order rank 6 */
static pthread_cond_t not_empty = PTHREAD_COND_INITIALIZER;
static log_rec_t ring[LOG_QUEUE_SIZE];
static unsigned head, count;                  /* oldest record, number of records */
static bool running, stopping;                /* logger thread exists / is asked to finish */
static unsigned long long dropped;            /* dropped since the last "N dropped" line */
static unsigned long long dropped_total;      /* dropped since log_start() */

/* ---- used by the logger thread and by log_start/log_stop only ---- */
static pthread_t logger_thread;
static FILE *out;

/* ---- the synchronous fallback ---- */
static pthread_mutex_t sync_lock = PTHREAD_MUTEX_INITIALIZER;

static const char *level_name(int level)
{
    switch (level) {
    case LOG_INFO: return "INFO";
    case LOG_WARN: return "WARN";
    case LOG_ERR:  return "ERR";
    }
    return "?";
}

/* "YYYY-MM-DD HH:MM:SS.mmm [LEVEL] text\n" to f (no flushing). Real (wall-clock) time. */
static void write_line(FILE *f, const struct timespec *ts, int level, const char *text)
{
    char when[32] = "unknown-time";
    struct tm tm;

    if (localtime_r(&ts->tv_sec, &tm) != NULL)
        strftime(when, sizeof when, "%Y-%m-%d %H:%M:%S", &tm);
    fprintf(f, "%s.%03ld [%s] %s\n", when, ts->tv_nsec / 1000000L, level_name(level), text);
}

static void write_sync(const struct timespec *ts, int level, const char *text)
{
    int rc = pthread_mutex_lock(&sync_lock);

    if (rc != 0) {                      /* cannot happen; do not lose the message */
        fprintf(stderr, "log: mutex lock failed: %s\n", strerror(rc));
        return;
    }
    write_line(stderr, ts, level, text);
    fflush(stderr);
    pthread_mutex_unlock(&sync_lock);
}

void log_msg(enum log_level level, const char *fmt, ...)
{
    char text[LOG_TEXT_LEN];
    struct timespec ts;
    va_list ap;

    /* Format first, outside any lock. Text longer than the record is cut. */
    va_start(ap, fmt);
    vsnprintf(text, sizeof text, fmt, ap);
    va_end(ap);
    if (clock_gettime(CLOCK_REALTIME, &ts) < 0) {
        ts.tv_sec = 0;
        ts.tv_nsec = 0;
    }

    int rc = pthread_mutex_lock(&qlock);
    if (rc != 0) {                      /* cannot happen; do not lose the message */
        fprintf(stderr, "log: mutex lock failed: %s\n", strerror(rc));
        return;
    }
    if (running && !stopping) {
        if (count < LOG_QUEUE_SIZE) {
            log_rec_t *r = &ring[(head + count) % LOG_QUEUE_SIZE];
            r->ts = ts;
            r->level = (int)level;
            memcpy(r->text, text, sizeof r->text);      /* a copy: nothing points into the caller */
            count++;
        } else {
            dropped++;                                  /* full: drop, never wait */
            dropped_total++;
        }
        pthread_cond_signal(&not_empty);
        pthread_mutex_unlock(&qlock);
        return;
    }
    pthread_mutex_unlock(&qlock);
    write_sync(&ts, (int)level, text);                  /* no logger: the old synchronous way */
}

/* ------------------------------------------------------------------ */
/* The logger thread                                                   */
/* ------------------------------------------------------------------ */

static void *logger_main(void *arg)
{
    log_rec_t batch[LOG_BATCH];
    bool write_failed = false;
    int rc;

    (void)arg;
    rc = pthread_setname_np(pthread_self(), "logger");
    if (rc != 0)
        fprintf(stderr, "log: cannot set thread name: %s\n", strerror(rc));

    for (;;) {
        unsigned n = 0;
        unsigned long long lost;

        pthread_mutex_lock(&qlock);
        while (count == 0 && dropped == 0 && !stopping)          /* while, never if */
            pthread_cond_wait(&not_empty, &qlock);
        if (count == 0 && dropped == 0) {                        /* stopping and nothing left */
            pthread_mutex_unlock(&qlock);
            break;
        }
        while (n < LOG_BATCH && count > 0) {                     /* pop a batch */
            batch[n++] = ring[head];
            head = (head + 1) % LOG_QUEUE_SIZE;
            count--;
        }
        lost = dropped;
        dropped = 0;
        pthread_mutex_unlock(&qlock);

        /* All I/O happens here, with no lock held. */
        for (unsigned i = 0; i < n; i++)
            write_line(out, &batch[i].ts, batch[i].level, batch[i].text);
        if (lost != 0) {
            char text[LOG_TEXT_LEN];
            struct timespec now;

            snprintf(text, sizeof text, "%llu log messages dropped", lost);
            if (clock_gettime(CLOCK_REALTIME, &now) < 0) {
                now.tv_sec = 0;
                now.tv_nsec = 0;
            }
            write_line(out, &now, LOG_WARN, text);
        }
        if (fflush(out) != 0 && !write_failed) {         /* tell once, do not flood stderr */
            write_failed = true;
            fprintf(stderr, "log: write to the log file failed: %s\n", strerror(errno));
        }
    }
    return NULL;
}

int log_start(const char *path)
{
    int fd, rc;

    pthread_mutex_lock(&qlock);
    rc = running ? EBUSY : 0;
    pthread_mutex_unlock(&qlock);
    if (rc != 0)
        return rc;

    fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    if (fd < 0)
        return errno;
    out = fdopen(fd, "a");
    if (out == NULL) {
        rc = errno;
        close(fd);
        return rc;
    }

    pthread_mutex_lock(&qlock);
    running = true;
    stopping = false;
    head = count = 0;
    dropped = dropped_total = 0;
    pthread_mutex_unlock(&qlock);

    rc = pthread_create(&logger_thread, NULL, logger_main, NULL);
    if (rc != 0) {
        pthread_mutex_lock(&qlock);
        running = false;
        pthread_mutex_unlock(&qlock);
        fclose(out);
        out = NULL;
        return rc;
    }
    return 0;
}

void log_stop(void)
{
    int rc;

    pthread_mutex_lock(&qlock);
    if (!running || stopping) {
        pthread_mutex_unlock(&qlock);
        return;
    }
    stopping = true;                              /* from now on log_msg writes to stderr */
    pthread_cond_broadcast(&not_empty);
    pthread_mutex_unlock(&qlock);

    rc = pthread_join(logger_thread, NULL);       /* it drains the ring first */
    if (rc != 0)
        fprintf(stderr, "log: join of the logger thread failed: %s\n", strerror(rc));

    pthread_mutex_lock(&qlock);
    running = false;
    stopping = false;
    pthread_mutex_unlock(&qlock);
    if (fclose(out) != 0)
        fprintf(stderr, "log: closing the log file failed: %s\n", strerror(errno));
    out = NULL;
}

unsigned long long log_dropped_total(void)
{
    unsigned long long n;

    pthread_mutex_lock(&qlock);
    n = dropped_total;
    pthread_mutex_unlock(&qlock);
    return n;
}
