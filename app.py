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

from engine import Order, OrderSide, OrderType, PaperTradingEngine

app = FastAPI(title="VintageCX Pro Institutional Terminal")

# --- SQLite Persistent Database Setup ---
DB_FILE = "vintagecx.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Users Table
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
    # Transactions / Banking Ledger
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
    # Seed default Admin Account if not exists
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

# --- Active User Session Store ---
SESSIONS = {}  # session_token -> user_dict

# --- Market State & Admin Override Engine ---
INSTRUMENTS = {
    "RELIANCE": {"price": 2980.50, "open": 2975.00, "high": 2995.00, "low": 2968.00},
    "TCS": {"price": 4120.00, "open": 4110.00, "high": 4145.00, "low": 4100.00},
    "INFY": {"price": 1890.25, "open": 1880.00, "high": 1905.00, "low": 1875.00},
    "HDFCBANK": {"price": 1640.10, "open": 1635.00, "high": 1652.00, "low": 1630.00},
    "TATAMOTORS": {"price": 995.80, "open": 988.00, "high": 1005.00, "low": 985.00},
}

MARKET_MODE = "AUTO"  # Options: "AUTO" or "MANUAL"
MANUAL_TARGETS = {sym: data["price"] for sym, data in INSTRUMENTS.items()}

# In-memory Trading Engines per user
USER_ENGINES = {}

def get_user_engine(user_id: int, starting_balance: float):
    if user_id not in USER_ENGINES:
        USER_ENGINES[user_id] = PaperTradingEngine(initial_balance=starting_balance)
    return USER_ENGINES[user_id]

# --- Dynamic Icon Generation (512x512 PNG) ---
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

# --- Auth Dependency Helper ---
def get_current_user(session_token: str = Cookie(default=None)):
    if session_token and session_token in SESSIONS:
        return SESSIONS[session_token]
    return None

# --- Auth Pydantic Schemas ---
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
    mode: str  # "AUTO" or "MANUAL"
    targets: dict = {}

# --- Auth & Account APIs ---
@app.post("/api/auth/register")
def register(data: RegisterPayload):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    salt = secrets.token_hex(8)
    pwd_hash = hashlib.pbkdf2_hmac('sha256', data.password.encode(), salt.encode(), 100000).hex() + ":" + salt
    try:
        c.execute("""
            INSERT INTO users (username, email, password_hash)
            VALUES (?, ?, ?)
        """, (data.username, data.email, pwd_hash))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Username or email already exists")
    conn.close()
    return {"status": "SUCCESS", "message": "Registered successfully"}

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
    user_info = {
        "id": uid, "username": uname, "email": uemail,
        "balance": bal, "kyc_status": kyc, "is_admin": bool(is_adm)
    }
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
    # Fetch freshest balance & KYC from DB
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT balance, kyc_status FROM users WHERE id = ?", (user["id"],))
    row = c.fetchone()
    conn.close()
    if row:
        user["balance"], user["kyc_status"] = row
    return {"authenticated": True, "user": user}

@app.post("/api/user/kyc")
def submit_kyc(data: KYCPayload, user: dict = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET kyc_status = 'VERIFIED', id_card_num = ? WHERE id = ?", (data.id_card_num, user["id"]))
    conn.commit()
    conn.close()
    user["kyc_status"] = "VERIFIED"
    return {"status": "SUCCESS", "message": "KYC Verified successfully"}

@app.post("/api/wallet/deposit")
def deposit_funds(data: FundPayload, user: dict = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (data.amount, user["id"]))
    c.execute("INSERT INTO transactions (user_id, type, amount, status, timestamp) VALUES (?, 'DEPOSIT', ?, 'SUCCESS', ?)",
              (user["id"], data.amount, int(time.time())))
    conn.commit()
    c.execute("SELECT balance FROM users WHERE id = ?", (user["id"],))
    new_bal = c.fetchone()[0]
    conn.close()
    user["balance"] = new_bal
    # Also credit active trading engine
    eng = get_user_engine(user["id"], new_bal)
    eng.cash_balance += data.amount
    return {"status": "SUCCESS", "new_balance": new_bal}

@app.post("/api/wallet/withdraw")
def withdraw_funds(data: FundPayload, user: dict = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if user["kyc_status"] != "VERIFIED":
        raise HTTPException(status_code=400, detail="KYC Verification required for withdrawals")
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE id = ?", (user["id"],))
    bal = c.fetchone()[0]
    if bal < data.amount:
        conn.close()
        raise HTTPException(status_code=400, detail="Insufficient wallet balance")
    
    c.execute("UPDATE users SET balance = balance - ? WHERE id = ?", (data.amount, user["id"]))
    c.execute("INSERT INTO transactions (user_id, type, amount, status, timestamp) VALUES (?, 'WITHDRAWAL', ?, 'SUCCESS', ?)",
              (user["id"], data.amount, int(time.time())))
    conn.commit()
    new_bal = bal - data.amount
    conn.close()
    user["balance"] = new_bal
    eng = get_user_engine(user["id"], new_bal)
    eng.cash_balance = max(0.0, eng.cash_balance - data.amount)
    return {"status": "SUCCESS", "new_balance": new_bal}

# --- Admin God-Mode Market Override API ---
@app.post("/api/admin/market-control")
def admin_market_control(data: AdminMarketPayload, user: dict = Depends(get_current_user)):
    global MARKET_MODE, MANUAL_TARGETS
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin authorization required")
    
    MARKET_MODE = data.mode
    if data.targets:
        for sym, price in data.targets.items():
            if sym in INSTRUMENTS:
                MANUAL_TARGETS[sym] = float(price)
                INSTRUMENTS[sym]["price"] = float(price)
    return {"status": "SUCCESS", "mode": MARKET_MODE, "targets": MANUAL_TARGETS}

# --- Trading Orders API ---
class OrderRequest(BaseModel):
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int = Field(gt=0)
    price: float = Field(default=0.0, ge=0.0)

@app.post("/api/order")
def place_order(req: OrderRequest, user: dict = Depends(get_current_user)):
    user_id = user["id"] if user else 0
    starting_bal = user["balance"] if user else 100000.0
    eng = get_user_engine(user_id, starting_bal)
    
    sym = req.symbol.upper()
    if sym not in INSTRUMENTS:
        raise HTTPException(status_code=404, detail="Instrument not found")
    
    order = Order(symbol=sym, side=req.side, order_type=req.order_type, quantity=req.quantity, price=req.price)
    result = eng.execute_order(order, INSTRUMENTS[sym]["price"])
    return result

# --- WebSocket Feed (Includes Admin Market Control Mechanics) ---
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
                    # Drift or lock toward the admin target set in god mode
                    target = MANUAL_TARGETS.get(sym, data["price"])
                    diff = target - data["price"]
                    step = round(diff * 0.4, 2)
                    new_price = max(1.0, round(data["price"] + step, 2))
                else:
                    # Natural market fluctuations
                    tick = round(random.uniform(-1.2, 1.2), 2)
                    new_price = max(1.0, round(data["price"] + tick, 2))
                
                data["price"] = new_price
                c = candles[sym]
                c["time"] = current_time
                c["high"] = max(c["high"], new_price)
                c["low"] = min(c["low"], new_price)
                c["close"] = new_price

            prices = {k: v["price"] for k, v in INSTRUMENTS.items()}
            await websocket.send_text(json.dumps({
                "type": "TICK",
                "mode": MARKET_MODE,
                "market": INSTRUMENTS,
                "candles": candles
            }))
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass

# --- Front-end Terminal with Admin, KYC & Fund Modals ---
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
                --border-color: #232A36;
                --text-primary: #F0F3F6;
                --text-muted: #848E9C;
                --neon-green: #00FFA3;
                --neon-purple: #5A31F4;
                --trade-green: #089981;
                --trade-red: #F23645;
                --gold: #F59E0B;
            }
            * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
            body { background: var(--bg-main); color: var(--text-primary); display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

            /* Header */
            header { height: 52px; background: var(--bg-card); border-bottom: 1px solid var(--border-color); display: flex; align-items: center; justify-content: space-between; padding: 0 12px; z-index: 10; flex-shrink: 0; }
            .header-left { display: flex; align-items: center; gap: 8px; }
            .menu-toggle { background: transparent; border: none; color: #fff; font-size: 1.4rem; cursor: pointer; padding: 4px; }
            .logo-icon { width: 30px; height: 30px; border-radius: 6px; animation: logoIntro 1.2s cubic-bezier(0.16, 1, 0.3, 1) forwards; }
            @keyframes logoIntro { 0% { transform: scale(0.2); opacity: 0; } 100% { transform: scale(1); opacity: 1; filter: drop-shadow(0 0 10px rgba(0, 255, 163, 0.45)); } }
            .brand-name { font-weight: 800; font-size: 1.05rem; }
            .header-right { display: flex; align-items: center; gap: 10px; }

            .btn-auth { background: var(--neon-purple); border: none; color: #fff; font-weight: 700; font-size: 0.75rem; padding: 6px 12px; border-radius: 6px; cursor: pointer; }
            .user-chip { display: flex; align-items: center; gap: 6px; background: #1C222E; padding: 4px 8px; border-radius: 6px; font-size: 0.78rem; cursor: pointer; }

            /* Dynamic Panes & Bottom Nav */
            .views-container { flex: 1; position: relative; overflow: hidden; display: flex; }
            .view-pane { position: absolute; inset: 0; display: none; flex-direction: column; background: var(--bg-main); overflow-y: auto; }
            .view-pane.active { display: flex; }

            .chart-bar { padding: 10px 14px; background: var(--bg-card); border-bottom: 1px solid var(--border-color); display: flex; justify-content: space-between; align-items: center; }
            #chart-container { flex: 1; width: 100%; min-height: 280px; }

            .stock-item { display: flex; justify-content: space-between; padding: 14px 16px; border-bottom: 1px solid var(--border-color); cursor: pointer; }
            .stock-item:hover { background: #1C222E; }

            /* Trading Form */
            .trade-box { padding: 16px; display: flex; flex-direction: column; gap: 12px; max-width: 450px; margin: 0 auto; width: 100%; }
            .toggle-group { display: flex; border-radius: 6px; overflow: hidden; background: #0B0E14; padding: 2px; }
            .toggle-btn { flex: 1; padding: 10px; border: none; background: transparent; color: var(--text-muted); font-weight: 700; cursor: pointer; border-radius: 4px; font-size: 0.9rem; }
            .toggle-btn.active.buy { background: var(--trade-green); color: white; }
            .toggle-btn.active.sell { background: var(--trade-red); color: white; }
            .input-box { display: flex; flex-direction: column; gap: 5px; }
            .input-box label { font-size: 0.72rem; color: var(--text-muted); font-weight: 600; text-transform: uppercase; }
            .input-box input, .input-box select { background: #0B0E14; border: 1px solid var(--border-color); color: #fff; padding: 12px; border-radius: 6px; outline: none; font-size: 0.95rem; }
            .btn-action { background: var(--trade-green); border: none; color: #fff; font-weight: 800; padding: 14px; border-radius: 6px; cursor: pointer; font-size: 1rem; margin-top: 6px; }
            .btn-action.sell-mode { background: var(--trade-red); }

            /* Mobile Bottom Bar */
            .bottom-nav { height: 56px; background: var(--bg-card); border-top: 1px solid var(--border-color); display: flex; align-items: center; justify-content: space-around; flex-shrink: 0; z-index: 10; }
            .nav-item { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 3px; background: none; border: none; color: var(--text-muted); font-size: 0.72rem; font-weight: 600; cursor: pointer; height: 100%; }
            .nav-item.active { color: var(--neon-green); }

            /* Desktop Adaptations */
            @media (min-width: 900px) {
                .bottom-nav { display: none; }
                .view-pane { position: static; display: flex !important; }
                #view-watchlist { width: 280px; border-right: 1px solid var(--border-color); }
                #view-chart { flex: 1; }
                #view-trade { width: 340px; border-left: 1px solid var(--border-color); }
                #view-portfolio { display: none !important; }
            }

            /* Modals (Auth, KYC, Funds, Admin) */
            .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.7); backdrop-filter: blur(5px); z-index: 500; display: none; align-items: center; justify-content: center; padding: 16px; }
            .modal-card { background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 12px; width: 100%; max-width: 400px; padding: 24px; display: flex; flex-direction: column; gap: 14px; box-shadow: 0 10px 40px rgba(0,0,0,0.8); }
            .modal-title { font-size: 1.1rem; font-weight: 700; display: flex; justify-content: space-between; align-items: center; }
            .modal-close { background: none; border: none; color: var(--text-muted); font-size: 1.3rem; cursor: pointer; }
        </style>
    </head>
    <body>
        <header>
            <div class="header-left">
                <button class="menu-toggle" onclick="toggleDrawer()">☰</button>
                <div style="display:flex; align-items:center; gap:6px;">
                    <img src="/icon-512.png" alt="Logo" class="logo-icon">
                    <span class="brand-name">VINTAGE<span style="color:var(--neon-green)">CX</span></span>
                </div>
            </div>
            <div class="header-right">
                <div id="auth-buttons">
                    <button class="btn-auth" onclick="openModal('auth-modal')">Login / Register</button>
                </div>
                <div id="user-display" style="display:none;" class="user-chip" onclick="toggleDrawer()">
                    <span id="user-display-name">Trader</span>
                    <span style="color:var(--neon-green);" id="user-display-bal">₹1,00,000</span>
                </div>
            </div>
        </header>

        <!-- Slide Drawer: Insights, KYC, Funds & Admin Control -->
        <div class="modal-overlay" id="drawer-overlay" onclick="toggleDrawer()" style="z-index:200;"></div>
        <div style="position:fixed; top:0; left:-320px; width:300px; height:100%; background:var(--bg-card); z-index:201; border-right:1px solid var(--border-color); padding:20px; transition:transform 0.25s; display:flex; flex-direction:column; gap:14px;" id="side-drawer">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <h3 style="font-size:1.1rem;">Trader Dashboard</h3>
                <button class="menu-toggle" onclick="toggleDrawer()">✕</button>
            </div>

            <!-- KYC Status Badge -->
            <div style="background:var(--bg-main); border:1px solid var(--border-color); border-radius:8px; padding:12px;">
                <div style="font-size:0.75rem; color:var(--text-muted); font-weight:700;">KYC STATUS</div>
                <div style="display:flex; justify-content:space-between; align-items:center; margin-top:6px;">
                    <span id="kyc-badge" style="color:var(--gold); font-weight:700; font-size:0.85rem;">UNVERIFIED</span>
                    <button class="btn-auth" style="padding:4px 8px; font-size:0.7rem;" onclick="openModal('kyc-modal')">Verify ID</button>
                </div>
            </div>

            <!-- Wallet Actions -->
            <div style="background:var(--bg-main); border:1px solid var(--border-color); border-radius:8px; padding:12px; display:flex; flex-direction:column; gap:8px;">
                <div style="font-size:0.75rem; color:var(--text-muted); font-weight:700;">WALLET SETTLEMENTS</div>
                <div style="display:flex; gap:8px;">
                    <button class="btn-action" style="flex:1; padding:8px; font-size:0.8rem; margin:0;" onclick="openModal('deposit-modal')">+ Deposit</button>
                    <button class="btn-action" style="flex:1; padding:8px; font-size:0.8rem; margin:0; background:#242B35;" onclick="openModal('withdraw-modal')">Withdraw</button>
                </div>
            </div>

            <!-- Admin Market Fluctuation Controls -->
            <div id="admin-panel-card" style="display:none; background:#1C1326; border:1px solid var(--neon-purple); border-radius:8px; padding:12px;">
                <div style="font-size:0.75rem; color:#A78BFA; font-weight:800;">ADMIN MARKET CONTROLLER</div>
                <p style="font-size:0.72rem; color:var(--text-muted); margin-top:4px;">Toggle manual drift or pump/dump controls.</p>
                <button class="btn-auth" style="width:100%; margin-top:8px; background:var(--neon-purple);" onclick="openModal('admin-modal')">Open God-Mode Control</button>
            </div>

            <button class="btn-auth" style="margin-top:auto; background:#F23645;" onclick="logout()">Logout</button>
        </div>

        <!-- Terminal Workspace -->
        <div class="views-container">
            <div class="view-pane active" id="view-chart">
                <div class="chart-bar">
                    <div>
                        <strong id="selected-sym" style="font-size:1.1rem;">RELIANCE</strong>
                        <span style="color:var(--text-muted); font-size:0.75rem; margin-left:6px;">NSE EQ</span>
                    </div>
                    <div id="sym-price" style="font-weight:700; font-size:1.1rem; color:var(--neon-green);">₹2,980.50</div>
                </div>
                <div id="chart-container"></div>
            </div>

            <div class="view-pane" id="view-watchlist">
                <div style="padding:10px 16px; border-bottom:1px solid var(--border-color); font-size:0.75rem; color:var(--text-muted); font-weight:700;">INSTRUMENTS</div>
                <div id="watchlist-list"></div>
            </div>

            <div class="view-pane" id="view-trade">
                <div class="trade-box">
                    <div class="toggle-group">
                        <button class="toggle-btn active buy" id="tab-buy" onclick="setSide('BUY')">BUY</button>
                        <button class="toggle-btn" id="tab-sell" onclick="setSide('SELL')">SELL</button>
                    </div>
                    <div class="input-box">
                        <label>Order Type</label>
                        <select id="order-type">
                            <option value="MARKET">Market</option>
                            <option value="LIMIT">Limit</option>
                        </select>
                    </div>
                    <div class="input-box">
                        <label>Quantity</label>
                        <input type="number" id="order-qty" value="1" min="1">
                    </div>
                    <button class="btn-action" id="submit-btn" onclick="submitOrder()">BUY RELIANCE</button>
                    <div id="order-feedback" style="font-size:0.85rem; text-align:center;"></div>
                </div>
            </div>

            <div class="view-pane" id="view-portfolio">
                <div style="padding:10px 16px; border-bottom:1px solid var(--border-color); font-size:0.75rem; color:var(--text-muted); font-weight:700;">OPEN POSITIONS</div>
                <div id="positions-list" style="padding:12px;"></div>
            </div>
        </div>

        <!-- Mobile Bottom Tabs -->
        <nav class="bottom-nav">
            <button class="nav-item active" onclick="switchTab('chart', this)"><span>Chart</span></button>
            <button class="nav-item" onclick="switchTab('watchlist', this)"><span>Watchlist</span></button>
            <button class="nav-item" onclick="switchTab('trade', this)"><span>Trade</span></button>
            <button class="nav-item" onclick="switchTab('portfolio', this)"><span>Portfolio</span></button>
        </nav>

        <!-- Modal 1: Login / Register -->
        <div class="modal-overlay" id="auth-modal">
            <div class="modal-card">
                <div class="modal-title">
                    <span id="auth-modal-title">Sign In to VintageCX</span>
                    <button class="modal-close" onclick="closeModal('auth-modal')">✕</button>
                </div>
                <div class="input-box">
                    <label>Username</label>
                    <input type="text" id="auth-username">
                </div>
                <div class="input-box" id="email-box" style="display:none;">
                    <label>Email Address</label>
                    <input type="email" id="auth-email">
                </div>
                <div class="input-box">
                    <label>Password</label>
                    <input type="password" id="auth-password">
                </div>
                <button class="btn-action" id="auth-submit-btn" onclick="handleAuthSubmit()">Login</button>
                <div style="text-align:center; font-size:0.8rem; color:var(--text-muted); cursor:pointer;" onclick="toggleAuthMode()" id="auth-toggle-link">
                    Don't have an account? Register here.
                </div>
            </div>
        </div>

        <!-- Modal 2: KYC Verification -->
        <div class="modal-overlay" id="kyc-modal">
            <div class="modal-card">
                <div class="modal-title">
                    <span>Identity Verification (KYC)</span>
                    <button class="modal-close" onclick="closeModal('kyc-modal')">✕</button>
                </div>
                <p style="font-size:0.8rem; color:var(--text-muted);">Enter your National ID (Aadhaar/PAN/National ID Number) to unlock full withdrawal privileges.</p>
                <div class="input-box">
                    <label>National ID Number</label>
                    <input type="text" id="kyc-id-input" placeholder="e.g. ABCDE1234F">
                </div>
                <button class="btn-action" onclick="submitKYC()">Submit ID for Verification</button>
            </div>
        </div>

        <!-- Modal 3: Deposit Funds -->
        <div class="modal-overlay" id="deposit-modal">
            <div class="modal-card">
                <div class="modal-title">
                    <span>Add Funds to Wallet</span>
                    <button class="modal-close" onclick="closeModal('deposit-modal')">✕</button>
                </div>
                <div class="input-box">
                    <label>Amount (₹)</label>
                    <input type="number" id="deposit-amt" value="50000">
                </div>
                <button class="btn-action" onclick="depositFunds()">Deposit Instantly</button>
            </div>
        </div>

        <!-- Modal 4: Withdraw Funds -->
        <div class="modal-overlay" id="withdraw-modal">
            <div class="modal-card">
                <div class="modal-title">
                    <span>Withdraw Capital</span>
                    <button class="modal-close" onclick="closeModal('withdraw-modal')">✕</button>
                </div>
                <div class="input-box">
                    <label>Amount (₹)</label>
                    <input type="number" id="withdraw-amt" value="10000">
                </div>
                <button class="btn-action" style="background:#F23645;" onclick="withdrawFunds()">Request Withdrawal</button>
            </div>
        </div>

        <!-- Modal 5: Admin Market Controller (God-Mode) -->
        <div class="modal-overlay" id="admin-modal">
            <div class="modal-card" style="max-width:440px;">
                <div class="modal-title">
                    <span>Admin Market Fluctuation Controller</span>
                    <button class="modal-close" onclick="closeModal('admin-modal')">✕</button>
                </div>
                <div class="input-box">
                    <label>Simulation Mode</label>
                    <select id="admin-mode-select">
                        <option value="AUTO">Automatic (Natural Real-Time Drift)</option>
                        <option value="MANUAL">Manual Fluctuation / Price Override</option>
                    </select>
                </div>
                <div class="input-box">
                    <label>Target Price for RELIANCE (₹)</label>
                    <input type="number" id="admin-price-reliance" value="3000.00" step="1">
                </div>
                <div class="input-box">
                    <label>Target Price for TATAMOTORS (₹)</label>
                    <input type="number" id="admin-price-tatamotors" value="1000.00" step="1">
                </div>
                <button class="btn-action" style="background:var(--neon-purple);" onclick="saveAdminControl()">Apply Market Controls</button>
            </div>
        </div>

        <script>
            let isRegisterMode = false;
            let currentUser = null;
            let currentSymbol = "RELIANCE";
            let currentSide = "BUY";

            function openModal(id) { document.getElementById(id).style.display = 'flex'; }
            function closeModal(id) { document.getElementById(id).style.display = 'none'; }
            function toggleDrawer() {
                const drawer = document.getElementById('side-drawer');
                const overlay = document.getElementById('drawer-overlay');
                const isOpen = drawer.style.transform === 'translateX(320px)';
                drawer.style.transform = isOpen ? 'translateX(0px)' : 'translateX(320px)';
                overlay.style.display = isOpen ? 'none' : 'block';
            }

            function toggleAuthMode() {
                isRegisterMode = !isRegisterMode;
                document.getElementById('email-box').style.display = isRegisterMode ? 'flex' : 'none';
                document.getElementById('auth-modal-title').innerText = isRegisterMode ? 'Create VintageCX Account' : 'Sign In to VintageCX';
                document.getElementById('auth-submit-btn').innerText = isRegisterMode ? 'Register' : 'Login';
                document.getElementById('auth-toggle-link').innerText = isRegisterMode ? 'Already have an account? Login' : "Don't have an account? Register here.";
            }

            async function handleAuthSubmit() {
                const u = document.getElementById('auth-username').value;
                const p = document.getElementById('auth-password').value;
                if (isRegisterMode) {
                    const e = document.getElementById('auth-email').value;
                    const res = await fetch('/api/auth/register', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({username: u, email: e, password: p})
                    });
                    const d = await res.json();
                    if (res.ok) { alert("Registration successful! Now logging in..."); toggleAuthMode(); }
                    else { alert(d.detail || 'Registration failed'); }
                } else {
                    const res = await fetch('/api/auth/login', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({username: u, password: p})
                    });
                    const d = await res.json();
                    if (res.ok) { closeModal('auth-modal'); checkAuthStatus(); }
                    else { alert(d.detail || 'Login failed'); }
                }
            }

            async function checkAuthStatus() {
                const res = await fetch('/api/user/me');
                const d = await res.json();
                if (d.authenticated) {
                    currentUser = d.user;
                    document.getElementById('auth-buttons').style.display = 'none';
                    document.getElementById('user-display').style.display = 'flex';
                    document.getElementById('user-display-name').innerText = currentUser.username;
                    document.getElementById('user-display-bal').innerText = `₹${currentUser.balance.toLocaleString('en-IN')}`;
                    document.getElementById('kyc-badge').innerText = currentUser.kyc_status;
                    document.getElementById('kyc-badge').style.color = currentUser.kyc_status === 'VERIFIED' ? 'var(--neon-green)' : 'var(--gold)';
                    if (currentUser.is_admin) {
                        document.getElementById('admin-panel-card').style.display = 'block';
                    }
                }
            }
            checkAuthStatus();

            async function logout() {
                await fetch('/api/auth/logout', {method: 'POST'});
                location.reload();
            }

            async function submitKYC() {
                const idCard = document.getElementById('kyc-id-input').value;
                const res = await fetch('/api/user/kyc', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({id_card_num: idCard})
                });
                if (res.ok) { alert('KYC Verified!'); closeModal('kyc-modal'); checkAuthStatus(); }
            }

            async function depositFunds() {
                const amt = parseFloat(document.getElementById('deposit-amt').value);
                const res = await fetch('/api/wallet/deposit', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({amount: amt})
                });
                if (res.ok) { alert(`Deposited ₹${amt} successfully!`); closeModal('deposit-modal'); checkAuthStatus(); }
            }

            async function withdrawFunds() {
                const amt = parseFloat(document.getElementById('withdraw-amt').value);
                const res = await fetch('/api/wallet/withdraw', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({amount: amt})
                });
                const d = await res.json();
                if (res.ok) { alert(`Withdrawal of ₹${amt} processed!`); closeModal('withdraw-modal'); checkAuthStatus(); }
                else { alert(d.detail || 'Withdrawal failed'); }
            }

            async function saveAdminControl() {
                const mode = document.getElementById('admin-mode-select').value;
                const rel = parseFloat(document.getElementById('admin-price-reliance').value);
                const tata = parseFloat(document.getElementById('admin-price-tatamotors').value);
                const res = await fetch('/api/admin/market-control', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        mode: mode,
                        targets: {"RELIANCE": rel, "TATAMOTORS": tata}
                    })
                });
                if (res.ok) { alert('Market override settings applied live!'); closeModal('admin-modal'); }
            }

            // --- Chart Setup & Layout Switching ---
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

            function switchTab(viewId, el) {
                document.querySelectorAll('.view-pane').forEach(p => p.classList.remove('active'));
                document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
                document.getElementById('view-' + viewId).classList.add('active');
                if (el) el.classList.add('active');
                if (viewId === 'chart') setTimeout(() => chart.resize(chartContainer.clientWidth, chartContainer.clientHeight), 50);
            }

            function setSide(side) {
                currentSide = side;
                document.getElementById("tab-buy").className = side === 'BUY' ? 'toggle-btn active buy' : 'toggle-btn';
                document.getElementById("tab-sell").className = side === 'SELL' ? 'toggle-btn active sell' : 'toggle-btn';
                document.getElementById("submit-btn").className = side === 'BUY' ? 'btn-action' : 'btn-action sell-mode';
                document.getElementById("submit-btn").innerText = `${side} ${currentSymbol}`;
            }

            function selectStock(sym, price) {
                currentSymbol = sym;
                document.getElementById("selected-sym").innerText = sym;
                document.getElementById("sym-price").innerText = `₹${price.toFixed(2)}`;
                setSide(currentSide);
                switchTab('chart', document.querySelector('.nav-item'));
            }

            // WebSocket Live Feed
            const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/market-feed`);
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                const wl = document.getElementById("watchlist-list");
                wl.innerHTML = Object.entries(data.market).map(([sym, item]) => `
                    <div class="stock-item" onclick="selectStock('${sym}', ${item.price})">
                        <div><strong>${sym}</strong><div style="font-size:0.7rem; color:var(--text-muted);">NSE EQ</div></div>
                        <div style="font-weight:700;">₹${item.price.toFixed(2)}</div>
                    </div>
                `).join("");

                if (data.candles[currentSymbol]) {
                    candleSeries.update(data.candles[currentSymbol]);
                    document.getElementById("sym-price").innerText = `₹${data.candles[currentSymbol].close.toFixed(2)}`;
                }
            };

            async function submitOrder() {
                const qty = parseInt(document.getElementById("order-qty").value);
                const res = await fetch("/api/order", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        symbol: currentSymbol,
                        side: currentSide,
                        order_type: document.getElementById("order-type").value,
                        quantity: qty,
                        price: 0.0
                    })
                });
                const result = await res.json();
                const alertEl = document.getElementById("order-feedback");
                alertEl.innerText = result.status === "FILLED" 
                    ? `✓ Executed: ${result.side} ${result.quantity} ${result.symbol} @ ₹${result.execution_price}`
                    : `✗ Rejected: ${result.reason}`;
                alertEl.style.color = result.status === "FILLED" ? "var(--trade-green)" : "var(--trade-red)";
                checkAuthStatus();
            }
        </script>
    </body>
    </html>
    """
