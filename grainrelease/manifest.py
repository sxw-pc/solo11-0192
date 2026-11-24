"""Prepare grain shipment filing sections."""

def prepare(document):
    return {'manifest_id': document['manifest_id'], 'pages': document['pages']}
