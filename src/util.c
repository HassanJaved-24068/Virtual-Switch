/*
 * util.c - MAC address and time helpers.
 */
#include "common.h"

#include <ctype.h>

/* ------------------------------------------------------------------ */
/* MAC helpers                                                         */
/* ------------------------------------------------------------------ */

/* Returns 0..15 for a hex digit, or -1 if c is not a hex digit. */
static int hex_value(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/*
 * Parse "aa:bb:cc:dd:ee:ff" (exactly 17 characters) into 6 bytes.
 * Returns 0 on success and -1 on any error. On error, out is untouched.
 */
int mac_parse(const char *str, mac_t out)
{
    mac_t tmp;

    if (str == NULL || out == NULL || strlen(str) != MAC_STR_LEN - 1)
        return -1;

    for (int i = 0; i < MAC_LEN; i++) {
        int pos = i * 3;
        int hi = hex_value(str[pos]);
        int lo = hex_value(str[pos + 1]);
        if (hi < 0 || lo < 0)
            return -1;
        if (i < MAC_LEN - 1 && str[pos + 2] != ':')
            return -1;
        tmp[i] = (uint8_t)((hi << 4) | lo);
    }
    memcpy(out, tmp, MAC_LEN);
    return 0;
}

/* Write the MAC as lower-case text. outlen must be at least MAC_STR_LEN. */
int mac_format(const mac_t mac, char *out, size_t outlen)
{
    if (mac == NULL || out == NULL || outlen < MAC_STR_LEN)
        return -1;
    snprintf(out, outlen, "%02x:%02x:%02x:%02x:%02x:%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    return 0;
}

/* Broadcast is FF:FF:FF:FF:FF:FF. */
bool mac_is_broadcast(const mac_t mac)
{
    for (int i = 0; i < MAC_LEN; i++)
        if (mac[i] != 0xFF)
            return false;
    return true;
}

/* Multicast means the lowest bit of the first byte is 1. */
bool mac_is_multicast(const mac_t mac)
{
    return (mac[0] & 0x01) != 0;
}

/* ------------------------------------------------------------------ */
/* Time helpers                                                        */
/* ------------------------------------------------------------------ */

/* Current monotonic time. clock_gettime cannot fail for a valid clock id. */
struct timespec now_mono(void)
{
    struct timespec ts;
    CHECK(clock_gettime(CLOCK_MONOTONIC, &ts));
    return ts;
}

/* Return t + ms milliseconds (ms may be negative). tv_nsec stays in range. */
struct timespec ts_add_ms(struct timespec t, long ms)
{
    long long total_ns = (long long)t.tv_nsec + (long long)(ms % 1000) * 1000000LL;
    long long secs = (long long)t.tv_sec + ms / 1000;

    secs += total_ns / 1000000000LL;
    total_ns %= 1000000000LL;
    if (total_ns < 0) {
        total_ns += 1000000000LL;
        secs -= 1;
    }
    t.tv_sec = (time_t)secs;
    t.tv_nsec = (long)total_ns;
    return t;
}

/* Return a - b in milliseconds (negative if a is earlier than b). */
long ts_diff_ms(struct timespec a, struct timespec b)
{
    long long ns = ((long long)a.tv_sec - (long long)b.tv_sec) * 1000000000LL
                 + ((long long)a.tv_nsec - (long long)b.tv_nsec);
    return (long)(ns / 1000000LL);
}

/* Absolute monotonic deadline: use with a MONOTONIC condition variable. */
struct timespec deadline_mono(long ms)
{
    return ts_add_ms(now_mono(), ms);
}

/* Absolute real-time deadline: sem_timedwait requires CLOCK_REALTIME. */
struct timespec deadline_real(long sec)
{
    struct timespec ts;
    CHECK(clock_gettime(CLOCK_REALTIME, &ts));
    ts.tv_sec += sec;
    return ts;
}

/* Initialise a condition variable that measures timed waits with CLOCK_MONOTONIC. */
int cond_init_mono(pthread_cond_t *cond)
{
    pthread_condattr_t attr;
    int rc = pthread_condattr_init(&attr);
    if (rc != 0)
        return rc;
    rc = pthread_condattr_setclock(&attr, CLOCK_MONOTONIC);
    if (rc == 0)
        rc = pthread_cond_init(cond, &attr);
    CHECK_PT(pthread_condattr_destroy(&attr));
    return rc;
}
