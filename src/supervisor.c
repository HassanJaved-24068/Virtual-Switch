/*
 * supervisor.c - restarts the switch after a crash (Part 20, bonus).
 *
 *   ./supervisor <switch_program> <tcp_port> <num_ports> [switch options]
 *
 * The supervisor forks; the child execv()s the switch with the same arguments plus --persist-mac, so the MAC table lives in the shared-memory object
 * /vswitch_mac_<tcp_port> and the next incarnation recovers it. The parent is a single-threaded process that waits for events with poll() on a signalfd
 * (SIGCHLD, SIGINT, SIGTERM): no signal handlers, no sleeping, no spinning; the restart delay is the poll() timeout.
 *
 * Restart policy: the child exited with a non-zero status, or was killed by a signal other than SIGINT / SIGTERM -> restart after 1 s. At most
 * MAX_RESTARTS restarts within RESTART_WINDOW_SEC seconds, then the supervisor gives up (exit status 1). A clean exit (status 0, or a child that was ended by
 * SIGINT / SIGTERM) ends the supervisor. SIGINT or SIGTERM to the supervisor is forwarded to the child; after the child is reaped the supervisor unlinks
 * /vswitch_mac_<tcp_port> (the switch itself never does) and exits. If the child had crashed and could not clean up after itself, its other leftovers
 * (the counters /vswitch_stats_P and the admin socket) are removed too, so that nothing stays behind.
 */
#include "admin.h"
#include "common.h"
#include "stats.h"

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/signalfd.h>
#include <sys/wait.h>
#include <unistd.h>

#define MAX_RESTARTS 5
#define RESTART_WINDOW_SEC 60
#define RESTART_DELAY_MS 1000

static void usage(const char *prog)
{
    fprintf(stderr, "usage: %s <switch_program> <tcp_port 1-65535> <num_ports> [switch options]\n", prog);
}

static long mono_ms(void)
{
    struct timespec ts = now_mono();
    return (long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

/* Remove a file or shared-memory object that must not stay behind (absent is fine). */
static void remove_leftover(const char *what, int rc)
{
    if (rc < 0 && errno != ENOENT)
        fprintf(stderr, "supervisor: cannot remove %s: %s\n", what, strerror(errno));
}

static void clean_up(int tcp_port, bool child_crashed)
{
    char name[64], path[128];

    snprintf(name, sizeof name, MAC_SHM_FMT, tcp_port);
    remove_leftover(name, shm_unlink(name));
    if (child_crashed) {                         /* a switch that died by itself did not remove its own files */
        remove_leftover(STATS_SHM_NAME, shm_unlink(STATS_SHM_NAME));
        admin_path(tcp_port, path, sizeof path);
        remove_leftover(path, unlink(path));
    }
}

/* Fork and exec the switch. Returns the child's pid, or -1 (reported). */
static pid_t start_child(const char *prog, char **args, int nargs, const sigset_t *orig_mask)
{
    pid_t pid = fork();

    if (pid < 0) {
        fprintf(stderr, "supervisor: fork failed: %s\n", strerror(errno));
        return -1;
    }
    if (pid == 0) {
        /* The supervisor blocks SIGINT / SIGTERM / SIGCHLD for its signalfd; the switch starts with the mask it had when we were started. */
        if (sigprocmask(SIG_SETMASK, orig_mask, NULL) < 0) {
            fprintf(stderr, "supervisor: child: sigprocmask failed: %s\n", strerror(errno));
            _exit(126);
        }
        char *argv[nargs + 3];
        int n = 0;
        argv[n++] = (char *)prog;
        for (int i = 0; i < nargs; i++)
            argv[n++] = args[i];
        bool has = false;
        for (int i = 0; i < nargs; i++)
            has = has || strcmp(args[i], "--persist-mac") == 0;
        if (!has)
            argv[n++] = "--persist-mac";
        argv[n] = NULL;
        if (execv(prog, argv) < 0)
            fprintf(stderr, "supervisor: cannot execute %s: %s\n", prog, strerror(errno));
        _exit(127);
    }
    return pid;
}

int main(int argc, char **argv)
{
    if (argc < 4) {
        usage(argv[0]);
        return EXIT_FAILURE;
    }
    char *end;
    errno = 0;
    long port = strtol(argv[2], &end, 10);
    if (errno != 0 || *end != '\0' || port < 1 || port > 65535) {
        fprintf(stderr, "supervisor: bad TCP port '%s'\n", argv[2]);
        return EXIT_FAILURE;
    }
    const char *prog = argv[1];
    char **args = argv + 2;                      /* tcp_port, num_ports and the options */
    const int nargs = argc - 2;

    sigset_t mask, orig;
    if (sigemptyset(&mask) < 0 || sigaddset(&mask, SIGINT) < 0 || sigaddset(&mask, SIGTERM) < 0 || sigaddset(&mask, SIGCHLD) < 0) {
        fprintf(stderr, "supervisor: cannot build the signal set: %s\n", strerror(errno));
        return EXIT_FAILURE;
    }
    if (sigprocmask(SIG_BLOCK, &mask, &orig) < 0) {
        fprintf(stderr, "supervisor: cannot block signals: %s\n", strerror(errno));
        return EXIT_FAILURE;
    }
    const int sfd = signalfd(-1, &mask, SFD_CLOEXEC | SFD_NONBLOCK);
    if (sfd < 0) {
        fprintf(stderr, "supervisor: signalfd failed: %s\n", strerror(errno));
        return EXIT_FAILURE;
    }

    pid_t child = -1;                            /* -1: no child running */
    long restart_at = mono_ms();                 /* when the next child may start */
    long starts[MAX_RESTARTS];                   /* times of the last restarts (not the first start) */
    int nstarts = 0;
    bool stopping = false, crashed = false, first = true;
    int result = EXIT_SUCCESS;

    for (;;) {
        if (child < 0 && !stopping && mono_ms() >= restart_at) {
            child = start_child(prog, args, nargs, &orig);
            if (child < 0) {
                result = EXIT_FAILURE;
                break;
            }
            if (first)
                fprintf(stderr, "supervisor: started %s (pid %ld)\n", prog, (long)child);
            else
                fprintf(stderr, "supervisor: restarted %s (pid %ld)\n", prog, (long)child);
            first = false;
        }
        if (child < 0 && stopping)
            break;

        struct pollfd pfd = {.fd = sfd, .events = POLLIN};
        const int timeout = child < 0 ? (int)(restart_at - mono_ms() > 0 ? restart_at - mono_ms() : 0) : -1;
        const int n = poll(&pfd, 1, timeout);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            fprintf(stderr, "supervisor: poll failed: %s\n", strerror(errno));
            result = EXIT_FAILURE;
            break;
        }

        struct signalfd_siginfo si;
        ssize_t r;
        while ((r = read(sfd, &si, sizeof si)) == (ssize_t)sizeof si) {
            if (si.ssi_signo == SIGINT || si.ssi_signo == SIGTERM) {
                fprintf(stderr, "supervisor: signal %u: stopping\n", si.ssi_signo);
                stopping = true;
                if (child >= 0 && kill(child, (int)si.ssi_signo) < 0 && errno != ESRCH)
                    fprintf(stderr, "supervisor: cannot signal the switch: %s\n", strerror(errno));
            }
        }
        if (r < 0 && errno != EAGAIN && errno != EINTR) {
            fprintf(stderr, "supervisor: reading the signalfd failed: %s\n", strerror(errno));
            result = EXIT_FAILURE;
            break;
        }

        if (child >= 0) {                        /* SIGCHLD, or any other wake-up: has the child ended? */
            int status;
            pid_t w = waitpid(child, &status, WNOHANG);

            if (w < 0 && errno != EINTR) {
                fprintf(stderr, "supervisor: waitpid failed: %s\n", strerror(errno));
                result = EXIT_FAILURE;
                break;
            }
            if (w == child) {
                child = -1;
                bool restart = false;

                if (WIFEXITED(status)) {
                    fprintf(stderr, "supervisor: the switch exited with status %d\n", WEXITSTATUS(status));
                    restart = WEXITSTATUS(status) != 0;
                    crashed = WEXITSTATUS(status) != 0;
                } else if (WIFSIGNALED(status)) {
                    const int sig = WTERMSIG(status);
                    fprintf(stderr, "supervisor: the switch was killed by signal %d\n", sig);
                    restart = sig != SIGINT && sig != SIGTERM;
                    crashed = true;              /* a signal death leaves its files behind */
                }
                if (stopping) {
                    if (restart)
                        result = EXIT_FAILURE;
                    break;
                }
                if (!restart)
                    break;                       /* a clean end of the switch ends the supervisor */

                const long now = mono_ms();      /* the restart budget: MAX_RESTARTS within RESTART_WINDOW_SEC */
                int keep = 0;
                for (int i = 0; i < nstarts; i++)
                    if (now - starts[i] < RESTART_WINDOW_SEC * 1000L)
                        starts[keep++] = starts[i];
                nstarts = keep;
                if (nstarts >= MAX_RESTARTS) {
                    fprintf(stderr, "supervisor: %d restarts within %d s: giving up\n", MAX_RESTARTS, RESTART_WINDOW_SEC);
                    result = EXIT_FAILURE;
                    break;
                }
                starts[nstarts++] = now + RESTART_DELAY_MS;
                restart_at = now + RESTART_DELAY_MS;
                fprintf(stderr, "supervisor: restarting in %d ms\n", RESTART_DELAY_MS);
            }
        }
    }

    clean_up((int)port, crashed);
    if (close(sfd) < 0)
        fprintf(stderr, "supervisor: close failed: %s\n", strerror(errno));
    return result;
}
