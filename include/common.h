/*
 * common.h - constants, small types and helper declarations shared by
 * every program in the Virtual Switch project.
 */
#ifndef COMMON_H
#define COMMON_H

#include <errno.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* ---- Fixed constants (roadmap section 2.3) ---- */
#define MAX_PORTS 64          /* valid port numbers are 1..num_ports */
#define MAX_PAYLOAD 1500      /* largest allowed payload in bytes */
#define HDR_LEN 17            /* frame header is always 17 bytes */

/*
 * ---- Default settings (these become config options in Part 10) ----
 * They can be overridden at compile time (-DNAME=value). The Makefile uses this
 * to build `switch_small`, a switch with a tiny MAC table and short aging time,
 * for the tests, until the config file exists.
 */
#ifndef DEFAULT_QUEUE_CAPACITY
#define DEFAULT_QUEUE_CAPACITY 64     /* frames per priority class */
#endif
#ifndef DEFAULT_MAC_AGING_SEC
#define DEFAULT_MAC_AGING_SEC 300     /* MAC aging time T */
#endif
#ifndef DEFAULT_MAC_TABLE_SIZE
#define DEFAULT_MAC_TABLE_SIZE 1024   /* MAC table entries K */
#endif
#ifndef DEFAULT_MAC_BUCKETS
#define DEFAULT_MAC_BUCKETS 256       /* hash buckets (power of 2) */
#endif
#ifndef DEFAULT_MAX_MACS_PER_PORT
#define DEFAULT_MAX_MACS_PER_PORT 16  /* port security limit M */
#endif
#ifndef DEFAULT_PORT_WAIT_SEC
#define DEFAULT_PORT_WAIT_SEC 5       /* wait time for a free port */
#endif
#define DEFAULT_PRIO_AGING_MS 200     /* low-priority aging */
#define DEFAULT_WRR_HIGH 5            /* weighted round robin weights (Part 11) */
#define DEFAULT_WRR_MEDIUM 3
#define DEFAULT_WRR_LOW 1
#define DEFAULT_RATE_LIMIT 0          /* frames per second per port, 0 = unlimited (Part 12) */
#define DEFAULT_TRUNK_PORT 0          /* 0 = none (Part 13) */
#define DEFAULT_MAC_DUMP_FILE "mac_dump.txt"
#define DEFAULT_VLAN 1
#define DEFAULT_LOG_FILE "switch.log" /* config log_file (Part 10); until then the environment
                                       * variable VSWITCH_LOG_FILE may name another file */

#define MAC_LEN 6                     /* bytes in a MAC address */
#define MAC_SHM_FMT "/vswitch_mac_%d"   /* Part 20: the shared-memory MAC table of a switch started with --persist-mac, named by its TCP port */
#define MAC_STR_LEN 18                /* "aa:bb:cc:dd:ee:ff" + '\0' */

/* A MAC address is just 6 bytes. */
typedef uint8_t mac_t[MAC_LEN];

/*
 * CHECK(call): for system calls that return -1 on error and set errno.
 * Prints the error with file and line, then exits. Use it at startup only.
 */
#define CHECK(call)                                                        \
    do {                                                                   \
        if ((call) == -1) {                                                \
            fprintf(stderr, "%s:%d: %s failed: %s\n", __FILE__, __LINE__,  \
                    #call, strerror(errno));                               \
            exit(EXIT_FAILURE);                                            \
        }                                                                  \
    } while (0)

/*
 * CHECK_PT(rc): for pthread calls. They return an error code (0 = success)
 * and do NOT set errno, so we pass the returned code to strerror.
 */
#define CHECK_PT(rc)                                                       \
    do {                                                                   \
        int check_pt_rc_ = (rc);                                           \
        if (check_pt_rc_ != 0) {                                           \
            fprintf(stderr, "%s:%d: %s failed: %s\n", __FILE__, __LINE__,  \
                    #rc, strerror(check_pt_rc_));                          \
            exit(EXIT_FAILURE);                                            \
        }                                                                  \
    } while (0)

/* ---- Strict numeric command-line arguments (util.c) ---- */
/*
 * Parse an unsigned decimal number: ONLY the characters '0'-'9' (so no sign, no white space, no "0x", no decimal point), at least one, any number of leading zeros.
 * strtol() is not used because it accepts leading white space and a '+' or '-' sign. Overflow is detected while the digits are accumulated, and the value must be in
 * [lo, hi] (hi <= LONG_MAX). Returns 0 and stores the value in *out, or -1 with errno = EINVAL (empty / NULL / not only digits) or ERANGE (too big or outside [lo, hi]);
 * *out is untouched on an error.
 */
int parse_uint(const char *s, unsigned long lo, unsigned long hi, long *out);

/* ---- MAC helpers (util.c) ---- */
int mac_parse(const char *str, mac_t out);                /* 0 ok, -1 bad */
int mac_format(const mac_t mac, char *out, size_t outlen);/* 0 ok, -1 small */
bool mac_is_broadcast(const mac_t mac);
bool mac_is_multicast(const mac_t mac);                   /* low bit of byte 0 */

/* ---- Time helpers (util.c) ---- */
struct timespec now_mono(void);                           /* CLOCK_MONOTONIC */
struct timespec ts_add_ms(struct timespec t, long ms);
long ts_diff_ms(struct timespec a, struct timespec b);    /* a - b in ms */
struct timespec deadline_mono(long ms);                   /* now + ms (mono) */
struct timespec deadline_real(long sec);                  /* now + sec (real) */
int cond_init_mono(pthread_cond_t *cond);                 /* 0 ok, else errno */

#endif /* COMMON_H */
