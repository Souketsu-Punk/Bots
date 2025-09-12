import websocket
import json
import threading
import time
import csv
import random
from datetime import datetime

DERIV_API_TOKEN = "JDKYPoc3aLSHjiY"
APP_ID = "96437"

def last_digit_from_quote(q):
    """
    Robustly get the last decimal digit from a numeric tick quote.
    Works even if q is float/int by scanning the string from the end
    and picking the first digit encountered.
    """
    s = f"{q}"
    for ch in reversed(s):
        if ch.isdigit():
            return int(ch)
    # fallback (shouldn't happen), default to 0
    return 0

class DynamicOddEvenBot:
    def __init__(self, token):
        self.token = token
        self.balance = None
        self.stake = 1.0
        self.loss_count = 0
        self.ws = None
        self.lock = threading.Lock()

        # tick storage
        self.ticks = []

        # win/loss tracking
        self.total_trades = 0
        self.total_wins = 0
        self.total_losses = 0

        # logging
        self.csv_file = open("trades_log.csv", "a", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        if self.csv_file.tell() == 0:
            self.csv_writer.writerow(["Time", "Contract", "Result", "Profit", "Balance", "StrikeRate"])

    def connect(self):
        self.ws = websocket.WebSocketApp(
            f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
        )
        threading.Thread(target=self.ws.run_forever, daemon=True).start()

    def on_open(self, ws):
        print("[WS] open")
        self.send({"authorize": self.token})

    def on_message(self, ws, msg):
        data = json.loads(msg)

        if "error" in data and data["error"]:
            print("[WS][ERROR]", data["error"])
            return

        if data.get("msg_type") == "authorize":
            print("[WS] authorized")
            try:
                self.balance = float(data["authorize"]["balance"])
            except Exception:
                self.balance = None
            print(f"[START] start balance: {self.balance}")
            # get a block of history for warmup
            self.send({
                "ticks_history": "R_10",
                "count": 1000,
                "end": "latest",
                "style": "ticks"
            })
            # subscribe to live ticks (history is NOT a live subscription)
            self.send({"ticks": "R_10", "subscribe": 1})

        elif data.get("msg_type") == "history":
            # prices are floats; do NOT subscript floats. Extract last digit safely.
            prices = data.get("history", {}).get("prices", []) or []
            self.ticks = [last_digit_from_quote(p) for p in prices]
            print("[WS] warmup ticks loaded:", len(self.ticks))
            print("[BOT] warmup complete, starting decision loop.")
            threading.Thread(target=self.decision_loop, daemon=True).start()

        elif data.get("msg_type") == "tick":
            quote = data.get("tick", {}).get("quote")
            if quote is not None:
                self.ticks.append(last_digit_from_quote(quote))
                if len(self.ticks) > 1000:
                    self.ticks.pop(0)

        elif data.get("msg_type") == "proposal":
            # buy immediately with the quoted proposal id
            prop = data.get("proposal", {})
            pid = prop.get("id")
            if pid:
                self.send({"buy": pid, "price": self.stake})

        elif data.get("msg_type") == "buy":
            contract_id = data.get("buy", {}).get("contract_id")
            if contract_id:
                print(f"[BOT] Bought contract {contract_id}, stake={self.stake}")
                self.send({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1})

        elif data.get("msg_type") == "proposal_open_contract":
            poc = data.get("proposal_open_contract", {})
            if poc.get("is_sold"):
                try:
                    profit = float(poc.get("profit", 0.0))
                except Exception:
                    profit = 0.0
                if self.balance is not None:
                    self.balance += profit

                result = "WIN" if profit > 0 else "LOSS"
                if profit > 0:
                    self.total_wins += 1
                    self.loss_count = 0
                else:
                    self.total_losses += 1
                    self.loss_count += 1

                self.total_trades += 1
                strike_rate = (self.total_wins / self.total_trades) * 100 if self.total_trades > 0 else 0.0

                print(f"[BOT] {result} Profit={profit:.2f}, Balance={self.balance:.2f if self.balance is not None else 0.0}, SR={strike_rate:.2f}%")

                self.csv_writer.writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "ODD/EVEN",
                    result,
                    f"{profit:.2f}",
                    f"{self.balance:.2f}" if self.balance is not None else "",
                    f"{strike_rate:.2f}%"
                ])
                self.csv_file.flush()

                # stake adjustment (unchanged logic)
                if profit > 0:
                    self.stake = 1.0
                else:
                    self.stake = min(10.0, self.stake * 1.5)

    def on_error(self, ws, err):
        print("[WS][ERROR]", err)

    def on_close(self, ws, code, msg):
        print("[WS] closed", code, msg)

    def send(self, payload):
        try:
            self.ws.send(json.dumps(payload))
        except Exception as e:
            print("[WS][ERROR] send failed:", e)

    def decision_loop(self):
        while True:
            if len(self.ticks) < 100:
                time.sleep(1)
                continue

            odd_count = sum(1 for d in self.ticks if d % 2 == 1)
            even_count = len(self.ticks) - odd_count
            total = len(self.ticks)
            odd_prob = odd_count / total if total else 0.5
            even_prob = even_count / total if total else 0.5

            choice = "DIGITODD" if odd_prob > even_prob else "DIGITEVEN"
            print(f"[BOT] Deciding: OddProb={odd_prob:.3f}, EvenProb={even_prob:.3f}, Choice={choice}")

            # random jitter to avoid trap (kept)
            if random.random() < 0.05:
                choice = "DIGITODD" if choice == "DIGITEVEN" else "DIGITEVEN"
                print("[BOT] Anti-trap flip ->", choice)

            # request a proposal for the chosen side
            self.send({
                "proposal": 1,
                "amount": self.stake,
                "basis": "stake",
                "contract_type": choice,
                "currency": "USD",
                "duration": 1,
                "duration_unit": "t",
                "symbol": "R_10"
            })

            time.sleep(3)  # pacing between trades

if __name__ == "__main__":
    bot = DynamicOddEvenBot(DERIV_API_TOKEN)
    bot.connect()
    while True:
        time.sleep(1)
