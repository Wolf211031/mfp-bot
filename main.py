import os
import sys
import time
import json
import uuid
import logging
import urllib.request
import urllib.parse
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse
import uvicorn

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("mfp_bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("MFPBot")

# --- CONFIGURATION ---
API_KEY = "fp_live_d1fc94efa55ae1c34e859ee7ede568d222d93edd1886179def9adf075fbbcff7"
BASE_URL = "https://developers.myfundedperpetuals.com"
ACCOUNT_ID = "jh71tm3e391rhw0pvmp5811k398f35v1"  # FP-13265579
MARKET_ID = "binance|BTCUSDT"
ORDER_SIZE = 0.01  # Ultra-low risk: 0.01 BTC (~$8.50 per 1% move)
LEVERAGE = 5
MARGIN_MODE = "cross"
SLIPPAGE_BPS = 50

# Strict Risk Controls
MIN_DAILY_LOSS_ROOM = 35.00  # Halt if daily loss room drops below $35

# In-memory history for live dashboard feed
signals_history = []
bot_start_time = time.time()

app = FastAPI(title="MyFundedPerps Agentic Dashboard & Webhook Bridge")

def api_request(endpoint: str, method: str = "GET", payload: dict = None):
    url = f"{BASE_URL}{endpoint}"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "Idempotency-Key": str(uuid.uuid4())
    }
    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            res_body = resp.read().decode("utf-8")
            return json.loads(res_body) if res_body else {}
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8")
        logger.error(f"HTTPError {e.code} on {method} {endpoint}: {err_msg}")
        try:
            return {"error": True, "code": e.code, "detail": json.loads(err_msg)}
        except:
            return {"error": True, "code": e.code, "detail": err_msg}
    except Exception as e:
        logger.error(f"Exception on {method} {endpoint}: {e}")
        return {"error": True, "detail": str(e)}

def get_account_detail():
    res = api_request(f"/v1/accounts/{ACCOUNT_ID}")
    if res.get("data"):
        return res["data"]
    return None

def get_open_positions():
    res = api_request(f"/v1/positions?account_id={ACCOUNT_ID}")
    if res.get("data") is not None:
        return res["data"]
    return []

def get_market_quote(side: str):
    quoted_market = urllib.parse.quote(MARKET_ID)
    res = api_request(f"/v1/markets/{quoted_market}/quote?side={side}&size={ORDER_SIZE}")
    if res.get("data"):
        return res["data"]
    return {}

def close_all():
    logger.info(f"Closing all positions for account {ACCOUNT_ID}...")
    res = api_request(f"/v1/accounts/{ACCOUNT_ID}/close-all-positions", method="POST", payload={})
    logger.info(f"Close all response: {res}")
    return res

@app.get("/api/status")
def get_status_json():
    acc = get_account_detail()
    positions = get_open_positions()
    risk = acc.get("risk", {}) if acc else {}
    return {
        "status": "online",
        "account_number": acc.get("account_number", "FP-13265579") if acc else "FP-13265579",
        "balance": acc.get("balance", 2541.23) if acc else 2541.23,
        "equity": risk.get("equity", 2541.23),
        "daily_loss_room": risk.get("daily_loss_room", 75.0),
        "max_drawdown_room": risk.get("max_drawdown_room", 116.23),
        "remaining_profit": risk.get("remaining_profit", 183.77),
        "open_positions": positions,
        "order_size": ORDER_SIZE,
        "market": "BTCUSDT Perpetual",
        "min_daily_loss_room": MIN_DAILY_LOSS_ROOM,
        "signals": signals_history[:25],
        "uptime_seconds": int(time.time() - bot_start_time)
    }

@app.post("/api/close-all")
def trigger_close_all():
    res = close_all()
    signals_history.insert(0, {
        "time": datetime.now().strftime("%H:%M:%S"),
        "type": "MANUAL FLATTEN",
        "details": "Closed all open positions from dashboard button",
        "status": "executed"
    })
    return {"status": "success", "result": res}

@app.post("/webhook")
async def handle_webhook(request: Request):
    t_start = time.time()
    try:
        raw_body = await request.body()
        body_text = raw_body.decode("utf-8")
        logger.info(f"Incoming Webhook: {body_text}")
        
        try:
            payload = json.loads(body_text)
        except Exception:
            payload = {}

        action = str(payload.get("action", "")).strip().lower()
        price = payload.get("price")
        ticker = payload.get("ticker", "BTCUSD")

        # 1. Official Risk Check directly from MyFundedPerps engine
        acc = get_account_detail()
        risk = acc.get("risk", {}) if acc else {}
        daily_loss_room = risk.get("daily_loss_room", 75.0)

        if daily_loss_room < MIN_DAILY_LOSS_ROOM:
            logger.error(f"RISK CHECK FAILED: daily_loss_room (${daily_loss_room:.2f}) < ${MIN_DAILY_LOSS_ROOM}. Rejected.")
            signals_history.insert(0, {
                "time": datetime.now().strftime("%H:%M:%S"),
                "action": action.upper() if action else "UNKNOWN",
                "price": price or "-",
                "status": "REJECTED (Floor Protected)",
                "latency_ms": int((time.time() - t_start) * 1000)
            })
            return JSONResponse(status_code=403, content={"status": "rejected", "reason": "Daily loss room below safe threshold"})

        # 2. Action Routing
        if action in ["exit", "close", "flat"]:
            logger.info("Executing EXIT signal: Closing all positions...")
            close_res = close_all()
            signals_history.insert(0, {
                "time": datetime.now().strftime("%H:%M:%S"),
                "action": "EXIT",
                "price": price or "-",
                "status": "EXECUTED (Position Closed)",
                "latency_ms": int((time.time() - t_start) * 1000)
            })
            return {"status": "success", "action": "exit", "result": close_res}

        elif action in ["buy", "long"]:
            positions = get_open_positions()
            for p in positions:
                m_id = p.get("market_id") or p.get("symbol") or ""
                p_side = str(p.get("side", "")).lower()
                if (MARKET_ID in m_id or "BTC" in m_id) and p_side == "buy":
                    signals_history.insert(0, {
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "action": "BUY",
                        "price": price or "-",
                        "status": "IGNORED (Already Long)",
                        "latency_ms": int((time.time() - t_start) * 1000)
                    })
                    return {"status": "ignored", "reason": "Already Long"}
                elif (MARKET_ID in m_id or "BTC" in m_id) and p_side == "sell":
                    close_all()
                    time.sleep(0.5)

            quote = get_market_quote("buy")
            expected_price = quote.get("estimated_fill_price") or quote.get("ask") or price
            if not expected_price:
                expected_price = 86000.0

            order_payload = {
                "client_order_id": f"gs-buy-{int(time.time()*1000)}",
                "type": "market",
                "account_id": ACCOUNT_ID,
                "market_id": MARKET_ID,
                "side": "buy",
                "size": ORDER_SIZE,
                "expected_price": float(expected_price),
                "leverage": LEVERAGE,
                "margin_mode": MARGIN_MODE,
                "slippage_tolerance_bps": SLIPPAGE_BPS
            }

            res = api_request("/v1/orders", method="POST", payload=order_payload)
            latency = int((time.time() - t_start) * 1000)
            
            if res.get("error"):
                status_label = f"FAILED: {res.get('code', 'API Error')}"
            else:
                status_label = "FILLED"

            signals_history.insert(0, {
                "time": datetime.now().strftime("%H:%M:%S"),
                "action": "BUY (0.01 BTC)",
                "price": round(float(expected_price), 2),
                "status": status_label,
                "latency_ms": latency
            })
            return {"status": "submitted", "order": order_payload, "response": res}

        elif action in ["sell", "short"]:
            positions = get_open_positions()
            for p in positions:
                m_id = p.get("market_id") or p.get("symbol") or ""
                p_side = str(p.get("side", "")).lower()
                if (MARKET_ID in m_id or "BTC" in m_id) and p_side == "sell":
                    signals_history.insert(0, {
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "action": "SELL",
                        "price": price or "-",
                        "status": "IGNORED (Already Short)",
                        "latency_ms": int((time.time() - t_start) * 1000)
                    })
                    return {"status": "ignored", "reason": "Already Short"}
                elif (MARKET_ID in m_id or "BTC" in m_id) and p_side == "buy":
                    close_all()
                    time.sleep(0.5)

            quote = get_market_quote("sell")
            expected_price = quote.get("estimated_fill_price") or quote.get("bid") or price
            if not expected_price:
                expected_price = 86000.0

            order_payload = {
                "client_order_id": f"gs-sell-{int(time.time()*1000)}",
                "type": "market",
                "account_id": ACCOUNT_ID,
                "market_id": MARKET_ID,
                "side": "sell",
                "size": ORDER_SIZE,
                "expected_price": float(expected_price),
                "leverage": LEVERAGE,
                "margin_mode": MARGIN_MODE,
                "slippage_tolerance_bps": SLIPPAGE_BPS
            }

            res = api_request("/v1/orders", method="POST", payload=order_payload)
            latency = int((time.time() - t_start) * 1000)

            if res.get("error"):
                status_label = f"FAILED: {res.get('code', 'API Error')}"
            else:
                status_label = "FILLED"

            signals_history.insert(0, {
                "time": datetime.now().strftime("%H:%M:%S"),
                "action": "SELL (0.01 BTC)",
                "price": round(float(expected_price), 2),
                "status": status_label,
                "latency_ms": latency
            })
            return {"status": "submitted", "order": order_payload, "response": res}

        else:
            signals_history.insert(0, {
                "time": datetime.now().strftime("%H:%M:%S"),
                "action": action.upper() if action else "PING/TEST",
                "price": price or "-",
                "status": f"IGNORED (Unknown action: {action})",
                "latency_ms": int((time.time() - t_start) * 1000)
            })
            return {"status": "ignored", "reason": f"Unknown action: {action}"}

    except Exception as e:
        logger.error(f"Error handling webhook: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def render_dashboard():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MyFundedPerps - Live Cloud Bot Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #090B0E;
            --card-bg: #12161C;
            --card-border: #1E2530;
            --accent-green: #00E676;
            --accent-green-glow: rgba(0, 230, 118, 0.15);
            --accent-blue: #2979FF;
            --accent-red: #FF5252;
            --text-main: #FFFFFF;
            --text-muted: #8B98A5;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background-color: var(--bg);
            color: var(--text-main);
            font-family: 'Inter', -apple-system, sans-serif;
            padding: 24px;
            min-height: 100vh;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 24px;
            border-bottom: 1px solid var(--card-border);
            margin-bottom: 28px;
        }
        .logo-area { display: flex; align-items: center; gap: 14px; }
        .logo-badge {
            background: linear-gradient(135deg, #00E676, #00B0FF);
            color: #000;
            font-weight: 800;
            padding: 6px 12px;
            border-radius: 8px;
            font-size: 13px;
            letter-spacing: 0.5px;
        }
        .logo-title { font-size: 20px; font-weight: 700; }
        .status-badge {
            display: flex;
            align-items: center;
            gap: 8px;
            background: var(--accent-green-glow);
            border: 1px solid rgba(0, 230, 118, 0.3);
            color: var(--accent-green);
            padding: 8px 16px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: 600;
        }
        .pulsing-dot {
            width: 8px;
            height: 8px;
            background-color: var(--accent-green);
            border-radius: 50%;
            box-shadow: 0 0 10px var(--accent-green);
            animation: pulse 2s infinite;
        }
        @keyframes pulse { 0% { opacity: 1; transform: scale(1); } 50% { opacity: 0.4; transform: scale(1.3); } 100% { opacity: 1; transform: scale(1); } }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 18px;
            margin-bottom: 24px;
        }
        .card {
            background-color: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 22px;
            position: relative;
            overflow: hidden;
            transition: transform 0.2s ease;
        }
        .card:hover { transform: translateY(-2px); }
        .card-label {
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: var(--text-muted);
            margin-bottom: 8px;
            font-weight: 600;
        }
        .card-value {
            font-family: 'JetBrains Mono', monospace;
            font-size: 28px;
            font-weight: 700;
            color: var(--text-main);
        }
        .card-subtext {
            font-size: 13px;
            margin-top: 8px;
            color: var(--text-muted);
        }
        .text-green { color: var(--accent-green); }
        .text-blue { color: var(--accent-blue); }
        .text-red { color: var(--accent-red); }

        .progress-bar-bg {
            background: #1E2530;
            height: 8px;
            border-radius: 4px;
            margin-top: 12px;
            overflow: hidden;
        }
        .progress-bar-fill {
            background: linear-gradient(90deg, #00E676, #00B0FF);
            height: 100%;
            border-radius: 4px;
            transition: width 0.5s ease;
        }

        .two-cols {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 24px;
        }
        @media (max-width: 850px) { .two-cols { grid-template-columns: 1fr; } }

        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }
        .section-title { font-size: 16px; font-weight: 700; letter-spacing: -0.2px; }

        .position-box {
            background: #171C24;
            border: 1px solid var(--card-border);
            border-radius: 10px;
            padding: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .flat-box {
            background: rgba(255, 255, 255, 0.02);
            border: 1px dashed var(--card-border);
            border-radius: 10px;
            padding: 24px;
            text-align: center;
            color: var(--text-muted);
            font-size: 14px;
        }

        .table-card {
            background-color: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 14px;
            padding: 20px;
            overflow-x: auto;
        }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th {
            text-align: left;
            padding: 12px 14px;
            color: var(--text-muted);
            border-bottom: 1px solid var(--card-border);
            font-weight: 600;
            text-transform: uppercase;
            font-size: 11px;
            letter-spacing: 0.5px;
        }
        td {
            padding: 14px;
            border-bottom: 1px solid rgba(255,255,255,0.03);
            font-family: 'JetBrains Mono', monospace;
        }
        .badge {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 700;
        }
        .badge-buy { background: rgba(0, 230, 118, 0.15); color: var(--accent-green); }
        .badge-sell { background: rgba(255, 82, 82, 0.15); color: var(--accent-red); }
        .badge-exit { background: rgba(41, 121, 255, 0.15); color: var(--accent-blue); }

        .btn-flatten {
            background: rgba(255, 82, 82, 0.15);
            border: 1px solid var(--accent-red);
            color: var(--accent-red);
            padding: 8px 16px;
            border-radius: 8px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }
        .btn-flatten:hover { background: var(--accent-red); color: #fff; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="logo-area">
                <span class="logo-badge">CLOUD AGENT</span>
                <span class="logo-title">MyFundedPerps Live Console</span>
            </div>
            <div class="status-badge">
                <span class="pulsing-dot"></span>
                <span>ENGINE ACTIVE & LISTENING</span>
            </div>
        </header>

        <div class="grid">
            <div class="card">
                <div class="card-label">Current Equity</div>
                <div class="card-value text-green" id="equity">$2,541.23</div>
                <div class="card-subtext" id="account-id">Account: FP-13265579</div>
            </div>

            <div class="card">
                <div class="card-label">Daily Loss Room</div>
                <div class="card-value text-blue" id="daily-room">$75.00</div>
                <div class="card-subtext">Safety Halt at $35.00</div>
                <div class="progress-bar-bg">
                    <div class="progress-bar-fill" id="daily-bar" style="width: 100%;"></div>
                </div>
            </div>

            <div class="card">
                <div class="card-label">Profit Remaining to Pass</div>
                <div class="card-value" id="remaining-target">$183.77</div>
                <div class="card-subtext">Challenge Target: $2,725.00</div>
                <div class="progress-bar-bg">
                    <div class="progress-bar-fill" id="target-bar" style="width: 82%;"></div>
                </div>
            </div>

            <div class="card">
                <div class="card-label">Trading Configuration</div>
                <div class="card-value" style="font-size: 22px;">0.01 BTC</div>
                <div class="card-subtext">5x Leverage | Cross Margin</div>
            </div>
        </div>

        <div class="two-cols">
            <div class="card">
                <div class="section-header">
                    <span class="section-title">Open Position</span>
                    <button class="btn-flatten" onclick="closeAllPositions()">Emergency Flatten</button>
                </div>
                <div id="position-container">
                    <div class="flat-box">Currently Flat (No Open Position)</div>
                </div>
            </div>

            <div class="card">
                <div class="section-header">
                    <span class="section-title">Webhook Configuration</span>
                </div>
                <div style="font-size: 13px; line-height: 1.6; color: var(--text-muted);">
                    <div><strong>Permanent Cloud Webhook:</strong></div>
                    <div style="background: #171C24; padding: 10px; border-radius: 6px; font-family: monospace; color: #00E676; margin: 8px 0; word-break: break-all;">
                        https://mfp-bot-5ogv.onrender.com/webhook
                    </div>
                    <div><strong>Engine:</strong> Golden Spring Pro v3 (3m BTC)</div>
                    <div><strong>Risk Per Trade:</strong> ~$8.50 per 1% move</div>
                </div>
            </div>
        </div>

        <div class="table-card">
            <div class="section-header">
                <span class="section-title">Live Signal Execution Feed</span>
                <span style="font-size: 12px; color: var(--text-muted);" id="refresh-time">Auto-refreshed</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>Action</th>
                        <th>Price</th>
                        <th>Latency</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody id="signals-tbody">
                    <tr>
                        <td colspan="5" style="text-align: center; color: var(--text-muted); padding: 24px;">
                            Waiting for incoming TradingView signals...
                        </td>
                    </tr>
                </tbody>
            </table>
        </div>
    </div>

    <script>
        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();

                document.getElementById('equity').innerText = '$' + parseFloat(data.equity).toFixed(2);
                document.getElementById('account-id').innerText = 'Account: ' + data.account_number;
                document.getElementById('daily-room').innerText = '$' + parseFloat(data.daily_loss_room).toFixed(2);
                document.getElementById('remaining-target').innerText = '$' + parseFloat(data.remaining_profit).toFixed(2);

                const dailyPct = Math.min(100, Math.max(0, (data.daily_loss_room / 75.0) * 100));
                document.getElementById('daily-bar').style.width = dailyPct + '%';

                const targetPct = Math.min(100, Math.max(0, ((225 - data.remaining_profit) / 225) * 100));
                document.getElementById('target-bar').style.width = targetPct + '%';

                const posContainer = document.getElementById('position-container');
                if (data.open_positions && data.open_positions.length > 0) {
                    const p = data.open_positions[0];
                    posContainer.innerHTML = `
                        <div class="position-box">
                            <div>
                                <div style="font-weight: 700; font-size: 15px;">${p.market_id || 'BTCUSDT'}</div>
                                <div style="font-size: 12px; color: var(--text-muted); margin-top: 4px;">Size: ${p.size} BTC | Entry: $${p.entry_price || 'Market'}</div>
                            </div>
                            <div style="text-align: right;">
                                <div class="badge ${p.side === 'buy' ? 'badge-buy' : 'badge-sell'}">${(p.side || 'LONG').toUpperCase()}</div>
                            </div>
                        </div>
                    `;
                } else {
                    posContainer.innerHTML = '<div class="flat-box">Currently Flat (No Open Position)</div>';
                }

                const tbody = document.getElementById('signals-tbody');
                if (data.signals && data.signals.length > 0) {
                    tbody.innerHTML = data.signals.map(s => `
                        <tr>
                            <td>${s.time}</td>
                            <td><span class="badge ${s.action && s.action.includes('BUY') ? 'badge-buy' : s.action && s.action.includes('SELL') ? 'badge-sell' : 'badge-exit'}">${s.action || 'SIGNAL'}</span></td>
                            <td>$${s.price || '-'}</td>
                            <td>${s.latency_ms ? s.latency_ms + 'ms' : '-'}</td>
                            <td style="color: ${s.status && (s.status.includes('REJECTED') || s.status.includes('FAILED')) ? '#FF5252' : '#00E676'};">${s.status}</td>
                        </tr>
                    `).join('');
                }

                document.getElementById('refresh-time').innerText = 'Last updated: ' + new Date().toLocaleTimeString();
            } catch (e) {
                console.error('Fetch error:', e);
            }
        }

        async function closeAllPositions() {
            if (confirm('Are you sure you want to close all open positions immediately?')) {
                const res = await fetch('/api/close-all', { method: 'POST' });
                alert('Flatten order sent to MyFundedPerps!');
                fetchStatus();
            }
        }

        fetchStatus();
        setInterval(fetchStatus, 3000);
    </script>
</body>
</html>
"""
    return html

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
