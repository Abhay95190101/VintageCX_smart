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

def generate_png_icon():
    width, height = 512, 512
    raw_rows = bytearray()
    for y in range(height):
        raw_rows.append(0)
        for x in range(width):
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
            raw_rows.extend([r, g, b, a])
    compressed = zlib.compress(raw_rows, level=6)
    png = bytearray(b"\x89PNG\r\n\x1a\n")
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    png.extend(struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF))
    png.extend(struct.pack(">I", len(compressed)) + b"IDAT" + compressed + struct.pack(">I", zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF))
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
        "description": "Next-Gen Mobile Paper Trading Terminal",
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

@app.get("/", response_class=HTMLResponse)
def index_view():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
        <title>VintageCX Pro Terminal</title>
        <link rel="manifest" href="/manifest.json">
        <link rel="icon" type="image/png" href="/icon-512.png">
        <meta name="theme-color" content="#0B0E14">
        <meta name="apple-mobile-web-app-capable" content="yes">
        <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
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
            * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; -webkit-tap-highlight-color: transparent; }
            body { background: var(--bg-main); color: var(--text-primary); display: flex; flex-direction: column; height: 100vh; height: 100dvh; overflow: hidden; }

            /* Header */
            header { height: 52px; background: var(--bg-card); border-bottom: 1px solid var(--border-color); display: flex; align-items: center; justify-content: space-between; padding: 0 12px; z-index: 10; flex-shrink: 0; }
            .header-left { display: flex; align-items: center; gap: 8px; }
            .menu-toggle { background: transparent; border: none; color: #fff; font-size: 1.4rem; cursor: pointer; padding: 4px; }
            .logo-icon { width: 30px; height: 30px; border-radius: 6px; animation: logoIntro 1.2s cubic-bezier(0.16, 1, 0.3, 1) forwards; }
            @keyframes logoIntro { 0% { transform: scale(0.2); opacity: 0; } 100% { transform: scale(1); opacity: 1; filter: drop-shadow(0 0 10px rgba(0, 255, 163, 0.45)); } }
            .brand-name { font-weight: 800; font-size: 1.05rem; }
            .badge-pro { background: var(--neon-purple); color: #fff; font-size: 0.65rem; font-weight: 800; padding: 2px 5px; border-radius: 4px; margin-left: 3px; }
            .header-right { text-align: right; }
            .cash-text { font-size: 0.72rem; color: var(--text-muted); }
            .cash-num { font-size: 0.85rem; font-weight: 700; color: #fff; }

            /* Drawer */
            .drawer-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.65); backdrop-filter: blur(4px); z-index: 200; opacity: 0; pointer-events: none; transition: opacity 0.25s ease; }
            .drawer-overlay.active { opacity: 1; pointer-events: auto; }
            .drawer { position: fixed; top: 0; left: -320px; width: 300px; height: 100%; background: var(--bg-card); z-index: 201; border-right: 1px solid var(--border-color); padding: 20px; transition: transform 0.25s ease-out; display: flex; flex-direction: column; gap: 16px; }
            .drawer.active { transform: translateX(320px); }
            .card-section { background: var(--bg-main); border: 1px solid var(--border-color); border-radius: 8px; padding: 12px; }
            .card-title { font-size: 0.72rem; text-transform: uppercase; color: var(--text-muted); font-weight: 700; margin-bottom: 8px; }

            /* Layout Panes */
            .views-container { flex: 1; position: relative; overflow: hidden; }
            .view-pane { position: absolute; inset: 0; display: none; flex-direction: column; background: var(--bg-main); overflow-y: auto; }
            .view-pane.active { display: flex; }

            /* Stock Header */
            .chart-bar { padding: 10px 14px; background: var(--bg-card); border-bottom: 1px solid var(--border-color); display: flex; justify-content: space-between; align-items: center; }
            #chart-container { flex: 1; width: 100%; min-height: 280px; }

            /* Watchlist Items */
            .stock-item { display: flex; justify-content: space-between; padding: 14px 16px; border-bottom: 1px solid var(--border-color); cursor: pointer; }
            .stock-item:active { background: #1C222E; }

            /* Order Form */
            .trade-box { padding: 16px; display: flex; flex-direction: column; gap: 12px; }
            .toggle-group { display: flex; border-radius: 6px; overflow: hidden; background: #0B0E14; padding: 2px; }
            .toggle-btn { flex: 1; padding: 10px; border: none; background: transparent; color: var(--text-muted); font-weight: 700; cursor: pointer; border-radius: 4px; font-size: 0.9rem; }
            .toggle-btn.active.buy { background: var(--trade-green); color: white; }
            .toggle-btn.active.sell { background: var(--trade-red); color: white; }
            .input-box { display: flex; flex-direction: column; gap: 5px; }
            .input-box label { font-size: 0.72rem; color: var(--text-muted); font-weight: 600; text-transform: uppercase; }
            .input-box input, .input-box select { background: #0B0E14; border: 1px solid var(--border-color); color: #fff; padding: 12px; border-radius: 6px; outline: none; font-size: 1rem; }
            .btn-action { background: var(--trade-green); border: none; color: #fff; font-weight: 800; padding: 14px; border-radius: 6px; cursor: pointer; font-size: 1rem; margin-top: 6px; }
            .btn-action.sell-mode { background: var(--trade-red); }

            /* Mobile Bottom Navigation Bar */
            .bottom-nav { height: 56px; background: var(--bg-card); border-top: 1px solid var(--border-color); display: flex; align-items: center; justify-content: space-around; flex-shrink: 0; z-index: 10; padding-bottom: env(safe-area-inset-bottom); }
            .nav-item { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 3px; background: none; border: none; color: var(--text-muted); font-size: 0.72rem; font-weight: 600; cursor: pointer; height: 100%; }
            .nav-item svg { width: 19px; height: 19px; fill: currentColor; }
            .nav-item.active { color: var(--neon-green); }

            /* Desktop Responsive Fixes */
            @media (min-width: 900px) {
                .bottom-nav { display: none; }
                .views-container { display: flex; }
                .view-pane { position: static; display: flex !important; }
                #view-watchlist { width: 280px; border-right: 1px solid var(--border-color); }
                #view-chart { flex: 1; }
                #view-trade { width: 320px; border-left: 1px solid var(--border-color); }
                #view-portfolio { display: none !important; }
            }
        </style>
    </head>
    <body>
        <header>
            <div class="header-left">
                <button class="menu-toggle" onclick="toggleDrawer()">☰</button>
                <div style="display:flex; align-items:center; gap:6px;" onclick="toggleDrawer()">
                    <img src="/icon-512.png" alt="Logo" class="logo-icon">
                    <span class="brand-name">VINTAGE<span style="color:var(--neon-green)">CX</span><span class="badge-pro">PRO</span></span>
                </div>
            </div>
            <div class="header-right">
                <div class="cash-text">Available Margin</div>
                <div class="cash-num" id="cash-balance">₹1,00,000.00</div>
            </div>
        </header>

        <!-- Insights Drawer -->
        <div class="drawer-overlay" id="drawer-overlay" onclick="toggleDrawer()"></div>
        <div class="drawer" id="side-drawer">
            <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--border-color); padding-bottom:10px;">
                <h3 style="font-size:1.1rem;">Terminal Insights</h3>
                <button class="menu-toggle" onclick="toggleDrawer()">✕</button>
            </div>
            <div class="card-section">
                <div class="card-title">Portfolio Analytics</div>
                <div style="display:flex; justify-content:space-between; margin-bottom:8px; font-size:0.85rem;">
                    <span style="color:var(--text-muted);">Total Portfolio</span>
                    <strong id="drawer-portfolio-val">₹1,00,000.00</strong>
                </div>
                <div style="display:flex; justify-content:space-between; font-size:0.85rem;">
                    <span style="color:var(--text-muted);">Unrealized P&L</span>
                    <span id="drawer-unrealized-pnl" style="color:var(--trade-green); font-weight:700;">+₹0.00 (0.00%)</span>
                </div>
            </div>
            <div class="card-section">
                <div class="card-title">Risk Management Shield</div>
                <div style="display:flex; justify-content:space-between; font-size:0.85rem; margin-bottom:6px;">
                    <span style="color:var(--text-muted);">Daily Drawdown</span>
                    <span style="color:var(--neon-green);">0.0% / Max ₹5,000</span>
                </div>
                <div style="display:flex; justify-content:space-between; font-size:0.85rem;">
                    <span style="color:var(--text-muted);">Max Order Limit</span>
                    <span>500 Qty</span>
                </div>
            </div>
        </div>

        <!-- Dynamic Views -->
        <div class="views-container">
            <!-- 1. Chart View (Default on Mobile) -->
            <div class="view-pane active" id="view-chart">
                <div class="chart-bar">
                    <div>
                        <strong id="selected-sym" style="font-size:1.1rem;">RELIANCE</strong>
                        <span style="color:var(--text-muted); font-size:0.75rem; margin-left:6px;">NSE</span>
                    </div>
                    <div id="sym-price" style="font-weight:700; font-size:1.1rem; color:var(--neon-green);">₹2,980.50</div>
                </div>
                <div id="chart-container"></div>
            </div>

            <!-- 2. Watchlist View -->
            <div class="view-pane" id="view-watchlist">
                <div style="padding:10px 16px; border-bottom:1px solid var(--border-color); font-size:0.75rem; color:var(--text-muted); font-weight:700;">NSE INSTRUMENTS</div>
                <div id="watchlist-list"></div>
            </div>

            <!-- 3. Trade Execution View -->
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
                        <label>Quantity (Max 500)</label>
                        <input type="number" id="order-qty" value="1" min="1" max="500">
                    </div>
                    <div class="input-box">
                        <label>Price (₹)</label>
                        <input type="number" id="order-price" value="0.0" step="0.05">
                    </div>
                    <button class="btn-action" id="submit-btn" onclick="submitOrder()">BUY RELIANCE</button>
                    <div id="order-feedback" style="font-size:0.85rem; text-align:center;"></div>
                </div>
            </div>

            <!-- 4. Positions / Portfolio View -->
            <div class="view-pane" id="view-portfolio">
                <div style="padding:10px 16px; border-bottom:1px solid var(--border-color); font-size:0.75rem; color:var(--text-muted); font-weight:700;">OPEN POSITIONS</div>
                <div id="positions-list" style="padding:12px;"></div>
            </div>
        </div>

        <!-- Mobile Bottom Tabs -->
        <nav class="bottom-nav">
            <button class="nav-item active" onclick="switchTab('chart', this)">
                <svg viewBox="0 0 24 24"><path d="M3.5 18.5l6-6 4 4 7-7-1.5-1.5-5.5 5.5-4-4-7.5 7.5z"/></svg>
                <span>Chart</span>
            </button>
            <button class="nav-item" onclick="switchTab('watchlist', this)">
                <svg viewBox="0 0 24 24"><path d="M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7z"/></svg>
                <span>Watchlist</span>
            </button>
            <button class="nav-item" onclick="switchTab('trade', this)">
                <svg viewBox="0 0 24 24"><path d="M7 10l5 5 5-5z"/></svg>
                <span>Trade</span>
            </button>
            <button class="nav-item" onclick="switchTab('portfolio', this)">
                <svg viewBox="0 0 24 24"><path d="M20 6h-4V4c0-1.11-.89-2-2-2h-4c-1.11 0-2 .89-2 2v2H4c-1.11 0-1.99.89-1.99 2L2 19c0 1.11.89 2 2 2h16c1.11 0 2-.89 2-2V8c0-1.11-.89-2-2-2zm-6 0h-4V4h4v2z"/></svg>
                <span>Portfolio</span>
            </button>
        </nav>

        <script>
            if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');

            function toggleDrawer() {
                document.getElementById('side-drawer').classList.toggle('active');
                document.getElementById('drawer-overlay').classList.toggle('active');
            }

            function switchTab(viewId, el) {
                document.querySelectorAll('.view-pane').forEach(p => p.classList.remove('active'));
                document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
                document.getElementById('view-' + viewId).classList.add('active');
                if (el) el.classList.add('active');
                if (viewId === 'chart') {
                    setTimeout(() => chart.resize(chartContainer.clientWidth, chartContainer.clientHeight), 50);
                }
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
                    submitBtn.className = "btn-action";
                    submitBtn.innerText = `BUY ${currentSymbol}`;
                } else {
                    sellBtn.className = "toggle-btn active sell";
                    buyBtn.className = "toggle-btn";
                    submitBtn.className = "btn-action sell-mode";
                    submitBtn.innerText = `SELL ${currentSymbol}`;
                }
            }

            function selectStock(sym, price) {
                currentSymbol = sym;
                document.getElementById("selected-sym").innerText = sym;
                document.getElementById("sym-price").innerText = `₹${price.toFixed(2)}`;
                setSide(currentSide);
                seedData(price);
                switchTab('chart', document.querySelector('.nav-item'));
            }

            const wsProtocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const ws = new WebSocket(`${wsProtocol}//${location.host}/ws/market-feed`);

            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);

                // Watchlist
                const wl = document.getElementById("watchlist-list");
                wl.innerHTML = Object.entries(data.market).map(([sym, item]) => `
                    <div class="stock-item" onclick="selectStock('${sym}', ${item.price})">
                        <div>
                            <div style="font-weight:700; font-size:0.95rem;">${sym}</div>
                            <div style="font-size:0.7rem; color:var(--text-muted);">NSE EQ</div>
                        </div>
                        <div style="font-weight:700; font-size:0.95rem; text-align:right;">₹${item.price.toFixed(2)}</div>
                    </div>
                `).join("");

                if (data.candles[currentSymbol]) {
                    candleSeries.update(data.candles[currentSymbol]);
                    document.getElementById("sym-price").innerText = `₹${data.candles[currentSymbol].close.toFixed(2)}`;
                }

                // Balance & PnL
                const cash = data.portfolio.cash_balance;
                const total = data.portfolio.total_portfolio_value;
                const pnl = total - 100000.0;
                const pnlPercent = (pnl / 100000.0) * 100;

                document.getElementById("cash-balance").innerText = `₹${cash.toLocaleString('en-IN', {minimumFractionDigits: 2})}`;
                document.getElementById("drawer-portfolio-val").innerText = `₹${total.toLocaleString('en-IN', {minimumFractionDigits: 2})}`;
                
                const drawerPnl = document.getElementById("drawer-unrealized-pnl");
                drawerPnl.innerText = `${pnl >= 0 ? '+' : ''}₹${pnl.toFixed(2)} (${pnlPercent.toFixed(2)}%)`;
                drawerPnl.style.color = pnl >= 0 ? "var(--trade-green)" : "var(--trade-red)";

                // Positions
                const pContainer = document.getElementById("positions-list");
                const pos = data.portfolio.positions;
                if (!pos || pos.length === 0) {
                    pContainer.innerHTML = `<div style="color:var(--text-muted); font-size:0.85rem; text-align:center; padding:20px;">No open positions</div>`;
                } else {
                    pContainer.innerHTML = pos.map(p => `
                        <div style="background:#141822; border:1px solid #232A36; padding:12px; border-radius:8px; margin-bottom:8px;">
                            <div style="display:flex; justify-content:space-between; font-weight:700;"><span>${p.symbol}</span><span>Qty: ${p.quantity}</span></div>
                            <div style="display:flex; justify-content:space-between; color:var(--text-muted); font-size:0.85rem; margin-top:4px;">
                                <span>LTP: ₹${p.current_price.toFixed(2)}</span>
                                <span style="color:#089981;">₹${p.market_value.toFixed(2)}</span>
                            </div>
                        </div>
                    `).join("");
                }
            };

            async function submitOrder() {
                const qty = parseInt(document.getElementById("order-qty").value);
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
