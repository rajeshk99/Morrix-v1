class GameEngine:
    def __init__(self):
        self.board = [None] * 9

    winning_combinations = [
        [0, 1, 2],
        [3, 4, 5],
        [6, 7, 8],
        [0, 3, 6],
        [1, 4, 7],
        [2, 5, 8],
        [0, 4, 8],
        [2, 4, 6]
    ]

    # 1-indexed adjacency map (positions 1-9)
    adjacent_positions = {
        1: [2, 4, 5],
        2: [1, 3, 5],
        3: [2, 6, 5],
        4: [1, 7, 5],
        5: [1, 2, 3, 4, 6, 7, 8, 9],
        6: [3, 9, 5],
        7: [4, 8, 5],
        8: [7, 9, 5],
        9: [6, 8, 5]
    }

    def reset(self):
        """Reset the board for a rematch."""
        self.board = [None] * 9

    def board_to_string(self):
        rows = []
        for i in range(3):
            row = []
            for j in range(3):
                value = self.board[3 * i + j]
                row.append(value if value else " ")
            rows.append(" | ".join(row))
        return "\n---------\n".join(rows)

    def display_board(self):
        for i in range(3):
            for j in range(3):
                value = self.board[3 * i + j]
                print(value if value else " ", end=" ")
                if j < 2:
                    print("|", end=" ")
            print()
            if i < 2:
                print("---------")

    def place_piece(self, position, player):
        """Place a piece at position (1-9). Returns True on success."""
        if position < 1 or position > 9:
            return False
        if self.board[position - 1] is None:
            self.board[position - 1] = player
            return True
        return False

    def check_win(self, player):
        """Return True if player has three in a row."""
        for combo in self.winning_combinations:
            a, b, c = combo
            if (self.board[a] == player and
                    self.board[b] == player and
                    self.board[c] == player):
                return True
        return False

    def move_piece(self, from_pos, to_pos, player):
        """Move a piece from from_pos to to_pos (both 1-indexed). Returns True on success."""
        if from_pos < 1 or from_pos > 9 or to_pos < 1 or to_pos > 9:
            return False
        if self.board[from_pos - 1] != player:
            return False
        if self.board[to_pos - 1] is not None:
            return False
        if to_pos not in self.adjacent_positions[from_pos]:
            return False
        self.board[from_pos - 1] = None
        self.board[to_pos - 1] = player
        return True

    def get_pieces(self, player):
        """Return list of 1-indexed positions occupied by player."""
        return [i + 1 for i, v in enumerate(self.board) if v == player]

    def play_game(self):
        """Local CLI game loop."""
        for i in range(6):
            player = "X" if i % 2 == 0 else "O"
            print(f"Player {player} turn")
            while True:
                position = int(input("Enter position: "))
                if self.place_piece(position, player):
                    break
                else:
                    print("Try again")
            self.display_board()
            if self.check_win(player):
                print(f"Player {player} wins!")
                return

        current_player = "O" if i % 2 == 0 else "X"
        while True:
            self.display_board()
            print(f"Player {current_player} move")
            from_pos = int(input("Move from: "))
            to_pos = int(input("Move to: "))
            if self.move_piece(from_pos, to_pos, current_player):
                if self.check_win(current_player):
                    self.display_board()
                    print(f"Player {current_player} wins!")
                    break
                current_player = "O" if current_player == "X" else "X"
            else:
                print("Invalid move, try again")
