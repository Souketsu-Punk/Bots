#!/usr/bin/env python3
# Paste token into API_TOKEN and run
# pip install websocket-client


import json, time, uuid, threading, queue, csv, sys, random
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

try:
    import websocket
except Exception:
    print("pip install websocket-client")
    sys.exit(1)

#CONFIG 
API_TOKEN = "Asdfg"   # put demo token here
SYMBOL = "R_10"
STAKE = 1.0
DURATION_CHOICES = (1, 2)  # ticks
WINDOW = 50                 # smaller for testing
WARMUP = 10                 
Z_THRESHOLD = 1.64
MIN_EDGE = 0.01
MIN_PAYOUT_RATIO = 0.9
PING_INTERVAL = 30
CSV_LOG = "trades_log.csv"
APP_ID = "Enter ID Here"
WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}"


#Functions
def last_digit_from_quote(q: float) -> Optional[int]:
    s = f"{q:.2f}"
    for ch in reversed(s):
        if ch.isdigit(): return int(ch)
    return None

def z_score(p_hat: float, p0: float, n: int) -> float:
    if n <= 0: return 0.0
    var = p0 * (1 - p0) / n
    if var <= 0: return 0.0
    return (p_hat - p0) / (var ** 0.5)

class DigitWindow:
    def __init__(self, size:int):
        self.size = size
        self.buf: Deque[int] = deque(maxlen=size)
        self.counts = [0]*10
        self.n = 0
    def add(self,d:int):
        if len(self.buf)==self.size:
            old = self.buf[0]; self.counts[old]-=1
        self.buf.append(d); self.counts[d]+=1; self.n=len(self.buf)
    def freq(self): return self.counts, self.n

@dataclass
class Candidate:
    side: str
    threshold:int
    p_hat:float
    p0:float
    z:float
    edge:float
    fair_b:float
    fair_ev:float

class Strategy:
    def __init__(self, window: DigitWindow):
        self.window = window
    def find_best(self) -> Optional[Candidate]:
        counts, n = self.window.freq()
        if n < WARMUP: return None
        freq = [c / n for c in counts]
        pref=[0.0]*11; s=0.0
        for i in range(10):
            s+=freq[i]; pref[i+1]=s
        best=None
        for t in range(0,9):
            p_over_hat = pref[10] - pref[t+1]
            p_over0 = (9-t)/10.0
            z_over = z_score(p_over_hat,p_over0,n)
            edge_over = p_over_hat - p_over0
            fair_b_over = (1.0/p_over_hat - 1.0) if p_over_hat>0 else 0.0
            fair_ev_over = (p_over_hat*fair_b_over) - (1-p_over_hat)

            p_under_hat = pref[t]
            p_under0 = t/10.0
            z_under = z_score(p_under_hat,p_under0,n)
            edge_under = p_under_hat - p_under0
            fair_b_under = (1.0/p_under_hat - 1.0) if p_under_hat>0 else 0.0
            fair_ev_under = (p_under_hat*fair_b_under) - (1-p_under_hat)

            for side,p_hat,p0,z,edge,fair_b,fair_ev in [
                ("over",p_over_hat,p_over0,z_over,edge_over,fair_b_over,fair_ev_over),
                ("under",p_under_hat,p_under0,z_under,edge_under,fair_b_under,fair_ev_under),
            ]:
                if edge < MIN_EDGE: continue
                if z < Z_THRESHOLD: continue
                if fair_ev <= 0: continue
                cand = Candidate(side,t,p_hat,p0,z,edge,fair_b,fair_ev)
                if best is None or cand.fair_ev > best.fair_ev: best=cand
        return best

class Trader:
    def __init__(self):
        self.ws = None
        self.dwin = DigitWindow(WINDOW)
        self.strategy = Strategy(self.dwin)
        self.auth = False
        self.proposal_waiters = {}
        self.buy_q = queue.Queue()
        self.pending_contracts = {}
        self.trade_no = 0
        self.wins = 0
        self.losses = 0
        self.last_trade_ts = 0.0
        self.tick_queue = queue.Queue(maxsize=200)
        self._init_csv()

    def _init_csv(self):
        try:
            with open(CSV_LOG,"a",newline="") as f:
                w=csv.writer(f)
                if f.tell()==0:
                    w.writerow(["ts","trade_no","side","threshold","dur_ticks","p_hat","z","edge","payout","payout_ratio","stake","profit","wins","losses"])
        except Exception as e:
            print("CSV init err:",e)

    def _log(self,row):
        try:
            with open(CSV_LOG,"a",newline="") as f:
                w=csv.writer(f); w.writerow(row)
        except:
            pass

    def on_open(self, ws):
        print("[WS] open")
        if not API_TOKEN or "PUT_DEMO_TOKEN_HERE" in API_TOKEN:
            print("✖️  Put demo token into API_TOKEN at top of script.")
            self.stop()
            return
        ws.send(json.dumps({"authorize": API_TOKEN}))
        threading.Thread(target=self._pinger, daemon=True).start()

    def on_message(self, ws, raw):
        try: data=json.loads(raw)
        except Exception: return
        if "error" in data and data["error"]:
            print("[WS][ERROR]",data["error"])
            return
        msg = data.get("msg_type") or ("tick" if "tick" in data else None)
        if msg == "authorize":
            print("[WS] authorized")
            self.auth=True
            ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
            print("[WS] subscribed", SYMBOL)
            return
        if "tick" in data:
            tick=data["tick"]; q=tick.get("quote")
            if q is None: return
            try: quote=float(q)
            except: return
            d = last_digit_from_quote(quote)
            if d is None: return
            self.dwin.add(d)
            try: self.tick_queue.put_nowait(quote)
            except: pass
            return
        if "proposal" in data:
            passth = data.get("echo_req", {}).get("passthrough") or data.get("passthrough")
            tag = passth.get("tag") if isinstance(passth,dict) else None
            if tag and tag in self.proposal_waiters:
                entry = self.proposal_waiters.pop(tag)
                entry['proposal'] = data['proposal']
                entry['event'].set()
            else:
                print("[DEBUG] Unmatched proposal:", data.get("proposal",{}).get("id"))
            return
        if "buy" in data:
            self.buy_q.put(data["buy"])
            return
        if "proposal_open_contract" in data:
            poc = data["proposal_open_contract"]
            cid = str(poc.get("contract_id") or poc.get("id") or poc.get("identifier"))
            profit = None
            if "profit" in poc:
                try: profit=float(poc["profit"])
                except: profit=None
            sub_id = None
            if "subscription" in data and isinstance(data["subscription"], dict):
                sub_id = data["subscription"].get("id")
            self.pending_contracts[cid] = {"update":poc,"profit":profit,"sub_id":sub_id}
            return

    def on_error(self, ws, err):
        print("[WS][ERROR]", err)

    def on_close(self, ws, code, reason):
        print("[WS] closed",code,reason)

    def _pinger(self):
        while True:
            try:
                if self.ws: self.ws.send(json.dumps({"ping":1}))
            except: pass
            time.sleep(PING_INTERVAL)

    def send_proposal_and_wait(self, side, threshold, stake, duration_ticks, timeout=5.0):
        tag = uuid.uuid4().hex
        payload = {
            "proposal": 1,
            "amount": float(stake),
            "basis": "stake",
            "contract_type": "DIGITOVER" if side=="over" else "DIGITUNDER",
            "currency": "USD",
            "duration": int(duration_ticks),
            "duration_unit": "t",
            "symbol": SYMBOL,
            "barrier": str(threshold),
            "passthrough": {"tag": tag,"side":side,"threshold":threshold,"duration":duration_ticks}
        }
        waiter = {"event": threading.Event(), "proposal": None}
        self.proposal_waiters[tag] = waiter
        try:
            self.ws.send(json.dumps(payload))
        except Exception as e:
            print("[ERR] send proposal:", e)
            self.proposal_waiters.pop(tag, None)
            return None
        ok = waiter['event'].wait(timeout)
        if not ok:
            self.proposal_waiters.pop(tag, None)
            return None
        return waiter['proposal']

    def buy_and_wait(self, proposal, stake, timeout=6.0):
        pid = proposal.get("id") or proposal.get("proposal_id")
        if not pid: return None
        buy_req = {"buy": pid, "price": float(stake)}
        try: self.ws.send(json.dumps(buy_req))
        except Exception as e: print("[ERR] send buy:", e); return None
        try:
            buy = self.buy_q.get(timeout=timeout)
            cid = buy.get("contract_id")
            return str(cid) if cid else None
        except queue.Empty: return None

    def await_settlement(self, contract_id, timeout=90.0):
        try: self.ws.send(json.dumps({"proposal_open_contract":1,"contract_id":contract_id,"subscribe":1}))
        except: pass
        deadline = time.time() + timeout
        profit = None; sub_id = None
        while time.time()<deadline:
            info = self.pending_contracts.get(str(contract_id))
            if info and "profit" in info and info["profit"] is not None:
                profit = info["profit"]
                sub_id = info.get("sub_id")
                break
            time.sleep(0.1)
        if sub_id:
            try: self.ws.send(json.dumps({"forget": sub_id}))
            except: pass
        self.pending_contracts.pop(str(contract_id),None)
        return profit

    def main_loop(self):
        while True:
            try: _ = self.tick_queue.get(timeout=2.0)
            except queue.Empty: continue
            if not self.auth: continue
            if time.time() - self.last_trade_ts < 0.25: continue
            cand = self.strategy.find_best()
            if cand is None: continue
            duration = random.choice(DURATION_CHOICES)
            stake = STAKE
            prop = self.send_proposal_and_wait(cand.side, cand.threshold, stake, duration, timeout=5.0)
            if not prop: continue
            payout = float(prop.get("payout",0))
            plat_b = (payout-stake)/stake
            plat_ev = (cand.p_hat*plat_b)-(1-cand.p_hat)
            fair_payout = stake / max(1e-12,cand.p_hat)
            payout_ratio = payout / fair_payout if fair_payout>0 else 0.0
            if payout_ratio < MIN_PAYOUT_RATIO: continue
            if plat_ev <=0: continue
            cid = self.buy_and_wait(prop, stake, timeout=6.0)
            if not cid: print("[WARN] buy timed out/no response"); continue
            profit = self.await_settlement(cid, timeout=max(20,duration*20))
            if profit is None: print(f"[WARN] no settlement for {cid}"); continue
            self.trade_no +=1
            if profit>0: self.wins+=1
            else: self.losses+=1
            self.last_trade_ts=time.time()
            print(f"[TRADE {self.trade_no}] {cand.side.upper()}>{cand.threshold} dur={duration}t stake={stake} p_hat={cand.p_hat:.4f} z={cand.z:.2f} edge={cand.edge:.4f} payout={payout:.2f} profit={profit:+.2f} wins={self.wins} losses={self.losses}")
            self._log([int(time.time()),self.trade_no,cand.side,cand.threshold,duration,f"{cand.p_hat:.6f}",f"{cand.z:.4f}",f"{cand.edge:.6f}",f"{payout:.4f}",f"{payout_ratio:.4f}",stake,profit,self.wins,self.losses])

    def start(self):
        def run_ws():
            self.ws = websocket.WebSocketApp(WS_URL,on_open=self.on_open,on_message=self.on_message,on_error=self.on_error,on_close=self.on_close)
            self.ws.run_forever(ping_interval=25,ping_timeout=10)
        threading.Thread(target=run_ws,daemon=True).start()
        self.main_loop_thread = threading.Thread(target=self.main_loop,daemon=True)
        self.main_loop_thread.start()

    def stop(self):
        try:
            if self.ws: self.ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 0}))
        except: pass
        try:
            if self.ws: self.ws.close()
        except: pass
        print(f"Stopped. trades={self.trade_no} wins={self.wins} losses={self.losses}")

# -------------- RUN --------------
if __name__=="__main__":
    trader = Trader()
    try:
        trader.start()
        while True: time.sleep(1)
    except KeyboardInterrupt:
        trader.stop()
