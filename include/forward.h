/*
 * forward.h - the forwarding decision.
 * Part 4: flood to every UP port except the one the frame came from.
 */
#ifndef FORWARD_H
#define FORWARD_H

#include "frame.h"
#include "port.h"

/*
 * Called by the receiver of in_port with its own reference to f. Queues the
 * frame (shared, not copied) on every other UP port. Never blocks. The
 * caller still owns its own reference afterwards and must frame_unref() it.
 */
void forward_frame(port_t *in_port, frame_t *f);

#endif /* FORWARD_H */
