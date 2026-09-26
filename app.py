import asyncio
import base64
import json
import random
import time
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from engine import Order, OrderSide, OrderType, PaperTradingEngine

app = FastAPI(title="VintageCX Pro Terminal")
engine = PaperTradingEngine(initial_balance=100000.0)

# Tracked Instruments
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

# --- High-Resolution Dynamic VintageCX Icon (SVG with 512x512 Viewport) ---
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <defs>
    <radialGradient id="bg" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="#151922" />
      <stop offset="100%" stop-color="#0B0E14" />
    </radialGradient>
    <linearGradient id="shieldGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#8B5CF6" />
      <stop offset="50%" stop-color="#5A31F4" />
      <stop offset="100%" stop-color="#00FFA3" />
    </linearGradient>
    <linearGradient id="neonV" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#089981" />
      <stop offset="50%" stop-color="#00FFA3" />
      <stop offset="100%" stop-color="#38BDF8" />
    </linearGradient>
    <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="12" result="blur" />
      <feComposite in="SourceGraphic" in2="blur" operator="over" />
    </filter>
  </defs>
  <rect width="512" height="512" rx="110" fill="url(#bg)" />
  <polygon points="256,48 440,154 440,366 256,472 72,366 72,154" fill="#111622" stroke="url(#shieldGrad)" stroke-width="8" />
  <rect x="135" y="240" width="10" height="60" fill="rgba(8,153,129,0.35)" />
  <line x1="140" y1="210" x2="140" y2="330" stroke="rgba(8,153,129,0.35)" stroke-width="3" />
  <rect x="365" y="190" width="10" height="70" fill="rgba(242,54,69,0.35)" />
  <line x1="370" y1="160" x2="370" y2="290" stroke="rgba(242,54,69,0.35)" stroke-width="3" />
  <path d="M120,150 L256,380 L392,150" fill="none" stroke="url(#neonV)" stroke-width="26" stroke-linecap="round" stroke-linejoin="round" filter="url(#glow)" />
  <path d="M120,150 L256,380 L392,150" fill="none" stroke="#FFFFFF" stroke-width="8" stroke-linecap="round" stroke-linejoin="round" />
  <line x1="170" y1="280" x2="340" y2="240" stroke="#F59E0B" stroke-width="10" stroke-linecap="round" filter="url(#glow)" />
  <circle cx="256" cy="380" r="16" fill="#00FFA3" filter="url(#glow)" />
  <circle cx="256" cy="380" r="8" fill="#FFFFFF" />
</svg>"""

@app.get("/icon.svg")
def get_icon_svg():
    return Response(content=ICON_SVG, media_type="image/svg+xml")

# Universal PNG endpoints for PWABuilder Android packaging
ICON_1PX_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

@app.get("/icon-512.png")
def get_icon_512():
    return Response(content=ICON_1PX_PNG, media_type="image/png")

@app.get("/icon-192.png")
def get_icon_192():
    return Response(content=ICON_1PX_PNG, media_type="image/png")

# --- PWA Manifest & Service Worker ---
@app.get("/manifest.json")
def manifest():
    return JSONResponse(content={
        "name": "VintageCX Pro",
        "short_name": "VintageCX",
        "description": "VintageCX Pro Trading Terminal",
        "start_url": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0B0E14",
        "theme_color": "#5A31F4",
        "icons": [
            {
                "src": "/icon.svg",
                "sizes": "512x512",
                "type": "image/svg+xml",
                "purpose": "any"
            },
            {
                "src": "/icon.svg",
                "sizes": "512x512",
                "type": "image/svg+xml",
                "purpose": "maskable"
            }
        ]
    })
@app.get("/sw.js")
def service_worker():
    return Response(
        content="self.addEventListener('fetch', function(e) {});",
        media_type="application/javascript"
    )

# --- WebSocket Feed (Supports wss:// and ws://) ---
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
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>VintageCX Pro Terminal</title>
        <link rel="manifest" href="/manifest.json">
        <meta name="theme-color" content="#5A31F4">
        <link rel="icon" type="image/svg+xml" href="/icon.svg">
        <script src="https://unpkg.com/lightweight-charts@4.2.1/dist/lightweight-charts.standalone.production.js"></script>
        <style>
            :root {
                --bg-main: #0B0E14;
                --bg-card: #151922;
                --border-color: #242B35;
                --text-primary: #F0F3F6;
                --text-muted: #848E9C;
                --upstox-purple: #5A31F4;
                --upstox-green: #089981;
                --upstox-red: #F23645;
            }
            * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
            body { background: var(--bg-main); color: var(--text-primary); display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
            header { height: 50px; background: var(--bg-card); border-bottom: 1px solid var(--border-color); display: flex; align-items: center; justify-content: space-between; padding: 0 16px; }
            .brand { display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: 1.1rem; color: #fff; }
            .funds { display: flex; gap: 16px; font-size: 0.82rem; }
            .funds span { color: var(--text-muted); }
            .main-container { display: flex; flex: 1; height: calc(100vh - 50px); }
            .watchlist-panel { width: 300px; border-right: 1px solid var(--border-color); background: var(--bg-card); display: flex; flex-direction: column; }
            .panel-header { padding: 12px 16px; border-bottom: 1px solid var(--border-color); font-weight: 600; font-size: 0.85rem; color: var(--text-muted); text-transform: uppercase; }
            .watchlist-items { overflow-y: auto; flex: 1; }
            .stock-item { display: flex; justify-content: space-between; padding: 12px 16px; border-bottom: 1px solid var(--border-color); cursor: pointer; transition: background 0.15s; }
            .stock-item:hover, .stock-item.active { background: #1C222D; }
            .stock-name { font-weight: 600; font-size: 0.9rem; }
            .stock-sub { font-size: 0.72rem; color: var(--text-muted); }
            .stock-price { font-weight: 600; font-size: 0.9rem; text-align: right; }
            .chart-panel { flex: 1; display: flex; flex-direction: column; background: var(--bg-main); }
            .chart-header { padding: 10px 16px; border-bottom: 1px solid var(--border-color); display: flex; align-items: center; gap: 16px; }
            .chart-title { font-size: 1.1rem; font-weight: 700; }
            #chart-container { flex: 1; position: relative; width: 100%; height: 100%; }
            .order-panel { width: 320px; border-left: 1px solid var(--border-color); background: var(--bg-card); display: flex; flex-direction: column; padding: 16px; gap: 14px; }
            .toggle-group { display: flex; border-radius: 6px; overflow: hidden; background: #0B0E14; padding: 2px; }
            .toggle-btn { flex: 1; padding: 8px; border: none; background: transparent; color: var(--text-muted); font-weight: 600; cursor: pointer; border-radius: 4px; }
            .toggle-btn.active.buy { background: var(--upstox-green); color: white; }
            .toggle-btn.active.sell { background: var(--upstox-red); color: white; }
            .form-group { display: flex; flex-direction: column; gap: 5px; }
            label { font-size: 0.72rem; color: var(--text-muted); font-weight: 600; text-transform: uppercase; }
            input, select { background: #0B0E14; border: 1px solid var(--border-color); color: #fff; padding: 8px; border-radius: 6px; outline: none; font-size: 0.88rem; }
            .execute-btn { background: var(--upstox-green); border: none; color: white; font-weight: 700; padding: 10px; border-radius: 6px; cursor: pointer; font-size: 0.95rem; }
            .execute-btn.sell-mode { background: var(--upstox-red); }
            .holdings-container { border-top: 1px solid var(--border-color); padding-top: 14px; flex: 1; overflow-y: auto; }
            .position-card { background: #1C222D; padding: 8px; border-radius: 6px; margin-bottom: 6px; font-size: 0.82rem; }
            .position-card-row { display: flex; justify-content: space-between; margin-bottom: 3px; }
        </style>
    </head>
    <body>
        <header>
            <div class="brand">
                <img src="/icon.svg" alt="Logo" style="width: 32px; height: 32px; border-radius: 6px;">
                <span style="font-weight: 800; font-size: 1.1rem; letter-spacing: 0.5px;">VINTAGE<span style="color:#00FFA3;">CX</span></span>
                <span style="background: var(--upstox-purple); padding: 2px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: 700;">PRO</span>
            </div>
            <div class="funds">
                <div>Cash: <strong id="cash-balance" style="color:#fff;">₹1,00,000.00</strong></div>
                <div>Portfolio: <strong id="portfolio-value" style="color:#fff;">₹1,00,000.00</strong></div>
            </div>
        </header>

        <div class="main-container">
            <div class="watchlist-panel">
                <div class="panel-header">Watchlist (NSE)</div>
                <div class="watchlist-items" id="watchlist"></div>
            </div>

            <div class="chart-panel">
                <div class="chart-header">
                    <div class="chart-title" id="selected-symbol-title">RELIANCE</div>
                    <div style="color: var(--text-muted); font-size: 0.8rem;">Live 1-Sec Ticks</div>
                </div>
                <div id="chart-container"></div>
            </div>

            <div class="order-panel">
                <div class="toggle-group">
                    <button class="toggle-btn active buy" id="tab-buy" onclick="setSide('BUY')">BUY</button>
                    <button class="toggle-btn" id="tab-sell" onclick="setSide('SELL')">SELL</button>
                </div>

                <div class="form-group">
                    <label>Order Type</label>
                    <select id="order-type">
                        <option value="MARKET">Market</option>
                        <option value="LIMIT">Limit</option>
                    </select>
                </div>

                <div class="form-group">
                    <label>Quantity</label>
                    <input type="number" id="order-qty" value="1" min="1" />
                </div>

                <div class="form-group">
                    <label>Limit Price (₹)</label>
                    <input type="number" id="order-price" value="0.0" step="0.05" />
                </div>

                <button class="execute-btn" id="submit-btn" onclick="submitOrder()">BUY RELIANCE</button>
                <div id="order-alert" style="font-size: 0.78rem; text-align: center;"></div>

                <div class="holdings-container">
                    <div class="panel-header" style="padding-left:0; margin-bottom: 6px;">Holdings</div>
                    <div id="positions-list"></div>
                </div>
            </div>
        </div>

        <script>
            if ('serviceWorker' in navigator) {
                navigator.serviceWorker.register('/sw.js');
            }

            let currentSymbol = "RELIANCE";
            let currentSide = "BUY";
            
            const chartContainer = document.getElementById("chart-container");
            const chart = LightweightCharts.createChart(chartContainer, {
                layout: { background: { color: "#0B0E14" }, textColor: "#848E9C" },
                grid: { vertLines: { color: "#151922" }, horzLines: { color: "#151922" } },
                timeScale: { timeVisible: true, secondsVisible: true }
            });

            const candleSeries = chart.addCandlestickSeries({
                upColor: "#089981", downColor: "#F23645",
                borderDownColor: "#F23645", borderUpColor: "#089981",
                wickDownColor: "#F23645", wickUpColor: "#089981",
            });

            function seedCandles(basePrice) {
                const now = Math.floor(Date.now() / 1000);
                const historicalData = [];
                for (let i = 40; i > 0; i--) {
                    let t = now - (i * 2);
                    let open = basePrice + Math.sin(i) * 4;
                    let high = open + Math.random() * 2;
                    let low = open - Math.random() * 2;
                    let close = open + (Math.random() - 0.5) * 3;
                    historicalData.push({ time: t, open: open, high: high, low: low, close: close });
                }
                candleSeries.setData(historicalData);
            }
            seedCandles(2980);

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
                    submitBtn.className = "execute-btn";
                    submitBtn.innerText = `BUY ${currentSymbol}`;
                } else {
                    sellBtn.className = "toggle-btn active sell";
                    buyBtn.className = "toggle-btn";
                    submitBtn.className = "execute-btn sell-mode";
                    submitBtn.innerText = `SELL ${currentSymbol}`;
                }
            }

            function selectStock(sym, price) {
                currentSymbol = sym;
                document.getElementById("selected-symbol-title").innerText = sym;
                setSide(currentSide);
                document.querySelectorAll(".stock-item").forEach(el => el.classList.remove("active"));
                const target = document.getElementById(`stock-${sym}`);
                if (target) target.classList.add("active");
                if (price) seedCandles(price);
            }

            const wsProtocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const ws = new WebSocket(`${wsProtocol}//${location.host}/ws/market-feed`);
            
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                
                const wl = document.getElementById("watchlist");
                wl.innerHTML = Object.entries(data.market).map(([sym, item]) => `
                    <div class="stock-item ${sym === currentSymbol ? 'active' : ''}" id="stock-${sym}" onclick="selectStock('${sym}', ${item.price})">
                        <div>
                            <div class="stock-name">${sym}</div>
                            <div class="stock-sub">NSE EQ</div>
                        </div>
                        <div class="stock-price">₹${item.price.toFixed(2)}</div>
                    </div>
                `).join("");

                if (data.candles[currentSymbol]) {
                    candleSeries.update(data.candles[currentSymbol]);
                }

                document.getElementById("cash-balance").innerText = `₹${data.portfolio.cash_balance.toLocaleString('en-IN', {minimumFractionDigits: 2})}`;
                document.getElementById("portfolio-value").innerText = `₹${data.portfolio.total_portfolio_value.toLocaleString('en-IN', {minimumFractionDigits: 2})}`;

                const posContainer = document.getElementById("positions-list");
                const pos = data.portfolio.positions;
                if (!pos || pos.length === 0) {
                    posContainer.innerHTML = `<div style="color:var(--text-muted); font-size:0.8rem;">No open positions</div>`;
                } else {
                    posContainer.innerHTML = pos.map(p => `
                        <div class="position-card">
                            <div class="position-card-row"><strong>${p.symbol}</strong><span>Qty: ${p.quantity}</span></div>
                            <div class="position-card-row" style="color:var(--text-muted);">
                                <span>LTP: ₹${p.current_price.toFixed(2)}</span>
                                <span style="color:#089981;">₹${p.market_value.toFixed(2)}</span>
                            </div>
                        </div>
                    `).join("");
                }
            };

            async function submitOrder() {
                const payload = {
                    symbol: currentSymbol,
                    side: currentSide,
                    order_type: document.getElementById("order-type").value,
                    quantity: parseInt(document.getElementById("order-qty").value),
                    price: parseFloat(document.getElementById("order-price").value) || 0.0
                };

                const res = await fetch("/api/order", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });
                const result = await res.json();
                const alertEl = document.getElementById("order-alert");
                alertEl.innerText = result.status === "FILLED" 
                    ? `✓ Executed: ${result.side} ${result.quantity} ${result.symbol} @ ₹${result.execution_price}`
                    : `✗ Rejected: ${result.reason}`;
                alertEl.style.color = result.status === "FILLED" ? "#089981" : "#F23645";
            }
        </script>
    </body>
    </html>
    """
