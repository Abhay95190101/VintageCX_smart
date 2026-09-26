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
            balance_usd REAL DEFAULT 50000.0,
            email_verified INTEGER DEFAULT 0,
            kyc_status TEXT DEFAULT 'UNVERIFIED',
            id_card_num TEXT DEFAULT '',
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
            INSERT INTO users (username, email, password_hash, balance_usd, email_verified, kyc_status, is_admin)
            VALUES ('admin', 'admin@vintagecx.com', ?, 100000.0, 1, 'VERIFIED', 1)
        """, (pwd_hash,))
    conn.commit()
    conn.close()

init_db()

SESSIONS = {}

# Multi-Asset Catalog
INSTRUMENTS = {
    "EUR/USD": {"price": 1.08450, "lot_size": 100000, "category": "FOREX", "currency": "USD", "precision": 5},
    "GBP/USD": {"price": 1.29820, "lot_size": 100000, "category": "FOREX", "currency": "USD", "precision": 5},
    "USD/JPY": {"price": 152.450, "lot_size": 100000, "category": "FOREX", "currency": "USD", "precision": 3},
    "XAU/USD (Gold)": {"price": 2735.60, "lot_size": 100, "category": "COMMODITY", "currency": "USD", "precision": 2},
    "XTI/USD (Crude)": {"price": 71.40, "lot_size": 1000, "category": "COMMODITY", "currency": "USD", "precision": 2},
    "NIFTY 50": {"price": 24850.00, "lot_size": 25, "category": "INDEX", "currency": "INR", "precision": 2},
    "BANKNIFTY": {"price": 51200.00, "lot_size": 15, "category": "INDEX", "currency": "INR", "precision": 2},
    "RELIANCE": {"price": 2980.50, "lot_size": 250, "category": "EQUITY", "currency": "INR", "precision": 2}
}

USD_INR_RATE = 84.00
MARKET_MODE = "AUTO"  # "AUTO" or "MANUAL"
MANUAL_TARGETS = {sym: data["price"] for sym, data in INSTRUMENTS.items()}

# PNG Icon Generator
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

class KYCPayload(BaseModel):
    id_card_num: str

class VerifyEmailPayload(BaseModel):
    code: str

class AdminMarketPayload(BaseModel):
    mode: str
    targets: dict = {}

class EnterMarketPayload(BaseModel):
    symbol: str
    side: str
    order_type: str
    lots: float = Field(gt=0)
    limit_price: float = Field(default=0.0)

class ExitPositionPayload(BaseModel):
    position_id: int

# --- Auth APIs ---
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
    return {"status": "SUCCESS", "message": "Verification code sent to email"}

@app.post("/api/auth/verify-email")
def verify_email(data: VerifyEmailPayload, user: dict = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (user["id"],))
    conn.commit()
    conn.close()
    user["email_verified"] = 1
    return {"status": "SUCCESS"}

@app.post("/api/auth/login")
def login(data: LoginPayload, response: Response):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, username, email, password_hash, balance_usd, email_verified, kyc_status, is_admin FROM users WHERE username = ?", (data.username,))
    row = c.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=400, detail="Invalid username or password")
    
    uid, uname, uemail, stored_hash, bal_usd, em_ver, kyc, is_adm = row
    pwd_part, salt = stored_hash.split(":")
    check_hash = hashlib.pbkdf2_hmac('sha256', data.password.encode(), salt.encode(), 100000).hex()
    if pwd_part != check_hash:
        raise HTTPException(status_code=400, detail="Invalid username or password")
    
    token = secrets.token_urlsafe(32)
    user_info = {
        "id": uid, "username": uname, "email": uemail,
        "balance_usd": bal_usd, "email_verified": bool(em_ver),
        "kyc_status": kyc, "is_admin": bool(is_adm)
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
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT balance_usd, email_verified, kyc_status FROM users WHERE id = ?", (user["id"],))
    row = c.fetchone()
    if row:
        user["balance_usd"], user["email_verified"], user["kyc_status"] = row[0], bool(row[1]), row[2]
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
        pnl_usd = raw_pnl if curr == "USD" else (raw_pnl / USD_INR_RATE)
        total_unrealized_usd += pnl_usd

        positions.append({
            "id": pid, "symbol": sym, "category": cat, "side": side, "lots": lots, "units": units,
            "entry_price": entry_p, "current_price": ltp, "currency": curr,
            "pnl_usd": round(pnl_usd, 2), "pnl_inr": round(pnl_usd * USD_INR_RATE, 2)
        })

    return {
        "authenticated": True,
        "user": user,
        "positions": positions,
        "total_unrealized_usd": round(total_unrealized_usd, 2),
        "total_unrealized_inr": round(total_unrealized_usd * USD_INR_RATE, 2)
    }

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
    return {"status": "SUCCESS"}

# Admin Market Override API
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

# Trade Execution
@app.post("/api/trade/enter")
def enter_market(data: EnterMarketPayload, user: dict = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Please login first")
    sym = data.symbol
    if sym not in INSTRUMENTS:
        raise HTTPException(status_code=404, detail="Instrument not found")
    
    inst = INSTRUMENTS[sym]
    total_units = round(data.lots * inst["lot_size"], 4)
    exec_price = inst["price"] if data.order_type == "MARKET" else data.limit_price
    
    lev_rate = 0.02 if inst["category"] in ["FOREX", "COMMODITY"] else 0.10
    required_margin_native = (exec_price * total_units) * lev_rate
    required_margin_usd = required_margin_native if inst["currency"] == "USD" else (required_margin_native / USD_INR_RATE)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT balance_usd FROM users WHERE id = ?", (user["id"],))
    bal_usd = c.fetchone()[0]

    if bal_usd < required_margin_usd:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Insufficient Margin. Need: ${required_margin_usd:.2f} USD")
    
    c.execute("UPDATE users SET balance_usd = balance_usd - ? WHERE id = ?", (required_margin_usd, user["id"]))
    c.execute("""
        INSERT INTO positions (user_id, symbol, category, side, lots, units, entry_price, currency, status, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
    """, (user["id"], sym, inst["category"], data.side.upper(), data.lots, total_units, exec_price, inst["currency"], int(time.time())))
    conn.commit()
    conn.close()
    return {"status": "FILLED", "price": exec_price, "units": total_units, "margin_usd": round(required_margin_usd, 2)}

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
        raise HTTPException(status_code=404, detail="Position not found")
    
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
    return {"status": "CLOSED", "pnl_usd": round(pnl_usd, 2)}

# WebSocket Market Engine
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
                
                if MARKET_MODE == "MANUAL":
                    target = MANUAL_TARGETS.get(sym, data["price"])
                    diff = target - data["price"]
                    step = round(diff * 0.35, prec)
                    new_price = max(0.00001, round(data["price"] + step, prec))
                else:
                    if cat == "FOREX":
                        delta = round(random.uniform(-0.0003, 0.0003), prec)
                    elif cat == "COMMODITY":
                        delta = round(random.uniform(-0.6, 0.6), prec)
                    else:
                        delta = round(random.uniform(-2.0, 2.0), prec)
                    new_price = max(0.00001, round(data["price"] + delta, prec))
                
                data["price"] = new_price
                c = candles[sym]
                c["time"] = current_time
                c["high"] = max(c["high"], new_price)
                c["low"] = min(c["low"], new_price)
                c["close"] = new_price

            await websocket.send_text(json.dumps({
                "type": "TICK",
                "mode": MARKET_MODE,
                "market": INSTRUMENTS,
                "candles": candles
            }))
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass

# Complete Unified Frontend
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
                --gold: #F59E0B;
            }
            * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
            body { background: var(--bg-main); color: var(--text-main); display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

            header { height: 52px; background: var(--bg-card); border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; padding: 0 12px; z-index: 10; flex-shrink: 0; }
            .header-left { display: flex; align-items: center; gap: 8px; }
            .menu-btn { background: transparent; border: none; color: #fff; font-size: 1.4rem; cursor: pointer; padding: 4px; }
            .logo-icon { width: 30px; height: 30px; border-radius: 6px; }
            .brand-name { font-weight: 800; font-size: 1.05rem; }

            .currency-toggle { background: #0B0E14; border: 1px solid var(--border); border-radius: 20px; display: flex; padding: 2px; }
            .curr-btn { background: transparent; border: none; color: var(--text-muted); padding: 4px 10px; font-size: 0.72rem; font-weight: 800; border-radius: 14px; cursor: pointer; }
            .curr-btn.active { background: var(--neon); color: #000; }

            .btn-auth { background: var(--purple); border: none; color: #fff; font-weight: 700; font-size: 0.75rem; padding: 6px 12px; border-radius: 6px; cursor: pointer; }
            .user-badge { display: flex; align-items: center; gap: 6px; background: #1C222E; padding: 4px 8px; border-radius: 6px; font-size: 0.75rem; cursor: pointer; }

            /* Panes */
            .views-container { flex: 1; position: relative; overflow: hidden; display: flex; }
            .view-pane { position: absolute; inset: 0; display: none; flex-direction: column; background: var(--bg-main); overflow-y: auto; }
            .view-pane.active { display: flex; }

            .category-bar { display: flex; gap: 6px; padding: 8px 12px; background: #0E121B; border-bottom: 1px solid var(--border); overflow-x: auto; }
            .cat-tab { background: #181F2C; border: 1px solid var(--border); color: var(--text-muted); padding: 6px 12px; border-radius: 14px; font-size: 0.72rem; font-weight: 700; white-space: nowrap; cursor: pointer; }
            .cat-tab.active { background: var(--purple); color: #fff; border-color: var(--purple); }

            /* Trading Form */
            .order-container { padding: 14px; max-width: 440px; margin: 0 auto; width: 100%; display: flex; flex-direction: column; gap: 12px; }
            .side-toggle { display: flex; background: #0B0E14; border-radius: 6px; padding: 3px; }
            .side-btn { flex: 1; padding: 10px; border: none; background: transparent; color: var(--text-muted); font-weight: 700; cursor: pointer; border-radius: 4px; font-size: 0.88rem; }
            .side-btn.active.buy { background: var(--green); color: #fff; }
            .side-btn.active.sell { background: var(--red); color: #fff; }

            .calc-box { background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; display: flex; justify-content: space-between; font-size: 0.82rem; }
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

            /* Bottom Nav */
            .bottom-nav { height: 54px; background: var(--bg-card); border-top: 1px solid var(--border); display: flex; align-items: center; justify-content: space-around; flex-shrink: 0; }
            .nav-item { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; background: none; border: none; color: var(--text-muted); font-size: 0.72rem; font-weight: 700; cursor: pointer; height: 100%; }
            .nav-item.active { color: var(--neon); }

            /* Modals & Slide Drawer */
            .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.7); backdrop-filter: blur(5px); z-index: 500; display: none; align-items: center; justify-content: center; padding: 16px; }
            .modal-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; width: 100%; max-width: 400px; padding: 22px; display: flex; flex-direction: column; gap: 14px; }
            .modal-title { font-size: 1.1rem; font-weight: 700; display: flex; justify-content: space-between; align-items: center; }
            .modal-close { background: none; border: none; color: var(--text-muted); font-size: 1.3rem; cursor: pointer; }

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
            <div class="header-left">
                <button class="menu-btn" onclick="toggleDrawer()">☰</button>
                <div style="display:flex; align-items:center; gap:6px;">
                    <img src="/icon-512.png" alt="Logo" class="logo-icon">
                    <span class="brand-name">VINTAGE<span style="color:var(--neon);">CX</span></span>
                </div>
            </div>

            <!-- Currency Toggle -->
            <div class="currency-toggle">
                <button class="curr-btn active" id="btn-usd" onclick="setCurrency('USD')">$ USD</button>
                <button class="curr-btn" id="btn-inr" onclick="setCurrency('INR')">₹ INR</button>
            </div>

            <div style="display:flex; align-items:center; gap:8px;">
                <div id="auth-buttons">
                    <button class="btn-auth" onclick="openModal('auth-modal')">Login</button>
                </div>
                <div id="user-display" style="display:none;" class="user-badge" onclick="toggleDrawer()">
                    <span id="user-display-name">User</span>
                    <span style="color:var(--neon);" id="user-display-bal">$50,000</span>
                </div>
            </div>
        </header>

        <!-- Slide Drawer: Profile, KYC, Email & Admin Controls -->
        <div class="modal-overlay" id="drawer-overlay" onclick="toggleDrawer()" style="z-index:200;"></div>
        <div style="position:fixed; top:0; left:-320px; width:300px; height:100%; background:var(--bg-card); z-index:201; border-right:1px solid var(--border); padding:20px; transition:transform 0.25s; display:flex; flex-direction:column; gap:14px;" id="side-drawer">
            <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--border); padding-bottom:10px;">
                <h3 style="font-size:1.1rem;">Account Center</h3>
                <button class="menu-btn" onclick="toggleDrawer()">✕</button>
            </div>

            <!-- Email Verification Card -->
            <div style="background:var(--bg-main); border:1px solid var(--border); border-radius:8px; padding:12px;">
                <div style="font-size:0.75rem; color:var(--text-muted); font-weight:700;">EMAIL VERIFICATION</div>
                <div style="display:flex; justify-content:space-between; align-items:center; margin-top:6px;">
                    <span id="email-badge" style="font-weight:700; font-size:0.85rem; color:var(--gold);">UNVERIFIED</span>
                    <button class="btn-auth" id="email-verify-btn" style="padding:4px 8px; font-size:0.7rem;" onclick="openModal('email-modal')">Verify Code</button>
                </div>
            </div>

            <!-- National ID / KYC Card -->
            <div style="background:var(--bg-main); border:1px solid var(--border); border-radius:8px; padding:12px;">
                <div style="font-size:0.75rem; color:var(--text-muted); font-weight:700;">IDENTITY VERIFICATION (KYC)</div>
                <div style="display:flex; justify-content:space-between; align-items:center; margin-top:6px;">
                    <span id="kyc-badge" style="font-weight:700; font-size:0.85rem; color:var(--gold);">UNVERIFIED</span>
                    <button class="btn-auth" id="kyc-btn" style="padding:4px 8px; font-size:0.7rem;" onclick="openModal('kyc-modal')">Submit ID</button>
                </div>
            </div>

            <!-- Admin Market Override -->
            <div id="admin-panel-card" style="display:none; background:#1C1326; border:1px solid var(--purple); border-radius:8px; padding:12px;">
                <div style="font-size:0.75rem; color:#A78BFA; font-weight:800;">ADMIN MARKET CONTROLLER</div>
                <p style="font-size:0.72rem; color:var(--text-muted); margin-top:4px;">Override market fluctuations manually.</p>
                <button class="btn-auth" style="width:100%; margin-top:8px; background:var(--purple);" onclick="openModal('admin-modal')">Open God-Mode Control</button>
            </div>

            <button class="btn-auth" style="margin-top:auto; background:#F23645;" onclick="logout()">Logout</button>
        </div>

        <!-- Terminal Panes -->
        <div class="views-container">
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

            <div class="view-pane" id="view-watchlist">
                <div class="category-bar">
                    <button class="cat-tab active" onclick="filterWatchlist('ALL', this)">ALL</button>
                    <button class="cat-tab" onclick="filterWatchlist('FOREX', this)">FOREX</button>
                    <button class="cat-tab" onclick="filterWatchlist('COMMODITY', this)">COMMODITIES</button>
                    <button class="cat-tab" onclick="filterWatchlist('INDEX', this)">INDICES</button>
                </div>
                <div id="watchlist-box" style="flex:1; overflow-y:auto;"></div>
            </div>

            <div class="view-pane" id="view-trade">
                <div class="order-container">
                    <div class="side-toggle">
                        <button class="side-btn active buy" id="btn-buy" onclick="setSide('BUY')">BUY / LONG</button>
                        <button class="side-btn" id="btn-sell" onclick="setSide('SELL')">SELL / SHORT</button>
                    </div>

                    <div class="calc-box">
                        <div><span style="color:var(--text-muted);">Contract Units:</span> <strong id="calc-units">100,000</strong></div>
                        <div><span style="color:var(--text-muted);">Lot Sizing:</span> <strong id="calc-lotsize">1.00 Lot</strong></div>
                    </div>

                    <div class="input-group">
                        <label>Order Type</label>
                        <select id="order-type">
                            <option value="MARKET">Market Order (Instant)</option>
                            <option value="LIMIT">Limit Order</option>
                        </select>
                    </div>

                    <div class="input-group">
                        <label>Lots</label>
                        <input type="number" id="trade-lots" value="1.0" min="0.01" step="0.1" oninput="updateCalculations()">
                    </div>

                    <div class="calc-box">
                        <div><span style="color:var(--text-muted);">Margin Required:</span> <strong id="calc-margin">$2,169.00</strong></div>
                        <div><span style="color:var(--text-muted);">Leverage:</span> <strong id="calc-lev">1:50</strong></div>
                    </div>

                    <button class="btn-action" id="btn-submit-order" onclick="submitEnterMarket()">ENTER MARKET</button>
                    <div id="order-feedback" style="font-size:0.85rem; text-align:center;"></div>
                </div>
            </div>

            <div class="view-pane" id="view-positions">
                <div style="padding:10px 14px; background:var(--bg-card); border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center;">
                    <span style="font-size:0.75rem; font-weight:700; color:var(--text-muted);">OPEN CONTRACTS</span>
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

        <!-- Modal 1: Auth (Login & Register) -->
        <div class="modal-overlay" id="auth-modal">
            <div class="modal-card">
                <div class="modal-title">
                    <span id="auth-title">Sign In</span>
                    <button class="modal-close" onclick="closeModal('auth-modal')">✕</button>
                </div>
                <div class="input-group">
                    <label>Username</label>
                    <input type="text" id="auth-u">
                </div>
                <div class="input-group" id="email-field" style="display:none;">
                    <label>Email Address</label>
                    <input type="email" id="auth-e">
                </div>
                <div class="input-group">
                    <label>Password</label>
                    <input type="password" id="auth-p">
                </div>
                <button class="btn-action" id="auth-btn" onclick="submitAuth()">Sign In</button>
                <div style="text-align:center; font-size:0.8rem; color:var(--text-muted); cursor:pointer;" onclick="toggleAuth()" id="auth-toggle">
                    Don't have an account? Register here.
                </div>
            </div>
        </div>

        <!-- Modal 2: Email Verification -->
        <div class="modal-overlay" id="email-modal">
            <div class="modal-card">
                <div class="modal-title">
                    <span>Verify Email Address</span>
                    <button class="modal-close" onclick="closeModal('email-modal')">✕</button>
                </div>
                <p style="font-size:0.8rem; color:var(--text-muted);">Enter the 6-digit confirmation code sent to your email.</p>
                <div class="input-group">
                    <label>Confirmation Code</label>
                    <input type="text" id="email-code" placeholder="e.g. 582910">
                </div>
                <button class="btn-action" onclick="submitEmailCode()">Confirm Email</button>
            </div>
        </div>

        <!-- Modal 3: KYC ID Verification -->
        <div class="modal-overlay" id="kyc-modal">
            <div class="modal-card">
                <div class="modal-title">
                    <span>Identity Verification (KYC)</span>
                    <button class="modal-close" onclick="closeModal('kyc-modal')">✕</button>
                </div>
                <p style="font-size:0.8rem; color:var(--text-muted);">Enter your National ID / Passport / PAN Card Number for verification.</p>
                <div class="input-group">
                    <label>ID Card Number</label>
                    <input type="text" id="kyc-id" placeholder="e.g. PASS-9201948">
                </div>
                <button class="btn-action" onclick="submitKYC()">Submit ID</button>
            </div>
        </div>

        <!-- Modal 4: Admin Market Fluctuation Controller -->
        <div class="modal-overlay" id="admin-modal">
            <div class="modal-card" style="max-width:440px;">
                <div class="modal-title">
                    <span>Admin Market Fluctuation Controller</span>
                    <button class="modal-close" onclick="closeModal('admin-modal')">✕</button>
                </div>
                <div class="input-group">
                    <label>Fluctuation Mode</label>
                    <select id="admin-mode">
                        <option value="AUTO">Automatic (Natural Drift / Live Feeds)</option>
                        <option value="MANUAL">Manual Fluctuation (Force Price Target)</option>
                    </select>
                </div>
                <div class="input-group">
                    <label>EUR/USD Target</label>
                    <input type="number" id="admin-eur" value="1.08500" step="0.0001">
                </div>
                <div class="input-group">
                    <label>Gold (XAU/USD) Target</label>
                    <input type="number" id="admin-gold" value="2750.00" step="1">
                </div>
                <div class="input-group">
                    <label>NIFTY 50 Target</label>
                    <input type="number" id="admin-nifty" value="25000.00" step="10">
                </div>
                <button class="btn-action" style="background:var(--purple);" onclick="saveAdminFluctuations()">Apply Controls</button>
            </div>
        </div>

        <script>
            let isRegister = false;
            let currentUser = null;
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

            function openModal(id) { document.getElementById(id).style.display = 'flex'; }
            function closeModal(id) { document.getElementById(id).style.display = 'none'; }
            function toggleDrawer() {
                const d = document.getElementById('side-drawer');
                const o = document.getElementById('drawer-overlay');
                const open = d.style.transform === 'translateX(320px)';
                d.style.transform = open ? 'translateX(0px)' : 'translateX(320px)';
                o.style.display = open ? 'none' : 'block';
            }

            function setCurrency(c) {
                activeCurrency = c;
                document.getElementById('btn-usd').className = c === 'USD' ? 'curr-btn active' : 'curr-btn';
                document.getElementById('btn-inr').className = c === 'INR' ? 'curr-btn active' : 'curr-btn';
                updateCalculations();
                loadUserPortfolio();
            }

            function toggleAuth() {
                isRegister = !isRegister;
                document.getElementById('email-field').style.display = isRegister ? 'flex' : 'none';
                document.getElementById('auth-title').innerText = isRegister ? 'Register Account' : 'Sign In';
                document.getElementById('auth-btn').innerText = isRegister ? 'Register' : 'Sign In';
                document.getElementById('auth-toggle').innerText = isRegister ? 'Already have an account? Sign In' : "Don't have an account? Register here.";
            }

            async function submitAuth() {
                const u = document.getElementById('auth-u').value;
                const p = document.getElementById('auth-p').value;
                if (isRegister) {
                    const e = document.getElementById('auth-e').value;
                    const res = await fetch('/api/auth/register', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({username: u, email: e, password: p})
                    });
                    const d = await res.json();
                    if (res.ok) { alert("Registered! You can now log in."); toggleAuth(); }
                    else { alert(d.detail || "Registration failed"); }
                } else {
                    const res = await fetch('/api/auth/login', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({username: u, password: p})
                    });
                    const d = await res.json();
                    if (res.ok) { closeModal('auth-modal'); checkAuth(); }
                    else { alert(d.detail || "Login failed"); }
                }
            }

            async function checkAuth() {
                const res = await fetch('/api/user/me');
                const d = await res.json();
                if (d.authenticated) {
                    currentUser = d.user;
                    document.getElementById('auth-buttons').style.display = 'none';
                    document.getElementById('user-display').style.display = 'flex';
                    document.getElementById('user-display-name').innerText = currentUser.username;
                    
                    // Email verification status
                    const emBadge = document.getElementById('email-badge');
                    emBadge.innerText = currentUser.email_verified ? 'VERIFIED' : 'UNVERIFIED';
                    emBadge.style.color = currentUser.email_verified ? 'var(--neon)' : 'var(--gold)';
                    if (currentUser.email_verified) document.getElementById('email-verify-btn').style.display = 'none';

                    // KYC status
                    const kycBadge = document.getElementById('kyc-badge');
                    kycBadge.innerText = currentUser.kyc_status;
                    kycBadge.style.color = currentUser.kyc_status === 'VERIFIED' ? 'var(--neon)' : 'var(--gold)';
                    if (currentUser.kyc_status === 'VERIFIED') document.getElementById('kyc-btn').style.display = 'none';

                    // Admin card toggle
                    if (currentUser.is_admin) {
                        document.getElementById('admin-panel-card').style.display = 'block';
                    }
                }
            }
            checkAuth();

            async function logout() {
                await fetch('/api/auth/logout', {method: 'POST'});
                location.reload();
            }

            async function submitEmailCode() {
                const code = document.getElementById('email-code').value;
                const res = await fetch('/api/auth/verify-email', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({code: code})
                });
                if (res.ok) { alert("Email Verified!"); closeModal('email-modal'); checkAuth(); }
            }

            async function submitKYC() {
                const idNum = document.getElementById('kyc-id').value;
                const res = await fetch('/api/user/kyc', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({id_card_num: idNum})
                });
                if (res.ok) { alert("ID Submitted and Verified!"); closeModal('kyc-modal'); checkAuth(); }
            }

            async function saveAdminFluctuations() {
                const mode = document.getElementById('admin-mode').value;
                const eur = parseFloat(document.getElementById('admin-eur').value);
                const gold = parseFloat(document.getElementById('admin-gold').value);
                const nifty = parseFloat(document.getElementById('admin-nifty').value);

                const res = await fetch('/api/admin/market-control', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        mode: mode,
                        targets: {"EUR/USD": eur, "XAU/USD (Gold)": gold, "NIFTY 50": nifty}
                    })
                });
                if (res.ok) { alert("Market Fluctuation settings applied live!"); closeModal('admin-modal'); }
            }

            function switchTab(viewId, el) {
                document.querySelectorAll('.view-pane').forEach(p => p.classList.remove('active'));
                document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
                document.getElementById('view-' + viewId).classList.add('active');
                if (el) el.classList.add('active');
                if (viewId === 'chart') setTimeout(() => chart.resize(chartContainer.clientWidth, chartContainer.clientHeight), 50);
            }

            function setSide(s) {
                currentSide = s;
                document.getElementById('btn-buy').className = s === 'BUY' ? 'side-btn active buy' : 'side-btn';
                document.getElementById('btn-sell').className = s === 'SELL' ? 'side-btn active sell' : 'side-btn';
                const sub = document.getElementById('btn-submit-order');
                sub.className = s === 'BUY' ? 'btn-action' : 'btn-action sell-mode';
                sub.innerText = `${s} ${currentSymbol}`;
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
                    feed.innerText = `✓ Filled: ${lots} Lot(s) @ ${d.price}`;
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
                    alert(`Closed position! PnL: $${d.pnl_usd}`);
                    loadUserPortfolio();
                } else {
                    alert(d.detail || 'Exit failed');
                }
            }

            async function loadUserPortfolio() {
                const res = await fetch('/api/user/me');
                const d = await res.json();
                if (d.authenticated) {
                    const bal = activeCurrency === 'USD' ? `$${d.user.balance_usd.toLocaleString('en-US', {minimumFractionDigits:2})}` : `₹${d.user.balance_inr.toLocaleString('en-IN', {minimumFractionDigits:2})}`;
                    document.getElementById('user-display-bal').innerText = bal;

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
