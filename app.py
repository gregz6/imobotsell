import os, time, json, sqlite3, signal, sys, math
import requests

# -------- CONFIG via variables d'environnement --------
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]       # ex: -1001234567890
PAIR_IDS  = [p.strip() for p in os.getenv("PAIR_IDS","").split(",") if p.strip()]  # ex: base:0x007b...
THRESHOLD_USD = float(os.getenv("THRESHOLD_USD","250"))
RED_UNIT_USD  = float(os.getenv("RED_UNIT_USD","250"))  # 1 🔴 par 250$
POLL_SECONDS  = int(os.getenv("POLL_SECONDS","6"))
BASESCAN_API_KEY = os.getenv("BASESCAN_API_KEY","")     # crée la clé sur basescan.org

DB_PATH = "seen.sqlite"
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # ERC-20 Transfer

session = requests.Session()

def tg_send(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
    r = session.post(url, data=data, timeout=15)
    r.raise_for_status()

def setup_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS seen_trades (
        id TEXT PRIMARY KEY,
        ts INTEGER
    )""")
    con.commit()
    return con

def mark_seen(con, tid):
    try:
        con.execute("INSERT INTO seen_trades (id, ts) VALUES (?, strftime('%s','now'))", (tid,))
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False

# ---------- Dexscreener helpers ----------
def ds_fetch_trades(pair_id, limit=50):
    url = f"https://api.dexscreener.com/latest/dex/trades/{pair_id}?limit={limit}"
    r = session.get(url, timeout=15)
    r.raise_for_status()
    return r.json().get("trades", [])

def ds_fetch_pair_info(pair_id):
    # pair_id format: "base:0xPAIR"
    chain, pair_addr = pair_id.split(":")
    url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair_addr}"
    r = session.get(url, timeout=15)
    r.raise_for_status()
    pairs = r.json().get("pairs", [])
    if not pairs:
        return None
    p = pairs[0]
    # On prend baseToken comme étant IMO (c'est le cas sur la plupart des paires IMO/USDC)
    base = p.get("baseToken", {})
    quote = p.get("quoteToken", {})
    dex = p.get("dexId") or "?"
    return {
        "pairAddress": p.get("pairAddress"),
        "dexId": dex,
        "baseToken": {
            "symbol": base.get("symbol") or "?",
            "address": (base.get("address") or "").lower(),
            "decimals": int(base.get("decimals") or 18),
        },
        "quoteToken": {
            "symbol": quote.get("symbol") or "?",
            "address": (quote.get("address") or "").lower(),
            "decimals": int(quote.get("decimals") or 18),
        }
    }

# ---------- BaseScan helpers ----------
def bs_tx_receipt(txhash):
    url = "https://api.basescan.org/api"
    params = {"module":"proxy","action":"eth_getTransactionReceipt","txhash":txhash,"apikey":BASESCAN_API_KEY}
    r = session.get(url, params=params, timeout=15)
    r.raise_for_status()
    js = r.json()
    return js.get("result") or {}

def bs_token_balance(token_addr, wallet_addr):
    if not token_addr or not wallet_addr:
        return None
    url = "https://api.basescan.org/api"
    params = {
        "module":"account","action":"tokenbalance",
        "contractaddress":token_addr,
        "address":wallet_addr,
        "tag":"latest","apikey":BASESCAN_API_KEY
    }
    r = session.get(url, params=params, timeout=15)
    r.raise_for_status()
    js = r.json()
    if js.get("status") == "1":
        try:
            return int(js.get("result"))
        except Exception:
            return None
    return None

def find_seller_address_from_tx(txhash, token_addr, pair_addr):
    rec = bs_tx_receipt(txhash)
    logs = rec.get("logs") or []
    pair_addr = (pair_addr or "").lower()
    token_addr = (token_addr or "").lower()
    for lg in logs:
        if (lg.get("address","").lower() == token_addr) and (lg.get("topics") and lg["topics"][0].lower()==TRANSFER_TOPIC0):
            if len(lg["topics"]) >= 3:
                to = "0x"+lg["topics"][2][-40:]
                from_addr = "0x"+lg["topics"][1][-40:]
                if to.lower() == pair_addr:
                    return from_addr.lower()
    return None

def red_bullets(value_usd):
    n = max(1, math.floor(float(value_usd) / RED_UNIT_USD))
    return "🔴" * n

def fmt_num(x, digits=0):
    if digits==0:
        return ("$" + f"{float(x):,.0f}".replace(",", " "))
    return f"{float(x):,.{digits}f}"

def build_message(tx, pair_addr, value_usd, qty_imo, price_usd, imo_left_fmt):
    link_tx = f"https://basescan.org/tx/{tx}" if tx else ""
    parts = [
        f"🚨 IMO Sell! [Tx]({link_tx})",
        "",
        red_bullets(value_usd),
        "",
        f"Received: {fmt_num(value_usd,0)} USDT",
        f"Sold: {float(qty_imo):,.2f} IMO".replace(",", " "),
        f"Price: {float(price_usd):.4f}",
        "",
        f"IMO left on address: {imo_left_fmt} IMO",
    ]
    return "\n".join(parts)

def main():
    if not PAIR_IDS:
        print("ERROR: set PAIR_IDS", file=sys.stderr); sys.exit(1)

    con = setup_db()
    pair_infos = {}
    for pid in PAIR_IDS:
        info = ds_fetch_pair_info(pid)
        if not info:
            print(f"Cannot fetch pair info for {pid}", file=sys.stderr); sys.exit(1)
        pair_infos[pid] = info

    running = True
    def stop(*_): 
        nonlocal running; running=False; print("Stopping...", flush=True)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print("Monitoring:", PAIR_IDS, flush=True)

    while running:
        try:
            for pid in PAIR_IDS:
                trades = ds_fetch_trades(pid, limit=50)
                trades.sort(key=lambda t: int(t.get("blockTimestamp") or 0))
                info = pair_infos[pid]
                pair_addr = info["pairAddress"]
                dex = info["dexId"]
                imo_addr = info["baseToken"]["address"]
                imo_dec  = info["baseToken"]["decimals"]

                for t in trades:
                    tid = t.get("tradeId") or t.get("txId") or ""
                    if not tid: 
                        continue
                    if not mark_seen(con, tid):
                        continue

                    side = (t.get("side") or "").lower()
                    if side != "sell":
                        continue

                    value_usd = float(t.get("volumeUsd") or 0.0)
                    if value_usd < THRESHOLD_USD:
                        continue

                    qty = t.get("amountToken0") or "0"
                    try:
                        qty = float(qty)
                    except Exception:
                        qty = 0.0
                    price = float(t.get("priceUsd") or 0.0)
                    tx = t.get("txId") or ""

                    seller = None
                    if BASESCAN_API_KEY and tx and imo_addr and pair_addr:
                        try:
                            seller = find_seller_address_from_tx(tx, imo_addr, pair_addr)
                        except Exception:
                            seller = None

                    imo_left_fmt = "?"
                    if seller and BASESCAN_API_KEY:
                        try:
                            raw_bal = bs_token_balance(imo_addr, seller)
                            if raw_bal is not None:
                                bal = raw_bal / (10 ** imo_dec)
                                imo_left_fmt = f"{bal:,.2f}".replace(",", " ")
                        except Exception:
                            pass

                    msg = build_message(tx, pair_addr, value_usd, qty, price, imo_left_fmt)
                    try:
                        tg_send(msg)
                    except Exception as e:
                        print("Telegram send error:", repr(e), file=sys.stderr)

        except Exception as e:
            print("Loop error:", repr(e), file=sys.stderr)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
