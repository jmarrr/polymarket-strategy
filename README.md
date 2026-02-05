# Polymarket 15-Minute Sniper

Automated trading bot for Polymarket 15-minute crypto resolution markets. Monitors BTC, ETH, and SOL order books via WebSocket and executes trades when prices hit target thresholds.

**Must run locally** - Polymarket blocks VPS/datacenter IPs via Cloudflare.

## Setup

### 1. Install Dependencies

**Windows:**
```bash
pip install -r requirements.txt
```

**Linux/macOS:**
```bash
# Create virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment

Create a `.env` file:

```env
PRIVATE_KEY=your_polygon_private_key
FUNDER_ADDRESS=your_funder_wallet_address
```

- `PRIVATE_KEY` - Your Polygon wallet private key (for signing transactions)
- `FUNDER_ADDRESS` - Your funder/proxy wallet address (Safe wallet)

### 3. Configure Trading (in sniper.py)

```python
EXECUTE_TRADES = True       # Set True to enable trading
MAX_POSITION_SIZE = 50      # Max USDC per trade
MAX_TOTAL_EXPOSURE = 200    # Max total USDC at risk
AUTO_SNIPE = True           # Auto-execute when opportunity found
```

## Running

**Windows:**
```bash
python sniper.py
```

**Linux/macOS:**
```bash
source venv/bin/activate  # if using virtual environment
python3 sniper.py
```

The bot will:
- Connect to WebSocket for real-time order book updates
- Display live prices in terminal with Rich formatting
- Start web dashboard at `http://localhost:5000`
- Execute trades when price >= target threshold

## Web Dashboard

Access at `http://localhost:5000` to view:
- Live asset prices and timers
- Recent trades
- Error log

## Price Targets

Only trades in the final minute before resolution:

| Time Left | Target |
|-----------|--------|
| < 60s     | $0.96  |
| >= 60s    | Waiting (no trading) |

## Logs

Trade history saved to `logs/trades.log`
