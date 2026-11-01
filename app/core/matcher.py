from collections import defaultdict

from .models import Batch, Confirmation, Delivery, Order


def match_order(batch: Batch, order_id: str) -> tuple[Order, list[Delivery], dict[str, list[Confirmation]]]:
    matches = [order for order in batch.orders if order.order_id == order_id]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one order {order_id}; found {len(matches)}")
    deliveries = [item for item in batch.deliveries if item.order_id == order_id]
    confirmations: dict[str, list[Confirmation]] = defaultdict(list)
    for item in batch.confirmations:
        confirmations[item.delivery_id].append(item)
    return matches[0], deliveries, confirmations
