/*
 * frame.h - the wire protocol: reading/writing whole byte blocks and
 * packing/unpacking the 17-byte frame header.
 *
 * Wire format (all fixed, must match exactly):
 *   offset 0   length   2 bytes  network byte order (payload length 0..1500)
 *   offset 2   src MAC  6 bytes
 *   offset 8   dst MAC  6 bytes
 *   offset 14  vlan     2 bytes  network byte order (1..4094)
 *   offset 16  prio     1 byte   (0 high, 1 normal, 2 low)
 *   offset 17  payload  `length` bytes
 */
#ifndef FRAME_H
#define FRAME_H

#include <pthread.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

#include "common.h"

/* Byte offsets inside the header. */
#define FRAME_OFF_LEN 0
#define FRAME_OFF_SRC 2
#define FRAME_OFF_DST 8
#define FRAME_OFF_VLAN 14
#define FRAME_OFF_PRIO 16

/*
 * The header as a normal C struct. This is only for use inside our program.
 * It is NEVER sent or read directly: sizeof would be 18 because of padding.
 * frame_pack / frame_unpack copy it field by field to and from bytes.
 */
typedef struct {
    uint16_t len;      /* payload length, host byte order */
    mac_t src;
    mac_t dst;
    uint16_t vlan;     /* host byte order */
    uint8_t prio;
} frame_hdr_t;

/* Result of frame_recv(). */
enum frame_rc {
    FR_OK = 1,              /* one complete frame was read */
    FR_EOF = 0,             /* clean end of stream, before any header byte */
    FR_ERR = -1,            /* read() failed; errno is set */
    FR_TRUNC_HDR = -2,      /* stream ended inside the 17-byte header */
    FR_TRUNC_PAYLOAD = -3,  /* stream ended inside the payload */
    FR_PROTO = -4           /* protocol violation: length > 1500 */
};

/*
 * Read exactly n bytes. Retries on EINTR.
 * Returns n when everything arrived; the number of bytes read (0..n-1) if
 * EOF came first (0 means EOF before any byte); -1 on error (errno kept).
 */
ssize_t read_full(int fd, void *buf, size_t n);

/*
 * Write exactly n bytes. Retries on EINTR.
 * Returns 0 on success, -1 on error (errno kept, e.g. EPIPE, ECONNRESET).
 */
int write_full(int fd, const void *buf, size_t n);

/*
 * Pack header + payload into out (needs HDR_LEN + hdr->len bytes).
 * Returns the number of bytes written (HDR_LEN + len), or -1 with errno set
 * if hdr->len > MAX_PAYLOAD (EMSGSIZE) or payload is NULL while len > 0 (EINVAL).
 */
int frame_pack(const frame_hdr_t *hdr, const uint8_t *payload, uint8_t *out);

/* Read the 17 header bytes in `bytes` into hdr. Does not check the values. */
void frame_unpack(const uint8_t *bytes, frame_hdr_t *hdr);

/*
 * Read one frame. payload must have room for MAX_PAYLOAD bytes; it is not
 * NUL-terminated. Returns an enum frame_rc value.
 */
enum frame_rc frame_recv(int fd, frame_hdr_t *hdr, uint8_t *payload);

/* Pack and send one frame. Returns 0 on success, -1 on error (errno set). */
int frame_send(int fd, const frame_hdr_t *hdr, const uint8_t *payload);

/* Short text for a frame_rc value, for log and error messages. */
const char *frame_rc_str(enum frame_rc rc);

/* ------------------------------------------------------------------ */
/* Reference-counted frame (Part 4)                                    */
/* ------------------------------------------------------------------ */

/*
 * One received frame, shared (not copied) between all the output queues
 * that hold it. wire[] holds the frame exactly as it goes on the wire
 * (17 header bytes + payload), so a sender can write it with one write_full.
 * The other fields are the same header values, unpacked for easy use.
 *
 * Ownership rule: whoever holds a reference must call frame_unref() once.
 * The thread that brings the count to 0 frees the frame.
 */
typedef struct {
    pthread_mutex_t ref_lock;      /* protects refcount */
    int refcount;
    uint16_t len, vlan;
    uint8_t src[MAC_LEN], dst[MAC_LEN], prio;
    uint8_t wire[HDR_LEN + MAX_PAYLOAD];
} frame_t;

/* New frame with refcount 1 (the creator's own reference). NULL on failure (errno set). */
frame_t *frame_new(void);

/* Add one reference. */
void frame_ref(frame_t *f);

/* Drop one reference; frees the frame when the last one is dropped. */
void frame_unref(frame_t *f);

/* Number of frames allocated and not yet freed (for leak checks in tests). */
long frame_live_count(void);

/*
 * Like frame_recv(), but reads straight into f->wire and fills the unpacked
 * fields of f. Same return values. f->len, vlan, src, dst and prio are
 * valid when the result is FR_OK, FR_TRUNC_PAYLOAD or FR_PROTO.
 */
enum frame_rc frame_recv_f(int fd, frame_t *f);

#endif /* FRAME_H */
