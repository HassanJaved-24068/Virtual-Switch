/*
 * test_port.c - port lifecycle tests (Part 5): port_start, port_request_down,
 * the reaper, the free-port semaphore and the teardown notification.
 *
 * Hosts are loopback TCP connections created inside the test. Waiting is done
 * with port_wait_down() (the same notification an admin command will use),
 * never with sleep. Meant to be run normally, under valgrind and under
 * ThreadSanitizer.
 */
#include "forward.h"
#include "frame.h"
#include "port.h"
#include "stats.h"

#include <arpa/inet.h>
#include <dirent.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <signal.h>
#include <sys/socket.h>
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

#define WAIT_MS 10000

/* ---- helpers ---- */

static int count_dir(const char *path)
{
    DIR *d = opendir(path);
    int n = 0;
    struct dirent *e;

    if (d == NULL)
        return -1;
    while ((e = readdir(d)) != NULL)
        if (e->d_name[0] != '.')
            n++;
    closedir(d);
    return n - 1;                       /* minus the descriptor used by opendir itself */
}

static int open_fds(void)    { return count_dir("/proc/self/fd"); }
static int open_threads(void) { return count_dir("/proc/self/task") + 1; }

/*
 * After pthread_join the kernel may need a moment before the thread's entry
 * disappears from /proc/self/task. Wait (at most 5 s) until the number of
 * threads is `want`; return the number seen at the end.
 */
static int threads_settle(int want)
{
    for (int i = 0; i < 5000; i++) {
        int n = open_threads();
        if (n == want)
            return n;
        usleep(1000);                   /* test code: polling /proc, not synchronisation */
    }
    return open_threads();
}

/* Threads of the test process itself (main, plus helper threads of a sanitizer),
 * measured once at the start, before any switch thread exists. */
static int base_threads;

/* Thread count expected right after set_up(): the base plus the reaper. Waits for
 * the threads of the previous test to disappear from /proc first. */
static int baseline_with_reaper(void)
{
    int want = base_threads + 1;
    return threads_settle(want);
}

static int sem_value(void)
{
    int v = -1;
    CHECK(sem_getvalue(&sw.free_ports, &v));
    return v;
}

/* Loopback TCP connection: *host is the "host" end, *sw_fd the switch end. */
static void tcp_pair(int *host, int *sw_fd)
{
    int ls, one = 1;
    struct sockaddr_in a;
    socklen_t al = sizeof a;

    CHECK(ls = socket(AF_INET, SOCK_STREAM, 0));
    CHECK(setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one));
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    CHECK(bind(ls, (struct sockaddr *)&a, sizeof a));
    CHECK(listen(ls, 4));
    CHECK(getsockname(ls, (struct sockaddr *)&a, &al));
    CHECK(*host = socket(AF_INET, SOCK_STREAM, 0));
    CHECK(connect(*host, (struct sockaddr *)&a, sizeof a));
    CHECK(*sw_fd = accept(ls, NULL, NULL));
    CHECK(close(ls));
}

/* Read a port's state the way the switch does: under ports_lock (read side). */
static enum port_state port_state(int id)
{
    enum port_state st;

    CHECK_PT(pthread_rwlock_rdlock(&sw.ports_lock));
    st = sw.ports[id].state;
    CHECK_PT(pthread_rwlock_unlock(&sw.ports_lock));
    return st;
}

static void set_up(int nports)
{
    switch_init(nports);
    CHECK_PT(stats_init(MAX_PORTS) == 0 ? 0 : EINVAL);
    CHECK_PT(pthread_create(&sw.reaper, NULL, reaper_main, NULL));
}

static void tear_down(void)
{
    reaper_finish();
    stats_destroy();
}

/* Start a host on the lowest FREE port. Returns the port id; *peer is the host's end. */
static int start_host(int *peer)
{
    int fd, id, err;

    EXPECT(sem_trywait(&sw.free_ports) == 0);          /* the accept thread's token */
    tcp_pair(peer, &fd);
    enum port_start_rc rc = port_start(fd, &id, &err);
    EXPECT(rc == PS_OK);
    return id;
}

/* The host leaves normally. Returns once the reaper has finished that port. */
static void host_leaves(int id, int peer)
{
    unsigned long g = port_down_count(id);
    CHECK(close(peer));
    EXPECT(port_wait_down(id, g, WAIT_MS));
}

static void host_resets(int id, int peer)
{
    unsigned long g = port_down_count(id);
    struct linger lg = {1, 0};
    CHECK(setsockopt(peer, SOL_SOCKET, SO_LINGER, &lg, sizeof lg));
    CHECK(close(peer));
    EXPECT(port_wait_down(id, g, WAIT_MS));
}

/* ---- 1. one host comes and goes ---- */
static void test_one_host(void)
{
    printf("a host connects and leaves: port UP, then FREE, token returned\n");
    set_up(4);
    int thr0 = baseline_with_reaper();
    int fds0 = open_fds();
    EXPECT(sem_value() == 4);

    int peer;
    int id = start_host(&peer);
    EXPECT(id == 1);
    EXPECT(port_state(1) == PORT_UP);
    EXPECT(sem_value() == 3);                           /* value + held tokens = FREE ports */
    EXPECT(open_threads() == thr0 + 2);                 /* receiver + sender (created, so present at once) */

    host_leaves(id, peer);
    EXPECT(port_state(1) == PORT_FREE);
    EXPECT(sw.ports[1].rfd == -1 && sw.ports[1].wfd == -1);
    EXPECT(sem_value() == 4);
    EXPECT(port_down_count(1) == 1);
    EXPECT(threads_settle(thr0) == thr0);               /* both threads joined */
    EXPECT(open_fds() == fds0);                         /* socket closed exactly once */
    tear_down();
}

/* ---- 2. frames flow, then the receiving host dies while a frame is queued ---- */
static void test_forwarding_and_drain(void)
{
    printf("flooding between two ports; a host that never reads dies with frames queued\n");
    long live0 = frame_live_count();
    set_up(4);
    int pa, pb;
    int a = start_host(&pa), b = start_host(&pb);
    EXPECT(a == 1 && b == 2);

    frame_hdr_t h = {.len = 5, .vlan = 7, .prio = 1};
    memset(h.src, 0x02, MAC_LEN);
    memset(h.dst, 0xFF, MAC_LEN);
    EXPECT(frame_send(pa, &h, (const uint8_t *)"hello") == 0);

    frame_hdr_t r;
    uint8_t pay[MAX_PAYLOAD];
    EXPECT(frame_recv(pb, &r, pay) == FR_OK);           /* the sender thread of port 2 wrote it */
    /* Part 13: the host wrote vlan 7; an access port overwrites it with the port's VLAN (1 by default). */
    EXPECT(r.len == 5 && r.vlan == 1 && memcmp(pay, "hello", 5) == 0);
    EXPECT(stats_get(1, STAT_IN) == 1);

    /* Fill port 2's queue: host B never reads, so its sender blocks in write(). */
    uint8_t big[MAX_PAYLOAD];
    memset(big, 'x', sizeof big);
    h.len = MAX_PAYLOAD;
    for (int i = 0; i < 3000; i++)
        EXPECT(frame_send(pa, &h, big) == 0);

    host_resets(b, pb);                                 /* B dies; its queued frames are drained */
    host_leaves(a, pa);
    EXPECT(port_state(1) == PORT_FREE && port_state(2) == PORT_FREE);
    EXPECT(sem_value() == 4);
    tear_down();
    EXPECT(frame_live_count() == live0);                /* every frame was released exactly once */
}

/* ---- 3. lowest FREE port is reused ---- */
static void test_lowest_free(void)
{
    printf("the lowest FREE port is given out, also after a gap\n");
    set_up(4);
    int p1, p2, p3;
    EXPECT(start_host(&p1) == 1);
    EXPECT(start_host(&p2) == 2);
    EXPECT(start_host(&p3) == 3);
    host_leaves(2, p2);
    int p2b;
    EXPECT(start_host(&p2b) == 2);                      /* the gap is filled first */
    host_leaves(1, p1);
    int p1b, p4;
    EXPECT(start_host(&p1b) == 1);
    EXPECT(start_host(&p4) == 4);
    EXPECT(sem_value() == 0);
    host_leaves(1, p1b);
    host_leaves(2, p2b);
    host_leaves(3, p3);
    host_leaves(4, p4);
    EXPECT(sem_value() == 4);
    tear_down();
}

/* ---- 4. no free port ---- */
static void test_none_free(void)
{
    printf("no FREE port: port_start changes nothing and the token is not posted\n");
    set_up(2);
    int pa, pb;
    start_host(&pa);
    start_host(&pb);
    EXPECT(sem_value() == 0);

    int host, fd, id = -1, err = -1;
    tcp_pair(&host, &fd);
    EXPECT(port_start(fd, &id, &err) == PS_NONE);
    EXPECT(id == 0);
    EXPECT(sem_value() == 0);                           /* nothing was posted */
    EXPECT(fcntl(fd, F_GETFD) >= 0);                    /* fd is untouched: still the caller's */
    CHECK(close(fd));
    CHECK(close(host));

    host_leaves(1, pa);
    host_leaves(2, pb);
    EXPECT(sem_value() == 2);
    tear_down();
}

/* ---- 5. many threads ask for the same teardown ---- */
#define ASKERS 16
static pthread_barrier_t bar;
static port_t *target;
static _Atomic int winners;

static void *asker(void *arg)
{
    (void)arg;
    pthread_barrier_wait(&bar);
    if (port_request_down(target, PORT_FREE))
        atomic_fetch_add(&winners, 1);
    return NULL;
}

static void test_single_teardown(void)
{
    printf("%d threads request the teardown of one port at once: exactly one wins\n", ASKERS);
    set_up(3);
    int peer;
    int id = start_host(&peer);
    target = &sw.ports[id];
    atomic_store(&winners, 0);
    CHECK_PT(pthread_barrier_init(&bar, NULL, ASKERS));

    unsigned long g = port_down_count(id);
    pthread_t th[ASKERS];
    for (int i = 0; i < ASKERS; i++)
        CHECK_PT(pthread_create(&th[i], NULL, asker, NULL));
    for (int i = 0; i < ASKERS; i++)
        CHECK_PT(pthread_join(th[i], NULL));
    EXPECT(atomic_load(&winners) == 1);
    EXPECT(port_wait_down(id, g, WAIT_MS));
    EXPECT(port_down_count(id) == g + 1);               /* torn down once, not 16 times */
    EXPECT(sem_value() == 3);                           /* one post, not 16 */
    EXPECT(port_state(id) == PORT_FREE);
    EXPECT(!port_request_down(&sw.ports[id], PORT_FREE));       /* a FREE port cannot be torn down */
    CHECK_PT(pthread_barrier_destroy(&bar));
    CHECK(close(peer));
    tear_down();
}

/* ---- 6. DISABLED as final state ---- */
static void test_disabled(void)
{
    printf("final state DISABLED: port is cleaned up but its token is not returned\n");
    set_up(3);
    int thr0 = baseline_with_reaper();
    int fds0 = open_fds();
    int peer;
    int id = start_host(&peer);
    EXPECT(sem_value() == 2);
    unsigned long g = port_down_count(id);
    EXPECT(port_request_down(&sw.ports[id], PORT_DISABLED));
    EXPECT(port_wait_down(id, g, WAIT_MS));
    EXPECT(port_state(id) == PORT_DISABLED);
    EXPECT(sem_value() == 2);                           /* no sem_post for a DISABLED port */
    EXPECT(threads_settle(thr0) == thr0 && open_fds() == fds0 + 1);   /* +1: our own end `peer` */
    EXPECT(!port_request_down(&sw.ports[id], PORT_FREE));       /* DISABLED cannot be requested down */

    int p2, p3;
    EXPECT(start_host(&p2) == 2);                       /* port 1 is skipped */
    EXPECT(start_host(&p3) == 3);
    int host, fd, pid = -1, err;
    tcp_pair(&host, &fd);
    EXPECT(port_start(fd, &pid, &err) == PS_NONE);      /* only the DISABLED port is left */
    CHECK(close(fd));
    CHECK(close(host));
    CHECK(close(peer));
    host_leaves(2, p2);
    host_leaves(3, p3);
    tear_down();
}

/* ---- 7. sender and receiver fail at the same moment ---- */
static void test_simultaneous_failures(void)
{
    printf("receiver read error and sender write error at the same time (300 rounds)\n");
    set_up(4);
    int thr0 = baseline_with_reaper();
    int fds0 = open_fds();
    int pa;
    int a = start_host(&pa);
    EXPECT(a == 1);
    frame_hdr_t h = {.len = 100, .vlan = 1, .prio = 1};
    memset(h.src, 0x02, MAC_LEN);
    memset(h.dst, 0xFF, MAC_LEN);
    uint8_t data[100];
    memset(data, 'd', sizeof data);

    int ok = 1;
    for (int round = 0; round < 300; round++) {
        int pb;
        int b = start_host(&pb);
        if (b != 2)
            ok = 0;
        /* flood port 2 so that its sender is busy writing */
        for (int i = 0; i < round % 40; i++)
            if (frame_send(pa, &h, data) != 0)
                ok = 0;
        unsigned long g = port_down_count(b);
        struct linger lg = {1, 0};
        /* The RST makes the receiver's read AND the sender's write fail. */
        if (setsockopt(pb, SOL_SOCKET, SO_LINGER, &lg, sizeof lg) != 0 || close(pb) != 0)
            ok = 0;
        if (!port_wait_down(b, g, WAIT_MS))
            ok = 0;
        if (port_down_count(b) != g + 1)                /* exactly one teardown per round */
            ok = 0;
        if (port_state(b) != PORT_FREE || sem_value() != 3)
            ok = 0;
    }
    EXPECT(ok);
    EXPECT(port_down_count(2) == 300);
    host_leaves(1, pa);
    EXPECT(sem_value() == 4);
    EXPECT(threads_settle(thr0) == thr0);
    EXPECT(open_fds() == fds0);
    tear_down();
}

/* ---- 8. many connect/disconnect cycles ---- */
static void test_many_cycles(void)
{
    printf("1000 connect/disconnect cycles: threads, descriptors and tokens stay constant\n");
    set_up(2);
    int thr0 = baseline_with_reaper();
    int fds0 = open_fds();
    int ok = 1;
    for (int i = 0; i < 1000; i++) {
        int peer, fd, id, err;
        if (sem_trywait(&sw.free_ports) != 0)
            ok = 0;
        tcp_pair(&peer, &fd);
        if (port_start(fd, &id, &err) != PS_OK || id != 1)
            ok = 0;
        unsigned long g = port_down_count(1);
        if (i % 2)
            close(peer);
        else {
            struct linger lg = {1, 0};
            setsockopt(peer, SOL_SOCKET, SO_LINGER, &lg, sizeof lg);
            close(peer);
        }
        if (!port_wait_down(1, g, WAIT_MS) || port_down_count(1) != g + 1)
            ok = 0;
        if (sem_value() != 2 || port_state(1) != PORT_FREE)
            ok = 0;
    }
    EXPECT(ok);
    EXPECT(port_down_count(1) == 1000);
    EXPECT(threads_settle(thr0) == thr0);
    EXPECT(open_fds() == fds0);
    tear_down();
}

/* ---- 9. reaper_finish completes the queued teardowns ---- */
static void test_reaper_finish(void)
{
    printf("reaper_finish: queued teardowns are completed before the reaper exits\n");
    set_up(4);
    int p[3];
    for (int i = 0; i < 3; i++)
        start_host(&p[i]);
    for (int i = 1; i <= 3; i++)
        EXPECT(port_request_down(&sw.ports[i], PORT_FREE));
    reaper_finish();                                    /* returns after the 3 teardowns */
    for (int i = 1; i <= 3; i++)
        EXPECT(port_state(i) == PORT_FREE);
    EXPECT(sem_value() == 4);
    for (int i = 0; i < 3; i++)
        CHECK(close(p[i]));
    stats_destroy();
}

/* ---- 10. the notification ---- */
static void test_notification(void)
{
    printf("port_wait_down / port_down_count\n");
    set_up(2);
    EXPECT(port_down_count(1) == 0);
    EXPECT(!port_wait_down(1, 0, 50));                  /* nothing happens: times out */
    EXPECT(port_down_count(-1) == 0 && port_down_count(MAX_PORTS + 1) == 0);
    EXPECT(!port_wait_down(-1, 0, 10));
    int peer;
    start_host(&peer);
    EXPECT(!port_wait_down(1, 0, 50));                  /* still UP */
    host_leaves(1, peer);
    EXPECT(port_wait_down(1, 0, 10));                   /* already past generation 0: returns at once */
    EXPECT(!port_wait_down(1, 1, 50));
    EXPECT(port_down_count(2) == 0);                    /* other ports unaffected */
    tear_down();
}

int main(void)
{
    signal(SIGPIPE, SIG_IGN);                           /* the switch does this in main() */
    base_threads = open_threads();

    /* The switch logs a lot; keep it in a file and look for error lines at the end. */
    char path[] = "/tmp/test_port_log_XXXXXX";
    int logfd = mkstemp(path);
    CHECK(logfd);
    CHECK(dup2(logfd, STDERR_FILENO));

    test_one_host();
    test_forwarding_and_drain();
    test_lowest_free();
    test_none_free();
    test_single_teardown();
    test_disabled();
    test_simultaneous_failures();
    test_many_cycles();
    test_reaper_finish();
    test_notification();

    /* No double close, join or semaphore problem may have been logged. */
    char cmd[256];
    snprintf(cmd, sizeof cmd,
             "grep -E 'close failed|join of|shutdown failed|sem_post failed|ERR\\]' %s > /dev/null", path);
    EXPECT(system(cmd) != 0);                           /* grep finds nothing */
    CHECK(unlink(path));

    printf("\n%d checks, %d failures\n", checks, failures);
    printf(failures == 0 ? "ALL PASSED\n" : "SOME FAILED\n");
    return failures == 0 ? 0 : 1;
}
