/*
 * config.c - parse the configuration file (Part 10). Pure: no globals, no logging.
 *
 * fgets one line at a time, cut the comment ('#'), trim, split at the first '=', and check the value
 * with strtol (errno, endptr, range). Every line is checked; if any is bad nothing is copied to the
 * caller's config_t. The error list has every bad line number.
 */
#include "config.h"

#include <ctype.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>

#define LINE_MAX_LEN 512            /* longest line, with the '\n' and the '\0' */

/* The numeric keys that have one value. Table driven: name, field, allowed range. */
struct scalar {
    const char *name;
    size_t off;
    long lo, hi;
};

static const struct scalar scalars[] = {
    {"mac_aging_seconds", offsetof(config_t, mac_aging_sec), 1, 86400},
    {"max_macs_per_port", offsetof(config_t, max_macs_per_port), 1, 65536},
    {"port_wait_timeout", offsetof(config_t, port_wait_sec), 0, 3600},
    {"prio_aging_ms", offsetof(config_t, prio_aging_ms), 1, 60000},
    {"rate_limit", offsetof(config_t, rate_limit), 0, 1000000},
    {"mac_table_size", offsetof(config_t, mac_table_size), 1, 65536},
    {"queue_capacity", offsetof(config_t, queue_capacity), 1, 65536},
    {"trunk_port", offsetof(config_t, trunk_port), 0, MAX_PORTS},
};
#define NSCALARS ((int)(sizeof scalars / sizeof scalars[0]))

#define VLAN_MIN 1
#define VLAN_MAX 4094
#define RATE_MAX 1000000
#define WRR_MIN 1
#define WRR_MAX 1000

void config_defaults(config_t *cfg, int num_ports)
{
    memset(cfg, 0, sizeof *cfg);
    cfg->num_ports = num_ports;
    cfg->mac_aging_sec = DEFAULT_MAC_AGING_SEC;
    cfg->max_macs_per_port = DEFAULT_MAX_MACS_PER_PORT;
    cfg->port_wait_sec = DEFAULT_PORT_WAIT_SEC;
    cfg->wrr_weights[0] = DEFAULT_WRR_HIGH;
    cfg->wrr_weights[1] = DEFAULT_WRR_MEDIUM;
    cfg->wrr_weights[2] = DEFAULT_WRR_LOW;
    cfg->prio_aging_ms = DEFAULT_PRIO_AGING_MS;
    cfg->rate_limit = DEFAULT_RATE_LIMIT;
    snprintf(cfg->mac_dump_file, sizeof cfg->mac_dump_file, "%s", DEFAULT_MAC_DUMP_FILE);
    for (int i = 0; i <= MAX_PORTS; i++) {
        cfg->port_vlan[i] = 0;
        cfg->port_rate[i] = -1;
    }
    cfg->mac_table_size = DEFAULT_MAC_TABLE_SIZE;
    cfg->queue_capacity = DEFAULT_QUEUE_CAPACITY;
    cfg->trunk_port = DEFAULT_TRUNK_PORT;
    snprintf(cfg->log_file, sizeof cfg->log_file, "%s", DEFAULT_LOG_FILE);
}

int config_port_rate(const config_t *cfg, int port)
{
    if (port >= 1 && port <= MAX_PORTS && cfg->port_rate[port] >= 0)
        return cfg->port_rate[port];
    return cfg->rate_limit;
}

int config_port_vlan(const config_t *cfg, int port)
{
    if (port >= 1 && port <= MAX_PORTS && cfg->port_vlan[port] >= 1)
        return cfg->port_vlan[port];
    return DEFAULT_VLAN;
}

int config_mac_buckets(int mac_table_size)
{
    int b = 1;

    if (mac_table_size >= 256)
        return 256;
    while (b * 2 <= mac_table_size)
        b *= 2;
    return b;
}

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

static void add_error(config_error_t *e, int line, const char *fmt, ...)
{
    char text[200];
    va_list ap;

    va_start(ap, fmt);
    vsnprintf(text, sizeof text, fmt, ap);
    va_end(ap);
    if (e->count < CONFIG_MAX_ERRORS) {
        size_t used = strlen(e->msg);
        int n;

        e->lines[e->count] = line;
        if (line > 0)
            n = snprintf(e->msg + used, sizeof e->msg - used, "%sline %d: %s", used ? "; " : "", line, text);
        else
            n = snprintf(e->msg + used, sizeof e->msg - used, "%s%s", used ? "; " : "", text);
        (void)n;                          /* a message that does not fit is cut, never overflows */
    }
    e->count++;
}

/* Trim leading and trailing white space (also '\r' of a DOS line end) in place. */
static char *trim(char *s)
{
    char *end;

    while (isspace((unsigned char)*s))
        s++;
    end = s + strlen(s);
    while (end > s && isspace((unsigned char)end[-1]))
        end--;
    *end = '\0';
    return s;
}

/* A whole string as a decimal long in [lo, hi]: no sign other than '-', no trailing text. NULL if good, else why. */
static const char *parse_long(const char *s, long lo, long hi, long *out)
{
    char *end;
    long v;

    if (*s == '\0' || (!isdigit((unsigned char)*s) && *s != '-'))
        return "not a number";
    errno = 0;
    v = strtol(s, &end, 10);
    if (end == s || *end != '\0')
        return "not a number (trailing characters)";
    if (errno == ERANGE || v < lo || v > hi)
        return "out of range";
    *out = v;
    return NULL;
}

static bool path_ok(const char *v, const char **why)
{
    size_t n = strlen(v);

    if (n == 0) {
        *why = "empty file name";
        return false;
    }
    if (n >= CONFIG_PATH_LEN) {
        *why = "file name too long";
        return false;
    }
    for (const char *p = v; *p; p++)
        if (iscntrl((unsigned char)*p)) {
            *why = "control character in the file name";
            return false;
        }
    return true;
}

/* "5,3,1" -> three weights (an empty or non-numeric piece is an error). NULL if good, else why. */
static const char *parse_wrr(char *v, int out[3])
{
    static const char *const need3 = "wrr_weights needs exactly 3 weights: high,medium,low";
    char *cur = v;
    int n = 0;

    for (;;) {
        char *comma = strchr(cur, ',');
        long w;

        if (comma != NULL)
            *comma = '\0';
        if (n >= 3)
            return need3;
        if (parse_long(trim(cur), WRR_MIN, WRR_MAX, &w) != NULL)
            return "bad weight in wrr_weights (each must be a number 1..1000)";
        out[n++] = (int)w;
        if (comma == NULL)
            break;
        cur = comma + 1;
    }
    return n == 3 ? NULL : need3;
}

/* ------------------------------------------------------------------ */
/* One "key = value" line                                              */
/* ------------------------------------------------------------------ */

struct seen {
    bool scalar[NSCALARS];
    bool wrr, log_file, dump_file;
    bool vlan[MAX_PORTS + 1], rate[MAX_PORTS + 1];
};

static void parse_line(config_t *c, struct seen *seen, config_error_t *e, int lineno, char *key, char *value)
{
    const char *why;
    long v;

    /* numeric keys with one value */
    for (int i = 0; i < NSCALARS; i++) {
        if (strcmp(key, scalars[i].name) != 0)
            continue;
        if (seen->scalar[i]) {
            add_error(e, lineno, "duplicate key '%s'", key);
            return;
        }
        seen->scalar[i] = true;
        why = parse_long(value, scalars[i].lo, scalars[i].hi, &v);
        if (why != NULL) {
            if (strcmp(why, "out of range") == 0)
                add_error(e, lineno, "%s = %s is out of range (%ld..%ld)", key, value, scalars[i].lo, scalars[i].hi);
            else
                add_error(e, lineno, "%s: bad value '%s' (%s)", key, value, why);
            return;
        }
        if (strcmp(key, "trunk_port") == 0 && v > c->num_ports) {
            add_error(e, lineno, "trunk_port = %ld: this switch has only %d ports", v, c->num_ports);
            return;
        }
        *(int *)((char *)c + scalars[i].off) = (int)v;
        return;
    }

    if (strcmp(key, "wrr_weights") == 0) {
        if (seen->wrr) {
            add_error(e, lineno, "duplicate key '%s'", key);
            return;
        }
        seen->wrr = true;
        int w[3];
        why = parse_wrr(value, w);
        if (why != NULL) {
            add_error(e, lineno, "%s", why);
            return;
        }
        memcpy(c->wrr_weights, w, sizeof w);
        return;
    }

    if (strcmp(key, "log_file") == 0 || strcmp(key, "mac_dump_file") == 0) {
        bool is_log = key[0] == 'l';
        bool *flag = is_log ? &seen->log_file : &seen->dump_file;

        if (*flag) {
            add_error(e, lineno, "duplicate key '%s'", key);
            return;
        }
        *flag = true;
        if (!path_ok(value, &why)) {
            add_error(e, lineno, "%s: %s", key, why);
            return;
        }
        snprintf(is_log ? c->log_file : c->mac_dump_file, CONFIG_PATH_LEN, "%s", value);
        return;
    }

    /* port.N.vlan and port.N.rate */
    if (strncmp(key, "port.", 5) == 0) {
        char *end;
        long n;

        errno = 0;
        n = strtol(key + 5, &end, 10);
        if (end != key + 5 && *end == '.' && errno == 0 && isdigit((unsigned char)key[5]) &&
            (strcmp(end + 1, "vlan") == 0 || strcmp(end + 1, "rate") == 0)) {
            bool is_vlan = end[1] == 'v';

            if (n < 1 || n > c->num_ports) {
                add_error(e, lineno, "%s: this switch has ports 1..%d", key, c->num_ports);
                return;
            }
            bool *flag = is_vlan ? &seen->vlan[n] : &seen->rate[n];
            if (*flag) {
                add_error(e, lineno, "duplicate key '%s'", key);
                return;
            }
            *flag = true;
            why = is_vlan ? parse_long(value, VLAN_MIN, VLAN_MAX, &v) : parse_long(value, 0, RATE_MAX, &v);
            if (why != NULL) {
                if (strcmp(why, "out of range") == 0)
                    add_error(e, lineno, "%s = %s is out of range (%d..%d)", key, value,
                              is_vlan ? VLAN_MIN : 0, is_vlan ? VLAN_MAX : RATE_MAX);
                else
                    add_error(e, lineno, "%s: bad value '%s' (%s)", key, value, why);
                return;
            }
            if (is_vlan)
                c->port_vlan[n] = (int)v;
            else
                c->port_rate[n] = (int)v;
            return;
        }
    }

    add_error(e, lineno, "unknown key '%s'", key);
}

/* ------------------------------------------------------------------ */

int config_load(const char *path, config_t *cfg, config_error_t *err)
{
    config_t tmp = *cfg;                       /* all changes go into this copy */
    config_error_t e;
    struct seen seen;
    char line[LINE_MAX_LEN];
    int lineno = 0;
    FILE *f;

    memset(&e, 0, sizeof e);
    memset(&seen, 0, sizeof seen);

    f = fopen(path, "r");
    if (f == NULL) {
        add_error(&e, 0, "cannot open '%s': %s", path, strerror(errno));
        goto failed;
    }

    while (fgets(line, sizeof line, f) != NULL) {
        size_t len = strlen(line);
        bool too_long = len > 0 && line[len - 1] != '\n' && !feof(f);
        char *hash, *eq, *key, *value;

        lineno++;
        if (too_long) {                        /* the buffer was full: skip the rest of this line */
            int ch;

            while ((ch = fgetc(f)) != EOF && ch != '\n')
                ;
            add_error(&e, lineno, "line too long (more than %d characters)", LINE_MAX_LEN - 2);
            continue;
        }
        hash = strchr(line, '#');
        if (hash != NULL)
            *hash = '\0';
        key = trim(line);
        if (*key == '\0')
            continue;                          /* empty or comment only */
        eq = strchr(key, '=');
        if (eq == NULL) {
            add_error(&e, lineno, "expected 'key = value'");
            continue;
        }
        *eq = '\0';
        value = trim(eq + 1);
        key = trim(key);
        if (*key == '\0') {
            add_error(&e, lineno, "missing key before '='");
            continue;
        }
        if (*value == '\0') {
            add_error(&e, lineno, "missing value for '%s'", key);
            continue;
        }
        parse_line(&tmp, &seen, &e, lineno, key, value);
    }
    if (ferror(f))
        add_error(&e, 0, "read error on '%s': %s", path, strerror(errno));
    if (fclose(f) != 0)
        add_error(&e, 0, "closing '%s' failed: %s", path, strerror(errno));
    f = NULL;

failed:
    if (e.count > 0) {
        if (e.count > CONFIG_MAX_ERRORS) {
            size_t used = strlen(e.msg);
            snprintf(e.msg + used, sizeof e.msg - used, "; ... (%d more)", e.count - CONFIG_MAX_ERRORS);
        }
        if (err != NULL)
            *err = e;
        return -1;
    }
    if (err != NULL)
        *err = e;
    *cfg = tmp;                                /* the only place where the caller's config changes */
    return 0;
}
