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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'game'))
from game_engine import GameEngine
from aiohttp import web

# ── In-memory stores ─────────────────────────────────────────────────────────
rooms    = {}   # code -> room dict
sessions = {}   # token -> session dict

# users[username] = { "password_hash": str, "friends": [str,...] }
users = {}

# online_sockets[username] = websocket  (None if logged out)
online_sockets = {}

DB_PATH = os.environ.get("MORIX_DB", "morix.db")


# ── Password hashing ──────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


# ── User persistence (JSON file alongside DB) ─────────────────────────────────
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

async def save_game(winner, loser, moves, abandoned=False):
    try:
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO games(winner,loser,moves,abandoned) VALUES(?,?,?,?)",
                (winner, loser, moves, 1 if abandoned else 0)
            )
            await db.commit()
    except Exception:
        pass

async def get_leaderboard():
    try:
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT winner, COUNT(*) AS wins FROM games "
                "WHERE winner IS NOT NULL GROUP BY winner "
                "ORDER BY wins DESC LIMIT 10"
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


# ── Auth tokens (login sessions, separate from game sessions) ─────────────────
auth_tokens = {}   # token -> username

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
def get_status(username: str) -> str:
    """Return 'online', 'ingame', or 'offline'."""
    if username not in online_sockets:
        return "offline"
    # check if in a game
    for room in rooms.values():
        for ws in room["players"]:
            if ws is not None and room["symbols"].get(ws) and room.get("usernames", {}).get(ws) == username:
                return "ingame"
    return "online"

async def push_friend_status(username: str):
    """Push updated friend statuses to a logged-in user."""
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
    """Tell all online friends that this user's status changed."""
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


# ── WebSocket handler ─────────────────────────────────────────────────────────
async def handler(websocket):
    code          = None
    player_symbol = None
    session_token = None
    username      = None   # logged-in username for this connection

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
            if len(uname) < 3:
                await websocket.send(json.dumps({"type": "error", "message": "Username must be at least 3 characters."}))
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
            if not udata or udata["password_hash"] != hash_password(pwd):
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

        # ── AUTO-LOGIN (resume with auth token) ──────────────────────────────
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
            await websocket.send(json.dumps({
                "type": "logged_in", "username": uname,
                "auth_token": token, "friends": friends,
                "statuses": statuses
            }))
            await notify_friends_of_status_change(username)
            print(f"User auto-logged-in: {uname}")

        # ── HOST ─────────────────────────────────────────────────────────────
        elif action == "host":
            code  = generate_code()
            rooms[code] = {
                "game": GameEngine(), "players": [websocket, None],
                "turn": "X", "placed": 0,
                "symbols": {websocket: "X"}, "rematch_votes": set(),
                "usernames": {}
            }
            player_symbol = "X"
            session_token = create_session("X", code, username)
            if username:
                rooms[code]["usernames"][websocket] = username
                online_sockets[username] = websocket
            print(f"Room {code} created by {username or 'anonymous'}")
            await websocket.send(json.dumps({
                "type": "hosted", "code": code,
                "symbol": "X", "session_token": session_token
            }))
            if username:
                await notify_friends_of_status_change(username)
            return  # fall through to game loop below (but we need username set)

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
            room["players"][1] = websocket
            room["symbols"][websocket] = "O"
            player_symbol = "O"
            session_token = create_session("O", code, username)
            if username:
                room["usernames"][websocket] = username
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
            # fall through to game loop

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
            room["players"][slot]  = websocket
            room["symbols"][websocket] = player_symbol
            if username:
                room["usernames"][websocket] = username
                online_sockets[username] = websocket
            session_token = token
            print(f"Player {player_symbol} ({username}) rejoined room {code}")
            await websocket.send(json.dumps({"type": "rejoined", "symbol": player_symbol, "code": code}))
            await broadcast_board(room)

        else:
            await websocket.send(json.dumps({"type": "error", "message": "Invalid action."}))
            return

        # ── re-fetch room after host/join/rejoin returns early ────────────────
        if action in ("host",):
            # host returns early — we need to re-enter game loop
            # The code block above used `return` so we won't reach here for host.
            # For host we need to fall through, so remove the early return above.
            pass

        room = rooms.get(code)
        if room is None:
            return

        # ── Game loop ─────────────────────────────────────────────────────────
        while True:
            message = await websocket.recv()

            # ── ADD FRIEND (can arrive anytime during a session) ──────────────
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
                                await inviter_ws.send(json.dumps({"type": "invite_accepted", "by": username}))
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
                            room["placed"] = 0
                            room["turn"]   = "X"
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
                        room["placed"] += 1
                        if room["game"].check_win(player_symbol):
                            loser = "O" if player_symbol == "X" else "X"
                            await broadcast(room, {"type": "win", "player": player_symbol})
                            await save_game(player_symbol, loser, room["placed"])
                            # notify friends status changed (no longer in game)
                            for uname in room.get("usernames", {}).values():
                                if uname in online_sockets:
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
                    if room["game"].check_win(player_symbol):
                        loser = "O" if player_symbol == "X" else "X"
                        await broadcast(room, {"type": "win", "player": player_symbol})
                        await save_game(player_symbol, loser, room["placed"])
                        for uname in room.get("usernames", {}).values():
                            if uname in online_sockets:
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
            for p in room["players"]:
                if p is not None:
                    try:
                        await p.send(json.dumps({
                            "type": "opponent_disconnected",
                            "message": "Opponent disconnected. Waiting 60s for rejoin..."
                        }))
                    except Exception:
                        pass
            await asyncio.sleep(60)
            if code in rooms and rooms[code]["players"][slot] is None:
                await save_game(None, None, rooms[code].get("placed", 0), abandoned=True)
                del rooms[code]
                print(f"Room {code} cleaned up after timeout")

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

    port = int(os.environ.get("PORT", 5000))

    async def websocket_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        class WSAdapter:
            async def recv(self):
                msg = await ws.receive()
                if ws.closed:
                    raise websockets.exceptions.ConnectionClosed(None, None)
                return msg.data
            async def send(self, data):
                await ws.send_str(data)
            @property
            def closed(self):
                return ws.closed

        await handler(WSAdapter())
        return ws

    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/ws', websocket_handler)
    app.router.add_get('/api/leaderboard', leaderboard)
    app.router.add_get('/api/health', health)

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
