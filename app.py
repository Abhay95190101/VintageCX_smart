import asyncio
import json
import random
import struct
import time
import zlib
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from engine import Order, OrderSide, OrderType, PaperTradingEngine

app = FastAPI(title="VintageCX Pro Terminal")
engine = PaperTradingEngine(initial_balance=100000.0)

# Tracked NSE Instruments
INSTRUMENTS = {
    "RELIANCE": {"price": 2980.50, "open": 2975.00, "high": 2995.00, "low": 2968.00},
    "TCS": {"price": 4120.00, "open": 4110.00, "high": 4145.00, "low": 4100.00},
    "INFY": {"price": 1890.25, "open": 1880.00, "high": 1905.00, "low": 1875.00},
    "HDFCBANK": {"price": 1640.10, "open": 1635.00, "high": 1652.00, "low": 1630.00},
    "TATAMOTORS": {"price": 995.80, "open": 988.00, "high": 1005.00, "low": 985.00},
}

class OrderRequest(BaseModel):
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int = Field(gt=0)
    price: float = Field(default=0.0, ge=0.0)

@app.post("/api/order")
def place_order(req: OrderRequest):
    sym = req.symbol.upper()
    if sym not in INSTRUMENTS:
        raise HTTPException(status_code=404, detail="Instrument not found")
    
    order = Order(
        symbol=sym,
        side=req.side,
        order_type=req.order_type,
        quantity=req.quantity,
        price=req.price
    )
    result = engine.execute_order(order, INSTRUMENTS[sym]["price"])
    return result

@app.get("/api/portfolio")
def get_portfolio():
    prices = {k: v["price"] for k, v in INSTRUMENTS.items()}
    return engine.get_portfolio_summary(prices)

# --- Dynamic True PNG Generation (Zero dependencies, 512x512 Pure Binary) ---
def generate_png_icon():
    width, height = 512, 512
    raw_rows = bytearray()
    
    for y in range(height):
        raw_rows.append(0)  # PNG Filter byte: None
        for x in range(width):
            dx = (x - 256) / 256
            dy = (y - 256) / 256
            dist = (dx * dx + dy * dy) ** 0.5
            
            # Stylized Fintech Dark Badge with Glowing Border & Neon Center
            if dist < 0.90:
                if 0.82 <= dist <= 0.88:
                    # Outer Neon Purple / Mint Gradient border
                    r = int(90 + 50 * dx)
                    g = int(49 + 180 * (dy + 1) / 2)
                    b = 244
                    a = 255
                elif abs(dx) + abs(dy) < 0.35 and dy > -0.2:
                    # Central Cyan/Mint "V" Apex Glow
                    r, g, b, a = 0, 255, 163, 255
                else:
                    # Deep Terminal Carbon Surface
                    r, g, b, a = 11, 14, 20, 255
            else:
                r, g, b, a = 0, 0, 0, 0
                
            raw_rows.extend([r, g, b, a])
            
    compressed = zlib.compress(raw_rows, level=6)
    
    png = bytearray(b"\x89PNG\r\n\x1a\n")
    # IHDR
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    png.extend(struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF))
    # IDAT
    png.extend(struct.pack(">I", len(compressed)) + b"IDAT" + compressed + struct.pack(">I", zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF))
    # IEND
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

# --- PWA Manifest & Service Worker ---
@app.get("/manifest.json")
def manifest():
    return JSONResponse(content={
        "name": "VintageCX Pro Terminal",
        "short_name": "VintageCX",
        "description": "Next-Gen Paper Trading & Risk Management Terminal",
        "start_url": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0B0E14",
        "theme_color": "#5A31F4",
        "icons": [
            {
                "src": "/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any"
            },
            {
                "src": "/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable"
            },
            {
                "src": "/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any"
            }
        ]
    })

@app.get("/sw.js")
def service_worker():
    return Response(content="self.addEventListener('fetch', function(e) {});", media_type="application/javascript")

# --- Real-Time Market WebSocket ---
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
                tick = round(random.uniform(-1.5, 1.5), 2)
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
                "market": INSTRUMENTS,
                "candles": candles,
                "portfolio": engine.get_portfolio_summary(prices)
            }))
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass

# --- High Performance Frontend Terminal ---
@app.get("/", response_class=HTMLResponse)
def index_view():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>VintageCX Pro Terminal</title>
        <link rel="manifest" href="/manifest.json">
        <link rel="icon" type="image/png" href="/icon-512.png">
        <meta name="theme-color" content="#5A31F4">
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
            }
            * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
            body { background: var(--bg-main); color: var(--text-primary); display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

            /* --- Header & Animated Branding --- */
            header { height: 56px; background: var(--bg-card); border-bottom: 1px solid var(--border-color); display: flex; align-items: center; justify-content: space-between; padding: 0 16px; z-index: 100; }
            .header-left { display: flex; align-items: center; gap: 14px; }
            .menu-toggle { background: transparent; border: none; color: #fff; font-size: 1.5rem; cursor: pointer; padding: 4px 8px; border-radius: 4px; transition: background 0.2s; }
            .menu-toggle:hover { background: #1C222E; }
            
            .brand-container { display: flex; align-items: center; gap: 10px; cursor: pointer; }
            .logo-icon { width: 34px; height: 34px; border-radius: 8px; animation: logoIntro 1.2s cubic-bezier(0.16, 1, 0.3, 1) forwards, logoPulse 3s ease-in-out infinite 1.2s; }
            
            @keyframes logoIntro {
                0% { transform: scale(0.2) rotate(-90deg); opacity: 0; filter: drop-shadow(0 0 0px var(--neon-green)); }
                70% { transform: scale(1.15) rotate(10deg); opacity: 1; }
                100% { transform: scale(1) rotate(0deg); opacity: 1; filter: drop-shadow(0 0 12px rgba(0, 255, 163, 0.45)); }
            }
            @keyframes logoPulse {
                0%, 100% { filter: drop-shadow(0 0 6px rgba(0, 255, 163, 0.3)); }
                50% { filter: drop-shadow(0 0 18px rgba(90, 49, 244, 0.7)); }
            }

            .brand-name { font-weight: 800; font-size: 1.15rem; letter-spacing: 0.5px; }
            .badge-pro { background: var(--neon-purple); color: #fff; font-size: 0.68rem; font-weight: 800; padding: 2px 6px; border-radius: 4px; margin-left: 4px; }

            .header-metrics { display: flex; align-items: center; gap: 20px; font-size: 0.85rem; }
            .pnl-badge { padding: 4px 10px; border-radius: 6px; font-weight: 700; background: rgba(8, 153, 129, 0.15); color: var(--trade-green); }
            .pnl-badge.negative { background: rgba(242, 54, 69, 0.15); color: var(--trade-red); }

            /* --- Slide-Out Insight & Risk Drawer --- */
            .drawer-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); backdrop-filter: blur(4px); z-index: 200; opacity: 0; pointer-events: none; transition: opacity 0.3s ease; }
            .drawer-overlay.active { opacity: 1; pointer-events: auto; }
            .drawer { position: fixed; top: 0; left: -360px; width: 340px; height: 100vh; background: var(--bg-card); z-index: 201; border-right: 1px solid var(--border-color); padding: 24px; transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1); display: flex; flex-direction: column; gap: 20px; box-shadow: 10px 0 30px rgba(0,0,0,0.5); }
            .drawer.active { transform: translateX(360px); }
            .drawer-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-color); padding-bottom: 14px; }
            .card-section { background: var(--bg-main); border: 1px solid var(--border-color); border-radius: 10px; padding: 14px; }
            .card-title { font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); font-weight: 700; margin-bottom: 10px; letter-spacing: 0.5px; }

            /* --- Main Body Layout --- */
            .main-container { display: flex; flex: 1; height: calc(100vh - 56px); }
            .watchlist-panel { width: 280px; border-right: 1px solid var(--border-color); background: var(--bg-card); display: flex; flex-direction: column; }
            .watchlist-items { overflow-y: auto; flex: 1; }
            .stock-item { display: flex; justify-content: space-between; padding: 14px 16px; border-bottom: 1px solid var(--border-color); cursor: pointer; transition: background 0.15s; }
            .stock-item:hover, .stock-item.active { background: #1C222E; }
            
            .chart-panel { flex: 1; display: flex; flex-direction: column; background: var(--bg-main); }
            .chart-header { padding: 12px 18px; border-bottom: 1px solid var(--border-color); display: flex; justify-content: space-between; align-items: center; }
            #chart-container { flex: 1; width: 100%; height: 100%; }

            .order-panel { width: 320px; border-left: 1px solid var(--border-color); background: var(--bg-card); display: flex; flex-direction: column; padding: 16px; gap: 14px; }
            .toggle-group { display: flex; border-radius: 6px; overflow: hidden; background: #0B0E14; padding: 2px; }
            .toggle-btn { flex: 1; padding: 8px; border: none; background: transparent; color: var(--text-muted); font-weight: 700; cursor: pointer; border-radius: 4px; }
            .toggle-btn.active.buy { background: var(--trade-green); color: white; }
            .toggle-btn.active.sell { background: var(--trade-red); color: white; }

            .input-box { display: flex; flex-direction: column; gap: 5px; }
            .input-box label { font-size: 0.72rem; color: var(--text-muted); font-weight: 600; text-transform: uppercase; }
            .input-box input, .input-box select { background: #0B0E14; border: 1px solid var(--border-color); color: #fff; padding: 9px; border-radius: 6px; outline: none; font-size: 0.9rem; }
            .btn-submit { background: var(--trade-green); border: none; color: #fff; font-weight: 700; padding: 12px; border-radius: 6px; cursor: pointer; font-size: 0.95rem; margin-top: 6px; }
            .btn-submit.sell-mode { background: var(--trade-red); }

            .progress-bar-bg { width: 100%; height: 6px; background: #232A36; border-radius: 3px; overflow: hidden; margin-top: 6px; }
            .progress-bar-fill { height: 100%; width: 28%; background: var(--neon-green); transition: width 0.3s; }
        </style>
    </head>
    <body>
        <!-- Header -->
        <header>
            <div class="header-left">
                <button class="menu-toggle" onclick="toggleDrawer()">☰</button>
                <div class="brand-container" onclick="toggleDrawer()">
                    <img src="/icon-512.png" alt="VintageCX" class="logo-icon">
                    <div class="brand-name">VINTAGE<span style="color:var(--neon-green)">CX</span><span class="badge-pro">PRO</span></div>
                </div>
            </div>
            
            <div class="header-metrics">
                <div>Cash: <strong id="cash-balance">₹1,00,000.00</strong></div>
                <div class="pnl-badge" id="net-pnl-badge">P&L: +₹0.00 (0.00%)</div>
            </div>
        </header>

        <!-- Slide-out Menu: Insights & Risk Management -->
        <div class="drawer-overlay" id="drawer-overlay" onclick="toggleDrawer()"></div>
        <div class="drawer" id="side-drawer">
            <div class="drawer-header">
                <h3>Terminal Insights</h3>
                <button class="menu-toggle" onclick="toggleDrawer()">✕</button>
            </div>

            <!-- Portfolio Insight -->
            <div class="card-section">
                <div class="card-title">Portfolio Analytics</div>
                <div style="display:flex; justify-content:space-between; margin-bottom:8px;">
                    <span style="color:var(--text-muted); font-size:0.85rem;">Total Portfolio</span>
                    <strong id="drawer-portfolio-val">₹1,00,000.00</strong>
                </div>
                <div style="display:flex; justify-content:space-between; margin-bottom:8px;">
                    <span style="color:var(--text-muted); font-size:0.85rem;">Available Margin</span>
                    <span style="color:var(--neon-green); font-size:0.85rem;" id="drawer-margin-val">₹1,00,000.00</span>
                </div>
                <div style="display:flex; justify-content:space-between;">
                    <span style="color:var(--text-muted); font-size:0.85rem;">Engine Status</span>
                    <span style="color:var(--neon-green); font-size:0.85rem;">● Paper Real-Time</span>
                </div>
            </div>

            <!-- Risk Management -->
            <div class="card-section">
                <div class="card-title">Risk Management Shield</div>
                <div style="font-size:0.82rem; margin-bottom: 6px; display:flex; justify-content:space-between;">
                    <span>Daily Drawdown Limit</span>
                    <span style="color:#F23645;">₹5,000.00</span>
                </div>
                <div class="progress-bar-bg">
                    <div class="progress-bar-fill" id="drawdown-progress"></div>
                </div>
                <div style="display:flex; justify-content:space-between; margin-top:12px; font-size:0.82rem;">
                    <span style="color:var(--text-muted);">Max Order Size</span>
                    <span>500 Qty</span>
                </div>
                <div style="display:flex; justify-content:space-between; margin-top:6px; font-size:0.82rem;">
                    <span style="color:var(--text-muted);">Exposure Buffer</span>
                    <span style="color:var(--neon-green);">HEALTHY</span>
                </div>
            </div>

            <!-- P&L Insight -->
            <div class="card-section">
                <div class="card-title">Profit & Loss Summary</div>
                <div style="display:flex; justify-content:space-between; font-size:0.9rem; font-weight:700;">
                    <span>Unrealized P&L</span>
                    <span id="drawer-unrealized-pnl" style="color:var(--trade-green);">₹0.00</span>
                </div>
            </div>
        </div>

        <!-- Terminal Workspace -->
        <div class="main-container">
            <!-- Watchlist -->
            <div class="watchlist-panel">
                <div style="padding:12px 16px; border-bottom:1px solid var(--border-color); font-weight:700; font-size:0.8rem; color:var(--text-muted);">MARKET WATCH (NSE)</div>
                <div class="watchlist-items" id="watchlist"></div>
            </div>

            <!-- Chart -->
            <div class="chart-panel">
                <div class="chart-header">
                    <div>
                        <span id="selected-sym" style="font-size:1.2rem; font-weight:800;">RELIANCE</span>
                        <span style="color:var(--text-muted); font-size:0.8rem; margin-left:8px;">NSE EQ REAL-TIME</span>
                    </div>
                    <div id="sym-price" style="font-weight:700; font-size:1.1rem; color:var(--neon-green);">₹2,980.50</div>
                </div>
                <div id="chart-container"></div>
            </div>

            <!-- Order & Risk Execution -->
            <div class="order-panel">
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
                    <input type="number" id="order-qty" value="1" min="1" max="500">
                </div>

                <div class="input-box">
                    <label>Price</label>
                    <input type="number" id="order-price" value="0.0" step="0.05">
                </div>

                <button class="btn-submit" id="submit-btn" onclick="submitOrder()">BUY RELIANCE</button>
                <div id="order-feedback" style="font-size:0.8rem; text-align:center;"></div>

                <div style="border-top:1px solid var(--border-color); padding-top:10px; flex:1; overflow-y:auto;">
                    <div style="font-size:0.75rem; color:var(--text-muted); font-weight:700; margin-bottom:8px;">OPEN POSITIONS</div>
                    <div id="positions-container"></div>
                </div>
            </div>
        </div>

        <script>
            if ('serviceWorker' in navigator) {
                navigator.serviceWorker.register('/sw.js');
            }

            function toggleDrawer() {
                document.getElementById('side-drawer').classList.toggle('active');
                document.getElementById('drawer-overlay').classList.toggle('active');
            }

            let currentSymbol = "RELIANCE";
            let currentSide = "BUY";

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
                    let open = basePrice + Math.sin(i) * 3;
                    let high = open + Math.random() * 2;
                    let low = open - Math.random() * 2;
                    let close = open + (Math.random() - 0.5) * 2;
                    candles.push({ time: t, open: open, high: high, low: low, close: close });
                }
                candleSeries.setData(candles);
            }
            seedData(2980.50);

            window.addEventListener('resize', () => {
                chart.resize(chartContainer.clientWidth, chartContainer.clientHeight);
            });

            function setSide(side) {
                currentSide = side;
                const buyBtn = document.getElementById("tab-buy");
                const sellBtn = document.getElementById("tab-sell");
                const submitBtn = document.getElementById("submit-btn");

                if (side === "BUY") {
                    buyBtn.className = "toggle-btn active buy";
                    sellBtn.className = "toggle-btn";
                    submitBtn.className = "btn-submit";
                    submitBtn.innerText = `BUY ${currentSymbol}`;
                } else {
                    sellBtn.className = "toggle-btn active sell";
                    buyBtn.className = "toggle-btn";
                    submitBtn.className = "btn-submit sell-mode";
                    submitBtn.innerText = `SELL ${currentSymbol}`;
                }
            }

            function selectStock(sym, price) {
                currentSymbol = sym;
                document.getElementById("selected-sym").innerText = sym;
                document.getElementById("sym-price").innerText = `₹${price.toFixed(2)}`;
                setSide(currentSide);
                document.querySelectorAll(".stock-item").forEach(el => el.classList.remove("active"));
                const target = document.getElementById(`stock-${sym}`);
                if (target) target.classList.add("active");
                seedData(price);
            }

            const wsProtocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const ws = new WebSocket(`${wsProtocol}//${location.host}/ws/market-feed`);

            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);

                // Watchlist render
                const wl = document.getElementById("watchlist");
                wl.innerHTML = Object.entries(data.market).map(([sym, item]) => `
                    <div class="stock-item ${sym === currentSymbol ? 'active' : ''}" id="stock-${sym}" onclick="selectStock('${sym}', ${item.price})">
                        <div>
                            <div style="font-weight:700; font-size:0.9rem;">${sym}</div>
                            <div style="font-size:0.7rem; color:var(--text-muted);">NSE EQ</div>
                        </div>
                        <div style="font-weight:700; text-align:right;">₹${item.price.toFixed(2)}</div>
                    </div>
                `).join("");

                if (data.candles[currentSymbol]) {
                    candleSeries.update(data.candles[currentSymbol]);
                    document.getElementById("sym-price").innerText = `₹${data.candles[currentSymbol].close.toFixed(2)}`;
                }

                // Balance & PnL updates
                const cash = data.portfolio.cash_balance;
                const total = data.portfolio.total_portfolio_value;
                const pnl = total - 100000.0;
                const pnlPercent = (pnl / 100000.0) * 100;

                document.getElementById("cash-balance").innerText = `₹${cash.toLocaleString('en-IN', {minimumFractionDigits: 2})}`;
                document.getElementById("drawer-margin-val").innerText = `₹${cash.toLocaleString('en-IN', {minimumFractionDigits: 2})}`;
                document.getElementById("drawer-portfolio-val").innerText = `₹${total.toLocaleString('en-IN', {minimumFractionDigits: 2})}`;
                
                const pnlBadge = document.getElementById("net-pnl-badge");
                pnlBadge.innerText = `P&L: ${pnl >= 0 ? '+' : ''}₹${pnl.toFixed(2)} (${pnlPercent.toFixed(2)}%)`;
                pnlBadge.className = pnl >= 0 ? "pnl-badge" : "pnl-badge negative";

                const drawerPnl = document.getElementById("drawer-unrealized-pnl");
                drawerPnl.innerText = `${pnl >= 0 ? '+' : ''}₹${pnl.toFixed(2)}`;
                drawerPnl.style.color = pnl >= 0 ? "var(--trade-green)" : "var(--trade-red)";

                // Positions
                const pContainer = document.getElementById("positions-container");
                const pos = data.portfolio.positions;
                if (!pos || pos.length === 0) {
                    pContainer.innerHTML = `<div style="color:var(--text-muted); font-size:0.8rem;">No open positions</div>`;
                } else {
                    pContainer.innerHTML = pos.map(p => `
                        <div style="background:#1C222E; padding:8px; border-radius:6px; margin-bottom:6px; font-size:0.82rem;">
                            <div style="display:flex; justify-content:space-between;"><strong>${p.symbol}</strong><span>Qty: ${p.quantity}</span></div>
                            <div style="display:flex; justify-content:space-between; color:var(--text-muted); margin-top:2px;">
                                <span>LTP: ₹${p.current_price.toFixed(2)}</span>
                                <span style="color:#089981;">₹${p.market_value.toFixed(2)}</span>
                            </div>
                        </div>
                    `).join("");
                }
            };

            async function submitOrder() {
                const qty = parseInt(document.getElementById("order-qty").value);
                if (qty > 500) {
                    alert("Risk Limit: Maximum 500 quantity allowed per execution!");
                    return;
                }

                const payload = {
                    symbol: currentSymbol,
                    side: currentSide,
                    order_type: document.getElementById("order-type").value,
                    quantity: qty,
                    price: parseFloat(document.getElementById("order-price").value) || 0.0
                };

                const res = await fetch("/api/order", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });
                const result = await res.json();
                const alertEl = document.getElementById("order-feedback");
                alertEl.innerText = result.status === "FILLED" 
                    ? `✓ Executed: ${result.side} ${result.quantity} ${result.symbol} @ ₹${result.execution_price}`
                    : `✗ Rejected: ${result.reason}`;
                alertEl.style.color = result.status === "FILLED" ? "var(--trade-green)" : "var(--trade-red)";
            }
        </script>
    </body>
    </html>
    """
