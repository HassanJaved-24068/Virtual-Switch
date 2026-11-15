#!/bin/bash
# Byte order check: a payload of 0x0102 (258) bytes must put "01 02" at the
# start of the frame on the wire. The bytes are captured and shown with xxd.
cd "$(dirname "$0")/.." || exit 1
HOST=${HOST:-./host}
CAP=$(mktemp)
trap 'rm -f "$CAP"' EXIT

# Tiny capture server: accept one connection, store everything it receives.
PORTFILE=$(mktemp)
python3 -c "
import socket, sys
s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(1)
open('$PORTFILE', 'w').write(str(s.getsockname()[1]))
c, _ = s.accept()
data = b''
while True:
    d = c.recv(4096)
    if not d: break
    data += d
open('$CAP', 'wb').write(data)
" &
SRV=$!
for _ in $(seq 100); do [ -s "$PORTFILE" ] && break; sleep 0.05; done
PORT=$(cat "$PORTFILE"); rm -f "$PORTFILE"

TEXT=$(printf 'a%.0s' $(seq 258))
printf 'sendx 02:00:00:00:00:0b 258 1 %s\nquit\n' "$TEXT" | $HOST 127.0.0.1 "$PORT" 02:00:00:00:00:0a
wait $SRV

echo "first 20 bytes on the wire:"
xxd -l 20 "$CAP"
HEX=$(xxd -p -l 17 "$CAP" | tr -d '\n')
# 0102 | src 02000000000a | dst 02000000000b | vlan 0102 (=258) | prio 01
if [ "$HEX" = "0102""02000000000a""02000000000b""0102""01" ] && [ "$(stat -c %s "$CAP")" = 275 ]; then
    echo "wire_bytes: PASS (length 258 -> 01 02, vlan 258 -> 01 02, 17-byte header, 275 bytes total)"
else
    echo "wire_bytes: FAIL (got $HEX)"; exit 1
fi
