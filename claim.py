"""
Auto-Claim Script for Polymarket

Checks for redeemable positions and claims them.
Run via cron/Task Scheduler every 1-2 hours.

Requires: pip install polymarket-apis (Python >=3.12)
"""

import os
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Configuration
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS")

# Minimum position size to bother redeeming (avoid dust)
MIN_REDEEM_SIZE = 1


async def get_redeemable_positions(data_client, user_address: str) -> list:
    """Fetch all redeemable positions for user."""
    try:
        # get_positions returns a list directly (not a coroutine)
        positions = data_client.get_positions(
            user=user_address,
            redeemable=True,
            size_threshold=MIN_REDEEM_SIZE,
            limit=100
        )
        return positions
    except Exception as e:
        logger.error(f"Error fetching positions: {e}")
        return []


async def redeem_position(web3_client, position) -> bool:
    """Redeem a single position."""
    try:
        # Position is a Pydantic model, access as attributes
        condition_id = getattr(position, "conditionId", None) or getattr(position, "condition_id", None)
        size = int(float(getattr(position, "size", 0)))
        outcome_index = getattr(position, "outcomeIndex", None)
        if outcome_index is None:
            outcome_index = getattr(position, "outcome_index", 0)
        neg_risk = getattr(position, "negRisk", None)
        if neg_risk is None:
            neg_risk = getattr(position, "neg_risk", False)
        title = str(getattr(position, "title", "Unknown"))[:40]

        if size <= 0:
            return False

        # Build amounts array: [Yes shares, No shares]
        # outcomeIndex 0 = Yes, 1 = No
        if outcome_index == 0:
            amounts = [size, 0]
        else:
            amounts = [0, size]

        logger.info(f"Redeeming: {title} | {size} shares")

        # Try different redeem methods based on what's available
        if hasattr(web3_client, 'redeem_positions'):
            tx_hash = await web3_client.redeem_positions(
                condition_id=condition_id,
                amounts=amounts,
            )
        elif hasattr(web3_client, 'redeem'):
            tx_hash = await web3_client.redeem(
                condition_id=condition_id,
                amounts=amounts,
            )
        else:
            # List available methods for debugging
            methods = [m for m in dir(web3_client) if not m.startswith('_')]
            logger.error(f"  -> No redeem method found. Available: {methods[:20]}")
            return False

        logger.info(f"  -> Success! TX: {tx_hash}")
        return True

    except Exception as e:
        logger.error(f"  -> Failed: {e}")
        return False


async def main():
    """Main claim loop."""
    try:
        from polymarket_apis import (
            PolymarketDataClient,
            PolymarketGaslessWeb3Client,
        )
    except ImportError:
        logger.error("polymarket-apis not installed. Run: pip install polymarket-apis")
        logger.error("Requires Python >= 3.12")
        return

    if not PRIVATE_KEY or not FUNDER_ADDRESS:
        logger.error("Missing PRIVATE_KEY or FUNDER_ADDRESS in .env")
        return

    logger.info("=" * 50)
    logger.info("Polymarket Auto-Claim")
    logger.info("=" * 50)

    # Initialize clients
    data_client = PolymarketDataClient()

    # Use gasless client for Safe wallets (no gas fees)
    web3_client = PolymarketGaslessWeb3Client(
        private_key=PRIVATE_KEY,
        signature_type=2,  # Safe/Gnosis wallet
    )

    # Get redeemable positions
    logger.info(f"Checking positions for {FUNDER_ADDRESS}...")
    positions = await get_redeemable_positions(data_client, FUNDER_ADDRESS)

    if not positions:
        logger.info("No redeemable positions found.")
        return

    logger.info(f"Found {len(positions)} redeemable position(s)")

    # Redeem each position
    success_count = 0
    total_value = 0.0

    for pos in positions:
        value = float(getattr(pos, "currentValue", 0) or getattr(pos, "current_value", 0) or 0)
        total_value += value

        if await redeem_position(web3_client, pos):
            success_count += 1

        # Small delay between redeems
        await asyncio.sleep(1)

    # Summary
    logger.info("-" * 50)
    logger.info(f"Redeemed: {success_count}/{len(positions)} positions")
    logger.info(f"Total value: ${total_value:.2f}")
    logger.info("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
