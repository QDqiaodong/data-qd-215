#!/usr/bin/env python3
"""Offline example for the chunk-aware ``process_cycles`` API.

Run directly from a source checkout:

    python3 examples/run_cycles.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from battery_cycles import process_cycles  # noqa: E402


def sample(sid: str, time_s: str, current_a: str) -> dict[str, str]:
    return {
        "id": sid,
        "time_s": time_s,
        "voltage_v": "10",
        "current_a": current_a,
    }


def main() -> None:
    # s2 is the shared boundary sample: c0 ends with it and c1 starts with it.
    rows = [
        sample("s0", "0", "2"),
        sample("s1", "10", "2"),
        sample("s2", "20", "0"),
        sample("s3", "30", "-1"),
        sample("s4", "40", "-1"),
    ]
    chunk0 = {"chunk_id": "chunk-0", "ordinal": 0, "samples": rows[:3]}
    chunk1 = {"chunk_id": "chunk-1", "ordinal": 1, "samples": rows[2:]}

    # Deliberately reverse arrival order and replay chunk-0 exactly.
    data = {
        "active_current_a": "1",
        "min_phase_seconds": "5",
        "runs": [
            {
                "run_id": "battery-run-001",
                "closed": True,
                "final_ordinal": 1,
                "chunks": [chunk1, chunk0, dict(chunk0)],
            }
        ],
    }

    result = process_cycles(data)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
