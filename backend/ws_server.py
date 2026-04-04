import asyncio
import websockets
import json
import random
import string
import os
import time
import secrets
import hashlib
import sys
import re

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'game'))
from game_engine import GameEngine
from aiohttp import web, WSMsgType

# ── In-memory stores ─────────────────────────────────────────────────────────
rooms          = {}
sessions       = {}
online_sockets = {}
auth_tokens    = {}

# Invite-game coordination:
#   pending_game_for[username] = {code, symbol, session_token, opponent}
#   pending_events[username]   = asyncio.Event  (fires when above is set)
pending_game_for = {}
pending_events   = {}

DB_PATH = os.environ.get("MORIX_DB", "morix.db")


# ── Password hashing (PBKDF2-SHA256 + salt) ───────────────────────────────────
def hash_password(password: str, salt: bytes = None) -> str:
    salt = salt or os.urandom(16)
    key  = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 260_000)
    return salt.hex() + ':' + key.hex()

def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, key_hex = stored.split(':', 1)
        salt = bytes.fromhex(salt_hex)
        expected = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 260_000).hex()
        return secrets.compare_digest(expected, key_hex)
    except Exception:
        return False


# ── Input sanitisation ────────────────────────────────────────────────────────
_USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{3,32}$')
def valid_username(name: str) -> bool:
    return bool(_USERNAME_RE.match(name))


# ── SQLite — single DB for users, games, leaderboard ─────────────────────────
async def init_db():
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username      TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                friends       TEXT NOT NULL DEFAULT '[]',
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS games (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                winner    TEXT,
                loser     TEXT,
                moves     INTEGER,
                abandoned INTEGER DEFAULT 0,
                ts        DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS players (
                username TEXT PRIMARY KEY,
                wins     INTEGER DEFAULT 0,
                losses   INTEGER DEFAULT 0
            )
        """)
        await db.commit()
    print(f"Database ready: {DB_PATH}")

    # One-time migration from legacy morix_users.json
    USERS_PATH = os.environ.get("MORIX_USERS", "morix_users.json")
    if os.path.exists(USERS_PATH):
        import aiosqlite as _aio
        async with _aio.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM users") as cur:
                count = (await cur.fetchone())[0]
        if count == 0:
            try:
                with open(USERS_PATH) as f:
                    legacy = json.load(f)
                async with _aio.connect(DB_PATH) as db:
                    for uname, udata in legacy.items():
                        await db.execute(
                            "INSERT OR IGNORE INTO users(username,password_hash,friends) VALUES(?,?,?)",
                            (uname, udata.get("password_hash", ""), json.dumps(udata.get("friends", [])))
                        )
                    await db.commit()
                print(f"Migrated {len(legacy)} users from {USERS_PATH}")
            except Exception as e:
                print(f"Migration warning: {e}")


# ── User DB helpers ───────────────────────────────────────────────────────────
async def db_get_user(username: str):
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT password_hash, friends FROM users WHERE username=?", (username,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    return {"password_hash": row[0], "friends": json.loads(row[1])}

async def db_user_exists(username: str) -> bool:
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM users WHERE username=?", (username,)) as cur:
            return (await cur.fetchone()) is not None

async def db_create_user(username: str, password_hash: str):
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(username,password_hash,friends) VALUES(?,?,?)",
            (username, password_hash, "[]")
        )
        await db.commit()

async def db_get_friends(username: str) -> list:
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT friends FROM users WHERE username=?", (username,)) as cur:
            row = await cur.fetchone()
    return json.loads(row[0]) if row else []

async def db_set_friends(username: str, friends: list):
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET friends=? WHERE username=?",
            (json.dumps(friends), username)
        )
        await db.commit()

async def db_update_password(username: str, password_hash: str):
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET password_hash=? WHERE username=?",
            (password_hash, username)
        )
        await db.commit()

async def db_all_usernames() -> list:
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT username FROM users") as cur:
            return [r[0] for r in await cur.fetchall()]


# ── Game DB helpers ───────────────────────────────────────────────────────────
async def save_game(winner, loser, total_moves, abandoned=False):
    try:
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO games(winner,loser,moves,abandoned) VALUES(?,?,?,?)",
                (winner, loser, total_moves, 1 if abandoned else 0)
            )
            if winner and not abandoned:
                await db.execute(
                    "INSERT INTO players(username,wins) VALUES(?,1) "
                    "ON CONFLICT(username) DO UPDATE SET wins=wins+1", (winner,)
                )
            if loser and not abandoned:
                await db.execute(
                    "INSERT INTO players(username,losses) VALUES(?,1) "
                    "ON CONFLICT(username) DO UPDATE SET losses=losses+1", (loser,)
                )
            await db.commit()
    except Exception as e:
        print(f"save_game error: {e}")

async def get_leaderboard():
    try:
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT username, wins FROM players ORDER BY wins DESC LIMIT 10"
            ) as cur:
                rows = await cur.fetchall()
        return [{"player": r[0], "wins": r[1]} for r in rows]
    except Exception:
        return []


# ── Session helpers ───────────────────────────────────────────────────────────
def create_session(symbol, room_code, username=None):
    token = secrets.token_hex(16)
    sessions[token] = {
        "symbol": symbol, "room_code": room_code,
        "username": username, "expires": time.time() + 3600
    }
    return token

def validate_session(token):
    s = sessions.get(token)
    if not s or s["expires"] < time.time():
        sessions.pop(token, None)
        return None
    return s

def invalidate_sessions_for_room(code: str):
    for tok in [t for t, s in sessions.items() if s.get("room_code") == code]:
        del sessions[tok]

def purge_expired_sessions():
    now = time.time()
    for t in [t for t, s in sessions.items() if s["expires"] < now]:
        del sessions[t]


# ── Auth tokens ───────────────────────────────────────────────────────────────
def create_auth_token(username: str) -> str:
    token = secrets.token_hex(20)
    auth_tokens[token] = {"username": username, "expires": time.time() + 86400 * 7}
    return token

def validate_auth_token(token: str):
    entry = auth_tokens.get(token)
    if not entry or entry["expires"] < time.time():
        auth_tokens.pop(token, None)
        return None
    return entry["username"]


# ── Friend / status helpers ───────────────────────────────────────────────────
def get_status(username: str) -> str:
    if username not in online_sockets:
        return "offline"
    for room in rooms.values():
        if username in room.get("player_usernames", {}).values():
            return "ingame"
    return "online"

async def push_friend_status(username: str):
    ws = online_sockets.get(username)
    if not ws:
        return
    friends  = await db_get_friends(username)
    statuses = {f: get_status(f) for f in friends}
    try:
        await ws.send(json.dumps({"type": "friend_statuses", "statuses": statuses}))
    except Exception:
        pass

async def notify_friends_of_status_change(username: str):
    for other in await db_all_usernames():
        if other == username or other not in online_sockets:
            continue
        friends = await db_get_friends(other)
        if username in friends:
            await push_friend_status(other)


# ── Room helpers ──────────────────────────────────────────────────────────────
def generate_code():
    while True:
        code = ''.join(random.choices(string.digits, k=4))
        if code not in rooms:
            return code

def make_room(ws_x, username_x):
    first_turn = random.choice(["X", "O"])
    code = generate_code()
    rooms[code] = {
        "game":             GameEngine(),
        "players":          [ws_x, None],
        "turn":             first_turn,
        "placed":           0,
        "total_moves":      0,
        "player_usernames": {"X": username_x, "O": None},
        "symbols":          {ws_x: "X"},
        "rematch_votes":    {},
        "first_turn":       first_turn,
        "game_over":        False,
    }
    return code

async def broadcast_board(room):
    board = room["game"].board
    msg = json.dumps({"type": "board", "board": board, "turn": room["turn"]})
    for p in room["players"]:
        if p is not None:
            try: await p.send(msg)
            except Exception: pass

async def broadcast(room, payload):
    msg = json.dumps(payload)
    for p in room["players"]:
        if p is not None:
            try: await p.send(msg)
            except Exception: pass

async def safe_send(ws, payload):
    try:
        await ws.send(json.dumps(payload) if not isinstance(payload, str) else payload)
    except Exception:
        pass


# ── Destroy room immediately (explicit leave) ─────────────────────────────────
async def destroy_room(code: str, leaving_symbol: str, leaving_username: str):
    room = rooms.pop(code, None)
    # Always invalidate sessions for this room so auto-rejoin on refresh can't
    # pull the player back in (Bug #1 fix)
    invalidate_sessions_for_room(code)
    if room is None:
        return
    other_slot = 1 if leaving_symbol == "X" else 0
    opp_ws = room["players"][other_slot]
    # Only notify opponent if game wasn't already over (avoid double-notifying)
    if opp_ws and not room.get("game_over"):
        try:
            await opp_ws.send(json.dumps({
                "type":    "opponent_left",
                "message": f"{leaving_username or 'Opponent'} left the game."
            }))
        except Exception:
            pass
    for uname in room.get("player_usernames", {}).values():
        if uname and uname in online_sockets:
            await notify_friends_of_status_change(uname)
    print(f"Room {code} destroyed — {leaving_symbol} ({leaving_username}) left")


# ── Room cleanup after disconnect (60 s grace period) ─────────────────────────
async def _room_cleanup_task(code: str, slot: int):
    await asyncio.sleep(60)
    if code not in rooms:
        return
    room = rooms[code]
    if room["players"][slot] is None:
        await save_game(None, None, room.get("total_moves", 0), abandoned=True)
        del rooms[code]
        print(f"Room {code} cleaned up after 60 s timeout")


# ── Social / meta message handler ─────────────────────────────────────────────
async def handle_social(websocket, username, parsed, state):
    """Handle non-game actions. Returns True if consumed."""
    a = parsed.get("action") if isinstance(parsed, dict) else None
    if not a:
        return False

    # search_user
    if a == "search_user":
        query = (parsed.get("username") or "").strip()
        found = bool(query) and bool(username) and await db_user_exists(query) and query != username
        await safe_send(websocket, {"type": "search_user_result", "found": found, "username": query if found else ""})
        return True

    # add_friend
    if a == "add_friend":
        if not username:
            await safe_send(websocket, {"type": "add_friend_result", "success": False, "message": "Not logged in."})
            return True
        fn = (parsed.get("username") or "").strip()
        if not fn:
            await safe_send(websocket, {"type": "add_friend_result", "success": False, "message": "Enter a username."})
            return True
        if fn == username:
            await safe_send(websocket, {"type": "add_friend_result", "success": False, "message": "That's you!"})
            return True
        if not await db_user_exists(fn):
            await safe_send(websocket, {"type": "add_friend_result", "success": False, "message": "User not found."})
            return True
        friends = await db_get_friends(username)
        if fn in friends:
            await safe_send(websocket, {"type": "add_friend_result", "success": False, "message": "Already friends."})
            return True
        friends.append(fn)
        await db_set_friends(username, friends)
        await safe_send(websocket, {"type": "add_friend_result", "success": True, "friend": fn, "status": get_status(fn)})
        return True

    # remove_friend
    if a == "remove_friend":
        if not username:
            return True
        fn = (parsed.get("username") or "").strip()
        friends = [f for f in await db_get_friends(username) if f != fn]
        await db_set_friends(username, friends)
        await safe_send(websocket, {"type": "remove_friend_result", "success": True, "friend": fn})
        return True

    # send_invite
    if a == "send_invite":
        if not username:
            return True
        target = parsed.get("to", "")
        target_ws = online_sockets.get(target)
        if not target_ws:
            await safe_send(websocket, {"type": "invite_result", "success": False, "message": f"{target} is offline."})
            return True
        try:
            await target_ws.send(json.dumps({"type": "incoming_invite", "from": username}))
            await safe_send(websocket, {"type": "invite_result", "success": True, "to": target})
        except Exception:
            await safe_send(websocket, {"type": "invite_result", "success": False, "message": f"Could not reach {target}."})
        return True

    # accept_invite
    if a == "accept_invite":
        if not username:
            return True
        inviter = parsed.get("from", "")
        inviter_ws = online_sockets.get(inviter)
        if not inviter_ws:
            await safe_send(websocket, {"type": "error", "message": f"{inviter} went offline."})
            return True

        code       = generate_code()
        first_turn = random.choice(["X", "O"])
        rooms[code] = {
            "game":             GameEngine(),
            "players":          [inviter_ws, websocket],
            "turn":             first_turn,
            "placed":           0,
            "total_moves":      0,
            "player_usernames": {"X": inviter, "O": username},
            "symbols":          {inviter_ws: "X", websocket: "O"},
            "rematch_votes":    {},
            "first_turn":       first_turn,
            "game_over":        False,
        }
        tok_x = create_session("X", code, inviter)
        tok_o = create_session("O", code, username)

        # ── KEY FIX: signal the inviter's lobby loop BEFORE sending any game
        # messages, so their server-side coroutine enters the game loop first.
        pending_game_for[inviter] = {
            "code": code, "symbol": "X",
            "session_token": tok_x, "opponent": username
        }
        ev = pending_events.get(inviter)
        if ev:
            ev.set()

        # Yield so the inviter's lobby loop coroutine can run and break out
        await asyncio.sleep(0)

        # Now send game_ready / start / board — inviter's handler is in game loop
        try:
            await inviter_ws.send(json.dumps({
                "type": "game_ready", "code": code, "symbol": "X",
                "session_token": tok_x, "opponent": username, "first_turn": first_turn
            }))
        except Exception:
            pass

        state["code"]          = code
        state["player_symbol"] = "O"
        state["session_token"] = tok_o
        await safe_send(websocket, {
            "type": "game_ready", "code": code, "symbol": "O",
            "session_token": tok_o, "opponent": inviter, "first_turn": first_turn
        })

        await broadcast(rooms[code], {"type": "start", "first_turn": first_turn})
        await broadcast_board(rooms[code])

        await notify_friends_of_status_change(inviter)
        await notify_friends_of_status_change(username)
        return True

    # decline_invite
    if a == "decline_invite":
        inviter = parsed.get("from", "")
        inviter_ws = online_sockets.get(inviter)
        if inviter_ws:
            try:
                await inviter_ws.send(json.dumps({"type": "invite_declined", "by": username}))
            except Exception:
                pass
        return True

    return False


# ── WebSocket handler ─────────────────────────────────────────────────────────
async def handler(websocket):
    code          = None
    player_symbol = None
    session_token = None
    username      = None

    try:
        raw    = await asyncio.wait_for(websocket.recv(), timeout=30)
        msg    = json.loads(raw)
        action = msg.get("action")

        # ── REGISTER ────────────────────────────────────────────────────────
        if action == "register":
            uname = (msg.get("username") or "").strip()
            pwd   = msg.get("password", "")
            if not uname or not pwd:
                await websocket.send(json.dumps({"type": "error", "message": "Username and password required."})); return
            if not valid_username(uname):
                await websocket.send(json.dumps({"type": "error", "message": "Username must be 3–32 chars: letters, numbers, underscore only."})); return
            if len(pwd) < 6:
                await websocket.send(json.dumps({"type": "error", "message": "Password must be at least 6 characters."})); return
            if await db_user_exists(uname):
                await websocket.send(json.dumps({"type": "error", "message": "Username already taken."})); return
            await db_create_user(uname, hash_password(pwd))
            auth_token = create_auth_token(uname)
            username   = uname
            online_sockets[username] = websocket
            await websocket.send(json.dumps({"type": "registered", "username": uname, "auth_token": auth_token, "friends": [], "statuses": {}}))
            await notify_friends_of_status_change(username)
            print(f"Registered: {uname}")

        # ── LOGIN ────────────────────────────────────────────────────────────
        elif action == "login":
            uname = (msg.get("username") or "").strip()
            pwd   = msg.get("password", "")
            udata = await db_get_user(uname)
            ok    = False
            if udata:
                stored = udata.get("password_hash", "")
                if ':' in stored:
                    ok = verify_password(pwd, stored)
                else:
                    # Legacy SHA-256 migration
                    if stored == hashlib.sha256(pwd.encode()).hexdigest():
                        await db_update_password(uname, hash_password(pwd))
                        ok = True
            if not ok:
                await websocket.send(json.dumps({"type": "error", "message": "Invalid username or password."})); return
            auth_token = create_auth_token(uname)
            username   = uname
            online_sockets[username] = websocket
            friends  = await db_get_friends(uname)
            statuses = {f: get_status(f) for f in friends}
            await websocket.send(json.dumps({"type": "logged_in", "username": uname, "auth_token": auth_token, "friends": friends, "statuses": statuses}))
            await notify_friends_of_status_change(username)
            print(f"Login: {uname}")

        # ── AUTO-LOGIN ───────────────────────────────────────────────────────
        elif action == "auto_login":
            token = msg.get("auth_token", "")
            uname = validate_auth_token(token)
            if not uname or not await db_user_exists(uname):
                await websocket.send(json.dumps({"type": "error", "message": "Session expired. Please log in again."})); return
            username = uname
            online_sockets[username] = websocket
            friends  = await db_get_friends(uname)
            statuses = {f: get_status(f) for f in friends}
            active_game = None
            for t, s in sessions.items():
                if s.get("username") == username and s["expires"] > time.time() and s["room_code"] in rooms:
                    active_game = {"session_token": t, "code": s["room_code"], "symbol": s["symbol"]}
                    break
            await websocket.send(json.dumps({
                "type": "logged_in", "username": uname, "auth_token": token,
                "friends": friends, "statuses": statuses, "active_game": active_game
            }))
            await notify_friends_of_status_change(username)
            print(f"Auto-login: {uname}")

        # ── HOST ─────────────────────────────────────────────────────────────
        elif action == "host":
            code = make_room(websocket, username)
            player_symbol = "X"
            session_token = create_session("X", code, username)
            if username:
                online_sockets[username] = websocket
            await websocket.send(json.dumps({"type": "hosted", "code": code, "symbol": "X", "session_token": session_token}))
            if username:
                await notify_friends_of_status_change(username)
            print(f"Hosted room {code} by {username}")

        # ── JOIN ─────────────────────────────────────────────────────────────
        elif action == "join":
            jcode = msg.get("code", "").strip()
            if jcode not in rooms:
                await websocket.send(json.dumps({"type": "error", "message": "Room not found."})); return
            room = rooms[jcode]
            if room["players"][1] is not None:
                await websocket.send(json.dumps({"type": "error", "message": "Room is already full."})); return
            code  = jcode
            room["players"][1]            = websocket
            room["symbols"][websocket]    = "O"
            room["player_usernames"]["O"] = username
            player_symbol = "O"
            session_token = create_session("O", code, username)
            await websocket.send(json.dumps({"type": "joined", "symbol": "O", "session_token": session_token}))
            for p in room["players"]:
                if p: await p.send(json.dumps({"type": "start", "first_turn": room["turn"]}))
            await broadcast_board(room)
            if username:
                await notify_friends_of_status_change(username)
            print(f"Joined room {code} by {username}")

        # ── REJOIN ────────────────────────────────────────────────────────────
        elif action == "rejoin":
            token = msg.get("token", "")
            sess  = validate_session(token)
            if not sess:
                await websocket.send(json.dumps({"type": "error", "message": "Session expired."})); return
            code          = sess["room_code"]
            player_symbol = sess["symbol"]
            username      = sess.get("username")
            if code not in rooms:
                await websocket.send(json.dumps({"type": "error", "message": "Room no longer exists."})); return
            room = rooms[code]
            slot = 0 if player_symbol == "X" else 1
            room["players"][slot]                   = websocket
            room["symbols"][websocket]              = player_symbol
            room["player_usernames"][player_symbol] = username
            if username:
                online_sockets[username] = websocket
            session_token = token
            await websocket.send(json.dumps({"type": "rejoined", "symbol": player_symbol, "code": code}))
            await broadcast_board(room)
            print(f"Rejoined room {code} as {player_symbol} ({username})")

        else:
            await websocket.send(json.dumps({"type": "error", "message": "Invalid action."})); return

        # ── LOBBY LOOP ────────────────────────────────────────────────────────
        # Entered only for logged-in users who aren't yet in a room.
        # Uses asyncio.wait() so an invite-accepted event can break the loop
        # immediately — no polling delay, no dropped game messages.
        if code is None and username is not None:
            ev = asyncio.Event()
            pending_events[username] = ev
            state = {"code": None, "player_symbol": None, "session_token": None}

            try:
                while state["code"] is None:
                    recv_task  = asyncio.create_task(websocket.recv())
                    event_task = asyncio.create_task(ev.wait())

                    done, pending_tasks = await asyncio.wait(
                        {recv_task, event_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for t in pending_tasks:
                        t.cancel()
                        try: await t
                        except (asyncio.CancelledError, Exception): pass

                    # Invite accepted — break into game loop
                    if event_task in done:
                        pg = pending_game_for.pop(username, None)
                        if pg:
                            code          = pg["code"]
                            player_symbol = pg["symbol"]
                            session_token = pg["session_token"]
                            break
                        ev.clear()
                        continue

                    # Normal message received
                    try:
                        raw2 = recv_task.result()
                    except Exception:
                        raise websockets.exceptions.ConnectionClosed(None, None)

                    try:
                        p2 = json.loads(raw2)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(p2, dict):
                        continue
                    a2 = p2.get("action")

                    if await handle_social(websocket, username, p2, state):
                        if state["code"]:
                            code          = state["code"]
                            player_symbol = state["player_symbol"]
                            session_token = state["session_token"]
                        continue

                    if a2 == "host":
                        code = make_room(websocket, username)
                        player_symbol = "X"
                        session_token = create_session("X", code, username)
                        online_sockets[username] = websocket
                        await safe_send(websocket, {"type": "hosted", "code": code, "symbol": "X", "session_token": session_token})
                        await notify_friends_of_status_change(username)
                        print(f"Hosted room {code} by {username} (lobby)")
                        break

                    if a2 == "join":
                        jcode = p2.get("code", "").strip()
                        if jcode not in rooms:
                            await safe_send(websocket, {"type": "error", "message": "Room not found."}); continue
                        jroom = rooms[jcode]
                        if jroom["players"][1] is not None:
                            await safe_send(websocket, {"type": "error", "message": "Room is already full."}); continue
                        code = jcode
                        jroom["players"][1]            = websocket
                        jroom["symbols"][websocket]    = "O"
                        jroom["player_usernames"]["O"] = username
                        player_symbol = "O"
                        session_token = create_session("O", code, username)
                        await safe_send(websocket, {"type": "joined", "symbol": "O", "session_token": session_token})
                        for p in jroom["players"]:
                            if p: await p.send(json.dumps({"type": "start", "first_turn": jroom["turn"]}))
                        await broadcast_board(jroom)
                        await notify_friends_of_status_change(username)
                        print(f"Joined room {code} by {username} (lobby)")
                        break

                    if a2 == "rejoin":
                        token2 = p2.get("token", "")
                        sess2  = validate_session(token2)
                        if not sess2:
                            await safe_send(websocket, {"type": "error", "message": "Session expired."}); continue
                        rcode = sess2["room_code"]
                        if rcode not in rooms:
                            await safe_send(websocket, {"type": "error", "message": "Room no longer exists."}); continue
                        code          = rcode
                        player_symbol = sess2["symbol"]
                        username      = sess2.get("username")
                        rroom = rooms[code]
                        slot = 0 if player_symbol == "X" else 1
                        rroom["players"][slot]                   = websocket
                        rroom["symbols"][websocket]              = player_symbol
                        rroom["player_usernames"][player_symbol] = username
                        online_sockets[username] = websocket
                        session_token = token2
                        await safe_send(websocket, {"type": "rejoined", "symbol": player_symbol, "code": code})
                        await broadcast_board(rroom)
                        print(f"Rejoined room {code} as {player_symbol} ({username}) (lobby)")
                        break
            finally:
                pending_events.pop(username, None)
                pending_game_for.pop(username, None)

        # ── Fetch room reference ──────────────────────────────────────────────
        room = rooms.get(code)
        if room is None:
            return

        # ── Game loop ─────────────────────────────────────────────────────────
        while True:
            message = await websocket.recv()

            try:
                parsed = json.loads(message)
                if isinstance(parsed, dict):
                    inner_action = parsed.get("action")

                    state = {"code": None, "player_symbol": None, "session_token": None}
                    if await handle_social(websocket, username, parsed, state):
                        continue

                    # ── EXPLICIT LEAVE ────────────────────────────────────────
                    if inner_action == "leave_game":
                        await destroy_room(code, player_symbol, username)
                        return

                    # ── REMATCH REQUEST ───────────────────────────────────────
                    if inner_action == "rematch_request":
                        opp_slot = 1 if player_symbol == "X" else 0
                        opp_ws   = room["players"][opp_slot]
                        if opp_ws:
                            await safe_send(opp_ws, {"type": "rematch_request", "from": player_symbol})
                        room["rematch_votes"][player_symbol] = "pending"
                        continue

                    # ── REMATCH ACCEPT ────────────────────────────────────────
                    if inner_action == "rematch_accept":
                        first_turn = random.choice(["X", "O"])
                        room["game"].reset()
                        room["placed"]        = 0
                        room["total_moves"]   = 0
                        room["turn"]          = first_turn
                        room["first_turn"]    = first_turn
                        room["rematch_votes"] = {}
                        room["game_over"]     = False
                        await broadcast(room, {"type": "rematch_start", "first_turn": first_turn})
                        await broadcast_board(room)
                        continue

                    # ── REMATCH DECLINE ───────────────────────────────────────
                    if inner_action == "rematch_decline":
                        opp_slot = 1 if player_symbol == "X" else 0
                        opp_ws   = room["players"][opp_slot]
                        if opp_ws:
                            await safe_send(opp_ws, {"type": "rematch_declined", "by": player_symbol})
                        room["rematch_votes"] = {}
                        rooms.pop(code, None)
                        return

            except (ValueError, TypeError):
                pass

            # Skip moves when game is over or not this player's turn
            if room is None or room.get("game_over") or player_symbol != room["turn"]:
                continue

            # ── PLACEMENT ─────────────────────────────────────────────────────
            if room["placed"] < 6:
                try:
                    pos = int(json.loads(message))
                    if room["game"].place_piece(pos, player_symbol):
                        room["placed"]      += 1
                        room["total_moves"] += 1
                        if room["game"].check_win(player_symbol):
                            loser = "O" if player_symbol == "X" else "X"
                            room["game_over"] = True
                            await broadcast(room, {"type": "win", "player": player_symbol})
                            await save_game(player_symbol, loser, room["total_moves"])
                            for un in room.get("player_usernames", {}).values():
                                if un and un in online_sockets:
                                    await notify_friends_of_status_change(un)
                            continue
                        room["turn"] = "O" if room["turn"] == "X" else "X"
                        await broadcast_board(room)
                except (ValueError, TypeError, KeyError):
                    pass
                continue

            # ── MOVEMENT ──────────────────────────────────────────────────────
            try:
                move = json.loads(message)
                if room["game"].move_piece(move["from"], move["to"], player_symbol):
                    room["total_moves"] += 1
                    if room["game"].check_win(player_symbol):
                        loser = "O" if player_symbol == "X" else "X"
                        room["game_over"] = True
                        await broadcast(room, {"type": "win", "player": player_symbol})
                        await save_game(player_symbol, loser, room["total_moves"])
                        for un in room.get("player_usernames", {}).values():
                            if un and un in online_sockets:
                                await notify_friends_of_status_change(un)
                        continue
                    room["turn"] = "O" if room["turn"] == "X" else "X"
                    await broadcast_board(room)
            except (ValueError, KeyError, json.JSONDecodeError):
                pass

    except websockets.exceptions.ConnectionClosed:
        print(f"Disconnected: {player_symbol} ({username}) room={code}")
        if username:
            pending_game_for.pop(username, None)
            ev = pending_events.pop(username, None)
            if ev: ev.set()  # unblock the lobby loop so it can exit cleanly
        if username and username in online_sockets and online_sockets[username] is websocket:
            del online_sockets[username]
            await notify_friends_of_status_change(username)
        if code and code in rooms:
            room = rooms[code]
            slot = 0 if player_symbol == "X" else 1
            room["players"][slot] = None
            if player_symbol:
                room["player_usernames"][player_symbol] = None
            if not room.get("game_over"):
                for p in room["players"]:
                    if p is not None:
                        try:
                            await p.send(json.dumps({
                                "type":    "opponent_disconnected",
                                "message": "Opponent disconnected. Waiting 60s for rejoin..."
                            }))
                        except Exception:
                            pass
            asyncio.create_task(_room_cleanup_task(code, slot))

    except asyncio.TimeoutError:
        print("Handshake timed out")


# ── HTTP routes ───────────────────────────────────────────────────────────────
async def index(request):
    base = os.path.dirname(os.path.abspath(__file__))
    return web.FileResponse(os.path.join(base, '..', 'frontend', 'index.html'))

async def leaderboard_api(request):
    return web.json_response(await get_leaderboard())

async def health(request):
    return web.json_response({"status": "ok", "rooms": len(rooms), "users_online": len(online_sockets)})


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    await init_db()
    port = int(os.environ.get("PORT", 5000))

    async def websocket_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        class WSAdapter:
            async def recv(self):
                msg = await ws.receive()
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                    raise websockets.exceptions.ConnectionClosed(None, None)
                if msg.type == WSMsgType.ERROR:
                    raise websockets.exceptions.ConnectionClosed(None, None)
                if msg.type == WSMsgType.PING:
                    await ws.pong(msg.data)
                    return await self.recv()
                return msg.data

            async def send(self, data):
                if not ws.closed:
                    await ws.send_str(data)

            @property
            def closed(self):
                return ws.closed

        await handler(WSAdapter())
        return ws

    app = web.Application()
    app.router.add_get('/',                index)
    app.router.add_get('/ws',              websocket_handler)
    app.router.add_get('/api/leaderboard', leaderboard_api)
    app.router.add_get('/api/health',      health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Morix server on port {port}")

    async def cleanup_loop():
        while True:
            await asyncio.sleep(600)
            purge_expired_sessions()

    asyncio.create_task(cleanup_loop())
    await asyncio.Future()


asyncio.run(main())