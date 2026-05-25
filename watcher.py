import os, socket, struct

HOST = os.environ["RCON_HOST"]
PORT = int(os.environ["RCON_PORT"])
PW   = os.environ["RCON_PASSWORD"]

def send(sock, i, t, body):
    p = struct.pack("<ii", i, t) + body.encode() + b"\x00\x00"
    sock.sendall(struct.pack("<i", len(p)) + p)

def rexact(sock, n):
    d = b""
    while len(d) < n:
        c = sock.recv(n - len(d))
        if not c:
            raise IOError("connection closed")
        d += c
    return d

def recv(sock):
    ln = struct.unpack("<i", rexact(sock, 4))[0]
    data = rexact(sock, ln)
    i, t = struct.unpack("<ii", data[:8])
    return i, t, data[8:-2].decode("utf-8", "replace")

s = socket.create_connection((HOST, PORT), timeout=10)
s.settimeout(8)
print("CONNECTED to", HOST, PORT)

send(s, 1, 3, PW)
for _ in range(5):
    try:
        i, t, b = recv(s)
        print("AUTH: id", i, "type", t, "body", repr(b))
        if t == 2:
            break
    except Exception as e:
        print("AUTH error:", e); break

send(s, 2, 2, "sql SELECT MAX(rowid) FROM game_events;")
send(s, 3, 2, "")
for _ in range(20):
    try:
        i, t, b = recv(s)
        print("CMD: id", i, "type", t, "len", len(b), "body", repr(b))
        if i == 3:
            break
    except Exception as e:
        print("CMD end:", e); break

s.close()
print("DONE")
