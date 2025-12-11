import os
import json
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from dotenv import load_dotenv
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY, SELL

load_dotenv()

CLOB_HOST = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")
GAMMA_HOST = "https://gamma-api.polymarket.com"
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
FUNDER = os.getenv("FUNDER_ADDRESS")

# Configuration
REFRESH_INTERVAL = 0.3  # seconds between checks (300ms - safe within rate limits)
MARKET_BASE = "bitcoin"  # bitcoin, ethereum, or solana
TIMEFRAME = "15m"  # "hourly" or "15m"

# Trading Configuration
EXECUTE_TRADES = True  # Set to True to enable actual trading
MAX_POSITION_SIZE = 60  # Maximum position size in USDC per side
MIN_EDGE = 0.01  # Minimum edge (1%) required to execute

# Global trading client (initialized once for speed)
_trading_client = None


def get_current_et_time():
    """Get current time in Eastern Time."""
    try:
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # Fallback: manually calculate ET (UTC-5 for EST, UTC-4 for EDT)
        from datetime import timezone, timedelta
        utc_now = datetime.now(timezone.utc)
        et_offset = timedelta(hours=-5)  # EST
        return utc_now + et_offset


def format_hour_ampm(hour: int) -> str:
    """Convert 24-hour to 12-hour format with am/pm."""
    if hour == 0:
        return "12am"
    elif hour < 12:
        return f"{hour}am"
    elif hour == 12:
        return "12pm"
    else:
        return f"{hour - 12}pm"


def get_15m_interval_timestamp() -> int:
    """Get Unix timestamp for the current 15-minute interval."""
    et_now = get_current_et_time()
    # Round down to nearest 15 minutes
    minute = (et_now.minute // 15) * 15
    interval_time = et_now.replace(minute=minute, second=0, microsecond=0)
    return int(interval_time.timestamp())


def get_current_interval_end_time(timeframe: str = "15m"):
    """Get the end time of the current interval."""
    from datetime import timedelta
    et_now = get_current_et_time()
    
    if timeframe == "15m":
        # Round down to current 15-min interval start
        minute = (et_now.minute // 15) * 15
        interval_start = et_now.replace(minute=minute, second=0, microsecond=0)
        # End is 15 minutes later
        interval_end = interval_start + timedelta(minutes=15)
    else:
        # Hourly - round down to current hour
        interval_start = et_now.replace(minute=0, second=0, microsecond=0)
        interval_end = interval_start + timedelta(hours=1)
    
    return interval_end


def should_switch_to_next_market(timeframe: str = "15m") -> bool:
    """Check if we should switch to the next market based on current time."""
    et_now = get_current_et_time()
    interval_end = get_current_interval_end_time(timeframe)
    
    # Switch when we're past the interval end time
    return et_now >= interval_end


def generate_market_slug(base: str = "bitcoin", timeframe: str = "hourly") -> str:
    """
    Generate market slug based on current ET time.
    
    Hourly format: {base}-up-or-down-{month}-{day}-{hour}{am/pm}-et
    Example: bitcoin-up-or-down-december-10-1pm-et
    
    15m format: btc-updown-15m-{unix_timestamp}
    Example: btc-updown-15m-1765433700
    """
    et_now = get_current_et_time()
    
    if timeframe == "15m":
        # Map base to short form
        base_short = {"bitcoin": "btc", "ethereum": "eth", "solana": "sol"}.get(base, base)
        timestamp = get_15m_interval_timestamp()
        slug = f"{base_short}-updown-15m-{timestamp}"
    else:
        # Hourly format
        month = et_now.strftime("%B").lower()
        day = et_now.day
        hour_str = format_hour_ampm(et_now.hour)
        slug = f"{base}-up-or-down-{month}-{day}-{hour_str}-et"
    
    return slug


def generate_next_slug(base: str = "bitcoin", timeframe: str = "hourly") -> str:
    """
    Generate market slug for the next time period based on current ET time.
    For hourly: next hour
    For 15m: next 15-minute interval
    """
    from datetime import timedelta
    et_now = get_current_et_time()
    
    if timeframe == "15m":
        # Round to current 15-min interval, then add 15 minutes
        current_minute = (et_now.minute // 15) * 15
        current_interval = et_now.replace(minute=current_minute, second=0, microsecond=0)
        next_time = current_interval + timedelta(minutes=15)
        
        # Map base to short form
        base_short = {"bitcoin": "btc", "ethereum": "eth", "solana": "sol"}.get(base, base)
        timestamp = int(next_time.timestamp())
        slug = f"{base_short}-updown-15m-{timestamp}"
    else:
        # Add 1 hour
        next_time = et_now + timedelta(hours=1)
        month = next_time.strftime("%B").lower()
        day = next_time.day
        hour_str = format_hour_ampm(next_time.hour)
        slug = f"{base}-up-or-down-{month}-{day}-{hour_str}-et"
    
    return slug


def get_client():
    """Initialize and return the CLOB client."""
    client = ClobClient(CLOB_HOST, key=PRIVATE_KEY, chain_id=POLYGON, signature_type=2, funder=FUNDER)
    if PRIVATE_KEY and FUNDER:
        client.set_api_creds(client.create_or_derive_api_creds())
    return client


def get_trading_client():
    """Get or create the global trading client (cached for speed)."""
    global _trading_client
    if _trading_client is None:
        if not PRIVATE_KEY or not FUNDER:
            raise ValueError("PRIVATE_KEY and FUNDER_ADDRESS required for trading")
        _trading_client = ClobClient(CLOB_HOST, key=PRIVATE_KEY, chain_id=POLYGON, signature_type=2, funder=FUNDER)
        _trading_client.set_api_creds(_trading_client.create_or_derive_api_creds())
    return _trading_client


def fetch_market_by_slug(slug: str) -> dict | None:
    """
    Fetch a specific market/event by slug from Polymarket Gamma API.
    """
    # Try fetching as an event first
    resp = requests.get(
        f"{GAMMA_HOST}/events",
        params={"slug": slug, "limit": 1},
        timeout=10,
    )
    
    if resp.status_code == 200:
        data = resp.json()
        if data and len(data) > 0:
            return data[0]
    
    # Try fetching as a market
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


def check_market_status(slug: str) -> tuple[bool, dict | None]:
    """
    Check if market is still open.
    
    Returns:
        (is_open, market_data)
    """
    event_data = fetch_market_by_slug(slug)
    if not event_data:
        return False, None
    
    markets = event_data.get("markets", [])
    if not markets:
        return False, None
    
    # Check if any market is still open
    for market in markets:
        if not market.get('closed', False):
            return True, event_data
    
    return False, event_data


def analyze_orderbook_quick(client, market: dict) -> dict | None:
    """
    Quick order book analysis - returns data without verbose output.
    """
    try:
        clob_token_ids = json.loads(market.get("clobTokenIds", "[]"))
        outcomes = json.loads(market.get("outcomes", "[]"))
        
        if len(clob_token_ids) != 2 or len(outcomes) != 2:
            return None
        
        # Find which index is the positive outcome (Yes/Up) and negative (No/Down)
        outcome_0_lower = outcomes[0].lower()
        positive_idx = 0 if outcome_0_lower in ["yes", "up"] else 1
        negative_idx = 1 if positive_idx == 0 else 0
        
        yes_token_id = clob_token_ids[positive_idx]
        no_token_id = clob_token_ids[negative_idx]
        
        # Get order books
        yes_orderbook = client.get_order_book(yes_token_id)
        no_orderbook = client.get_order_book(no_token_id)
        
        # Extract order book data
        yes_bids_raw = yes_orderbook.bids if hasattr(yes_orderbook, 'bids') else []
        yes_asks_raw = yes_orderbook.asks if hasattr(yes_orderbook, 'asks') else []
        no_bids_raw = no_orderbook.bids if hasattr(no_orderbook, 'bids') else []
        no_asks_raw = no_orderbook.asks if hasattr(no_orderbook, 'asks') else []
        
        if not (yes_asks_raw and no_asks_raw and yes_bids_raw and no_bids_raw):
            return None
        
        # Sort order books properly
        yes_asks = sorted(yes_asks_raw, key=lambda x: float(x.price))
        yes_bids = sorted(yes_bids_raw, key=lambda x: float(x.price), reverse=True)
        no_asks = sorted(no_asks_raw, key=lambda x: float(x.price))
        no_bids = sorted(no_bids_raw, key=lambda x: float(x.price), reverse=True)
        
        # Extract best prices
        yes_ask = float(yes_asks[0].price)  # Lowest ask
        no_ask = float(no_asks[0].price)    # Lowest ask
        yes_bid = float(yes_bids[0].price)  # Highest bid
        no_bid = float(no_bids[0].price)    # Highest bid
        
        # Get sizes
        yes_ask_size = float(yes_asks[0].size)
        no_ask_size = float(no_asks[0].size)
        yes_bid_size = float(yes_bids[0].size)
        no_bid_size = float(no_bids[0].size)
        
        # Calculate buy cost (only buy arbitrage supported)
        buy_both_cost = yes_ask + no_ask
        buy_edge = 1.0 - buy_both_cost
        has_arbitrage = buy_both_cost < 1.0
        
        return {
            "market_slug": market.get('slug', 'unknown'),
            "question": market.get('question', ''),
            "positive_outcome": outcomes[positive_idx],
            "negative_outcome": outcomes[negative_idx],
            "yes_token_id": yes_token_id,
            "no_token_id": no_token_id,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "yes_bid_size": yes_bid_size,
            "yes_ask_size": yes_ask_size,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "no_bid_size": no_bid_size,
            "no_ask_size": no_ask_size,
            "buy_both_cost": buy_both_cost,
            "has_arbitrage": has_arbitrage,
            "buy_edge": buy_edge,
        }
        
    except Exception as e:
        return None


def execute_arbitrage(client, result: dict, max_size: float = MAX_POSITION_SIZE) -> bool:
    """
    Execute arbitrage trade by buying both YES and NO tokens.
    Uses parallel execution for speed.
    
    Args:
        client: CLOB client (unused, uses cached trading client)
        result: Arbitrage opportunity result from analyze_orderbook_quick
        max_size: Maximum position size in USDC
    
    Returns:
        True if successful, False otherwise
    """
    if not EXECUTE_TRADES:
        print("   ⚠️ Trading disabled")
        return False
    
    if not PRIVATE_KEY or not FUNDER:
        print("   ❌ Missing credentials")
        return False
    
    if not result.get('has_arbitrage'):
        return False
    
    try:
        # Get cached trading client for speed
        trading_client = get_trading_client()
        
        pos = result.get('positive_outcome', 'UP')
        neg = result.get('negative_outcome', 'DOWN')
        
        # Prices: max 2 decimals
        yes_price = round(result['yes_ask'], 2)
        no_price = round(result['no_ask'], 2)
        
        # Polymarket minimum order size is $1 USDC per side
        MIN_ORDER_VALUE = 1.0
        
        # Calculate minimum shares needed to meet $1 minimum for each side
        min_shares_for_yes = int(MIN_ORDER_VALUE / yes_price) + 1 if yes_price > 0 else 1
        min_shares_for_no = int(MIN_ORDER_VALUE / no_price) + 1 if no_price > 0 else 1
        min_shares_required = max(min_shares_for_yes, min_shares_for_no)
        
        # Calculate position size based on liquidity and max budget
        available_size = min(result['yes_ask_size'], result['no_ask_size'])
        total_cost = yes_price + no_price
        max_affordable = int(max_size / total_cost)
        
        position_size = min(max_affordable, int(available_size))
        
        # Check if we can meet minimum order requirements
        if position_size < min_shares_required:
            print(f"   ⚠️ Size {position_size} below minimum {min_shares_required} (need ${MIN_ORDER_VALUE} per side)")
            return False
        
        if position_size < 1:
            print(f"   ⚠️ Size too small")
            return False
        
        if result['buy_edge'] < MIN_EDGE:
            print(f"   ⚠️ Edge too small: {result['buy_edge']:.4f}")
            return False
        
        # Pre-create both orders for speed
        yes_order = OrderArgs(price=yes_price, size=position_size, side=BUY, token_id=result['yes_token_id'])
        no_order = OrderArgs(price=no_price, size=position_size, side=BUY, token_id=result['no_token_id'])
        
        # Sign both orders
        signed_yes = trading_client.create_order(yes_order)
        signed_no = trading_client.create_order(no_order)
        
        print(f"\n   ⚡ FAST EXECUTION: {position_size} shares @ ${total_cost:.2f}")
        
        # Track filled orders for potential rollback
        filled_orders = {}  # name -> (token_id, bid_price)
        order_info = {
            pos: (result['yes_token_id'], result['yes_bid']),
            neg: (result['no_token_id'], result['no_bid'])
        }
        
        # Execute both orders in parallel using ThreadPoolExecutor
        def post_order(signed_order, name):
            return name, trading_client.post_order(signed_order, OrderType.FOK)
        
        start_time = time.time()
        
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(post_order, signed_yes, pos),
                executor.submit(post_order, signed_no, neg)
            ]
            
            results = {}
            errors = []
            
            for future in as_completed(futures):
                try:
                    name, resp = future.result()
                    results[name] = resp
                    filled_orders[name] = order_info[name]
                    print(f"   ✅ {name} filled")
                except Exception as e:
                    errors.append(str(e))
                    print(f"   ❌ Order failed: {e}")
        
        exec_time = (time.time() - start_time) * 1000
        
        if len(results) == 2 and not errors:
            profit = position_size * result['buy_edge']
            print(f"\n   🎉 SUCCESS in {exec_time:.0f}ms | Profit: ${profit:.2f}")
            return True
        
        # PARTIAL FILL - Close filled positions to avoid directional risk
        if len(filled_orders) == 1:
            filled_name = list(filled_orders.keys())[0]
            token_id, bid_price = filled_orders[filled_name]
            
            print(f"\n   ⚠️ PARTIAL FILL - Emergency closing {filled_name}...")
            
            # Wait for tokens to settle (sometimes there's a brief delay)
            print(f"   ⏳ Waiting for tokens to settle...")
            time.sleep(2)
            
            # Retry loop for closing position
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    # Market sell = set very low price to guarantee immediate fill
                    sell_price = 0.01  # Minimum price = aggressive market sell
                    
                    sell_order = OrderArgs(
                        price=sell_price,
                        size=position_size,
                        side=SELL,
                        token_id=token_id
                    )
                    signed_sell = trading_client.create_order(sell_order)
                    resp_sell = trading_client.post_order(signed_sell, OrderType.FOK)
                    print(f"   🔄 {filled_name} MARKET SELL executed")
                    print(f"   ✅ Position closed")
                    break
                except Exception as sell_error:
                    error_msg = str(sell_error)
                    
                    if "balance" in error_msg.lower() or "allowance" in error_msg.lower():
                        if attempt < max_retries - 1:
                            print(f"   ⏳ Tokens not ready, retry {attempt + 2}/{max_retries}...")
                            time.sleep(2)
                            continue
                    
                    # Try GTC as backup on last attempt
                    if attempt == max_retries - 1:
                        print(f"   ⚠️ FOK failed, trying GTC at bid...")
                        try:
                            sell_price = round(bid_price, 2)
                            sell_order = OrderArgs(
                                price=sell_price,
                                size=position_size,
                                side=SELL,
                                token_id=token_id
                            )
                            signed_sell = trading_client.create_order(sell_order)
                            resp_sell = trading_client.post_order(signed_sell, OrderType.GTC)
                            print(f"   🔄 {filled_name} SELL order placed @ ${sell_price:.2f}")
                        except Exception as e2:
                            print(f"   ❌ Failed to close: {e2}")
                            print(f"   ⚠️ MANUAL ACTION: Sell {position_size} {filled_name}!")
            
            return False
        
        return False
            
    except Exception as e:
        print(f"   ❌ Error: {e}")
        return False


def analyze_orderbook_arbitrage(client, market: dict, verbose: bool = True) -> dict | None:
    """
    Analyze a market's order book for arbitrage opportunities.
    """
    try:
        clob_token_ids = json.loads(market.get("clobTokenIds", "[]"))
        outcomes = json.loads(market.get("outcomes", "[]"))
        
        if len(clob_token_ids) != 2 or len(outcomes) != 2:
            if verbose:
                print(f"   ⚠️  Not a binary market (outcomes: {outcomes})")
            return None
        
        # Map token IDs to outcomes correctly
        # For Yes/No markets: Yes = positive, No = negative
        # For Up/Down markets: Up = positive, Down = negative
        outcome_0_lower = outcomes[0].lower()
        positive_idx = 0 if outcome_0_lower in ["yes", "up"] else 1
        negative_idx = 1 if positive_idx == 0 else 0
        
        yes_token_id = clob_token_ids[positive_idx]
        no_token_id = clob_token_ids[negative_idx]
        
        if verbose:
            print(f"\n📊 Fetching order books...")
            print(f"   Outcomes: {outcomes}")
            print(f"   Positive ({outcomes[positive_idx]}): {yes_token_id}")
            print(f"   Negative ({outcomes[negative_idx]}): {no_token_id}")
        
        # Get order books
        yes_orderbook = client.get_order_book(yes_token_id)
        no_orderbook = client.get_order_book(no_token_id)
        
        # Extract order book data
        yes_bids_raw = yes_orderbook.bids if hasattr(yes_orderbook, 'bids') else []
        yes_asks_raw = yes_orderbook.asks if hasattr(yes_orderbook, 'asks') else []
        no_bids_raw = no_orderbook.bids if hasattr(no_orderbook, 'bids') else []
        no_asks_raw = no_orderbook.asks if hasattr(no_orderbook, 'asks') else []
        
        if not (yes_asks_raw and no_asks_raw):
            if verbose:
                print("\n⚠️  No asks available - cannot calculate buy arbitrage")
            return None
        
        if not (yes_bids_raw and no_bids_raw):
            if verbose:
                print("\n⚠️  No bids available - cannot calculate sell arbitrage")
            return None
        
        # Sort order books properly
        # Asks: ascending (lowest = best ask)
        # Bids: descending (highest = best bid)
        yes_asks = sorted(yes_asks_raw, key=lambda x: float(x.price))
        yes_bids = sorted(yes_bids_raw, key=lambda x: float(x.price), reverse=True)
        no_asks = sorted(no_asks_raw, key=lambda x: float(x.price))
        no_bids = sorted(no_bids_raw, key=lambda x: float(x.price), reverse=True)
        
        if verbose:
            print(f"\n📈 YES Order Book ({len(yes_bids)} bids, {len(yes_asks)} asks):")
            print(f"   Best Bid: ${yes_bids[0].price} x {yes_bids[0].size}")
            print(f"   Best Ask: ${yes_asks[0].price} x {yes_asks[0].size}")
            
            print(f"\n📉 NO Order Book ({len(no_bids)} bids, {len(no_asks)} asks):")
            print(f"   Best Bid: ${no_bids[0].price} x {no_bids[0].size}")
            print(f"   Best Ask: ${no_asks[0].price} x {no_asks[0].size}")
        
        # Extract best prices
        yes_ask = float(yes_asks[0].price)  # Lowest ask
        no_ask = float(no_asks[0].price)    # Lowest ask
        yes_bid = float(yes_bids[0].price)  # Highest bid
        no_bid = float(no_bids[0].price)    # Highest bid
        
        # Get sizes
        yes_ask_size = float(yes_asks[0].size)
        no_ask_size = float(no_asks[0].size)
        yes_bid_size = float(yes_bids[0].size)
        no_bid_size = float(no_bids[0].size)
        
        # Calculate buy cost (only buy arbitrage supported on Polymarket)
        buy_both_cost = yes_ask + no_ask
        buy_edge = 1.0 - buy_both_cost
        has_arbitrage = buy_both_cost < 1.0
        
        # Midpoint prices
        yes_mid = (yes_bid + yes_ask) / 2
        no_mid = (no_bid + no_ask) / 2
        
        if verbose:
            print(f"\n" + "="*60)
            print(f"💰 PRICE ANALYSIS")
            print(f"="*60)
            print(f"\n   YES:")
            print(f"      Best Bid:  ${yes_bid:.4f} (size: {yes_bid_size:.2f})")
            print(f"      Best Ask:  ${yes_ask:.4f} (size: {yes_ask_size:.2f})")
            print(f"      Midpoint:  ${yes_mid:.4f}")
            print(f"      Spread:    ${yes_ask - yes_bid:.4f} ({((yes_ask - yes_bid) / yes_mid * 100):.2f}%)")
            
            print(f"\n   NO:")
            print(f"      Best Bid:  ${no_bid:.4f} (size: {no_bid_size:.2f})")
            print(f"      Best Ask:  ${no_ask:.4f} (size: {no_ask_size:.2f})")
            print(f"      Midpoint:  ${no_mid:.4f}")
            print(f"      Spread:    ${no_ask - no_bid:.4f} ({((no_ask - no_bid) / no_mid * 100):.2f}%)")
            
            print(f"\n" + "="*60)
            print(f"🎯 ARBITRAGE ANALYSIS")
            print(f"="*60)
            print(f"\n   Buy Both (YES + NO at ask prices):")
            print(f"      Total Cost:  ${buy_both_cost:.4f}")
            print(f"      Guaranteed:  $1.00 (on resolution)")
            print(f"      Edge:        ${buy_edge:.4f} ({buy_edge * 100:.2f}%)")
            if has_arbitrage:
                print(f"      ✅ ARBITRAGE EXISTS! Buy both sides for guaranteed profit")
                max_size = min(yes_ask_size, no_ask_size)
                potential_profit = buy_edge * max_size
                print(f"      Max Size: {max_size:.2f} shares")
                print(f"      Potential Profit: ${potential_profit:.2f}")
            else:
                print(f"      ❌ No arbitrage (cost > $1.00)")
            
            mid_sum = yes_mid + no_mid
            print(f"\n   Midpoint Sum: ${mid_sum:.4f}")
            if abs(mid_sum - 1.0) > 0.01:
                print(f"      ⚠️  Market appears mispriced (sum should be ~$1.00)")
            else:
                print(f"      ✅ Market properly priced")
        
        result = {
            "market_slug": market.get('slug', 'unknown'),
            "question": market.get('question', ''),
            "yes_token_id": yes_token_id,
            "no_token_id": no_token_id,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "yes_bid_size": yes_bid_size,
            "yes_ask_size": yes_ask_size,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "no_bid_size": no_bid_size,
            "no_ask_size": no_ask_size,
            "buy_both_cost": buy_both_cost,
            "has_arbitrage": has_arbitrage,
            "buy_edge": buy_edge,
        }
        
        return result
        
    except Exception as e:
        if verbose:
            print(f"❌ Error analyzing order book: {e}")
        return None


def monitor_market_continuous(slug: str, interval: int = REFRESH_INTERVAL, auto_update: bool = True, base: str = MARKET_BASE, timeframe: str = TIMEFRAME):
    """
    Continuously monitor a market for arbitrage until it closes.
    Auto-updates to next market when current one closes.
    
    Args:
        slug: The market/event slug to monitor
        interval: Seconds between each check
        auto_update: If True, automatically switch to next market when closed
        base: Base asset for slug generation (bitcoin, ethereum, solana)
        timeframe: Market timeframe ("hourly" or "15m")
    """
    current_slug = slug
    total_arbitrage_found = 0
    
    while True:  # Outer loop for auto-updating slug
        # Get interval end time for display
        interval_end = get_current_interval_end_time(timeframe)
        
        print(f"\n{'='*60}")
        print(f"🔄 CONTINUOUS ARBITRAGE MONITOR")
        print(f"{'='*60}")
        print(f"\n📌 Market: {current_slug}")
        print(f"⏱️  Refresh: {interval}s | Ends: {interval_end.strftime('%H:%M:%S ET')}")
        print(f"🔄 Auto-update: {auto_update}")
        print(f"🛑 Press Ctrl+C to stop")
        print(f"{'─'*60}")
        
        # Initial setup
        print(f"\n📡 Fetching market data...")
        event_data = fetch_market_by_slug(current_slug)
        
        if not event_data:
            print(f"❌ Could not find market with slug: {current_slug}")
            if auto_update:
                # Try next hour's slug
                next_slug = generate_next_slug(base, timeframe)
                print(f"🔄 Trying next hour's market: {next_slug}")
                current_slug = next_slug
                time.sleep(5)
                continue
            else:
                return
        
        print(f"✅ Found event: {event_data.get('title', 'Unknown')}")
        
        # Initialize client once
        print(f"\n🔧 Initializing CLOB client...")
        try:
            client = get_client()
            print(f"✅ Client initialized")
        except Exception as e:
            print(f"❌ Failed to initialize client: {e}")
            return
        
        markets = event_data.get("markets", [])
        if not markets:
            print(f"❌ No markets found in event")
            if auto_update:
                next_slug = generate_next_slug(base, timeframe)
                print(f"🔄 Trying next hour's market: {next_slug}")
                current_slug = next_slug
                time.sleep(5)
                continue
            else:
                return
        
        # Filter to open markets
        open_markets = [m for m in markets if not m.get('closed', False)]
        if not open_markets:
            print(f"❌ All markets are closed")
            if auto_update:
                next_slug = generate_next_slug(base, timeframe)
                print(f"🔄 Switching to next hour's market: {next_slug}")
                current_slug = next_slug
                time.sleep(5)
                continue
            else:
                return
        
        print(f"\n📊 Monitoring {len(open_markets)} open market(s)")
        print(f"\n{'='*60}")
        print(f"🚀 STARTING CONTINUOUS MONITORING...")
        print(f"{'='*60}\n")
        
        iteration = 0
        arbitrage_found_count = 0
        
        try:
            while True:  # Inner loop for monitoring current market
                iteration += 1
                et_now = get_current_et_time()
                now = et_now.strftime("%H:%M:%S ET")
                
                # Proactively check if we should switch to next market based on time
                if auto_update and should_switch_to_next_market(timeframe):
                    print(f"\n{'='*60}")
                    print(f"⏰ INTERVAL ENDED at {now}")
                    print(f"{'='*60}")
                    print(f"   Total iterations: {iteration}")
                    print(f"   Arbitrage opportunities found: {arbitrage_found_count}")
                    total_arbitrage_found += arbitrage_found_count
                    
                    next_slug = generate_market_slug(base, timeframe)  # Get current interval's slug
                    print(f"\n🔄 Switching to new market: {next_slug}")
                    current_slug = next_slug
                    time.sleep(2)  # Brief pause before switching
                    break  # Break inner loop to restart with new slug
                
                # Also check if API reports market as closed (backup check)
                is_open, updated_event = check_market_status(current_slug)
                if not is_open:
                    print(f"\n{'='*60}")
                    print(f"🏁 MARKET CLOSED at {now}")
                    print(f"{'='*60}")
                    print(f"   Total iterations: {iteration}")
                    print(f"   Arbitrage opportunities found: {arbitrage_found_count}")
                    total_arbitrage_found += arbitrage_found_count
                    
                    if auto_update:
                        next_slug = generate_market_slug(base, timeframe)
                        print(f"\n🔄 Auto-updating to next market: {next_slug}")
                        current_slug = next_slug
                        time.sleep(2)
                        break  # Break inner loop to restart with new slug
                    else:
                        return
                
                # Update markets list
                if updated_event:
                    markets = updated_event.get("markets", [])
                    open_markets = [m for m in markets if not m.get('closed', False)]
                
                if not open_markets:
                    print(f"\n🏁 All markets closed at {now}")
                    total_arbitrage_found += arbitrage_found_count
                    
                    if auto_update:
                        next_slug = generate_market_slug(base, timeframe)
                        print(f"\n🔄 Auto-updating to next market: {next_slug}")
                        current_slug = next_slug
                        time.sleep(2)
                        break
                    else:
                        return
                
                # Compact header for each iteration
                print(f"[{now}] Scan #{iteration}", end="")
                
                found_arb = False
                for market in open_markets:
                    result = analyze_orderbook_quick(client, market)
                    
                    if result:
                        buy_cost = result['buy_both_cost']
                        buy_edge = result['buy_edge']
                        
                        # Check for arbitrage
                        if result['has_arbitrage']:
                            found_arb = True
                            arbitrage_found_count += 1
                            
                            pos = result.get('positive_outcome', 'YES')
                            neg = result.get('negative_outcome', 'NO')
                            max_size = min(result['yes_ask_size'], result['no_ask_size'])
                            profit = buy_edge * max_size
                            
                            print(f"\n{'🚨'*20}")
                            print(f"🎯 ARBITRAGE FOUND at {now}!")
                            print(f"{'🚨'*20}")
                            print(f"\n   Market: {result['question'][:70]}...")
                            print(f"\n   {pos}: bid=${result['yes_bid']:.4f} ask=${result['yes_ask']:.4f}")
                            print(f"   {neg}:  bid=${result['no_bid']:.4f} ask=${result['no_ask']:.4f}")
                            print(f"\n   💰 BUY BOTH: Cost ${buy_cost:.4f} → Edge ${buy_edge:.4f} ({buy_edge*100:.2f}%)")
                            print(f"      Max size: {max_size:.2f} | Potential profit: ${profit:.2f}")
                            
                            # Execute the arbitrage trade
                            if EXECUTE_TRADES:
                                execute_arbitrage(client, result)
                            
                            print(f"\n{'─'*60}")
                        else:
                            # No arbitrage - show order book and costs
                            pos = result.get('positive_outcome', 'YES')
                            neg = result.get('negative_outcome', 'NO')
                            print(f"\n   {pos}: bid=${result['yes_bid']:.4f} ask=${result['yes_ask']:.4f} | {neg}: bid=${result['no_bid']:.4f} ask=${result['no_ask']:.4f}")
                            print(f"   Buy Both: ${buy_cost:.4f} | Edge: ${buy_edge:.4f} | No arb", end="")
                    else:
                        print(f"\n   ⚠️ Empty order book - waiting for liquidity", end="")
                
                if not found_arb:
                    print()  # New line after compact output
                
                # Wait before next check
                time.sleep(interval)
                
        except KeyboardInterrupt:
            print(f"\n\n{'='*60}")
            print(f"🛑 MONITORING STOPPED BY USER")
            print(f"{'='*60}")
            print(f"   Total iterations: {iteration}")
            print(f"   Arbitrage opportunities found: {arbitrage_found_count}")
            print(f"   Total arbitrage across all markets: {total_arbitrage_found + arbitrage_found_count}")
            print(f"{'='*60}")
            return


def find_arbitrage_by_slug(slug: str):
    """
    One-time arbitrage check for a specific market slug.
    """
    print(f"\n{'='*60}")
    print(f"🔍 POLYMARKET ARBITRAGE FINDER")
    print(f"{'='*60}")
    print(f"\n📌 Searching for: {slug}")
    
    print(f"\n📡 Fetching market data...")
    event_data = fetch_market_by_slug(slug)
    
    if not event_data:
        print(f"❌ Could not find market with slug: {slug}")
        return None
    
    print(f"✅ Found event: {event_data.get('title', 'Unknown')}")
    
    print(f"\n🔧 Initializing CLOB client...")
    try:
        client = get_client()
        print(f"✅ Client initialized")
    except Exception as e:
        print(f"❌ Failed to initialize client: {e}")
        return None
    
    markets = event_data.get("markets", [])
    print(f"\n📊 Found {len(markets)} market(s) to analyze")
    
    results = []
    for i, market in enumerate(markets, 1):
        question = market.get('question', 'Unknown')
        market_slug = market.get('slug', 'unknown')
        liquidity = float(market.get('liquidity', 0))
        
        print(f"\n{'─'*60}")
        print(f"Market {i}/{len(markets)}: {question[:80]}...")
        print(f"   Slug: {market_slug}")
        print(f"   Liquidity: ${liquidity:,.2f}")
        
        if market.get('closed', False):
            print(f"   ⚠️  Market is CLOSED - skipping")
            continue
        
        if liquidity < 100:
            print(f"   ⚠️  Low liquidity - results may be unreliable")
        
        result = analyze_orderbook_arbitrage(client, market)
        if result:
            results.append(result)
    
    # Summary
    print(f"\n{'='*60}")
    print(f"📋 SUMMARY")
    print(f"{'='*60}")
    
    arb_count = sum(1 for r in results if r.get('has_arbitrage'))
    
    if arb_count > 0:
        print(f"\n🎯 Found {arb_count} arbitrage opportunity(s)!")
        for r in results:
            if r.get('has_arbitrage'):
                print(f"\n   Market: {r['question'][:60]}...")
                print(f"   ✅ Edge: ${r['buy_edge']:.4f} ({r['buy_edge']*100:.2f}%)")
    else:
        print(f"\n❌ No arbitrage opportunities found in this market")
    
    return results


def main():
    """Continuous monitoring with auto-generated slug based on current ET time."""
    et_now = get_current_et_time()
    
    print(f"\n{'='*60}")
    print(f"🎰 POLYMARKET ARBITRAGE SCANNER")
    print(f"{'='*60}")
    print(f"   ET Time: {et_now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   Asset: {MARKET_BASE.upper()}")
    print(f"\nSelect timeframe:")
    print(f"   1. Hourly")
    print(f"   2. 15m")
    print(f"{'─'*60}")
    
    choice = input("Enter choice (1 or 2): ").strip()
    
    if choice == "2":
        timeframe = "15m"
    else:
        timeframe = "hourly"
    
    slug = generate_market_slug(MARKET_BASE, timeframe)
    
    print(f"\n{'='*60}")
    print(f"   Timeframe: {timeframe}")
    print(f"   Slug: {slug}")
    print(f"{'─'*60}")
    print(f"   💰 Trading: {'ENABLED' if EXECUTE_TRADES else 'DISABLED'}")
    if EXECUTE_TRADES:
        print(f"   📊 Max position: ${MAX_POSITION_SIZE} per trade")
        print(f"   📈 Min edge: {MIN_EDGE*100:.1f}%")
        if not PRIVATE_KEY or not FUNDER:
            print(f"   ⚠️  WARNING: Missing PRIVATE_KEY or FUNDER in .env!")
        else:
            # Pre-initialize trading client for faster first execution
            print(f"   ⚡ Pre-warming trading client...")
            try:
                get_trading_client()
                print(f"   ✅ Trading client ready!")
            except Exception as e:
                print(f"   ❌ Failed to init trading client: {e}")
    print(f"{'='*60}")
    
    monitor_market_continuous(slug, auto_update=True, base=MARKET_BASE, timeframe=timeframe)


if __name__ == "__main__":
    main()
