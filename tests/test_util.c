/*
 * test_util.c - unit tests for util.c, common.h and log.c (Part 1).
 * Prints PASS/FAIL for every check; exit code 0 only if all passed.
 */
#include "common.h"
#include "log.h"

#include <fcntl.h>
#include <sys/wait.h>
#include <unistd.h>

static int checks = 0;
static int failures = 0;

#define EXPECT(cond)                                                       \
    do {                                                                   \
        checks++;                                                          \
        if (!(cond)) {                                                     \
            failures++;                                                    \
            printf("  FAIL line %d: %s\n", __LINE__, #cond);               \
        }                                                                  \
    } while (0)

static void test_mac_parse_ok(void)
{
    printf("mac_parse: valid strings\n");
    mac_t m;
    EXPECT(mac_parse("02:00:00:00:00:0a", m) == 0);
    EXPECT(m[0] == 0x02 && m[1] == 0 && m[4] == 0 && m[5] == 0x0a);
    EXPECT(mac_parse("AA:bB:Cc:dD:eE:fF", m) == 0);   /* mixed case */
    EXPECT(m[0] == 0xAA && m[1] == 0xBB && m[2] == 0xCC &&
           m[3] == 0xDD && m[4] == 0xEE && m[5] == 0xFF);
    EXPECT(mac_parse("00:00:00:00:00:00", m) == 0);
    EXPECT(mac_parse("ff:ff:ff:ff:ff:ff", m) == 0 && mac_is_broadcast(m));
}

static void test_mac_parse_bad(void)
{
    printf("mac_parse: invalid strings\n");
    const char *bad[] = {
        "",                          /* empty */
        "02:00:00:00:00",            /* too few groups */
        "02:00:00:00:00:0a:01",      /* too many groups */
        "02:00:00:00:00:0",          /* short last group (16 chars) */
        "02:00:00:00:00:0aa",        /* long last group (18 chars) */
        "02:00:00:00:00:0a ",        /* trailing space */
        " 02:00:00:00:00:0a",        /* leading space */
        "02:00:00:00:00:0g",         /* non-hex letter */
        "0x:00:00:00:00:0a",         /* non-hex letter first byte */
        "02-00-00-00-00-0a",         /* wrong separator */
        "02:00:00:00:00;0a",         /* wrong separator */
        "0200:0000:000a:xx",         /* wrong layout */
        "020000000000a0000",         /* no separators, 17 chars */
        "+2:00:00:00:00:0a",         /* sign character */
        "02:00:00:00:00:-a",         /* sign character */
    };
    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        mac_t m = {1, 2, 3, 4, 5, 6};
        checks++;
        if (mac_parse(bad[i], m) != -1) {
            failures++;
            printf("  FAIL: \"%s\" was accepted\n", bad[i]);
        }
        EXPECT(m[0] == 1 && m[5] == 6);           /* output untouched on error */
    }
    mac_t m;
    EXPECT(mac_parse(NULL, m) == -1);
    EXPECT(mac_parse("02:00:00:00:00:0a", NULL) == -1);
}

static void test_mac_format(void)
{
    printf("mac_format and round trip\n");
    mac_t m = {0x02, 0x00, 0x00, 0x00, 0x00, 0x0a};
    char s[MAC_STR_LEN];
    EXPECT(mac_format(m, s, sizeof s) == 0);
    EXPECT(strcmp(s, "02:00:00:00:00:0a") == 0);

    /* buffer too small must fail and not overflow */
    char small[MAC_STR_LEN - 1];
    EXPECT(mac_format(m, small, sizeof small) == -1);
    EXPECT(mac_format(m, NULL, 18) == -1);

    /* round trip: every value of byte 0 and byte 5, plus text -> bytes -> text */
    for (int v = 0; v < 256; v++) {
        mac_t a = {(uint8_t)v, 0x12, 0xab, 0xcd, 0xef, (uint8_t)(255 - v)};
        mac_t b;
        char t[MAC_STR_LEN];
        if (mac_format(a, t, sizeof t) != 0 || mac_parse(t, b) != 0 ||
            memcmp(a, b, MAC_LEN) != 0) {
            checks++;
            failures++;
            printf("  FAIL: round trip for v=%d\n", v);
            return;
        }
    }
    checks++;
    char t2[MAC_STR_LEN];
    mac_t c;
    EXPECT(mac_parse("de:ad:be:ef:00:01", c) == 0);
    EXPECT(mac_format(c, t2, sizeof t2) == 0 && strcmp(t2, "de:ad:be:ef:00:01") == 0);
}

static void test_mac_flags(void)
{
    printf("mac_is_broadcast / mac_is_multicast\n");
    mac_t bc = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};
    mac_t almost = {0xff, 0xff, 0xff, 0xff, 0xff, 0xfe};
    mac_t mc = {0x01, 0x00, 0x5e, 0x00, 0x00, 0x01};
    mac_t uni = {0x02, 0x00, 0x00, 0x00, 0x00, 0x0a};
    mac_t zero = {0, 0, 0, 0, 0, 0};
    EXPECT(mac_is_broadcast(bc));
    EXPECT(!mac_is_broadcast(almost));
    EXPECT(!mac_is_broadcast(mc));
    EXPECT(!mac_is_broadcast(zero));
    EXPECT(mac_is_multicast(bc));          /* broadcast has the low bit set */
    EXPECT(mac_is_multicast(mc));
    EXPECT(!mac_is_multicast(uni));        /* 0x02: locally administered, unicast */
    EXPECT(!mac_is_multicast(zero));
}

static void test_time_helpers(void)
{
    printf("time helpers\n");
    struct timespec t = {10, 500000000L};
    struct timespec r = ts_add_ms(t, 700);          /* carries into seconds */
    EXPECT(r.tv_sec == 11 && r.tv_nsec == 200000000L);
    r = ts_add_ms(t, 0);
    EXPECT(r.tv_sec == 10 && r.tv_nsec == 500000000L);
    r = ts_add_ms(t, 2500);
    EXPECT(r.tv_sec == 13 && r.tv_nsec == 0);
    r = ts_add_ms(t, -700);                         /* borrow from seconds */
    EXPECT(r.tv_sec == 9 && r.tv_nsec == 800000000L);
    r = ts_add_ms(t, -2500);
    EXPECT(r.tv_sec == 8 && r.tv_nsec == 0);
    r = ts_add_ms(t, 499);                          /* just below carry */
    EXPECT(r.tv_sec == 10 && r.tv_nsec == 999000000L);

    struct timespec a = {12, 100000000L};
    EXPECT(ts_diff_ms(a, t) == 1600);
    EXPECT(ts_diff_ms(t, a) == -1600);
    EXPECT(ts_diff_ms(a, a) == 0);
    EXPECT(ts_diff_ms(ts_add_ms(t, 123456), t) == 123456);

    struct timespec n1 = now_mono();
    struct timespec n2 = now_mono();
    EXPECT(ts_diff_ms(n2, n1) >= 0);

    long d = ts_diff_ms(deadline_mono(1500), now_mono());
    EXPECT(d >= 1400 && d <= 1500);
    d = ts_diff_ms(deadline_mono(0), now_mono());
    EXPECT(d >= -50 && d <= 0);

    struct timespec real_now;
    EXPECT(clock_gettime(CLOCK_REALTIME, &real_now) == 0);
    long rd = ts_diff_ms(deadline_real(3), real_now);
    EXPECT(rd >= 2900 && rd <= 3100);
}

/* cond_init_mono: a timed wait with an absolute CLOCK_MONOTONIC deadline must
 * time out after about the requested time (not instantly, not hours later). */
static void test_cond_init_mono(void)
{
    printf("cond_init_mono\n");
    pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
    pthread_cond_t cv;
    EXPECT(cond_init_mono(&cv) == 0);

    struct timespec start = now_mono();
    struct timespec dl = deadline_mono(200);
    EXPECT(pthread_mutex_lock(&mu) == 0);
    int rc = 0;
    while (rc == 0)                    /* only ETIMEDOUT ends the loop (spurious wakeups ok) */
        rc = pthread_cond_timedwait(&cv, &mu, &dl);
    EXPECT(pthread_mutex_unlock(&mu) == 0);
    long waited = ts_diff_ms(now_mono(), start);
    EXPECT(rc == ETIMEDOUT);
    EXPECT(waited >= 190 && waited < 1000);

    EXPECT(pthread_cond_destroy(&cv) == 0);
    EXPECT(pthread_mutex_destroy(&mu) == 0);
}

/* Run fn in a child process; return its exit status (or -1) and capture stderr. */
static int run_child(void (*fn)(void), char *errbuf, size_t errlen)
{
    int p[2];
    fflush(stdout);                 /* else the child copies our unflushed output */
    if (pipe(p) != 0)
        return -1;
    pid_t pid = fork();
    if (pid < 0)
        return -1;
    if (pid == 0) {
        close(p[0]);
        dup2(p[1], STDERR_FILENO);
        fn();
        _exit(0);
    }
    close(p[1]);
    size_t got = 0;
    ssize_t n;
    while (got < errlen - 1 && (n = read(p[0], errbuf + got, errlen - 1 - got)) > 0)
        got += (size_t)n;
    errbuf[got] = '\0';
    close(p[0]);
    int st = 0;
    if (waitpid(pid, &st, 0) < 0 || !WIFEXITED(st))
        return -1;
    return WEXITSTATUS(st);
}

static void child_check_ok(void)    { CHECK(close(open("/dev/null", O_RDONLY))); CHECK_PT(0); }
static void child_check_fail(void)  { CHECK(close(-1)); }
static void child_check_pt_fail(void) { CHECK_PT(EINVAL); }

static void test_check_macros(void)
{
    printf("CHECK and CHECK_PT\n");
    char err[512];
    EXPECT(run_child(child_check_ok, err, sizeof err) == 0 && err[0] == '\0');

    int st = run_child(child_check_fail, err, sizeof err);
    EXPECT(st == EXIT_FAILURE);
    EXPECT(strstr(err, "close(-1)") != NULL);
    EXPECT(strstr(err, "test_util.c") != NULL);
    EXPECT(strstr(err, strerror(EBADF)) != NULL);

    st = run_child(child_check_pt_fail, err, sizeof err);
    EXPECT(st == EXIT_FAILURE);
    EXPECT(strstr(err, strerror(EINVAL)) != NULL);   /* uses rc, not errno */
}

/* ---- logging ---- */

static void child_log_one(void)
{
    log_msg(LOG_INFO, "hello %d", 42);
    log_msg(LOG_WARN, "careful %s", "now");
    log_msg(LOG_ERR, "bad");
}

static void child_log_long(void)
{
    char big[3000];
    memset(big, 'x', sizeof big - 1);
    big[sizeof big - 1] = '\0';
    log_msg(LOG_INFO, "%s", big);        /* must be truncated, not overflow */
    log_msg(LOG_INFO, "after");
}

#define LOG_THREADS 4
#define LOG_LINES 100

static void *log_worker(void *arg)
{
    int id = (int)(intptr_t)arg;
    for (int i = 0; i < LOG_LINES; i++)
        log_msg(LOG_INFO, "thread=%d line=%03d end", id, i);
    return NULL;
}

static void child_log_threads(void)
{
    pthread_t th[LOG_THREADS];
    for (intptr_t i = 0; i < LOG_THREADS; i++)
        CHECK_PT(pthread_create(&th[i], NULL, log_worker, (void *)i));
    for (int i = 0; i < LOG_THREADS; i++)
        CHECK_PT(pthread_join(th[i], NULL));
}

static void test_log(void)
{
    printf("log_msg\n");
    static char out[1 << 16];

    EXPECT(run_child(child_log_one, out, sizeof out) == 0);
    /* timestamp format: YYYY-MM-DD HH:MM:SS.mmm [LEVEL] text */
    int y, mo, d, h, mi, s, ms;
    char lvl[8], text[64];
    EXPECT(sscanf(out, "%d-%d-%d %d:%d:%d.%d [%7[A-Z]] %63[^\n]",
                  &y, &mo, &d, &h, &mi, &s, &ms, lvl, text) == 9);
    EXPECT(y >= 2024 && mo >= 1 && mo <= 12 && d >= 1 && d <= 31);
    EXPECT(h <= 23 && mi <= 59 && s <= 60 && ms >= 0 && ms <= 999);
    EXPECT(strcmp(lvl, "INFO") == 0 && strcmp(text, "hello 42") == 0);
    EXPECT(strstr(out, "[WARN] careful now\n") != NULL);
    EXPECT(strstr(out, "[ERR] bad\n") != NULL);

    EXPECT(run_child(child_log_long, out, sizeof out) == 0);
    EXPECT(strstr(out, "[INFO] after\n") != NULL);
    EXPECT(strlen(out) < 1300);                       /* long line was truncated */

    /* 4 threads x 100 lines: every line must be whole (no interleaving). */
    EXPECT(run_child(child_log_threads, out, sizeof out) == 0);
    int lines = 0, good = 0;
    char *save = NULL;
    for (char *ln = strtok_r(out, "\n", &save); ln; ln = strtok_r(NULL, "\n", &save)) {
        int t, i;
        char tail[8];
        const char *p = strstr(ln, "[INFO] ");
        lines++;
        if (p && sscanf(p, "[INFO] thread=%d line=%d %7s", &t, &i, tail) == 3 &&
            strcmp(tail, "end") == 0 && t >= 0 && t < LOG_THREADS && i < LOG_LINES)
            good++;
    }
    EXPECT(lines == LOG_THREADS * LOG_LINES);
    EXPECT(good == lines);
}

int main(void)
{
    test_mac_parse_ok();
    test_mac_parse_bad();
    test_mac_format();
    test_mac_flags();
    test_time_helpers();
    test_cond_init_mono();
    test_check_macros();
    test_log();

    printf("\n%d checks, %d failures\n", checks, failures);
    printf(failures == 0 ? "ALL PASSED\n" : "SOME FAILED\n");
    return failures == 0 ? 0 : 1;
}
