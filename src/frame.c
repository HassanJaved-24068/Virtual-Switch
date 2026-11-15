/*
 * frame.c - wire protocol: full reads/writes and frame pack/unpack.
 */
#include "frame.h"

#include <arpa/inet.h>
#include <errno.h>
#include <string.h>
#include <unistd.h>

/* ------------------------------------------------------------------ */
/* Full reads and writes                                               */
/* ------------------------------------------------------------------ */

/* A stream socket may return fewer bytes than asked, so we loop. */
ssize_t read_full(int fd, void *buf, size_t n)
{
    uint8_t *p = buf;
    size_t got = 0;

    while (got < n) {
        ssize_t r = read(fd, p + got, n - got);
        if (r < 0) {
            if (errno == EINTR)
                continue;           /* interrupted by a signal: try again */
            return -1;              /* real error, errno already set */
        }
        if (r == 0)
            break;                  /* EOF: the other side closed */
        got += (size_t)r;
    }
    return (ssize_t)got;
}

/* write() may also write only part of the data, so we loop here too. */
int write_full(int fd, const void *buf, size_t n)
{
    const uint8_t *p = buf;
    size_t done = 0;

    while (done < n) {
        ssize_t w = write(fd, p + done, n - done);
        if (w < 0) {
            if (errno == EINTR)
                continue;
            return -1;              /* EPIPE, ECONNRESET, ... */
        }
        if (w == 0) {               /* should not happen for n > 0; avoid looping forever */
            errno = EIO;
            return -1;
        }
        done += (size_t)w;
    }
    return 0;
}

/* ------------------------------------------------------------------ */
/* Pack / unpack                                                       */
/* ------------------------------------------------------------------ */

int frame_pack(const frame_hdr_t *hdr, const uint8_t *payload, uint8_t *out)
{
    if (hdr->len > MAX_PAYLOAD) {
        errno = EMSGSIZE;
        return -1;
    }
    if (hdr->len > 0 && payload == NULL) {
        errno = EINVAL;
        return -1;
    }

    uint16_t len_net = htons(hdr->len);      /* multi-byte fields: network order */
    uint16_t vlan_net = htons(hdr->vlan);

    memcpy(out + FRAME_OFF_LEN, &len_net, 2);
    memcpy(out + FRAME_OFF_SRC, hdr->src, MAC_LEN);
    memcpy(out + FRAME_OFF_DST, hdr->dst, MAC_LEN);
    memcpy(out + FRAME_OFF_VLAN, &vlan_net, 2);
    out[FRAME_OFF_PRIO] = hdr->prio;
    if (hdr->len > 0)
        memcpy(out + HDR_LEN, payload, hdr->len);
    return HDR_LEN + hdr->len;
}

void frame_unpack(const uint8_t *bytes, frame_hdr_t *hdr)
{
    uint16_t len_net, vlan_net;

    memcpy(&len_net, bytes + FRAME_OFF_LEN, 2);
    memcpy(hdr->src, bytes + FRAME_OFF_SRC, MAC_LEN);
    memcpy(hdr->dst, bytes + FRAME_OFF_DST, MAC_LEN);
    memcpy(&vlan_net, bytes + FRAME_OFF_VLAN, 2);
    hdr->prio = bytes[FRAME_OFF_PRIO];
    hdr->len = ntohs(len_net);
    hdr->vlan = ntohs(vlan_net);
}

/* ------------------------------------------------------------------ */
/* Receive / send a whole frame                                        */
/* ------------------------------------------------------------------ */

enum frame_rc frame_recv(int fd, frame_hdr_t *hdr, uint8_t *payload)
{
    uint8_t raw[HDR_LEN];

    /* Step 1: the 17 header bytes. */
    ssize_t r = read_full(fd, raw, HDR_LEN);
    if (r < 0)
        return FR_ERR;
    if (r == 0)
        return FR_EOF;                  /* closed between frames: normal */
    if (r < HDR_LEN)
        return FR_TRUNC_HDR;            /* closed in the middle of a header */

    frame_unpack(raw, hdr);

    /* Step 2: check the length BEFORE using it to read. */
    if (hdr->len > MAX_PAYLOAD)
        return FR_PROTO;

    /* Step 3: exactly len payload bytes (len may be 0: nothing to read). */
    if (hdr->len > 0) {
        r = read_full(fd, payload, hdr->len);
        if (r < 0)
            return FR_ERR;
        if (r < hdr->len)
            return FR_TRUNC_PAYLOAD;
    }
    return FR_OK;
}

int frame_send(int fd, const frame_hdr_t *hdr, const uint8_t *payload)
{
    uint8_t buf[HDR_LEN + MAX_PAYLOAD];
    int total = frame_pack(hdr, payload, buf);

    if (total < 0)
        return -1;
    return write_full(fd, buf, (size_t)total);
}

const char *frame_rc_str(enum frame_rc rc)
{
    switch (rc) {
    case FR_OK:            return "ok";
    case FR_EOF:           return "end of stream";
    case FR_ERR:           return "read error";
    case FR_TRUNC_HDR:     return "truncated header";
    case FR_TRUNC_PAYLOAD: return "truncated payload";
    case FR_PROTO:         return "protocol violation (length > 1500)";
    }
    return "unknown";
}
