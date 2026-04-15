# =============================================================================
# UT BOT TRADING SYSTEM - Single File Version
# =============================================================================

import os
import json
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, render_template_string, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# =============================================================================
# ENVIRONMENT VARIABLES — set these in Render dashboard or edit here
# =============================================================================

PORT                = int(os.environ.get("PORT", 5000))
LOT_SIZE_BTC        = float(os.environ.get("LOT_SIZE_BTC", 0.001))
USDT_INR_MANUAL     = float(os.environ.get("USDT_INR_MANUAL", 85.0))
USDT_INR_AUTO_FETCH = os.environ.get("USDT_INR_AUTO_FETCH", "true").lower() == "true"
START_BALANCE_INR   = float(os.environ.get("START_BALANCE_INR", 10000.0))
TRADE_START_HOUR    = int(os.environ.get("TRADE_START_HOUR", 18))
TRADE_END_HOUR      = int(os.environ.get("TRADE_END_HOUR", 23))

# =============================================================================
# FILE PATHS
# =============================================================================

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
TRADES_FILE  = os.path.join(SCRIPT_DIR, "trades.json")
CONFIG_FILE  = os.path.join(SCRIPT_DIR, "config.json")

# =============================================================================
# DEFAULT CONFIG
# =============================================================================

def get_default_config():
    return {
        "lot_size": {
            "btc_amount": LOT_SIZE_BTC
        },
        "usdt_inr": {
            "auto_fetch": USDT_INR_AUTO_FETCH,
            "manual_rate": USDT_INR_MANUAL,
            "cache_seconds": 300
        },
        "cooldown": {
            "base_seconds": 300,
            "min_seconds": 60,
            "max_seconds": 3600,
            "atr_avg_period": 20
        },
        "stop_loss": {
            "atr_multiplier": 2.0,
            "max_loss_pct": 3.0
        },
        "take_profit": {
            "long_atr_multiplier": 3.0,
            "short_atr_multiplier": 2.0
        },
        "trading_hours": {
            "enabled": True,
            "start_hour": TRADE_START_HOUR,
            "end_hour": TRADE_END_HOUR
        },
        "daily_limits": {
            "max_daily_loss_inr": 1000.0,
            "max_daily_trades": 20,
            "max_consecutive_losses": 5
        },
        "account": {
            "start_balance_inr": START_BALANCE_INR,
            "min_balance_inr": 5000.0,
            "max_drawdown_pct": 20.0
        }
    }

# =============================================================================
# CONFIG HELPERS
# =============================================================================

def load_config():
    if not os.path.exists(CONFIG_FILE):
        cfg = get_default_config()
        save_config(cfg)
        return cfg
    with open(CONFIG_FILE) as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=4)

# =============================================================================
# USDT / INR RATE FETCHER
# =============================================================================

_rate_cache = {"rate": None, "fetched_at": 0}

def get_usdt_inr_rate():
    cfg = load_config()["usdt_inr"]

    if not cfg["auto_fetch"]:
        return cfg["manual_rate"]

    now = time.time()
    if _rate_cache["rate"] and (now - _rate_cache["fetched_at"]) < cfg["cache_seconds"]:
        return _rate_cache["rate"]

    sources = [
        ("https://open.er-api.com/v6/latest/USD",             lambda d: d["rates"]["INR"]),
        ("https://api.frankfurter.app/latest?from=USD&to=INR", lambda d: d["rates"]["INR"]),
    ]

    for url, extractor in sources:
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                rate = float(extractor(r.json()))
                _rate_cache["rate"] = rate
                _rate_cache["fetched_at"] = now
                logger.info("USDT/INR rate fetched: %s", rate)
                return rate
        except Exception as e:
            logger.warning("Rate fetch failed (%s): %s", url, e)

    fallback = cfg["manual_rate"]
    logger.warning("Using manual USDT/INR rate: %s", fallback)
    return fallback

# =============================================================================
# BINANCE PUBLIC API
# =============================================================================

class BinanceAPI:
    ENDPOINTS = [
        "https://api.binance.com",
        "https://api1.binance.com",
        "https://api2.binance.com",
        "https://api3.binance.com",
        "https://data.binance.com",
    ]

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        self._best = None

    def _get(self, path, params, timeout=10):
        if self._best:
            order = [self._best] + [e for e in self.ENDPOINTS if e != self._best]
        else:
            order = self.ENDPOINTS
        for ep in order:
            try:
                r = self.session.get(ep + path, params=params, timeout=timeout)
                if r.status_code == 200:
                    self._best = ep
                    return r.json()
                logger.warning("Binance %s -> %s", ep, r.status_code)
            except Exception as e:
                logger.warning("Binance %s error: %s", ep, e)
        return None

    def price(self, symbol="BTCUSDT"):
        d = self._get("/api/v3/ticker/price", {"symbol": symbol})
        return float(d["price"]) if d else None

    def klines(self, symbol="BTCUSDT", interval="5m", limit=350):
        return self._get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})

_binance = BinanceAPI()

# =============================================================================
# UT BOT LOGIC — pure Python, no pandas
# =============================================================================

def _rolling_mean(values, period):
    result = [None] * len(values)
    for i in range(period - 1, len(values)):
        result[i] = sum(values[i - period + 1: i + 1]) / period
    return result

def fetch_candles():
    raw = _binance.klines()
    if not raw:
        return []
    candles = []
    for r in raw:
        candles.append({
            "time":  int(r[0]),
            "open":  float(r[1]),
            "high":  float(r[2]),
            "low":   float(r[3]),
            "close": float(r[4]),
        })
    return candles

def calc_utbot(candles, keyvalue, atr_period):
    n     = len(candles)
    tr    = [c["high"] - c["low"] for c in candles]
    atr   = _rolling_mean(tr, atr_period)
    nLoss = [keyvalue * (a if a is not None else 0) for a in atr]

    stops = [candles[0]["close"]]
    pos   = [0]

    for i in range(1, n):
        ps  = stops[-1]
        src = candles[i]["close"]
        s1  = candles[i - 1]["close"]
        nl  = nLoss[i]
        if src > ps and s1 > ps:
            ns = max(ps, src - nl)
        elif src < ps and s1 < ps:
            ns = min(ps, src + nl)
        else:
            ns = src - nl if src > ps else src + nl
        stops.append(ns)
        if s1 < ps and src > ps:
            pos.append(1)
        elif s1 > ps and src < ps:
            pos.append(-1)
        else:
            pos.append(pos[-1])

    return {"stops": stops, "pos": pos}

def get_signal():
    candles = fetch_candles()
    if not candles:
        return {"signal": "No Data", "price": 0, "atr": 0, "utbot_stop": 0, "atr_avg": 0}

    ut1 = calc_utbot(candles, 2, 1)
    ut2 = calc_utbot(candles, 2, 300)

    price = candles[-1]["close"]
    sig1  = ut1["pos"][-1]
    sig2  = ut2["pos"][-1]
    stop1 = ut1["stops"][-1]
    stop2 = ut2["stops"][-1]

    cfg    = load_config()
    period = cfg["cooldown"]["atr_avg_period"]

    tr14    = [c["high"] - c["low"] for c in candles]
    atr14   = _rolling_mean(tr14, 14)
    atr_now = next((v for v in reversed(atr14) if v is not None), 0.0)

    valid_atrs = [v for v in atr14 if v is not None]
    if valid_atrs:
        atr_avg = sum(valid_atrs[-period:]) / min(period, len(valid_atrs))
    else:
        atr_avg = atr_now

    signal     = "Hold"
    utbot_stop = price

    if sig2 == 1:
        signal     = "Buy"
        utbot_stop = stop2
    if sig1 == -1:
        signal     = "Sell"
        utbot_stop = stop1

    return {
        "signal":     signal,
        "price":      price,
        "atr":        atr_now,
        "utbot_stop": utbot_stop,
        "atr_avg":    atr_avg,
    }

# =============================================================================
# RISK & COOLDOWN
# =============================================================================

def compute_cooldown(atr_now, atr_avg):
    cfg  = load_config()["cooldown"]
    base = cfg["base_seconds"]
    lo   = cfg["min_seconds"]
    hi   = cfg["max_seconds"]
    if atr_avg and atr_avg > 0:
        secs = base * (atr_now / atr_avg)
    else:
        secs = base
    return int(max(lo, min(hi, secs)))

def calc_sl(entry, side, atr, utbot_stop):
    cfg  = load_config()["stop_loss"]
    mult = cfg["atr_multiplier"]
    pct  = cfg["max_loss_pct"] / 100
    if side == "LONG":
        sl_atr = entry - atr * mult
        sl_pct = entry * (1 - pct)
        sl_can = max(sl_atr, sl_pct)
        return round(max(sl_can, utbot_stop), 2)
    else:
        sl_atr = entry + atr * mult
        sl_pct = entry * (1 + pct)
        sl_can = min(sl_atr, sl_pct)
        return round(min(sl_can, utbot_stop), 2)

def calc_tp(entry, side, atr):
    cfg = load_config()["take_profit"]
    if side == "LONG":
        return round(entry + atr * cfg["long_atr_multiplier"], 2)
    else:
        return round(entry - atr * cfg["short_atr_multiplier"], 2)

# =============================================================================
# DEMO TRADER
# =============================================================================

def empty_state():
    return {
        "balance":        START_BALANCE_INR,
        "open_trade":     None,
        "history":        [],
        "order_log":      [],
        "cooldown_until": 0,
        "daily": {
            "date":               str(datetime.now().date()),
            "trades":             0,
            "loss_inr":           0.0,
            "profit_inr":         0.0,
            "consecutive_losses": 0,
            "peak_balance":       START_BALANCE_INR,
        }
    }

def load_trades():
    if not os.path.exists(TRADES_FILE):
        s = empty_state()
        save_trades(s)
        return s
    with open(TRADES_FILE) as f:
        return json.load(f)

def save_trades(data):
    with open(TRADES_FILE, "w") as f:
        json.dump(data, f, indent=4)

def reset_daily_if_needed(data):
    today = str(datetime.now().date())
    if data["daily"]["date"] != today:
        data["daily"] = {
            "date":               today,
            "trades":             0,
            "loss_inr":           0.0,
            "profit_inr":         0.0,
            "consecutive_losses": 0,
            "peak_balance":       max(data["balance"], data["daily"]["peak_balance"]),
        }
    return data

def can_trade(data):
    cfg   = load_config()
    daily = data["daily"]
    lim   = cfg["daily_limits"]
    acct  = cfg["account"]

    if time.time() < data.get("cooldown_until", 0):
        remaining = int(data["cooldown_until"] - time.time())
        return False, "Cooldown active — %ds remaining" % remaining

    if daily["trades"] >= lim["max_daily_trades"]:
        return False, "Daily trade limit reached"
    if daily["loss_inr"] >= lim["max_daily_loss_inr"]:
        return False, "Daily loss limit reached"
    if daily["consecutive_losses"] >= lim["max_consecutive_losses"]:
        return False, "Max consecutive losses reached"
    if data["balance"] < acct["min_balance_inr"]:
        return False, "Balance below minimum"

    peak = daily["peak_balance"]
    if peak > 0:
        dd = (peak - data["balance"]) / peak * 100
        if dd >= acct["max_drawdown_pct"]:
            return False, "Max drawdown exceeded"

    return True, ""

def close_position(data, price, reason):
    trade      = data["open_trade"]
    rate       = get_usdt_inr_rate()
    entry      = trade["entry_price"]
    amount     = trade["amount"]

    if trade["type"] == "LONG":
        profit_usdt = (price - entry) * amount
    else:
        profit_usdt = (entry - price) * amount

    profit_inr       = round(profit_usdt * rate, 2)
    data["balance"]  = round(data["balance"] + profit_inr, 2)

    record = {
        "type":          trade["type"],
        "entry_price":   entry,
        "exit_price":    price,
        "amount":        amount,
        "profit_usdt":   round(profit_usdt, 4),
        "profit_inr":    profit_inr,
        "balance_after": data["balance"],
        "closed_at":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "exit_reason":   reason,
    }
    data["history"].append(record)

    data["daily"]["trades"] += 1
    if profit_inr < 0:
        data["daily"]["loss_inr"]           += abs(profit_inr)
        data["daily"]["consecutive_losses"] += 1
    else:
        data["daily"]["profit_inr"]         += profit_inr
        data["daily"]["consecutive_losses"]  = 0

    if data["balance"] > data["daily"]["peak_balance"]:
        data["daily"]["peak_balance"] = data["balance"]

    data["open_trade"] = None
    return record

def update_trade(sig):
    signal     = sig["signal"].capitalize()
    price      = sig["price"]
    atr        = sig["atr"]
    atr_avg    = sig["atr_avg"]
    utbot_stop = sig["utbot_stop"]

    data       = load_trades()
    data       = reset_daily_if_needed(data)
    cfg        = load_config()
    rate       = get_usdt_inr_rate()

    trade      = data.get("open_trade")
    action_msg = ""
    closed_rec = None

    # Check SL / TP on open trade
    if trade:
        sl  = trade.get("stop_loss")
        tp  = trade.get("tp_price")
        hit = None

        if trade["type"] == "LONG":
            if sl and price <= sl:
                hit = ("SL", price)
            elif tp and price >= tp:
                hit = ("TP1", price)
        else:
            if sl and price >= sl:
                hit = ("SL", price)
            elif tp and price <= tp:
                hit = ("TP1", price)

        if hit:
            reason, hit_price = hit
            closed_rec  = close_position(data, hit_price, reason + " Hit")
            icon        = "🛑" if reason == "SL" else "✅"
            action_msg  = "%s %s HIT @ $%.2f | P/L: Rs%.2f" % (icon, reason, hit_price, closed_rec["profit_inr"])
            cd_secs     = compute_cooldown(atr, atr_avg)
            data["cooldown_until"] = time.time() + cd_secs
            logger.info("Cooldown set: %ds", cd_secs)
            trade = None

    trade = data.get("open_trade")

    if signal == "Hold":
        if not action_msg:
            action_msg = "Holding — waiting for signal"

    elif signal in ("Buy", "Sell"):
        side = "LONG" if signal == "Buy" else "SHORT"

        if trade and trade["type"] != side:
            closed_rec  = close_position(data, price, "Opposite Signal")
            action_msg += "Closed %s @ $%.2f P/L:Rs%.2f | " % (trade["type"], price, closed_rec["profit_inr"])
            cd_secs     = compute_cooldown(atr, atr_avg)
            data["cooldown_until"] = time.time() + cd_secs
            trade = None

        trade = data.get("open_trade")

        if trade and trade["type"] == side:
            action_msg = "Already in %s — ignoring repeat signal" % side
        else:
            ok, reason = can_trade(data)
            if not ok:
                action_msg = "Blocked: %s" % reason
            else:
                lot = cfg["lot_size"]["btc_amount"]
                sl  = calc_sl(price, side, atr, utbot_stop)
                tp  = calc_tp(price, side, atr)

                data["open_trade"] = {
                    "type":        side,
                    "entry_price": price,
                    "amount":      lot,
                    "stop_loss":   sl,
                    "tp_price":    tp,
                    "opened_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "atr_entry":   atr,
                }
                icon        = "🟢" if side == "LONG" else "🔴"
                action_msg += "%s OPENED %s @ $%.2f | Lot:%.4f BTC | SL:$%.2f | TP1:$%.2f" % (
                    icon, side, price, lot, sl, tp
                )

    data.setdefault("order_log", []).append({
        "time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "signal": signal,
        "price":  price,
        "action": action_msg,
    })

    save_trades(data)

    open_t  = data.get("open_trade")
    live_pl = None
    if open_t:
        if open_t["type"] == "LONG":
            usdt_pl = (price - open_t["entry_price"]) * open_t["amount"]
        else:
            usdt_pl = (open_t["entry_price"] - price) * open_t["amount"]
        live_pl = round(usdt_pl * rate, 2)

    cd_remaining = max(0, int(data.get("cooldown_until", 0) - time.time()))

    return {
        "price":              price,
        "signal":             signal,
        "action":             action_msg,
        "balance":            data["balance"],
        "usdt_inr_rate":      rate,
        "holding":            open_t is not None,
        "open_trade":         open_t,
        "live_pl_inr":        live_pl,
        "closed_trade":       closed_rec,
        "cooldown_remaining": cd_remaining,
        "atr":                atr,
        "daily":              data["daily"],
        "lot_size":           cfg["lot_size"]["btc_amount"],
    }

def force_close(reason="Force Close"):
    data = load_trades()
    if not data.get("open_trade"):
        return None
    price = _binance.price() or data["open_trade"]["entry_price"]
    rec   = close_position(data, price, reason)
    save_trades(data)
    return rec

# =============================================================================
# TRADING HOURS
# =============================================================================

def trading_allowed():
    cfg = load_config()["trading_hours"]
    if not cfg["enabled"]:
        return True, ""
    h = datetime.now().hour
    if cfg["start_hour"] <= h < cfg["end_hour"]:
        return True, ""
    return False, "Outside trading hours (%d:00-%d:00)" % (cfg["start_hour"], cfg["end_hour"])

# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)

@app.route("/signal")
def signal_route():
    try:
        allowed, reason = trading_allowed()
        sig = get_signal()

        if sig["signal"] == "No Data":
            return jsonify({"error": "No market data"}), 500

        if not allowed:
            data   = load_trades()
            rate   = get_usdt_inr_rate()
            open_t = data.get("open_trade")
            live_pl = None
            if open_t:
                if open_t["type"] == "LONG":
                    usdt_pl = (sig["price"] - open_t["entry_price"]) * open_t["amount"]
                else:
                    usdt_pl = (open_t["entry_price"] - sig["price"]) * open_t["amount"]
                live_pl = round(usdt_pl * rate, 2)
            cd_rem = max(0, int(data.get("cooldown_until", 0) - time.time()))
            return jsonify({
                "price":              sig["price"],
                "signal":             "Hold",
                "action":             "PAUSED: %s" % reason,
                "balance":            data["balance"],
                "usdt_inr_rate":      rate,
                "holding":            open_t is not None,
                "open_trade":         open_t,
                "live_pl_inr":        live_pl,
                "cooldown_remaining": cd_rem,
                "atr":                sig["atr"],
                "trading_allowed":    False,
                "pause_reason":       reason,
                "lot_size":           load_config()["lot_size"]["btc_amount"],
            })

        result = update_trade(sig)
        result["trading_allowed"] = True
        result["pause_reason"]    = None
        return jsonify(result)

    except Exception as e:
        logger.exception("Error in /signal")
        return jsonify({"error": str(e)}), 500

@app.route("/status")
def status_route():
    try:
        data   = load_trades()
        price  = _binance.price()
        rate   = get_usdt_inr_rate()
        open_t = data.get("open_trade")
        live_pl = None
        if open_t and price:
            if open_t["type"] == "LONG":
                usdt_pl = (price - open_t["entry_price"]) * open_t["amount"]
            else:
                usdt_pl = (open_t["entry_price"] - price) * open_t["amount"]
            live_pl = round(usdt_pl * rate, 2)
        cd_rem = max(0, int(data.get("cooldown_until", 0) - time.time()))
        return jsonify({
            "balance":            data["balance"],
            "open_trade":         open_t,
            "current_price":      price,
            "live_pl_inr":        live_pl,
            "usdt_inr_rate":      rate,
            "cooldown_remaining": cd_rem,
            "daily":              data["daily"],
            "total_trades":       len(data.get("history", [])),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/history")
def history_route():
    return jsonify(load_trades().get("history", []))

@app.route("/orders")
def orders_route():
    return jsonify(list(reversed(load_trades().get("order_log", []))))

@app.route("/config", methods=["GET", "POST"])
def config_route():
    if request.method == "GET":
        return jsonify(load_config())
    try:
        save_config(request.json)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

@app.route("/config/lot-size", methods=["POST"])
def set_lot_size():
    try:
        amount = float(request.json.get("btc_amount", 0))
        if amount <= 0:
            return jsonify({"success": False, "error": "Amount must be > 0"}), 400
        cfg = load_config()
        cfg["lot_size"]["btc_amount"] = amount
        save_config(cfg)
        return jsonify({"success": True, "btc_amount": amount})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

@app.route("/config/usdt-rate", methods=["POST"])
def set_usdt_rate():
    try:
        rate = float(request.json.get("rate", 0))
        if rate <= 0:
            return jsonify({"success": False, "error": "Rate must be > 0"}), 400
        cfg = load_config()
        cfg["usdt_inr"]["manual_rate"] = rate
        cfg["usdt_inr"]["auto_fetch"]  = False
        save_config(cfg)
        _rate_cache["rate"] = None
        return jsonify({"success": True, "rate": rate})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

@app.route("/config/usdt-rate/auto", methods=["POST"])
def enable_auto_rate():
    cfg = load_config()
    cfg["usdt_inr"]["auto_fetch"] = True
    save_config(cfg)
    _rate_cache["rate"] = None
    return jsonify({"success": True})

@app.route("/control", methods=["POST"])
def control_route():
    try:
        action = request.json.get("action")
        if action == "force_close":
            rec = force_close("Manual Force Close")
            return jsonify({"success": True, "closed_trade": rec})
        elif action == "reset_cooldown":
            data = load_trades()
            data["cooldown_until"] = 0
            save_trades(data)
            return jsonify({"success": True, "message": "Cooldown cleared"})
        elif action == "reset_daily":
            data = load_trades()
            data["daily"] = {
                "date":               str(datetime.now().date()),
                "trades":             0,
                "loss_inr":           0.0,
                "profit_inr":         0.0,
                "consecutive_losses": 0,
                "peak_balance":       data["balance"],
            }
            save_trades(data)
            return jsonify({"success": True, "message": "Daily stats reset"})
        else:
            return jsonify({"success": False, "error": "Unknown action"}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

@app.route("/ping")
def ping():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

# =============================================================================
# DASHBOARD HTML
# =============================================================================

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>UT Bot Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0f14;color:#e0e0e0;font-family:'Segoe UI',sans-serif;padding:20px}
h1{color:#f0b90b;margin-bottom:20px;font-size:1.4rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}
.card{background:#161a23;border:1px solid #2a2f3e;border-radius:10px;padding:14px}
.card .label{font-size:.72rem;color:#888;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}
.card .value{font-size:1.3rem;font-weight:700;color:#f0b90b}
.green{color:#26a17b!important} .red{color:#e74c3c!important} .grey{color:#888!important}
.section{background:#161a23;border:1px solid #2a2f3e;border-radius:10px;padding:16px;margin-bottom:16px}
.section h2{font-size:.85rem;color:#aaa;margin-bottom:12px;text-transform:uppercase}
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{color:#888;font-weight:600;padding:6px 8px;text-align:left;border-bottom:1px solid #2a2f3e}
td{padding:6px 8px;border-bottom:1px solid #1e2230}
.badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:.7rem;font-weight:700}
.badge.buy{background:#1a3a2a;color:#26a17b}
.badge.sell{background:#3a1a1a;color:#e74c3c}
.badge.hold{background:#2a2a1a;color:#f0b90b}
.action-bar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px;align-items:flex-end}
button{background:#1e2230;border:1px solid #2a2f3e;color:#e0e0e0;padding:8px 14px;border-radius:6px;cursor:pointer;font-size:.8rem}
button:hover{background:#f0b90b;color:#000}
input[type=number]{background:#1e2230;border:1px solid #2a2f3e;color:#e0e0e0;padding:7px 10px;border-radius:6px;font-size:.8rem;width:130px}
label{font-size:.75rem;color:#888;display:block;margin-bottom:4px}
.status-bar{padding:8px 14px;border-radius:6px;margin-bottom:14px;font-size:.82rem;font-weight:600}
.active{background:#1a3a2a;color:#26a17b;border:1px solid #26a17b}
.paused{background:#3a1a1a;color:#e74c3c;border:1px solid #e74c3c}
.cooldown{background:#2a2a1a;color:#f0b90b;border:1px solid #f0b90b}
</style>
</head>
<body>
<h1>UT Bot — Demo Trading Dashboard</h1>
<div id="statusBar" class="status-bar active">Connecting...</div>

<div class="grid">
  <div class="card"><div class="label">BTC Price</div><div class="value" id="price">—</div></div>
  <div class="card"><div class="label">Signal</div><div class="value" id="signal">—</div></div>
  <div class="card"><div class="label">Balance</div><div class="value" id="balance">—</div></div>
  <div class="card"><div class="label">Live P/L</div><div class="value" id="livePl">—</div></div>
  <div class="card"><div class="label">USDT/INR</div><div class="value" id="rate">—</div></div>
  <div class="card"><div class="label">ATR</div><div class="value" id="atr">—</div></div>
  <div class="card"><div class="label">Lot Size</div><div class="value" id="lotSize">—</div></div>
  <div class="card"><div class="label">Cooldown</div><div class="value" id="cooldown">—</div></div>
</div>

<div class="section" id="openTradeSection" style="display:none">
  <h2>Open Position</h2>
  <div class="grid" style="margin-bottom:0">
    <div class="card"><div class="label">Side</div><div class="value" id="tSide">—</div></div>
    <div class="card"><div class="label">Entry</div><div class="value" id="tEntry">—</div></div>
    <div class="card"><div class="label">Stop Loss</div><div class="value red" id="tSl">—</div></div>
    <div class="card"><div class="label">TP1</div><div class="value green" id="tTp">—</div></div>
  </div>
</div>

<div class="section">
  <h2>Controls</h2>
  <div class="action-bar">
    <button onclick="forceClose()">Force Close</button>
    <button onclick="resetCooldown()">Reset Cooldown</button>
    <button onclick="resetDaily()">Reset Daily Stats</button>
  </div>
  <div class="action-bar">
    <div><label>Lot Size (BTC)</label><input type="number" id="lotInput" step="0.0001" min="0.0001" placeholder="0.001"></div>
    <button onclick="setLot()">Set Lot</button>
    <div><label>USDT/INR Rate</label><input type="number" id="rateInput" step="0.1" placeholder="85.0"></div>
    <button onclick="setRate()">Set Rate</button>
    <button onclick="autoRate()">Auto Rate</button>
  </div>
</div>

<div class="section">
  <h2>Daily Stats</h2>
  <div class="grid" style="margin-bottom:0">
    <div class="card"><div class="label">Trades</div><div class="value" id="dTrades">—</div></div>
    <div class="card"><div class="label">Daily Loss</div><div class="value red" id="dLoss">—</div></div>
    <div class="card"><div class="label">Daily Profit</div><div class="value green" id="dProfit">—</div></div>
    <div class="card"><div class="label">Consec. Losses</div><div class="value" id="dConsec">—</div></div>
  </div>
</div>

<div class="section">
  <h2>Order Log</h2>
  <div style="max-height:260px;overflow-y:auto">
    <table>
      <thead><tr><th>Time</th><th>Signal</th><th>Price</th><th>Action</th></tr></thead>
      <tbody id="orderLog"></tbody>
    </table>
  </div>
</div>

<div class="section">
  <h2>Trade History</h2>
  <div style="max-height:260px;overflow-y:auto">
    <table>
      <thead><tr><th>Type</th><th>Entry</th><th>Exit</th><th>P/L INR</th><th>Reason</th><th>Time</th></tr></thead>
      <tbody id="tradeHistory"></tbody>
    </table>
  </div>
</div>

<script>
async function fetchSignal(){
  try{
    const r=await fetch('/signal');
    const d=await r.json();
    if(d.error){setStatus('paused','Error: '+d.error);return;}
    document.getElementById('price').textContent='$'+(d.price||0).toFixed(2);
    const se=document.getElementById('signal');
    se.textContent=d.signal||'—';
    se.className='value '+(d.signal==='Buy'?'green':d.signal==='Sell'?'red':'grey');
    document.getElementById('balance').textContent='Rs'+(d.balance||0).toFixed(2);
    const pl=d.live_pl_inr;
    const pe=document.getElementById('livePl');
    pe.textContent=pl!=null?'Rs'+pl.toFixed(2):'—';
    pe.className='value '+(pl>0?'green':pl<0?'red':'grey');
    document.getElementById('rate').textContent=(d.usdt_inr_rate||0).toFixed(2);
    document.getElementById('atr').textContent='$'+(d.atr||0).toFixed(2);
    document.getElementById('lotSize').textContent=(d.lot_size||0)+' BTC';
    const cd=d.cooldown_remaining||0;
    document.getElementById('cooldown').textContent=cd>0?cd+'s':'—';
    if(!d.trading_allowed){setStatus('paused','PAUSED: '+d.pause_reason);}
    else if(cd>0){setStatus('cooldown','Cooldown: '+cd+'s remaining');}
    else{setStatus('active','Trading Active');}
    const ot=d.open_trade;
    document.getElementById('openTradeSection').style.display=ot?'block':'none';
    if(ot){
      const ts=document.getElementById('tSide');
      ts.textContent=ot.type;ts.className='value '+(ot.type==='LONG'?'green':'red');
      document.getElementById('tEntry').textContent='$'+(ot.entry_price||0).toFixed(2);
      document.getElementById('tSl').textContent='$'+(ot.stop_loss||0).toFixed(2);
      document.getElementById('tTp').textContent='$'+(ot.tp_price||0).toFixed(2);
    }
    if(d.daily){
      document.getElementById('dTrades').textContent=d.daily.trades;
      document.getElementById('dLoss').textContent='Rs'+(d.daily.loss_inr||0).toFixed(2);
      document.getElementById('dProfit').textContent='Rs'+(d.daily.profit_inr||0).toFixed(2);
      document.getElementById('dConsec').textContent=d.daily.consecutive_losses;
    }
  }catch(e){setStatus('paused','Fetch error');}
}
async function fetchOrders(){
  const r=await fetch('/orders');const logs=await r.json();
  document.getElementById('orderLog').innerHTML=logs.slice(0,30).map(l=>
    '<tr><td>'+l.time+'</td><td><span class="badge '+(l.signal||'').toLowerCase()+'">'+l.signal+'</span></td><td>$'+(l.price||0).toFixed(2)+'</td><td style="font-size:.75rem">'+l.action+'</td></tr>'
  ).join('');
}
async function fetchHistory(){
  const r=await fetch('/history');const h=await r.json();
  document.getElementById('tradeHistory').innerHTML=[...h].reverse().slice(0,20).map(t=>
    '<tr><td><span class="badge '+(t.type==='LONG'?'buy':'sell')+'">'+t.type+'</span></td><td>$'+t.entry_price.toFixed(2)+'</td><td>$'+t.exit_price.toFixed(2)+'</td><td class="'+(t.profit_inr>=0?'green':'red')+'">Rs'+t.profit_inr.toFixed(2)+'</td><td style="font-size:.7rem">'+t.exit_reason+'</td><td style="font-size:.7rem">'+t.closed_at+'</td></tr>'
  ).join('');
}
function setStatus(t,m){const e=document.getElementById('statusBar');e.className='status-bar '+t;e.textContent=m;}
async function post(url,body){return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}
async function forceClose(){if(!confirm('Force close?'))return;await post('/control',{action:'force_close'});fetchSignal();}
async function resetCooldown(){await post('/control',{action:'reset_cooldown'});fetchSignal();}
async function resetDaily(){if(!confirm('Reset daily stats?'))return;await post('/control',{action:'reset_daily'});fetchSignal();}
async function setLot(){const v=parseFloat(document.getElementById('lotInput').value);if(!v)return alert('Enter valid amount');await post('/config/lot-size',{btc_amount:v});fetchSignal();}
async function setRate(){const v=parseFloat(document.getElementById('rateInput').value);if(!v)return alert('Enter valid rate');await post('/config/usdt-rate',{rate:v});fetchSignal();}
async function autoRate(){await post('/config/usdt-rate/auto',{});fetchSignal();}
fetchSignal();fetchOrders();fetchHistory();
setInterval(()=>{fetchSignal();fetchOrders();fetchHistory();},10000);
</script>
</body>
</html>"""

# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    print("=" * 55)
    print("UT Bot Started")
    print("Dashboard : http://localhost:%d" % PORT)
    print("=" * 55)
    app.run(host="0.0.0.0", port=PORT, debug=False)
