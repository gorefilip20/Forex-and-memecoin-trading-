"""State reconciliation helpers. A mismatch must pause new entries."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Reconciliation:
    ok: bool
    mismatches: tuple[str, ...]


def compare_positions(local: dict[str, dict], broker: dict[str, dict]) -> Reconciliation:
    mismatches = []
    if set(local) != set(broker):
        missing_local = sorted(set(broker) - set(local))
        missing_broker = sorted(set(local) - set(broker))
        if missing_local: mismatches.append(f"broker-only positions: {','.join(missing_local)}")
        if missing_broker: mismatches.append(f"local-only positions: {','.join(missing_broker)}")
    for ticket in sorted(set(local) & set(broker)):
        for key in ("direction", "entry_price", "stop_loss", "take_profit"):
            if str(local[ticket].get(key)) != str(broker[ticket].get(key)):
                mismatches.append(f"{ticket}:{key} differs")
    return Reconciliation(not mismatches, tuple(mismatches))
