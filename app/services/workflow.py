from app.core.models import Batch, Confirmation, VerificationResult
from .task_manager import TaskManager


def submit_confirmation(batch: Batch, confirmation: Confirmation, manager: TaskManager) -> VerificationResult:
    """Accept a new evidence record and immediately reverify its order."""
    matching = [delivery for delivery in batch.deliveries if delivery.delivery_id == confirmation.delivery_id]
    if len(matching) != 1:
        raise ValueError(f"Expected one delivery {confirmation.delivery_id}; found {len(matching)}")
    if any(existing.confirmation_id == confirmation.confirmation_id for existing in batch.confirmations):
        raise ValueError(f"Confirmation {confirmation.confirmation_id} already exists")
    batch.confirmations.append(confirmation)
    return manager.verify(batch, matching[0].order_id)
