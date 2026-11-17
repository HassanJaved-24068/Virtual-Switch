/*
 * log.h - logging API.
 *
 * Part 1: synchronous version (mutex + fprintf to stderr).
 * Part 9: queued version. log_start() opens the log file and starts ONE logger thread;
 * from then on log_msg() only formats the text, copies it into a bounded ring (1024
 * records) and returns: it never waits for the file and never waits for space (a full ring
 * drops the message and counts it). The logger thread writes the lines and reports drops.
 * Without log_start() (host, swctl, unit tests, the switch before/after the logger) log_msg()
 * falls back to the synchronous write to stderr, so the API is the same everywhere.
 */
#ifndef LOG_H
#define LOG_H

enum log_level {
    LOG_INFO,
    LOG_WARN,
    LOG_ERR
};

#define LOG_QUEUE_SIZE 1024      /* records in the ring */
#define LOG_TEXT_LEN 256         /* bytes per record, with the terminating '\0' (longer text is cut) */

/*
 * One line: "YYYY-MM-DD HH:MM:SS.mmm [LEVEL] message". Thread-safe. Queued when the logger
 * runs (never blocks on I/O or on a full ring), otherwise written to stderr at once.
 * Lock order: the logger lock is rank 6 (after ports_lock, queue, bucket, reaper and
 * frame locks; before the stats mutex): never call log_msg while holding the stats mutex.
 */
void log_msg(enum log_level level, const char *fmt, ...)
    __attribute__((format(printf, 2, 3)));

/*
 * Open `path` (O_CREAT | O_APPEND: existing content is kept) and start the logger thread.
 * Returns 0, or an errno value (EBUSY if it already runs). Call it after signal_setup(), so
 * the thread inherits the blocked signal mask.
 */
int log_start(const char *path);

/*
 * Stop the logger: set "stopping", wake it, let it write everything that is still queued (and the
 * count of dropped messages), join it, close the file. It must be the LAST thread of the switch to stop,
 * so that the shutdown messages are written. Safe to call when the logger does not run.
 * Afterwards log_msg() writes to stderr again.
 */
void log_stop(void);

/* Messages dropped because the ring was full, since log_start() (for tests and statistics). */
unsigned long long log_dropped_total(void);

#endif /* LOG_H */
