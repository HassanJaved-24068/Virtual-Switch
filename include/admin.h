/*
 * admin.h - the admin socket (Part 15).
 *
 * An admin thread in the switch listens on the UNIX-domain socket /tmp/vswitch_<tcp_port>.sock (mode 0600, only the same user may connect) and serves ONE client at a time.
 * Protocol: the client sends ONE line ending in '\n' (at most 256 bytes; it has 2 s in total to send it); the switch replies with text lines, then a last line
 * "OK" or "ERR <reason>", and closes the connection.
 *
 * Commands:  show mac | show ports | clear mac | shutdown <port> | up <port> | vlan <port> <vlan> | mirror <port> <sniffer> | mirror <port> off
 * The four write commands (shutdown, up, vlan, mirror) are parsed and validated here; what they DO belongs to Parts 16 and 17 (until then a valid one
 * answers "ERR not implemented yet (Part N)"). "stats" and "stats --watch" are not commands of the socket: swctl reads the shared-memory counters itself.
 *
 * Lock order: the handlers take ports_lock for reading (show ports) or only the MAC table locks (mac_foreach, mac_clear); no lock is held while writing
 * to the client or to the log.
 */
#ifndef ADMIN_H
#define ADMIN_H

#include <pthread.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>

#define ADMIN_MAX_LINE 256              /* longest request line, '\n' included */
#define ADMIN_TIMEOUT_MS 2000           /* time a client has to send its line */

enum admin_cmd_kind {
    CMD_SHOW_MAC, CMD_SHOW_PORTS, CMD_CLEAR_MAC,
    CMD_SHUTDOWN, CMD_UP, CMD_VLAN, CMD_MIRROR, CMD_MIRROR_OFF
};

typedef struct {
    enum admin_cmd_kind kind;
    int port;                           /* shutdown, up, vlan, mirror */
    int arg;                            /* vlan: the VLAN; mirror: the sniffer port */
} admin_cmd_t;

/*
 * Parse one request line (without its '\n'). num_ports bounds the port numbers. Returns 0 and fills *cmd, or -1 and a reason in err
 * (for example "unknown command", "port 9 out of range (1-4)", "VLAN 4095 out of range (1-4094)"). No side effects: unit tested without a switch.
 */
int admin_parse(const char *line, int num_ports, admin_cmd_t *cmd, char *err, size_t errlen);

/* Socket path of the switch that listens on TCP port `tcp_port`: "/tmp/vswitch_<tcp_port>.sock" (inline: swctl uses it too). */
static inline void admin_path(int tcp_port, char *out, size_t outlen)
{
    snprintf(out, outlen, "/tmp/vswitch_%d.sock", tcp_port);
}

/* Create the socket: unlink a stale path, bind, chmod 0600, listen. Returns 0 or an errno value (nothing is left open). Start-up only. */
int admin_open(int tcp_port);

/* Start the admin thread (after the MAC table exists). Returns 0 or an errno value. */
int admin_start(pthread_t *thread);

/*
 * Stop the admin thread (if `started`), join it, close the socket and unlink the path. Safe when admin_open() was never called, and when it is
 * called twice. The thread leaves its accept loop at once (the switch's wake_efd and shutdown(fd, SHUT_RDWR)), even while it waits for a silent client.
 */
void admin_close(bool started, pthread_t thread);

#endif /* ADMIN_H */
