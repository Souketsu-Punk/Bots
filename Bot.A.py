import websocket
import json
import time

"""
Deriv Digits Bot
- Trades four contracts simultaneously: Under 3, Under 6, Over 5, Under 7
- Each contract can have its own stake amount
- No recovery strategy
"""

# ====== USER CONFIG ======
DERIV_APP_ID = "Pu ID here"
API_TOKEN = "PUT TOKEN HERE"
SYMBOL = "R_10"
DURATION_TICKS = 1
TRADE_COOLDOWN_SEC = 1.0  # seconds
# ==========================

open_contracts = {}
last_trade_time = 0.0

DIGIT_CONTRACTS = [
    {"type": "DIGITUNDER", "barrier": 3, "stake": 1.00},
    {"type": "DIGITUNDER", "barrier": 6, "stake": 0.50},
    {"type": "DIGITOVER",  "barrier": 5, "stake": 0.75},
    {"type": "DIGITUNDER", "barrier": 7, "stake": 1.25}
]


def buy_contract(ws: websocket.WebSocketApp, contract_type: str, barrier: int, stake: float):
    payload = {
        "buy": 1,
        "price": stake,
        "parameters": {
            "amount": stake,
            "basis": "stake",
            "contract_type": contract_type,
            "barrier": str(barrier),
            "currency": "USD",
            "duration": DURATION_TICKS,
            "duration_unit": "t",
            "symbol": SYMBOL
        }
    }
    ws.send(json.dumps(payload))
    print(f"[TRADE] Sent {contract_type} (barrier {barrier}) @ ${stake:.2f}")


def on_message(ws, message):
    global last_trade_time

    data = json.loads(message)
    msg_type = data.get("msg_type")

    # --- Authorization ---
    if msg_type == "authorize":
        print(f"[AUTHORIZED] Logged in.")
        ws.send(json.dumps({"ticks": SYMBOL}))
        print("[BOT] Subscribed to tick stream.")

    # --- Tick stream handler ---
    elif msg_type == "tick":
        now = time.time()
        if now - last_trade_time >= TRADE_COOLDOWN_SEC:
            for contract in DIGIT_CONTRACTS:
                buy_contract(ws, contract["type"], contract["barrier"], contract["stake"])
            last_trade_time = now

    # --- Buy confirmation ---
    elif msg_type == "buy":
        buy_info = data.get("buy", {})
        contract_id = buy_info.get("contract_id")
        longcode = buy_info.get("longcode", "UNKNOWN")
        if contract_id:
            open_contracts[contract_id] = longcode
            print(f"[BOUGHT] #{contract_id} | {longcode}")
            ws.send(json.dumps({
                "proposal_open_contract": 1,
                "contract_id": contract_id,
                "subscribe": 1
            }))
        else:
            print("[ERROR] No contract_id returned in buy response.")

    # --- Contract result handler ---
    elif msg_type == "proposal_open_contract":
        poc = data.get("proposal_open_contract", {})
        contract_id = poc.get("contract_id")
        if contract_id and poc.get("is_sold"):
            profit = poc.get("profit", 0.0)
            buy_price = poc.get("buy_price", 0.0)
            sell_price = poc.get("sell_price", 0.0)
            result = "WIN" if profit > 0 else "LOSS"
            contract_desc = open_contracts.get(contract_id, "UNKNOWN")
            print(f"[RESULT] {result} | {contract_desc} | Profit: ${profit:.2f}")

            # Forget stream
            ws.send(json.dumps({
                "forget": poc.get("subscription", {}).get("id")
            }))
            open_contracts.pop(contract_id, None)

    # --- Error handler ---
    elif msg_type == "error":
        err = data.get("error", {})
        print(f"[API ERROR] {err.get('message')}")


def on_open(ws):
    print("[CONNECTED] Authorizing...")
    ws.send(json.dumps({"authorize": API_TOKEN}))


def on_error(ws, error):
    print(f"[ERROR] {error}")


def on_close(ws, close_status_code, close_msg):
    print(f"[CLOSED] {close_status_code} - {close_msg}")


if __name__ == "__main__":
    ws = websocket.WebSocketApp(
        f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    ws.run_forever(ping_interval=30)
