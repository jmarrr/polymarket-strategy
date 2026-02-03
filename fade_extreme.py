"""
Fade Extreme Backtester for Polymarket

Strategy: When one side (UP or DOWN) hits an extreme price (e.g., $0.93+)
with significant time remaining (e.g., >5 min), log an opportunity to fade it.
Track resolution and calculate simulated PnL.

NO LIVE TRADES — purely data collection to CSV for backtesting.
"""

import os
import csv
import json
import time
import threading
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from websocket import WebSocketApp

import requests
from dotenv import load_dotenv

load_dotenv()

# ── API Configuration ────────────────────────────────────────────────────────
GAMMA_HOST = "https://gamma-api.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com"

# ── Strategy Configuration ───────────────────────────────────────────────────
ASSET = "bitcoin"
EXTREME_THRESHOLD = 0.93       # Price at which we consider fading
MIN_MINUTES_REMAINING = 5      # Don't fade if <5 min left
SIMULATED_SIZE = 50            # $50 USDC per simulated trade
PROFIT_TARGET_PCT = 10         # % profit target to exit (e.g., 50% = sell at 1.5x entry)

# ── Globals ──────────────────────────────────────────────────────────────────
_print_lock = threading.Lock()
_status_line = ""

# CSV file path
LOG_DIR = Path("logs")
CSV_FILE = LOG_DIR / "fade_extreme.csv"


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _update_status(status: str):
    global _status_line
    with _print_lock:
        _status_line = status
        print(f"\r{status}\033[K", end="", flush=True)


def _print_event(msg: str):
    with _print_lock:
        print(f"\r\033[K{msg}", flush=True)
        if _status_line:
            print(f"\r{_status_line}\033[K", end="", flush=True)


def get_current_et_time():
    try:
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        from datetime import timezone
        utc_now = datetime.now(timezone.utc)
        return utc_now + timedelta(hours=-5)


def get_15m_interval_timestamp() -> int:
    et_now = get_current_et_time()
    minute = (et_now.minute // 15) * 15
    interval_time = et_now.replace(minute=minute, second=0, microsecond=0)
    return int(interval_time.timestamp())


def get_minutes_remaining() -> float:
    et_now = get_current_et_time()
    minute = (et_now.minute // 15) * 15
    interval_end = et_now.replace(minute=minute, second=0, microsecond=0) + timedelta(minutes=15)
    return max(0, (interval_end - et_now).total_seconds() / 60)


def generate_market_slug(base: str = "bitcoin") -> str:
    base_short = {"bitcoin": "btc", "ethereum": "eth", "solana": "sol", "xrp": "xrp"}.get(base, base)
    return f"{base_short}-updown-15m-{get_15m_interval_timestamp()}"


def fetch_market_by_slug(slug: str) -> dict | None:
    try:
        resp = requests.get(f"{GAMMA_HOST}/events", params={"slug": slug, "limit": 1}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data:
                return data[0]
        resp = requests.get(f"{GAMMA_HOST}/markets", params={"slug": slug, "limit": 1}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data:
                return {"title": data[0].get("question", ""), "markets": [data[0]], "slug": slug}
        return None
    except Exception as e:
        _print_event(f"Error fetching market: {e}")
        return None


def setup_csv():
    """Ensure CSV file exists with header."""
    LOG_DIR.mkdir(exist_ok=True)
    if not CSV_FILE.exists():
        with open(CSV_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "interval_slug", "minutes_remaining",
                "extreme_side", "extreme_price", "fade_side", "fade_buy_price",
                "target_sell_price", "exit_type", "exit_price", "resolution", "pnl"
            ])


def append_opportunity(row: dict):
    """Append a row to CSV."""
    with open(CSV_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            row.get("timestamp", ""),
            row.get("interval_slug", ""),
            row.get("minutes_remaining", ""),
            row.get("extreme_side", ""),
            row.get("extreme_price", ""),
            row.get("fade_side", ""),
            row.get("fade_buy_price", ""),
            row.get("target_sell_price", ""),
            row.get("exit_type", ""),       # "TARGET_HIT" or "RESOLUTION"
            row.get("exit_price", ""),      # actual exit price
            row.get("resolution", ""),
            row.get("pnl", ""),
        ])


# ═══════════════════════════════════════════════════════════════════════════════
# FadeExtremeMonitor
# ═══════════════════════════════════════════════════════════════════════════════

class FadeExtremeMonitor:
    """WebSocket-based monitor that logs fade-the-extreme opportunities."""

    def __init__(self, market_info: dict, current_slug: str):
        self.market_info = market_info
        self.current_slug = current_slug
        self.outcomes = json.loads(market_info.get("outcomes", "[]"))
        self.token_ids = json.loads(market_info.get("clobTokenIds", "[]"))

        outcome_0 = self.outcomes[0].lower() if self.outcomes else ""
        self.up_idx = 0 if outcome_0 in ("yes", "up") else 1
        self.down_idx = 1 if self.up_idx == 0 else 0

        self.up_token = self.token_ids[self.up_idx] if len(self.token_ids) > self.up_idx else None
        self.down_token = self.token_ids[self.down_idx] if len(self.token_ids) > self.down_idx else None

        # Order book state
        self.orderbooks: dict = {}
        self.up_price = 0.0
        self.up_size = 0.0
        self.down_price = 0.0
        self.down_size = 0.0

        # State
        self.ws = None
        self.running = False
        self.stopped = False
        self.warmed_up = False

        # Tracking for this interval
        self.logged_this_interval: set = set()  # ("UP",) or ("DOWN",) — dedupe
        self.pending_opportunities: list = []   # opportunities awaiting exit or resolution
        self.active_positions: list = []        # positions watching for profit target

        # Session stats
        self.total_logged = 0
        self.total_resolved = 0
        self.wins = 0
        self.target_hits = 0
        self.total_pnl = 0.0

    # ── WebSocket handlers ────────────────────────────────────────────────

    def on_open(self, ws):
        _print_event(f"[BTC] WebSocket connected")
        self.warmed_up = False
        subscribe_msg = {"assets_ids": [self.up_token, self.down_token], "type": "market"}
        ws.send(json.dumps(subscribe_msg))

        self.running = True

        def ping_loop():
            while self.running:
                try:
                    ws.send("PING")
                    time.sleep(10)
                except Exception:
                    break

        threading.Thread(target=ping_loop, daemon=True).start()

    def on_message(self, ws, message):
        if message == "PONG":
            return
        try:
            data = json.loads(message)
            self._process_message(data)
        except json.JSONDecodeError:
            pass

    def on_error(self, ws, error):
        _print_event(f"[BTC] WS error: {error}")

    def on_close(self, ws, code, msg):
        _print_event(f"[BTC] WS closed (code={code})")
        self.running = False

    # ── Message processing ────────────────────────────────────────────────

    def _process_message(self, data):
        if isinstance(data, dict):
            for change in data.get("price_changes", []):
                asset_id = change.get("asset_id")
                side = change.get("side")
                price = change.get("price")
                size = change.get("size")
                if asset_id and side and price is not None:
                    if asset_id not in self.orderbooks:
                        self.orderbooks[asset_id] = {"bids": [], "asks": []}
                    book_side = "bids" if side == "BUY" else "asks"
                    self._update_book_level(asset_id, book_side, price, size)
            if data.get("price_changes"):
                self.warmed_up = True
                self._check_extreme()
            return

        if not isinstance(data, list):
            return

        for event in data:
            etype = event.get("event_type")
            asset_id = event.get("asset_id")
            if etype == "book" and asset_id:
                self.orderbooks[asset_id] = {
                    "bids": event.get("bids", []),
                    "asks": event.get("asks", []),
                }
                self._check_extreme()
            elif etype == "price_change" and asset_id:
                if asset_id not in self.orderbooks:
                    self.orderbooks[asset_id] = {"bids": [], "asks": []}
                for change in event.get("changes", []):
                    side = change.get("side")
                    price = change.get("price")
                    size = change.get("size")
                    if side and price is not None:
                        book_side = "bids" if side == "BUY" else "asks"
                        self._update_book_level(asset_id, book_side, price, size)
                self.warmed_up = True
                self._check_extreme()

    def _update_book_level(self, asset_id: str, side: str, price: str, size: str):
        book = self.orderbooks.get(asset_id, {"bids": [], "asks": []})
        levels = [l for l in book.get(side, []) if l.get("price") != price]
        if float(size) > 0:
            levels.append({"price": price, "size": size})
        if side == "bids":
            levels.sort(key=lambda x: float(x["price"]), reverse=True)
        else:
            levels.sort(key=lambda x: float(x["price"]))
        book[side] = levels
        self.orderbooks[asset_id] = book

    # ── Extreme detection ─────────────────────────────────────────────────

    def _check_extreme(self):
        if not self.up_token or not self.down_token:
            return

        up_asks = self.orderbooks.get(self.up_token, {}).get("asks", [])
        down_asks = self.orderbooks.get(self.down_token, {}).get("asks", [])
        if not up_asks or not down_asks:
            return

        self.up_price = float(up_asks[0]["price"])
        self.up_size = float(up_asks[0]["size"])
        self.down_price = float(down_asks[0]["price"])
        self.down_size = float(down_asks[0]["size"])

        minutes_left = get_minutes_remaining()
        et_now = get_current_et_time()

        # Stale data check
        price_sum = self.up_price + self.down_price
        prices_valid = price_sum <= 1.15

        # Status line
        win_str = f"{self.wins}/{self.total_resolved}" if self.total_resolved > 0 else "0/0"
        active_str = f"Active: {len(self.active_positions)}"
        status = (
            f"[{et_now.strftime('%H:%M:%S')}] [BTC] "
            f"{minutes_left:.1f}min | "
            f"UP ${self.up_price:.2f} | DN ${self.down_price:.2f} | "
            f"Logged: {self.total_logged} | {active_str} | Wins: {win_str} | PnL: ${self.total_pnl:.2f}"
        )
        _update_status(status)

        if not self.warmed_up or not prices_valid:
            return

        # Check active positions for profit target hits
        self._check_profit_targets()

        # Check for extreme
        if minutes_left < MIN_MINUTES_REMAINING:
            return  # Too close to resolution

        # Check UP extreme
        if self.up_price >= EXTREME_THRESHOLD and "UP" not in self.logged_this_interval:
            self._log_opportunity("UP", self.up_price, "DOWN", self.down_price, minutes_left)
            self.logged_this_interval.add("UP")

        # Check DOWN extreme
        if self.down_price >= EXTREME_THRESHOLD and "DOWN" not in self.logged_this_interval:
            self._log_opportunity("DOWN", self.down_price, "UP", self.up_price, minutes_left)
            self.logged_this_interval.add("DOWN")

    def _check_profit_targets(self):
        """Check if any active positions hit their profit target."""
        still_active = []
        for pos in self.active_positions:
            fade_side = pos["fade_side"]
            target_price = pos["target_sell_price"]

            # Get current bid price for the fade side (what we'd sell at)
            if fade_side == "UP":
                token = self.up_token
                bids = self.orderbooks.get(token, {}).get("bids", [])
                current_bid = float(bids[0]["price"]) if bids else 0
            else:
                token = self.down_token
                bids = self.orderbooks.get(token, {}).get("bids", [])
                current_bid = float(bids[0]["price"]) if bids else 0

            if current_bid >= target_price:
                # Target hit! Calculate PnL and log
                buy_price = pos["fade_buy_price"]
                shares = SIMULATED_SIZE / buy_price
                pnl = shares * current_bid - SIMULATED_SIZE

                pos["row"]["exit_type"] = "TARGET_HIT"
                pos["row"]["exit_price"] = f"{current_bid:.2f}"
                pos["row"]["resolution"] = "N/A"
                pos["row"]["pnl"] = f"{pnl:.2f}"

                append_opportunity(pos["row"])

                self.total_resolved += 1
                self.wins += 1
                self.target_hits += 1
                self.total_pnl += pnl

                _print_event(
                    f"   TARGET HIT! {fade_side} reached ${current_bid:.2f} "
                    f"(target: ${target_price:.2f}) | PnL: ${pnl:.2f}"
                )
            else:
                still_active.append(pos)

        self.active_positions = still_active

    def _log_opportunity(self, extreme_side: str, extreme_price: float,
                         fade_side: str, fade_buy_price: float, minutes_left: float):
        et_now = get_current_et_time()
        timestamp = et_now.strftime("%Y-%m-%d %H:%M:%S")

        # Calculate target sell price (entry + profit target %)
        target_sell_price = fade_buy_price * (1 + PROFIT_TARGET_PCT / 100)

        row = {
            "timestamp": timestamp,
            "interval_slug": self.current_slug,
            "minutes_remaining": f"{minutes_left:.1f}",
            "extreme_side": extreme_side,
            "extreme_price": f"{extreme_price:.2f}",
            "fade_side": fade_side,
            "fade_buy_price": f"{fade_buy_price:.2f}",
            "target_sell_price": f"{target_sell_price:.2f}",
            "exit_type": "",       # "TARGET_HIT" or "RESOLUTION"
            "exit_price": "",      # actual exit price
            "resolution": "",      # which side won (if held to resolution)
            "pnl": "",
        }

        # Add to active positions for profit target tracking
        self.active_positions.append({
            "row": row,
            "fade_side": fade_side,
            "fade_buy_price": fade_buy_price,
            "target_sell_price": target_sell_price,
        })

        self.total_logged += 1

        _print_event(
            f"\n{'='*50}\n"
            f"FADE EXTREME DETECTED\n"
            f"{'='*50}\n"
            f"   Extreme: {extreme_side} @ ${extreme_price:.2f}\n"
            f"   Fade:    {fade_side} @ ${fade_buy_price:.2f}\n"
            f"   Target:  ${target_sell_price:.2f} ({PROFIT_TARGET_PCT}% profit)\n"
            f"   Time:    {minutes_left:.1f} min remaining\n"
            f"   Slug:    {self.current_slug}\n"
            f"{'='*50}"
        )

    def resolve_pending(self, winning_side: str):
        """Called when interval ends. Resolve active positions that didn't hit target."""
        for pos in self.active_positions:
            fade_side = pos["fade_side"]
            fade_buy_price = pos["fade_buy_price"]
            row = pos["row"]

            row["exit_type"] = "RESOLUTION"
            row["resolution"] = winning_side

            if fade_side == winning_side:
                # Win: shares * $1 - cost
                shares = SIMULATED_SIZE / fade_buy_price
                pnl = shares * 1.0 - SIMULATED_SIZE
                row["exit_price"] = "1.00"
                self.wins += 1
            else:
                # Lose: lost the cost
                pnl = -SIMULATED_SIZE
                row["exit_price"] = "0.00"

            row["pnl"] = f"{pnl:.2f}"
            self.total_pnl += pnl
            self.total_resolved += 1

            # Write to CSV
            append_opportunity(row)

            _print_event(
                f"   RESOLVED: {row['extreme_side']} extreme -> "
                f"Fade {fade_side} {'WON' if fade_side == winning_side else 'LOST'} "
                f"(PnL: ${pnl:.2f})"
            )

        self.active_positions.clear()

    def reset_for_new_interval(self, new_slug: str, market_info: dict):
        """Reset state for a new interval."""
        self.current_slug = new_slug
        self.market_info = market_info
        self.outcomes = json.loads(market_info.get("outcomes", "[]"))
        self.token_ids = json.loads(market_info.get("clobTokenIds", "[]"))

        outcome_0 = self.outcomes[0].lower() if self.outcomes else ""
        self.up_idx = 0 if outcome_0 in ("yes", "up") else 1
        self.down_idx = 1 if self.up_idx == 0 else 0

        self.up_token = self.token_ids[self.up_idx] if len(self.token_ids) > self.up_idx else None
        self.down_token = self.token_ids[self.down_idx] if len(self.token_ids) > self.down_idx else None

        self.orderbooks.clear()
        self.logged_this_interval.clear()
        self.active_positions.clear()
        self.warmed_up = False

    # ── Run / stop ────────────────────────────────────────────────────────

    def run(self):
        ws_url = f"{WS_URL}/ws/market"
        max_retries = 10
        retry_count = 0

        while not self.stopped:
            self.ws = WebSocketApp(
                ws_url,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
                on_open=self.on_open,
            )
            _print_event(f"[BTC] Connecting to WebSocket...")
            self.ws.run_forever(ping_interval=30, ping_timeout=10)

            if self.stopped:
                break
            retry_count += 1
            if retry_count > max_retries:
                _print_event(f"[BTC] Max reconnect attempts reached")
                break
            wait = min(2 ** retry_count, 30)
            _print_event(f"[BTC] Reconnecting in {wait}s ({retry_count}/{max_retries})...")
            time.sleep(wait)

    def stop(self):
        self.stopped = True
        self.running = False
        if self.ws:
            self.ws.close()

    def get_summary(self) -> dict:
        return {
            "total_logged": self.total_logged,
            "total_resolved": self.total_resolved,
            "wins": self.wins,
            "target_hits": self.target_hits,
            "total_pnl": self.total_pnl,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Main loop
# ═══════════════════════════════════════════════════════════════════════════════

def determine_resolution(slug: str) -> str | None:
    """Fetch market and determine which side won (resolved to $1)."""
    try:
        event_data = fetch_market_by_slug(slug)
        if not event_data:
            return None

        markets = event_data.get("markets", [])
        if not markets:
            return None

        market = markets[0]
        outcomes = json.loads(market.get("outcomes", "[]"))
        # Check outcomePrices or final prices
        # If market resolved, one outcome = $1, other = $0
        # For now, we'll try to infer from the question or final state
        # Simplest: check if closed and which outcome has price ~1.0

        # Try to get final prices from the market
        outcome_prices = market.get("outcomePrices")
        if outcome_prices:
            prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
            if len(prices) >= 2:
                # Find which is closer to 1.0
                outcome_0 = outcomes[0].lower() if outcomes else ""
                up_idx = 0 if outcome_0 in ("yes", "up") else 1
                down_idx = 1 if up_idx == 0 else 0

                up_price = float(prices[up_idx]) if prices[up_idx] else 0
                down_price = float(prices[down_idx]) if prices[down_idx] else 0

                if up_price > 0.9:
                    return "UP"
                elif down_price > 0.9:
                    return "DOWN"

        return None
    except Exception as e:
        _print_event(f"Error determining resolution: {e}")
        return None


def monitor_btc():
    """Main monitoring loop."""
    setup_csv()

    current_slug = None
    monitor = None
    ws_thread = None

    while True:
        try:
            slug = generate_market_slug(ASSET)
            minutes_left = get_minutes_remaining()

            if slug != current_slug:
                # ── Interval transition ───────────────────────────────
                if monitor and current_slug:
                    # Resolve pending opportunities from previous interval
                    _print_event(f"\n[BTC] Interval ended: {current_slug}")

                    # Wait a bit for market to settle/resolve
                    time.sleep(3)

                    # Determine winner
                    winner = determine_resolution(current_slug)
                    if winner:
                        _print_event(f"[BTC] Resolution: {winner} won")
                        monitor.resolve_pending(winner)
                    else:
                        _print_event(f"[BTC] Could not determine resolution (market may not be settled yet)")
                        # Still resolve as unknown — mark as N/A
                        for pos in monitor.active_positions:
                            pos["row"]["exit_type"] = "RESOLUTION"
                            pos["row"]["resolution"] = "UNKNOWN"
                            pos["row"]["exit_price"] = "N/A"
                            pos["row"]["pnl"] = "0"
                            append_opportunity(pos["row"])
                        monitor.active_positions.clear()

                    monitor.stop()
                    time.sleep(1)

                current_slug = slug
                _print_event(f"\n[BTC] New interval: {slug} | {minutes_left:.1f}min remaining")

                event_data = fetch_market_by_slug(slug)
                if not event_data:
                    _print_event(f"[BTC] Waiting for market {slug}...")
                    time.sleep(5)
                    current_slug = None
                    continue

                markets = event_data.get("markets", [])
                open_markets = [m for m in markets if not m.get("closed", False)]
                if not open_markets:
                    _print_event(f"[BTC] Market closed, waiting for next...")
                    time.sleep(5)
                    current_slug = None
                    continue

                market = open_markets[0]
                _print_event(f"[BTC] Found: {market.get('question', '')[:60]}...")

                monitor = FadeExtremeMonitor(market, current_slug)

                # Start WebSocket thread
                ws_thread = threading.Thread(target=monitor.run, daemon=True)
                ws_thread.start()

                # Wait until interval ends
                while ws_thread.is_alive():
                    new_slug = generate_market_slug(ASSET)
                    if new_slug != current_slug:
                        break
                    time.sleep(1)
            else:
                time.sleep(5)

        except Exception as e:
            _print_event(f"[BTC] Error: {e}")
            time.sleep(5)


def main():
    print(f"\n{'='*60}")
    print(f"FADE EXTREME BACKTESTER")
    print(f"{'='*60}")
    print(f"   Asset:            {ASSET.upper()}")
    print(f"   Extreme threshold:${EXTREME_THRESHOLD:.2f}")
    print(f"   Min time left:    {MIN_MINUTES_REMAINING} min")
    print(f"   Profit target:    {PROFIT_TARGET_PCT}%")
    print(f"   Simulated size:   ${SIMULATED_SIZE}")
    print(f"   CSV output:       {CSV_FILE}")
    print(f"\n   Exit strategy: Sell at {PROFIT_TARGET_PCT}% profit OR hold to resolution")
    print(f"\n   NO LIVE TRADES — test mode only")
    print(f"\n   Press Ctrl+C to stop")
    print(f"{'='*60}\n")

    try:
        monitor_btc()
    except KeyboardInterrupt:
        pass

    # Exit summary
    print(f"\n\n{'='*60}")
    print(f"FADE EXTREME BACKTESTER STOPPED")
    print(f"{'='*60}")
    print(f"   Check {CSV_FILE} for logged opportunities")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
