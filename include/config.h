/*
 * config.h - the configuration file (Part 10).
 *
 * Format ("key = value", '#' starts a comment, see README.md):
 *   mac_aging_seconds = 300      reloadable          mac_table_size = 1024   startup only
 *   max_macs_per_port = 16       reloadable          queue_capacity = 64     startup only
 *   port_wait_timeout = 5        reloadable          trunk_port     = 0      startup only
 *   wrr_weights       = 5,3,1    reloadable          log_file = switch.log   startup only
 *   prio_aging_ms     = 200      reloadable          mac_dump_file = mac_dump.txt   reloadable
 *   rate_limit        = 0        reloadable
 *   port.N.vlan = 20             reloadable          port.N.rate = 100       reloadable
 *
 * This part of the module is PURE: it parses a file into a config_t and does not touch the running
 * switch (no globals, no logging), so it can be tested alone. The live configuration, the
 * reload on SIGHUP and the MAC dump on SIGUSR1 are in reload.c.
 */
#ifndef CONFIG_H
#define CONFIG_H

#include <stddef.h>

#include "common.h"

#define CONFIG_PATH_LEN 256        /* longest file name (with the '\0') */
#define CONFIG_MAX_ERRORS 16       /* errors remembered per file; the rest are only counted */

typedef struct {
    int num_ports;                 /* from the command line (not a file key): limits port.N.* and trunk_port */

    /* ---- reloadable (SIGHUP) ---- */
    int mac_aging_sec;             /* mac_aging_seconds, 1..86400 */
    int max_macs_per_port;         /* max_macs_per_port, 1..65536 */
    int port_wait_sec;             /* port_wait_timeout, 0..3600 */
    int wrr_weights[3];            /* wrr_weights = high,medium,low; each 1..1000 */
    int prio_aging_ms;             /* prio_aging_ms, 1..60000 */
    int rate_limit;                /* rate_limit, frames/s for every port, 0..1000000, 0 = unlimited */
    char mac_dump_file[CONFIG_PATH_LEN];
    int port_vlan[MAX_PORTS + 1];  /* port.N.vlan, 1..4094; 0 = not set in the file (default VLAN) */
    int port_rate[MAX_PORTS + 1];  /* port.N.rate, 0..1000000; -1 = not set (rate_limit applies) */

    /* ---- startup only: a change in a reload is reported as "ignored until restart" ---- */
    int mac_table_size;            /* mac_table_size, 1..65536 */
    int queue_capacity;            /* queue_capacity, 1..65536 frames per priority class */
    int trunk_port;                /* trunk_port, 0 (none) or 1..num_ports */
    char log_file[CONFIG_PATH_LEN];
} config_t;

typedef struct {
    int count;                     /* number of errors found (also those not remembered) */
    int lines[CONFIG_MAX_ERRORS];  /* line numbers of the first errors (0 = the file itself) */
    char msg[640];                 /* "line 3: ...; line 7: ..." for all remembered errors */
} config_error_t;

/* Fill cfg with the defaults of roadmap 2.3 for a switch with num_ports ports. */
void config_defaults(config_t *cfg, int num_ports);

/*
 * Read the file `path` into *cfg, which the caller has filled with config_defaults() (the keys that
 * the file does not mention keep that value). The whole file is checked; if ANY line is bad
 * (unknown key, bad number, out of range, duplicate key, missing '=', line too long ...) or the file
 * cannot be read, *cfg is NOT changed, -1 is returned and *err lists every bad line with its line
 * number. Returns 0 on success. `err` may be NULL.
 */
int config_load(const char *path, config_t *cfg, config_error_t *err);

/* The rate (frames/s, 0 = unlimited) of port `port` (1..MAX_PORTS): port.N.rate if set, else rate_limit. */
int config_port_rate(const config_t *cfg, int port);

/* The VLAN of access port `port` (1..MAX_PORTS): port.N.vlan if set, else DEFAULT_VLAN (1). */
int config_port_vlan(const config_t *cfg, int port);

/* Bucket count for a table of K entries: 256, or the largest power of two <= K for a small table. */
int config_mac_buckets(int mac_table_size);

#endif /* CONFIG_H */
