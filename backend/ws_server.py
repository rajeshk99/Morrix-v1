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
rooms    = {}
sessions = {}
users    = {}
online_sockets = {}

DB_PATH = os.environ.get("MORIX_DB", "morix.db")


# ── Password hashing (PBKDF2 + salt) ─────────────────────────────────────────
# FIX #4: replaced bare SHA-256 with salted PBKDF2.
# Stored format: "<hex-salt>:<hex-key>"
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

def _try_migrate_legacy(uname: str, password: str) -> bool:
    """Upgrade a plain-SHA256 record to PBKDF2 on first login after update."""
    udata = users.get(uname, {})
    stored = udata.get("password_hash", "")
    if ':' not in stored:
        if stored == hashlib.sha256(password.encode()).hexdigest():
            udata["password_hash"] = hash_password(password)
            save_users()
            return True
        return False
    return False


# ── Input sanitisation ────────────────────────────────────────────────────────
# FIX #8: restrict usernames to alphanumeric + underscore to prevent XSS.
_USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{3,32}$')
def valid_username(name: str) -> bool:
    return bool(_USERNAME_RE.match(name))


# ── User persistence ──────────────────────────────────────────────────────────
USERS_PATH = os.environ.get("MORIX_USERS", "morix_users.json")

def load_users():
    global users
    if os.path.exists(USERS_PATH):
        try:
            with open(USERS_PATH) as f:
                users = json.load(f)
            print(f"Loaded {len(users)} user(s) from {USERS_PATH}")
        except Exception as e:
            print(f"Could not load users file: {e}")
            users = {}

def save_users():
    try:
        with open(USERS_PATH, "w") as f:
            json.dump(users, f, indent=2)
    except Exception as e:
        print(f"Could not save users: {e}")


# ── Database helpers ──────────────────────────────────────────────────────────
async def init_db():
    try:
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
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
            # FIX #13: players table is now written to by save_game()
            await db.execute("""
                CREATE TABLE IF NOT EXISTS players (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    wins     INTEGER DEFAULT 0,
                    losses   INTEGER DEFAULT 0
                )
            """)
            await db.commit()
        print("Database initialised.")
    except ImportError:
        print("aiosqlite not installed — running without DB persistence.")

# FIX #7: total_moves counts placement + movement moves, not just placements.
# FIX #13: upsert wins/losses into the players table.
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
                    "ON CONFLICT(username) DO UPDATE SET wins=wins+1",
                    (winner,)
                )
            if loser and not abandoned:
                await db.execute(
                    "INSERT INTO players(username,losses) VALUES(?,1) "
                    "ON CONFLICT(username) DO UPDATE SET losses=losses+1",
                    (loser,)
                )
            await db.commit()
    except Exception:
        pass

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
        "symbol":    symbol,
        "room_code": room_code,
        "username":  username,
        "expires":   time.time() + 3600
    }
    return token

def validate_session(token):
    s = sessions.get(token)
    if not s or s["expires"] < time.time():
        sessions.pop(token, None)
        return None
    return s

def purge_expired_sessions():
    now = time.time()
    for t in [t for t, s in sessions.items() if s["expires"] < now]:
        del sessions[t]


# ── Auth tokens ───────────────────────────────────────────────────────────────
auth_tokens = {}

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


# ── Friend status helpers ─────────────────────────────────────────────────────
# FIX #5: use string-keyed player_usernames dict instead of ws-object keys.
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
    udata = users.get(username, {})
    friends = udata.get("friends", [])
    statuses = {f: get_status(f) for f in friends}
    try:
        await ws.send(json.dumps({"type": "friend_statuses", "statuses": statuses}))
    except Exception:
        pass

async def notify_friends_of_status_change(username: str):
    for other, udata in users.items():
        if username in udata.get("friends", []) and other in online_sockets:
            await push_friend_status(other)


# ── Room helpers ──────────────────────────────────────────────────────────────
def generate_code():
    while True:
        code = ''.join(random.choices(string.digits, k=4))
        if code not in rooms:
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


# FIX #6: disconnect cleanup runs as a background task, not blocking the handler.
async def _room_cleanup_task(code: str, slot: int):
    await asyncio.sleep(60)
    if code not in rooms:
        return
    room = rooms[code]
    if room["players"][slot] is None:
        await save_game(None, None, room.get("total_moves", 0), abandoned=True)
        del rooms[code]
        print(f"Room {code} cleaned up after 60s timeout")


# ── WebSocket handler ─────────────────────────────────────────────────────────
async def handler(websocket):
    code          = None
    player_symbol = None
    session_token = None
    username      = None

    try:
        raw = await asyncio.wait_for(websocket.recv(), timeout=30)
        msg = json.loads(raw)
        action = msg.get("action")

        # ── REGISTER ────────────────────────────────────────────────────────
        if action == "register":
            uname = (msg.get("username") or "").strip()
            pwd   = msg.get("password", "")
            if not uname or not pwd:
                await websocket.send(json.dumps({"type": "error", "message": "Username and password required."}))
                return
            if not valid_username(uname):
                await websocket.send(json.dumps({"type": "error", "message": "Username must be 3–32 chars: letters, numbers, underscore only."}))
                return
            if len(pwd) < 6:
                await websocket.send(json.dumps({"type": "error", "message": "Password must be at least 6 characters."}))
                return
            if uname in users:
                await websocket.send(json.dumps({"type": "error", "message": "Username already taken."}))
                return
            users[uname] = {"password_hash": hash_password(pwd), "friends": []}
            save_users()
            auth_token = create_auth_token(uname)
            username   = uname
            online_sockets[username] = websocket
            await websocket.send(json.dumps({"type": "registered", "username": uname, "auth_token": auth_token}))
            await notify_friends_of_status_change(username)
            print(f"New user registered: {uname}")

        # ── LOGIN ────────────────────────────────────────────────────────────
        elif action == "login":
            uname = (msg.get("username") or "").strip()
            pwd   = msg.get("password", "")
            udata = users.get(uname)
            authenticated = False
            if udata:
                stored = udata.get("password_hash", "")
                if ':' in stored:
                    authenticated = verify_password(pwd, stored)
                else:
                    # migrate legacy plain-sha256 record on first login
                    authenticated = _try_migrate_legacy(uname, pwd)
            if not authenticated:
                await websocket.send(json.dumps({"type": "error", "message": "Invalid username or password."}))
                return
            auth_token = create_auth_token(uname)
            username   = uname
            online_sockets[username] = websocket
            friends    = udata.get("friends", [])
            statuses   = {f: get_status(f) for f in friends}
            await websocket.send(json.dumps({
                "type": "logged_in", "username": uname,
                "auth_token": auth_token, "friends": friends,
                "statuses": statuses
            }))
            await notify_friends_of_status_change(username)
            print(f"User logged in: {uname}")

        # ── AUTO-LOGIN ───────────────────────────────────────────────────────
        elif action == "auto_login":
            token = msg.get("auth_token", "")
            uname = validate_auth_token(token)
            if not uname or uname not in users:
                await websocket.send(json.dumps({"type": "error", "message": "Session expired. Please log in again."}))
                return
            username = uname
            online_sockets[username] = websocket
            udata    = users[username]
            friends  = udata.get("friends", [])
            statuses = {f: get_status(f) for f in friends}
            # FIX #11: include active game info so client can auto-rejoin
            active_game = None
            for t, s in sessions.items():
                if s.get("username") == username and s["expires"] > time.time() and s["room_code"] in rooms:
                    active_game = {"session_token": t, "code": s["room_code"], "symbol": s["symbol"]}
                    break
            await websocket.send(json.dumps({
                "type": "logged_in", "username": uname,
                "auth_token": token, "friends": friends,
                "statuses": statuses,
                "active_game": active_game
            }))
            await notify_friends_of_status_change(username)
            print(f"User auto-logged-in: {uname}")

        # ── HOST ─────────────────────────────────────────────────────────────
        # FIX #1: removed the early `return` that killed the host's connection.
        # FIX #5: room uses player_usernames keyed by symbol string, not ws object.
        elif action == "host":
            code  = generate_code()
            rooms[code] = {
                "game":             GameEngine(),
                "players":          [websocket, None],
                "turn":             "X",
                "placed":           0,
                "total_moves":      0,
                "player_usernames": {"X": username, "O": None},
                "symbols":          {websocket: "X"},
                "rematch_votes":    set(),
            }
            player_symbol = "X"
            session_token = create_session("X", code, username)
            if username:
                online_sockets[username] = websocket
            print(f"Room {code} created by {username or 'anonymous'}")
            await websocket.send(json.dumps({
                "type": "hosted", "code": code,
                "symbol": "X", "session_token": session_token
            }))
            if username:
                await notify_friends_of_status_change(username)
            # NOTE: intentionally NO `return` here — fall through to game loop

        # ── JOIN ─────────────────────────────────────────────────────────────
        elif action == "join":
            code  = msg.get("code", "").strip()
            if code not in rooms:
                await websocket.send(json.dumps({"type": "error", "message": "Room not found."}))
                return
            room = rooms[code]
            if room["players"][1] is not None:
                await websocket.send(json.dumps({"type": "error", "message": "Room is already full."}))
                return
            room["players"][1]                  = websocket
            room["symbols"][websocket]          = "O"
            room["player_usernames"]["O"]       = username
            player_symbol = "O"
            session_token = create_session("O", code, username)
            print(f"Player O ({username or 'anonymous'}) joined room {code}")
            await websocket.send(json.dumps({
                "type": "joined", "symbol": "O", "session_token": session_token
            }))
            for p in room["players"]:
                if p:
                    await p.send(json.dumps({"type": "start"}))
            await broadcast_board(room)
            if username:
                await notify_friends_of_status_change(username)

        # ── REJOIN ────────────────────────────────────────────────────────────
        elif action == "rejoin":
            token = msg.get("token", "")
            sess  = validate_session(token)
            if not sess:
                await websocket.send(json.dumps({"type": "error", "message": "Session expired."}))
                return
            code          = sess["room_code"]
            player_symbol = sess["symbol"]
            username      = sess.get("username")
            if code not in rooms:
                await websocket.send(json.dumps({"type": "error", "message": "Room no longer exists."}))
                return
            room = rooms[code]
            slot = 0 if player_symbol == "X" else 1
            room["players"][slot]              = websocket
            room["symbols"][websocket]         = player_symbol
            room["player_usernames"][player_symbol] = username
            if username:
                online_sockets[username] = websocket
            session_token = token
            print(f"Player {player_symbol} ({username}) rejoined room {code}")
            await websocket.send(json.dumps({"type": "rejoined", "symbol": player_symbol, "code": code}))
            await broadcast_board(room)

        else:
            await websocket.send(json.dumps({"type": "error", "message": "Invalid action."}))
            return

        # ── re-fetch room ─────────────────────────────────────────────────────
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

                    if inner_action == "add_friend":
                        if not username:
                            await websocket.send(json.dumps({"type": "error", "message": "Not logged in."}))
                            continue
                        friend_name = (parsed.get("username") or "").strip()
                        if not friend_name:
                            await websocket.send(json.dumps({"type": "add_friend_result", "success": False, "message": "Enter a username."}))
                            continue
                        if friend_name == username:
                            await websocket.send(json.dumps({"type": "add_friend_result", "success": False, "message": "That's you!"}))
                            continue
                        if friend_name not in users:
                            await websocket.send(json.dumps({"type": "add_friend_result", "success": False, "message": "User not found."}))
                            continue
                        udata = users[username]
                        if friend_name in udata.get("friends", []):
                            await websocket.send(json.dumps({"type": "add_friend_result", "success": False, "message": "Already friends."}))
                            continue
                        udata.setdefault("friends", []).append(friend_name)
                        save_users()
                        status = get_status(friend_name)
                        await websocket.send(json.dumps({
                            "type": "add_friend_result", "success": True,
                            "friend": friend_name, "status": status
                        }))
                        continue

                    if inner_action == "remove_friend":
                        if not username:
                            continue
                        friend_name = (parsed.get("username") or "").strip()
                        udata = users.get(username, {})
                        udata["friends"] = [f for f in udata.get("friends", []) if f != friend_name]
                        save_users()
                        await websocket.send(json.dumps({"type": "remove_friend_result", "success": True, "friend": friend_name}))
                        continue

                    if inner_action == "send_invite":
                        if not username:
                            continue
                        target = parsed.get("to", "")
                        target_ws = online_sockets.get(target)
                        if not target_ws:
                            await websocket.send(json.dumps({"type": "invite_result", "success": False, "message": f"{target} is offline."}))
                            continue
                        try:
                            await target_ws.send(json.dumps({"type": "incoming_invite", "from": username}))
                            await websocket.send(json.dumps({"type": "invite_result", "success": True, "to": target}))
                        except Exception:
                            await websocket.send(json.dumps({"type": "invite_result", "success": False, "message": f"Could not reach {target}."}))
                        continue

                    if inner_action == "accept_invite":
                        if not username:
                            continue
                        inviter = parsed.get("from", "")
                        inviter_ws = online_sockets.get(inviter)
                        if inviter_ws:
                            try:
                                # FIX #3: tell the inviter who accepted so they can host
                                # and push the room code back to the accepter.
                                await inviter_ws.send(json.dumps({"type": "invite_accepted", "by": username}))
                            except Exception:
                                pass
                        continue

                    # FIX #3: inviter relays the room code to the accepter after hosting
                    if inner_action == "send_room_code":
                        if not username:
                            continue
                        target    = parsed.get("to", "")
                        room_code = parsed.get("code", "")
                        target_ws = online_sockets.get(target)
                        if target_ws and room_code:
                            try:
                                await target_ws.send(json.dumps({"type": "room_code_for_invitee", "code": room_code}))
                            except Exception:
                                pass
                        continue

                    if inner_action == "decline_invite":
                        if not username:
                            continue
                        inviter = parsed.get("from", "")
                        inviter_ws = online_sockets.get(inviter)
                        if inviter_ws:
                            try:
                                await inviter_ws.send(json.dumps({"type": "invite_declined", "by": username}))
                            except Exception:
                                pass
                        continue

                    if inner_action == "rematch":
                        if room is None: continue
                        room["rematch_votes"].add(player_symbol)
                        if len(room["rematch_votes"]) == 2:
                            room["game"].reset()
                            room["placed"]      = 0
                            room["total_moves"] = 0
                            room["turn"]        = "X"
                            room["rematch_votes"] = set()
                            await broadcast(room, {"type": "rematch_start"})
                            await broadcast_board(room)
                        else:
                            await broadcast(room, {"type": "rematch_waiting"})
                        continue

            except (ValueError, TypeError):
                pass

            if room is None or player_symbol != room["turn"]:
                continue

            # PLACEMENT
            if room["placed"] < 6:
                try:
                    pos = int(json.loads(message))
                    if room["game"].place_piece(pos, player_symbol):
                        room["placed"]      += 1
                        room["total_moves"] += 1
                        if room["game"].check_win(player_symbol):
                            loser = "O" if player_symbol == "X" else "X"
                            await broadcast(room, {"type": "win", "player": player_symbol})
                            await save_game(player_symbol, loser, room["total_moves"])
                            for uname in room.get("player_usernames", {}).values():
                                if uname and uname in online_sockets:
                                    await notify_friends_of_status_change(uname)
                            del rooms[code]
                            return
                        room["turn"] = "O" if room["turn"] == "X" else "X"
                        await broadcast_board(room)
                except (ValueError, TypeError, KeyError):
                    pass
                continue

            # MOVEMENT
            try:
                move = json.loads(message)
                if room["game"].move_piece(move["from"], move["to"], player_symbol):
                    room["total_moves"] += 1
                    if room["game"].check_win(player_symbol):
                        loser = "O" if player_symbol == "X" else "X"
                        await broadcast(room, {"type": "win", "player": player_symbol})
                        await save_game(player_symbol, loser, room["total_moves"])
                        for uname in room.get("player_usernames", {}).values():
                            if uname and uname in online_sockets:
                                await notify_friends_of_status_change(uname)
                        del rooms[code]
                        return
                    room["turn"] = "O" if room["turn"] == "X" else "X"
                    await broadcast_board(room)
            except (ValueError, KeyError, json.JSONDecodeError):
                pass

    except websockets.exceptions.ConnectionClosed:
        print(f"Player {player_symbol} ({username}) disconnected from room {code}")
        if username and username in online_sockets and online_sockets[username] is websocket:
            del online_sockets[username]
            await notify_friends_of_status_change(username)
        if code and code in rooms:
            room = rooms[code]
            slot = 0 if player_symbol == "X" else 1
            room["players"][slot] = None
            if player_symbol:
                room["player_usernames"][player_symbol] = None
            for p in room["players"]:
                if p is not None:
                    try:
                        await p.send(json.dumps({
                            "type":    "opponent_disconnected",
                            "message": "Opponent disconnected. Waiting 60s for rejoin..."
                        }))
                    except Exception:
                        pass
            # FIX #6: background task — handler exits immediately
            asyncio.create_task(_room_cleanup_task(code, slot))

    except asyncio.TimeoutError:
        print("Connection timed out")


# ── HTTP routes ───────────────────────────────────────────────────────────────
async def index(request):
    base = os.path.dirname(os.path.abspath(__file__))
    return web.FileResponse(os.path.join(base, '..', 'frontend', 'index.html'))

async def leaderboard(request):
    return web.json_response(await get_leaderboard())

async def health(request):
    return web.json_response({"status": "ok", "rooms": len(rooms), "users_online": len(online_sockets)})


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    load_users()
    await init_db()

    # FIX #14: no hardcoded PORT — Render injects it automatically
    port = int(os.environ.get("PORT", 5000))

    async def websocket_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        class WSAdapter:
            async def recv(self):
                # FIX #10: guard against CLOSE/PING/ERROR message types
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
    app.router.add_get('/api/leaderboard', leaderboard)
    app.router.add_get('/api/health',      health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Morix server running on port {port}")
    print(f"  WebSocket:   ws://0.0.0.0:{port}/ws")
    print(f"  Leaderboard: http://0.0.0.0:{port}/api/leaderboard")

    async def cleanup_loop():
        while True:
            await asyncio.sleep(600)
            purge_expired_sessions()

    asyncio.create_task(cleanup_loop())
    await asyncio.Future()


asyncio.run(main())
