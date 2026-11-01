from decimal import Decimal
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class OrderItem(BaseModel):
    sku: str
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)


class Order(BaseModel):
    order_id: str
    customer: str
    currency: str = "CNY"
    items: list[OrderItem] = Field(min_length=1)
    source: str
    delivery_due_date: date | None = None
    allow_split_delivery: bool = False


class DeliveryItem(BaseModel):
    sku: str
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)


class Delivery(BaseModel):
    delivery_id: str
    order_id: str
    customer: str
    items: list[DeliveryItem] = Field(min_length=1)
    declared_amount: Decimal = Field(ge=0)
    source: str


class ConfirmationItem(BaseModel):
    sku: str
    quantity: Decimal = Field(ge=0)


class Confirmation(BaseModel):
    confirmation_id: str
    delivery_id: str
    items: list[ConfirmationItem] = Field(default_factory=list)
    evidence_ref: str | None = None
    source: str
    condition: Literal["accepted", "partial", "damaged", "refused"] = "accepted"
    note: str = ""


class Batch(BaseModel):
    orders: list[Order]
    deliveries: list[Delivery]
    confirmations: list[Confirmation]


class Issue(BaseModel):
    issue_id: str
    order_id: str
    delivery_id: str | None = None
    code: str
    detail: str
    sources: list[str]
    action: str


class EvidenceTask(BaseModel):
    task_id: str
    issue_id: str
    order_id: str
    status: Literal["open", "resolved"] = "open"
    action: str
    resolution: str | None = None


class AuditEvent(BaseModel):
    step: str
    detail: str


class VerificationResult(BaseModel):
    order_id: str
    status: Literal["verified", "needs_evidence"]
    issues: list[Issue]
    tasks: list[EvidenceTask]
    log: list[AuditEvent]
