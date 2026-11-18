/*
 * test_stats.c - unit tests for the shared-memory counters (Part 14): src/stats.c.
 *
 * What is checked (all without a running switch, so that process-shared and robust behaviour can be tested exactly):
 *   - the object /vswitch_stats_P exists with mode 0600, and its size is PAGE + round_up(sizeof(stats_data_t), PAGE)
 *   - the control area is at offset 0, the data area at PAGE (counters seen through an independent mapping)
 *   - magic and version are published, and a reader detects a wrong magic / version
 *   - threads of one process, and other processes with their own mapping, never lose an increment
 *   - state / VLAN / mirror fields are copied in and readable from another process
 *   - the mutex is process-shared and ROBUST: a process killed while it holds it does not wedge anybody
 *   - a stale segment is replaced; the umask does not change the mode
 *   - shutdown: unlink first (the name is gone), mappings that exist keep working, the magic is cleared
 *   - a newer switch's segment is not unlinked by an older switch that shuts down
 *   - failure paths: bad arguments, bad names, a failing ftruncate: nothing is left behind
 * Nothing here is timing based: processes are synchronised with pipes, and a watchdog alarm ends a hung test.
 */
#include "stats.h"

#include <fcntl.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
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

#define NAME STATS_SHM_NAME
#define DEVPATH "/dev/shm/vswitch_stats_P"

static size_t PAGE;
static char other_name[64];                 /* a private name for the tests that must not touch the real segment */

/* ---- helpers ---- */

/* Map an existing segment the way a reader process does (own mapping, read + write because locking writes). */
static stats_ctl_t *map_segment(const char *name, size_t *len_out)
{
    int fd = shm_open(name, O_RDWR, 0);
    if (fd < 0)
        return NULL;
    struct stat st;
    if (fstat(fd, &st) < 0) {
        close(fd);
        return NULL;
    }
    void *m = mmap(NULL, (size_t)st.st_size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (m == MAP_FAILED)
        return NULL;
    if (len_out)
        *len_out = (size_t)st.st_size;
    return m;
}

static stats_port_t *ports_of(stats_ctl_t *c)
{
    return ((stats_data_t *)((char *)c + PAGE))->port;
}

static bool exists(const char *name)
{
    int fd = shm_open(name, O_RDONLY, 0);
    if (fd < 0)
        return false;
    close(fd);
    return true;
}

static ino_t inode_of(const char *name)
{
    int fd = shm_open(name, O_RDONLY, 0);
    struct stat st;
    if (fd < 0 || fstat(fd, &st) < 0) {
        if (fd >= 0)
            close(fd);
        return 0;
    }
    close(fd);
    return st.st_ino;
}

/* Wait for a child; returns its exit status, or -1000 - signal if it was killed. */
static int reap(pid_t pid)
{
    int st;
    if (waitpid(pid, &st, 0) != pid)
        return -1;
    int code = WIFEXITED(st) ? WEXITSTATUS(st) : -1000 - WTERMSIG(st);
    if (code != 0 && code != -1000 - SIGKILL)
        printf("  (child %d ended with status %d)\n", (int)pid, code);
    return code;
}

/* ---- tests ---- */

static void test_layout_and_object(void)
{
    printf("segment: name, mode 0600, size, offsets, magic and version\n");
    EXPECT(PAGE >= 4096 && (PAGE & (PAGE - 1)) == 0);
    size_t want = PAGE + (sizeof(stats_data_t) + PAGE - 1) / PAGE * PAGE;
    EXPECT(stats_shm_size(PAGE) == want);
    EXPECT(stats_shm_size(0) == 0 && stats_shm_size(100) == 0 && stats_shm_size(4097) == 0);   /* unusable page sizes */
    EXPECT(stats_shm_size(8192) == 8192 + (sizeof(stats_data_t) + 8191) / 8192 * 8192);
    EXPECT(sizeof(stats_ctl_t) <= PAGE);
    EXPECT(offsetof(stats_ctl_t, magic) == 0);                  /* the control structure is at offset 0 */
    EXPECT(sizeof(stats_port_t) == 4 * 8 + 3 * 4 + 4);          /* 4 x u64, 3 x int (+ alignment padding) */

    EXPECT(!exists(NAME));
    EXPECT(stats_init(MAX_PORTS) == 0);
    EXPECT(exists(NAME));
    EXPECT(access(DEVPATH, F_OK) == 0);                         /* visible in /dev/shm (ls -l /dev/shm) */

    struct stat st;
    EXPECT(stat(DEVPATH, &st) == 0);
    EXPECT((st.st_mode & 07777) == 0600);                       /* same user only */
    EXPECT(st.st_uid == getuid());
    EXPECT((size_t)st.st_size == want);                         /* exactly PAGE + round_up(sizeof(stats_data_t), PAGE) */
    EXPECT(st.st_size % (off_t)PAGE == 0);

    size_t len = 0;
    stats_ctl_t *c = map_segment(NAME, &len);
    EXPECT(c != NULL && len == want);
    if (c == NULL)
        return;
    EXPECT(c->magic == STATS_MAGIC && c->version == STATS_VERSION && c->num_ports == MAX_PORTS);
    EXPECT(stats_validate(c) == 0);

    /* the data area starts exactly at PAGE: a counter written through the API appears there, and only there */
    stats_inc(3, STAT_IN);
    stats_inc(3, STAT_OUT);
    stats_inc(3, STAT_OUT);
    stats_inc(3, STAT_DROP);
    stats_inc(3, STAT_MIRROR);
    stats_inc(3, STAT_MIRROR);
    stats_inc(3, STAT_MIRROR);
    stats_port_t *p = ports_of(c);
    EXPECT(p[3].in == 1 && p[3].out == 2 && p[3].dropped == 1 && p[3].mirrored == 3);
    EXPECT(p[2].in == 0 && p[4].in == 0 && p[0].in == 0);
    stats_inc(MAX_PORTS, STAT_DROP);                            /* the last slot of the array */
    EXPECT(p[MAX_PORTS].dropped == 1);
    /* the control page beyond the structure is zero, the data of the other ports is untouched */
    bool pad_zero = true;
    for (size_t i = sizeof(stats_ctl_t); i < PAGE; i++)
        if (((unsigned char *)c)[i] != 0)
            pad_zero = false;
    EXPECT(pad_zero);
    munmap(c, len);
    stats_destroy();
}

static void test_validate(void)
{
    printf("reader: invalid magic / version are detected\n");
    stats_ctl_t good;
    memset(&good, 0, sizeof good);
    good.magic = STATS_MAGIC;
    good.version = STATS_VERSION;
    EXPECT(stats_validate(&good) == 0);
    stats_ctl_t bad = good;
    bad.magic = 0;                                              /* not published yet / shut down */
    EXPECT(stats_validate(&bad) == EINVAL);
    bad.magic = STATS_MAGIC ^ 1;
    EXPECT(stats_validate(&bad) == EINVAL);
    bad = good;
    bad.version = STATS_VERSION + 1;
    EXPECT(stats_validate(&bad) == EPROTO);
    bad.version = 0;
    EXPECT(stats_validate(&bad) == EPROTO);

    /* a segment that somebody else created (not by the switch): zero-filled, so the magic is wrong */
    int fd = shm_open(other_name, O_CREAT | O_EXCL | O_RDWR, 0600);
    EXPECT(fd >= 0);
    if (fd >= 0) {
        EXPECT(ftruncate(fd, (off_t)stats_shm_size(PAGE)) == 0);
        stats_ctl_t *c = mmap(NULL, stats_shm_size(PAGE), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        EXPECT(c != MAP_FAILED && stats_validate(c) == EINVAL);
        if (c != MAP_FAILED)
            munmap(c, stats_shm_size(PAGE));
        close(fd);
    }
    shm_unlink(other_name);
}

static void *inc_worker(void *arg)
{
    int port = (int)(intptr_t)arg;
    for (int i = 0; i < 5000; i++) {
        stats_inc(port, STAT_IN);
        stats_inc(port, STAT_OUT);
        stats_inc(port, STAT_DROP);
        stats_inc(port, STAT_MIRROR);
        stats_inc(7, STAT_IN);                                  /* every thread also hits ONE shared counter */
    }
    return NULL;
}

static void test_threads(void)
{
    printf("counters: 8 threads, four counters, no lost increments\n");
    EXPECT(stats_init(MAX_PORTS) == 0);
    pthread_t th[8];
    for (intptr_t i = 0; i < 8; i++)
        CHECK_PT(pthread_create(&th[i], NULL, inc_worker, (void *)(i + 1)));
    for (int i = 0; i < 8; i++)
        CHECK_PT(pthread_join(th[i], NULL));
    bool ok = true;
    for (int p = 1; p <= 8; p++) {
        unsigned long long extra = p == 7 ? 8 * 5000 : 0;
        if (stats_get(p, STAT_IN) != 5000 + extra || stats_get(p, STAT_OUT) != 5000 ||
            stats_get(p, STAT_DROP) != 5000 || stats_get(p, STAT_MIRROR) != 5000)
            ok = false;
    }
    EXPECT(ok);
    EXPECT(stats_get(9, STAT_IN) == 0);
    stats_destroy();
}

#define CHILDREN 4
#define PER_CHILD 20000

static void test_other_processes(void)
{
    printf("processes: other processes with their own mapping update and read the same counters\n");
    EXPECT(stats_init(MAX_PORTS) == 0);
    pid_t pid[CHILDREN];
    for (int c = 0; c < CHILDREN; c++) {
        pid[c] = fork();
        if (pid[c] == 0) {
            size_t len;
            stats_ctl_t *ctl = map_segment(NAME, &len);
            if (ctl == NULL || stats_validate(ctl) != 0)
                _exit(2);
            stats_port_t *p = ports_of(ctl);
            for (int i = 0; i < PER_CHILD; i++) {
                if (stats_lock(&ctl->lock) != 0)
                    _exit(3);
                p[5].in++;
                p[5].out += 2;
                stats_unlock(&ctl->lock);
            }
            _exit(0);
        }
    }
    /* meanwhile this process updates the same counters through the API, from two threads */
    pthread_t th[2];
    for (intptr_t i = 0; i < 2; i++)
        CHECK_PT(pthread_create(&th[i], NULL, inc_worker, (void *)5));
    for (int i = 0; i < 2; i++)
        CHECK_PT(pthread_join(th[i], NULL));
    int bad = 0;
    for (int c = 0; c < CHILDREN; c++)
        bad += reap(pid[c]) != 0;
    EXPECT(bad == 0);
    EXPECT(stats_get(5, STAT_IN) == (unsigned long long)CHILDREN * PER_CHILD + 2 * 5000);
    EXPECT(stats_get(5, STAT_OUT) == (unsigned long long)CHILDREN * PER_CHILD * 2 + 2 * 5000);
    EXPECT(stats_get(5, STAT_DROP) == 2 * 5000 && stats_get(5, STAT_MIRROR) == 2 * 5000);

    /* a separate reader process sees the exact values (all four counters) */
    stats_inc(2, STAT_IN);
    stats_inc(2, STAT_OUT);
    stats_inc(2, STAT_OUT);
    stats_inc(2, STAT_DROP);
    stats_inc(2, STAT_DROP);
    stats_inc(2, STAT_DROP);
    for (int i = 0; i < 4; i++)
        stats_inc(2, STAT_MIRROR);
    pid_t r = fork();
    if (r == 0) {
        stats_ctl_t *ctl = map_segment(NAME, NULL);
        if (ctl == NULL || stats_validate(ctl) != 0 || stats_lock(&ctl->lock) != 0)
            _exit(2);
        stats_port_t *p = ports_of(ctl);
        int ok = p[2].in == 1 && p[2].out == 2 && p[2].dropped == 3 && p[2].mirrored == 4;
        stats_unlock(&ctl->lock);
        _exit(ok ? 0 : 1);
    }
    EXPECT(reap(r) == 0);
    stats_destroy();
}

static void test_port_fields(void)
{
    printf("port fields: state, VLAN and mirror_to are copied in and readable from another process\n");
    EXPECT(stats_init(8) == 0);
    int st, vl, mi;
    EXPECT(stats_get_port(3, &st, &vl, &mi) == 0 && st == 0 && vl == 0 && mi == 0);
    EXPECT(stats_set_state(3, 1) == 0);
    EXPECT(stats_set_vlan(3, 4094) == 0);
    EXPECT(stats_set_mirror(3, 7) == 0);
    EXPECT(stats_get_port(3, &st, &vl, &mi) == 0 && st == 1 && vl == 4094 && mi == 7);
    EXPECT(stats_set_state(3, 3) == 0 && stats_set_vlan(3, 10) == 0 && stats_set_mirror(3, 0) == 0);
    EXPECT(stats_get_port(3, &st, &vl, &mi) == 0 && st == 3 && vl == 10 && mi == 0);
    EXPECT(stats_get_port(4, &st, &vl, &mi) == 0 && st == 0 && vl == 0 && mi == 0);          /* other ports untouched */
    EXPECT(stats_set_state(0, 2) == 0 && stats_set_state(8, 2) == 0);                        /* ports 0..max */
    EXPECT(stats_set_state(9, 1) == EINVAL && stats_set_vlan(-1, 1) == EINVAL && stats_set_mirror(99, 1) == EINVAL);
    EXPECT(stats_get_port(9, &st, &vl, &mi) == EINVAL);
    stats_inc(3, STAT_IN);
    EXPECT(stats_get(3, STAT_IN) == 1);                         /* counters and fields do not disturb each other */

    stats_ctl_t *c = map_segment(NAME, NULL);
    EXPECT(c != NULL && c->num_ports == 8);
    if (c != NULL) {
        stats_port_t *p = ports_of(c);
        EXPECT(p[3].state == 3 && p[3].vlan == 10 && p[3].mirror_to == 0 && p[3].in == 1);
        EXPECT(p[0].state == 2 && p[8].state == 2);
        munmap(c, stats_shm_size(PAGE));
    }
    stats_destroy();
}

static void *blocked_inc(void *arg)
{
    *(unsigned long long *)arg = stats_inc(1, STAT_IN);         /* blocks while the dead child's lock is held */
    return NULL;
}

/* A child maps the segment, locks the mutex, tells the parent and waits to be killed (SIGKILL): the lock is
 * held by a dead process. */
static pid_t child_dies_holding_lock(int *go_fd)
{
    int ready[2], go[2];
    CHECK(pipe(ready));
    CHECK(pipe(go));
    pid_t pid = fork();
    if (pid == 0) {
        close(ready[0]);
        close(go[1]);
        stats_ctl_t *ctl = map_segment(NAME, NULL);
        if (ctl == NULL || stats_lock(&ctl->lock) != 0)
            _exit(2);
        ports_of(ctl)[1].in += 1000;                            /* a half-finished "update" under the lock */
        char b = 1;
        if (write(ready[1], &b, 1) != 1)
            _exit(3);
        read(go[0], &b, 1);                                     /* never returns: the parent kills us */
        _exit(4);
    }
    close(ready[1]);
    close(go[0]);
    char b;
    if (read(ready[0], &b, 1) != 1)
        printf("  (child did not report)\n");
    close(ready[0]);
    *go_fd = go[1];
    return pid;
}

static void test_robust_mutex(void)
{
    printf("robust mutex: a process that dies holding the lock does not wedge the switch\n");
    alarm(60);                                                  /* watchdog: a wedged mutex would hang here forever */
    EXPECT(stats_init(MAX_PORTS) == 0);
#ifdef __GLIBC__
    {   /* glibc keeps the attributes in the mutex kind: 16 = robust, 128 = process-shared (nptl/pthreadP.h) */
        stats_ctl_t *k = map_segment(NAME, NULL);
        EXPECT(k != NULL && (k->lock.__data.__kind & 16) != 0 && (k->lock.__data.__kind & 128) != 0);
        if (k != NULL)
            munmap(k, stats_shm_size(PAGE));
    }
#endif
    EXPECT(stats_inc(1, STAT_IN) == 1);

    /* (a) the owner is dead before anybody asks for the lock */
    int go;
    pid_t pid = child_dies_holding_lock(&go);
    kill(pid, SIGKILL);
    EXPECT(reap(pid) == -1000 - SIGKILL);
    close(go);
    EXPECT(stats_inc(1, STAT_IN) == 1002);                      /* EOWNERDEAD recovered: 1 + 1000 (child) + 1 */
    EXPECT(stats_inc(1, STAT_IN) == 1003);                      /* and the mutex works normally afterwards */
    EXPECT(stats_get(1, STAT_IN) == 1003);

    /* (b) a thread is already waiting for the lock when the owner dies */
    pid = child_dies_holding_lock(&go);
    unsigned long long v = 0;
    pthread_t th;
    CHECK_PT(pthread_create(&th, NULL, blocked_inc, &v));
    kill(pid, SIGKILL);
    EXPECT(reap(pid) == -1000 - SIGKILL);
    CHECK_PT(pthread_join(th, NULL));
    close(go);
    EXPECT(v == 2004);                                          /* 1003 + 1000 + 1 */
    EXPECT(stats_get(1, STAT_IN) == 2004);

    /* (c) a second process can still lock it, and one that is killed twice in a row is recovered twice */
    pid = child_dies_holding_lock(&go);
    kill(pid, SIGKILL);
    reap(pid);
    close(go);
    pid_t r = fork();
    if (r == 0) {
        stats_ctl_t *ctl = map_segment(NAME, NULL);
        if (ctl == NULL || stats_lock(&ctl->lock) != 0)         /* EOWNERDEAD handled inside stats_lock */
            _exit(2);
        ports_of(ctl)[2].out = 42;
        stats_unlock(&ctl->lock);
        _exit(0);
    }
    EXPECT(reap(r) == 0);
    EXPECT(stats_get(2, STAT_OUT) == 42);
    EXPECT(stats_get(1, STAT_IN) == 3004);

    /* (d) the mutex is really process-shared: while another process holds it, trylock from here fails with EBUSY,
     *     and after the other process releases it, it can be taken */
    int ready[2], go2[2];
    CHECK(pipe(ready));
    CHECK(pipe(go2));
    pid = fork();
    if (pid == 0) {
        stats_ctl_t *ctl = map_segment(NAME, NULL);
        if (ctl == NULL || stats_lock(&ctl->lock) != 0)
            _exit(2);
        char b = 1;
        if (write(ready[1], &b, 1) != 1)
            _exit(3);
        read(go2[0], &b, 1);                                    /* wait for the parent's "release" */
        stats_unlock(&ctl->lock);
        close(ready[0]); close(ready[1]); close(go2[0]); close(go2[1]);   /* valgrind --track-fds: leave nothing open */
        _exit(0);
    }
    char b;
    EXPECT(read(ready[0], &b, 1) == 1);
    stats_ctl_t *ctl = map_segment(NAME, NULL);
    EXPECT(ctl != NULL);
    if (ctl != NULL) {
        EXPECT(pthread_mutex_trylock(&ctl->lock) == EBUSY);     /* held by the other process */
        /* a waiter in THIS process must be woken when the OTHER process unlocks (a private futex would never wake) */
        pthread_t waiter;
        unsigned long long got = 0;
        CHECK_PT(pthread_create(&waiter, NULL, blocked_inc, &got));
        b = 1;
        EXPECT(write(go2[1], &b, 1) == 1);
        CHECK_PT(pthread_join(waiter, NULL));
        EXPECT(got == 3005);                                    /* 3004 + 1 */
        EXPECT(reap(pid) == 0);
        EXPECT(pthread_mutex_trylock(&ctl->lock) == 0);         /* free now */
        stats_unlock(&ctl->lock);
        munmap(ctl, stats_shm_size(PAGE));
    }
    close(ready[0]); close(ready[1]); close(go2[0]); close(go2[1]);

    stats_destroy();
    alarm(0);
}

static void test_stale_segment(void)
{
    printf("start-up: a stale segment (junk, or left by a crashed switch) is replaced\n");
    /* (a) a junk object with the real name, wrong mode and wrong size */
    int fd = shm_open(NAME, O_CREAT | O_RDWR, 0644);
    EXPECT(fd >= 0);
    if (fd >= 0) {
        EXPECT(ftruncate(fd, 100) == 0);
        EXPECT(write(fd, "garbage", 7) == 7);
        close(fd);
    }
    ino_t stale = inode_of(NAME);
    EXPECT(stats_init(MAX_PORTS) == 0);
    EXPECT(inode_of(NAME) != 0 && inode_of(NAME) != stale);     /* a different object: the stale one was unlinked */
    struct stat st;
    EXPECT(stat(DEVPATH, &st) == 0 && (st.st_mode & 07777) == 0600 && (size_t)st.st_size == stats_shm_size(PAGE));
    stats_ctl_t *c = map_segment(NAME, NULL);
    EXPECT(c != NULL && stats_validate(c) == 0);
    if (c != NULL)
        munmap(c, stats_shm_size(PAGE));
    stats_destroy();
    EXPECT(!exists(NAME));

    /* (b) a "crash": a process initialises and counts, then exits without stats_destroy (in a real crash not even
     *     the atexit handler runs: use _exit) */
    pid_t pid = fork();
    if (pid == 0) {
        if (stats_init(MAX_PORTS) != 0)
            _exit(2);
        stats_inc(4, STAT_IN);
        stats_inc(4, STAT_IN);
        _exit(0);
    }
    EXPECT(reap(pid) == 0);
    EXPECT(exists(NAME));                                       /* the crash left it behind */
    stale = inode_of(NAME);
    EXPECT(stats_init(MAX_PORTS) == 0);
    EXPECT(inode_of(NAME) != stale);
    EXPECT(stats_get(4, STAT_IN) == 0);                         /* fresh counters */
    stats_destroy();

    /* (c) a crash while the mutex was held: the stale object has a locked mutex; the new one is a new object */
    EXPECT(stats_init(MAX_PORTS) == 0);
    int go;
    pid = child_dies_holding_lock(&go);
    kill(pid, SIGKILL);
    reap(pid);
    close(go);
    stats_ctl_t *old = map_segment(NAME, NULL);                 /* keep the stale mapping ... */
    EXPECT(old != NULL);
    stats_destroy();                                            /* ... the switch "restarts" */
    EXPECT(stats_init(MAX_PORTS) == 0);
    EXPECT(stats_inc(1, STAT_IN) == 1);                         /* not blocked, not recovered: a clean new mutex */
    if (old != NULL)
        munmap(old, stats_shm_size(PAGE));
    stats_destroy();

    /* (d) the umask does not change the mode */
    mode_t old_umask = umask(0);
    EXPECT(stats_init(MAX_PORTS) == 0);
    EXPECT(stat(DEVPATH, &st) == 0 && (st.st_mode & 07777) == 0600);
    stats_destroy();
    umask(0777);
    EXPECT(stats_init(MAX_PORTS) == 0);
    EXPECT(stat(DEVPATH, &st) == 0 && (st.st_mode & 07777) == 0600);
    stats_destroy();
    umask(old_umask);
}

static void test_shutdown(void)
{
    printf("shutdown: unlink first, existing mappings keep working, the magic is cleared, safe twice\n");
    EXPECT(stats_init(MAX_PORTS) == 0);
    stats_inc(1, STAT_IN);
    stats_ctl_t *c = map_segment(NAME, NULL);                   /* a reader that mapped it before shutdown */
    EXPECT(c != NULL && stats_validate(c) == 0);
    stats_destroy();
    EXPECT(!exists(NAME));                                      /* nobody can open it any more */
    EXPECT(access(DEVPATH, F_OK) != 0);
    if (c != NULL) {
        EXPECT(ports_of(c)[1].in == 1);                         /* the memory is still there for the old mapping */
        EXPECT(stats_validate(c) == EINVAL);                    /* but it says "shut down" */
        munmap(c, stats_shm_size(PAGE));
    }
    EXPECT(stats_get(1, STAT_IN) == 0);                         /* API after destroy: ignored */
    EXPECT(stats_inc(1, STAT_IN) == 0);
    EXPECT(stats_set_vlan(1, 5) == EINVAL);
    stats_destroy();                                            /* twice: no crash, no error */
    stats_destroy();
    EXPECT(!exists(NAME));

    /* re-initialisation without destroy starts again (and leaves exactly one segment) */
    EXPECT(stats_init(8) == 0);
    stats_inc(1, STAT_IN);
    EXPECT(stats_init(MAX_PORTS) == 0);
    EXPECT(stats_get(1, STAT_IN) == 0);
    stats_destroy();
    EXPECT(!exists(NAME));
}

static void test_newer_segment_kept(void)
{
    printf("shutdown: an older switch does not unlink the segment of a newer one\n");
    int up[2], down[2];
    CHECK(pipe(up));
    CHECK(pipe(down));
    pid_t pid = fork();
    if (pid == 0) {                                             /* "switch A": started first */
        if (stats_init(MAX_PORTS) != 0)
            _exit(2);
        char b = 1;
        if (write(up[1], &b, 1) != 1)
            _exit(3);
        if (read(down[0], &b, 1) != 1)
            _exit(4);
        stats_destroy();                                        /* A shuts down while B runs */
        close(up[0]); close(up[1]); close(down[0]); close(down[1]);
        _exit(0);
    }
    char b;
    EXPECT(read(up[0], &b, 1) == 1);
    EXPECT(stats_init(MAX_PORTS) == 0);                         /* "switch B": A's segment looks stale, B replaces it */
    stats_inc(1, STAT_IN);
    b = 1;
    EXPECT(write(down[1], &b, 1) == 1);
    EXPECT(reap(pid) == 0);                                     /* A has finished its shutdown */
    EXPECT(exists(NAME));                                       /* B's segment is still there */
    stats_ctl_t *c = map_segment(NAME, NULL);
    EXPECT(c != NULL && stats_validate(c) == 0 && ports_of(c)[1].in == 1);
    if (c != NULL)
        munmap(c, stats_shm_size(PAGE));
    stats_destroy();                                            /* B removes its own */
    EXPECT(!exists(NAME));
    close(up[0]); close(up[1]); close(down[0]); close(down[1]);
}

static int ftruncate_fails_child(void)
{
    signal(SIGXFSZ, SIG_IGN);                                   /* ftruncate beyond the limit: EFBIG instead of a signal */
    struct rlimit rl = { 0, 0 };
    if (setrlimit(RLIMIT_FSIZE, &rl) != 0)
        return 2;
    int rc = stats_init_named(other_name, MAX_PORTS);
    if (rc == 0)
        return 3;                                               /* must fail */
    if (exists(other_name))
        return 4;                                               /* the half-created object must be gone */
    if (stats_inc(1, STAT_IN) != 0 || stats_get(1, STAT_IN) != 0)
        return 5;                                               /* and nothing is initialised */
    return 0;
}

static void test_failures(void)
{
    printf("failure paths: nothing is left behind\n");
    EXPECT(stats_init(-1) == EINVAL);
    EXPECT(stats_init(MAX_PORTS + 1) == EINVAL);
    EXPECT(stats_init_named(NULL, 4) == EINVAL);
    EXPECT(stats_init_named("", 4) == EINVAL);
    EXPECT(stats_init_named("noslash", 4) == EINVAL);
    EXPECT(stats_init_named("/a/b", 4) != 0);                   /* shm_open refuses a slash inside the name */
    char longname[400];
    longname[0] = '/';
    memset(longname + 1, 'x', sizeof longname - 2);
    longname[sizeof longname - 1] = '\0';
    EXPECT(stats_init_named(longname, 4) == EINVAL);
    EXPECT(!exists(NAME) && !exists(other_name));
    EXPECT(stats_inc(1, STAT_IN) == 0);                         /* still not initialised */

    pid_t pid = fork();                                         /* ftruncate fails: the object created before it is removed */
    if (pid == 0)
        _exit(ftruncate_fails_child());
    EXPECT(reap(pid) == 0);
    EXPECT(!exists(other_name));

    /* a failed re-init must not break a good segment's neighbours: init works again afterwards */
    EXPECT(stats_init(4) == 0);
    EXPECT(stats_init(MAX_PORTS + 5) == EINVAL);                /* argument check happens before anything is touched */
    EXPECT(stats_inc(4, STAT_IN) == 1);
    stats_destroy();
    EXPECT(!exists(NAME));
}

int main(void)
{
    setvbuf(stdout, NULL, _IOLBF, 0);                           /* children forked later must not inherit unflushed output */
    PAGE = stats_page_size();
    snprintf(other_name, sizeof other_name, "/vswitch_stats_T%d", (int)getpid());
    shm_unlink(NAME);                                           /* start from a clean state (a crashed earlier test) */

    test_layout_and_object();
    test_validate();
    test_threads();
    test_other_processes();
    test_port_fields();
    test_robust_mutex();
    test_stale_segment();
    test_shutdown();
    test_newer_segment_kept();
    test_failures();

    EXPECT(!exists(NAME));                                      /* the whole run leaves nothing behind */
    printf("\n%d checks, %d failures\n", checks, failures);
    printf(failures == 0 ? "ALL PASSED\n" : "SOME FAILED\n");
    return failures == 0 ? 0 : 1;
}
