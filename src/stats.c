/*
 * stats.c - per-port counters in POSIX shared memory (Part 14). See stats.h for the layout.
 *
 * Lock order: the stats mutex is always taken last. Never call log_msg while holding it (this file never does).
 *
 * The mutex lives in the segment and is PROCESS_SHARED + ROBUST: a reader process that dies while holding it does
 * not wedge the switch (the next locker gets EOWNERDEAD, calls pthread_mutex_consistent and goes on).
 */
#include "stats.h"

#include <errno.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

/* The mapping of this process. `base` is published last (release) and cleared first by stats_destroy; the page size
 * and the length are written before it. Init and destroy run only while no other thread uses the counters. */
static _Atomic(void *) base;
static size_t map_len;
static size_t page_sz;
static char shm_name[256];
static dev_t shm_dev;
static ino_t shm_ino;
static bool shm_created;               /* the name may be ours to unlink */
static atomic_flag atexit_done = ATOMIC_FLAG_INIT;
static atomic_flag lock_warned = ATOMIC_FLAG_INIT;

size_t stats_page_size(void)
{
    long p = sysconf(_SC_PAGESIZE);
    return p > 0 ? (size_t)p : 0;
}

size_t stats_shm_size(size_t page)
{
    if (page < sizeof(stats_ctl_t) || (page & (page - 1)) != 0)
        return 0;
    size_t data = sizeof(stats_data_t);
    size_t rounded = (data + page - 1) / page * page;     /* round_up; no overflow: sizeof is a few KB */
    return page + rounded;
}

int stats_validate(const stats_ctl_t *ctl)
{
    if (__atomic_load_n(&ctl->magic, __ATOMIC_ACQUIRE) != STATS_MAGIC)
        return EINVAL;
    if (ctl->version != STATS_VERSION)
        return EPROTO;
    return 0;
}

int stats_lock(pthread_mutex_t *m)
{
    int rc = pthread_mutex_lock(m);
    if (rc == EOWNERDEAD) {
        /* The owner died holding the lock. We own it now. The protected data are 64-bit and int fields that are
         * updated one at a time, so there is nothing to repair: just mark the mutex usable again. */
        rc = pthread_mutex_consistent(m);
        if (rc != 0) {
            (void)pthread_mutex_unlock(m);        /* the mutex becomes unrecoverable for everybody */
            return rc;
        }
    }
    return rc;
}

void stats_unlock(pthread_mutex_t *m)
{
    (void)pthread_mutex_unlock(m);
}

/* ------------------------------------------------------------------ */
/* Creation and removal of the segment                                 */
/* ------------------------------------------------------------------ */

/* Unlink the name only if it still refers to the segment THIS process created: a newer switch that replaced our
 * (stale-looking) segment must keep its own. */
static void unlink_if_ours(void)
{
    if (!shm_created)
        return;
    shm_created = false;
    int fd = shm_open(shm_name, O_RDONLY, 0);
    if (fd < 0)
        return;                                   /* already gone */
    struct stat st;
    bool ours = fstat(fd, &st) == 0 && st.st_dev == shm_dev && st.st_ino == shm_ino;
    (void)close(fd);                              /* read-only descriptor: a failed close loses nothing */
    if (ours)
        (void)shm_unlink(shm_name);
}

/* exit() on a start-up failure (CHECK, bind...) must not leave the segment behind. Only the NAME is removed: other
 * threads may still be running, so the mapping and the mutex stay. */
static void atexit_unlink(void)
{
    unlink_if_ours();
}

int stats_init_named(const char *name, int max_port)
{
    if (max_port < 0 || max_port > MAX_PORTS || name == NULL || name[0] != '/' || strlen(name) >= sizeof shm_name)
        return EINVAL;
    if (atomic_load(&base) != NULL)
        stats_destroy();                          /* re-initialisation: start again */

    size_t page = stats_page_size();
    size_t len = stats_shm_size(page);
    if (len == 0)
        return EINVAL;

    /* Create the object. If one exists it is stale (a crash left it, or an older switch): unlink and retry. O_EXCL
     * guarantees that we never fill in somebody else's object. */
    int fd = -1;
    for (int attempt = 0; attempt < 5 && fd < 0; attempt++) {
        fd = shm_open(name, O_CREAT | O_EXCL | O_RDWR, 0600);
        if (fd < 0) {
            if (errno != EEXIST)
                return errno;
            if (shm_unlink(name) < 0 && errno != ENOENT)
                return errno;
        }
    }
    if (fd < 0)
        return EEXIST;                            /* somebody keeps re-creating it */

    int rc = 0;
    struct stat st;
    void *m = MAP_FAILED;

    /* The mode of a new object is 0600 & ~umask; make it exactly 0600 (same user only) whatever the umask. */
    if (fchmod(fd, 0600) < 0 || fstat(fd, &st) < 0 || ftruncate(fd, (off_t)len) < 0) {
        rc = errno;
        goto fail;
    }
    m = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (m == MAP_FAILED) {
        rc = errno;
        goto fail;
    }
    if (close(fd) < 0) {                          /* the mapping stays valid */
        rc = errno;
        fd = -1;
        goto fail;
    }
    fd = -1;

    /* A new object is zero-filled by ftruncate. Initialise the control area, then the data area, then publish. */
    stats_ctl_t *ctl = m;
    stats_data_t *data = (stats_data_t *)((char *)m + page);
    pthread_mutexattr_t attr;

    rc = pthread_mutexattr_init(&attr);
    if (rc != 0)
        goto fail;
    rc = pthread_mutexattr_setpshared(&attr, PTHREAD_PROCESS_SHARED);
    if (rc == 0)
        rc = pthread_mutexattr_setrobust(&attr, PTHREAD_MUTEX_ROBUST);
    if (rc == 0)
        rc = pthread_mutex_init(&ctl->lock, &attr);
    (void)pthread_mutexattr_destroy(&attr);
    if (rc != 0)
        goto fail;

    ctl->version = STATS_VERSION;
    ctl->num_ports = max_port;
    memset(data, 0, sizeof *data);
    __atomic_store_n(&ctl->magic, STATS_MAGIC, __ATOMIC_RELEASE);   /* readers trust the segment from here */

    snprintf(shm_name, sizeof shm_name, "%s", name);
    shm_dev = st.st_dev;
    shm_ino = st.st_ino;
    shm_created = true;
    map_len = len;
    page_sz = page;
    atomic_store(&base, m);

    if (!atomic_flag_test_and_set(&atexit_done))
        atexit(atexit_unlink);
    return 0;

fail:
    /* Nothing stays behind: no mapping, no descriptor, no object with this name (it is ours, we just created it). */
    if (m != MAP_FAILED)
        (void)munmap(m, len);                     /* error path: the error that brought us here is the one to report */
    if (fd >= 0)
        (void)close(fd);
    (void)shm_unlink(name);
    return rc != 0 ? rc : EIO;
}

int stats_init(int max_port)
{
    return stats_init_named(STATS_SHM_NAME, max_port);
}

void stats_destroy(void)
{
    stats_ctl_t *ctl = atomic_exchange(&base, NULL);
    if (ctl == NULL)
        return;

    /* 1. Unlink first: nobody can open the object from now on. Processes that already mapped it keep the memory. */
    unlink_if_ours();
    /* 2. Readers with an old mapping must not trust it any more; then destroy the mutex; 3. unmap. */
    __atomic_store_n(&ctl->magic, 0, __ATOMIC_RELEASE);
    (void)pthread_mutex_destroy(&ctl->lock);      /* EBUSY if a reader still holds it: nothing more to do */
    if (munmap(ctl, map_len) < 0)
        fprintf(stderr, "stats: munmap of the shared counters failed: %s\n", strerror(errno));   /* log_msg is not used by this file */
    map_len = 0;
}

/* ------------------------------------------------------------------ */
/* Counters and port fields                                            */
/* ------------------------------------------------------------------ */

static stats_port_t *port_slot(stats_ctl_t *ctl, int port)
{
    if (port < 0 || port > ctl->num_ports)
        return NULL;
    return &((stats_data_t *)((char *)ctl + page_sz))->port[port];
}

static uint64_t *counter(stats_port_t *s, enum stat_kind kind)
{
    switch (kind) {
    case STAT_IN:     return &s->in;
    case STAT_OUT:    return &s->out;
    case STAT_DROP:   return &s->dropped;
    case STAT_MIRROR: return &s->mirrored;
    default:          return NULL;
    }
}

/* Lock the shared mutex, or give up quietly: a failure here means the segment was damaged by another process; the
 * switch must keep forwarding, so the update is lost (reported once on stderr) instead of exiting. */
static bool lock_or_warn(stats_ctl_t *ctl)
{
    int rc = stats_lock(&ctl->lock);
    if (rc != 0) {
        if (!atomic_flag_test_and_set(&lock_warned))
            fprintf(stderr, "stats: cannot lock the shared counters: %s (updates are lost)\n", strerror(rc));
        return false;
    }
    return true;
}

unsigned long long stats_inc(int port, enum stat_kind kind)
{
    stats_ctl_t *ctl = atomic_load(&base);
    unsigned long long v = 0;

    if (ctl == NULL || (int)kind < 0 || kind >= STAT_KINDS || !lock_or_warn(ctl))
        return 0;
    stats_port_t *s = port_slot(ctl, port);
    if (s != NULL)
        v = ++*counter(s, kind);
    stats_unlock(&ctl->lock);
    return v;
}

unsigned long long stats_get(int port, enum stat_kind kind)
{
    stats_ctl_t *ctl = atomic_load(&base);
    unsigned long long v = 0;

    if (ctl == NULL || (int)kind < 0 || kind >= STAT_KINDS || !lock_or_warn(ctl))
        return 0;
    stats_port_t *s = port_slot(ctl, port);
    if (s != NULL)
        v = *counter(s, kind);
    stats_unlock(&ctl->lock);
    return v;
}

/* which: 0 state, 1 vlan, 2 mirror_to */
static int set_field(int port, int which, int value)
{
    stats_ctl_t *ctl = atomic_load(&base);
    int rc = EINVAL;

    if (ctl == NULL || !lock_or_warn(ctl))
        return EINVAL;
    stats_port_t *s = port_slot(ctl, port);
    if (s != NULL) {
        *(which == 0 ? &s->state : which == 1 ? &s->vlan : &s->mirror_to) = value;
        rc = 0;
    }
    stats_unlock(&ctl->lock);
    return rc;
}

int stats_set_state(int port, int state)       { return set_field(port, 0, state); }
int stats_set_vlan(int port, int vlan)         { return set_field(port, 1, vlan); }
int stats_set_mirror(int port, int mirror_to)  { return set_field(port, 2, mirror_to); }

int stats_get_port(int port, int *state, int *vlan, int *mirror_to)
{
    stats_ctl_t *ctl = atomic_load(&base);
    int rc = EINVAL;

    if (ctl == NULL || !lock_or_warn(ctl))
        return EINVAL;
    stats_port_t *s = port_slot(ctl, port);
    if (s != NULL) {
        *state = s->state;
        *vlan = s->vlan;
        *mirror_to = s->mirror_to;
        rc = 0;
    }
    stats_unlock(&ctl->lock);
    return rc;
}
