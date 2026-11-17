/*
 * sig.h - signal handling and shutdown request (Part 7).
 *
 * No asynchronous signal handler is used. main() blocks SIGINT, SIGTERM, SIGHUP
 * and SIGUSR1 before it creates any thread (so every thread inherits the mask),
 * and one dedicated thread collects them with sigwait().
 */
#ifndef SIG_H
#define SIG_H

#include <pthread.h>

/*
 * Ignore SIGPIPE and block SIGINT/SIGTERM/SIGHUP/SIGUSR1 in the calling thread.
 * Call it in main() before the first pthread_create(). Returns 0 or an errno value.
 */
int signal_setup(void);

/* Start the signal thread. Returns 0 or an errno value. */
int signal_start(pthread_t *thread);

/* The signal thread: loops in sigwait(); SIGINT/SIGTERM request shutdown and end the loop. */
void *signal_main(void *arg);

/*
 * Make sure the signal thread ends, then join it. Used by the shutdown sequence
 * (normally the thread has already ended itself after SIGINT/SIGTERM) and by the
 * start-up failure path (the thread is still in sigwait()).
 */
void signal_stop_join(pthread_t thread);

/*
 * Ask the whole switch to stop. Thread-safe and idempotent:
 *   lock shutdown_lock; shutdown_requested = true; shutting_down = true;
 *   broadcast shutdown_cond (lock still held); unlock; write 1 to wake_efd.
 */
void request_shutdown(void);

#endif /* SIG_H */
