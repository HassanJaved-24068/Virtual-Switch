/*
 * forward.c - forwarding decision. Part 19: port 0 (the pipe trunk to a peer switch) takes part like any trunk port: the loops start at 0.
 * Part 4: flooding. Part 6: MAC learning and unicast forwarding.
 * Part 11: the priority of a frame chooses the ring of every output queue (queue.c); a priority above 2
 * is queued as low priority and counted per ingress port (bad_prio), with one log line per port.
 * Part 13: VLANs. The VLAN of a frame is fixed at ingress: an access port stamps its own VLAN into the frame
 * (f->vlan and wire bytes 14-15) before anything else happens; the trunk port keeps the frame's VLAN and drops
 * a frame whose VLAN is outside 1..4094 (counted). A frame is then sent only to UP ports that are the trunk or
 * have that VLAN, for unicast and flooding alike, and the MAC lookup is keyed by (VLAN, MAC). The frame is
 * never copied: it is shared by every output queue, so the zero-copy broadcast is kept.
 * Part 17: port mirroring (mirror_copy): copies of the traffic of a mirrored port are queued to its sniffer port.
 */
#include "forward.h"

#include "log.h"
#include "mactable.h"
#include "stats.h"

#include <string.h>

/* Log a "queue full" message for the 1st drop and then every 1000th. */
#define DROP_LOG_EVERY 1000

/*
 * Part 17: send one mirrored copy of f for port x (roadmap: frame_ref + outq_try_push). The caller holds the ports_lock read lock, so the states cannot change
 * under us. The sniffer s = x.mirror_to gets the SAME frame (one more reference, no copy) if it is UP and is not the ingress port of this frame; the VLAN is
 * ignored on purpose. Success counts "mirrored" on x; a full queue of the sniffer unrefs and counts "dropped" on s: the copy goes into the sniffer's own bounded
 * queue, so a slow sniffer can only lose copies, it never slows x. There is no de-duplication: a sniffer that is itself in the VLAN of the mirrored traffic
 * receives a flooded frame twice (once normally, once mirrored); give the sniffer its own VLAN to see every frame once.
 */
static void mirror_copy(const port_t *x, int ingress, frame_t *f, unsigned long long *drops)
{
    const int s = atomic_load(&x->mirror_to);

    if (s <= 0 || s > sw.num_ports || s == ingress || sw.ports[s].state != PORT_UP)
        return;
    frame_ref(f);
    if (outq_try_push(&sw.ports[s].q, f)) {
        stats_inc(x->id, STAT_MIRROR);
    } else {
        frame_unref(f);
        unsigned long long n = stats_inc(s, STAT_DROP);
        if (n == 1 || n % DROP_LOG_EVERY == 0)
            drops[s] = n;
    }
}

enum learn_rc forward_frame(port_t *in_port, frame_t *f)
{
    unsigned long long drops[MAX_PORTS + 1];     /* counts worth logging, 0 = nothing */
    bool sent[MAX_PORTS + 1];                    /* Part 17: the ports whose queue took the frame (their mirroring, direction (b)) */
    struct mac_move move;
    enum learn_rc lr = LEARN_OK;
    bool first_bad_prio = false;

    memset(drops, 0, sizeof drops);
    memset(sent, 0, sizeof sent);
    move.moved = false;

    /* The read lock keeps ports from being torn down while we use their queues. */
    CHECK_PT(pthread_rwlock_rdlock(&sw.ports_lock));

    if (mac_is_broadcast(f->src)) {              /* nobody may send from the broadcast address */
        CHECK_PT(pthread_rwlock_unlock(&sw.ports_lock));
        return LEARN_OK;
    }

    /* The VLAN of the frame is decided here, once. The frame is still private to this thread (reference count 1),
     * so it may be changed; afterwards it is only read. */
    if (in_port->trunk) {
        if (f->vlan < 1 || f->vlan > 4094) {     /* a trunk frame must say which VLAN it belongs to */
            unsigned long long n = stats_inc(in_port->id, STAT_DROP);

            CHECK_PT(pthread_rwlock_unlock(&sw.ports_lock));
            if (n == 1 || n % DROP_LOG_EVERY == 0)
                log_msg(LOG_WARN, "port %d: trunk frame with invalid VLAN %u dropped (%llu dropped so far)",
                        in_port->id, (unsigned)f->vlan, n);
            return LEARN_OK;
        }
    } else {
        f->vlan = in_port->vlan;                 /* access port: the port decides, whatever the host wrote */
        f->wire[FRAME_OFF_VLAN] = (uint8_t)(f->vlan >> 8);
        f->wire[FRAME_OFF_VLAN + 1] = (uint8_t)(f->vlan & 0xff);
    }

    /* Learn the source (a multicast source is never learned). */
    if (!mac_is_multicast(f->src))
        lr = mac_learn_ex(f->vlan, f->src, in_port->id, &move);
    if (lr == LEARN_VIOLATION) {                 /* too many MACs on this port: drop the frame */
        CHECK_PT(pthread_rwlock_unlock(&sw.ports_lock));
        return LEARN_VIOLATION;
    }

    /* A priority above 2 is not valid: queue.c treats it as 2 (low). Count it on the ingress port; the
     * first one is logged (after the lock is released), the rest only counted. The frame itself is not changed. */
    if (f->prio > OUTQ_LOW)
        first_bad_prio = atomic_fetch_add(&in_port->bad_prio, 1) == 0;

    /* Decide where it goes: one port (target) or every other UP port (target = 0). */
    int target = 0;
    bool filtered = false;

    if (!mac_is_multicast(f->dst)) {             /* multicast includes broadcast: those flood */
        int p = mac_lookup(f->vlan, f->dst);

        if (p == in_port->id)
            filtered = true;                     /* the destination is behind the ingress port */
        else if (p >= 0 && p <= sw.num_ports && sw.ports[p].state == PORT_UP && port_vlan_ok(&sw.ports[p], f->vlan))
            target = p;                          /* known, UP and in this VLAN (or the trunk): only that port */
        /* unknown, or its port is not UP / not in this VLAN: flood (inside the VLAN) */
    }

    for (int i = 0; i <= sw.num_ports && !filtered; i++) {      /* Part 19: i = 0 is the pipe trunk to the peer switch (never UP in a single switch) */
        port_t *dst = &sw.ports[i];

        if (dst == in_port || dst->state != PORT_UP || !port_vlan_ok(dst, f->vlan))
            continue;
        if (target != 0 && i != target)
            continue;

        frame_ref(f);                            /* one reference for the queue */
        if (!outq_try_push(&dst->q, f)) {
            frame_unref(f);                      /* queue full: take it back */
            unsigned long long n = stats_inc(i, STAT_DROP);
            if (n == 1 || n % DROP_LOG_EVERY == 0)
                drops[i] = n;
        } else {
            sent[i] = true;
        }
    }

    /* Part 17, mirroring: "all traffic of port X" = (a) every frame received on X and (b) every frame successfully queued to X. */
    mirror_copy(in_port, in_port->id, f, drops);                             /* (a) */
    for (int i = 0; i <= sw.num_ports; i++)
        if (sent[i])
            mirror_copy(&sw.ports[i], in_port->id, f, drops);                /* (b) */
    CHECK_PT(pthread_rwlock_unlock(&sw.ports_lock));

    /* Logging is done after the lock is released. */
    if (move.moved)
        mac_log_move(&move);
    if (first_bad_prio)
        log_msg(LOG_WARN, "port %d: bad_prio: a frame with priority %u (valid: 0-2) is queued as low priority; "
                "further ones are only counted", in_port->id, (unsigned)f->prio);
    for (int i = 0; i <= sw.num_ports; i++)
        if (drops[i] != 0)
            log_msg(LOG_WARN, "port %d: output queue full, %llu frames dropped so far", i, drops[i]);
    return lr;
}
