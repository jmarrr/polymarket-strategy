import os
import json
import time
import requests
from datetime import datetime
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from dotenv import load_dotenv
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY

load_dotenv()

# API Configuration
CLOB_HOST = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")
GAMMA_HOST = "https://gamma-api.polymarket.com"
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
FUNDER = os.getenv("FUNDER_ADDRESS")

# ═══════════════════════════════════════════════════════════════════════════════
# LIQUIDITY PROVIDER CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

# Strategy: Buy both YES and NO below mid to capture spread
# Profit = $1.00 (resolution) - (YES cost + NO cost)

# Liquidity Requirements
MIN_LIQUIDITY_USDC = 100          # Minimum total market liquidity ($)
MIN_ORDER_BOOK_DEPTH = 2          # Minimum number of orders on each side
MIN_BEST_LEVEL_SIZE = 5           # Minimum size at best bid/ask
MAX_SPREAD_CENTS = 0.10           # Maximum spread in $ (wider = more opportunity)

# Pricing Strategy
BID_BELOW_MID_BPS = 50            # Place bids X bps below mid (50 = 0.5%)
MIN_EDGE_CENTS = 0.01             # Minimum edge to place orders (1 cent = $0.01)
TARGET_EDGE_CENTS = 0.02          # Target edge when both sides fill (2 cents)

# Position Management
ORDER_SIZE = 10                   # Size per order in shares
MAX_POSITION_PER_SIDE = 100       # Max shares per outcome
MAX_IMBALANCE = 20                # Max difference between YES and NO holdings

# Timing
REFRESH_INTERVAL = 1.0            # Seconds between order book checks
QUOTE_LIFETIME = 15               # Seconds before refreshing quotes

# Execution
DRY_RUN = True                    # Set False to execute real trades

# ═══════════════════════════════════════════════════════════════════════════════


class OrderBook:
    """Represents an order book with analysis methods."""
    
    def __init__(self, bids: list, asks: list):
        self.bids = sorted(bids, key=lambda x: float(x.price), reverse=True) if bids else []
        self.asks = sorted(asks, key=lambda x: float(x.price)) if asks else []
    
    @property
    def best_bid(self) -> Optional[float]:
        return float(self.bids[0].price) if self.bids else None
    
    @property
    def best_ask(self) -> Optional[float]:
        return float(self.asks[0].price) if self.asks else None
    
    @property
    def best_bid_size(self) -> Optional[float]:
        return float(self.bids[0].size) if self.bids else None
    
    @property
    def best_ask_size(self) -> Optional[float]:
        return float(self.asks[0].size) if self.asks else None
    
    @property
    def spread(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None
    
    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None
    
    def get_cumulative_size(self, side: str, levels: int = 3) -> float:
        book = self.bids if side == "bid" else self.asks
        return sum(float(level.size) for level in book[:levels])


class LiquidityProvider:
    """
    Balanced Liquidity Provider for Polymarket.
    
    Strategy:
    1. Place BUY orders on BOTH YES and NO sides
    2. Price below mid to capture spread
    3. When both fill: guaranteed $1 at resolution, profit = edge
    4. Keep positions balanced to minimize directional risk
    """
    
    def __init__(self, slug: str, dry_run: bool = DRY_RUN):
        self.slug = slug
        self.dry_run = dry_run
        self.client = None
        self.market = None
        self.token_ids = {}
        self.outcomes = {}
        
        # Position tracking
        self.position = {"yes": 0, "no": 0}
        self.avg_cost = {"yes": 0.0, "no": 0.0}
        self.active_orders = {"yes": [], "no": []}
        
        # Stats
        self.orders_placed = 0
        self.total_cost = 0.0
        self.paired_positions = 0  # Positions that are hedged (min of YES, NO)
    
    def initialize(self) -> bool:
        """Initialize client and fetch market data."""
        print(f"\n{'═'*70}")
        print(f"💧 POLYMARKET LIQUIDITY PROVIDER")
        print(f"{'═'*70}")
        print(f"\n📌 Market: {self.slug}")
        print(f"🔧 Mode: {'DRY RUN' if self.dry_run else '🔴 LIVE TRADING'}")
        
        # Initialize CLOB client
        print(f"\n⚙️  Initializing client...")
        try:
            self.client = ClobClient(
                CLOB_HOST, 
                key=PRIVATE_KEY, 
                chain_id=POLYGON, 
                signature_type=2, 
                funder=FUNDER
            )
            if PRIVATE_KEY and FUNDER:
                self.client.set_api_creds(self.client.create_or_derive_api_creds())
            print(f"   ✅ Client ready")
        except Exception as e:
            print(f"   ❌ Failed: {e}")
            return False
        
        # Fetch market
        print(f"\n📡 Fetching market...")
        market_data = self._fetch_market()
        if not market_data:
            print(f"   ❌ Market not found: {self.slug}")
            return False
        
        markets = market_data.get("markets", [])
        if not markets:
            print(f"   ❌ No tradeable markets found")
            return False
        
        # Use first open market
        self.market = None
        for m in markets:
            if not m.get('closed', False):
                self.market = m
                break
        
        if not self.market:
            print(f"   ❌ All markets are closed")
            return False
        
        # Parse token IDs and outcomes
        try:
            clob_token_ids = json.loads(self.market.get("clobTokenIds", "[]"))
            outcomes = json.loads(self.market.get("outcomes", "[]"))
            
            if len(clob_token_ids) != 2 or len(outcomes) != 2:
                print(f"   ❌ Not a binary market")
                return False
            
            outcome_0_lower = outcomes[0].lower()
            yes_idx = 0 if outcome_0_lower in ["yes", "up"] else 1
            no_idx = 1 if yes_idx == 0 else 0
            
            self.token_ids = {
                "yes": clob_token_ids[yes_idx],
                "no": clob_token_ids[no_idx]
            }
            self.outcomes = {
                "yes": outcomes[yes_idx],
                "no": outcomes[no_idx]
            }
            
            print(f"   ✅ Market: {self.market.get('question', 'Unknown')[:60]}...")
            print(f"   📊 Liquidity: ${float(self.market.get('liquidity', 0)):,.0f}")
            print(f"   🎯 {self.outcomes['yes']}: {self.token_ids['yes'][:20]}...")
            print(f"   🎯 {self.outcomes['no']}: {self.token_ids['no'][:20]}...")
            
        except Exception as e:
            print(f"   ❌ Failed to parse market: {e}")
            return False
        
        return True
    
    def _fetch_market(self) -> Optional[dict]:
        """Fetch market data by slug."""
        try:
            resp = requests.get(
                f"{GAMMA_HOST}/events",
                params={"slug": self.slug, "limit": 1},
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    return data[0]
            
            resp = requests.get(
                f"{GAMMA_HOST}/markets",
                params={"slug": self.slug, "limit": 1},
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    market = data[0]
                    return {"title": market.get("question", ""), "markets": [market], "slug": self.slug}
            
        except Exception as e:
            print(f"   ⚠️ Fetch error: {e}")
        return None
    
    def get_orderbooks(self) -> tuple[Optional[OrderBook], Optional[OrderBook]]:
        """Fetch order books for both outcomes."""
        try:
            yes_ob_raw = self.client.get_order_book(self.token_ids["yes"])
            no_ob_raw = self.client.get_order_book(self.token_ids["no"])
            
            yes_orderbook = OrderBook(
                yes_ob_raw.bids if hasattr(yes_ob_raw, 'bids') else [],
                yes_ob_raw.asks if hasattr(yes_ob_raw, 'asks') else []
            )
            no_orderbook = OrderBook(
                no_ob_raw.bids if hasattr(no_ob_raw, 'bids') else [],
                no_ob_raw.asks if hasattr(no_ob_raw, 'asks') else []
            )
            
            return yes_orderbook, no_orderbook
        except Exception as e:
            print(f"   ⚠️ Order book fetch error: {e}")
            return None, None
    
    def check_liquidity(self, yes_ob: OrderBook, no_ob: OrderBook) -> tuple[bool, list[str]]:
        """Check if market meets liquidity requirements."""
        failures = []
        
        liquidity = float(self.market.get('liquidity', 0))
        if liquidity < MIN_LIQUIDITY_USDC:
            failures.append(f"Liquidity ${liquidity:,.0f} < ${MIN_LIQUIDITY_USDC}")
        
        for name, ob in [("YES", yes_ob), ("NO", no_ob)]:
            if len(ob.bids) < MIN_ORDER_BOOK_DEPTH:
                failures.append(f"{name} bid depth {len(ob.bids)} < {MIN_ORDER_BOOK_DEPTH}")
            if len(ob.asks) < MIN_ORDER_BOOK_DEPTH:
                failures.append(f"{name} ask depth {len(ob.asks)} < {MIN_ORDER_BOOK_DEPTH}")
            if ob.spread and ob.spread > MAX_SPREAD_CENTS:
                failures.append(f"{name} spread ${ob.spread:.2f} > ${MAX_SPREAD_CENTS}")
        
        return len(failures) == 0, failures
    
    def calculate_opportunity(self, yes_ob: OrderBook, no_ob: OrderBook) -> dict:
        """
        Calculate the liquidity providing opportunity.
        
        Returns pricing for both sides and expected edge.
        """
        if not yes_ob.mid_price or not no_ob.mid_price:
            return {}
        
        yes_mid = yes_ob.mid_price
        no_mid = no_ob.mid_price
        
        # Calculate offset from mid
        offset = BID_BELOW_MID_BPS / 10000
        
        # Our bid prices (below mid to capture spread)
        yes_bid = yes_mid - offset
        no_bid = no_mid - offset
        
        # Clamp to valid range and don't cross the spread
        yes_bid = max(0.01, min(yes_bid, yes_ob.best_bid + 0.01 if yes_ob.best_bid else 0.99))
        no_bid = max(0.01, min(no_bid, no_ob.best_bid + 0.01 if no_ob.best_bid else 0.99))
        
        # Round to 2 decimals
        yes_bid = round(yes_bid, 2)
        no_bid = round(no_bid, 2)
        
        # Calculate expected edge if both fill
        total_cost = yes_bid + no_bid
        edge = 1.0 - total_cost  # At resolution, one side pays $1
        
        # Current market prices (what takers pay)
        yes_ask = yes_ob.best_ask or 0
        no_ask = no_ob.best_ask or 0
        taker_cost = yes_ask + no_ask
        
        return {
            "yes_mid": yes_mid,
            "no_mid": no_mid,
            "yes_bid": yes_bid,
            "no_bid": no_bid,
            "yes_best_bid": yes_ob.best_bid,
            "no_best_bid": no_ob.best_bid,
            "yes_best_ask": yes_ob.best_ask,
            "no_best_ask": no_ob.best_ask,
            "total_cost": total_cost,
            "edge": edge,
            "taker_cost": taker_cost,
            "taker_edge": 1.0 - taker_cost,
        }
    
    def should_place_orders(self, opp: dict) -> tuple[bool, str]:
        """Decide if we should place orders based on opportunity."""
        
        # Check minimum edge
        if opp["edge"] < MIN_EDGE_CENTS:
            return False, f"Edge ${opp['edge']:.3f} < ${MIN_EDGE_CENTS:.3f}"
        
        # Check position limits
        if self.position["yes"] >= MAX_POSITION_PER_SIDE:
            return False, f"YES position {self.position['yes']} >= {MAX_POSITION_PER_SIDE}"
        if self.position["no"] >= MAX_POSITION_PER_SIDE:
            return False, f"NO position {self.position['no']} >= {MAX_POSITION_PER_SIDE}"
        
        # Check imbalance
        imbalance = abs(self.position["yes"] - self.position["no"])
        if imbalance >= MAX_IMBALANCE:
            return False, f"Imbalance {imbalance} >= {MAX_IMBALANCE}"
        
        return True, "OK"
    
    def cancel_all_orders(self):
        """Cancel all active orders."""
        if self.dry_run:
            self.active_orders = {"yes": [], "no": []}
            return
        
        try:
            for side in ["yes", "no"]:
                for order_id in self.active_orders[side]:
                    try:
                        self.client.cancel(order_id)
                    except:
                        pass
                self.active_orders[side] = []
        except Exception as e:
            print(f"   ⚠️ Cancel error: {e}")
    
    def place_orders(self, opp: dict) -> bool:
        """Place balanced BUY orders on both sides."""
        
        print(f"\n   📊 Placing Orders:")
        print(f"      {self.outcomes['yes']}: BUY @ ${opp['yes_bid']:.2f} x {ORDER_SIZE}")
        print(f"      {self.outcomes['no']}:  BUY @ ${opp['no_bid']:.2f} x {ORDER_SIZE}")
        print(f"      Total Cost: ${opp['total_cost']:.4f} | Edge: ${opp['edge']:.4f}")
        
        if self.dry_run:
            print(f"      [DRY RUN] Orders not placed")
            return True
        
        success = True
        
        for side in ["yes", "no"]:
            try:
                price = opp[f"{side}_bid"]
                token_id = self.token_ids[side]
                
                order = OrderArgs(
                    price=price,
                    size=ORDER_SIZE,
                    side=BUY,
                    token_id=token_id
                )
                signed = self.client.create_order(order)
                resp = self.client.post_order(signed, OrderType.GTC)
                order_id = resp.get('orderID', resp.get('id', 'unknown'))
                self.active_orders[side].append(order_id)
                print(f"      ✅ {self.outcomes[side]} BUY placed: {order_id[:16]}...")
                self.orders_placed += 1
                
            except Exception as e:
                print(f"      ❌ {self.outcomes[side]} error: {e}")
                success = False
        
        return success
    
    def display_status(self, yes_ob: OrderBook, no_ob: OrderBook, opp: dict):
        """Display current market status."""
        print(f"\n   {'─'*60}")
        print(f"   📈 MARKET STATUS")
        print(f"   {'─'*60}")
        
        # Order book summary
        print(f"\n   {self.outcomes['yes']:>10}: bid=${yes_ob.best_bid:.2f} ask=${yes_ob.best_ask:.2f} mid=${opp['yes_mid']:.3f}")
        print(f"   {self.outcomes['no']:>10}: bid=${no_ob.best_bid:.2f} ask=${no_ob.best_ask:.2f} mid=${opp['no_mid']:.3f}")
        
        # Edge analysis
        print(f"\n   💰 EDGE ANALYSIS:")
        print(f"      Taker cost (buy at asks): ${opp['taker_cost']:.4f} → Edge: ${opp['taker_edge']:.4f}")
        print(f"      Our cost (buy at bids):   ${opp['total_cost']:.4f} → Edge: ${opp['edge']:.4f}")
        
        # Position
        paired = min(self.position["yes"], self.position["no"])
        unhedged_yes = self.position["yes"] - paired
        unhedged_no = self.position["no"] - paired
        
        print(f"\n   📦 POSITION:")
        print(f"      {self.outcomes['yes']}: {self.position['yes']} shares")
        print(f"      {self.outcomes['no']}: {self.position['no']} shares")
        print(f"      Paired (hedged): {paired} | Unhedged: Y={unhedged_yes} N={unhedged_no}")
        
        if paired > 0:
            guaranteed = paired * 1.0
            print(f"      💵 Guaranteed at resolution: ${guaranteed:.2f}")
    
    def run(self):
        """Main liquidity providing loop."""
        if not self.initialize():
            return
        
        print(f"\n{'═'*70}")
        print(f"🚀 STARTING LIQUIDITY PROVIDER")
        print(f"{'═'*70}")
        print(f"\n   📊 Strategy: Buy BOTH sides below mid")
        print(f"   💰 Target Edge: ${TARGET_EDGE_CENTS:.2f} per pair")
        print(f"   📦 Order Size: {ORDER_SIZE} shares per side")
        print(f"   🔄 Refresh: {REFRESH_INTERVAL}s | Quote Life: {QUOTE_LIFETIME}s")
        print(f"   ⚖️  Max Imbalance: {MAX_IMBALANCE} shares")
        print(f"   🛑 Press Ctrl+C to stop")
        
        iteration = 0
        last_quote_time = 0
        
        try:
            while True:
                iteration += 1
                now = datetime.now().strftime("%H:%M:%S")
                current_time = time.time()
                
                print(f"\n{'═'*70}")
                print(f"[{now}] Iteration #{iteration}")
                
                # Get order books
                yes_ob, no_ob = self.get_orderbooks()
                if not yes_ob or not no_ob:
                    print(f"   ⚠️ Failed to get order books")
                    time.sleep(REFRESH_INTERVAL)
                    continue
                
                # Check liquidity
                passes, failures = self.check_liquidity(yes_ob, no_ob)
                if not passes:
                    print(f"   ⚠️ Liquidity check failed:")
                    for f in failures:
                        print(f"      • {f}")
                    self.cancel_all_orders()
                    time.sleep(REFRESH_INTERVAL)
                    continue
                
                # Calculate opportunity
                opp = self.calculate_opportunity(yes_ob, no_ob)
                if not opp:
                    print(f"   ⚠️ Could not calculate opportunity")
                    time.sleep(REFRESH_INTERVAL)
                    continue
                
                # Display status
                self.display_status(yes_ob, no_ob, opp)
                
                # Check if we should place orders
                should_place, reason = self.should_place_orders(opp)
                
                # Refresh quotes if needed
                should_refresh = (current_time - last_quote_time) >= QUOTE_LIFETIME
                
                if should_refresh and should_place:
                    print(f"\n   🔄 Refreshing orders...")
                    self.cancel_all_orders()
                    self.place_orders(opp)
                    last_quote_time = current_time
                elif not should_place:
                    print(f"\n   ⏸️  Not placing orders: {reason}")
                    self.cancel_all_orders()
                else:
                    remaining = int(QUOTE_LIFETIME - (current_time - last_quote_time))
                    print(f"\n   ⏳ Order refresh in {remaining}s")
                
                time.sleep(REFRESH_INTERVAL)
                
        except KeyboardInterrupt:
            print(f"\n\n{'═'*70}")
            print(f"🛑 LIQUIDITY PROVIDER STOPPED")
            print(f"{'═'*70}")
        finally:
            print(f"\n🧹 Cancelling all orders...")
            self.cancel_all_orders()
            
            print(f"\n📊 FINAL STATS:")
            print(f"   Iterations: {iteration}")
            print(f"   Orders Placed: {self.orders_placed}")
            print(f"   Position: {self.outcomes['yes']}={self.position['yes']}, {self.outcomes['no']}={self.position['no']}")
            
            paired = min(self.position["yes"], self.position["no"])
            if paired > 0:
                print(f"   Paired Positions: {paired}")
                print(f"   Guaranteed Value: ${paired:.2f}")


def main():
    """Main entry point."""
    print(f"\n{'═'*70}")
    print(f"💧 POLYMARKET LIQUIDITY PROVIDER v2.0")
    print(f"{'═'*70}")
    print(f"\n📋 Strategy:")
    print(f"   • Place BUY orders on BOTH YES and NO")
    print(f"   • Price below mid to capture spread")
    print(f"   • When both fill: guaranteed $1 at resolution")
    print(f"   • Profit = $1.00 - (YES cost + NO cost)")
    
    if not PRIVATE_KEY or not FUNDER:
        print(f"\n⚠️  WARNING: Missing PRIVATE_KEY or FUNDER in .env")
        print(f"   Running in DRY RUN mode only")
    
    print(f"\n📝 Enter market slug (or 'q' to quit):")
    slug = input("\n> ").strip()
    
    if slug.lower() == 'q':
        print("Goodbye!")
        return
    
    if not slug:
        print("❌ No slug provided")
        return
    
    print(f"\n⚙️  Configuration:")
    print(f"   1. DRY RUN (no real trades)")
    print(f"   2. LIVE TRADING")
    
    choice = input("\nChoice [1]: ").strip()
    dry_run = choice != "2"
    
    if not dry_run:
        print(f"\n⚠️  WARNING: LIVE TRADING MODE")
        confirm = input("Type 'CONFIRM' to proceed: ").strip()
        if confirm != "CONFIRM":
            print("Cancelled.")
            return
    
    provider = LiquidityProvider(slug, dry_run=dry_run)
    provider.run()


if __name__ == "__main__":
    main()
