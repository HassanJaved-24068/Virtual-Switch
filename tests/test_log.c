/*
 * test_log.c - unit tests of the asynchronous logger (Part 9): log_start / log_msg / log_stop.
 * Also run under ThreadSanitizer (build/test_log_tsan).
 *
 * The log file is a temporary file, or a FIFO that nobody reads (a stalled sink) to fill the
 * ring on purpose. Every wait has a limit, so a deadlock fails the test instead of hanging.
 */
#include "common.h"
#include "log.h"

#include <fcntl.h>
#include <poll.h>
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

static char dir[] = "/tmp/test_log_XXXXXX";

static char *path_of(const char *name)
{
    static char buf[4][128];
    static int k;
    char *p = buf[k++ % 4];

    snprintf(p, 128, "%s/%s", dir, name);
    return p;
}

static double now_s(void)
{
    struct timespec ts = now_mono();
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

/* Read a whole file. Returns a malloc'd string (caller frees), or NULL. */
static char *slurp(const char *path)
{
    FILE *f = fopen(path, "r");
    size_t cap = 1 << 16, n = 0;
    char *buf;

    if (f == NULL)
        return NULL;
    buf = malloc(cap);
    while (buf != NULL) {
        size_t got = fread(buf + n, 1, cap - n - 1, f);
        n += got;
        if (got == 0)
            break;
        if (cap - n < 2) {
            char *nb = realloc(buf, cap *= 2);
            if (nb == NULL) { free(buf); buf = NULL; break; }
            buf = nb;
        }
    }
    if (buf != NULL)
        buf[n] = '\0';
    fclose(f);
    return buf;
}

static int count_lines(const char *text)
{
    int n = 0;

    for (const char *p = text; *p; p++)
        if (*p == '\n')
            n++;
    return n;
}

/* ---- 1. no logger: synchronous fallback to stderr ---- */
static void test_sync_fallback(void)
{
    printf("without a logger: log_msg writes to stderr at once\n");
    char *f = path_of("stderr.txt");
    pid_t pid = fork();                     /* the child redirects ITS stderr; the test's descriptors stay as they are */

    CHECK_PT(pid < 0 ? errno : 0);
    if (pid == 0) {
        int fd = open(f, O_WRONLY | O_CREAT | O_TRUNC, 0644);

        if (fd < 0 || dup2(fd, STDERR_FILENO) < 0)
            _exit(2);
        log_msg(LOG_INFO, "sync %d", 1);
        log_stop();                                     /* harmless when nothing runs */
        log_msg(LOG_WARN, "sync %s", "two");
        close(STDERR_FILENO);                           /* leave no descriptor open (valgrind --track-fds) */
        close(fd);
        _exit(0);
    }
    int st = 0;
    EXPECT(waitpid(pid, &st, 0) == pid && WIFEXITED(st) && WEXITSTATUS(st) == 0);

    char *t = slurp(f);
    EXPECT(t != NULL && strstr(t, "[INFO] sync 1\n") != NULL && strstr(t, "[WARN] sync two\n") != NULL);
    free(t);
    EXPECT(log_dropped_total() == 0);
}

/* ---- 2. basic: format, levels, append, order ---- */
static void test_basic(void)
{
    printf("start, three levels, format YYYY-MM-DD HH:MM:SS.mmm [LEVEL] text, append, stop\n");
    char *f = path_of("basic.log");
    FILE *pre = fopen(f, "w");

    fputs("existing line\n", pre);                      /* O_APPEND: this must survive */
    fclose(pre);

    EXPECT(log_start(f) == 0);
    EXPECT(log_start(f) == EBUSY);                      /* only one logger */
    log_msg(LOG_INFO, "hello %d", 42);
    log_msg(LOG_WARN, "careful %s", "now");
    log_msg(LOG_ERR, "bad");
    log_stop();
    log_stop();                                         /* a second stop is harmless */

    char *t = slurp(f);
    EXPECT(t != NULL && strncmp(t, "existing line\n", 14) == 0);
    int y, mo, d, h, mi, s, ms;
    char lvl[8], text[64];
    EXPECT(t != NULL && sscanf(t + 14, "%d-%d-%d %d:%d:%d.%d [%7[A-Z]] %63[^\n]",
                               &y, &mo, &d, &h, &mi, &s, &ms, lvl, text) == 9);
    EXPECT(y >= 2024 && mo >= 1 && mo <= 12 && d >= 1 && d <= 31 && h <= 23 && mi <= 59 && s <= 60);
    EXPECT(ms >= 0 && ms <= 999 && strcmp(lvl, "INFO") == 0 && strcmp(text, "hello 42") == 0);
    EXPECT(t != NULL && strstr(t, "[WARN] careful now\n") != NULL && strstr(t, "[ERR] bad\n") != NULL);
    EXPECT(t != NULL && count_lines(t) == 4);
    free(t);

    /* a second run appends again */
    EXPECT(log_start(f) == 0);
    log_msg(LOG_INFO, "second run");
    log_stop();
    t = slurp(f);
    EXPECT(t != NULL && count_lines(t) == 5 && strstr(t, "[INFO] second run\n") != NULL);
    free(t);

    EXPECT(log_start("/nonexistent-dir/x.log") == ENOENT);     /* errors are returned, not fatal */
    log_msg(LOG_INFO, "after a failed start");                 /* still works (stderr) */
}

/* ---- 3. a record is 256 bytes: longer text is cut, nothing overflows ---- */
static void test_truncate(void)
{
    printf("text longer than a record is cut to %d bytes; %% in arguments is safe\n", LOG_TEXT_LEN - 1);
    char *f = path_of("trunc.log");
    char big[3000];

    memset(big, 'x', sizeof big - 1);
    big[sizeof big - 1] = '\0';
    EXPECT(log_start(f) == 0);
    log_msg(LOG_INFO, "%s", big);
    log_msg(LOG_INFO, "100%% %s %d", "%n%s", 7);
    log_msg(LOG_INFO, "after");
    log_stop();

    char *t = slurp(f);
    EXPECT(t != NULL && count_lines(t) == 3);
    if (t != NULL) {
        char *nl = strchr(t, '\n');
        char *tag = strstr(t, "[INFO] ");
        EXPECT(nl != NULL && tag != NULL && (size_t)(nl - (tag + 7)) == LOG_TEXT_LEN - 1);
        EXPECT(strstr(t, "[INFO] 100% %n%s 7\n") != NULL && strstr(t, "[INFO] after\n") != NULL);
    }
    free(t);
}

/* ---- 4. 8 threads: lines are whole, in order per thread, delivered + dropped = sent ---- */
#define THREADS 8
#define PER_THREAD 5000

static void *worker(void *arg)
{
    int id = (int)(intptr_t)arg;

    for (int i = 0; i < PER_THREAD; i++)
        log_msg(LOG_INFO, "t=%d n=%05d end", id, i);
    return NULL;
}

static void test_concurrent(void)
{
    printf("%d threads x %d messages: whole lines, per-thread order, delivered + dropped = sent\n",
           THREADS, PER_THREAD);
    char *f = path_of("conc.log");
    pthread_t th[THREADS];

    EXPECT(log_start(f) == 0);
    for (intptr_t i = 0; i < THREADS; i++)
        CHECK_PT(pthread_create(&th[i], NULL, worker, (void *)i));
    for (int i = 0; i < THREADS; i++)
        CHECK_PT(pthread_join(th[i], NULL));
    unsigned long long dropped_now = log_dropped_total();
    log_stop();

    char *t = slurp(f);
    EXPECT(t != NULL);
    if (t == NULL)
        return;
    int last[THREADS], whole = 0, bad = 0, order = 0;
    unsigned long long reported = 0;
    for (int i = 0; i < THREADS; i++)
        last[i] = -1;
    char *save = NULL;
    for (char *ln = strtok_r(t, "\n", &save); ln; ln = strtok_r(NULL, "\n", &save)) {
        int id, n;
        unsigned long long k;
        const char *p = strstr(ln, "[INFO] ");
        char tail[8];

        if (p != NULL && sscanf(p + 7, "t=%d n=%d %7s", &id, &n, tail) == 3 && strcmp(tail, "end") == 0 &&
            id >= 0 && id < THREADS) {
            whole++;
            if (n <= last[id])
                order++;                    /* a thread's messages must stay in order */
            last[id] = n;
        } else if (strstr(ln, "[WARN] ") != NULL && sscanf(strstr(ln, "[WARN] ") + 7, "%llu log messages dropped", &k) == 1) {
            reported += k;
        } else {
            bad++;
        }
    }
    printf("  %d delivered, %llu dropped (reported %llu)\n", whole, dropped_now, reported);
    EXPECT(bad == 0);                                           /* no torn or interleaved line */
    EXPECT(order == 0);
    EXPECT((unsigned long long)whole + reported == (unsigned long long)THREADS * PER_THREAD);
    EXPECT(reported == dropped_now);                            /* the count in the file is exact */
    free(t);
}

/* ---- 5. shutdown drains everything that is queued ---- */
static void test_drain(void)
{
    printf("log_stop drains the ring: 1000 queued messages are all written, in order\n");
    char *f = path_of("drain.log");

    EXPECT(log_start(f) == 0);
    for (int i = 0; i < 1000; i++)                     /* fewer than the ring (1024): none can be dropped */
        log_msg(LOG_INFO, "drain %04d", i);
    log_msg(LOG_INFO, "drain last");
    log_stop();                                         /* immediately */

    char *t = slurp(f);
    EXPECT(t != NULL && count_lines(t) == 1001);
    int ok = t != NULL;
    for (int i = 0; ok && i < 1000; i++) {
        char want[32];
        snprintf(want, sizeof want, "[INFO] drain %04d\n", i);
        char *p = strstr(t, want);
        if (p == NULL)
            ok = 0;
    }
    EXPECT(ok);
    EXPECT(t != NULL && strstr(t, "[INFO] drain last\n") != NULL);
    if (t != NULL) {                                    /* the last message is the last line */
        const char *end = t + strlen(t);
        EXPECT(end - t > 11 && strcmp(end - 11, "drain last\n") == 0);
    }
    free(t);

    /* after the stop the messages go to stderr again and nothing crashes */
    log_msg(LOG_INFO, "after stop (this line goes to stderr)");
    EXPECT(1);
}

/* ---- 6. a stalled sink: the producers never wait, drops are counted exactly ---- */
struct reader {
    int fd;
    pthread_t th;
    char *buf;
    size_t len, cap;
    volatile int stop;
};

static void *reader_main(void *arg)
{
    struct reader *r = arg;
    char chunk[4096];

    for (;;) {
        ssize_t n = read(r->fd, chunk, sizeof chunk);
        if (n > 0) {
            if (r->len + (size_t)n + 1 > r->cap) {
                r->cap = r->cap ? r->cap * 2 : 1 << 20;
                r->buf = realloc(r->buf, r->cap);
            }
            memcpy(r->buf + r->len, chunk, (size_t)n);
            r->len += (size_t)n;
        } else if (n == 0) {
            break;                                      /* the writer closed the FIFO */
        } else if (errno != EINTR) {
            break;
        }
    }
    if (r->buf != NULL)
        r->buf[r->len] = '\0';
    return NULL;
}

static void test_stalled_sink(void)
{
    printf("stalled log file (a FIFO nobody reads): log_msg never waits, drops are counted exactly\n");
    char *f = path_of("stalled.fifo");
    enum { SENT = 200000 };

    CHECK_PT(mkfifo(f, 0600) < 0 ? errno : 0);
    struct reader r = {.fd = open(f, O_RDONLY | O_NONBLOCK)};       /* so that the open below does not block */
    CHECK_PT(r.fd < 0 ? errno : 0);
    EXPECT(log_start(f) == 0);                                      /* opens the write end */
    int fl = fcntl(r.fd, F_GETFL);
    CHECK_PT(fcntl(r.fd, F_SETFL, fl & ~O_NONBLOCK) < 0 ? errno : 0);   /* the reader now blocks in read() */

    double t0 = now_s();
    for (int i = 0; i < SENT; i++)                      /* ~64 KB fit in the pipe: then the logger blocks */
        log_msg(LOG_INFO, "stalled message number %06d with some padding text to fill the pipe", i);
    double took = now_s() - t0;
    unsigned long long dropped_now = log_dropped_total();
    printf("  %d messages in %.3f s while the logger was blocked; %llu dropped\n", SENT, took, dropped_now);
    EXPECT(took < 5.0);                                 /* a blocking log_msg would never return */
    EXPECT(dropped_now > 0);

    /* now let it drain: start reading, then stop (the stop waits for the logger to finish) */
    CHECK_PT(pthread_create(&r.th, NULL, reader_main, &r));
    log_stop();                                         /* drains, then closes the write end: the reader gets EOF */
    CHECK_PT(pthread_join(r.th, NULL));
    CHECK_PT(close(r.fd) < 0 ? errno : 0);

    EXPECT(r.buf != NULL);
    unsigned long long reported = 0, whole = 0;
    if (r.buf != NULL) {
        char *save = NULL;
        for (char *ln = strtok_r(r.buf, "\n", &save); ln; ln = strtok_r(NULL, "\n", &save)) {
            unsigned long long k;
            if (strstr(ln, "stalled message number ") != NULL)
                whole++;
            else if (strstr(ln, "[WARN] ") != NULL && sscanf(strstr(ln, "[WARN] ") + 7, "%llu log messages dropped", &k) == 1)
                reported += k;
        }
    }
    printf("  %llu written, %llu reported as dropped\n", whole, reported);
    EXPECT(whole + reported == SENT);                   /* nothing is lost without being counted */
    EXPECT(reported == dropped_now);
    EXPECT(whole <= LOG_QUEUE_SIZE + 4096);             /* about: ring + what the pipe and the buffers hold */
    free(r.buf);
    unlink(f);
}

int main(void)
{
    if (mkdtemp(dir) == NULL) {
        perror("mkdtemp");
        return 1;
    }
    test_sync_fallback();
    test_basic();
    test_truncate();
    test_concurrent();
    test_drain();
    test_stalled_sink();

    char cmd[160];
    snprintf(cmd, sizeof cmd, "rm -rf %s", dir);
    if (system(cmd) != 0)
        printf("warning: could not remove %s\n", dir);
    printf("\n%d checks, %d failures\n", checks, failures);
    printf(failures == 0 ? "ALL PASSED\n" : "SOME FAILED\n");
    return failures == 0 ? 0 : 1;
}
