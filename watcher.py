#!/usr/bin/env python3
"""
Conan Exiles -> Discord PvP kill feed + scoreboard.
"""

import os
import sys
import json
import socket
import struct
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
RCON_HOST = os.environ.get("RCON_HOST", "")
RCON_PORT = int(os.environ.get("RCON_PORT") or 0)
RCON_PASSWORD = os.environ.get("RCON_PASSWORD", "")

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_KILLS", "")
DISCORD_SCORE_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_SCORE", "")

STATE_FILE = os.environ.get("STATE_FILE", "state.json")

KILL_EVENT_TYPE = 103
MAX_KILLS_PER_RUN = 50

# ---------------------------------------------------------------------------
# RCON constants
# ---------------------------------------------------------------------------
SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0


class RconError(Exception):
    pass


# ---------------------------------------------------------------------------
# RCON helpers
# ---------------------------------------------------------------------------
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

    body = payload[8:-2].decode("utf-8", errors="replace")

    return req_id, ptype, body


DEBUG = os.environ.get("RCON_DEBUG", "1") == "1"


def rcon_command(command, connect_timeout=10, read_timeout=8):
    with socket.create_connection((RCON_HOST, RCON_PORT), timeout=connect_timeout) as sock:

        sock.settimeout(read_timeout)

        # AUTH
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
            raise RconError("RCON authentication failed")

        # COMMAND
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
# SQL parsing
# ---------------------------------------------------------------------------
def _row_cells(line):
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

    raise RconError("Could not read MAX(rowid)")


def fetch_new_kills(last_rowid):
    query = (
        "sql SELECT rowid, ownerName, causerName FROM game_events "
        f"WHERE eventType = {KILL_EVENT_TYPE} "
        "AND causerName IS NOT NULL AND causerName <> '' "
        f"AND rowid > {last_rowid} "
        f"ORDER BY rowid ASC LIMIT {MAX_KILLS_PER_RUN};"
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

        try:
            rowid = int(cells[0].split()[-1])

        except (ValueError, IndexError):
            continue

        victim = cells[1] if len(cells) > 1 else ""
        killer = cells[2] if len(cells) > 2 else ""

        if killer:
            kills.append((rowid, victim, killer))

    return kills


# ---------------------------------------------------------------------------
# State handling
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


# ---------------------------------------------------------------------------
# Discord webhook
# ---------------------------------------------------------------------------
def _send_webhook(webhook_url, payload, method="POST", message_id=None):

    if method == "PATCH":
        url = f"{webhook_url}/messages/{message_id}"
    else:
        url = webhook_url + "?wait=true"

    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "ConanKillFeed/1.0"
        }
    )

    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()

    try:
        return json.loads(raw)

    except (ValueError, json.JSONDecodeError):
        return None


def post_discord(content):
    _send_webhook(DISCORD_WEBHOOK, {"content": content})


# ---------------------------------------------------------------------------
# Scoreboard
# ---------------------------------------------------------------------------
def build_scoreboard(kills, deaths):

    players = set(kills) | set(deaths)

    if not players:
        return "🏆 **PvP Scoreboard**\n\n_No kills yet._"

    ordered = sorted(
        players,
        key=lambda p: (
            -kills.get(p, 0),
            deaths.get(p, 0),
            p.lower()
        )
    )

    lines = ["🏆 **PvP Scoreboard**", ""]

    for i, p in enumerate(ordered[:25], start=1):
        lines.append(
            f"{i}. **{p}** — {kills.get(p, 0)} K / {deaths.get(p, 0)} D"
        )

    return "\n".join(lines)


def update_scoreboard(state):

    if not DISCORD_SCORE_WEBHOOK:
        return

    payload = {
        "content": build_scoreboard(
            state.get("kills", {}),
            state.get("deaths", {})
        )
    }

    msg_id = state.get("score_message_id")

    if msg_id:
        try:
            _send_webhook(
                DISCORD_SCORE_WEBHOOK,
                payload,
                method="PATCH",
                message_id=msg_id
            )
            return

        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise

    result = _send_webhook(
        DISCORD_SCORE_WEBHOOK,
        payload,
        method="POST"
    )

    if result and "id" in result:
        state["score_message_id"] = result["id"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():

    if not RCON_HOST or not RCON_PORT:
        sys.exit("ERROR: RCON_HOST / RCON_PORT not set")

    if not RCON_PASSWORD:
        sys.exit("ERROR: RCON_PASSWORD not set")

    if not DISCORD_WEBHOOK:
        sys.exit("ERROR: DISCORD_WEBHOOK_KILLS not set")

    state = load_state()

    last = state.get("last_rowid")

    kills_tally = state.setdefault("kills", {})
    deaths_tally = state.setdefault("deaths", {})

    # FIRST RUN
    if last is None:
        current = get_max_rowid()

        state["last_rowid"] = current

        save_state(state)

        print(f"Bootstrapped bookmark at rowid {current}")

        return

    # FETCH KILLS
    new_kills = fetch_new_kills(last)

    highest = last

    for rowid, victim, killer in new_kills:

        post_discord(f"☠️  **{killer}** killed **{victim}**")

        kills_tally[killer] = kills_tally.get(killer, 0) + 1

        deaths_tally[victim] = deaths_tally.get(victim, 0) + 1

        highest = max(highest, rowid)

    # UPDATE SCOREBOARD
    if new_kills:

        update_scoreboard(state)

        state["last_rowid"] = highest

        save_state(state)

    print(f"Posted {len(new_kills)} kill(s). Bookmark is now rowid {highest}.")


if __name__ == "__main__":
    main()
