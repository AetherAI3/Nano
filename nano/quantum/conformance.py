"""Deterministic fixture generators; no runtime, randomness or external I/O."""
from .contract import QuantumPlan, build_plan, prepare_bits, sample
from .canonical import canonical_digest


def large_mapping_fixture() -> QuantumPlan:
    """400 recipients, three distinct chunks each, one preparation and PUB per chunk."""
    bank = {"records": [f"record-{i}" for i in range(1200)]}
    digest = canonical_digest(bank)
    operations = []
    for recipient in range(400):
        for chunk in range(3):
            index = recipient * 3 + chunk
            # Bits are in variable order. IBM's display convention reverses them.
            bits = format(index, "011b")[::-1]
            packet = {"recipient_id": f"recipient-{recipient:03d}", "chunk": chunk, "slot": 0,
                      "evidence_bank": {"reference": "fixture:large-bank", "digest": digest},
                      "codec": {"name": "evidence_index", "version": 1}, "payload_width": 11,
                      "authentication_width": 0, "bit_significance": "lsb_first", "expected_records": 1}
            preparation_id = f"prepare-{index}"
            operations.append(prepare_bits(bits, operation_id=preparation_id, packets=[packet]))
            operations.append(sample(preparation_id, operation_id=f"sample-{index}", expected_width=11,
                                     shots=2, max_circuits=1200, max_wall_s=60, max_qpu_s=0,
                                     pub_index=index))
    return build_plan(operations)
