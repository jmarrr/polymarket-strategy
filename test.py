from py_clob_client.client import ClobClient


def main():
    host = "https://clob.polymarket.com"
    client = ClobClient(host)

    orderbook = client.get_order_book(
        "67015398797318643638076760033194648614803914000175296510947890653124241941595"
    )
    
    # Sort asks ascending (lowest price = best ask)
    sorted_asks = sorted(orderbook.asks, key=lambda x: float(x.price))
    # Sort bids descending (highest price = best bid)
    sorted_bids = sorted(orderbook.bids, key=lambda x: float(x.price), reverse=True)
    
    # Best ask = lowest ask price
    if sorted_asks:
        best_ask = sorted_asks[0]
        print(f"Best Ask: price=${best_ask.price}, size={best_ask.size}")
    else:
        print("No asks available")
    
    # Best bid = highest bid price
    if sorted_bids:
        best_bid = sorted_bids[0]
        print(f"Best Bid: price=${best_bid.price}, size={best_bid.size}")
    else:
        print("No bids available")
    
    # Show sorted order book
    print(f"\nAsks (sorted low→high):")
    for ask in sorted_asks[:5]:
        print(f"   ${ask.price} x {ask.size}")
    
    print(f"\nBids (sorted high→low):")
    for bid in sorted_bids[:5]:
        print(f"   ${bid.price} x {bid.size}")

    # hash = client.get_order_book_hash(orderbook)
    # print("orderbook hash", hash)


main()