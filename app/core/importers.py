"""Load either one JSON batch or a directory of three flat CSV files."""
import csv
from collections import OrderedDict
from pathlib import Path

from .models import Batch


def load_batch(path: Path) -> Batch:
    if path.is_file() and path.suffix.lower() == ".json":
        return Batch.model_validate_json(path.read_text(encoding="utf-8"))
    if not path.is_dir():
        raise ValueError("Input must be a JSON batch or a CSV directory")
    specs = {
        "orders": ("order_id", ("order_id", "customer", "currency", "source"),
                   ("sku", "quantity", "unit_price")),
        "deliveries": ("delivery_id", ("delivery_id", "order_id", "customer",
                                        "declared_amount", "source"),
                       ("sku", "quantity", "unit_price")),
        "confirmations": ("confirmation_id", ("confirmation_id", "delivery_id",
                                              "evidence_ref", "source"),
                          ("sku", "quantity")),
    }
    data = {}
    for name, (key, headers, item_fields) in specs.items():
        file = path / f"{name}.csv"
        with file.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = set(headers) | set(item_fields)
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError(f"{file}: missing columns {sorted(required - set(reader.fieldnames or []))}")
            groups = OrderedDict()
            for line_number, row in enumerate(reader, start=2):
                if not row[key] or not row["source"]:
                    raise ValueError(f"{file}:{line_number}: missing ID or source")
                identity = (row[key], row["source"])
                header = {field: row[field] for field in headers}
                if name == "confirmations" and not header["evidence_ref"]:
                    header["evidence_ref"] = None
                if identity not in groups:
                    groups[identity] = {**header, "items": []}
                elif any(groups[identity][field] != header[field] for field in headers):
                    raise ValueError(f"{file}:{line_number}: inconsistent document header")
                if row["sku"]:
                    groups[identity]["items"].append({field: row[field] for field in item_fields})
                elif name != "confirmations":
                    raise ValueError(f"{file}:{line_number}: missing SKU")
            data[name] = list(groups.values())
    return Batch.model_validate(data)
