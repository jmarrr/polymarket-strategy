"""
Multi-Asset 15m Resolution Sniper for Polymarket

Strategy: Monitor crypto 15-minute markets and buy when:
- Either UP or DOWN hits the target price
- Let it resolve to $1.00 for profit

Supports: Bitcoin, Ethereum, Solana, XRP
Uses WebSocket for real-time order book updates.
"""

import os
import json
import time
import logging
import requests
import threading
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from websocket import WebSocketApp

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY
from dotenv import load_dotenv

load_dotenv()

# API Configuration
CLOB_HOST = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")
GAMMA_HOST = "https://gamma-api.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com"
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
FUNDER = os.getenv("FUNDER_ADDRESS")

# Strategy Configuration
MONITORED_ASSETS = ["bitcoin", "ethereum", "solana", "xrp"]

# Time-based target price tiers (seconds_threshold, target_price)
# More aggressive as time runs out
PRICE_TIERS = [
    (30, 0.85),   # <= 30s: $0.85 (aggressive)
    (60, 0.92),   # <= 60s: $0.92 (medium)
    (float('inf'), 0.96),  # > 60s: $0.96 (conservative)
]


def get_target_price(seconds_remaining: int) -> float:
    """Get target price based on time remaining until resolution."""
    for threshold, price in PRICE_TIERS:
        if seconds_remaining <= threshold:
            return price
    return 0.98  # Fallback

# Trading Configuration
EXECUTE_TRADES = True  # Set to True to enable actual trading
MAX_POSITION_SIZE = 50 # Maximum USDC per trade
AUTO_SNIPE = True  # Automatically execute when opportunity found

# Global trading client
_trading_client = None
_trade_lock = threading.Lock()
_print_lock = threading.Lock()

# Per-asset status lines (updated in-place on screen)
_asset_status = {}  # {label: status_string}
_asset_order = []   # ordered list of labels for consistent display

# Gate output until all WebSockets are connected
_connected_count = 0
_expected_connections = len(MONITORED_ASSETS)
_all_connected = False

# Position tracking
_positions = {}  # {asset: {"side": str, "size": int, "price": float, "cost": float}}
_total_exposure = 0.0
_position_lock = threading.Lock()
MAX_TOTAL_EXPOSURE = 200  # Maximum total USDC across all positions

# Trade logger
_trade_logger = None


def _setup_trade_logger():
    """Setup file logger for trades."""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    logger = logging.getLogger("trades")
    logger.setLevel(logging.INFO)

    # Avoid duplicate handlers
    if not logger.handlers:
        handler = logging.FileHandler(log_dir / "trades.log")
        handler.setFormatter(logging.Formatter(
            '%(asctime)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        logger.addHandler(handler)
    return logger


def log_trade(asset: str, side: str, price: float, size: int, success: bool, order_id: str = ""):
    """Log a trade to file."""
    global _trade_logger
    if _trade_logger is None:
        _trade_logger = _setup_trade_logger()

    status = "SUCCESS" if success else "FAILED"
    cost = size * price
    _trade_logger.info(f"{status} | {asset} | {side} | ${price:.4f} | {size} shares | ${cost:.2f} | {order_id}")


def can_open_position(cost: float) -> bool:
    """Check if we can open a position without exceeding max exposure."""
    with _position_lock:
        return (_total_exposure + cost) <= MAX_TOTAL_EXPOSURE


def record_position(asset: str, side: str, size: int, price: float):
    """Record a new position."""
    global _total_exposure
    with _position_lock:
        cost = size * price
        _positions[asset] = {"side": side, "size": size, "price": price, "cost": cost}
        _total_exposure += cost


def get_total_exposure() -> float:
    """Get current total exposure across all positions."""
    with _position_lock:
        return _total_exposure


def _refresh_status():
    """Redraw all asset status lines in place."""
    if not _asset_order:
        return
    # Move cursor up N lines, overwrite each, then move back down
    n = len(_asset_order)
    lines = []
    for label in _asset_order:
        line = _asset_status.get(label, "")
        lines.append(f"\r{line}\033[K")  # \033[K clears rest of line
    output = f"\033[{n}A" + "\n".join(lines) + "\n"
    print(output, end="", flush=True)


def _update_asset_status(label: str, status: str):
    """Thread-safe update of an asset's status line."""
    global _all_connected
    with _print_lock:
        if not _all_connected:
            _asset_status[label] = status
            if label not in _asset_order:
                _asset_order.append(label)
            return
        if label not in _asset_status:
            _asset_order.append(label)
            _asset_status[label] = status
            # Print a new blank line to reserve space
            print(status, flush=True)
        else:
            _asset_status[label] = status
            _refresh_status()


def get_trading_client():
    """Get or create the trading client."""
    global _trading_client
    if _trading_client is None:
        if not PRIVATE_KEY or not FUNDER:
            raise ValueError("PRIVATE_KEY and FUNDER_ADDRESS required for trading")
        _trading_client = ClobClient(CLOB_HOST, key=PRIVATE_KEY, chain_id=POLYGON, signature_type=2, funder=FUNDER)
        _trading_client.set_api_creds(_trading_client.create_or_derive_api_creds())
    return _trading_client


# Read-only client for order book queries (no API creds needed)
_read_client = None

def get_read_client():
    """Get or create a read-only client for order book queries."""
    global _read_client
    if _read_client is None:
        _read_client = ClobClient(CLOB_HOST, chain_id=POLYGON)
    return _read_client


def get_current_et_time():
    """Get current time in Eastern Time."""
    try:
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        from datetime import timezone
        utc_now = datetime.now(timezone.utc)
        et_offset = timedelta(hours=-5)  # EST
        return utc_now + et_offset


def get_15m_interval_timestamp() -> int:
    """Get Unix timestamp for the current 15-minute interval."""
    et_now = get_current_et_time()
    minute = (et_now.minute // 15) * 15
    interval_time = et_now.replace(minute=minute, second=0, microsecond=0)
    return int(interval_time.timestamp())


def generate_market_slug(base: str = "bitcoin") -> str:
    """Generate market slug for current 15-minute interval."""
    base_short = {"bitcoin": "btc", "ethereum": "eth", "solana": "sol", "xrp": "xrp"}.get(base, base)
    timestamp = get_15m_interval_timestamp()
    return f"{base_short}-updown-15m-{timestamp}"


def fetch_market_by_slug(slug: str) -> dict | None:
    """Fetch market data by slug."""
    try:
        # Try as event first
        resp = requests.get(
            f"{GAMMA_HOST}/events",
            params={"slug": slug, "limit": 1},
            timeout=10,
        )
        
        if resp.status_code == 200:
            data = resp.json()
            if data and len(data) > 0:
                return data[0]
        
        # Try as market
        resp = requests.get(
            f"{GAMMA_HOST}/markets",
            params={"slug": slug, "limit": 1},
            timeout=10,
        )
        
        if resp.status_code == 200:
            data = resp.json()
            if data and len(data) > 0:
                market = data[0]
                return {"title": market.get("question", ""), "markets": [market], "slug": slug}
        
        return None
    except Exception as e:
        print(f"❌ Error fetching market: {e}")
        return None


def get_order_book(token_id: str) -> dict | None:
    """Fetch order book for a token via REST API."""
    try:
        client = get_read_client()
        book = client.get_order_book(token_id)
        
        # Handle OrderBookSummary object - convert to dict format
        if hasattr(book, 'asks') and hasattr(book, 'bids'):
            # It's an OrderBookSummary object
            asks = []
            bids = []
            
            # Convert asks
            if book.asks:
                for ask in book.asks:
                    if hasattr(ask, 'price') and hasattr(ask, 'size'):
                        asks.append({"price": str(ask.price), "size": str(ask.size)})
                    elif isinstance(ask, dict):
                        asks.append(ask)
            
            # Convert bids
            if book.bids:
                for bid in book.bids:
                    if hasattr(bid, 'price') and hasattr(bid, 'size'):
                        bids.append({"price": str(bid.price), "size": str(bid.size)})
                    elif isinstance(bid, dict):
                        bids.append(bid)
            
            # Sort asks ascending, bids descending (best prices first)
            asks.sort(key=lambda x: float(x["price"]))
            bids.sort(key=lambda x: float(x["price"]), reverse=True)
            return {"asks": asks, "bids": bids}

        # Already a dict
        return book
    except Exception as e:
        return None


class SniperMonitor:
    """WebSocket-based order book monitor for sniping near resolution."""
    
    def __init__(self, market_info: dict, asset_label: str = "", interval_end_unix: int = 0):
        self.asset_label = asset_label.upper()
        self.market_info = market_info
        self.interval_end_unix = interval_end_unix
        self.question = market_info.get('question', '')
        self.outcomes = json.loads(market_info.get("outcomes", "[]"))
        self.token_ids = json.loads(market_info.get("clobTokenIds", "[]"))
        
        # Determine UP/DOWN indices
        outcome_0_lower = self.outcomes[0].lower() if self.outcomes else ""
        self.up_idx = 0 if outcome_0_lower in ["yes", "up"] else 1
        self.down_idx = 1 if self.up_idx == 0 else 0
        
        self.up_token = self.token_ids[self.up_idx] if len(self.token_ids) > self.up_idx else None
        self.down_token = self.token_ids[self.down_idx] if len(self.token_ids) > self.down_idx else None
        
        # Order book state
        self.orderbooks = {}
        self.ws = None
        self.running = False
        self.snipe_executed = False
        self.update_count = 0
        self.warmed_up = False  # Skip initial stale book snapshots
        self.stopped = False
        
        # Current prices (updated in real-time)
        self.up_price = 0.0
        self.up_size = 0.0
        self.down_price = 0.0
        self.down_size = 0.0
    
    def on_message(self, ws, message):
        """Handle incoming WebSocket messages."""
        if message == "PONG":
            return
        
        try:
            data = json.loads(message)
            self.process_message(data)
        except json.JSONDecodeError:
            pass
    
    def process_message(self, data):
        """Process order book update message."""
        # Handle dict format (price_changes messages)
        if isinstance(data, dict):
            price_changes = data.get("price_changes", [])
            for change in price_changes:
                asset_id = change.get("asset_id")
                side = change.get("side")
                price = change.get("price")
                size = change.get("size")
                
                if asset_id and side and price is not None:
                    if asset_id not in self.orderbooks:
                        self.orderbooks[asset_id] = {"bids": [], "asks": []}
                    book_side = "bids" if side == "BUY" else "asks"
                    self.update_book_level(asset_id, book_side, price, size)
            
            if price_changes:
                self.warmed_up = True
                self.check_snipe_opportunity()
            return
        
        # Handle list format (initial book snapshots)
        if not isinstance(data, list):
            return
        
        for event in data:
            event_type = event.get("event_type")
            asset_id = event.get("asset_id")
            
            if event_type == "book" and asset_id:
                self.orderbooks[asset_id] = {
                    "bids": event.get("bids", []),
                    "asks": event.get("asks", [])
                }
                # Don't trade on initial snapshot (often stale)
                self.check_snipe_opportunity()
            
            elif event_type == "price_change" and asset_id:
                if asset_id not in self.orderbooks:
                    self.orderbooks[asset_id] = {"bids": [], "asks": []}

                changes = event.get("changes", [])
                for change in changes:
                    side = change.get("side")
                    price = change.get("price")
                    size = change.get("size")

                    if side and price is not None:
                        book_side = "bids" if side == "BUY" else "asks"
                        self.update_book_level(asset_id, book_side, price, size)

                self.warmed_up = True
                self.check_snipe_opportunity()
    
    def update_book_level(self, asset_id: str, side: str, price: str, size: str):
        """Update a single price level in the order book."""
        book = self.orderbooks.get(asset_id, {"bids": [], "asks": []})
        levels = book.get(side, [])
        
        levels = [l for l in levels if l.get("price") != price]
        
        if float(size) > 0:
            levels.append({"price": price, "size": size})
        
        if side == "bids":
            levels.sort(key=lambda x: float(x["price"]), reverse=True)
        else:
            levels.sort(key=lambda x: float(x["price"]))
        
        book[side] = levels
        self.orderbooks[asset_id] = book
    
    def check_snipe_opportunity(self):
        """Check for snipe opportunity with current order book state."""
        self.update_count += 1
        
        if not self.up_token or not self.down_token:
            return
        
        up_book = self.orderbooks.get(self.up_token, {})
        down_book = self.orderbooks.get(self.down_token, {})
        
        up_asks = up_book.get("asks", [])
        down_asks = down_book.get("asks", [])
        
        if not up_asks or not down_asks:
            return
        
        # Update current prices
        self.up_price = float(up_asks[0]["price"])
        self.up_size = float(up_asks[0]["size"])
        self.down_price = float(down_asks[0]["price"])
        self.down_size = float(down_asks[0]["size"])
        
        # Build countdown MM:SS from slug's interval end (matches Polymarket server time)
        total_secs = max(0, self.interval_end_unix - int(time.time()))
        mins = total_secs // 60
        secs = total_secs % 60

        # Get dynamic target price based on time remaining
        target = get_target_price(total_secs)

        # Build status line (pad tag to align columns)
        tag = f"[{self.asset_label}]" if self.asset_label else ""
        tag = tag.ljust(10)
        status = (
            f"{tag} "
            f"⏱️ {mins:02d}:{secs:02d} | "
            f"🎯 ${target:.2f} | "
            f"UP: ${self.up_price:.2f} | "
            f"DOWN: ${self.down_price:.2f} | "
        )

        # Sanity check: stale snapshots show both sides ~$0.99 (sum ~$1.98)
        # Only block high sums; low sums from thin/illiquid books are fine
        price_sum = self.up_price + self.down_price
        prices_valid = price_sum <= 1.15

        # Check if price hits target
        if not self.snipe_executed:
            if not self.warmed_up:
                status += "⏳ Warming up"
            elif not prices_valid:
                status += f"⚠️ Stale (sum=${price_sum:.2f})"
            else:
                opportunity = self.get_best_opportunity(target)

                if opportunity:
                    if EXECUTE_TRADES and AUTO_SNIPE:
                        # Check if already sniped or currently attempting
                        with _trade_lock:
                            if self.snipe_executed:
                                status += "✅ SNIPED!"
                                _update_asset_status(self.asset_label, status)
                                return
                            if hasattr(self, '_attempting_snipe') and self._attempting_snipe:
                                _update_asset_status(self.asset_label, status)
                                return
                            self._attempting_snipe = True

                        try:
                            success = execute_snipe(opportunity, target_price=target, monitor_label=self.asset_label)
                            if success:
                                with _trade_lock:
                                    self.snipe_executed = True
                                status += "✅ SNIPED!"
                                # Print permanent log line (won't be overwritten by status refresh)
                                with _print_lock:
                                    print(f"\n🎉 SNIPED {self.asset_label} {opportunity['side']} @ ${opportunity['price']:.2f}\n")
                        finally:
                            self._attempting_snipe = False
                        _update_asset_status(self.asset_label, status)
                    elif EXECUTE_TRADES:
                        status += f"🎯 {opportunity['side']} @ ${opportunity['price']:.2f}"
                        _update_asset_status(self.asset_label, status)
                        confirm = input("\n   Execute snipe? (y/n): ").strip().lower()
                        if confirm == "y":
                            success = execute_snipe(opportunity, target_price=target, monitor_label=self.asset_label)
                            if success:
                                self.snipe_executed = True
                                status = f"[{self.asset_label}]".ljust(12) + f"⏱️ {mins:02d}:{secs:02d} | 🎯 ${target:.2f} | UP: ${self.up_price:.2f} | DOWN: ${self.down_price:.2f} | ✅ SNIPED!"
                                _update_asset_status(self.asset_label, status)
                    else:
                        _update_asset_status(self.asset_label, f"[{self.asset_label}]".ljust(12) + "| ⚠️ Trading disabled")
                        self.snipe_executed = True
                    return
        elif self.snipe_executed:
            status += "✅ SNIPED!"

        _update_asset_status(self.asset_label, status)
    
    def get_best_opportunity(self, target_price: float) -> dict | None:
        """Get best snipe opportunity if price in range."""
        opportunities = []

        # Use small epsilon to match display rounding (0.9795 displays as $0.98)
        epsilon = 0.005
        if self.up_price >= (target_price - epsilon) and self.up_size > 0:
            opportunities.append({
                "side": "UP",
                "outcome": self.outcomes[self.up_idx],
                "token_id": self.up_token,
                "price": self.up_price,
                "size": self.up_size,
                "profit_per_share": 1.0 - self.up_price,
                "roi_percent": ((1.0 - self.up_price) / self.up_price) * 100,
            })

        if self.down_price >= (target_price - epsilon) and self.down_size > 0:
            opportunities.append({
                "side": "DOWN",
                "outcome": self.outcomes[self.down_idx],
                "token_id": self.down_token,
                "price": self.down_price,
                "size": self.down_size,
                "profit_per_share": 1.0 - self.down_price,
                "roi_percent": ((1.0 - self.down_price) / self.down_price) * 100,
            })
        
        if not opportunities:
            return None
        
        return max(opportunities, key=lambda x: x["price"])
    
    def on_error(self, ws, error):
        """Handle WebSocket errors."""
        _update_asset_status(self.asset_label, f"[{self.asset_label}]".ljust(12) + f"| ❌ WebSocket error: {str(error)[:30]}")

    def on_close(self, ws, close_status_code, close_msg):
        """Handle WebSocket close."""
        _update_asset_status(self.asset_label, f"[{self.asset_label}]".ljust(12) + f"| 🔌 WebSocket closed (code={close_status_code})")
        self.running = False

    def on_open(self, ws):
        """Handle WebSocket connection open."""
        global _connected_count, _all_connected

        # Reset warmup on reconnect so we don't trade on stale data
        self.warmed_up = False

        # Track connections; once all connected, flush buffered status lines
        _connected_count += 1
        if not _all_connected and _connected_count >= _expected_connections:
            _all_connected = True
            with _print_lock:
                print("✅ All WebSockets connected!\n")
                for label in _asset_order:
                    print(_asset_status.get(label, ""), flush=True)

        # Subscribe to market tokens
        subscribe_msg = {
            "assets_ids": [self.up_token, self.down_token],
            "type": "market"
        }
        ws.send(json.dumps(subscribe_msg))

        # Start ping thread
        def ping_loop():
            while self.running:
                try:
                    ws.send("PING")
                    time.sleep(10)
                except:
                    break

        self.running = True
        threading.Thread(target=ping_loop, daemon=True).start()

        # Start periodic status refresh thread (keeps display alive when no WS messages)
        def refresh_loop():
            while self.running:
                try:
                    self.check_snipe_opportunity()
                    time.sleep(2)
                except:
                    pass

        threading.Thread(target=refresh_loop, daemon=True).start()

        # Start periodic REST API sync to correct WebSocket drift
        def sync_loop():
            while self.running:
                try:
                    time.sleep(3)
                    if not self.running:
                        break
                    # Fetch both books first, then apply atomically
                    up_book = get_order_book(self.up_token) if self.up_token else None
                    down_book = get_order_book(self.down_token) if self.down_token else None
                    if up_book and down_book:
                        self.orderbooks[self.up_token] = up_book
                        self.orderbooks[self.down_token] = down_book
                    self.check_snipe_opportunity()
                except:
                    pass

        threading.Thread(target=sync_loop, daemon=True).start()

    def run(self):
        """Start the WebSocket connection with auto-reconnect."""
        ws_url = f"{WS_URL}/ws/market"
        max_retries = 10
        retry_count = 0

        while not self.stopped:
            self.ws = WebSocketApp(
                ws_url,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
                on_open=self.on_open
            )

            _update_asset_status(self.asset_label, f"[{self.asset_label}]".ljust(12) + "| 🔌 Connecting to WebSocket...")
            self.ws.run_forever(ping_interval=30, ping_timeout=10)

            if self.stopped:
                break

            # Only count as retry if connection failed (running was never set to True)
            # If it ran successfully for a while then disconnected, reset the counter
            if not self.running:
                retry_count += 1
            else:
                retry_count = 0  # Reset on successful connection that later dropped

            if retry_count > max_retries:
                _update_asset_status(self.asset_label, f"[{self.asset_label}]".ljust(12) + f"| ❌ Max reconnect attempts reached")
                break

            wait = min(2 ** retry_count, 30)
            _update_asset_status(self.asset_label, f"[{self.asset_label}]".ljust(12) + f"| 🔄 Reconnecting in {wait}s...")
            time.sleep(wait)
    
    def stop(self):
        """Stop the WebSocket connection permanently (no reconnect)."""
        self.stopped = True
        self.running = False
        if self.ws:
            self.ws.close()


def execute_snipe(opportunity: dict, size: int = None, target_price: float = 0.98, monitor_label: str = None) -> bool:
    """Execute snipe trade."""
    if not EXECUTE_TRADES:
        return False

    try:
        client = get_trading_client()

        # Verify orderbook liquidity is available before executing
        token_id = opportunity["token_id"]
        order_book = get_order_book(token_id)

        if not order_book:
            return False

        asks = order_book.get("asks", [])
        if not asks:
            return False

        # Verify there's liquidity at a reasonable price
        best_ask_price = float(asks[0].get("price", 0))
        best_ask_size = float(asks[0].get("size", 0))

        if best_ask_size <= 0:
            return False

        # Verify REST price also meets target (don't trust WebSocket alone)
        # Use same epsilon as WebSocket check for consistency
        epsilon = 0.005
        if best_ask_price < (target_price - epsilon):
            return False

        price = round(best_ask_price, 2)

        # Calculate position size
        if size is None:
            size = int(MAX_POSITION_SIZE / price)

        # Ensure minimum order value ($1)
        min_shares = int(1.0 / price) + 1
        if size < min_shares:
            size = min_shares

        # Cap by available liquidity (use fresh orderbook data)
        available = int(min(opportunity["size"], best_ask_size))
        if size > available:
            size = available

        if size < 1:
            return False

        # Check position limit
        cost = size * price
        if not can_open_position(cost):
            return False

        # Create and execute order
        order = OrderArgs(
            price=price,
            size=size,
            side=BUY,
            token_id=opportunity["token_id"]
        )

        with _trade_lock:
            signed_order = client.create_order(order)
            result = client.post_order(signed_order, OrderType.FOK)

        success = result.get("success", False)
        order_id = result.get("orderID", "")

        # Log the trade
        log_trade(monitor_label or "UNKNOWN", opportunity["side"], price, size, success, order_id)

        # Record position if successful
        if success:
            record_position(monitor_label or "UNKNOWN", opportunity["side"], size, price)

        return success

    except Exception as e:
        return False


def monitor_asset(asset: str):
    """Monitor loop for a single asset. Runs in its own thread."""
    label = asset.upper()
    current_slug = None
    monitor = None

    while True:
        try:
            slug = generate_market_slug(asset)

            # Check if we moved to a new interval
            if slug != current_slug:
                # Stop old monitor
                if monitor:
                    monitor.stop()
                    time.sleep(1)

                current_slug = slug
                # Calculate minutes remaining from slug timestamp
                slug_timestamp = int(slug.split("-")[-1])
                interval_end_unix = slug_timestamp + 900
                minutes_left = max(0, (interval_end_unix - int(time.time())) / 60)
                _update_asset_status(label, f"[{label}]".ljust(12) + f"| 🆕 New interval | Closes in {minutes_left:.1f}min")

                # Fetch market data
                event_data = fetch_market_by_slug(slug)

                if not event_data:
                    _update_asset_status(label, f"[{label}]".ljust(12) + "| ⏳ Waiting for market...")
                    time.sleep(5)
                    current_slug = None  # Reset to retry
                    continue

                markets = event_data.get("markets", [])
                if not markets:
                    _update_asset_status(label, f"[{label}]".ljust(12) + "| ⏳ No markets in event...")
                    time.sleep(5)
                    current_slug = None
                    continue

                # Get open market
                open_markets = [m for m in markets if not m.get('closed', False)]
                if not open_markets:
                    _update_asset_status(label, f"[{label}]".ljust(12) + "| ⏳ Market closed, waiting...")
                    time.sleep(5)
                    current_slug = None
                    continue

                market = open_markets[0]
                _update_asset_status(label, f"[{label}]".ljust(12) + f"| ✅ Found market, connecting...")

                # Extract end time from slug timestamp (start + 15min)
                slug_timestamp = int(slug.split("-")[-1])
                interval_end_unix = slug_timestamp + 900  # 15 minutes

                # Start WebSocket monitor
                monitor = SniperMonitor(market, asset_label=label, interval_end_unix=interval_end_unix)
                ws_thread = threading.Thread(target=monitor.run, daemon=True)
                ws_thread.start()

                # Monitor thread and check for interval end
                while ws_thread.is_alive():
                    new_slug = generate_market_slug(asset)
                    if new_slug != current_slug:
                        _update_asset_status(label, f"[{label}]".ljust(12) + "| 🔄 Interval ended, switching...")
                        monitor.stop()
                        break
                    time.sleep(1)
            else:
                time.sleep(5)

        except Exception as e:
            _update_asset_status(label, f"[{label}]".ljust(12) + f"| ❌ Error: {str(e)[:30]}")
            time.sleep(5)


def monitor_all_assets():
    """Main entry point: monitor all configured assets in parallel."""

    print(f"\n{'='*70}")
    print(f"🎯 MULTI-ASSET 15M RESOLUTION SNIPER (WebSocket)")
    print(f"{'='*70}")
    print(f"   Assets: {', '.join(a.upper() for a in MONITORED_ASSETS)}")
    print(f"   Target prices: ${PRICE_TIERS[-1][1]:.2f} (>60s) → ${PRICE_TIERS[1][1]:.2f} (30-60s) → ${PRICE_TIERS[0][1]:.2f} (<30s)")
    print(f"\n   💰 Trading: {'ENABLED' if EXECUTE_TRADES else 'DISABLED'}")
    if EXECUTE_TRADES:
        print(f"   📊 Max position: ${MAX_POSITION_SIZE} per trade")
        print(f"   🤖 Auto-snipe: {AUTO_SNIPE}")
    print(f"\n   🛑 Press Ctrl+C to stop")
    print(f"{'='*70}\n")

    # Pre-warm trading client if enabled
    if EXECUTE_TRADES:
        try:
            print("⚡ Pre-warming trading client...")
            get_trading_client()
            print("✅ Trading client ready!\n")
        except Exception as e:
            print(f"❌ Failed to init trading client: {e}\n")

    # Start one thread per asset
    threads = []
    for asset in MONITORED_ASSETS:
        t = threading.Thread(target=monitor_asset, args=(asset,), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(0.5)  # Stagger starts to avoid API burst

    # Main thread blocks until Ctrl+C
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n\n{'='*70}")
        print("🛑 MONITORING STOPPED")
        print(f"{'='*70}")


def main():
    """Main entry point."""
    monitor_all_assets()


if __name__ == "__main__":
    main()
