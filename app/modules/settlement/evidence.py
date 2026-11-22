from app.manifest import canonical, digest
from app.reasons import REASONS


def settlement_evidence(conclusion):
    payload = dict(conclusion, reason_labels=REASONS)
    return {'evidence_sha256': digest(canonical(payload).encode()), 'evidence': payload}
