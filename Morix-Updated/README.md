# Morix — Online 3 Men's Morris

Real-time multiplayer implementation of the classic strategy board game **Three Men's Morris**.  
Two players host or join a game using a 4-digit room code and play online in real time.

---

## What's New (v2)

| Area | Change |
|---|---|
| **Session management** | Session tokens issued on host/join; stored in `sessionStorage` for reconnect |
| **Reconnect** | Players can rejoin after a network drop within 60 seconds |
| **Rematch** | Both players can vote for a rematch without reloading |
| **DB persistence** | Game results saved to SQLite via `aiosqlite` (non-blocking) |
| **Leaderboard API** | `GET /api/leaderboard` returns top-10 winners; shown in-page via Ajax |
| **Health endpoint** | `GET /api/health` returns server status and active room count |
| **Disconnect UX** | Opponent disconnection shown with 60s wait rather than immediate game end |
| **Error handling** | `safe_send()` in legacy server; WSAdapter handles closed sockets cleanly |

---

## Technologies

- Python 3.10+
- `aiohttp` — HTTP + WebSocket server
- `aiosqlite` — async SQLite for game persistence
- `websockets` — WebSocket protocol helpers
- JavaScript / HTML / CSS — frontend (no framework)

---

## Project Structure

```
Morix/
├── backend/
│   ├── ws_server.py      ← Main WebSocket + HTTP server (aiohttp)
│   ├── server.py         ← Legacy raw TCP server (CLI mode)
│   └── client.py         ← CLI client for legacy server
├── frontend/
│   └── index.html        ← Browser game UI
├── game/
│   └── game_engine.py    ← Board logic (placement, movement, win detection)
├── requirements.txt
├── render.yaml
└── README.md
```

---

## How to Run Locally

**Install dependencies:**
```bash
pip install -r requirements.txt
```

**Start the WebSocket server:**
```bash
python backend/ws_server.py
```

**Open in browser:**  
Go to `http://localhost:5000` — host a game in one tab, join from another.

**Leaderboard API:**  
```
GET http://localhost:5000/api/leaderboard
GET http://localhost:5000/api/health
```

---

## Game Rules

Three Men's Morris is played on a **3×3 board**.

1. Each player has **three pieces**.
2. Players alternate placing pieces on empty positions (placement phase).
3. Once all 6 pieces are placed, players move pieces to **adjacent positions**.
4. The first player to form a **straight line of three pieces** wins (row, column, or diagonal).

---

## Message Protocol

### Client → Server

| Message | Phase | Description |
|---|---|---|
| `{"action":"host"}` | Lobby | Create a new room |
| `{"action":"join","code":"1234"}` | Lobby | Join an existing room |
| `{"action":"rejoin","token":"..."}` | Any | Rejoin after disconnect |
| `4` (integer) | Placement | Place piece at position 1–9 |
| `{"from":5,"to":2}` | Movement | Move piece |
| `{"action":"rematch"}` | Post-game | Request a rematch |

### Server → Client

| Message | Description |
|---|---|
| `{"type":"hosted","code":"7842","symbol":"X","session_token":"..."}` | Room created |
| `{"type":"joined","symbol":"O","session_token":"..."}` | Joined room |
| `{"type":"rejoined","symbol":"X","code":"7842"}` | Reconnected |
| `{"type":"start"}` | Both players ready |
| `{"type":"board","board":[...],"turn":"X"}` | Board state update |
| `{"type":"win","player":"X"}` | Game over |
| `{"type":"rematch_waiting"}` | One player voted rematch |
| `{"type":"rematch_start"}` | Both voted — board reset |
| `{"type":"opponent_disconnected","message":"..."}` | Opponent dropped |
| `{"type":"error","message":"..."}` | Error |

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `5000` | Server listen port |
| `MORIX_DB` | `morix.db` | SQLite database file path |

---

## Deployment (Render.com)

The included `render.yaml` configures a Render starter web service.  
> **Note:** Render's free tier has ephemeral disk — SQLite data is lost on restart.  
> For persistent leaderboards, provision a **Render PostgreSQL** database and switch to `asyncpg`.
