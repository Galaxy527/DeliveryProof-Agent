"""Generate ten anonymous construction-material delivery cases."""
import json
import csv
from pathlib import Path


orders, deliveries, confirmations = [], [], []
for n in range(1, 11):
    oid, did = f"ORD-{n:03}", f"DEL-{n:03}"
    orders.append({"order_id": oid, "customer": "张先生", "currency": "CNY",
                   "items": [{"sku": "水泥", "quantity": "100", "unit_price": "30"}],
                   "source": f"orders/{oid}.json"})
    qty = "95" if n in (1, 3) else "100"
    price = "31" if n == 4 else "30"
    declared = "2999" if n == 5 else str(int(qty) * int(price))
    deliveries.append({"delivery_id": did, "order_id": oid, "customer": "张先生",
                       "items": [{"sku": "水泥", "quantity": qty, "unit_price": price}],
                       "declared_amount": declared, "source": f"deliveries/{did}.json"})
    if n != 1 and n != 2:
        confirmations.append({"confirmation_id": f"CONF-{n:03}", "delivery_id": did,
                              "items": [{"sku": "水泥", "quantity": "90" if n == 6 else qty}],
                              "evidence_ref": f"signed/{did}.pdf", "source": f"confirmations/CONF-{n:03}.json"})
    if n == 2:
        confirmations.append({"confirmation_id": "CONF-002", "delivery_id": did,
                              "items": [{"sku": "水泥", "quantity": "100"}],
                              "evidence_ref": None, "source": "confirmations/CONF-002.json"})

# Duplicate delivery ID, kept as a separate source for audit.
deliveries.append({**deliveries[6], "source": "deliveries/DEL-007-copy.json"})
# A normal split delivery with signed confirmation for each part.
deliveries[7]["items"][0]["quantity"] = "60"
deliveries[7]["declared_amount"] = "1800"
confirmations[6]["items"][0]["quantity"] = "60"
deliveries.append({"delivery_id": "DEL-008-B", "order_id": "ORD-008", "customer": "张先生",
                   "items": [{"sku": "水泥", "quantity": "40", "unit_price": "30"}],
                   "declared_amount": "1200", "source": "deliveries/DEL-008-B.json"})
confirmations.append({"confirmation_id": "CONF-008-B", "delivery_id": "DEL-008-B",
                      "items": [{"sku": "水泥", "quantity": "40"}],
                      "evidence_ref": "signed/DEL-008-B.pdf", "source": "confirmations/CONF-008-B.json"})
# Two partial confirmations against one delivery.
confirmations[7]["items"][0]["quantity"] = "60"
confirmations.append({"confirmation_id": "CONF-009-B", "delivery_id": "DEL-009",
                      "items": [{"sku": "水泥", "quantity": "40"}],
                      "evidence_ref": "signed/DEL-009-B.pdf", "source": "confirmations/CONF-009-B.json"})
# An over-delivery example.
deliveries[9]["items"][0]["quantity"] = "105"
deliveries[9]["declared_amount"] = "3150"
confirmations[8]["items"][0]["quantity"] = "105"

path = Path(__file__).with_name("batch.json")
path.write_text(json.dumps({"orders": orders, "deliveries": deliveries,
                            "confirmations": confirmations}, ensure_ascii=False, indent=2), encoding="utf-8")
print(path)

csv_dir = path.parent / "csv"
csv_dir.mkdir(exist_ok=True)
for filename, records, fields, item_fields in (
    ("orders.csv", orders, ("order_id", "customer", "currency", "source"),
     ("sku", "quantity", "unit_price")),
    ("deliveries.csv", deliveries, ("delivery_id", "order_id", "customer", "declared_amount", "source"),
     ("sku", "quantity", "unit_price")),
    ("confirmations.csv", confirmations, ("confirmation_id", "delivery_id", "evidence_ref", "source"),
     ("sku", "quantity")),
):
    with (csv_dir / filename).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields + item_fields)
        writer.writeheader()
        for record in records:
            for item in record["items"] or [{}]:
                writer.writerow({**{field: record[field] for field in fields}, **item})
