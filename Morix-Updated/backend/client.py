import socket
import sys

HOST = "127.0.0.1"
PORT = 5001  # matches server.py


def main():
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect((HOST, PORT))
        print(f"Connected to Morix server at {HOST}:{PORT}")
    except ConnectionRefusedError:
        print(f"Could not connect to {HOST}:{PORT} — is server.py running?")
        sys.exit(1)

    try:
        while True:
            message = client.recv(1024).decode()
            if not message:
                print("Server closed the connection.")
                break

            print(message, end="")

            # Prompt the user whenever the server expects input
            if any(p in message for p in ("Place piece", "Move piece")):
                move = input()
                client.send((move + "\n").encode())

    except (ConnectionResetError, OSError):
        print("\nConnection lost.")
    finally:
        client.close()


if __name__ == "__main__":
    main()
