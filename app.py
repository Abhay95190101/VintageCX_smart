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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

app = FastAPI(title="VintageCX Pro Institutional Terminal")

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
            balance REAL DEFAULT 100000.0,
            kyc_status TEXT DEFAULT 'UNVERIFIED',
            id_card_num TEXT DEFAULT '',
            is_admin INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type TEXT,
            amount REAL,
            status TEXT,
            timestamp INTEGER
        )
    """)
    # Positions Table
    c.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            symbol TEXT,
            side TEXT,
            lots INTEGER,
            units INTEGER,
            entry_price REAL,
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
            INSERT INTO users (username, email, password_hash, balance, kyc_status, is_admin)
            VALUES ('admin', 'admin@vintagecx.com', ?, 10000000.0, 'VERIFIED', 1)
        """, (pwd_hash,))
    conn.commit()
    conn.close()

init_db()

SESSIONS = {}

# Instruments with standardized Lot Sizes
INSTRUMENTS = {
    "NIFTY 50": {"price": 24850.20, "lot_size": 25, "type": "INDEX"},
    "BANKNIFTY": {"price": 51220.40, "lot_size": 15, "type": "INDEX"},
    "RELIANCE": {"price": 2980.50, "lot_size": 250, "type": "F&O"},
    "TCS": {"price": 4120.00, "lot_size": 175, "type": "F&O"},
    "TATAMOTORS": {"price": 995.80, "lot_size": 700, "type": "F&O"},
    "INFY": {"price": 1890.25, "lot_size": 400, "type": "F&O"},
}

MARKET_MODE = "AUTO"
MANUAL_TARGETS = {sym: data["price"] for sym, data in INSTRUMENTS.items()}

# --- Icon generator ---
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
        "name": "VintageCX Pro Terminal",
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

class FundPayload(BaseModel):
    amount: float = Field(gt=0)

class KYCPayload(BaseModel):
    id_card_num: str

class AdminMarketPayload(BaseModel):
    mode: str
    targets: dict = {}

# Position & Order Schemas
class EnterMarketPayload(BaseModel):
    symbol: str
    side: str  # BUY or SELL
    order_type: str  # MARKET or LIMIT
    lots: int = Field(gt=0)
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
    c.execute("SELECT id, username, email, password_hash, balance, kyc_status, is_admin FROM users WHERE username = ?", (data.username,))
    row = c.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=400, detail="Invalid username or password")
    
    uid, uname, uemail, stored_hash, bal, kyc, is_adm = row
    pwd_part, salt = stored_hash.split(":")
    check_hash = hashlib.pbkdf2_hmac('sha256', data.password.encode(), salt.encode(), 100000).hex()
    if pwd_part != check_hash:
        raise HTTPException(status_code=400, detail="Invalid username or password")
    
    token = secrets.token_urlsafe(32)
    user_info = {"id": uid, "username": uname, "email": uemail, "balance": bal, "kyc_status": kyc, "is_admin": bool(is_adm)}
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
    c.execute("SELECT balance, kyc_status FROM users WHERE id = ?", (user["id"],))
    row = c.fetchone()
    if row:
        user["balance"], user["kyc_status"] = row
    # Fetch active open positions
    c.execute("SELECT id, symbol, side, lots, units, entry_price, timestamp FROM positions WHERE user_id = ? AND status = 'OPEN'", (user["id"],))
    pos_rows = c.fetchall()
    conn.close()
    
    positions = []
    total_unrealized_pnl = 0.0
    for r in pos_rows:
        pid, sym, side, lots, units, entry_p, tstamp = r
        ltp = INSTRUMENTS.get(sym, {}).get("price", entry_p)
        pnl = (ltp - entry_p) * units if side == "BUY" else (entry_p - ltp) * units
        total_unrealized_pnl += pnl
        positions.append({
            "id": pid, "symbol": sym, "side": side, "lots": lots, "units": units,
            "entry_price": entry_p, "current_price": ltp, "pnl": round(pnl, 2)
        })
    
    return {
        "authenticated": True,
        "user": user,
        "positions": positions,
        "total_unrealized_pnl": round(total_unrealized_pnl, 2)
    }

# --- Enter Market (Open Position) ---
@app.post("/api/trade/enter")
def enter_market(data: EnterMarketPayload, user: dict = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Please login to place trades")
    
    sym = data.symbol.upper()
    if sym not in INSTRUMENTS:
        raise HTTPException(status_code=404, detail="Symbol not found")
    
    inst = INSTRUMENTS[sym]
    lot_size = inst["lot_size"]
    total_units = data.lots * lot_size
    execution_price = inst["price"] if data.order_type == "MARKET" else data.limit_price
    
    # Margin requirement (10% leverage buffer)
    required_margin = round((execution_price * total_units) * 0.10, 2)
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE id = ?", (user["id"],))
    bal = c.fetchone()[0]
    
    if bal < required_margin:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Insufficient Margin. Required: ₹{required_margin:.2f}")
    
    c.execute("UPDATE users SET balance = balance - ? WHERE id = ?", (required_margin, user["id"]))
    c.execute("""
        INSERT INTO positions (user_id, symbol, side, lots, units, entry_price, status, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?)
    """, (user["id"], sym, data.side.upper(), data.lots, total_units, execution_price, int(time.time())))
    conn.commit()
    conn.close()
    return {"status": "FILLED", "price": execution_price, "units": total_units, "margin_used": required_margin}

# --- Exit Market (Square Off Position) ---
@app.post("/api/trade/exit")
def exit_market(data: ExitPositionPayload, user: dict = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, symbol, side, lots, units, entry_price FROM positions WHERE id = ? AND user_id = ? AND status = 'OPEN'", (data.position_id, user["id"]))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Position not found or already closed")
    
    pid, sym, side, lots, units, entry_p = row
    current_p = INSTRUMENTS.get(sym, {}).get("price", entry_p)
    pnl = (current_p - entry_p) * units if side == "BUY" else (entry_p - current_p) * units
    original_margin = (entry_p * units) * 0.10
    
    refund_amount = original_margin + pnl
    c.execute("UPDATE positions SET status = 'CLOSED' WHERE id = ?", (pid,))
    c.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (refund_amount, user["id"]))
    conn.commit()
    conn.close()
    return {"status": "CLOSED", "pnl": round(pnl, 2), "exit_price": current_p}

# --- Market WebSocket ---
@app.websocket("/ws/market-feed")
async def market_data_feed(websocket: WebSocket):
    await websocket.accept()
    current_time = int(time.time())
    candles = {}
    for sym in INSTRUMENTS:
        p = INSTRUMENTS[sym]["price"]
        candles[sym] = {"time": current_time, "open": p, "high": p, "low": p, "close": p}

    try:
        while True:
            current_time = int(time.time())
            for sym, data in INSTRUMENTS.items():
                if MARKET_MODE == "MANUAL":
                    target = MANUAL_TARGETS.get(sym, data["price"])
                    diff = target - data["price"]
                    step = round(diff * 0.4, 2)
                    new_price = max(1.0, round(data["price"] + step, 2))
                else:
                    tick = round(random.uniform(-1.5, 1.5), 2)
                    new_price = max(1.0, round(data["price"] + tick, 2))
                
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

# --- Front-end Terminal ---
@app.get("/", response_class=HTMLResponse)
def index_view():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>VintageCX Pro Terminal</title>
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
            }
            * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
            body { background: var(--bg-main); color: var(--text-main); display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

            header { height: 50px; background: var(--bg-card); border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; padding: 0 14px; flex-shrink: 0; }
            .brand { display: flex; align-items: center; gap: 8px; font-weight: 800; font-size: 1.1rem; }
            .brand img { width: 28px; height: 28px; border-radius: 6px; }

            .views-container { flex: 1; position: relative; overflow: hidden; display: flex; }
            .view-pane { position: absolute; inset: 0; display: none; flex-direction: column; background: var(--bg-main); overflow-y: auto; }
            .view-pane.active { display: flex; }

            /* Trading Form Panel */
            .order-container { padding: 16px; max-width: 440px; margin: 0 auto; width: 100%; display: flex; flex-direction: column; gap: 12px; }
            .side-toggle { display: flex; background: #0B0E14; border-radius: 6px; padding: 3px; }
            .side-btn { flex: 1; padding: 10px; border: none; background: transparent; color: var(--text-muted); font-weight: 700; cursor: pointer; border-radius: 4px; }
            .side-btn.active.buy { background: var(--green); color: #fff; }
            .side-btn.active.sell { background: var(--red); color: #fff; }

            .calc-row { background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; display: flex; justify-content: space-between; font-size: 0.85rem; }
            .calc-val { color: var(--neon); font-weight: 700; }

            .input-group { display: flex; flex-direction: column; gap: 5px; }
            .input-group label { font-size: 0.72rem; color: var(--text-muted); font-weight: 700; text-transform: uppercase; }
            .input-group input, .input-group select { background: #0B0E14; border: 1px solid var(--border); color: #fff; padding: 10px; border-radius: 6px; font-size: 0.95rem; }

            .btn-enter { background: var(--green); border: none; color: #fff; font-weight: 800; padding: 14px; border-radius: 6px; cursor: pointer; font-size: 1rem; margin-top: 6px; }
            .btn-enter.sell-mode { background: var(--red); }

            /* Positions List */
            .position-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 8px; }
            .pos-head { display: flex; justify-content: space-between; font-weight: 700; }
            .pos-details { display: flex; justify-content: space-between; color: var(--text-muted); font-size: 0.8rem; margin: 6px 0; }
            .btn-exit { background: #242B35; border: 1px solid var(--border); color: #F23645; font-weight: 700; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-size: 0.78rem; width: 100%; margin-top: 4px; }
            .btn-exit:hover { background: #F23645; color: #fff; }

            /* Bottom Nav */
            .bottom-nav { height: 52px; background: var(--bg-card); border-top: 1px solid var(--border); display: flex; align-items: center; justify-content: space-around; flex-shrink: 0; }
            .nav-item { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; background: none; border: none; color: var(--text-muted); font-size: 0.72rem; font-weight: 700; cursor: pointer; height: 100%; }
            .nav-item.active { color: var(--neon); }

            @media (min-width: 900px) {
                .bottom-nav { display: none; }
                .view-pane { position: static; display: flex !important; }
                #view-watchlist { width: 280px; border-right: 1px solid var(--border); }
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
                <span>VINTAGE<span style="color:var(--neon);">CX</span></span>
            </div>
            <div style="text-align:right;">
                <div style="font-size:0.7rem; color:var(--text-muted);">Available Margin</div>
                <div style="font-weight:700;" id="user-margin">₹1,00,000.00</div>
            </div>
        </header>

        <div class="views-container">
            <!-- 1. Live Chart -->
            <div class="view-pane active" id="view-chart">
                <div style="padding:10px 14px; background:var(--bg-card); border-bottom:1px solid var(--border); display:flex; justify-content:space-between;">
                    <div>
                        <strong id="disp-sym">NIFTY 50</strong>
                        <span style="font-size:0.75rem; color:var(--text-muted); margin-left:6px;" id="disp-type">INDEX</span>
                    </div>
                    <div id="disp-price" style="font-weight:700; color:var(--neon);">₹24,850.20</div>
                </div>
                <div id="chart-container" style="flex:1; width:100%; height:100%;"></div>
            </div>

            <!-- 2. Watchlist -->
            <div class="view-pane" id="view-watchlist">
                <div style="padding:10px 14px; border-bottom:1px solid var(--border); font-size:0.75rem; font-weight:700; color:var(--text-muted);">INSTRUMENTS & LOTS</div>
                <div id="watchlist-box"></div>
            </div>

            <!-- 3. Enter Market / Order Form -->
            <div class="view-pane" id="view-trade">
                <div class="order-container">
                    <div class="side-toggle">
                        <button class="side-btn active buy" id="btn-side-buy" onclick="setSide('BUY')">ENTER LONG (BUY)</button>
                        <button class="side-btn" id="btn-side-sell" onclick="setSide('SELL')">ENTER SHORT (SELL)</button>
                    </div>

                    <!-- Lot & Unit Dynamic Calculator -->
                    <div class="calc-row">
                        <div>Contract Units: <strong id="calc-units" class="calc-val">25</strong></div>
                        <div>Lot Size: <strong id="calc-lotsize" class="calc-val">25 / Lot</strong></div>
                    </div>

                    <div class="input-group">
                        <label>Order Type</label>
                        <select id="order-type" onchange="toggleOrderPrice()">
                            <option value="MARKET">Market Order (Enter Immediately)</option>
                            <option value="LIMIT">Limit Order (Specify Entry Price)</option>
                        </select>
                    </div>

                    <div class="input-group">
                        <label>Number of Lots</label>
                        <input type="number" id="trade-lots" value="1" min="1" oninput="updateCalculations()">
                    </div>

                    <div class="input-group" id="limit-price-group" style="display:none;">
                        <label>Entry Limit Price (₹)</label>
                        <input type="number" id="trade-price" value="0.0" step="0.05">
                    </div>

                    <button class="btn-enter" id="enter-btn" onclick="executeEnterMarket()">ENTER MARKET</button>
                    <div id="trade-feedback" style="font-size:0.85rem; text-align:center;"></div>
                </div>
            </div>

            <!-- 4. Active Positions & Exit Market Terminal -->
            <div class="view-pane" id="view-positions">
                <div style="padding:10px 14px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between;">
                    <span style="font-size:0.75rem; font-weight:700; color:var(--text-muted);">OPEN POSITIONS</span>
                    <span id="pos-total-pnl" style="font-weight:700; font-size:0.85rem;">P&L: ₹0.00</span>
                </div>
                <div id="positions-container" style="padding:12px;"></div>
            </div>
        </div>

        <nav class="bottom-nav">
            <button class="nav-item active" onclick="switchTab('chart', this)"><span>Chart</span></button>
            <button class="nav-item" onclick="switchTab('watchlist', this)"><span>Watchlist</span></button>
            <button class="nav-item" onclick="switchTab('trade', this)"><span>Enter Market</span></button>
            <button class="nav-item" onclick="switchTab('positions', this)"><span>Positions</span></button>
        </nav>

        <script>
            let currentSymbol = "NIFTY 50";
            let currentSide = "BUY";
            let currentLotSize = 25;
            let currentPrice = 24850.20;

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

            function seedData(basePrice) {
                const now = Math.floor(Date.now() / 1000);
                const candles = [];
                for (let i = 40; i > 0; i--) {
                    let t = now - (i * 2);
                    let open = basePrice + Math.sin(i) * 5;
                    let high = open + Math.random() * 3;
                    let low = open - Math.random() * 3;
                    let close = open + (Math.random() - 0.5) * 3;
                    candles.push({ time: t, open: open, high: high, low: low, close: close });
                }
                candleSeries.setData(candles);
            }
            seedData(currentPrice);

            window.addEventListener('resize', () => {
                chart.resize(chartContainer.clientWidth, chartContainer.clientHeight);
            });

            function switchTab(viewId, el) {
                document.querySelectorAll('.view-pane').forEach(p => p.classList.remove('active'));
                document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
                document.getElementById('view-' + viewId).classList.add('active');
                if (el) el.classList.add('active');
                if (viewId === 'chart') setTimeout(() => chart.resize(chartContainer.clientWidth, chartContainer.clientHeight), 50);
            }

            function setSide(side) {
                currentSide = side;
                document.getElementById('btn-side-buy').className = side === 'BUY' ? 'side-btn active buy' : 'side-btn';
                document.getElementById('btn-side-sell').className = side === 'SELL' ? 'side-btn active sell' : 'side-btn';
                const btn = document.getElementById('enter-btn');
                btn.className = side === 'BUY' ? 'btn-enter' : 'btn-enter sell-mode';
                btn.innerText = `ENTER MARKET (${side} ${currentSymbol})`;
            }

            function toggleOrderPrice() {
                const type = document.getElementById('order-type').value;
                document.getElementById('limit-price-group').style.display = type === 'LIMIT' ? 'flex' : 'none';
            }

            function updateCalculations() {
                const lots = parseInt(document.getElementById('trade-lots').value) || 1;
                const units = lots * currentLotSize;
                document.getElementById('calc-units').innerText = units;
                document.getElementById('calc-lotsize').innerText = `${currentLotSize} / Lot`;
            }

            function selectInstrument(sym, price, lotSize, type) {
                currentSymbol = sym;
                currentPrice = price;
                currentLotSize = lotSize;
                document.getElementById('disp-sym').innerText = sym;
                document.getElementById('disp-type').innerText = type;
                document.getElementById('disp-price').innerText = `₹${price.toFixed(2)}`;
                setSide(currentSide);
                updateCalculations();
                seedData(price);
                switchTab('chart', document.querySelector('.nav-item'));
            }

            // WebSocket Market Feed
            const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/market-feed`);
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                const wBox = document.getElementById('watchlist-box');
                wBox.innerHTML = Object.entries(data.market).map(([sym, item]) => `
                    <div style="padding:12px 14px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; cursor:pointer;" onclick="selectInstrument('${sym}', ${item.price}, ${item.lot_size}, '${item.type}')">
                        <div>
                            <strong>${sym}</strong>
                            <div style="font-size:0.7rem; color:var(--text-muted);">${item.lot_size} Units/Lot · ${item.type}</div>
                        </div>
                        <div style="text-align:right;">
                            <div style="font-weight:700;">₹${item.price.toFixed(2)}</div>
                        </div>
                    </div>
                `).join("");

                if (data.candles[currentSymbol]) {
                    candleSeries.update(data.candles[currentSymbol]);
                    document.getElementById('disp-price').innerText = `₹${data.candles[currentSymbol].close.toFixed(2)}`;
                }
            };

            // Enter Market Order
            async function executeEnterMarket() {
                const lots = parseInt(document.getElementById('trade-lots').value);
                const type = document.getElementById('order-type').value;
                const limitPrice = parseFloat(document.getElementById('trade-price').value) || 0.0;

                const res = await fetch('/api/trade/enter', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        symbol: currentSymbol,
                        side: currentSide,
                        order_type: type,
                        lots: lots,
                        limit_price: limitPrice
                    })
                });
                const d = await res.json();
                const feed = document.getElementById('trade-feedback');
                if (res.ok) {
                    feed.innerText = `✓ Position Opened: ${d.units} Units @ ₹${d.price}`;
                    feed.style.color = "var(--green)";
                    loadUserPositions();
                } else {
                    feed.innerText = `✗ Rejected: ${d.detail}`;
                    feed.style.color = "var(--red)";
                }
            }

            // Exit Market / Square Off Position
            async function exitPosition(posId) {
                const res = await fetch('/api/trade/exit', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({position_id: posId})
                });
                const d = await res.json();
                if (res.ok) {
                    alert(`Position Squared Off! P&L: ₹${d.pnl}`);
                    loadUserPositions();
                } else {
                    alert(d.detail || 'Exit failed');
                }
            }

            // Load Open Positions
            async function loadUserPositions() {
                const res = await fetch('/api/user/me');
                const d = await res.json();
                if (d.authenticated) {
                    document.getElementById('user-margin').innerText = `₹${d.user.balance.toLocaleString('en-IN', {minimumFractionDigits: 2})}`;
                    const posBox = document.getElementById('positions-container');
                    document.getElementById('pos-total-pnl').innerText = `P&L: ${d.total_unrealized_pnl >= 0 ? '+' : ''}₹${d.total_unrealized_pnl.toFixed(2)}`;
                    document.getElementById('pos-total-pnl').style.color = d.total_unrealized_pnl >= 0 ? "var(--green)" : "var(--red)";

                    if (!d.positions || d.positions.length === 0) {
                        posBox.innerHTML = `<div style="text-align:center; padding:20px; color:var(--text-muted); font-size:0.85rem;">No active positions open</div>`;
                    } else {
                        posBox.innerHTML = d.positions.map(p => `
                            <div class="position-card">
                                <div class="pos-head">
                                    <span>${p.symbol} <span style="font-size:0.75rem; color:${p.side === 'BUY' ? 'var(--green)' : 'var(--red)'};">${p.side} (${p.lots} Lots / ${p.units} Units)</span></span>
                                    <span style="color:${p.pnl >= 0 ? 'var(--green)' : 'var(--red)'};">${p.pnl >= 0 ? '+' : ''}₹${p.pnl.toFixed(2)}</span>
                                </div>
                                <div class="pos-details">
                                    <span>Avg: ₹${p.entry_price.toFixed(2)}</span>
                                    <span>LTP: ₹${p.current_price.toFixed(2)}</span>
                                </div>
                                <button class="btn-exit" onclick="exitPosition(${p.id})">EXIT MARKET (SQUARE OFF)</button>
                            </div>
                        `).join("");
                    }
                }
            }
            setInterval(loadUserPositions, 2000);
            loadUserPositions();
        </script>
    </body>
    </html>
    """
