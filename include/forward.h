/*
 * forward.h - the forwarding decision.
 * Part 4: flood to every UP port except the one the frame came from.
 * Part 6: learn the source MAC; send a known unicast only to its port.
 */
#ifndef FORWARD_H
#define FORWARD_H

#include "frame.h"
#include "mactable.h"
#include "port.h"

/*
 * Called by the receiver of in_port with its own reference to f.
 *   - a frame whose source is broadcast is dropped;
 *   - the source MAC is learned (unless it is multicast);
 *   - broadcast, multicast and unknown destinations are flooded to every other UP port;
 *   - a known destination gets the frame on its port only (if that port is UP);
 *   - a destination learned on the ingress port is filtered (dropped).
 * The frame is shared between queues, never copied. Never blocks.
 * The caller still owns its own reference afterwards and must frame_unref() it.
 *
 * Returns LEARN_VIOLATION if the ingress port learned too many MACs (the frame
 * was dropped); the caller must then shut the port down. It returns with NO lock
 * held, so the caller may call port_request_down().
 */
enum learn_rc forward_frame(port_t *in_port, frame_t *f);

#endif /* FORWARD_H */
