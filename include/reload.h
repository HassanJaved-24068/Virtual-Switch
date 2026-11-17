/*
 * reload.h - the live configuration of the running switch (Part 10).
 *
 * The configuration file is parsed by config.c (pure). This module owns what happens with the result:
 * installing it at start-up, handing copies to readers, the reload on SIGHUP and the MAC dump on
 * SIGUSR1. The functions that handle the signals are called from the signal thread (sigwait), never
 * from a signal handler, so they may lock, log and do file I/O.
 */
#ifndef RELOAD_H
#define RELOAD_H

#include "config.h"

/*
 * Make `cfg` the live configuration: copy it into sw.cfg under config_lock and publish the values that
 * the other threads read (aging time, MACs per port, port wait time, table size and buckets, queue
 * capacity). Call it once in main(), after switch_init() and before any thread exists.
 */
void config_install(const config_t *cfg);

/* A copy of the live configuration. The lock is held only while copying. Any thread. */
void config_snapshot(config_t *out);

/*
 * SIGHUP: reload sw.config_path. The file is loaded into a temporary config_t; if that fails ("reload
 * failed, line N: ...") the live configuration is NOT touched. Otherwise the reloadable fields are
 * swapped in under config_lock, every change is logged, and settings that only take effect at start-up
 * are reported as "ignored until restart". Returns 0, or -1 if the reload failed.
 */
int reload_config(void);

/*
 * SIGUSR1: write the MAC table to the live mac_dump_file (see mac_dump_file() in mactable.h) and log the
 * result. Returns the number of entries, or -1 on error.
 */
int dump_mac_table(void);

#endif /* RELOAD_H */
