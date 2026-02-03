# Polymarket Trading Bot

## Project Overview
Collection of trading bots and tools for Polymarket prediction markets, primarily focused on crypto 15-minute resolution markets.

## Key Files
- `sniper.py` — Multi-asset 15m resolution sniper (BTC, ETH, SOL, XRP). WebSocket-based, monitors order books and buys when price hits target.
- `arb.py` — Arbitrage bot
- `lp.py` — Liquidity provision bot
- `esport_sniper.py` — Esports market sniper
- `market_watcher.py` — Market monitoring tool
- `audio_agent.py` / `audio_monitor.py` — Audio-based monitoring tools
- `export_polymarket_activity.py` — Activity export utility

## Environment Variables
- `PRIVATE_KEY` — Polygon wallet private key for signing transactions
- `FUNDER_ADDRESS` — Funder wallet address
- `CLOB_API_URL` — Optional override for CLOB API endpoint (defaults to `https://clob.polymarket.com`)

## APIs Used
- **CLOB API** (`clob.polymarket.com`) — Order placement and order book queries via `py_clob_client`
- **Gamma API** (`gamma-api.polymarket.com`) — Market/event discovery by slug
- **WebSocket** (`ws-subscriptions-clob.polymarket.com`) — Real-time order book updates

## Dependencies
- `py_clob_client` — Polymarket CLOB client (provides `ClobClient`, `OrderArgs`, `OrderType`)
- `websocket-client` — WebSocket connections (`WebSocketApp`)
- `requests` — HTTP calls to Gamma API

## sniper.py Architecture
- `SniperMonitor` class manages one market's WebSocket connection and order book state
- `monitor_asset()` runs per-asset in its own thread, handles interval transitions
- `monitor_all_assets()` spawns threads for all configured assets
- **Stale data protection**: `warmed_up` flag skips initial snapshots; price sum check (`<= 1.15`) blocks stale data where both UP+DOWN show ~$0.99
- **Price drift check**: REST API verification before trade execution (blocks if price moved >$0.02)
- **Auto-reconnect**: Exponential backoff on WebSocket disconnect
- **Thread safety**: `_trade_lock` for order execution, `_print_lock` for console output
- ANSI escape codes used for in-place status line updates per asset

## Running
```bash
# Set environment variables first
export PRIVATE_KEY="your_private_key"
export FUNDER_ADDRESS="your_funder_address"

# Run the multi-asset sniper
python sniper.py
```

## Known Gotchas
- WebSocket initial book snapshot contains stale prices (both sides ~$0.99). The `warmed_up` flag + price sum upper bound check (`<= 1.15`) guard against this. Do not remove both protections.
- Illiquid markets may have low price sums (e.g. $0.24). The stale check only blocks HIGH sums to allow thin books through.
- Market slugs follow the pattern `{asset}-updown-15m-{unix_timestamp}` (e.g. `btc-updown-15m-1770034500`).
