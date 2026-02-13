"""
Order Book Data Recorder for Polymarket

Records every order book event (snapshots + updates) to CSV files
for analysis of price movements, liquidity, and spread dynamics.

Usage:
    python recorder.py                          # defaults: bitcoin, 5m
    python recorder.py --asset ethereum --interval 15
"""

import os
import csv
import json
import time
import argparse
import threading
import requests
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from websocket import WebSocketApp

# API Configuration
GAMMA_HOST = "https://gamma-api.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com"

# Output directory
LOG_DIR = Path("logs")


def get_current_et_time():
    """Get current time in Eastern Time."""
    try:
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        from datetime import timezone
        utc_now = datetime.now(timezone.utc)
        return utc_now + timedelta(hours=-5)


def get_interval_timestamp(interval_minutes: int) -> int:
    """Get Unix timestamp for the current interval."""
    et_now = get_current_et_time()
    minute = (et_now.minute // interval_minutes) * interval_minutes
    interval_time = et_now.replace(minute=minute, second=0, microsecond=0)
    return int(interval_time.timestamp())


def generate_market_slug(asset: str, interval_minutes: int) -> str:
    """Generate market slug for current interval."""
    base_short = {"bitcoin": "btc", "ethereum": "eth", "solana": "sol", "xrp": "xrp"}.get(asset, asset)
    timestamp = get_interval_timestamp(interval_minutes)
    return f"{base_short}-updown-{interval_minutes}m-{timestamp}"


def fetch_market_by_slug(slug: str) -> dict | None:
    """Fetch market data by slug."""
    try:
        resp = requests.get(f"{GAMMA_HOST}/events", params={"slug": slug, "limit": 1}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data and len(data) > 0:
                return data[0]

        resp = requests.get(f"{GAMMA_HOST}/markets", params={"slug": slug, "limit": 1}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data and len(data) > 0:
                market = data[0]
                return {"title": market.get("question", ""), "markets": [market], "slug": slug}

        return None
    except Exception as e:
        print(f"  Error fetching market: {e}")
        return None


class OrderBookRecorder:
    """Records all order book events to CSV."""

    def __init__(self, asset: str, interval_minutes: int):
        self.asset = asset
        self.interval_minutes = interval_minutes
        self.label = f"{asset.upper()}-{interval_minutes}M"
        self.interval_seconds = interval_minutes * 60

        # Market state
        self.up_token = None
        self.down_token = None
        self.interval_end_unix = 0
        self.orderbooks = {}  # {token_id: {bids: [...], asks: [...]}}

        # Recording state
        self.csv_writer = None
        self.csv_file = None
        self.event_count = 0
        self.ws = None
        self.running = False

    def setup_market(self, slug: str) -> bool:
        """Fetch market and extract UP/DOWN tokens."""
        event_data = fetch_market_by_slug(slug)
        if not event_data:
            return False

        markets = event_data.get("markets", [])
        open_markets = [m for m in markets if not m.get("closed", False)]
        if not open_markets:
            return False

        market = open_markets[0]
        outcomes = json.loads(market.get("outcomes", "[]"))
        token_ids = json.loads(market.get("clobTokenIds", "[]"))

        if len(outcomes) < 2 or len(token_ids) < 2:
            return False

        outcome_0_lower = outcomes[0].lower()
        up_idx = 0 if outcome_0_lower in ["yes", "up"] else 1
        down_idx = 1 if up_idx == 0 else 0

        self.up_token = token_ids[up_idx]
        self.down_token = token_ids[down_idx]
        self.orderbooks = {}
        self.event_count = 0
        return True

    def open_csv(self, slug_timestamp: str):
        """Open a new CSV file for this interval."""
        self.close_csv()
        LOG_DIR.mkdir(exist_ok=True)
        base_short = {"bitcoin": "btc", "ethereum": "eth", "solana": "sol", "xrp": "xrp"}.get(self.asset, self.asset)
        filename = LOG_DIR / f"orderbook_{base_short}_{self.interval_minutes}m_{slug_timestamp}.csv"
        self.csv_file = open(filename, "w", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow([
            "timestamp", "event_type", "side", "book_side", "price", "size",
            "best_up_ask", "best_down_ask", "price_sum", "seconds_remaining",
        ])
        print(f"  Recording to: {filename.name}")

    def close_csv(self):
        """Close current CSV file."""
        if self.csv_file:
            self.csv_file.close()
            self.csv_file = None
            self.csv_writer = None

    def get_best_ask(self, token_id: str) -> float:
        """Get best (lowest) ask price for a token."""
        book = self.orderbooks.get(token_id, {})
        asks = book.get("asks", [])
        if asks:
            return float(asks[0]["price"])
        return 0.0

    def get_best_size(self, token_id: str) -> float:
        """Get size at best ask for a token."""
        book = self.orderbooks.get(token_id, {})
        asks = book.get("asks", [])
        if asks:
            return float(asks[0]["size"])
        return 0.0

    def write_row(self, event_type: str, token_id: str, book_side: str, price: str, size: str):
        """Write a single row to CSV."""
        if not self.csv_writer:
            return

        side = "UP" if token_id == self.up_token else "DOWN"
        best_up = self.get_best_ask(self.up_token)
        best_down = self.get_best_ask(self.down_token)
        price_sum = best_up + best_down if best_up > 0 and best_down > 0 else 0.0
        secs_remaining = max(0, self.interval_end_unix - int(time.time()))

        self.csv_writer.writerow([
            datetime.utcnow().isoformat(timespec="milliseconds"),
            event_type, side, book_side, price, size,
            f"{best_up:.4f}" if best_up else "",
            f"{best_down:.4f}" if best_down else "",
            f"{price_sum:.4f}" if price_sum else "",
            secs_remaining,
        ])
        self.event_count += 1

        # Flush every 50 events
        if self.event_count % 50 == 0 and self.csv_file:
            self.csv_file.flush()

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

    def on_message(self, ws, message):
        """Handle WebSocket messages."""
        if message == "PONG":
            return
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        # Initial book snapshot (list format)
        if isinstance(data, list):
            for event in data:
                if event.get("event_type") == "book" and event.get("asset_id"):
                    asset_id = event["asset_id"]
                    self.orderbooks[asset_id] = {
                        "bids": event.get("bids", []),
                        "asks": event.get("asks", []),
                    }
                    for bid in event.get("bids", []):
                        self.write_row("snapshot", asset_id, "bid", bid["price"], bid["size"])
                    for ask in event.get("asks", []):
                        self.write_row("snapshot", asset_id, "ask", ask["price"], ask["size"])

        # Price change updates (dict format)
        elif isinstance(data, dict):
            for change in data.get("price_changes", []):
                asset_id = change.get("asset_id")
                side = change.get("side")
                price = change.get("price")
                size = change.get("size")
                if asset_id and side and price is not None:
                    book_side = "bid" if side == "BUY" else "ask"
                    self.update_book_level(asset_id, "bids" if side == "BUY" else "asks", price, size)
                    self.write_row("update", asset_id, book_side, price, size)

    def on_open(self, ws):
        """Subscribe to market tokens."""
        subscribe_msg = {"assets_ids": [self.up_token, self.down_token], "type": "market"}
        ws.send(json.dumps(subscribe_msg))

        def ping_loop():
            while self.running:
                try:
                    ws.send("PING")
                    time.sleep(10)
                except:
                    break

        self.running = True
        threading.Thread(target=ping_loop, daemon=True).start()

        # Status refresh thread
        def status_loop():
            while self.running:
                self.print_status()
                time.sleep(2)

        threading.Thread(target=status_loop, daemon=True).start()

    def on_error(self, ws, error):
        pass

    def on_close(self, ws, code, msg):
        self.running = False

    def print_status(self):
        """Print live status line."""
        secs = max(0, self.interval_end_unix - int(time.time()))
        mins = secs // 60
        s = secs % 60
        up_ask = self.get_best_ask(self.up_token)
        down_ask = self.get_best_ask(self.down_token)
        up_size = self.get_best_size(self.up_token)
        down_size = self.get_best_size(self.down_token)
        price_sum = up_ask + down_ask if up_ask > 0 and down_ask > 0 else 0
        print(
            f"\r[{self.label}] {mins:02d}:{s:02d} | "
            f"UP: ${up_ask:.2f} ({int(up_size)}) | "
            f"DOWN: ${down_ask:.2f} ({int(down_size)}) | "
            f"Sum: ${price_sum:.2f} | "
            f"Events: {self.event_count}",
            end="", flush=True,
        )

    def run_interval(self, slug: str):
        """Record one interval. Returns when interval ends or WS disconnects."""
        slug_timestamp = slug.split("-")[-1]
        self.interval_end_unix = int(slug_timestamp) + self.interval_seconds

        print(f"\n[{self.label}] New interval: {slug}")
        if not self.setup_market(slug):
            print(f"  Market not found, retrying in 5s...")
            return

        self.open_csv(slug_timestamp)

        self.ws = WebSocketApp(
            f"{WS_URL}/ws/market",
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
            on_open=self.on_open,
        )

        # Run WS in background thread so we can check for interval changes
        ws_thread = threading.Thread(target=lambda: self.ws.run_forever(ping_interval=30, ping_timeout=10), daemon=True)
        ws_thread.start()

        # Wait for interval to end
        current_slug = slug
        while ws_thread.is_alive():
            new_slug = generate_market_slug(self.asset, self.interval_minutes)
            if new_slug != current_slug:
                print(f"\n[{self.label}] Interval ended — {self.event_count} events recorded")
                self.running = False
                self.ws.close()
                break
            time.sleep(1)

        self.close_csv()

    def run(self):
        """Run continuously, recording each interval."""
        print(f"{'='*60}")
        print(f"Order Book Recorder — {self.label}")
        print(f"{'='*60}")

        while True:
            try:
                slug = generate_market_slug(self.asset, self.interval_minutes)
                self.run_interval(slug)
                time.sleep(2)
            except KeyboardInterrupt:
                print(f"\n\nStopped. Total events this interval: {self.event_count}")
                self.close_csv()
                break
            except Exception as e:
                print(f"\nError: {e}")
                self.close_csv()
                time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="Record Polymarket order book data to CSV")
    parser.add_argument("--asset", default="bitcoin", choices=["bitcoin", "ethereum", "solana", "xrp"])
    parser.add_argument("--interval", type=int, default=5, choices=[5, 15])
    args = parser.parse_args()

    recorder = OrderBookRecorder(args.asset, args.interval)
    recorder.run()


if __name__ == "__main__":
    main()
