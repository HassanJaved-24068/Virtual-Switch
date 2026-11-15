/*
 * log.h - simple logging API.
 * Part 1: synchronous version (mutex + fprintf to stderr).
 * Part 9 will replace the inside with a logger thread; the API stays.
 */
#ifndef LOG_H
#define LOG_H

enum log_level {
    LOG_INFO,
    LOG_WARN,
    LOG_ERR
};

/* Writes one line: "YYYY-MM-DD HH:MM:SS.mmm [LEVEL] message". Thread-safe. */
void log_msg(enum log_level level, const char *fmt, ...)
    __attribute__((format(printf, 2, 3)));

#endif /* LOG_H */
