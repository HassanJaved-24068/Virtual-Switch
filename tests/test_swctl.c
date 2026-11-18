/*
 * test_swctl.c - Part 15: the shared-memory side of swctl, without a switch.
 *
 * The test plays the switch (stats_init creates /vswitch_stats_P) and runs the real ./swctl against it:
 *   1. swctl stats prints the counters that were set (and exits 0);
 *   2. the data mapping of swctl is read-only: a process that maps the segment the same way (offset PAGE, PROT_READ, MAP_SHARED) and writes a counter
 *      gets SIGSEGV, while the control mapping (PROT_READ|PROT_WRITE) lets it lock and unlock the mutex;
 *   3. a process killed with SIGKILL while it holds the lock: swctl stats still works (EOWNERDEAD -> pthread_mutex_consistent), exit 0, and the
 *      mutex is usable by everybody afterwards;
 *   4. swctl stats does not print while it holds the lock: another process holding the lock for 1.5 s delays swctl, which then prints;
 *   5. swctl with no segment: exit 2 and "switch not running".
 * Needs ./swctl (make all). Run from the project directory.
 */
#include "stats.h"

#include <fcntl.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

static int checks = 0, failures = 0;

#define EXPECT(cond)                                                       \
    do {                                                                   \
        checks++;                                                          \
        if (!(cond)) {                                                     \
            failures++;                                                    \
            printf("  FAIL line %d: %s\n", __LINE__, #cond);               \
        }                                                                  \
    } while (0)

/* Run ./swctl with args; stdout goes to out, stderr to err (both NUL-terminated). Returns the exit status or -1000-signal. */
static int run_swctl(char *const args[], char *out, size_t on, char *err, size_t en, int timeout_s)
{
    int po[2], pe[2];
    CHECK(pipe(po));
    CHECK(pipe(pe));
    pid_t pid = fork();
    CHECK(pid);
    if (pid == 0) {
        alarm((unsigned)timeout_s);
        dup2(po[1], 1);
        dup2(pe[1], 2);
        close(po[0]); close(po[1]); close(pe[0]); close(pe[1]);
        execv("./swctl", args);
        _exit(127);
    }
    close(po[1]);
    close(pe[1]);
    size_t lo = 0, le = 0;
    ssize_t n;
    while ((n = read(po[0], out + lo, on - 1 - lo)) > 0)
        lo += (size_t)n;
    while ((n = read(pe[0], err + le, en - 1 - le)) > 0)
        le += (size_t)n;
    out[lo] = err[le] = '\0';
    close(po[0]);
    close(pe[0]);
    int st;
    CHECK(waitpid(pid, &st, 0));
    return WIFEXITED(st) ? WEXITSTATUS(st) : -1000 - WTERMSIG(st);
}

static const char *const STATS[] = { "./swctl", "stats", NULL };

/* map the segment the way swctl does */
static int map_like_swctl(stats_ctl_t **ctl, const stats_data_t **data)
{
    size_t page = stats_page_size(), len = stats_shm_size(page);
    int fd = shm_open(STATS_SHM_NAME, O_RDWR, 0);
    if (fd < 0)
        return -1;
    void *c = mmap(NULL, page, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    void *d = mmap(NULL, len - page, PROT_READ, MAP_SHARED, fd, (off_t)page);
    close(fd);
    if (c == MAP_FAILED || d == MAP_FAILED)
        return -1;
    *ctl = c;
    *data = d;
    return 0;
}

static void test_stats_output(void)
{
    char out[8192], err[1024];
    printf("swctl stats prints the counters of the segment\n");
    CHECK_PT(stats_init(4));
    for (int i = 0; i < 7; i++)
        stats_inc(1, STAT_IN);
    for (int i = 0; i < 3; i++)
        stats_inc(2, STAT_OUT);
    stats_inc(3, STAT_DROP);
    stats_inc(3, STAT_MIRROR);
    stats_set_state(1, 1);
    stats_set_vlan(1, 10);
    int rc = run_swctl((char *const *)STATS, out, sizeof out, err, sizeof err, 20);
    EXPECT(rc == 0);
    EXPECT(strstr(out, "PORT") != NULL && strstr(out, "MIRRORED") != NULL);
    EXPECT(strstr(out, "UP") != NULL && strstr(out, "10") != NULL);
    int p, in, o, d, m;
    char st[16], vl[16];
    int found = 0;
    for (char *l = strtok(out, "\n"); l; l = strtok(NULL, "\n")) {
        if (sscanf(l, "%d %15s %15s %d %d %d %d", &p, st, vl, &in, &o, &d, &m) == 7) {
            found++;
            if (p == 1) EXPECT(in == 7 && o == 0 && d == 0 && m == 0);
            if (p == 2) EXPECT(in == 0 && o == 3);
            if (p == 3) EXPECT(d == 1 && m == 1);
            if (p == 4) EXPECT(in == 0 && o == 0 && d == 0 && m == 0);
        }
    }
    EXPECT(found == 4);                                /* exactly the 4 ports of this segment */
}

static void test_mappings(void)
{
    printf("the mappings: control area writable (mutex), data area read-only (SIGSEGV on a write)\n");
    stats_ctl_t *ctl;
    const stats_data_t *data;
    EXPECT(map_like_swctl(&ctl, &data) == 0);
    EXPECT(stats_validate(ctl) == 0);
    EXPECT(stats_lock(&ctl->lock) == 0);               /* writes to the control mapping: no SIGSEGV */
    unsigned long long before = data->port[1].in;
    stats_unlock(&ctl->lock);
    EXPECT(before == 7);
    EXPECT(stats_get(1, STAT_IN) == 7);

    /* a child writes through the read-only data mapping */
    fflush(stdout);
    pid_t pid = fork();
    CHECK(pid);
    if (pid == 0) {
        volatile uint64_t *w = (volatile uint64_t *)&data->port[1].in;
        *w = 12345;
        _exit(0);                                      /* not reached */
    }
    int st;
    CHECK(waitpid(pid, &st, 0));
    EXPECT(WIFSIGNALED(st) && WTERMSIG(st) == SIGSEGV);
    EXPECT(stats_get(1, STAT_IN) == 7);                /* nothing changed */

    /* what the REAL swctl maps (r--s at offset PAGE) is checked through /proc/<pid>/maps in tests/test_admin.py */
    munmap(ctl, stats_page_size());
    munmap((void *)data, stats_shm_size(stats_page_size()) - stats_page_size());
}

static void test_dead_owner(void)
{
    char out[8192], err[1024];
    printf("a process killed while it holds the lock: swctl recovers the mutex (EOWNERDEAD -> consistent)\n");
    int ready[2];
    CHECK(pipe(ready));
    fflush(stdout);
    pid_t pid = fork();
    CHECK(pid);
    if (pid == 0) {
        stats_ctl_t *ctl;
        const stats_data_t *data;
        if (map_like_swctl(&ctl, &data) != 0 || stats_lock(&ctl->lock) != 0)
            _exit(2);
        char c = 'x';
        if (write(ready[1], &c, 1) != 1)
            _exit(3);
        for (;;)
            pause();
    }
    char c;
    CHECK(read(ready[0], &c, 1) == 1 ? 0 : -1);
    kill(pid, SIGKILL);
    int st;
    CHECK(waitpid(pid, &st, 0));
    EXPECT(WIFSIGNALED(st) && WTERMSIG(st) == SIGKILL);

    int rc = run_swctl((char *const *)STATS, out, sizeof out, err, sizeof err, 20);
    EXPECT(rc == 0);                                   /* not a hang (alarm would kill it), not an error */
    EXPECT(strstr(out, "PORT") != NULL);
    EXPECT(err[0] == '\0');
    stats_inc(1, STAT_IN);                             /* the switch's own lock still works */
    rc = run_swctl((char *const *)STATS, out, sizeof out, err, sizeof err, 20);
    EXPECT(rc == 0);
    close(ready[0]);
    close(ready[1]);
}

static void test_lock_not_held_while_printing(void)
{
    char out[8192], err[1024];
    printf("a reader waits for the lock (it is not skipped) and prints after it\n");
    int ready[2];
    CHECK(pipe(ready));
    fflush(stdout);
    pid_t pid = fork();
    CHECK(pid);
    if (pid == 0) {
        stats_ctl_t *ctl;
        const stats_data_t *data;
        if (map_like_swctl(&ctl, &data) != 0 || stats_lock(&ctl->lock) != 0)
            _exit(2);
        char c = 'x';
        if (write(ready[1], &c, 1) != 1)
            _exit(3);
        usleep(1500 * 1000);
        stats_unlock(&ctl->lock);
        _exit(0);
    }
    char c;
    CHECK(read(ready[0], &c, 1) == 1 ? 0 : -1);
    struct timespec t0 = now_mono();
    int rc = run_swctl((char *const *)STATS, out, sizeof out, err, sizeof err, 20);
    long ms = ts_diff_ms(now_mono(), t0);
    EXPECT(rc == 0);
    EXPECT(ms >= 1000);                                /* it really waited for the lock */
    EXPECT(strstr(out, "PORT") != NULL);
    int st;
    CHECK(waitpid(pid, &st, 0));
    EXPECT(WIFEXITED(st) && WEXITSTATUS(st) == 0);
    close(ready[0]);
    close(ready[1]);
}

static void test_no_segment(void)
{
    char out[8192], err[1024];
    printf("no segment: exit 2, 'switch not running'\n");
    stats_destroy();
    int rc = run_swctl((char *const *)STATS, out, sizeof out, err, sizeof err, 20);
    EXPECT(rc == 2);
    EXPECT(strstr(err, "switch not running") != NULL);
    EXPECT(out[0] == '\0');
    char *const w[] = { "./swctl", "stats", "--watch", NULL };
    rc = run_swctl(w, out, sizeof out, err, sizeof err, 20);
    EXPECT(rc == 2);
}

int main(void)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (access("./swctl", X_OK) != 0) {
        printf("test_swctl: ./swctl is missing (make all)\n");
        return 1;
    }
    test_stats_output();
    test_mappings();
    test_dead_owner();
    test_lock_not_held_while_printing();
    test_no_segment();
    printf("%d checks, %d failures\n%s\n", checks, failures, failures ? "FAILED" : "ALL PASSED");
    return failures ? 1 : 0;
}
