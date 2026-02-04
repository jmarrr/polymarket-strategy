# Polymarket Trading Bot

## Project Overview
Collection of trading bots and tools for Polymarket prediction markets, primarily focused on crypto 15-minute resolution markets.

**Note**: Must run locally (residential IP required). Polymarket blocks VPS/datacenter IPs via Cloudflare.

## Key Files

- `sniper.py` — Multi-asset 15m resolution sniper (BTC, ETH, SOL, XRP). WebSocket-based, monitors order books and buys when price hits target. Includes built-in web dashboard on port 5000.

## Environment Variables
- `PRIVATE_KEY` — Polygon wallet private key for signing transactions
- `FUNDER_ADDRESS` — Funder wallet address (Safe/proxy wallet)

## APIs Used
- **CLOB API** (`clob.polymarket.com`) — Order placement and order book queries via `py_clob_client`
- **Gamma API** (`gamma-api.polymarket.com`) — Market/event discovery by slug
- **WebSocket** (`ws-subscriptions-clob.polymarket.com`) — Real-time order book updates

## Dependencies
- `py_clob_client` — Polymarket CLOB client (provides `ClobClient`, `OrderArgs`, `OrderType`)
- `websocket-client` — WebSocket connections (`WebSocketApp`)
- `requests` — HTTP calls to Gamma API
- `flask` — Web dashboard server (integrated into sniper.py)
- `rich` — Terminal display formatting

## sniper.py Architecture
- `SniperMonitor` class manages one market's WebSocket connection and order book state
- `monitor_asset()` runs per-asset in its own thread, handles interval transitions
- `monitor_all_assets()` spawns threads for all configured assets
- **Stale data protection**: `warmed_up` flag skips initial snapshots; price sum check (`<= 1.15`) blocks stale data where both UP+DOWN show ~$0.99
- **FOK orders**: Fill-or-Kill orders ensure full position or nothing - no partial fills
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
    (float('inf'), 0.96),  # > 60s: $0.96 (conservative)
]
```

## Trading Configuration
- `EXECUTE_TRADES` — Enable/disable actual trading
- `MAX_POSITION_SIZE` — Maximum USDC per trade ($50 default)
- `MAX_TOTAL_EXPOSURE` — Maximum total USDC across all positions ($200 default)
- `AUTO_SNIPE` — Automatically execute when opportunity found

## Running

```bash
# Create .env file with credentials
PRIVATE_KEY=your_private_key
FUNDER_ADDRESS=your_funder_address

# Install dependencies
pip install -r requirements.txt

# Run the sniper (dashboard auto-starts on port 5000)
python sniper.py

# Access dashboard
http://localhost:5000
```

## Known Gotchas
- WebSocket initial book snapshot contains stale prices (both sides ~$0.99). The `warmed_up` flag + price sum upper bound check (`<= 1.15`) guard against this. Do not remove both protections.
- Illiquid markets may have low price sums (e.g. $0.24). The stale check only blocks HIGH sums to allow thin books through.
- Market slugs follow the pattern `{asset}-updown-15m-{unix_timestamp}` (e.g. `btc-updown-15m-1770034500`).
- FOK orders fail if liquidity is insufficient — check dashboard error log for failed trades.
- **VPS does not work** — Polymarket uses Cloudflare bot protection that blocks datacenter IPs. Must run locally with residential IP.
