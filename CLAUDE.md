# Polymarket Trading Bot

## Project Overview
Collection of trading bots and tools for Polymarket prediction markets, focused on crypto up/down resolution markets (5m and 15m intervals).

**Note**: Must run locally (residential IP required). Polymarket blocks VPS/datacenter IPs via Cloudflare.

## Key Files

- `sniper.py` — Multi-asset resolution sniper (5m + 15m). WebSocket-based, monitors order books and buys when price hits target. Discord notifications for trades.

## Environment Variables
- `PRIVATE_KEY` — Polygon wallet private key for signing transactions
- `FUNDER_ADDRESS` — Funder wallet address (Safe/proxy wallet)
- `DISCORD_WEBHOOK_URL` — Discord webhook URL for trade notifications (optional)

## APIs Used
- **CLOB API** (`clob.polymarket.com`) — Order placement and order book queries via `py_clob_client`
- **Gamma API** (`gamma-api.polymarket.com`) — Market/event discovery by slug
- **WebSocket** (`ws-subscriptions-clob.polymarket.com`) — Real-time order book updates

## Dependencies
- `py_clob_client` — Polymarket CLOB client (provides `ClobClient`, `OrderArgs`, `OrderType`)
- `websocket-client` — WebSocket connections (`WebSocketApp`)
- `requests` — HTTP calls to Gamma API
- `rich` — Terminal display formatting

## sniper.py Architecture
- `SniperMonitor` class manages one market's WebSocket connection and order book state
- `monitor_asset(asset, interval_minutes)` runs per-asset+interval in its own thread, handles interval transitions
- `monitor_all_assets()` spawns threads for all configured assets
- **Stale data protection**: `warmed_up` flag skips initial snapshots; price sum check (`<= 1.15`) blocks stale data where both UP+DOWN show ~$0.99
- **FOK orders**: Fill-or-Kill orders require full fill or entire order is cancelled (no partial fills)
- **Auto-reconnect**: Exponential backoff on WebSocket disconnect
- **Thread safety**: `_trade_lock` for order execution, `_print_lock` for console output
- **Position tracking**: `MAX_TOTAL_EXPOSURE` limits total capital at risk
- **Trade logging**: All trades logged to `logs/trades.log`

## Time-Based Target Pricing
More aggressive targets as time runs out:
```python
PRICE_TIERS = [
    (60, 0.98),    # < 60s (1min): $0.98
]
```

## Discord Notifications
Set `DISCORD_WEBHOOK_URL` in `.env` to receive notifications for:
- Successful trades, failed trades

## Trading Configuration
- `EXECUTE_TRADES` — Enable/disable actual trading
- `MAX_POSITION_SIZE` — Maximum USDC per trade ($100 default)
- `MAX_TOTAL_EXPOSURE` — Maximum total USDC across all positions ($500 default)
- `AUTO_SNIPE` — Automatically execute when opportunity found

## Running

```bash
# Create .env file with credentials
PRIVATE_KEY=your_private_key
FUNDER_ADDRESS=your_funder_address

# Install dependencies
pip install -r requirements.txt

# Run the sniper
python sniper.py
```

## Known Gotchas
- WebSocket initial book snapshot contains stale prices (both sides ~$0.99). The `warmed_up` flag + price sum upper bound check (`<= 1.15`) guard against this. Do not remove both protections.
- Illiquid markets may have low price sums (e.g. $0.24). The stale check only blocks HIGH sums to allow thin books through.
- Market slugs follow the pattern `{asset}-updown-{interval}-{unix_timestamp}` (e.g. `btc-updown-15m-1770034500`, `btc-updown-5m-1770034500`).
- FOK orders fail if liquidity is insufficient for full order — check Discord/logs for failed trades.
- **VPS does not work** — Polymarket uses Cloudflare bot protection that blocks datacenter IPs. Must run locally with residential IP.
