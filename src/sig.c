/*
 * sig.c - the signal thread and request_shutdown() (Part 7).
 */
#include "sig.h"

#include "common.h"
#include "log.h"
#include "port.h"
#include "reload.h"

#include <signal.h>
#include <stdint.h>
#include <unistd.h>

/* The signals the switch handles. Built by signal_setup(), read by the signal thread. */
static sigset_t handled;

int signal_setup(void)
{
    struct sigaction sa;
    int rc;

    /* A write to a host that has gone must fail with EPIPE, not kill the process. */
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = SIG_IGN;
    if (sigemptyset(&sa.sa_mask) < 0 || sigaction(SIGPIPE, &sa, NULL) < 0)
        return errno;

    if (sigemptyset(&handled) < 0 || sigaddset(&handled, SIGINT) < 0 ||
        sigaddset(&handled, SIGTERM) < 0 || sigaddset(&handled, SIGHUP) < 0 ||
        sigaddset(&handled, SIGUSR1) < 0)
        return errno;

    /* Blocked here, before any thread exists: every thread created later inherits it,
     * so a signal can only ever be taken by sigwait() in the signal thread. */
    rc = pthread_sigmask(SIG_BLOCK, &handled, NULL);      /* returns the error, errno unused */
    return rc;
}

void request_shutdown(void)
{
    /* 1-5: the predicate and the broadcast are under the SAME mutex the waiters use.
     * A waiter either sees shutdown_requested before it sleeps, or is already asleep
     * and receives the broadcast: the wake-up cannot be lost. */
    CHECK_PT(pthread_mutex_lock(&sw.shutdown_lock));
    sw.shutdown_requested = true;
    atomic_store(&sw.shutting_down, true);
    CHECK_PT(pthread_cond_broadcast(&sw.shutdown_cond));
    CHECK_PT(pthread_mutex_unlock(&sw.shutdown_lock));

    /* 6: wake the accept thread out of poll(). A second call just adds 1 to the
     * counter; EAGAIN would mean the counter is already at its maximum, i.e. the
     * eventfd is readable, which is all we need. */
    if (sw.wake_efd < 0)
        return;                                   /* unit tests without an eventfd */
    const uint64_t one = 1;
    for (;;) {
        ssize_t n = write(sw.wake_efd, &one, sizeof one);
        if (n == (ssize_t)sizeof one)
            return;
        if (n < 0 && errno == EINTR)
            continue;
        if (n < 0 && errno == EAGAIN)
            return;
        log_msg(LOG_ERR, "request_shutdown: writing the wake eventfd failed: %s",
                n < 0 ? strerror(errno) : "short write");
        return;
    }
}

void *signal_main(void *arg)
{
    int rc;

    (void)arg;
    rc = pthread_setname_np(pthread_self(), "signal");
    if (rc != 0)
        log_msg(LOG_WARN, "cannot set thread name: %s", strerror(rc));

    for (;;) {
        int sig = 0;

        rc = sigwait(&handled, &sig);                 /* returns the error code, errno unused */
        if (rc != 0) {
            /* Cannot happen with a valid set. Never loop on an error: stop the switch. */
            log_msg(LOG_ERR, "sigwait failed: %s: shutting down", strerror(rc));
            request_shutdown();
            return NULL;
        }
        switch (sig) {
        case SIGINT:
        case SIGTERM:
            log_msg(LOG_INFO, "%s received: shutting down", sig == SIGINT ? "SIGINT" : "SIGTERM");
            request_shutdown();
            return NULL;
        case SIGHUP:                                   /* Part 10: reload the configuration file */
            log_msg(LOG_INFO, "SIGHUP received: reloading the configuration");
            (void)reload_config();                      /* logs its own result; a failure keeps the old config */
            break;
        case SIGUSR1:                                  /* Part 10: dump the MAC table */
            log_msg(LOG_INFO, "SIGUSR1 received: dumping the MAC table");
            (void)dump_mac_table();
            break;
        default:
            log_msg(LOG_WARN, "unexpected signal %d ignored", sig);
            break;
        }
    }
}

int signal_start(pthread_t *thread)
{
    return pthread_create(thread, NULL, signal_main, NULL);
}

void signal_stop_join(pthread_t thread)
{
    /* If the thread already ended, this is harmless (ESRCH or a signal that is
     * discarded with the thread). If it is still in sigwait(), it wakes up, calls
     * request_shutdown() again (idempotent) and returns. */
    int rc = pthread_kill(thread, SIGTERM);
    if (rc != 0 && rc != ESRCH)
        log_msg(LOG_WARN, "cannot signal the signal thread: %s", strerror(rc));
    rc = pthread_join(thread, NULL);
    if (rc != 0)
        log_msg(LOG_ERR, "join of the signal thread failed: %s", strerror(rc));
}
