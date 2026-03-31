import socket
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'game'))
from game_engine import GameEngine

HOST = "127.0.0.1"
PORT = 5001  # Different port to avoid clash with ws_server.py

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind((HOST, PORT))
server.listen()

print("3 Men's Morris TCP Server (legacy CLI mode)")
print(f"Listening on {HOST}:{PORT}")
print("Waiting for players...")

game = GameEngine()

conn1, addr1 = server.accept()
print("Player 1 (X) connected:", addr1)

conn2, addr2 = server.accept()
print("Player 2 (O) connected:", addr2)

print("Both players connected. Game starting...")
conn1.send("Game starting. You are Player X\n".encode())
conn2.send("Game starting. You are Player O\n".encode())


def safe_send(conn, msg):
    """Send a message, ignoring broken-pipe errors."""
    try:
        conn.send(msg.encode() if isinstance(msg, str) else msg)
    except (BrokenPipeError, OSError):
        pass


def send_board():
    board_text = game.board_to_string()
    safe_send(conn1, board_text + "\n")
    safe_send(conn2, board_text + "\n")


def placement_phase():
    """Returns True if the game ended (win or disconnect) during placement."""
    for i in range(3):

        # Player X
        while True:
            safe_send(conn1, "Place piece (1-9): ")
            try:
                move = conn1.recv(1024).decode().strip()
            except (ConnectionResetError, OSError):
                print("Player X disconnected")
                safe_send(conn2, "Opponent disconnected. Game over.\n")
                return True

            if not move:
                print("Player X disconnected")
                safe_send(conn2, "Opponent disconnected. Game over.\n")
                return True

            try:
                pos = int(move)
            except ValueError:
                safe_send(conn1, "Enter a number between 1 and 9\n")
                continue

            if game.place_piece(pos, "X"):
                send_board()
                if game.check_win("X"):
                    safe_send(conn1, "You win!\n")
                    safe_send(conn2, "You lose!\n")
                    return True
                break
            else:
                safe_send(conn1, "Invalid move. Try again.\n")

        # Player O
        while True:
            safe_send(conn2, "Place piece (1-9): ")
            try:
                move = conn2.recv(1024).decode().strip()
            except (ConnectionResetError, OSError):
                print("Player O disconnected")
                safe_send(conn1, "Opponent disconnected. Game over.\n")
                return True

            if not move:
                print("Player O disconnected")
                safe_send(conn1, "Opponent disconnected. Game over.\n")
                return True

            try:
                pos = int(move)
            except ValueError:
                safe_send(conn2, "Enter a number between 1 and 9\n")
                continue

            if game.place_piece(pos, "O"):
                send_board()
                if game.check_win("O"):
                    safe_send(conn2, "You win!\n")
                    safe_send(conn1, "You lose!\n")
                    return True
                break
            else:
                safe_send(conn2, "Invalid move. Try again.\n")

    return False


def movement_phase():
    """Run the movement phase until a win or disconnect."""
    while True:

        # Player X
        while True:
            safe_send(conn1, "Move piece (from to): ")
            try:
                move = conn1.recv(1024).decode().strip()
            except (ConnectionResetError, OSError):
                print("Player X disconnected")
                safe_send(conn2, "Opponent disconnected. Game over.\n")
                return

            if not move:
                print("Player X disconnected")
                safe_send(conn2, "Opponent disconnected. Game over.\n")
                return

            try:
                from_pos, to_pos = map(int, move.split())
            except ValueError:
                safe_send(conn1, "Invalid format. Use: from to (e.g. 5 2)\n")
                continue

            if game.move_piece(from_pos, to_pos, "X"):
                send_board()
                if game.check_win("X"):
                    safe_send(conn1, "You win!\n")
                    safe_send(conn2, "You lose!\n")
                    return
                break
            else:
                safe_send(conn1, "Invalid move. Try again.\n")

        # Player O
        while True:
            safe_send(conn2, "Move piece (from to): ")
            try:
                move = conn2.recv(1024).decode().strip()
            except (ConnectionResetError, OSError):
                print("Player O disconnected")
                safe_send(conn1, "Opponent disconnected. Game over.\n")
                return

            if not move:
                print("Player O disconnected")
                safe_send(conn1, "Opponent disconnected. Game over.\n")
                return

            try:
                from_pos, to_pos = map(int, move.split())
            except ValueError:
                safe_send(conn2, "Invalid format. Use: from to (e.g. 5 2)\n")
                continue

            if game.move_piece(from_pos, to_pos, "O"):
                send_board()
                if game.check_win("O"):
                    safe_send(conn2, "You win!\n")
                    safe_send(conn1, "You lose!\n")
                    return
                break
            else:
                safe_send(conn2, "Invalid move. Try again.\n")


# ── Run the game ──────────────────────────────────────────────────────────────
send_board()
game_over = placement_phase()

if not game_over:
    safe_send(conn1, "--- Movement phase begins ---\n")
    safe_send(conn2, "--- Movement phase begins ---\n")
    movement_phase()

conn1.close()
conn2.close()
server.close()
print("Game ended. Server closed.")
