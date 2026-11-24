"""Read portal acknowledgements for a shipment filing."""
from .manifest import prepare


def status(document, acknowledgements):
    prepared = prepare(document)
    return 'accepted' if any(row['manifest_id'] == prepared['manifest_id'] for row in acknowledgements) else 'pending'
