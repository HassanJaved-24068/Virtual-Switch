/*
 * test_config.c - unit tests of the configuration file parser (Part 10): config_defaults, config_load.
 * Pure parser tests: no switch, no threads. Also run under ThreadSanitizer and valgrind.
 */
#include "config.h"

#include <fcntl.h>
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

static char dir[] = "/tmp/test_config_XXXXXX";
static int file_no;

/* Write `content` to a new file in the test directory and return its path. */
static const char *mkfile(const char *content)
{
    static char path[128];
    FILE *f;

    snprintf(path, sizeof path, "%s/c%03d.conf", dir, file_no++);
    f = fopen(path, "w");
    if (f == NULL) {
        perror("fopen");
        exit(1);
    }
    fputs(content, f);
    fclose(f);
    return path;
}

/* Load `content` over the defaults of an 8-port switch. Returns config_load's result. */
static int load(const char *content, config_t *c, config_error_t *e)
{
    config_defaults(c, 8);
    return config_load(mkfile(content), c, e);
}

static bool same(const config_t *a, const config_t *b)
{
    return memcmp(a, b, sizeof *a) == 0;
}

/* ---- 1. defaults ---- */
static void test_defaults(void)
{
    printf("defaults are the values of roadmap 2.3\n");
    config_t c;

    config_defaults(&c, 5);
    EXPECT(c.num_ports == 5 && c.mac_aging_sec == DEFAULT_MAC_AGING_SEC && c.mac_table_size == DEFAULT_MAC_TABLE_SIZE);
    EXPECT(c.max_macs_per_port == DEFAULT_MAX_MACS_PER_PORT && c.queue_capacity == DEFAULT_QUEUE_CAPACITY);
    EXPECT(c.port_wait_sec == DEFAULT_PORT_WAIT_SEC && c.prio_aging_ms == 200 && c.rate_limit == 0 && c.trunk_port == 0);
    EXPECT(c.wrr_weights[0] == 5 && c.wrr_weights[1] == 3 && c.wrr_weights[2] == 1);
    EXPECT(strcmp(c.log_file, "switch.log") == 0 && strcmp(c.mac_dump_file, "mac_dump.txt") == 0);
    for (int i = 0; i <= MAX_PORTS; i++)
        EXPECT(c.port_vlan[i] == 0 && c.port_rate[i] == -1);
    EXPECT(config_mac_buckets(1024) == 256 && config_mac_buckets(256) == 256 && config_mac_buckets(300) == 256);
    EXPECT(config_mac_buckets(255) == 128 && config_mac_buckets(4) == 4 && config_mac_buckets(5) == 4 &&
           config_mac_buckets(1) == 1 && config_mac_buckets(3) == 2);
}

/* ---- 2. a valid file with every key ---- */
static void test_valid(void)
{
    printf("a valid file with every key (the roadmap's example)\n");
    config_t c;
    config_error_t e;
    const char *text =
        "# vswitch.conf - lines are \"key = value\"; '#' starts a comment\n"
        "mac_aging_seconds = 120      # reloadable\n"
        "mac_table_size    = 2048     # startup only\n"
        "max_macs_per_port = 8        # reloadable\n"
        "queue_capacity    = 32       # startup only (per priority class)\n"
        "port_wait_timeout = 7        # reloadable\n"
        "wrr_weights       = 6,4,2    # reloadable\n"
        "prio_aging_ms     = 150      # reloadable\n"
        "rate_limit        = 500      # reloadable\n"
        "trunk_port        = 4        # startup only\n"
        "log_file          = /tmp/x/switch.log\n"
        "mac_dump_file     = /tmp/x/dump.txt\n"
        "port.3.vlan       = 20       # reloadable\n"
        "port.3.rate       = 100      # reloadable, overrides rate_limit\n"
        "port.8.vlan       = 4094\n"
        "port.1.rate       = 0\n";

    EXPECT(load(text, &c, &e) == 0 && e.count == 0);
    EXPECT(c.mac_aging_sec == 120 && c.mac_table_size == 2048 && c.max_macs_per_port == 8 && c.queue_capacity == 32);
    EXPECT(c.port_wait_sec == 7 && c.wrr_weights[0] == 6 && c.wrr_weights[1] == 4 && c.wrr_weights[2] == 2);
    EXPECT(c.prio_aging_ms == 150 && c.rate_limit == 500 && c.trunk_port == 4);
    EXPECT(strcmp(c.log_file, "/tmp/x/switch.log") == 0 && strcmp(c.mac_dump_file, "/tmp/x/dump.txt") == 0);
    EXPECT(c.port_vlan[3] == 20 && c.port_rate[3] == 100 && c.port_vlan[8] == 4094 && c.port_rate[1] == 0);
    EXPECT(c.port_vlan[1] == 0 && c.port_rate[2] == -1 && c.num_ports == 8);      /* untouched ports keep "not set" */

    /* keys that the file does not mention keep the caller's value */
    EXPECT(load("mac_aging_seconds = 9\n", &c, &e) == 0);
    EXPECT(c.mac_aging_sec == 9 && c.max_macs_per_port == DEFAULT_MAX_MACS_PER_PORT && c.wrr_weights[0] == 5);
    EXPECT(load("", &c, &e) == 0 && c.mac_aging_sec == DEFAULT_MAC_AGING_SEC);      /* an empty file is valid */
    EXPECT(load("# only a comment\n\n   \n\t\n", &c, &e) == 0);
}

/* ---- 3. comments and white space ---- */
static void test_whitespace(void)
{
    printf("comments, blank lines, tabs, CRLF, no final newline\n");
    config_t c;
    config_error_t e;

    EXPECT(load("  \t mac_aging_seconds\t=\t  45   \t # comment = with = equals\r\n"
                "\r\n"
                "   # indented comment\n"
                "max_macs_per_port=3\n"
                "port_wait_timeout =2#no space before the hash",
                &c, &e) == 0);
    EXPECT(c.mac_aging_sec == 45 && c.max_macs_per_port == 3 && c.port_wait_sec == 2);
    EXPECT(load("log_file = my log file.txt\n", &c, &e) == 0 && strcmp(c.log_file, "my log file.txt") == 0);
    EXPECT(load("mac_dump_file = a=b.txt\n", &c, &e) == 0 && strcmp(c.mac_dump_file, "a=b.txt") == 0);   /* only the first '=' splits */
    EXPECT(load("wrr_weights = 5 , 3 ,\t1\n", &c, &e) == 0 && c.wrr_weights[0] == 5 && c.wrr_weights[2] == 1);
}

/* ---- 4. the file cannot be read ---- */
static void test_missing_file(void)
{
    printf("missing file, a directory\n");
    config_t c, before;
    config_error_t e;

    config_defaults(&c, 4);
    c.mac_aging_sec = 77;
    before = c;
    EXPECT(config_load("/nonexistent-dir/none.conf", &c, &e) == -1);
    EXPECT(e.count == 1 && e.lines[0] == 0 && strstr(e.msg, "cannot open") != NULL && strstr(e.msg, "No such file") != NULL);
    EXPECT(same(&c, &before));
    EXPECT(config_load("/nonexistent-dir/none.conf", &c, NULL) == -1);               /* err may be NULL */
    EXPECT(config_load(dir, &c, &e) == -1 && e.count >= 1);                          /* reading a directory fails (EISDIR) */
    EXPECT(same(&c, &before));
}

/* ---- 5. numbers ---- */
static void test_bad_numbers(void)
{
    printf("bad numeric values are rejected with the line number\n");
    static const char *bad[] = {"abc", "30s", "0x10", "1e3", "-", "--5", "5 5", "3.5", "99999999999999999999", "+7", "٣"};
    config_t c, before;
    config_error_t e;
    char text[128];

    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        snprintf(text, sizeof text, "# header\n\nmac_aging_seconds = %s\n", bad[i]);
        config_defaults(&before, 8);
        EXPECT(load(text, &c, &e) == -1);
        EXPECT(e.count == 1 && e.lines[0] == 3 && strstr(e.msg, "line 3:") != NULL);
        config_defaults(&before, 8);
        EXPECT(same(&c, &before));                                                  /* c was the defaults; nothing changed */
    }
    EXPECT(load("mac_aging_seconds =\n", &c, &e) == -1 && strstr(e.msg, "missing value") != NULL);
    EXPECT(load("mac_aging_seconds = # nothing before the comment\n", &c, &e) == -1 && e.lines[0] == 1);
}

/* ---- 6. ranges ---- */
static void test_ranges(void)
{
    printf("range limits: lowest and highest value accepted, one outside rejected\n");
    static const struct { const char *key; long lo, hi; } t[] = {
        {"mac_aging_seconds", 1, 86400}, {"max_macs_per_port", 1, 65536}, {"port_wait_timeout", 0, 3600},
        {"prio_aging_ms", 1, 60000},     {"rate_limit", 0, 1000000},      {"mac_table_size", 1, 65536},
        {"queue_capacity", 1, 65536},    {"trunk_port", 0, 8},
    };
    config_t c;
    config_error_t e;
    char text[96];

    for (size_t i = 0; i < sizeof t / sizeof t[0]; i++) {
        snprintf(text, sizeof text, "%s = %ld\n", t[i].key, t[i].lo);
        EXPECT(load(text, &c, &e) == 0);
        snprintf(text, sizeof text, "%s = %ld\n", t[i].key, t[i].hi);
        EXPECT(load(text, &c, &e) == 0);
        snprintf(text, sizeof text, "%s = %ld\n", t[i].key, t[i].lo - 1);
        EXPECT(load(text, &c, &e) == -1 && strstr(e.msg, "out of range") != NULL && e.lines[0] == 1);
        snprintf(text, sizeof text, "%s = %ld\n", t[i].key, t[i].hi + 1);
        /* trunk_port: 9 is inside 0..MAX_PORTS but above this switch's 8 ports: its own message */
        EXPECT(load(text, &c, &e) == -1 &&
               strstr(e.msg, strcmp(t[i].key, "trunk_port") == 0 ? "only 8 ports" : "out of range") != NULL);
    }
    EXPECT(load("trunk_port = 9\n", &c, &e) == -1 && strstr(e.msg, "only 8 ports") != NULL);     /* more than num_ports */
}

/* ---- 7. unknown keys, syntax ---- */
static void test_unknown_and_syntax(void)
{
    printf("unknown keys and malformed lines\n");
    static const char *bad[] = {
        "foo = 1", "mac_aging = 5", "Mac_Aging_Seconds = 5", "mac_aging_seconds2 = 5", "port.3.foo = 1",
        "port..vlan = 1", "port.x.vlan = 1", "port.3.vlan.x = 1", "port.3 = 1", "port = 1", "ports.3.vlan = 1",
        "port.-3.vlan = 1", "port.3.VLAN = 1", "= 5", "just some words", "mac_aging_seconds 300", "mac_aging_seconds : 300",
    };
    config_t c;
    config_error_t e;

    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        char text[96];

        snprintf(text, sizeof text, "mac_aging_seconds = 10\n%s\n", bad[i]);
        EXPECT(load(text, &c, &e) == -1);
        EXPECT(e.count == 1 && e.lines[0] == 2);
    }
    EXPECT(load("foo = 1\n", &c, &e) == -1 && strstr(e.msg, "unknown key 'foo'") != NULL);
    EXPECT(load("mac_aging_seconds = 5\nmac_aging_seconds = 6\n", &c, &e) == -1 &&
           strstr(e.msg, "duplicate key") != NULL && e.lines[0] == 2);
    EXPECT(load("log_file = a\nlog_file = b\n", &c, &e) == -1 && strstr(e.msg, "duplicate") != NULL);
    EXPECT(load("wrr_weights = 1,1,1\nwrr_weights = 2,2,2\n", &c, &e) == -1 && strstr(e.msg, "duplicate") != NULL);
    EXPECT(load("log_file = a\tb\n", &c, &e) == -1 && strstr(e.msg, "control character") != NULL);
}

/* ---- 8. wrr weights ---- */
static void test_wrr(void)
{
    printf("wrr_weights: exactly three numbers 1..1000\n");
    static const char *bad[] = {"5,3", "5,3,1,1", "5,,1", "5,3,", ",3,1", "a,b,c", "0,3,1", "5,3,0", "5;3;1", "5 3 1",
                                "5,3,1001", "-1,3,1", "5,3.5,1", "5", ",,", "5,3,1x"};
    config_t c;
    config_error_t e;
    char text[64];

    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        snprintf(text, sizeof text, "wrr_weights = %s\n", bad[i]);
        EXPECT(load(text, &c, &e) == -1 && e.count == 1 && e.lines[0] == 1 && strstr(e.msg, "wrr_weights") != NULL);
        config_t d;
        config_defaults(&d, 8);
        EXPECT(same(&c, &d));                                                        /* a bad wrr line changed nothing */
    }
    EXPECT(load("wrr_weights = 1000,1,1\n", &c, &e) == 0 && c.wrr_weights[0] == 1000);
    EXPECT(load("wrr_weights = 1,1,1\n", &c, &e) == 0 && c.wrr_weights[2] == 1);
}

/* ---- 9. port settings ---- */
static void test_port_keys(void)
{
    printf("port.N.vlan and port.N.rate: valid values, bad ports, bad values\n");
    config_t c;
    config_error_t e;

    EXPECT(load("port.1.vlan = 1\nport.8.vlan = 4094\nport.2.rate = 0\nport.7.rate = 1000000\n", &c, &e) == 0);
    EXPECT(c.port_vlan[1] == 1 && c.port_vlan[8] == 4094 && c.port_rate[2] == 0 && c.port_rate[7] == 1000000);
    static const char *bad[] = {"port.0.vlan = 5", "port.9.vlan = 5", "port.65.rate = 5", "port.3.vlan = 0", "port.3.vlan = 4095",
                                "port.3.vlan = abc", "port.3.rate = -1", "port.3.rate = 1000001", "port.3.rate = 1.5",
                                "port.3.vlan =", "port.99999999999999999999.vlan = 5"};
    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        char text[96];

        snprintf(text, sizeof text, "# c\n%s\n", bad[i]);
        EXPECT(load(text, &c, &e) == -1 && e.count == 1 && e.lines[0] == 2);
    }
    EXPECT(load("port.3.vlan = 20\nport.3.vlan = 30\n", &c, &e) == -1 && strstr(e.msg, "duplicate key 'port.3.vlan'") != NULL);
    EXPECT(load("port.3.vlan = 20\nport.3.rate = 1\nport.3.vlan = 20\n", &c, &e) == -1 && e.lines[0] == 3);
    EXPECT(load("port.9.vlan = 5\n", &c, &e) == -1 && strstr(e.msg, "ports 1..8") != NULL);
}

/* ---- 10. several errors ---- */
static void test_multiple_errors(void)
{
    printf("several bad lines: all are reported with their line numbers\n");
    config_t c;
    config_error_t e;
    const char *text =
        "mac_aging_seconds = 10\n"      /* 1 ok */
        "bogus = 1\n"                   /* 2 */
        "max_macs_per_port = 0\n"       /* 3 */
        "# fine\n"                      /* 4 */
        "port.3.vlan = 9000\n"          /* 5 */
        "wrr_weights = 5,3\n"           /* 6 */
        "queue_capacity = 64\n"         /* 7 ok */
        "no equals sign\n";             /* 8 */

    EXPECT(load(text, &c, &e) == -1 && e.count == 5);
    EXPECT(e.lines[0] == 2 && e.lines[1] == 3 && e.lines[2] == 5 && e.lines[3] == 6 && e.lines[4] == 8);
    EXPECT(strstr(e.msg, "line 2:") && strstr(e.msg, "line 3:") && strstr(e.msg, "line 5:") && strstr(e.msg, "line 6:") &&
           strstr(e.msg, "line 8:"));
    EXPECT(strstr(e.msg, "line 1:") == NULL && strstr(e.msg, "line 4:") == NULL && strstr(e.msg, "line 7:") == NULL);

    /* a hundred bad lines: counted, the first CONFIG_MAX_ERRORS remembered, the message never overflows */
    char *big = malloc(100 * 12 + 1);
    EXPECT(big != NULL);
    if (big != NULL) {
        big[0] = '\0';
        for (int i = 0; i < 100; i++)
            strcat(big, "bad line x\n");
        EXPECT(load(big, &c, &e) == -1 && e.count == 100 && e.lines[CONFIG_MAX_ERRORS - 1] == CONFIG_MAX_ERRORS);
        EXPECT(strlen(e.msg) < sizeof e.msg && strstr(e.msg, "line 1:") != NULL);
        free(big);
    }
}

/* ---- 11. a failed load never changes the live configuration ---- */
static void test_failed_load_changes_nothing(void)
{
    printf("a failed load never changes the configuration, however many good lines came first\n");
    config_t c, before;
    config_error_t e;

    config_defaults(&c, 8);
    c.mac_aging_sec = 33;                      /* a "live" configuration with values that are not the defaults */
    c.wrr_weights[0] = 9;
    c.port_vlan[2] = 44;
    snprintf(c.log_file, sizeof c.log_file, "%s", "/live/log");
    before = c;

    const char *text =
        "mac_aging_seconds = 1\nmax_macs_per_port = 2\nport_wait_timeout = 3\nwrr_weights = 7,7,7\nprio_aging_ms = 5\n"
        "rate_limit = 6\nmac_table_size = 7\nqueue_capacity = 8\ntrunk_port = 1\nlog_file = /new/log\nmac_dump_file = /new/dump\n"
        "port.2.vlan = 99\nport.2.rate = 12\n"
        "this line is bad\n";
    EXPECT(config_load(mkfile(text), &c, &e) == -1);
    EXPECT(same(&c, &before));
    EXPECT(config_load(mkfile("mac_aging_seconds = 1\nport.2.vlan = 4095\n"), &c, &e) == -1 && same(&c, &before));
    EXPECT(config_load(mkfile("mac_aging_seconds = 1\n"), &c, &e) == 0 && c.mac_aging_sec == 1 && c.wrr_weights[0] == 9);
}

/* buf = prefix + n times ch + suffix (always terminated) */
static void fill(char *buf, size_t cap, const char *prefix, char ch, size_t n, const char *suffix)
{
    size_t len = (size_t)snprintf(buf, cap, "%s", prefix);

    if (len + n + strlen(suffix) + 1 > cap) {
        printf("test bug: buffer too small\n");
        exit(1);
    }
    memset(buf + len, ch, n);
    strcpy(buf + len + n, suffix);
}

/* ---- 12. long lines and line numbers after them ---- */
static void test_long_lines(void)
{
    printf("a line longer than the buffer is an error, and the line count stays right\n");
    config_t c;
    config_error_t e;
    char *text = malloc(2000);

    EXPECT(text != NULL);
    if (text == NULL)
        return;
    fill(text, 2000, "mac_aging_seconds = 5\n# ", 'x', 700, "\nmax_macs_per_port = 0\n");
    EXPECT(load(text, &c, &e) == -1 && e.count == 2);
    EXPECT(e.lines[0] == 2 && strstr(e.msg, "line too long") != NULL && e.lines[1] == 3);   /* the line after it is number 3 */

    /* a 400-character name fits in a line but is too long for a file name */
    fill(text, 2000, "log_file = ", 'a', 400, "\n");
    EXPECT(load(text, &c, &e) == -1 && strstr(e.msg, "line too long") == NULL && strstr(e.msg, "file name too long") != NULL);

    /* the limit is 510 characters per line (the buffer holds 511 bytes with the newline).
     * The last line without a newline: 510 characters are fine, 511 are "line too long". */
    fill(text, 2000, "# ", 'y', 508, "");
    EXPECT(strlen(text) == 510 && load(text, &c, &e) == 0);
    fill(text, 2000, "# ", 'y', 509, "");
    EXPECT(strlen(text) == 511 && load(text, &c, &e) == -1 && strstr(e.msg, "line too long") != NULL);
    /* with a newline: 509 characters + newline are fine, 511 + newline are too long */
    fill(text, 2000, "# ", 'y', 507, "\n");
    EXPECT(load(text, &c, &e) == 0);
    fill(text, 2000, "# ", 'y', 509, "\n");
    EXPECT(load(text, &c, &e) == -1 && strstr(e.msg, "line too long") != NULL);
    free(text);
}

int main(void)
{
    if (mkdtemp(dir) == NULL) {
        perror("mkdtemp");
        return 1;
    }
    test_defaults();
    test_valid();
    test_whitespace();
    test_missing_file();
    test_bad_numbers();
    test_ranges();
    test_unknown_and_syntax();
    test_wrr();
    test_port_keys();
    test_multiple_errors();
    test_failed_load_changes_nothing();
    test_long_lines();

    char cmd[160];
    snprintf(cmd, sizeof cmd, "rm -rf %s", dir);
    if (system(cmd) != 0)
        printf("warning: could not remove %s\n", dir);
    printf("\n%d checks, %d failures\n", checks, failures);
    printf(failures == 0 ? "ALL PASSED\n" : "SOME FAILED\n");
    return failures == 0 ? 0 : 1;
}
