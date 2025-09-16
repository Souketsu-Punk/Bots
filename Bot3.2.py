import websocket
import json
import time
from typing import Optional

"""
Deriv Rise/Fall bot via WebSocket.
- Trades both directions (Rise and Fall) at the same time using FIXED stakes (no recovery / martingale).
- On each tick, it sends one CALL (rise) and one PUT (fall) order.
"""

# ====== USER CONFIG ======
DERIV_APP_ID = "PUT ID HERE"    
API_TOKEN = "PUT TOKEN HERE"
SYMBOL = "R_10"                      
DURATION_TICKS = 1           
RISE_STAKE = 1.00            
FALL_STAKE = 1.00            
TRADE_COOLDOWN_SEC = 0.5     
# ==========================


open_contracts = {} 
last_trade_time: float = 0.0


def buy_contract(ws: websocket.WebSocketApp, side: str, stake: float):
    """Send a buy order for CALL (rise) or PUT (fall) with fixed stake."""
    payload = {
        "buy": 1,
        "price": stake,
        "parameters": {
            "amount": stake,
            "basis": "stake",
            "contract_type": side,          # CALL (rise) or PUT (fall)
            "currency": "USD",
            "duration": DURATION_TICKS,
            "duration_unit": "t",
            "symbol": SYMBOL,
            "allow_equals": 1
        }
    }
    ws.send(json.dumps(payload))
    print(f"[TRADE] Sent {side} @ ${stake:.2f} | {DURATION_TICKS}t on {SYMBOL}")


def on_message(ws, message):
    global last_trade_time

    data = json.loads(message)
    msg_type = data.get("msg_type")

    # --- Authorization ---
    if msg_type == "authorize":
        auth = data.get("authorize", {})
        loginid = auth.get("loginid")
        print(f"[AUTHORIZED] Logged in as: {loginid}")
        # Subscribe to ticks so we have a heartbeat from the market
        ws.send(json.dumps({"ticks": SYMBOL}))
        print("[BOT] Waiting for first tick to begin trading...")

    # --- Tick stream ---
    elif msg_type == "tick":
        if (time.time() - last_trade_time) >= TRADE_COOLDOWN_SEC:
            # Buy both CALL and PUT simultaneously
            buy_contract(ws, "CALL", RISE_STAKE)
            buy_contract(ws, "PUT", FALL_STAKE)
            last_trade_time = time.time()

    # --- Buy confirmation ---
    elif msg_type == "buy":
        buy_info = data.get("buy", {})
        contract_id = buy_info.get("contract_id")
        side = buy_info.get("longcode", "").split()[0] if buy_info.get("longcode") else "?"
        if contract_id:
            open_contracts[contract_id] = side
            print(f"[BOUGHT] Contract #{contract_id} ({side})")
            # Subscribe to updates for this contract
            ws.send(json.dumps({
                "proposal_open_contract": 1,
                "contract_id": contract_id,
                "subscribe": 1
            }))
        else:
            print("[WARN] No contract_id returned on buy.")

    # --- Contract updates ---
    elif msg_type == "proposal_open_contract":
        poc = data.get("proposal_open_contract", {})
        contract_id = poc.get("contract_id")
        if contract_id and contract_id in open_contracts:
            if poc.get("is_sold"):
                profit = poc.get("profit", 0.0) or 0.0
                buy_price = poc.get("buy_price", 0.0) or 0.0
                sell_price = poc.get("sell_price", 0.0) or 0.0
                status = "WIN" if profit > 0 else ("LOSS" if profit < 0 else "BREAKEVEN")
                side = open_contracts.get(contract_id, "?")
                print(f"[RESULT] {status} ({side}) | buy={buy_price:.2f} sell={sell_price:.2f} profit={profit:.2f}")

                # Stop updates for this contract
                ws.send(json.dumps({
                    "forget": poc.get("subscription", {}).get("id")
                }))
                open_contracts.pop(contract_id, None)

    # --- Errors from API ---
    elif msg_type == "error":
        err = data.get("error", {})
        print(f"[API ERROR] code={err.get('code')} message={err.get('message')}")


def on_open(ws):
    print("[CONNECTED] Authorizing...")
    ws.send(json.dumps({"authorize": API_TOKEN}))


def on_error(ws, error):
    print(f"[WS ERROR] {error}")


def on_close(ws, close_status_code, close_msg):
    print(f"[CLOSED] code={close_status_code} msg={close_msg}")


if __name__ == "__main__":
    ws = websocket.WebSocketApp(
        f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}",
        on_message=on_message,
        on_open=on_open,
        on_error=on_error,
        on_close=on_close,
    )
    ws.run_forever(ping_interval=30)
