#!/usr/bin/env python3
"""
Conan Exiles -> Discord PvP kill feed.

Runs on a schedule, such as GitHub Actions.

Features:
  - PvP kill feed
  - PvP scoreboard
  - Admin audit from ServerCommandLog.log over FTP
  - Discord webhook posting with User-Agent header fix
"""

import os
import sys
import re
import json
import socket
import struct
import urllib.request
import urllib.error
from ftplib import FTP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
RCON_HOST = os.environ.get("RCON_HOST", "")
RCON_PORT = int(os.environ.get("RCON_PORT") or 0)
RCON_PASSWORD = os.environ.get("RCON_PASSWORD", "")

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_KILLS", "")
DISCORD_SCORE_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_SCORE", "")

STATE_FILE = os.environ.get("STATE_FILE", "state.json")

# --- Admin audit ---
FTP_HOST = os.environ.get("FTP_HOST", "")
FTP_PORT = int(os.environ.get("FTP_PORT") or 21)
FTP_USER = os.environ.get("FTP_USER", "")
FTP_PASS = os.environ.get("FTP_PASS", "")
FTP_LOG_PATH = os.environ.get("FTP_LOG_PATH", "ConanSandbox/Saved/Logs/ServerCommandLog.log")
DISCORD_ADMIN_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_ADMIN", "")

MAX_ADMIN_LINES_PER_RUN = 25

KILL_EVENT_TYPE = 103
MAX_KILLS_PER_RUN = 50

# ---------------------------------------------------------------------------
# RCON
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
    body = payload[8:-2].decode("utf-8", errors="replace")
    return req_id, ptype, body


DEBUG = os.environ.get("RCON_DEBUG", "1") == "1"


def rcon_command(command, connect_timeout=10, read_timeout=8):
    with socket.create_connection((RCON_HOST, RCON_PORT), timeout=connect_timeout) as sock:
        sock.settimeout(read_timeout)

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

    raise RconError("Could not read MAX(rowid). Raw response was:\n" + raw)


def fetch_new_kills(last_rowid):
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
# State
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
# Discord
# ---------------------------------------------------------------------------
def _send_webhook(webhook_url, payload, method="POST", message_id=None):
    """POST a new webhook message or PATCH an existing one."""

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


def post_kill(killer, victim):
    embed = {
        "title": "⚔️ PvP Kill",
        "description": f"**{killer}** slew **{victim}**",
        "color": 0xB03A2E,
    }

    _send_webhook(DISCORD_WEBHOOK, {"embeds": [embed]})


# ---------------------------------------------------------------------------
# Scoreboard
# ---------------------------------------------------------------------------
def build_scoreboard(kills, deaths):
    players = set(kills) | set(deaths)

    if not players:
        return "_No kills yet — get out there._"

    ordered = sorted(
        players,
        key=lambda p: (-kills.get(p, 0), deaths.get(p, 0), p.lower())
    )

    medals = ["🥇", "🥈", "🥉"]
    lines = []

    for i, player in enumerate(ordered[:25]):
        rank = medals[i] if i < 3 else f"`{i + 1}.`"
        lines.append(
            f"{rank} **{player}** — {kills.get(player, 0)} K / {deaths.get(player, 0)} D"
        )

    return "\n".join(lines)


def update_scoreboard(state):
    if not DISCORD_SCORE_WEBHOOK:
        return

    embed = {
        "title": "🏆 PvP Leaderboard",
        "description": build_scoreboard(
            state.get("kills", {}),
            state.get("deaths", {})
        ),
        "color": 0xF1C40F,
        "footer": {"text": "Updates automatically after every kill"},
    }

    payload = {"embeds": [embed]}
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

    result = _send_webhook(DISCORD_SCORE_WEBHOOK, payload, method="POST")

    if result and "id" in result:
        state["score_message_id"] = result["id"]


# ---------------------------------------------------------------------------
# Admin audit
# ---------------------------------------------------------------------------
ADMIN_LINE_RE = re.compile(
    r"^\[(?P<ts>\d{4}\.\d\d\.\d\d-\d\d\.\d\d\.\d\d:\d+)\]\[\s*\d+\]"
    r"Player (?P<player>.+?)#\d+ (?P<action>.+?) \(player is admin\)\s*$"
)


def fetch_admin_log():
    lines = []

    ftp = FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=25)
    ftp.login(FTP_USER, FTP_PASS)

    try:
        ftp.retrlines("RETR " + FTP_LOG_PATH, lines.append)
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()

    return lines


def parse_admin_actions(lines):
    actions = []

    for line in lines:
        match = ADMIN_LINE_RE.match(line.strip())

        if not match:
            continue

        action = match.group("action")

        if action.startswith("used command: "):
            action = action[len("used command: "):].strip()

        actions.append(
            (
                match.group("ts"),
                match.group("player"),
                action
            )
        )

    return actions


COMMAND_LABELS = {
    "God": "🛡️ God mode",
    "DemiGod": "🛡️ Demigod mode",
    "Cloak": "👻 Cloak / invisible",
    "Ghost": "🚪 Ghost / no-clip",
    "Fly": "🕊️ Fly",
    "Walk": "🚶 Walk / landed",
    "NoBuildingCosts": "🧱 No building costs",
    "NoStabilityLoss": "🏗️ No stability loss",
    "NoSprintCost": "🏃 No sprint cost",
    "NoSpellCost": "✨ No spell cost",
    "NoHunger": "🍖 No hunger",
    "NoThirst": "💧 No thirst",
}


def humanize_action(action):
    if action.startswith("entered movement mode "):
        return "🔀 Movement: " + action[len("entered movement mode "):].strip()

    parts = action.split()
    cmd = parts[0] if parts else action

    if cmd == "SpawnItem" and len(parts) >= 3:
        return f"📦 Spawned {parts[2]}× item `{parts[1]}`"

    if cmd == "SpawnItem" and len(parts) == 2:
        return f"📦 Spawned item `{parts[1]}`"

    if cmd in COMMAND_LABELS:
        return COMMAND_LABELS[cmd]

    return f"⚙️ `{action}`"


def _fmt_log_time(ts):
    try:
        hh, mm = ts.split("-")[1].split(".")[:2]
        return f"{hh}:{mm}"
    except Exception:
        return ts


def run_admin_audit(state):
    if not (FTP_HOST and FTP_USER and FTP_PASS and DISCORD_ADMIN_WEBHOOK):
        return

    actions = parse_admin_actions(fetch_admin_log())

    if not actions:
        print("Admin audit: no admin-command lines in log.")
        return

    last_ts = state.get("last_admin_ts")

    if last_ts is None:
        state["last_admin_ts"] = actions[-1][0]
        save_state(state)
        print(f"Admin audit bootstrapped at {actions[-1][0]}. Nothing posted on first run.")
        return

    new_actions = [action for action in actions if action[0] > last_ts]

    if not new_actions:
        print("Admin audit: no new admin actions.")
        return

    deduped = []

    for ts, player, action in new_actions:
        if deduped and deduped[-1][1] == player and deduped[-1][2] == action:
            continue

        deduped.append((ts, player, action))

    shown = deduped[:MAX_ADMIN_LINES_PER_RUN]

    body = [
        f"`{_fmt_log_time(ts)}` **{player}** — {humanize_action(action)}"
        for ts, player, action in shown
    ]

    if len(deduped) > MAX_ADMIN_LINES_PER_RUN:
        body.append(
            f"\n_…and {len(deduped) - MAX_ADMIN_LINES_PER_RUN} more this period._"
        )

    embed = {
        "title": "🛡️ Admin Activity",
        "description": "\n".join(body),
        "color": 0xE67E22,
        "footer": {"text": "Conan Admin Audit"},
    }

    _send_webhook(DISCORD_ADMIN_WEBHOOK, {"embeds": [embed]})

    state["last_admin_ts"] = new_actions[-1][0]
    save_state(state)

    print(
        f"Admin audit: posted {len(shown)} action(s). "
        f"Bookmark now {new_actions[-1][0]}."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not RCON_HOST or not RCON_PORT:
        sys.exit("ERROR: RCON_HOST / RCON_PORT not set.")

    if not RCON_PASSWORD:
        sys.exit("ERROR: RCON_PASSWORD is not set.")

    if not DISCORD_WEBHOOK:
        sys.exit("ERROR: DISCORD_WEBHOOK_KILLS is not set.")

    state = load_state()

    last = state.get("last_rowid")
    kills_tally = state.setdefault("kills", {})
    deaths_tally = state.setdefault("deaths", {})

    if last is None:
        current = get_max_rowid()
        state["last_rowid"] = current
        save_state(state)
        print(f"Bootstrapped bookmark at rowid {current}. No kills posted on first run.")
    else:
        new_kills = fetch_new_kills(last)
        highest = last

        for rowid, victim, killer in new_kills:
            post_kill(killer, victim)

            kills_tally[killer] = kills_tally.get(killer, 0) + 1
            deaths_tally[victim] = deaths_tally.get(victim, 0) + 1

            highest = max(highest, rowid)

        if new_kills:
            update_scoreboard(state)
            state["last_rowid"] = highest
            save_state(state)

        print(f"Posted {len(new_kills)} kill(s). Bookmark is now rowid {highest}.")

    try:
        run_admin_audit(state)
    except Exception as e:
        print(f"Admin audit error skipped this run: {e}")


if __name__ == "__main__":
    main()
