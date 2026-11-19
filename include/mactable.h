/*
 * mactable.h - concurrent MAC address table (Part 6).
 *
 * One contiguous region (shared-memory ready: no pointers, only indices):
 *
 *     [ mactable_t header ][ bucket[0..nbuckets-1] ][ entry[0..K-1] ]
 *
 * Entries are chained inside their bucket and kept on a free list by INDEX.
 * Key = (vlan, mac). Bucket = FNV-1a(key) & (nbuckets - 1).
 *
 * Locks (global order, roadmap 2.6):  ports_lock -> bucket lock -> pool_lock.
 *   - a bucket lock protects the entries and chain links of that bucket;
 *   - pool_lock protects free_head, the free-list links, used flags and count;
 *   - at most ONE bucket lock is held at a time, and nobody waits for a
 *     bucket lock while holding pool_lock.
 * The mutexes are PROCESS_SHARED and ROBUST (needed by bonus Part 20; they
 * work in ordinary heap memory too).
 *
 * Part 21 (benchmark): mactable.c can be built with another locking strategy, -DMAC_LOCK=STRIPED (default, the rules above), GLOBAL (ONE mutex instead of the bucket
 * locks) or RWLOCK (ONE pthread_rwlock_t: mac_lookup takes the read lock, learn / age / flush / evict / clear take the write lock). pool_lock is unchanged. The variants
 * exist to be measured (bench/); the --persist-mac recovery of Part 20 is only supported by STRIPED (mac_attach_shm returns ENOTSUP otherwise).
 */
#ifndef MACTABLE_H
#define MACTABLE_H

#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stddef.h>
#include <time.h>

#include "common.h"

#define MAC_TABLE_MAGIC 0x4D414354u            /* "MACT" */

typedef struct {
    uint8_t mac[MAC_LEN];
    uint16_t vlan;
    int16_t port;
    int32_t next;                  /* next entry in the chain (or in the free list), -1 = end */
    struct timespec last_seen;     /* monotonic clock; refreshed when the MAC is seen as a SOURCE */
    bool used;                     /* set and cleared under pool_lock */
} mac_entry_t;

typedef struct {
    pthread_mutex_t lock;
    int32_t first;                 /* first entry of the chain, -1 = empty */
} mac_bucket_t;

typedef struct {
    uint32_t magic;
    int32_t K;                     /* number of entries in the pool */
    int32_t nbuckets;              /* power of two */
    /* Part 21: the lock of the GLOBAL and RWLOCK variants (mactable.c, -DMAC_LOCK=GLOBAL | RWLOCK). They exist in every build so that the layout never depends on the
     * variant; the default (STRIPED) build uses the per-bucket locks and never touches them. */
    pthread_mutex_t global_lock;
    pthread_rwlock_t rw_lock;
    pthread_mutex_t pool_lock;
    int32_t free_head;             /* first free entry, -1 = pool empty */
    int count;                     /* entries in use */
} mactable_t;

/* Result of mac_learn(). */
enum learn_rc {
    LEARN_OK,          /* learned, refreshed or moved */
    LEARN_VIOLATION,   /* the port now has more than M MACs: shut it down (port security) */
    LEARN_SKIPPED      /* nothing learned: table full after the retries, bad port, or no table */
};

/* Filled by mac_learn_ex() when a MAC moved from one port to another. */
struct mac_move {
    bool moved;
    uint16_t vlan;
    mac_t mac;
    int from, to;
};

/* What mac_foreach() hands to its callback. */
typedef struct {
    mac_t mac;
    uint16_t vlan;
    int port;
    long age_ms;
} mac_info_t;

/* Bytes needed for a table with K entries and nbuckets buckets. 0 if the sizes are invalid. */
size_t mac_region_size(int K, int nbuckets);

/*
 * Build a table in `region` (at least mac_region_size() bytes, 8-byte aligned)
 * and make it THE table used by all the functions below. K >= 1, nbuckets a
 * power of two. Returns 0 or an errno value.
 */
int mac_init(void *region, int K, int nbuckets);

/* Destroy the mutexes of the current table (the caller frees the region). */
void mac_destroy(void);

/*
 * Part 20 (--persist-mac): put the table in the POSIX shared-memory object `name` instead of the heap. shm_open(O_CREAT, no O_EXCL); if the object has exactly the size of
 * a K / nbuckets table and the header matches (magic, K, nbuckets), the table is REUSED and recovered, otherwise the object is resized and initialised from scratch.
 * Recovery (the switch is not running any thread yet): every bucket lock is taken (a lock held by the dead process comes back as EOWNERDEAD and is made consistent),
 * every chain is walked and checked (index in range, entry reached once, right bucket, valid port: a bad entry is dropped, the chain is cut there), the free list
 * and the entry count are REBUILT by a scan, and the mac_count of every port is rebuilt from the entries. *recovered = number of entries kept, or -1 for a fresh table.
 * On success *region / *len are the mapping (the caller munmaps it after mac_detach). Returns 0 or an errno value.
 */
int mac_attach_shm(const char *name, int K, int nbuckets, void **region, size_t *len, int *recovered);

/* Forget the table WITHOUT destroying its mutexes (a shared table is reused by the next switch; destroyed mutexes could not be locked again). */
void mac_detach(void);

/*
 * Learn the source MAC `mac` of a frame that came in on `port` (1..MAX_PORTS;
 * 0 is the bonus trunk). The caller must NOT pass a multicast or broadcast
 * source. mac_learn_ex() reports a move instead of logging it (so a caller
 * that holds a lock does not have to write the log while holding it);
 * mac_learn() logs "MAC moved: ..." itself.
 */
enum learn_rc mac_learn_ex(uint16_t vlan, const mac_t mac, int port, struct mac_move *mv);
enum learn_rc mac_learn(uint16_t vlan, const mac_t mac, int port);
void mac_log_move(const struct mac_move *mv);

/* The port of (vlan, mac) or -1. Does NOT refresh last_seen. */
int mac_lookup(uint16_t vlan, const mac_t mac);

/* Remove the entry that was seen longest ago. Returns true if one was freed. */
bool mac_evict_lru(void);

/* Remove entries not seen for more than max_age_sec seconds. Returns how many. */
int mac_age(long max_age_sec);

/* Remove every entry of one port / of all ports. Return how many were removed. */
int mac_flush_port(int port);
int mac_clear(void);

/*
 * Call cb for every entry (a copy, so cb runs with NO lock held and may call
 * any mac_* function). Returns the number of entries visited, or -1 on a
 * memory error.
 */
int mac_foreach(void (*cb)(const mac_info_t *info, void *arg), void *arg);

/* Write a header line and one line per entry to fp. Returns the number of entries. */
int mac_dump(FILE *fp);

/*
 * SIGUSR1 dump (Part 10): create/truncate `path` (mode "w") and write
 *     # MAC table dump, YYYY-MM-DD HH:MM:SS
 *     # vlan mac port age_s
 *     <vlan> <mac> <port> <age_s>          one line per entry, age in whole seconds since last seen
 * The buckets are visited one at a time (mac_foreach): each bucket's entries are copied under its lock and
 * written after the lock is released, so no MAC lock is held during file I/O. The result is a consistent
 * copy of every bucket, not one atomic snapshot of the whole table (the table keeps changing meanwhile).
 * Returns the number of entries written, or -1 with errno set (cannot open, write error, no memory).
 */
int mac_dump_file(const char *path);

/* Entries in use. */
int mac_size(void);

/*
 * Consistency check, for tests (call it while nothing else changes the table):
 *   - every port's mac_count equals the number of its entries in the table
 *   - count + free entries = K, the free list and the chains have no cycles,
 *     every entry is in the right bucket and has the right `used` flag.
 * Returns 0 if everything is consistent; otherwise -1 and a description in why.
 */
int mac_check_counts(char *why, size_t why_len);

/* Aging thread: every second removes entries older than sw.mac_aging_sec. */
void *aging_main(void *arg);

#endif /* MACTABLE_H */
