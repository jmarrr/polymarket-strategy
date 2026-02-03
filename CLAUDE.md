# Polymarket Trading Bot

## Project Overview
Collection of trading bots and tools for Polymarket prediction markets, primarily focused on crypto 15-minute resolution markets.

## Key Files

### Proven Working
- `sniper.py` — Multi-asset 15m resolution sniper (BTC, ETH, SOL, XRP). WebSocket-based, monitors order books and buys when price hits target.
- `dashboard.py` — Flask web dashboard for real-time monitoring

### Currently Testing
- `overreaction.py` — Overreaction strategy (experimental)

## Deployment Files
- `setup.sh` — One-command server setup script
- `sniper.service` — systemd service for auto-restart
- `requirements.txt` — Python dependencies

## Environment Variables
- `PRIVATE_KEY` — Polygon wallet private key for signing transactions
- `FUNDER_ADDRESS` — Funder wallet address (Safe/proxy wallet)
- `CLOB_API_URL` — Optional override for CLOB API endpoint (defaults to `https://clob.polymarket.com`)

## APIs Used
- **CLOB API** (`clob.polymarket.com`) — Order placement via `py_clob_client`
- **Gamma API** (`gamma-api.polymarket.com`) — Market/event discovery by slug
- **WebSocket** (`ws-subscriptions-clob.polymarket.com`) — Real-time order book updates

## Dependencies
- `py_clob_client` — Polymarket CLOB client (provides `ClobClient`, `OrderArgs`, `OrderType`)
- `websocket-client` — WebSocket connections (`WebSocketApp`)
- `requests` — HTTP calls to Gamma API
- `flask` — Web dashboard server

## sniper.py Architecture
- `SniperMonitor` class manages one market's WebSocket connection and order book state
- `monitor_asset()` runs per-asset in its own thread, handles interval transitions
- `monitor_all_assets()` spawns threads for all configured assets
- **Stale data protection**: `warmed_up` flag skips initial snapshots; price sum check (`<= 1.15`) blocks stale data where both UP+DOWN show ~$0.99
- **FOK orders**: Fill-or-Kill orders automatically fail if price doesn't exist at execution time
- **Auto-reconnect**: Exponential backoff on WebSocket disconnect
- **Thread safety**: `_trade_lock` for order execution, `_print_lock` for console output
- **Dashboard integration**: Shares data with web dashboard via `_dashboard_data` dict
- **Position tracking**: `MAX_TOTAL_EXPOSURE` limits total capital at risk
- **Trade logging**: All trades logged to `logs/trades.log`

## Time-Based Target Pricing
More aggressive targets as time runs out:
```python
PRICE_TIERS = [
    (30, 0.85),   # <= 30s remaining: $0.85
    (60, 0.92),   # <= 60s remaining: $0.92
    (float('inf'), 0.98),  # > 60s: $0.98 (conservative)
]
```

## Trading Configuration
- `EXECUTE_TRADES` — Enable/disable actual trading
- `MAX_POSITION_SIZE` — Maximum USDC per trade ($50 default)
- `MAX_TOTAL_EXPOSURE` — Maximum total USDC across all positions ($200 default)
- `AUTO_SNIPE` — Automatically execute when opportunity found

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for full step-by-step instructions.

## Known Gotchas
- WebSocket initial book snapshot contains stale prices (both sides ~$0.99). The `warmed_up` flag + price sum upper bound check (`<= 1.15`) guard against this. Do not remove both protections.
- Illiquid markets may have low price sums (e.g. $0.24). The stale check only blocks HIGH sums to allow thin books through.
- Market slugs follow the pattern `{asset}-updown-15m-{unix_timestamp}` (e.g. `btc-updown-15m-1770034500`).
- FOK orders fail silently if price moved — check dashboard error log for failed trades.
