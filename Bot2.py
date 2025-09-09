import websocket
import json
import time
import random
from collections import deque

#USER CONFIG
DERIV_APP_ID = "PUT ID HERE"
API_TOKEN = "PUT TOKEN HERE"
SYMBOL = "R_10"
STAKE = 1.0
LOSS_RECOVERY_MULTIPLIER = 1.5  # how aggressively stake increases after a loss
MAX_STAKE = 50                  # max safety limit
RECENT_TICKS = 50               # how many ticks to track for probability


# State trackers
balance = 0.0
last_trade_won = True
current_stake = STAKE
recent_digits = deque(maxlen=RECENT_TICKS)

def weighted_stake(last_win, last_stake):
    """Adjust stake based on win/loss using weighted recovery."""
    if last_win:
        return STAKE
    else:
        # stake increase
        return min(last_stake * LOSS_RECOVERY_MULTIPLIER, MAX_STAKE)

def select_digit_probability():
    """Choose a digit based on inverse frequency (1-8)."""
    if len(recent_digits) < 5:
        # not enough data, pick random
        return random.randint(1, 8)

    freq = {d: recent_digits.count(d) for d in range(1, 9)}
    max_freq = max(freq.values())
    weights = {d: max_freq - freq[d] + 1 for d in freq}  # inverse frequency weight

    # Weighted random selection
    digits = list(weights.keys())
    probs = [weights[d] for d in digits]
    total = sum(probs)
    probs = [p / total for p in probs]
    return random.choices(digits, probs)[0]

def on_message(ws, message):
    global balance, last_trade_won, current_stake

    data = json.loads(message)

    # Handle authorization
    if data.get("msg_type") == "authorize":
        print(f"[AUTHORIZED] Logged in as: {data['authorize']['loginid']}")
        ws.send(json.dumps({
            "ticks": SYMBOL
        }))
        print("[BOT] Starting trades...")

    # Handle ticks
    elif data.get("msg_type") == "tick":
        last_digit = int(str(data["tick"]["quote"])[-1])
        if 1 <= last_digit <= 8:
            recent_digits.append(last_digit)

        digit_to_trade = select_digit_probability()

        contract = {
            "buy": 1,
            "price": current_stake,
            "parameters": {
                "amount": current_stake,
                "basis": "stake",
                "contract_type": "DIGITMATCH",
                "currency": "USD",
                "duration": 1,
                "duration_unit": "t",
                "symbol": SYMBOL,
                "barrier": str(digit_to_trade)
            }
        }
        ws.send(json.dumps(contract))
        print(f"[TRADE] Buying DIGITMATCH {digit_to_trade} @ ${current_stake:.2f}")

    # Handle buy confirmation
    elif data.get("msg_type") == "buy":
        print(f"[BOUGHT] Contract #{data['buy']['contract_id']}")

    # Handle proposal_open_contract
    elif data.get("msg_type") == "proposal_open_contract":
        poc = data["proposal_open_contract"]
        if poc.get("is_sold"):
            profit = poc.get("profit", 0)
            if profit > 0:
                last_trade_won = True
                print(f"[WIN] Profit: ${profit:.2f}")
            else:
                last_trade_won = False
                print(f"[LOSS] Lost: ${-profit:.2f}")

            current_stake = weighted_stake(last_trade_won, current_stake)

def on_open(ws):
    print("[CONNECTED] Authorizing...")
    ws.send(json.dumps({"authorize": API_TOKEN}))

def on_error(ws, error):
    print(f"[ERROR] {error}")

def on_close(ws, close_status_code, close_msg):
    print("[CLOSED] Connection closed.")

if __name__ == "__main__":
    ws = websocket.WebSocketApp(
        f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}",
        on_message=on_message,
        on_open=on_open,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever()
