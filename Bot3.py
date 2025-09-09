#!/usr/bin/env python3
"""
adaptive_dynamic_overunder_bot.py

Trade R_10 Over/Under dynamically using last-1000-tick digit stats.
- Uses Deriv WebSocket: proposal -> buy -> monitor flow
- Dynamic threshold scanning (t = 1..8) to find best Over/Under split
- Uses live ticks_history (count=1000) to compute digit stats
- Weighted stake recovery after losses (conservative caps)
- Small randomization to avoid pattern traps
- CSV logging and safety controls
- Live console log of decisions and rolling summary
"""

import json
import time
import uuid
import threading
import queue
import csv
import random
from collections import deque
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple

try:
    import websocket
except Exception:
    print("pip install websocket-client")
    exit(1)

# -----------------------
# CONFIG
# -----------------------
DERIV_APP_ID = "96437"
DERIV_API_TOKEN = "JDKYPoc3aLSHjiY"  # demo token
WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"

SYMBOL = "R_10"
BASE_STAKE = 10.0
ALLOW_ADAPTIVE_MULTIPLIER = True

RECOVERY_MULTIPLIER = 1.2
MAX_STAKE = 50.0
MAX_STAKE_PCT_BAL = 0.02
MIN_STAKE = 0.5

WARMUP_TICKS = 20
SCAN_THRESHOLDS = list(range(1, 9))
PROPOSAL_TIMEOUT = 2.0
BUY_TIMEOUT = 5.0

EXPLORATION_PROB = 0.08
TEMPERATURE = 0.9

DAILY_TP = 40.0
DAILY_SL = -20.0
LOSS_STREAK_PAUSE = 3
PAUSE_TICKS = 6
COOLDOWN = 0.05
PING_INTERVAL = 25

CSV_FILE = "dynamic_overunder_trades.csv"
MIN_EV = 0.0

# -----------------------
# Helpers & dataclasses
# -----------------------
@dataclass
class Candidate:
    side: str
    threshold: int
    p_win: float
    payout: float
    net_b: float
    ev: float

def last_digit_from_quote(q: float) -> Optional[int]:
    s = f"{q:.2f}"
    for ch in reversed(s):
        if ch.isdigit():
            return int(ch)
    return None

def _next_req_id(self):
    if not hasattr(self, "_req_counter"):
        self._req_counter = 1
    else:
        self._req_counter += 1
    return self._req_counter

# -----------------------
# Bot class
# -----------------------
class DynamicOverUnderBot:
    def __init__(self, token: str, ws_url: str = WS_URL):
        self.token = token
        self.ws_url = ws_url
        self.ws: Optional[websocket.WebSocketApp] = None

        self.tick_q: "queue.Queue[float]" = queue.Queue(maxsize=2000)
        self.proposal_waiters: Dict[str, dict] = {}
        self.buy_q: "queue.Queue[dict]" = queue.Queue()
        self.pending_contracts: Dict[str, dict] = {}

        self.recent_ticks: deque = deque(maxlen=1000)
        self.live_digit_counts = [0]*10

        self.trade_no = 0
        self.wins = 0
        self.losses = 0
        self.loss_streak = 0
        self.consecutive_losses = 0
        self.current_stake = BASE_STAKE
        self.balance: float = 0.0
        self.start_balance: Optional[float] = None
        self.total_profit = 0.0
        self.last_trade_ts = 0.0
        self._tick_count = 0
        self.pause_until_tick = 0
        self.recent_profits: deque = deque(maxlen=50)
        self.current_min_ev = MIN_EV
        self._lock = threading.Lock()
        self._init_csv()
        self.last_trades: deque = deque(maxlen=10)  # rolling summary

    # CSV
    def _init_csv(self):
        try:
            with open(CSV_FILE, "a", newline="") as f:
                w = csv.writer(f)
                if f.tell() == 0:
                    w.writerow(["ts","trade_no","side","threshold","stake","payout","profit",
                                "p_win","net_b","ev","balance","note"])
        except Exception as e:
            print("[CSV] init error:", e)

    def _log(self, row: List):
        try:
            with open(CSV_FILE, "a", newline="") as f:
                csv.writer(f).writerow(row)
        except Exception:
            pass

    # -----------------------
    # WebSocket callbacks
    # -----------------------
    def _on_open(self, ws):
        print("[WS] open")
        if not self.token:
            print("✖️ Put your token in DERIV_API_TOKEN")
            self.stop()
            return
        ws.send(json.dumps({"authorize": self.token}))
        threading.Thread(target=self._pinger, daemon=True).start()

    def _on_message(self, ws, raw):
        try:
            data = json.loads(raw)
        except Exception:
            return
        if "error" in data:
            print("[WS][ERROR]", data["error"])
            return
        # authorize
        if data.get("msg_type") == "authorize" or "authorize" in data:
            auth = data.get("authorize") or {}
            print("[WS] authorized")
            if isinstance(auth, dict):
                for k,v in auth.items():
                    try:
                        if isinstance(v,(int,float)) and "balance" in k.lower():
                            self.start_balance = float(v)
                            self.balance = float(v)
                            print(f"[START] start balance: {self.balance:.2f}")
                            break
                    except: pass
            ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
            print(f"[WS] subscribed {SYMBOL}")
            return
        # tick
        if "tick" in data:
            q = data["tick"].get("quote")
            if q is not None:
                try:
                    quote = float(q)
                except: return
                self._tick_count += 1
                d = last_digit_from_quote(quote)
                if d is not None:
                    with self._lock:
                        if len(self.recent_ticks) == self.recent_ticks.maxlen:
                            oldq = self.recent_ticks[0]
                            oldd = last_digit_from_quote(oldq)
                            if oldd is not None:
                                self.live_digit_counts[oldd]-=1
                        self.recent_ticks.append(quote)
                        self.live_digit_counts[d]+=1
                try: self.tick_q.put_nowait(quote)
                except queue.Full: pass
            return
        # history response
        if "history" in data and ("echo_req" in data and data["echo_req"].get("tag")):
            tag = data["echo_req"].get("tag")
            with self._lock:
                self.proposal_waiters[tag]={"history":data["history"]}
            return
        # proposal
        if "proposal" in data:
            echo = data.get("echo_req") or {}
            passth = echo.get("passthrough") or data.get("passthrough") or {}
            tag = None
            if isinstance(passth,dict):
                tag = passth.get("tag")
            if tag and tag in self.proposal_waiters:
                entry=self.proposal_waiters.pop(tag)
                entry["proposal"]=data["proposal"]
                with self._lock:
                    self.proposal_waiters[tag]=entry
            else:
                echo_req=data.get("echo_req",{})
                req_id=echo_req.get("req_id")
                if req_id is not None:
                    with self._lock:
                        self.proposal_waiters[int(req_id)]={"proposal":data["proposal"]}
            return
        # buy
        if "buy" in data:
            try: self.buy_q.put_nowait(data["buy"])
            except queue.Full: pass
            return
        # contract update
        if "proposal_open_contract" in data or "contract_update" in data:
            poc = data.get("proposal_open_contract") or data.get("contract_update")
            if not isinstance(poc,dict): return
            cid = str(poc.get("contract_id") or poc.get("id") or poc.get("identifier"))
            profit = None
            if "profit" in poc:
                try: profit = float(poc["profit"])
                except: profit=None
            with self._lock:
                prev=self.pending_contracts.get(cid,{})
                prev.update({"update":poc,"profit":profit})
                self.pending_contracts[cid]=prev
                if profit is not None:
                    self.total_profit += profit
                    self.balance += profit
                    self.recent_profits.append(profit)
                    if profit>0: self.wins+=1; self.loss_streak=0
                    else: self.losses+=1; self.loss_streak+=1

    def _on_error(self, ws, err):
        print("[WS][ERROR]", err)
    def _on_close(self, ws, code, reason):
        print("[WS] closed", code, reason)
    def _pinger(self):
        while True:
            try:
                if self.ws:
                    self.ws.send(json.dumps({"ping":1}))
            except: pass
            time.sleep(PING_INTERVAL)

    # -----------------------
    # Request helpers
    # -----------------------
    def request_ticks_history(self, count:int=1000,timeout:float=3.0)->Optional[List[float]]:
        tag=uuid.uuid4().hex
        req={"ticks_history":SYMBOL,"end":"latest","count":int(count),
             "style":"ticks","passthrough":{"tag":tag},
             "req_id":int(tag) if tag.isdigit() else _next_req_id(self)}
        waiter={"event":threading.Event(),"history":None}
        with self._lock: self.proposal_waiters[tag]=waiter
        try: self.ws.send(json.dumps(req))
        except:
            with self._lock: self.proposal_waiters.pop(tag,None)
            return None
        deadline=time.time()+timeout
        while time.time()<deadline:
            with self._lock:
                entry=self.proposal_waiters.get(tag)
                if entry and entry.get("history"):
                    hist=entry["history"]
                    self.proposal_waiters.pop(tag,None)
                    quotes=[]
                    for p in hist.get("prices") or hist.get("candles") or []:
                        try: quotes.append(float(p))
                        except: pass
                    return quotes
            time.sleep(0.05)
        with self._lock: self.proposal_waiters.pop(tag,None)
        return None

    def request_proposal(self, side:str, threshold:int, stake:float, timeout:float=PROPOSAL_TIMEOUT)->Optional[dict]:
        tag=uuid.uuid4().hex
        contract_type="DIGITOVER" if side=="over" else "DIGITUNDER"
        req={"proposal":1,"amount":float(stake),"basis":"stake","contract_type":contract_type,
             "symbol":SYMBOL,"duration":1,"duration_unit":"t","currency":"USD",
             "barrier":str(threshold),
             "passthrough":{"tag":tag,"side":side,"threshold":threshold}}
        waiter={"event":threading.Event(),"proposal":None}
        with self._lock: self.proposal_waiters[tag]=waiter
        try: self.ws.send(json.dumps(req))
        except:
            with self._lock: self.proposal_waiters.pop(tag,None)
            return None
        deadline=time.time()+timeout
        while time.time()<deadline:
            with self._lock:
                entry=self.proposal_waiters.get(tag)
                if entry and entry.get("proposal"):
                    prop=entry["proposal"]
                    self.proposal_waiters.pop(tag,None)
                    return prop
            time.sleep(0.03)
        with self._lock: self.proposal_waiters.pop(tag,None)
        return None

    def buy_proposal(self, proposal:dict, stake:float, timeout:float=BUY_TIMEOUT)->Optional[str]:
        pid=proposal.get("id") or proposal.get("proposal_id")
        if not pid: return None
        buy_req={"buy":pid,"price":float(stake)}
        try: self.ws.send(json.dumps(buy_req))
        except: return None
        try:
            buy_resp=self.buy_q.get(timeout=timeout)
            cid=buy_resp.get("contract_id") or buy_resp.get("purchase",{}).get("contract_id")
            return str(cid) if cid else None
        except queue.Empty: return None

    def wait_for_settlement(self, contract_id:str,timeout:float=30.0)->Optional[Tuple[float,dict]]:
        try: self.ws.send(json.dumps({"proposal_open_contract":1,"contract_id":contract_id,"subscribe":1}))
        except: pass
        deadline=time.time()+timeout
        profit=None; info=None
        while time.time()<deadline:
            with self._lock:
                entry=self.pending_contracts.get(str(contract_id))
                if entry and "profit" in entry and entry["profit"] is not None:
                    profit=entry["profit"]
                    info=entry.get("update",{})
                    self.pending_contracts.pop(str(contract_id),None)
                    break
            time.sleep(0.05)
        return (profit,info) if profit is not None else None

    # -----------------------
    # Decision & stats
    # -----------------------
    def compute_digit_stats_from_history(self, history_quotes:List[float])->List[float]:
        counts=[0]*10
        for q in history_quotes:
            d=last_digit_from_quote(q)
            if d is not None: counts[d]+=1
        total=sum(counts)
        return [c/total if total>0 else 0.1 for c in counts]

    def compute_stats_and_choose(self)->Optional[Candidate]:
        # get last 1000 ticks
        with self._lock:
            if len(self.recent_ticks)<10: return None
            hist=list(self.recent_ticks)
        counts=self.compute_digit_stats_from_history(hist)
        best_ev=-999
        best_cand=None
        for t in SCAN_THRESHOLDS:
            # digit over t
            p_over=sum(counts[t:])
            p_under=sum(counts[:t])
            for side,p in [("over",p_over),("under",p_under)]:
                net_b=9*(1-0.0)/1-0.0  # approx, dummy
                ev=p*net_b-(1-p)
                if ev>best_ev:
                    best_ev=ev
                    best_cand=Candidate(side=side,threshold=t,p_win=p,payout=0,net_b=net_b,ev=ev)
        return best_cand

    def suggest_stake(self, base:float, cand:Candidate)->float:
        stake=base
        if self.loss_streak>0:
            stake=min(base*RECOVERY_MULTIPLIER**self.loss_streak, MAX_STAKE)
        stake=min(stake, self.balance*MAX_STAKE_PCT_BAL)
        stake=max(stake, MIN_STAKE)
        stake+=random.uniform(-0.05,0.05)
        return round(stake,2)

    # -----------------------
    # Decision loop with console log
    # -----------------------
    def decision_loop(self):
        while self._tick_count<WARMUP_TICKS:
            time.sleep(0.2)
        print("[BOT] warmup complete, starting decision loop.")
        while True:
            if time.time()-self.last_trade_ts<COOLDOWN:
                time.sleep(0.01)
                continue
            if self._tick_count<self.pause_until_tick:
                time.sleep(0.01)
                continue
            if self.start_balance is not None:
                daily_pnl=self.balance-self.start_balance
                if daily_pnl<=DAILY_SL or daily_pnl>=DAILY_TP:
                    print("[STOP] daily limit reached:", daily_pnl)
                    self.stop()
                    return

            cand=self.compute_stats_and_choose()
            if cand is None:
                time.sleep(0.05)
                continue

            stake=self.suggest_stake(BASE_STAKE,cand)

            # Console log of the decision before buying
            print(f"[TRADE DECISION] Side: {cand.side.upper()} | Threshold: {cand.threshold} | "
                  f"P_win: {cand.p_win:.3f} | EV: {cand.ev:.3f} | Stake: ${stake:.2f} | "
                  f"Balance: ${self.balance:.2f} | Loss streak: {self.loss_streak}")

            prop=self.request_proposal(cand.side,cand.threshold,stake)
            if not prop:
                time.sleep(0.01)
                continue

            payout=None
            for f in ("payout","ask_price","quote"):
                if f in prop:
                    try: payout=float(prop[f]); break
                    except: continue
            if payout is None:
                time.sleep(0.01)
                continue

            ask=prop.get("ask_price") or prop.get("price") or stake
            try: ask=float(ask)
            except: ask=stake
            net_b=(payout-ask)/ask if ask>0 else 0.0
            ev=cand.p_win*net_b-(1-cand.p_win)
            cand.payout=payout; cand.net_b=net_b; cand.ev=ev

            if cand.ev<=self.current_min_ev:
                time.sleep(0.01)
                continue

            cid=self.buy_proposal(prop,stake)
            self.last_trade_ts=time.time()

            if cid:
                profit_note="pending"
                row=[time.time(),self.trade_no,cand.side,cand.threshold,stake,payout,
                     profit_note,cand.p_win,net_b,ev,self.balance,""]
                self._log(row)
                self.trade_no+=1
                # add to rolling summary
                self.last_trades.append(row)
                self._print_rolling_summary()

            time.sleep(0.05)

    def _print_rolling_summary(self):
        print("=== Rolling last trades summary ===")
        for t in list(self.last_trades)[-10:]:
            print(f"{int(t[1])}: {t[2].upper()} T{t[3]} Stake:${t[4]:.2f} EV:{t[9]:.2f} Profit:{t[5]}")
        print("===============================")

    # -----------------------
    # Start / Stop
    # -----------------------
    def start(self):
        self.ws=websocket.WebSocketApp(self.ws_url,
                                       on_open=self._on_open,
                                       on_message=self._on_message,
                                       on_error=self._on_error,
                                       on_close=self._on_close)
        self.thread_ws=threading.Thread(target=self.ws.run_forever,daemon=True)
        self.thread_ws.start()
        threading.Thread(target=self.decision_loop,daemon=True).start()

    def stop(self):
        try:
            if self.ws:
                self.ws.close()
        except: pass
        print("[BOT] stopped.")

# -----------------------
# RUN
# -----------------------
if __name__=="__main__":
    import sys
    import time
    bot=DynamicOverUnderBot(DERIV_API_TOKEN)
    bot.start()
    while True:
        time.sleep(1)
