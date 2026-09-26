import asyncio
import hashlib
import json
import os
import random
import secrets
import sqlite3
import struct
import time
import zlib
from fastapi import Cookie, Depends, FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="VintageCX Pro Global Multi-Asset Terminal")

DB_FILE = "vintagecx.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            balance_usd REAL DEFAULT 50000.0,
            kyc_status TEXT DEFAULT 'VERIFIED',
            id_card_num TEXT DEFAULT 'DEMO-SIM-001',
            is_admin INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            symbol TEXT,
            category TEXT,
            side TEXT,
            lots REAL,
            units REAL,
            entry_price REAL,
            currency TEXT DEFAULT 'USD',
            status TEXT DEFAULT 'OPEN',
            timestamp INTEGER
        )
    """)
    # Seed default Admin
    c.execute("SELECT id FROM users WHERE username = 'admin'")
    if not c.fetchone():
        salt = secrets.token_hex(8)
        pwd_hash = hashlib.pbkdf2_hmac('sha256', b'admin123', salt.encode(), 100000).hex() + ":" + salt
        c.execute("""
            INSERT INTO users (username, email, password_hash, balance_usd, is_admin)
            VALUES ('admin', 'admin@vintagecx.com', ?, 100000.0, 1)
        """, (pwd_hash,))
    conn.commit()
    conn.close()

init_db()

SESSIONS = {}

# Multi-Asset Catalog (Forex, Commodities, Equity & Indices)
INSTRUMENTS = {
    # Forex (Currency: USD, Standard Lot = 100,000 units)
    "EUR/USD": {"price": 1.08450, "lot_size": 100000, "category": "FOREX", "currency": "USD", "precision": 5},
    "GBP/USD": {"price": 1.29820, "lot_size": 100000, "category": "FOREX", "currency": "USD", "precision": 5},
    "USD/JPY": {"price": 152.450, "lot_size": 100000, "category": "FOREX", "currency": "USD", "precision": 3},
    
    # Commodities (Currency: USD)
    "XAU/USD (Gold)": {"price": 2735.60, "lot_size": 100, "category": "COMMODITY", "currency": "USD", "precision": 2},
    "XTI/USD (Crude)": {"price": 71.40, "lot_size": 1000, "category": "COMMODITY", "currency": "USD", "precision": 2},
    "XAG/USD (Silver)": {"price": 32.85, "lot_size": 5000, "category": "COMMODITY", "currency": "USD", "precision": 3},
    
    # Indices & Indian Equity (Converted via live simulation feed)
    "NIFTY 50": {"price": 24850.00, "lot_size": 25, "category": "INDEX", "currency": "INR", "precision": 2},
    "BANKNIFTY": {"price": 51200.00, "lot_size": 15, "category": "INDEX", "currency": "INR", "precision": 2},
    "RELIANCE": {"price": 2980.50, "lot_size": 250, "category": "EQUITY", "currency": "INR", "precision": 2}
}

USD_INR_RATE = 84.00

# High-resolution PNG Icon Generator
def generate_png_icon():
    w, h = 512, 512
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            dx = (x - 256) / 256
            dy = (y - 256) / 256
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < 0.90:
                if 0.82 <= dist <= 0.88:
                    r, g, b, a = int(90 + 50 * dx), int(49 + 180 * (dy + 1) / 2), 244, 255
                elif abs(dx) + abs(dy) < 0.35 and dy > -0.2:
                    r, g, b, a = 0, 255, 163, 255
                else:
                    r, g, b, a = 11, 14, 20, 255
            else:
                r, g, b, a = 0, 0, 0, 0
            raw.extend([r, g, b, a])
    comp = zlib.compress(raw, level=6)
    png = bytearray(b"\x89PNG\r\n\x1a\n")
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    png.extend(struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF))
    png.extend(struct.pack(">I", len(comp)) + b"IDAT" + comp + struct.pack(">I", zlib.crc32(b"IDAT" + comp) & 0xFFFFFFFF))
    png.extend(struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF))
    return bytes(png)

DYNAMIC_ICON_PNG = generate_png_icon()

@app.get("/icon-512.png")
@app.get("/icon.png")
def get_icon():
    return Response(content=DYNAMIC_ICON_PNG, media_type="image/png")

@app.get("/icon-192.png")
def get_icon_192():
    return Response(content=DYNAMIC_ICON_PNG, media_type="image/png")

@app.get("/manifest.json")
def manifest():
    return JSONResponse(content={
        "name": "VintageCX Global Terminal",
        "short_name": "VintageCX",
        "id": "/",
        "start_url": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0B0E14",
        "theme_color": "#5A31F4",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}
        ]
    })

@app.get("/sw.js")
def service_worker():
    return Response(content="self.addEventListener('fetch', function(e) {});", media_type="application/javascript")

def get_current_user(session_token: str = Cookie(default=None)):
    if session_token and session_token in SESSIONS:
        return SESSIONS[session_token]
    return None

class RegisterPayload(BaseModel):
    username: str
    email: str
    password: str

class LoginPayload(BaseModel):
    username: str
    password: str

class TopupPayload(BaseModel):
    amount_usd: float = Field(gt=0)

class EnterMarketPayload(BaseModel):
    symbol: str
    side: str
    order_type: str
    lots: float = Field(gt=0)
    limit_price: float = Field(default=0.0)

class ExitPositionPayload(BaseModel):
    position_id: int

@app.post("/api/auth/register")
def register(data: RegisterPayload):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    salt = secrets.token_hex(8)
    pwd_hash = hashlib.pbkdf2_hmac('sha256', data.password.encode(), salt.encode(), 100000).hex() + ":" + salt
    try:
        c.execute("INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)", (data.username, data.email, pwd_hash))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Username or email already exists")
    conn.close()
    return {"status": "SUCCESS"}

@app.post("/api/auth/login")
def login(data: LoginPayload, response: Response):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, username, email, password_hash, balance_usd, is_admin FROM users WHERE username = ?", (data.username,))
    row = c.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=400, detail="Invalid username or password")
    
    uid, uname, uemail, stored_hash, bal_usd, is_adm = row
    pwd_part, salt = stored_hash.split(":")
    check_hash = hashlib.pbkdf2_hmac('sha256', data.password.encode(), salt.encode(), 100000).hex()
    if pwd_part != check_hash:
        raise HTTPException(status_code=400, detail="Invalid username or password")
    
    token = secrets.token_urlsafe(32)
    user_info = {"id": uid, "username": uname, "email": uemail, "balance_usd": bal_usd, "is_admin": bool(is_adm)}
    SESSIONS[token] = user_info
    response.set_cookie(key="session_token", value=token, httponly=True, max_age=86400*7)
    return {"status": "SUCCESS", "user": user_info}

@app.post("/api/auth/logout")
def logout(response: Response, session_token: str = Cookie(default=None)):
    if session_token in SESSIONS:
        del SESSIONS[session_token]
    response.delete_cookie("session_token")
    return {"status": "LOGGED_OUT"}

@app.get("/api/user/me")
def get_user_me(user: dict = Depends(get_current_user)):
    if not user:
        return {"authenticated": False}
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT balance_usd FROM users WHERE id = ?", (user["id"],))
    row = c.fetchone()
    if row:
        user["balance_usd"] = row[0]
        user["balance_inr"] = round(row[0] * USD_INR_RATE, 2)
    
    c.execute("SELECT id, symbol, category, side, lots, units, entry_price, currency FROM positions WHERE user_id = ? AND status = 'OPEN'", (user["id"],))
    pos_rows = c.fetchall()
    conn.close()

    positions = []
    total_unrealized_usd = 0.0

    for r in pos_rows:
        pid, sym, cat, side, lots, units, entry_p, curr = r
        ltp = INSTRUMENTS.get(sym, {}).get("price", entry_p)
        diff = (ltp - entry_p) if side == "BUY" else (entry_p - ltp)
        raw_pnl = diff * units

        # Standardize PnL to USD for global wallet balancing
        pnl_usd = raw_pnl if curr == "USD" else (raw_pnl / USD_INR_RATE)
        total_unrealized_usd += pnl_usd

        positions.append({
            "id": pid, "symbol": sym, "category": cat, "side": side, "lots": lots, "units": units,
            "entry_price": entry_p, "current_price": ltp, "currency": curr,
            "pnl_usd": round(pnl_usd, 2),
            "pnl_inr": round(pnl_usd * USD_INR_RATE, 2)
        })

    return {
        "authenticated": True,
        "user": user,
        "positions": positions,
        "total_unrealized_usd": round(total_unrealized_usd, 2),
        "total_unrealized_inr": round(total_unrealized_usd * USD_INR_RATE, 2)
    }

# Virtual Demo Capital Reset / Top-Up ($10K, $50K, $100K)
@app.post("/api/wallet/topup")
def topup_balance(data: TopupPayload, user: dict = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET balance_usd = ? WHERE id = ?", (data.amount_usd, user["id"]))
    conn.commit()
    conn.close()
    user["balance_usd"] = data.amount_usd
    return {"status": "SUCCESS", "new_balance_usd": data.amount_usd}

# Enter Market Execution
@app.post("/api/trade/enter")
def enter_market(data: EnterMarketPayload, user: dict = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Please sign in to trade")
    
    sym = data.symbol
    if sym not in INSTRUMENTS:
        raise HTTPException(status_code=404, detail="Instrument not found")
    
    inst = INSTRUMENTS[sym]
    lot_size = inst["lot_size"]
    total_units = round(data.lots * lot_size, 4)
    exec_price = inst["price"] if data.order_type == "MARKET" else data.limit_price
    
    # Margin model: 2% (1:50 leverage) for Forex/Commodities, 10% (1:10 leverage) for Indices
    lev_rate = 0.02 if inst["category"] in ["FOREX", "COMMODITY"] else 0.10
    notional_val = exec_price * total_units
    required_margin_native = notional_val * lev_rate
    required_margin_usd = required_margin_native if inst["currency"] == "USD" else (required_margin_native / USD_INR_RATE)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT balance_usd FROM users WHERE id = ?", (user["id"],))
    bal_usd = c.fetchone()[0]

    if bal_usd < required_margin_usd:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Insufficient Margin. Required: ${required_margin_usd:.2f} USD")
    
    c.execute("UPDATE users SET balance_usd = balance_usd - ? WHERE id = ?", (required_margin_usd, user["id"]))
    c.execute("""
        INSERT INTO positions (user_id, symbol, category, side, lots, units, entry_price, currency, status, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
    """, (user["id"], sym, inst["category"], data.side.upper(), data.lots, total_units, exec_price, inst["currency"], int(time.time())))
    conn.commit()
    conn.close()
    return {"status": "FILLED", "price": exec_price, "units": total_units, "margin_usd": round(required_margin_usd, 2)}

# Exit Market (Square Off)
@app.post("/api/trade/exit")
def exit_market(data: ExitPositionPayload, user: dict = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, symbol, side, lots, units, entry_price, currency FROM positions WHERE id = ? AND user_id = ? AND status = 'OPEN'", (data.position_id, user["id"]))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Position not found or already squared off")
    
    pid, sym, side, lots, units, entry_p, curr = row
    inst = INSTRUMENTS.get(sym, {"price": entry_p, "category": "FOREX"})
    current_p = inst["price"]
    
    diff = (current_p - entry_p) if side == "BUY" else (entry_p - current_p)
    raw_pnl = diff * units
    pnl_usd = raw_pnl if curr == "USD" else (raw_pnl / USD_INR_RATE)

    lev_rate = 0.02 if inst.get("category") in ["FOREX", "COMMODITY"] else 0.10
    orig_margin_usd = ((entry_p * units) * lev_rate) if curr == "USD" else (((entry_p * units) * lev_rate) / USD_INR_RATE)
    refund_usd = orig_margin_usd + pnl_usd

    c.execute("UPDATE positions SET status = 'CLOSED' WHERE id = ?", (pid,))
    c.execute("UPDATE users SET balance_usd = balance_usd + ? WHERE id = ?", (refund_usd, user["id"]))
    conn.commit()
    conn.close()
    return {"status": "CLOSED", "pnl_usd": round(pnl_usd, 2), "pnl_inr": round(pnl_usd * USD_INR_RATE, 2)}

# WebSocket Live Multi-Asset Ticker Feed
@app.websocket("/ws/market-feed")
async def market_data_feed(websocket: WebSocket):
    await websocket.accept()
    current_time = int(time.time())
    candles = {}
    for sym, data in INSTRUMENTS.items():
        p = data["price"]
        candles[sym] = {"time": current_time, "open": p, "high": p, "low": p, "close": p}

    try:
        while True:
            current_time = int(time.time())
            for sym, data in INSTRUMENTS.items():
                cat = data["category"]
                prec = data["precision"]
                
                # Dynamic volatility based on asset class
                if cat == "FOREX":
                    delta = round(random.uniform(-0.0004, 0.0004), prec)
                elif cat == "COMMODITY":
                    delta = round(random.uniform(-0.80, 0.80), prec)
                else:
                    delta = round(random.uniform(-2.5, 2.5), prec)
                
                new_price = max(0.00001, round(data["price"] + delta, prec))
                data["price"] = new_price

                c = candles[sym]
                c["time"] = current_time
                c["high"] = max(c["high"], new_price)
                c["low"] = min(c["low"], new_price)
                c["close"] = new_price

            await websocket.send_text(json.dumps({
                "type": "TICK",
                "market": INSTRUMENTS,
                "candles": candles
            }))
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass

# Responsive Multi-Asset Terminal Frontend
@app.get("/", response_class=HTMLResponse)
def index_view():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>VintageCX Pro | Global Multi-Asset Terminal</title>
        <link rel="manifest" href="/manifest.json">
        <link rel="icon" type="image/png" href="/icon-512.png">
        <meta name="theme-color" content="#0B0E14">
        <script src="https://unpkg.com/lightweight-charts@4.2.1/dist/lightweight-charts.standalone.production.js"></script>
        <style>
            :root {
                --bg-main: #0B0E14;
                --bg-card: #141822;
                --border: #232A36;
                --text-main: #F0F3F6;
                --text-muted: #848E9C;
                --neon: #00FFA3;
                --purple: #5A31F4;
                --green: #089981;
                --red: #F23645;
                --gold: #F59E0B;
            }
            * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
            body { background: var(--bg-main); color: var(--text-main); display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

            header { height: 52px; background: var(--bg-card); border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; padding: 0 12px; flex-shrink: 0; }
            .brand { display: flex; align-items: center; gap: 8px; font-weight: 800; font-size: 1.05rem; }
            .brand img { width: 30px; height: 30px; border-radius: 6px; }

            .currency-toggle { background: #0B0E14; border: 1px solid var(--border); border-radius: 20px; display: flex; padding: 2px; }
            .curr-btn { background: transparent; border: none; color: var(--text-muted); padding: 4px 10px; font-size: 0.72rem; font-weight: 800; border-radius: 14px; cursor: pointer; }
            .curr-btn.active { background: var(--neon); color: #000; }

            .views-container { flex: 1; position: relative; overflow: hidden; display: flex; }
            .view-pane { position: absolute; inset: 0; display: none; flex-direction: column; background: var(--bg-main); overflow-y: auto; }
            .view-pane.active { display: flex; }

            .category-bar { display: flex; gap: 6px; padding: 8px 12px; background: #0E121B; border-bottom: 1px solid var(--border); overflow-x: auto; }
            .cat-tab { background: #181F2C; border: 1px solid var(--border); color: var(--text-muted); padding: 6px 12px; border-radius: 14px; font-size: 0.72rem; font-weight: 700; white-space: nowrap; cursor: pointer; }
            .cat-tab.active { background: var(--purple); color: #fff; border-color: var(--purple); }

            /* Trading Panel */
            .order-container { padding: 14px; max-width: 440px; margin: 0 auto; width: 100%; display: flex; flex-direction: column; gap: 12px; }
            .side-toggle { display: flex; background: #0B0E14; border-radius: 6px; padding: 3px; }
            .side-btn { flex: 1; padding: 10px; border: none; background: transparent; color: var(--text-muted); font-weight: 700; cursor: pointer; border-radius: 4px; font-size: 0.88rem; }
            .side-btn.active.buy { background: var(--green); color: #fff; }
            .side-btn.active.sell { background: var(--red); color: #fff; }

            .calc-box { background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; display: flex; justify-content: space-between; font-size: 0.82rem; }
            .calc-box span { color: var(--text-muted); }
            .calc-box strong { color: var(--neon); }

            .input-group { display: flex; flex-direction: column; gap: 4px; }
            .input-group label { font-size: 0.72rem; color: var(--text-muted); font-weight: 700; text-transform: uppercase; }
            .input-group input, .input-group select { background: #0B0E14; border: 1px solid var(--border); color: #fff; padding: 10px; border-radius: 6px; font-size: 0.95rem; }

            .btn-action { background: var(--green); border: none; color: #fff; font-weight: 800; padding: 14px; border-radius: 6px; cursor: pointer; font-size: 1rem; }
            .btn-action.sell-mode { background: var(--red); }

            .position-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 8px; }
            .pos-head { display: flex; justify-content: space-between; font-weight: 700; }
            .pos-sub { font-size: 0.75rem; color: var(--text-muted); margin-top: 4px; display: flex; justify-content: space-between; }
            .btn-exit { background: #242B35; border: 1px solid var(--border); color: #F23645; font-weight: 700; padding: 8px; border-radius: 4px; cursor: pointer; font-size: 0.8rem; width: 100%; margin-top: 8px; }

            .bottom-nav { height: 54px; background: var(--bg-card); border-top: 1px solid var(--border); display: flex; align-items: center; justify-content: space-around; flex-shrink: 0; }
            .nav-item { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; background: none; border: none; color: var(--text-muted); font-size: 0.72rem; font-weight: 700; cursor: pointer; height: 100%; }
            .nav-item.active { color: var(--neon); }

            @media (min-width: 900px) {
                .bottom-nav { display: none; }
                .view-pane { position: static; display: flex !important; }
                #view-watchlist { width: 320px; border-right: 1px solid var(--border); }
                #view-chart { flex: 1; }
                #view-trade { width: 340px; border-left: 1px solid var(--border); }
                #view-positions { width: 320px; border-left: 1px solid var(--border); }
            }
        </style>
    </head>
    <body>
        <header>
            <div class="brand">
                <img src="/icon-512.png" alt="Logo">
                <span>VINTAGE<span style="color:var(--neon);">CX</span> <span style="font-size:0.65rem; background:var(--purple); padding:2px 5px; border-radius:4px;">PRO</span></span>
            </div>
            
            <!-- Currency Switcher ($ USD / ₹ INR) -->
            <div style="display:flex; align-items:center; gap:8px;">
                <div class="currency-toggle">
                    <button class="curr-btn active" id="btn-usd" onclick="setCurrency('USD')">$ USD</button>
                    <button class="curr-btn" id="btn-inr" onclick="setCurrency('INR')">₹ INR</button>
                </div>
                <div style="text-align:right;">
                    <div style="font-size:0.68rem; color:var(--text-muted);">Sim Wallet</div>
                    <div style="font-weight:800; font-size:0.85rem;" id="wallet-val">$50,000.00</div>
                </div>
            </div>
        </header>

        <div class="views-container">
            <!-- 1. Live Chart -->
            <div class="view-pane active" id="view-chart">
                <div style="padding:10px 14px; background:var(--bg-card); border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center;">
                    <div>
                        <strong id="disp-sym" style="font-size:1.1rem;">EUR/USD</strong>
                        <span style="font-size:0.75rem; color:var(--text-muted); margin-left:6px;" id="disp-cat">FOREX</span>
                    </div>
                    <div id="disp-price" style="font-weight:800; font-size:1.15rem; color:var(--neon);">1.08450</div>
                </div>
                <div id="chart-container" style="flex:1; width:100%; height:100%;"></div>
            </div>

            <!-- 2. Watchlist (Forex, Commodities, Indices) -->
            <div class="view-pane" id="view-watchlist">
                <div class="category-bar">
                    <button class="cat-tab active" onclick="filterWatchlist('ALL', this)">ALL</button>
                    <button class="cat-tab" onclick="filterWatchlist('FOREX', this)">FOREX</button>
                    <button class="cat-tab" onclick="filterWatchlist('COMMODITY', this)">COMMODITIES</button>
                    <button class="cat-tab" onclick="filterWatchlist('INDEX', this)">INDICES</button>
                </div>
                <div id="watchlist-box" style="flex:1; overflow-y:auto;"></div>
            </div>

            <!-- 3. Enter Market (Lots, Units, Leverage) -->
            <div class="view-pane" id="view-trade">
                <div class="order-container">
                    <div class="side-toggle">
                        <button class="side-btn active buy" id="btn-buy" onclick="setSide('BUY')">BUY / LONG</button>
                        <button class="side-btn" id="btn-sell" onclick="setSide('SELL')">SELL / SHORT</button>
                    </div>

                    <div class="calc-box">
                        <div><span>Contract Units:</span> <strong id="calc-units">100,000</strong></div>
                        <div><span>Lot Sizing:</span> <strong id="calc-lotsize">1.00 Lot</strong></div>
                    </div>

                    <div class="input-group">
                        <label>Order Type</label>
                        <select id="order-type">
                            <option value="MARKET">Market Execution (Instant)</option>
                            <option value="LIMIT">Limit Order (Pending)</option>
                        </select>
                    </div>

                    <div class="input-group">
                        <label>Trade Size (Standard Lots)</label>
                        <input type="number" id="trade-lots" value="1.0" min="0.01" step="0.1" oninput="updateCalculations()">
                    </div>

                    <div class="calc-box">
                        <div><span>Required Margin:</span> <strong id="calc-margin">$2,169.00</strong></div>
                        <div><span>Leverage:</span> <strong id="calc-lev">1:50</strong></div>
                    </div>

                    <button class="btn-action" id="btn-submit-order" onclick="submitEnterMarket()">ENTER MARKET</button>
                    <div id="order-feedback" style="font-size:0.85rem; text-align:center;"></div>

                    <!-- Demo Top-Up / Capital Reset -->
                    <div style="margin-top:14px; background:var(--bg-card); border:1px solid var(--border); border-radius:8px; padding:10px;">
                        <div style="font-size:0.72rem; color:var(--text-muted); font-weight:700;">PROP EVALUATION / DEMO TOP-UP</div>
                        <div style="display:flex; gap:6px; margin-top:8px;">
                            <button class="cat-tab" style="flex:1;" onclick="topupWallet(10000)">$10,000</button>
                            <button class="cat-tab" style="flex:1;" onclick="topupWallet(50000)">$50,000</button>
                            <button class="cat-tab" style="flex:1;" onclick="topupWallet(100000)">$100,000</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- 4. Open Positions Terminal -->
            <div class="view-pane" id="view-positions">
                <div style="padding:10px 14px; background:var(--bg-card); border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center;">
                    <span style="font-size:0.75rem; font-weight:700; color:var(--text-muted);">ACTIVE POSITIONS</span>
                    <span id="pos-total-pnl" style="font-weight:800; font-size:0.9rem;">P&L: $0.00</span>
                </div>
                <div id="positions-box" style="padding:12px; flex:1; overflow-y:auto;"></div>
            </div>
        </div>

        <nav class="bottom-nav">
            <button class="nav-item active" onclick="switchTab('chart', this)"><span>Chart</span></button>
            <button class="nav-item" onclick="switchTab('watchlist', this)"><span>Watchlist</span></button>
            <button class="nav-item" onclick="switchTab('trade', this)"><span>Trade</span></button>
            <button class="nav-item" onclick="switchTab('positions', this)"><span>Positions</span></button>
        </nav>

        <script>
            let currentSymbol = "EUR/USD";
            let currentSide = "BUY";
            let activeCurrency = "USD";
            let selectedCategory = "ALL";
            let marketDataCache = {};

            const chartContainer = document.getElementById("chart-container");
            const chart = LightweightCharts.createChart(chartContainer, {
                layout: { background: { color: "#0B0E14" }, textColor: "#848E9C" },
                grid: { vertLines: { color: "#141822" }, horzLines: { color: "#141822" } },
                timeScale: { timeVisible: true, secondsVisible: true }
            });
            const candleSeries = chart.addCandlestickSeries({
                upColor: "#089981", downColor: "#F23645",
                borderDownColor: "#F23645", borderUpColor: "#089981",
                wickDownColor: "#F23645", wickUpColor: "#089981",
            });

            function setCurrency(curr) {
                activeCurrency = curr;
                document.getElementById('btn-usd').className = curr === 'USD' ? 'curr-btn active' : 'curr-btn';
                document.getElementById('btn-inr').className = curr === 'INR' ? 'curr-btn active' : 'curr-btn';
                updateCalculations();
                loadUserPortfolio();
            }

            function switchTab(viewId, el) {
                document.querySelectorAll('.view-pane').forEach(p => p.classList.remove('active'));
                document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
                document.getElementById('view-' + viewId).classList.add('active');
                if (el) el.classList.add('active');
                if (viewId === 'chart') setTimeout(() => chart.resize(chartContainer.clientWidth, chartContainer.clientHeight), 50);
            }

            function setSide(side) {
                currentSide = side;
                document.getElementById('btn-buy').className = side === 'BUY' ? 'side-btn active buy' : 'side-btn';
                document.getElementById('btn-sell').className = side === 'SELL' ? 'side-btn active sell' : 'side-btn';
                const submit = document.getElementById('btn-submit-order');
                submit.className = side === 'BUY' ? 'btn-action' : 'btn-action sell-mode';
                submit.innerText = `${side} ${currentSymbol}`;
            }

            function filterWatchlist(cat, el) {
                selectedCategory = cat;
                document.querySelectorAll('.cat-tab').forEach(b => b.classList.remove('active'));
                if (el) el.classList.add('active');
                renderWatchlist();
            }

            function updateCalculations() {
                const lots = parseFloat(document.getElementById('trade-lots').value) || 1.0;
                const inst = marketDataCache[currentSymbol] || { lot_size: 100000, price: 1.0845, category: "FOREX", currency: "USD" };
                const units = lots * inst.lot_size;
                document.getElementById('calc-units').innerText = units.toLocaleString();
                document.getElementById('calc-lotsize').innerText = `${lots} Lot(s)`;

                const lev = (inst.category === 'FOREX' || inst.category === 'COMMODITY') ? 50 : 10;
                document.getElementById('calc-lev').innerText = `1:${lev}`;

                const marginReqNative = (inst.price * units) / lev;
                const marginReqUsd = inst.currency === "USD" ? marginReqNative : (marginReqNative / 84.0);

                if (activeCurrency === "USD") {
                    document.getElementById('calc-margin').innerText = `$${marginReqUsd.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
                } else {
                    document.getElementById('calc-margin').innerText = `₹${(marginReqUsd * 84.0).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
                }
            }

            function selectInstrument(sym) {
                currentSymbol = sym;
                const inst = marketDataCache[sym];
                if (!inst) return;
                document.getElementById('disp-sym').innerText = sym;
                document.getElementById('disp-cat').innerText = inst.category;
                document.getElementById('disp-price').innerText = inst.price.toFixed(inst.precision);
                setSide(currentSide);
                updateCalculations();
                switchTab('chart', document.querySelector('.nav-item'));
            }

            function renderWatchlist() {
                const box = document.getElementById('watchlist-box');
                box.innerHTML = Object.entries(marketDataCache)
                    .filter(([sym, d]) => selectedCategory === 'ALL' || d.category === selectedCategory)
                    .map(([sym, d]) => `
                        <div style="padding:12px 14px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; cursor:pointer;" onclick="selectInstrument('${sym}')">
                            <div>
                                <strong style="font-size:0.95rem;">${sym}</strong>
                                <div style="font-size:0.7rem; color:var(--text-muted);">${d.category} · 1 Lot = ${d.lot_size.toLocaleString()}</div>
                            </div>
                            <div style="text-align:right;">
                                <div style="font-weight:800; font-size:0.95rem;">${d.currency === 'USD' ? '$' : '₹'}${d.price.toFixed(d.precision)}</div>
                            </div>
                        </div>
                    `).join("");
            }

            // WebSocket Live Multi-Asset Ticker Feed
            const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/market-feed`);
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                marketDataCache = data.market;
                renderWatchlist();

                if (data.candles[currentSymbol]) {
                    candleSeries.update(data.candles[currentSymbol]);
                    const inst = marketDataCache[currentSymbol];
                    if (inst) document.getElementById('disp-price').innerText = inst.price.toFixed(inst.precision);
                }
            };

            async function submitEnterMarket() {
                const lots = parseFloat(document.getElementById('trade-lots').value);
                const orderType = document.getElementById('order-type').value;

                const res = await fetch('/api/trade/enter', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        symbol: currentSymbol,
                        side: currentSide,
                        order_type: orderType,
                        lots: lots,
                        limit_price: 0.0
                    })
                });
                const d = await res.json();
                const feed = document.getElementById('order-feedback');
                if (res.ok) {
                    feed.innerText = `✓ Filled: ${lots} Lot(s) ${currentSymbol} @ ${d.price}`;
                    feed.style.color = "var(--green)";
                    loadUserPortfolio();
                } else {
                    feed.innerText = `✗ ${d.detail}`;
                    feed.style.color = "var(--red)";
                }
            }

            async function exitPosition(posId) {
                const res = await fetch('/api/trade/exit', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({position_id: posId})
                });
                const d = await res.json();
                if (res.ok) {
                    alert(`Closed position! PnL: ${activeCurrency === 'USD' ? '$' + d.pnl_usd : '₹' + d.pnl_inr}`);
                    loadUserPortfolio();
                } else {
                    alert(d.detail || 'Exit failed');
                }
            }

            async function topupWallet(amt) {
                const res = await fetch('/api/wallet/topup', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({amount_usd: amt})
                });
                if (res.ok) {
                    alert(`Simulated Wallet Reset to $${amt.toLocaleString()} USD!`);
                    loadUserPortfolio();
                }
            }

            async function loadUserPortfolio() {
                const res = await fetch('/api/user/me');
                const d = await res.json();
                if (d.authenticated) {
                    const bal = activeCurrency === 'USD' ? `$${d.user.balance_usd.toLocaleString('en-US', {minimumFractionDigits:2})}` : `₹${d.user.balance_inr.toLocaleString('en-IN', {minimumFractionDigits:2})}`;
                    document.getElementById('wallet-val').innerText = bal;

                    const pnlVal = activeCurrency === 'USD' ? d.total_unrealized_usd : d.total_unrealized_inr;
                    const pnlSign = pnlVal >= 0 ? '+' : '';
                    const sym = activeCurrency === 'USD' ? '$' : '₹';
                    document.getElementById('pos-total-pnl').innerText = `P&L: ${pnlSign}${sym}${pnlVal.toFixed(2)}`;
                    document.getElementById('pos-total-pnl').style.color = pnlVal >= 0 ? "var(--green)" : "var(--red)";

                    const pBox = document.getElementById('positions-box');
                    if (!d.positions || d.positions.length === 0) {
                        pBox.innerHTML = `<div style="text-align:center; padding:30px; color:var(--text-muted); font-size:0.85rem;">No open contract positions</div>`;
                    } else {
                        pBox.innerHTML = d.positions.map(p => {
                            const curPnl = activeCurrency === 'USD' ? p.pnl_usd : p.pnl_inr;
                            return `
                                <div class="position-card">
                                    <div class="pos-head">
                                        <span>${p.symbol} <span style="font-size:0.75rem; color:${p.side === 'BUY' ? 'var(--green)' : 'var(--red)'};">${p.side} (${p.lots} Lots)</span></span>
                                        <span style="color:${curPnl >= 0 ? 'var(--green)' : 'var(--red)'};">${curPnl >= 0 ? '+' : ''}${sym}${curPnl.toFixed(2)}</span>
                                    </div>
                                    <div class="pos-sub">
                                        <span>Avg Entry: ${p.entry_price}</span>
                                        <span>Current LTP: ${p.current_price}</span>
                                    </div>
                                    <button class="btn-exit" onclick="exitPosition(${p.id})">EXIT MARKET (SQUARE OFF)</button>
                                </div>
                            `;
                        }).join("");
                    }
                }
            }
            setInterval(loadUserPortfolio, 2000);
            loadUserPortfolio();
        </script>
    </body>
    </html>
    """
