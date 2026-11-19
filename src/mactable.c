/*
 * mactable.c - concurrent MAC table with striped bucket locks (Part 6).
 * See mactable.h for the layout and the lock rules.
 */
#include "mactable.h"

#include "log.h"
#include "port.h"

#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

/*
 * Part 21: the locking strategy of the table, chosen at compile time with -DMAC_LOCK=STRIPED (default) | GLOBAL | RWLOCK (see mactable.h). The only difference is what
 * bucket_lock / bucket_rdlock / bucket_unlock do; every caller is the same code. At most ONE of these locks is held at a time (also for GLOBAL and RWLOCK, which would
 * otherwise deadlock on a second one), so the three variants are interchangeable.
 */
#define STRIPED 1
#define GLOBAL 2
#define RWLOCK 3
#ifndef MAC_LOCK
#define MAC_LOCK STRIPED
#endif
#if MAC_LOCK != STRIPED && MAC_LOCK != GLOBAL && MAC_LOCK != RWLOCK
#error "MAC_LOCK must be STRIPED, GLOBAL or RWLOCK"
#endif

#define LEARN_RETRIES 3                 /* evictions tried when the pool is empty */
#define MAX_BUCKETS (1 << 20)
#define MAX_ENTRIES (1 << 20)

static mactable_t *mt;                  /* THE table (inside a caller-provided region) */

/* ------------------------------------------------------------------ */
/* Layout helpers                                                      */
/* ------------------------------------------------------------------ */

static mac_bucket_t *buckets(mactable_t *t)
{
    return (mac_bucket_t *)(t + 1);
}

static mac_entry_t *entries(mactable_t *t)
{
    return (mac_entry_t *)(buckets(t) + t->nbuckets);
}

static bool is_power_of_two(int n)
{
    return n > 0 && (n & (n - 1)) == 0;
}

size_t mac_region_size(int K, int nbuckets)
{
    if (K < 1 || K > MAX_ENTRIES || !is_power_of_two(nbuckets) || nbuckets > MAX_BUCKETS)
        return 0;
    return sizeof(mactable_t) + (size_t)nbuckets * sizeof(mac_bucket_t) +
           (size_t)K * sizeof(mac_entry_t);
}

/* FNV-1a over the 8 key bytes: vlan (2 bytes, big endian) then the MAC. */
static uint32_t key_hash(uint16_t vlan, const mac_t mac)
{
    uint8_t key[8];
    uint32_t h = 2166136261u;

    key[0] = (uint8_t)(vlan >> 8);
    key[1] = (uint8_t)(vlan & 0xFF);
    memcpy(key + 2, mac, MAC_LEN);
    for (int i = 0; i < 8; i++) {
        h ^= key[i];
        h *= 16777619u;
    }
    return h;
}

static int bucket_of(uint16_t vlan, const mac_t mac)
{
    return (int)(key_hash(vlan, mac) & (uint32_t)(mt->nbuckets - 1));
}

/* ------------------------------------------------------------------ */
/* Locks                                                               */
/* ------------------------------------------------------------------ */

/*
 * Lock a robust mutex. If the previous owner died while holding it
 * (EOWNERDEAD) we are the new owner: mark the mutex consistent and go on.
 * (The data it protects may need repair; the bonus supervisor rebuilds the
 * counts, Part 20.) Any other error is a bug and ends the program.
 */
static void lock_robust(pthread_mutex_t *m)
{
    int rc = pthread_mutex_lock(m);

    if (rc == EOWNERDEAD)
        rc = pthread_mutex_consistent(m);
    CHECK_PT(rc);
}

#if MAC_LOCK == STRIPED
static void bucket_lock(mac_bucket_t *b)   { lock_robust(&b->lock); }
static void bucket_rdlock(mac_bucket_t *b) { lock_robust(&b->lock); }      /* a mutex has no read side */
static void bucket_unlock(mac_bucket_t *b) { CHECK_PT(pthread_mutex_unlock(&b->lock)); }
#elif MAC_LOCK == GLOBAL
static void bucket_lock(mac_bucket_t *b)   { (void)b; lock_robust(&mt->global_lock); }
static void bucket_rdlock(mac_bucket_t *b) { (void)b; lock_robust(&mt->global_lock); }
static void bucket_unlock(mac_bucket_t *b) { (void)b; CHECK_PT(pthread_mutex_unlock(&mt->global_lock)); }
#else /* RWLOCK */
static void bucket_lock(mac_bucket_t *b)   { (void)b; CHECK_PT(pthread_rwlock_wrlock(&mt->rw_lock)); }
static void bucket_rdlock(mac_bucket_t *b) { (void)b; CHECK_PT(pthread_rwlock_rdlock(&mt->rw_lock)); }
static void bucket_unlock(mac_bucket_t *b) { (void)b; CHECK_PT(pthread_rwlock_unlock(&mt->rw_lock)); }
#endif
static void pool_lock(void)               { lock_robust(&mt->pool_lock); }
static void pool_unlock(void)             { CHECK_PT(pthread_mutex_unlock(&mt->pool_lock)); }

/* ------------------------------------------------------------------ */
/* init / destroy                                                      */
/* ------------------------------------------------------------------ */

static int init_robust_mutex(pthread_mutex_t *m)
{
    pthread_mutexattr_t attr;
    int rc = pthread_mutexattr_init(&attr);

    if (rc != 0)
        return rc;
    rc = pthread_mutexattr_setpshared(&attr, PTHREAD_PROCESS_SHARED);
    if (rc == 0)
        rc = pthread_mutexattr_setrobust(&attr, PTHREAD_MUTEX_ROBUST);
    if (rc == 0)
        rc = pthread_mutex_init(m, &attr);
    CHECK_PT(pthread_mutexattr_destroy(&attr));
    return rc;
}

int mac_init(void *region, int K, int nbuckets)
{
    size_t size = mac_region_size(K, nbuckets);

    if (size == 0 || region == NULL || ((uintptr_t)region % _Alignof(mactable_t)) != 0)
        return EINVAL;

    memset(region, 0, size);
    mactable_t *t = region;
    t->K = K;
    t->nbuckets = nbuckets;
    t->count = 0;

    int rc = init_robust_mutex(&t->pool_lock);
    if (rc != 0)
        return rc;
    rc = init_robust_mutex(&t->global_lock);                 /* Part 21: only the GLOBAL variant uses it, but it always exists */
    if (rc != 0)
        return rc;
    pthread_rwlockattr_t rwattr;
    rc = pthread_rwlockattr_init(&rwattr);
    if (rc == 0)
        rc = pthread_rwlockattr_setpshared(&rwattr, PTHREAD_PROCESS_SHARED);
    if (rc == 0)
        rc = pthread_rwlock_init(&t->rw_lock, &rwattr);
    CHECK_PT(pthread_rwlockattr_destroy(&rwattr));
    if (rc != 0)
        return rc;
    for (int i = 0; i < nbuckets; i++) {
        buckets(t)[i].first = -1;
        rc = init_robust_mutex(&buckets(t)[i].lock);
        if (rc != 0)
            return rc;
    }

    /* All entries start on the free list: 0 -> 1 -> ... -> K-1. */
    for (int i = 0; i < K; i++) {
        entries(t)[i].next = (i + 1 < K) ? i + 1 : -1;
        entries(t)[i].used = false;
    }
    t->free_head = 0;

    __atomic_store_n(&t->magic, MAC_TABLE_MAGIC, __ATOMIC_RELEASE);   /* LAST: a table that was being built when the process died is never "recovered" */
    mt = t;
    return 0;
}

void mac_destroy(void)
{
    if (mt == NULL)
        return;
    for (int i = 0; i < mt->nbuckets; i++)
        CHECK_PT(pthread_mutex_destroy(&buckets(mt)[i].lock));
    CHECK_PT(pthread_mutex_destroy(&mt->pool_lock));
    CHECK_PT(pthread_mutex_destroy(&mt->global_lock));
    CHECK_PT(pthread_rwlock_destroy(&mt->rw_lock));
    mt->magic = 0;
    mt = NULL;
}

/* ------------------------------------------------------------------ */
/* Part 20: the table in shared memory, and its recovery               */
/* ------------------------------------------------------------------ */

/*
 * The table `t` was written by a switch that is gone (it may have crashed in the middle of an update). Make it consistent and return the number of entries kept (or -ENOMEM).
 * Nothing else uses it yet (no thread exists). `mt` must already point to t (bucket_of uses it).
 */
static int recover_table(mactable_t *t)
{
    mac_bucket_t *bk = buckets(t);
    mac_entry_t *ents = entries(t);
    uint8_t *seen = calloc((size_t)t->K, 1);
    int kept = 0, dropped = 0;

    if (seen == NULL)
        return -ENOMEM;
    for (int i = 0; i < t->nbuckets; i++) {
        mac_bucket_t *b = &bk[i];
        int32_t prev = -1, j;

        lock_robust(&b->lock);                               /* EOWNERDEAD (the owner crashed holding it) is handled here */
        for (j = b->first; j != -1;) {
            mac_entry_t *e = &ents[0];
            bool bad = j < 0 || j >= t->K;                   /* out of range, or reached twice (a cycle or two chains share it): cut the chain here */

            if (!bad && seen[j]) {
                bad = true;
            }
            if (bad) {
                if (prev < 0)
                    b->first = -1;
                else
                    ents[prev].next = -1;
                dropped++;
                break;
            }
            e = &ents[j];
            const int32_t next = e->next;
            mac_t m;
            memcpy(m, e->mac, MAC_LEN);
            if (e->port < 0 || e->port > MAX_PORTS || (e->port > sw.num_ports) || bucket_of(e->vlan, m) != i) {
                if (prev < 0)                                /* an entry that does not belong here (half written): unlink it, it goes back to the pool */
                    b->first = next;
                else
                    ents[prev].next = next;
                dropped++;
                j = next;
                continue;
            }
            seen[j] = 1;
            e->used = true;
            e->next = next;
            struct timespec now = now_mono();
            if (e->last_seen.tv_sec > now.tv_sec)            /* a time stamp from the future cannot be right: it would never age */
                e->last_seen = now;
            atomic_fetch_add(&sw.ports[e->port].mac_count, 1);
            kept++;
            prev = j;
            j = next;
        }
        bucket_unlock(b);
    }

    lock_robust(&t->pool_lock);                              /* the free list and the count are rebuilt, never trusted */
    t->free_head = -1;
    for (int32_t k = t->K - 1; k >= 0; k--) {
        if (!seen[k]) {
            ents[k].used = false;
            ents[k].next = t->free_head;
            t->free_head = k;
        }
    }
    t->count = kept;
    CHECK_PT(pthread_mutex_unlock(&t->pool_lock));
    free(seen);
    if (dropped > 0)
        log_msg(LOG_WARN, "MAC table recovery: %d damaged entr%s dropped", dropped, dropped == 1 ? "y" : "ies");
    return kept;
}

int mac_attach_shm(const char *name, int K, int nbuckets, void **region, size_t *len, int *recovered)
{
    const size_t size = mac_region_size(K, nbuckets);
    struct stat st;
    int rc = 0;

#if MAC_LOCK != STRIPED
    return ENOTSUP;                                            /* Part 21: the recovery relies on the robust per-bucket mutexes of the default build */
#endif
    if (size == 0 || name == NULL || name[0] != '/')           /* a POSIX shared-memory name starts with '/' */
        return EINVAL;
    int fd = shm_open(name, O_CREAT | O_RDWR, 0600);           /* no O_EXCL: an existing table is reused */
    if (fd < 0)
        return errno;
    if (fstat(fd, &st) < 0) {
        rc = errno;
        (void)close(fd);                                         /* error path: the fstat error is the one to report */
        return rc;
    }
    const bool fits = (size_t)st.st_size == size;
    if (!fits && (ftruncate(fd, 0) < 0 || ftruncate(fd, (off_t)size) < 0)) {   /* the wrong size: start from zeroed pages */
        rc = errno;
        (void)close(fd);
        return rc;
    }
    void *m = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    rc = m == MAP_FAILED ? errno : 0;
    if (close(fd) < 0 && rc == 0)
        rc = errno;
    if (m == MAP_FAILED)
        return rc;
    if (rc != 0) {
        (void)munmap(m, size);
        return rc;
    }

    mactable_t *t = m;
    if (fits && __atomic_load_n(&t->magic, __ATOMIC_ACQUIRE) == MAC_TABLE_MAGIC && t->K == K && t->nbuckets == nbuckets) {
        mt = t;
        int n = recover_table(t);
        if (n < 0) {
            mt = NULL;
            (void)munmap(m, size);
            return -n;
        }
        *recovered = n;
    } else {
        rc = mac_init(m, K, nbuckets);
        if (rc != 0) {
            (void)munmap(m, size);
            return rc;
        }
        *recovered = -1;
    }
    *region = m;
    *len = size;
    return 0;
}

void mac_detach(void)
{
    mt = NULL;
}

/* ------------------------------------------------------------------ */
/* Free list: pool_get / pool_put                                      */
/* Both are called while holding the bucket lock of the entry; they     */
/* take pool_lock themselves (bucket -> pool order).                    */
/* ------------------------------------------------------------------ */

/* An unused entry (now marked used), or -1 if the pool is empty. */
static int32_t pool_get(void)
{
    int32_t idx;

    pool_lock();
    idx = mt->free_head;
    if (idx >= 0) {
        mac_entry_t *e = &entries(mt)[idx];
        mt->free_head = e->next;
        e->next = -1;
        e->used = true;
        mt->count++;
    }
    pool_unlock();
    return idx;
}

static void pool_put(int32_t idx)
{
    mac_entry_t *e = &entries(mt)[idx];

    pool_lock();
    e->used = false;
    e->next = mt->free_head;
    mt->free_head = idx;
    mt->count--;
    pool_unlock();
}

/*
 * Remove entry idx from its chain and give it back to the pool. The caller
 * holds the bucket lock; prev is the entry before idx in the chain (-1 if
 * idx is first). Also takes one off the port's mac_count.
 */
static void remove_entry(mac_bucket_t *b, int32_t prev, int32_t idx)
{
    mac_entry_t *ents = entries(mt);
    mac_entry_t *e = &ents[idx];

    if (prev < 0)
        b->first = e->next;
    else
        ents[prev].next = e->next;
    atomic_fetch_sub(&sw.ports[e->port].mac_count, 1);
    pool_put(idx);
}

/* Index of (vlan, mac) in the chain of b (and the entry before it), or -1. Bucket lock held. */
static int32_t chain_find(mac_bucket_t *b, uint16_t vlan, const mac_t mac, int32_t *prev_out)
{
    mac_entry_t *ents = entries(mt);
    int32_t prev = -1;

    for (int32_t i = b->first; i >= 0; prev = i, i = ents[i].next) {
        if (ents[i].vlan == vlan && memcmp(ents[i].mac, mac, MAC_LEN) == 0) {
            if (prev_out != NULL)
                *prev_out = prev;
            return i;
        }
    }
    return -1;
}

/* ------------------------------------------------------------------ */
/* Learn and lookup                                                    */
/* ------------------------------------------------------------------ */

enum learn_rc mac_learn_ex(uint16_t vlan, const mac_t mac, int port, struct mac_move *mv)
{
    if (mv != NULL)
        mv->moved = false;
    if (mt == NULL || port < 0 || port > MAX_PORTS)
        return LEARN_SKIPPED;

    for (int attempt = 0; attempt <= LEARN_RETRIES; attempt++) {
        mac_bucket_t *b = &buckets(mt)[bucket_of(vlan, mac)];
        mac_entry_t *ents = entries(mt);

        bucket_lock(b);
        int32_t idx = chain_find(b, vlan, mac, NULL);       /* search again on every attempt */

        if (idx >= 0) {
            mac_entry_t *e = &ents[idx];
            int old = e->port;

            e->last_seen = now_mono();
            if (old != port) {                              /* the host moved */
                e->port = (int16_t)port;
                atomic_fetch_sub(&sw.ports[old].mac_count, 1);
                atomic_fetch_add(&sw.ports[port].mac_count, 1);
                if (mv != NULL) {
                    mv->moved = true;
                    mv->vlan = vlan;
                    memcpy(mv->mac, mac, MAC_LEN);
                    mv->from = old;
                    mv->to = port;
                }
            }
            bucket_unlock(b);
            return LEARN_OK;
        }

        idx = pool_get();
        if (idx >= 0) {
            mac_entry_t *e = &ents[idx];

            memcpy(e->mac, mac, MAC_LEN);
            e->vlan = vlan;
            e->port = (int16_t)port;
            e->last_seen = now_mono();
            e->next = b->first;                             /* insert at the head of the chain */
            b->first = idx;
            /* Counted while the bucket is still locked: a removal of this entry
             * (also done under this lock) can then never run before the increment. */
            int n = atomic_fetch_add(&sw.ports[port].mac_count, 1) + 1;
            bucket_unlock(b);

            if (n > atomic_load(&sw.max_macs_per_port) && !sw.ports[port].trunk)
                return LEARN_VIOLATION;
            return LEARN_OK;
        }

        /* The pool is empty. Free the bucket BEFORE evicting: eviction takes
         * other bucket locks, and only one may be held at a time. */
        bucket_unlock(b);
        if (attempt == LEARN_RETRIES)
            break;
        mac_evict_lru();
    }
    return LEARN_SKIPPED;                                   /* give up: the frame is still forwarded */
}

void mac_log_move(const struct mac_move *mv)
{
    char text[MAC_STR_LEN];

    mac_format(mv->mac, text, sizeof text);
    log_msg(LOG_INFO, "MAC moved: %s vlan %u port %d -> %d", text, mv->vlan, mv->from, mv->to);
}

enum learn_rc mac_learn(uint16_t vlan, const mac_t mac, int port)
{
    struct mac_move mv;
    enum learn_rc rc = mac_learn_ex(vlan, mac, port, &mv);

    if (mv.moved)
        mac_log_move(&mv);
    return rc;
}

int mac_lookup(uint16_t vlan, const mac_t mac)
{
    if (mt == NULL)
        return -1;

    mac_bucket_t *b = &buckets(mt)[bucket_of(vlan, mac)];
    int port = -1;

    bucket_rdlock(b);                                       /* RWLOCK: the read side (nothing is written here) */
    int32_t idx = chain_find(b, vlan, mac, NULL);
    if (idx >= 0)
        port = entries(mt)[idx].port;                       /* last_seen is NOT touched */
    bucket_unlock(b);
    return port;
}

/* ------------------------------------------------------------------ */
/* LRU eviction (no global list: scan the buckets, one lock at a time) */
/* ------------------------------------------------------------------ */

static bool older(const struct timespec *a, const struct timespec *b)
{
    return a->tv_sec < b->tv_sec || (a->tv_sec == b->tv_sec && a->tv_nsec < b->tv_nsec);
}

static bool same_time(const struct timespec *a, const struct timespec *b)
{
    return a->tv_sec == b->tv_sec && a->tv_nsec == b->tv_nsec;
}

bool mac_evict_lru(void)
{
    if (mt == NULL)
        return false;

    mac_entry_t *ents = entries(mt);

    for (int pass = 0; pass < 2; pass++) {              /* a changed candidate: scan once more */
        bool found = false;
        int victim_bucket = -1;
        int32_t victim = -1;
        uint16_t v_vlan = 0;
        mac_t v_mac;
        struct timespec v_time = {0, 0};

        /* 1. Find the oldest entry: lock a bucket, look at its chain, unlock. */
        for (int i = 0; i < mt->nbuckets; i++) {
            mac_bucket_t *b = &buckets(mt)[i];

            bucket_lock(b);
            for (int32_t j = b->first; j >= 0; j = ents[j].next) {
                if (!found || older(&ents[j].last_seen, &v_time)) {
                    found = true;
                    victim_bucket = i;
                    victim = j;
                    v_vlan = ents[j].vlan;
                    memcpy(v_mac, ents[j].mac, MAC_LEN);
                    v_time = ents[j].last_seen;
                }
            }
            bucket_unlock(b);
        }
        if (!found)
            return false;                               /* the table is empty */

        /* 2. Lock the victim's bucket again and check it is still the same entry. */
        mac_bucket_t *b = &buckets(mt)[victim_bucket];
        int32_t prev = -1;

        bucket_lock(b);
        int32_t idx = chain_find(b, v_vlan, v_mac, &prev);
        if (idx == victim && same_time(&ents[idx].last_seen, &v_time)) {
            remove_entry(b, prev, idx);
            bucket_unlock(b);
            return true;
        }
        bucket_unlock(b);                               /* changed meanwhile: try once more */
    }
    return false;
}

/* ------------------------------------------------------------------ */
/* Aging, flush, clear                                                 */
/* ------------------------------------------------------------------ */

/* Walk all buckets (one lock at a time) and remove the entries `pred` selects. */
typedef bool (*select_fn)(const mac_entry_t *e, const void *arg);

static int remove_where(select_fn pred, const void *arg)
{
    mac_entry_t *ents = entries(mt);
    int removed = 0;

    for (int i = 0; i < mt->nbuckets; i++) {
        mac_bucket_t *b = &buckets(mt)[i];
        int32_t prev = -1;

        bucket_lock(b);
        for (int32_t j = b->first; j >= 0;) {
            int32_t next = ents[j].next;                /* remove_entry reuses ents[j].next */

            if (pred(&ents[j], arg)) {
                remove_entry(b, prev, j);
                removed++;
            } else {
                prev = j;
            }
            j = next;
        }
        bucket_unlock(b);                               /* released before bucket i + 1 */
    }
    return removed;
}

struct age_arg {
    struct timespec now;
    long max_ms;
};

static bool too_old(const mac_entry_t *e, const void *arg)
{
    const struct age_arg *a = arg;

    return ts_diff_ms(a->now, e->last_seen) > a->max_ms;
}

int mac_age(long max_age_sec)
{
    if (mt == NULL)
        return 0;

    struct age_arg a = {.now = now_mono(), .max_ms = max_age_sec * 1000};

    return remove_where(too_old, &a);
}

static bool on_port(const mac_entry_t *e, const void *arg)
{
    return e->port == *(const int *)arg;
}

static bool any_entry(const mac_entry_t *e, const void *arg)
{
    (void)e;
    (void)arg;
    return true;
}

int mac_flush_port(int port)
{
    if (mt == NULL)
        return 0;
    return remove_where(on_port, &port);
}

int mac_clear(void)
{
    if (mt == NULL)
        return 0;
    return remove_where(any_entry, NULL);
}

/* ------------------------------------------------------------------ */
/* Iteration and dump                                                  */
/* ------------------------------------------------------------------ */

int mac_foreach(void (*cb)(const mac_info_t *info, void *arg), void *arg)
{
    if (mt == NULL)
        return 0;

    mac_entry_t *ents = entries(mt);
    int visited = 0;

    for (int i = 0; i < mt->nbuckets; i++) {
        mac_bucket_t *b = &buckets(mt)[i];
        mac_info_t *copy;
        struct timespec now;
        int n = 0, len = 0;

        /* Copy the chain under the lock, call the callback after unlocking. */
        bucket_lock(b);
        /* "Now" is read AFTER the bucket is locked, once per bucket. A learner refreshes last_seen under this
         * same lock, so every entry in the bucket was last seen before this moment and its age cannot be
         * negative. (One timestamp taken before the whole walk gave negative ages: an entry refreshed
         * while later buckets were still being copied has last_seen later than that timestamp.) */
        now = now_mono();
        for (int32_t j = b->first; j >= 0; j = ents[j].next)
            len++;
        if (len == 0) {
            bucket_unlock(b);
            continue;
        }
        copy = malloc((size_t)len * sizeof *copy);
        if (copy == NULL) {
            bucket_unlock(b);
            return -1;
        }
        for (int32_t j = b->first; j >= 0 && n < len; j = ents[j].next, n++) {
            memcpy(copy[n].mac, ents[j].mac, MAC_LEN);
            copy[n].vlan = ents[j].vlan;
            copy[n].port = ents[j].port;
            copy[n].age_ms = ts_diff_ms(now, ents[j].last_seen);
        }
        bucket_unlock(b);

        for (int k = 0; k < n; k++)
            cb(&copy[k], arg);
        visited += n;
        free(copy);
    }
    return visited;
}

static void dump_line(const mac_info_t *m, void *arg)
{
    char text[MAC_STR_LEN];

    mac_format(m->mac, text, sizeof text);
    fprintf((FILE *)arg, "vlan %-4u %s  port %-3d age %ld.%03ld s\n", m->vlan, text, m->port,
            m->age_ms / 1000, m->age_ms % 1000);
}

int mac_dump(FILE *fp)
{
    fprintf(fp, "# MAC table: vlan, mac, port, seconds since last seen\n");
    return mac_foreach(dump_line, fp);
}

/* One line of the Part 10 dump: "vlan mac port age_s". The callback runs with no MAC lock held. */
static void dump_entry(const mac_info_t *m, void *arg)
{
    char text[MAC_STR_LEN];

    mac_format(m->mac, text, sizeof text);
    fprintf((FILE *)arg, "%u %s %d %ld\n", m->vlan, text, m->port, m->age_ms / 1000);
}

int mac_dump_file(const char *path)
{
    FILE *fp = fopen(path, "w");
    struct timespec ts;
    struct tm tm;
    char when[32] = "unknown-time";
    int n, err = 0;

    if (fp == NULL)
        return -1;                                   /* errno is set by fopen */
    if (clock_gettime(CLOCK_REALTIME, &ts) == 0 && localtime_r(&ts.tv_sec, &tm) != NULL)
        strftime(when, sizeof when, "%Y-%m-%d %H:%M:%S", &tm);
    fprintf(fp, "# MAC table dump, %s\n# vlan mac port age_s\n", when);

    n = mac_foreach(dump_entry, fp);                 /* one bucket at a time */
    if (n < 0)
        err = ENOMEM;
    if (fflush(fp) != 0 && err == 0)
        err = errno != 0 ? errno : EIO;
    if (ferror(fp) && err == 0)
        err = EIO;
    if (fclose(fp) != 0 && err == 0)
        err = errno != 0 ? errno : EIO;
    if (err != 0) {
        errno = err;
        return -1;
    }
    return n;
}

int mac_size(void)
{
    int n = 0;

    if (mt == NULL)
        return 0;
    pool_lock();
    n = mt->count;
    pool_unlock();
    return n;
}

/* ------------------------------------------------------------------ */
/* Consistency check (tests)                                           */
/* ------------------------------------------------------------------ */

static int fail(char *why, size_t len, const char *fmt, ...)
    __attribute__((format(printf, 3, 4)));

static int fail(char *why, size_t len, const char *fmt, ...)
{
    va_list ap;

    if (why != NULL && len > 0) {
        va_start(ap, fmt);
        vsnprintf(why, len, fmt, ap);
        va_end(ap);
    }
    return -1;
}

int mac_check_counts(char *why, size_t why_len)
{
    if (mt == NULL)
        return fail(why, why_len, "no table");
    if (mt->magic != MAC_TABLE_MAGIC)
        return fail(why, why_len, "bad magic");

    mac_entry_t *ents = entries(mt);
    int per_port[MAX_PORTS + 1];
    int in_chains = 0;

    memset(per_port, 0, sizeof per_port);

    for (int i = 0; i < mt->nbuckets; i++) {
        mac_bucket_t *b = &buckets(mt)[i];

        bucket_lock(b);
        for (int32_t j = b->first; j >= 0; j = ents[j].next) {
            if (in_chains >= mt->K) {
                bucket_unlock(b);
                return fail(why, why_len, "more entries in the chains than K (cycle?)");
            }
            if (j >= mt->K) {
                bucket_unlock(b);
                return fail(why, why_len, "bucket %d: entry index %d out of range", i, j);
            }
            mac_entry_t *e = &ents[j];
            mac_t m;
            memcpy(m, e->mac, MAC_LEN);
            if (!e->used) {
                bucket_unlock(b);
                return fail(why, why_len, "bucket %d: chained entry %d is not marked used", i, j);
            }
            if (bucket_of(e->vlan, m) != i) {
                bucket_unlock(b);
                return fail(why, why_len, "entry %d is in bucket %d but hashes elsewhere", j, i);
            }
            if (e->port < 0 || e->port > MAX_PORTS) {
                bucket_unlock(b);
                return fail(why, why_len, "entry %d has bad port %d", j, e->port);
            }
            per_port[e->port]++;
            in_chains++;
        }
        bucket_unlock(b);
    }

    for (int p = 0; p <= MAX_PORTS; p++) {
        int mc = atomic_load(&sw.ports[p].mac_count);

        if (mc != per_port[p])
            return fail(why, why_len, "port %d: mac_count is %d but the table has %d entries",
                        p, mc, per_port[p]);
    }

    int free_entries = 0, count;
    pool_lock();
    count = mt->count;
    for (int32_t j = mt->free_head; j >= 0; j = ents[j].next) {
        if (j >= mt->K || free_entries >= mt->K || ents[j].used) {
            pool_unlock();
            return fail(why, why_len, "bad free list at entry %d (cycle, range or used flag)", j);
        }
        free_entries++;
    }
    pool_unlock();

    if (count != in_chains)
        return fail(why, why_len, "count is %d but %d entries are in the chains", count, in_chains);
    if (count + free_entries != mt->K)
        return fail(why, why_len, "count %d + free %d != K %d", count, free_entries, mt->K);
    return 0;
}

/* ------------------------------------------------------------------ */
/* Aging thread                                                        */
/* ------------------------------------------------------------------ */

#define AGING_PERIOD_MS 1000

void *aging_main(void *arg)
{
    struct timespec deadline = deadline_mono(AGING_PERIOD_MS);
    int rc;

    (void)arg;
    rc = pthread_setname_np(pthread_self(), "aging");
    if (rc != 0)
        log_msg(LOG_WARN, "cannot set aging thread name: %s", strerror(rc));

    for (;;) {
        /* A timed wait on shutdown_cond until the absolute deadline: it ends early
         * (and the loop exits) as soon as shutdown starts. No sleep(), no polling. */
        CHECK_PT(pthread_mutex_lock(&sw.shutdown_lock));
        rc = 0;
        while (rc == 0 && !sw.shutdown_requested)                  /* the predicate, under the lock */
            rc = pthread_cond_timedwait(&sw.shutdown_cond, &sw.shutdown_lock, &deadline);
        bool stop = sw.shutdown_requested;
        CHECK_PT(pthread_mutex_unlock(&sw.shutdown_lock));

        if (rc != 0 && rc != ETIMEDOUT)
            log_msg(LOG_ERR, "aging: pthread_cond_timedwait failed: %s", strerror(rc));
        if (stop)
            break;

        mac_age(atomic_load(&sw.mac_aging_sec));

        /* The next deadline is one period after the previous one (no drift); if we
         * fell far behind (machine suspended), start again from now. */
        deadline = ts_add_ms(deadline, AGING_PERIOD_MS);
        if (ts_diff_ms(deadline, now_mono()) < 0)
            deadline = deadline_mono(AGING_PERIOD_MS);
    }
    return NULL;
}
