#!/usr/bin/env python3
"""Offline end-to-end example for the metrological audit/credential API.

The script builds two batches from real ``process_cycles`` inputs:

1. ``batch-shared`` charges and discharges through one shared metering
   channel.  With nominal 10 Wh / 9 Wh and threshold 0.9 the margin is
   identically zero, so a nonzero-width calibration interval still passes.
2. ``batch-indep`` uses two independent channels.  It passes first, becomes
   uncertain when the discharge channel interval is widened, and is judged
   again after the interval is shrunk back.  Its first credential stays
   permanently revoked even though the later calibration is safe again.

Run directly from a source checkout:

    python3 examples/run_metrology.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from battery_cycles import MetrologyStore  # noqa: E402


def sample(sid: str, time_s: str, current_a: str, voltage_v: str = "1") -> dict[str, str]:
    return {
        "id": sid,
        "time_s": time_s,
        "voltage_v": voltage_v,
        "current_a": current_a,
    }


def exact_energy_run(run_id: str, charge_wh: int, discharge_wh: int) -> dict:
    """A real process_cycles run whose computed energies are exactly the targets."""

    # The 1 s charge->rest switch trapezoid adds Ic/7200 Wh, so the charge
    # current is solved to make the total exactly charge_wh.
    charge_current = Fraction(charge_wh) * Fraction(7200, 7201)
    rows = [
        sample("s0", "0", str(charge_current)),
        sample("s1", "3600", str(charge_current)),
        sample("s2", "3601", "0"),
        sample("s3", "7201", "0"),
        sample("s4", "7202", str(-discharge_wh)),
        sample("s5", "10802", str(-discharge_wh)),
    ]
    return {
        "run_id": run_id,
        "closed": True,
        "final_ordinal": 0,
        "chunks": [{"chunk_id": "c0", "ordinal": 0, "samples": rows}],
    }


def process_input(*runs: dict) -> dict:
    return {
        "active_current_a": "0.5",
        "min_phase_seconds": "0",
        "runs": list(runs),
    }


def main() -> None:
    shared_data = process_input(exact_energy_run("run-shared", 10, 9))
    indep_data = process_input(exact_energy_run("run-indep", 10, 9.2))

    shared_request = {
        "batch_id": "batch-shared",
        "threshold": "0.9",
        "charge_channel": "m-shared",
        "discharge_channel": "m-shared",
        "process_input": shared_data,
        "samples": [{"sample_id": "shared-1", "run_id": "run-shared", "cycle_id": "c1"}],
    }
    indep_request = {
        "batch_id": "batch-indep",
        "threshold": "0.9",
        "charge_channel": "m-charge",
        "discharge_channel": "m-discharge",
        "process_input": indep_data,
        "samples": [{"sample_id": "indep-1", "run_id": "run-indep", "cycle_id": "c1"}],
    }

    with tempfile.TemporaryDirectory() as directory:
        store = MetrologyStore(Path(directory) / "metrology.json")
        print("== initial archive (empty) ==")
        print("current version:", store.current_version)

        # Version 1: all three channels registered.
        store.apply_correction(
            {
                "m-shared": {"lo": "-0.1", "hi": "0.1"},
                "m-charge": {"lo": "0", "hi": "0"},
                "m-discharge": {"lo": "0", "hi": "0"},
            }
        )
        print("\n== version 1 audits ==")
        print(json.dumps(store.audit(shared_request), indent=2, ensure_ascii=False))
        print(json.dumps(store.audit(indep_request), indent=2, ensure_ascii=False))

        store.issue_credential("cred-shared-v1", shared_request)
        store.issue_credential("cred-indep-v1", indep_request)

        # Version 2: widen only the discharge channel of the independent batch.
        store.apply_correction(
            {"m-discharge": {"lo": "-0.03", "hi": "0.03"}}
        )
        print("\n== version 2: discharge interval widened ==")
        print(json.dumps(store.audit(indep_request), indent=2, ensure_ascii=False))
        print("shared still usable:", store.check_access("cred-shared-v1")["usable"])
        print("indep still usable:", store.check_access("cred-indep-v1")["usable"])

        # Version 3: shrink the discharge interval back to zero width.
        store.apply_correction({"m-discharge": {"lo": "0", "hi": "0"}})
        print("\n== version 3: discharge interval restored ==")
        print("indep audit verdict:", store.audit(indep_request)["verdict"])
        old_access = store.check_access("cred-indep-v1")
        print(
            "old indep credential usable:",
            old_access["usable"],
            "(first failed version:",
            old_access["first_failed_version"],
            "sample:",
            old_access["first_failed_sample"],
            ")",
        )

        # A new credential can be issued; the old number never revives.
        new_credential = store.issue_credential("cred-indep-v3", indep_request)
        print("new indep credential:", new_credential["credential_id"],
              "issued at version", new_credential["issued_at_version"])

        print("\n== immutable issuance archive (old credential) ==")
        print(json.dumps(store.get_credential("cred-indep-v1"), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
