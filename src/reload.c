/*
 * reload.c - the live configuration, SIGHUP reload and SIGUSR1 dump (Part 10).
 *
 * Locking: config_lock is a leaf. It is held only to copy a config_t or to swap fields; all file I/O
 * (parsing the file, writing the dump) and all logging happen with it released.
 */
#include "reload.h"

#include "log.h"
#include "mactable.h"
#include "port.h"

void config_install(const config_t *cfg)
{
    CHECK_PT(pthread_mutex_lock(&sw.config_lock));
    sw.cfg = *cfg;
    atomic_store(&sw.mac_aging_sec, cfg->mac_aging_sec);
    atomic_store(&sw.max_macs_per_port, cfg->max_macs_per_port);
    atomic_store(&sw.port_wait_sec, cfg->port_wait_sec);
    sw.mac_table_size = cfg->mac_table_size;
    sw.mac_buckets = config_mac_buckets(cfg->mac_table_size);
    sw.queue_capacity = cfg->queue_capacity;
    CHECK_PT(pthread_mutex_unlock(&sw.config_lock));
}

void config_snapshot(config_t *out)
{
    CHECK_PT(pthread_mutex_lock(&sw.config_lock));
    *out = sw.cfg;
    CHECK_PT(pthread_mutex_unlock(&sw.config_lock));
}

/* ------------------------------------------------------------------ */
/* SIGHUP                                                              */
/* ------------------------------------------------------------------ */

/* Log "key: old -> new" for an int setting. `note` explains a setting that nothing uses yet. */
static int report_int(const char *key, int old, int now, const char *note)
{
    if (old == now)
        return 0;
    log_msg(LOG_INFO, "reload: %s %d -> %d%s%s", key, old, now, note ? " " : "", note ? note : "");
    return 1;
}

/* A startup-only setting that the file now says differently. */
static void report_ignored(const char *key, int running, int in_file)
{
    if (running != in_file)
        log_msg(LOG_WARN, "reload: %s = %d in the file, running with %d: ignored until restart", key, in_file, running);
}

int reload_config(void)
{
    config_t fresh, old;
    config_error_t err;
    int changes = 0;

    if (sw.config_path == NULL) {
        log_msg(LOG_INFO, "SIGHUP received: no configuration file (the switch was started without -c): nothing to reload");
        return 0;
    }

    /* 1. Load into a temporary struct (the defaults, then the file). No lock, no change to the live config. */
    config_defaults(&fresh, sw.num_ports);
    if (config_load(sw.config_path, &fresh, &err) != 0) {
        log_msg(LOG_ERR, "reload failed, %s (%d error%s); the previous configuration stays in force",
                err.msg, err.count, err.count == 1 ? "" : "s");
        return -1;
    }

    /* 2. Swap the reloadable fields under config_lock. The startup-only fields keep their running values. */
    CHECK_PT(pthread_mutex_lock(&sw.config_lock));
    old = sw.cfg;
    sw.cfg.mac_aging_sec = fresh.mac_aging_sec;
    sw.cfg.max_macs_per_port = fresh.max_macs_per_port;
    sw.cfg.port_wait_sec = fresh.port_wait_sec;
    memcpy(sw.cfg.wrr_weights, fresh.wrr_weights, sizeof fresh.wrr_weights);
    sw.cfg.prio_aging_ms = fresh.prio_aging_ms;
    sw.cfg.rate_limit = fresh.rate_limit;
    memcpy(sw.cfg.mac_dump_file, fresh.mac_dump_file, sizeof fresh.mac_dump_file);
    memcpy(sw.cfg.port_vlan, fresh.port_vlan, sizeof fresh.port_vlan);
    memcpy(sw.cfg.port_rate, fresh.port_rate, sizeof fresh.port_rate);
    atomic_store(&sw.mac_aging_sec, fresh.mac_aging_sec);               /* read by the aging thread, once per cycle */
    atomic_store(&sw.max_macs_per_port, fresh.max_macs_per_port);       /* read by every learn */
    atomic_store(&sw.port_wait_sec, fresh.port_wait_sec);               /* read by the accept thread */
    CHECK_PT(pthread_mutex_unlock(&sw.config_lock));

    /* 3. Report. Logging happens with the lock released. */
    changes += report_int("mac_aging_seconds", old.mac_aging_sec, fresh.mac_aging_sec, "(the aging thread uses it from its next cycle)");
    changes += report_int("max_macs_per_port", old.max_macs_per_port, fresh.max_macs_per_port, "(applies to the next learned MAC)");
    changes += report_int("port_wait_timeout", old.port_wait_sec, fresh.port_wait_sec, "(applies to the next waiting host)");
    if (memcmp(old.wrr_weights, fresh.wrr_weights, sizeof old.wrr_weights) != 0) {
        log_msg(LOG_INFO, "reload: wrr_weights %d,%d,%d -> %d,%d,%d (stored; no effect until the priority queues of Part 11)",
                old.wrr_weights[0], old.wrr_weights[1], old.wrr_weights[2],
                fresh.wrr_weights[0], fresh.wrr_weights[1], fresh.wrr_weights[2]);
        changes++;
    }
    changes += report_int("prio_aging_ms", old.prio_aging_ms, fresh.prio_aging_ms, "(stored; no effect until Part 11)");
    changes += report_int("rate_limit", old.rate_limit, fresh.rate_limit, "(stored; no effect until the rate limiting of Part 12)");
    if (strcmp(old.mac_dump_file, fresh.mac_dump_file) != 0) {
        log_msg(LOG_INFO, "reload: mac_dump_file '%s' -> '%s'", old.mac_dump_file, fresh.mac_dump_file);
        changes++;
    }
    for (int i = 1; i <= sw.num_ports; i++) {
        /* The effects belong to later parts: VLAN switching (Part 13/16) and the token bucket (Part 12). The
         * values are kept in the live configuration so that those parts can read them; nothing pretends that
         * a port's VLAN or rate is applied now. */
        if (old.port_vlan[i] != fresh.port_vlan[i]) {
            log_msg(LOG_INFO, "reload: port.%d.vlan %d -> %d (stored; VLANs are not applied until Part 13)", i,
                    old.port_vlan[i], fresh.port_vlan[i]);
            changes++;
        }
        if (old.port_rate[i] != fresh.port_rate[i]) {
            log_msg(LOG_INFO, "reload: port.%d.rate %d -> %d (stored; rate limiting is not applied until Part 12)", i,
                    old.port_rate[i], fresh.port_rate[i]);
            changes++;
        }
    }
    report_ignored("mac_table_size", old.mac_table_size, fresh.mac_table_size);
    report_ignored("queue_capacity", old.queue_capacity, fresh.queue_capacity);
    report_ignored("trunk_port", old.trunk_port, fresh.trunk_port);
    if (strcmp(old.log_file, fresh.log_file) != 0)
        log_msg(LOG_WARN, "reload: log_file = '%s' in the file, it was '%s' at start: ignored until restart",
                fresh.log_file, old.log_file);

    log_msg(LOG_INFO, "configuration reloaded from '%s': %d setting%s changed (mac aging %ds)", sw.config_path, changes,
            changes == 1 ? "" : "s", fresh.mac_aging_sec);
    return 0;
}

/* ------------------------------------------------------------------ */
/* SIGUSR1                                                             */
/* ------------------------------------------------------------------ */

int dump_mac_table(void)
{
    config_t c;
    int n;

    config_snapshot(&c);                      /* the file name, copied; the lock is released before any I/O */
    n = mac_dump_file(c.mac_dump_file);
    if (n < 0) {
        int e = errno;

        log_msg(LOG_ERR, "SIGUSR1: cannot write the MAC table to '%s': %s", c.mac_dump_file, strerror(e));
        return -1;
    }
    log_msg(LOG_INFO, "SIGUSR1: MAC table dumped to '%s' (%d entr%s)", c.mac_dump_file, n, n == 1 ? "y" : "ies");
    return n;
}
