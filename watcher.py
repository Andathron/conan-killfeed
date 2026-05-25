#!/usr/bin/env python3
"""
Conan Exiles -> Discord PvP kill feed.

Runs on a schedule (e.g. GitHub Actions). Each run it:
  1. Connects to the server's RCON.
  2. Runs a SQL query for new PvP kills (eventType 103 with a real killer name).
  3. Posts each new kill to a Discord webhook.
  4. Saves a bookmark (the highest rowid seen) so it never re-posts old kills.

Pure standard library only - no pip installs needed.
"""

import os
import sys
import json
import socket
import struct
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Configuration (host/port default to your server; password + webhook come
# from environment variables / GitHub secrets and are NEVER stored in the file)
# ---------------------------------------------------------------------------
RCON_HOST = os.environ.get("RCON_HOST", "")
RCON_PORT = int(os.environ.get("RCON_PORT") or 0)
RCON_PASSWORD = os.environ.get("RCON_PASSWORD", "")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_KILLS", "")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

KILL_EVENT_TYPE = 103          # 103 = a player death
MAX_KILLS_PER_RUN = 50         # safety cap so one run can't flood the channel

# ---------------------------------------------------------------------------
# Minimal Source-RCON client (the protocol Conan Exiles uses)
# ---------------------------------------------------------------------------
SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0


class RconError(Exception):
    pass


def _send_packet(sock, req_id, ptype, body):
    payload = struct.pack("<ii", req_id, ptype) + body.encode("utf-8") + b"\x00\x00"
    sock.sendall(struct.pack("<i", len(payload)) + payload)


def _recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise RconError("Connection closed by server")
        data += chunk
    return data


def _recv_packet(sock):
    (length,) = struct.unpack("<i", _recv_exact(sock, 4))
    payload = _recv_exact(sock, length)
    req_id, ptype = struct.unpack("<ii", payload[:8])
    body = payload[8:-2].decode("utf-8", errors="replace")  # drop the 2 trailing nulls
    return req_id, ptype, body


DEBUG = os.environ.get("RCON_DEBUG", "1") == "1"


def rcon_command(command, connect_timeout=10, read_timeout=8):
    """Connect, authenticate, run one command, return the raw text response.

    Conan's RCON returns command output as packet type 2 and doesn't echo the
    request id, so we read packets until the socket goes quiet and collect the
    bodies.
    """
    with socket.create_connection((RCON_HOST, RCON_PORT), timeout=connect_timeout) as sock:
        sock.settimeout(read_timeout)

        # --- authenticate ---
        _send_packet(sock, 1, SERVERDATA_AUTH, RCON_PASSWORD)
        auth_id = None
        for _ in range(5):
            rid, ptype, body = _recv_packet(sock)
            if DEBUG:
                print(f"[auth] id={rid} type={ptype} body={body!r}")
            if ptype == SERVERDATA_AUTH_RESPONSE:
                auth_id = rid
                break
        if auth_id is None:
            raise RconError("No RCON auth response received")
        if auth_id == -1:
            raise RconError("RCON authentication failed (wrong password)")

        # --- run the command and collect the reply ---
        # Conan quirk: it returns command output as packet type 2 (not the
        # standard type 0), and it does NOT echo our request id back. So we
        # just read until the socket goes quiet. No sentinel packet - sending
        # an empty command only produces a "couldn't parse" error.
        _send_packet(sock, 2, SERVERDATA_EXECCOMMAND, command)

        chunks = []
        while True:
            try:
                rid, ptype, body = _recv_packet(sock)
            except (socket.timeout, RconError) as e:
                if DEBUG:
                    print(f"[cmd] read ended: {e}")
                break
            if DEBUG:
                print(f"[cmd] id={rid} type={ptype} len={len(body)} body={body!r}")
            if ptype in (SERVERDATA_RESPONSE_VALUE, SERVERDATA_EXECCOMMAND):
                chunks.append(body)
        return "".join(chunks)


# ---------------------------------------------------------------------------
# Parsing the SQL text output
# ---------------------------------------------------------------------------
def _row_cells(line):
    """Split one '#N  val | val | val' result line into stripped cells."""
    return [c.strip() for c in line.split("|")]


def get_max_rowid():
    raw = rcon_command("sql SELECT MAX(rowid) FROM game_events;")
    for line in raw.splitlines():
        if line.strip().startswith("#"):
            cells = _row_cells(line)
            try:
                return int(cells[0].split()[-1])
            except (ValueError, IndexError):
                continue
    raise RconError("Could not read MAX(rowid). Raw response was:\n" + raw)


def fetch_new_kills(last_rowid):
    """Return list of (rowid, victim, killer) for new PvP kills, oldest first."""
    query = (
        "sql SELECT rowid, ownerName, causerName FROM game_events "
        f"WHERE eventType = {KILL_EVENT_TYPE} "
        "AND causerName IS NOT NULL AND causerName <> '' "
        f"AND rowid > {last_rowid} ORDER BY rowid ASC LIMIT {MAX_KILLS_PER_RUN};"
    )
    raw = rcon_command(query)
    print("----- RAW RCON RESPONSE -----")
    print(raw if raw.strip() else "(empty)")
    print("-----------------------------")

    kills = []
    for line in raw.splitlines():
        if not line.strip().startswith("#"):
            continue
        cells = _row_cells(line)
        # SELECT order: rowid, ownerName (victim), causerName (killer)
        try:
            rowid = int(cells[0].split()[-1])
        except (ValueError, IndexError):
            continue
        victim = cells[1] if len(cells) > 1 else ""
        killer = cells[2] if len(cells) > 2 else ""
        if killer:  # ignore anything without a real killer name
            kills.append((rowid, victim, killer))
    return kills


# ---------------------------------------------------------------------------
# State + Discord
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def post_discord(content):
    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15):
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not RCON_HOST or not RCON_PORT:
        sys.exit("ERROR: RCON_HOST / RCON_PORT not set (add them as GitHub secrets).")
    if not RCON_PASSWORD:
        sys.exit("ERROR: RCON_PASSWORD is not set (add it as a GitHub secret).")
    if not DISCORD_WEBHOOK:
        sys.exit("ERROR: DISCORD_WEBHOOK_KILLS is not set (add it as a GitHub secret).")

    state = load_state()
    last = state.get("last_rowid")

    # First run ever: bookmark "now" so we don't replay the whole history.
    if last is None:
        current = get_max_rowid()
        state["last_rowid"] = current
        save_state(state)
        print(f"Bootstrapped bookmark at rowid {current}. No kills posted on first run.")
        return

    kills = fetch_new_kills(last)
    highest = last
    for rowid, victim, killer in kills:
        post_discord(f"\u2620\ufe0f  **{killer}** killed **{victim}**")
        highest = max(highest, rowid)

    if highest != last:
        state["last_rowid"] = highest
        save_state(state)

    print(f"Posted {len(kills)} kill(s). Bookmark is now rowid {highest}.")


if __name__ == "__main__":
    main()
