import os
import json

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from dotenv import load_dotenv
from py_clob_client.constants import POLYGON, END_CURSOR
from py_clob_client.order_builder.constants import BUY

load_dotenv()

HOST = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
FUNDER = os.getenv("FUNDER_ADDRESS")
GAMMA_HOST = os.getenv("GAMMA_API_URL", "https://gamma-api.polymarket.com")


def fetch_events_from_gamma(limit: int = 200) -> list[dict]:
    """
    Fetch event data from Polymarket Gamma `/events` endpoint.

    Currently filters to the 'up-or-down' tag (Up/Down BTC style markets).
    Increase `limit` or add pagination if you need more.
    """
    events: list[dict] = []
    offset = 0

    while True:
        resp = requests.get(
            f"{GAMMA_HOST}/events",
             params={
                "tags": "up-or-down",  # Up or Down events group
                "closed": "false",     # only open events
                "limit": limit,
                "offset": offset,
            },
            timeout=10,
        )

        if resp.status_code >= 400:
            print(f"Gamma API error {resp.status_code}: {resp.text}")
            break

        data = resp.json()
        page_events = data
        if isinstance(data, dict):
            page_events = data.get("events") or data.get("data") or []

        if not page_events:
            break

        events.extend(page_events)

        if len(page_events) < limit:
            break

        offset += limit

    return events


def main():
    token_id = "109359485040712045735067582106254400326549492004655837986062297574272503755662"

    # CLOB client setup (kept in case you still need trading features)
    client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=POLYGON, signature_type=2, funder=FUNDER)
    client.set_api_creds(client.create_or_derive_api_creds())

    # --- Fetch /events data from Gamma and save to JSON ---
    events = fetch_events_from_gamma(limit=200)

    output_path = os.path.join(os.path.dirname(__file__), "events.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2)

    print(f"Saved {len(events)} events to {output_path}")

    # order = OrderArgs(
    #     price=0.32,
    #     size=5.0,
    #     side=BUY,
    #     token_id=token_id
    # )

    # signed = client.create_order(order)
    # resp = client.post_order(signed, OrderType.GTC)
    # print(resp)


if __name__ == "__main__":
    main()