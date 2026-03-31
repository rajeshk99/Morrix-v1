import asyncio
import websockets
import json
import random
import string
import os
import time
import secrets
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'game'))
from game_engine import GameEngine
from aiohttp import web

# ── In-memory stores ────────────────────────────────────────────────────────
# rooms[code] = {
#   "game": GameEngine, "players": [ws|None, ws|None],
#   "turn": "X", "placed": 0,
#   "symbols": {ws: "X"|"O"}, "rematch_votes": set()
# }
rooms = {}

# sessions[token] = {"symbol": "X"|"O", "room_code": str, "expires": float}
sessions = {}

DB_PATH = os.environ.get("MORIX_DB", "morix.db")


# ── Database helpers ─────────────────────────────────────────────────────────
async def init_db():
    """Create tables if they don't exist."""
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
    """Persist a finished game result. Silently skips if aiosqlite unavailable."""
    try:
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO games(winner, loser, moves, abandoned) VALUES(?,?,?,?)",
                (winner, loser, moves, 1 if abandoned else 0)
            )
            await db.commit()
    except Exception:
        pass  # DB write failure must never crash the game


async def get_leaderboard():
    """Return top-10 winners."""
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


# ── Session helpers ──────────────────────────────────────────────────────────
def create_session(symbol, room_code):
    """Create a 32-char hex session token tied to a player."""
    token = secrets.token_hex(16)
    sessions[token] = {
        "symbol":    symbol,
        "room_code": room_code,
        "expires":   time.time() + 3600  # 1 hour
    }
    return token


def validate_session(token):
    """Return session dict or None if invalid/expired."""
    s = sessions.get(token)
    if not s or s["expires"] < time.time():
        sessions.pop(token, None)
        return None
    return s


def purge_expired_sessions():
    """Remove stale sessions (called periodically)."""
    now = time.time()
    expired = [t for t, s in sessions.items() if s["expires"] < now]
    for t in expired:
        del sessions[t]


# ── Room helpers ─────────────────────────────────────────────────────────────
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
            try:
                await p.send(msg)
            except Exception:
                pass


async def broadcast(room, payload):
    msg = json.dumps(payload)
    for p in room["players"]:
        if p is not None:
            try:
                await p.send(msg)
            except Exception:
                pass


# ── WebSocket handler ─────────────────────────────────────────────────────────
async def handler(websocket):
    code = None
    player_symbol = None
    session_token = None

    try:
        # ── Handshake (first message) ────────────────────────────────────────
        raw = await asyncio.wait_for(websocket.recv(), timeout=30)
        msg = json.loads(raw)
        action = msg.get("action")

        # HOST
        if action == "host":
            code = generate_code()
            rooms[code] = {
                "game":          GameEngine(),
                "players":       [websocket, None],
                "turn":          "X",
                "placed":        0,
                "symbols":       {websocket: "X"},
                "rematch_votes": set()
            }
            player_symbol = "X"
            session_token = create_session("X", code)
            print(f"Room {code} created")
            await websocket.send(json.dumps({
                "type": "hosted", "code": code,
                "symbol": "X", "session_token": session_token
            }))
            await websocket.send(json.dumps({"type": "wait"}))

        # JOIN
        elif action == "join":
            code = msg.get("code", "").strip()
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
            session_token = create_session("O", code)
            print(f"Player O joined room {code}")
            await websocket.send(json.dumps({
                "type": "joined", "symbol": "O",
                "session_token": session_token
            }))
            for p in room["players"]:
                if p:
                    await p.send(json.dumps({"type": "start"}))
            await broadcast_board(room)

        # REJOIN (reconnect with session token)
        elif action == "rejoin":
            token = msg.get("token", "")
            sess = validate_session(token)
            if not sess:
                await websocket.send(json.dumps({"type": "error", "message": "Session expired. Please start a new game."}))
                return
            code = sess["room_code"]
            player_symbol = sess["symbol"]
            if code not in rooms:
                await websocket.send(json.dumps({"type": "error", "message": "Room no longer exists."}))
                return
            room = rooms[code]
            slot = 0 if player_symbol == "X" else 1
            room["players"][slot] = websocket
            room["symbols"][websocket] = player_symbol
            session_token = token
            print(f"Player {player_symbol} rejoined room {code}")
            await websocket.send(json.dumps({"type": "rejoined", "symbol": player_symbol, "code": code}))
            await broadcast_board(room)

        else:
            await websocket.send(json.dumps({"type": "error", "message": "Invalid action."}))
            return

        room = rooms[code]

        # ── Game loop ────────────────────────────────────────────────────────
        while True:
            message = await websocket.recv()

            # Ignore out-of-turn moves
            if player_symbol != room["turn"]:
                continue

            # REMATCH request
            try:
                parsed = json.loads(message)
                if isinstance(parsed, dict) and parsed.get("action") == "rematch":
                    room["rematch_votes"].add(player_symbol)
                    if len(room["rematch_votes"]) == 2:
                        room["game"].reset()
                        room["placed"] = 0
                        room["turn"] = "X"
                        room["rematch_votes"] = set()
                        await broadcast(room, {"type": "rematch_start"})
                        await broadcast_board(room)
                    else:
                        await broadcast(room, {"type": "rematch_waiting"})
                    continue
            except (ValueError, TypeError):
                pass

            # PLACEMENT phase (placed < 6)
            if room["placed"] < 6:
                try:
                    pos = int(json.loads(message))
                    if room["game"].place_piece(pos, player_symbol):
                        room["placed"] += 1
                        if room["game"].check_win(player_symbol):
                            loser = "O" if player_symbol == "X" else "X"
                            await broadcast(room, {"type": "win", "player": player_symbol})
                            await save_game(player_symbol, loser, room["placed"])
                            del rooms[code]
                            return
                        room["turn"] = "O" if room["turn"] == "X" else "X"
                        await broadcast_board(room)
                except (ValueError, TypeError, KeyError) as e:
                    print(f"Placement error in room {code}: {e}")
                continue

            # MOVEMENT phase
            try:
                move = json.loads(message)
                from_pos = move["from"]
                to_pos = move["to"]
                if room["game"].move_piece(from_pos, to_pos, player_symbol):
                    if room["game"].check_win(player_symbol):
                        loser = "O" if player_symbol == "X" else "X"
                        await broadcast(room, {"type": "win", "player": player_symbol})
                        await save_game(player_symbol, loser, room["placed"])
                        del rooms[code]
                        return
                    room["turn"] = "O" if room["turn"] == "X" else "X"
                    await broadcast_board(room)
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                print(f"Movement error in room {code}: {e}")

    except websockets.exceptions.ConnectionClosed:
        print(f"Player {player_symbol} disconnected from room {code}")
        if code and code in rooms:
            room = rooms[code]
            slot = 0 if player_symbol == "X" else 1
            room["players"][slot] = None  # keep room alive for reconnect
            for p in room["players"]:
                if p is not None and p != websocket:
                    try:
                        await p.send(json.dumps({
                            "type": "opponent_disconnected",
                            "message": "Opponent disconnected. Waiting 60s for rejoin..."
                        }))
                    except Exception:
                        pass
            # Clean up room after 60 seconds if player doesn't rejoin
            await asyncio.sleep(60)
            if code in rooms:
                r = rooms[code]
                if r["players"][slot] is None:
                    await save_game(None, None, r.get("placed", 0), abandoned=True)
                    del rooms[code]
                    print(f"Room {code} cleaned up after disconnect timeout")

    except asyncio.TimeoutError:
        print("Connection timed out waiting for host/join message")


# ── HTTP routes ───────────────────────────────────────────────────────────────
async def index(request):
    base = os.path.dirname(os.path.abspath(__file__))
    return web.FileResponse(os.path.join(base, '..', 'frontend', 'index.html'))


async def leaderboard(request):
    data = await get_leaderboard()
    return web.json_response(data)


async def health(request):
    return web.json_response({"status": "ok", "rooms": len(rooms)})


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
    print(f"  WebSocket : ws://0.0.0.0:{port}/ws")
    print(f"  Leaderboard: http://0.0.0.0:{port}/api/leaderboard")

    # Periodic session cleanup every 10 minutes
    async def cleanup_loop():
        while True:
            await asyncio.sleep(600)
            purge_expired_sessions()

    asyncio.create_task(cleanup_loop())
    await asyncio.Future()


asyncio.run(main())
