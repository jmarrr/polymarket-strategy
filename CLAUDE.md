# Polymarket Trading Bot

## Project Overview
Collection of trading bots and tools for Polymarket prediction markets, primarily focused on crypto 15-minute resolution markets.

## Key Files

### Proven Working
- `sniper.py` — Multi-asset 15m resolution sniper (BTC, ETH, SOL, XRP). WebSocket-based, monitors order books and buys when price hits target.
- `claim.py` — Auto-claim script for redeeming winning positions (uses `polymarket-apis` package)

### Currently Testing
- `overreaction.py` — Overreaction strategy (experimental)

## Deployment Files
- `DEPLOYMENT.md` — Step-by-step Hetzner Cloud setup guide
- `setup.sh` — One-command server setup script
- `sniper.service` — systemd service for auto-restart
- `requirements.txt` — Python dependencies for sniper
- `requirements_claim.txt` — Python dependencies for claim script (separate venv recommended)

## Environment Variables
- `PRIVATE_KEY` — Polygon wallet private key for signing transactions
- `FUNDER_ADDRESS` — Funder wallet address (Safe/proxy wallet)
- `CLOB_API_URL` — Optional override for CLOB API endpoint (defaults to `https://clob.polymarket.com`)

## APIs Used
- **CLOB API** (`clob.polymarket.com`) — Order placement and order book queries via `py_clob_client`
- **Gamma API** (`gamma-api.polymarket.com`) — Market/event discovery by slug
- **Data API** (`data-api.polymarket.com`) — User positions, redeemable positions
- **WebSocket** (`ws-subscriptions-clob.polymarket.com`) — Real-time order book updates

## Dependencies
- `py_clob_client` — Polymarket CLOB client (provides `ClobClient`, `OrderArgs`, `OrderType`)
- `websocket-client` — WebSocket connections (`WebSocketApp`)
- `requests` — HTTP calls to Gamma API
- `polymarket-apis` — Third-party package for redeeming positions (requires Python >=3.12)

## sniper.py Architecture
- `SniperMonitor` class manages one market's WebSocket connection and order book state
- `monitor_asset()` runs per-asset in its own thread, handles interval transitions
- `monitor_all_assets()` spawns threads for all configured assets
- **Stale data protection**: `warmed_up` flag skips initial snapshots; price sum check (`<= 1.15`) blocks stale data where both UP+DOWN show ~$0.99
- **Price drift check**: REST API verification before trade execution (blocks if price < target)
- **Auto-reconnect**: Exponential backoff on WebSocket disconnect
- **Thread safety**: `_trade_lock` for order execution, `_print_lock` for console output
- **Inline status updates**: All messages use `_update_asset_status()` for in-place display (no scrolling)
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

## Running
```bash
# Set environment variables first
export PRIVATE_KEY="your_private_key"
export FUNDER_ADDRESS="your_funder_address"

# Run the multi-asset sniper
python sniper.py

# Run claim script (separate venv with polymarket-apis)
python claim.py
```

## Claiming Winnings
- `py_clob_client` does NOT support redeeming positions
- Use `claim.py` with `polymarket-apis` package (separate venv to avoid conflicts)
- Schedule via cron (Linux) or Task Scheduler (Windows) every 1-2 hours
- Capital gets locked until redeemed — important for 24/7 operation

## Deployment (Hetzner Cloud)
1. Create account at hetzner.com/cloud → Console
2. Create CX22 server (€3.29/mo) with Ubuntu 24.04, Ashburn VA location
3. SSH in and run: `curl -sSL https://raw.githubusercontent.com/jmarrr/polymarket-strategy/master/setup.sh | bash`
4. Configure `.env` with keys
5. Start: `systemctl start sniper`
6. Monitor: `journalctl -u sniper -f`

## Known Gotchas
- WebSocket initial book snapshot contains stale prices (both sides ~$0.99). The `warmed_up` flag + price sum upper bound check (`<= 1.15`) guard against this. Do not remove both protections.
- Illiquid markets may have low price sums (e.g. $0.24). The stale check only blocks HIGH sums to allow thin books through.
- Market slugs follow the pattern `{asset}-updown-15m-{unix_timestamp}` (e.g. `btc-updown-15m-1770034500`).
- Capital locks after winning trades until manually redeemed — use `claim.py` on a schedule for 24/7 operation.
- `polymarket-apis` requires Python >=3.12 and may conflict with `py_clob_client` — use separate venv.
