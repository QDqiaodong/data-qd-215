from __future__ import annotations

import copy
import json
import pathlib
import sys
import unittest
from fractions import Fraction

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from battery_cycles import InputError, process, process_cycles, process_run  # noqa: E402


def sample(sid, time_s, current_a, voltage_v="10"):
    return {
        "id": sid,
        "time_s": str(time_s),
        "voltage_v": str(voltage_v),
        "current_a": str(current_a),
    }


def chunk(chunk_id, ordinal, samples):
    return {"chunk_id": chunk_id, "ordinal": ordinal, "samples": samples}


def make_run(run_id, chunks, *, closed=True, final_ordinal=None):
    if closed:
        if final_ordinal is None:
            final_ordinal = max(item["ordinal"] for item in chunks)
        return {
            "run_id": run_id,
            "closed": True,
            "final_ordinal": final_ordinal,
            "chunks": chunks,
        }
    return {
        "run_id": run_id,
        "closed": False,
        "final_ordinal": None,
        "chunks": chunks,
    }


def envelope(*runs, active="1", minimum="5"):
    return {
        "active_current_a": active,
        "min_phase_seconds": minimum,
        "runs": list(runs),
    }


def run_by_id(result, run_id):
    return next(item for item in result["runs"] if item["run_id"] == run_id)


def currents_to_samples(currents, *, start_time=0, step=10, prefix="s"):
    """Create len(currents)+1 samples; interval j inherits current j."""
    values = list(currents)
    times = [start_time + index * step for index in range(len(values) + 1)]
    row_currents = values + [values[-1]]
    return [
        sample(f"{prefix}{index}", times[index], row_currents[index])
        for index in range(len(row_currents))
    ]


class ChunkedCycleAcceptanceTests(unittest.TestCase):
    def baseline_samples(self):
        return [
            sample("s0", 0, 2),
            sample("s1", 10, 2),
            sample("s2", 20, 0),
            sample("s3", 30, -1),
            sample("s4", 40, -1),
        ]

    def baseline_chunks(self):
        rows = self.baseline_samples()
        return [
            chunk("c0", 0, rows[:3]),
            chunk("c1", 1, rows[2:]),
        ]

    def test_boundary_overlap_order_and_retransmission_are_idempotent(self):
        ordered = make_run("run-a", self.baseline_chunks())
        reordered_chunks = [
            self.baseline_chunks()[1],
            copy.deepcopy(self.baseline_chunks()[0]),
            copy.deepcopy(self.baseline_chunks()[0]),
        ]
        reordered = make_run("run-a", reordered_chunks)

        first = process_cycles(envelope(ordered))
        second = process_cycles(envelope(reordered))
        self.assertEqual(first, second)

        run = run_by_id(first, "run-a")
        self.assertEqual(run["status"], "closed")
        self.assertEqual([p["phase_id"] for p in run["phases"]], ["p1", "p2", "p3"])
        self.assertEqual([p["state"] for p in run["phases"]], ["charge", "rest", "discharge"])

        # The reconstructed sequence is exactly the baseline process input.
        baseline = process(
            {
                "active_current_a": "1",
                "min_phase_seconds": "5",
                "samples": self.baseline_samples(),
            }
        )
        self.assertEqual(run["phases"], baseline["phases"])

        cycle = run["cycles"][0]
        self.assertEqual(cycle["cycle_id"], "c1")
        self.assertEqual(cycle["phase_ids"], ["p1", "p2", "p3"])
        self.assertEqual(cycle["charge_wh"], "1/12")
        self.assertEqual(cycle["discharge_wh"], "1/36")
        self.assertEqual(cycle["round_trip_efficiency"], "1/3")
        self.assertEqual(cycle["status"], "eligible")
        self.assertIsNone(cycle["reason"])
        self.assertIsNone(run["pending_cycle"])
        self.assertIsNone(run["continuation"])

        # JSON-safe exact rationals are strings/null/integers only.
        json.dumps(first, allow_nan=False)

    def test_open_pending_differs_from_closed_incomplete_and_extension_is_stable(self):
        charge_only = self.baseline_samples()[:3]
        first_chunk = chunk("c0", 0, charge_only)

        open_run = make_run("open-run", [first_chunk], closed=False)
        closed_run = make_run("closed-run", [copy.deepcopy(first_chunk)], closed=True, final_ordinal=0)
        result = process_cycles(envelope(open_run, closed_run))

        opened = run_by_id(result, "open-run")
        self.assertEqual(opened["status"], "open")
        self.assertEqual(opened["orphan_phase_ids"], [])
        self.assertEqual(opened["cycles"], [])
        pending = opened["pending_cycle"]
        self.assertEqual(pending["cycle_id"], "c1")
        self.assertEqual(pending["phase_ids"], ["p1"])
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["reason"], "missing_discharge")
        self.assertEqual(pending["charge_wh"], "1/12")
        self.assertEqual(pending["discharge_wh"], "0")
        self.assertEqual(pending["round_trip_efficiency"], "0")
        self.assertEqual(
            opened["continuation"],
            {
                "next_ordinal": 1,
                "last_sample_id": "s2",
            },
        )
        self.assertEqual(opened["continuation"]["next_chunk_ordinal"], 1)

        closed = run_by_id(result, "closed-run")
        self.assertEqual(closed["status"], "closed")
        self.assertIsNone(closed["pending_cycle"])
        self.assertIsNone(closed["continuation"])
        incomplete = closed["cycles"][0]
        self.assertEqual(incomplete["cycle_id"], "c1")
        self.assertEqual(incomplete["phase_ids"], ["p1"])
        self.assertEqual(incomplete["status"], "incomplete")
        self.assertEqual(incomplete["reason"], "missing_discharge")

        # Supply the next ordinal chunk through the continuation point.
        extended_run = make_run(
            "open-run",
            [copy.deepcopy(first_chunk), self.baseline_chunks()[1]],
            closed=False,
        )
        extended = run_by_id(process_cycles(envelope(extended_run)), "open-run")
        self.assertEqual([p["phase_id"] for p in extended["phases"]], ["p1", "p2", "p3"])
        self.assertEqual(extended["phases"][0]["watt_hours"], "1/12")
        self.assertEqual(extended["cycles"][0]["phase_ids"], ["p1", "p2", "p3"])
        self.assertEqual(extended["cycles"][0]["round_trip_efficiency"], "1/3")
        self.assertIsNone(extended["pending_cycle"])
        self.assertEqual(extended["continuation"]["next_ordinal"], 2)

    def test_leading_discharge_and_rest_are_orphans(self):
        rows = currents_to_samples([0, -2, 2, -2], prefix="o")
        run = make_run("orphan-run", [chunk("c0", 0, rows)])
        result = run_by_id(process_cycles(envelope(run)), "orphan-run")
        self.assertEqual([p["state"] for p in result["phases"]], ["rest", "discharge", "charge", "discharge"])
        self.assertEqual(result["orphan_phase_ids"], ["p1", "p2"])
        self.assertEqual(result["cycles"][0]["phase_ids"], ["p3", "p4"])

    def test_recharge_before_discharge_closes_previous_candidate(self):
        rows = currents_to_samples([2, 0, 2, -2], prefix="r")
        run = make_run("recharge-run", [chunk("c0", 0, rows)])
        result = run_by_id(process_cycles(envelope(run)), "recharge-run")
        self.assertEqual([p["state"] for p in result["phases"]], ["charge", "rest", "charge", "discharge"])
        self.assertEqual(len(result["cycles"]), 2)
        self.assertEqual(result["cycles"][0]["cycle_id"], "c1")
        self.assertEqual(result["cycles"][0]["phase_ids"], ["p1", "p2"])
        self.assertEqual(result["cycles"][0]["status"], "incomplete")
        self.assertEqual(result["cycles"][0]["reason"], "missing_discharge")
        self.assertEqual(result["cycles"][1]["cycle_id"], "c2")
        self.assertEqual(result["cycles"][1]["phase_ids"], ["p3", "p4"])
        self.assertEqual(result["cycles"][1]["status"], "eligible")

    def test_open_superseded_candidate_is_incomplete_and_new_one_pending(self):
        rows = currents_to_samples([2, 0, 2, 0], prefix="p")
        run = make_run("supersede-open", [chunk("c0", 0, rows)], closed=False)
        result = run_by_id(process_cycles(envelope(run)), "supersede-open")
        self.assertEqual([c["status"] for c in result["cycles"]], ["incomplete"])
        self.assertEqual(result["cycles"][0]["phase_ids"], ["p1", "p2"])
        self.assertEqual(result["pending_cycle"]["cycle_id"], "c2")
        self.assertEqual(result["pending_cycle"]["phase_ids"], ["p3", "p4"])
        self.assertEqual(result["pending_cycle"]["status"], "pending")

    def test_short_charge_and_short_discharge_are_ineligible(self):
        short_charge_rows = [
            sample("a0", 0, 2),
            sample("a1", 1, -2),
            sample("a2", 21, -2),
        ]
        short_charge = run_by_id(
            process_cycles(envelope(make_run("short-charge", [chunk("a", 0, short_charge_rows)]), minimum="15")),
            "short-charge",
        )
        cycle = short_charge["cycles"][0]
        self.assertEqual(cycle["status"], "ineligible")
        self.assertEqual(cycle["reason"], "short_active_phase")
        self.assertEqual(cycle["short_phase_ids"], ["p1"])

        short_discharge_rows = [
            sample("b0", 0, 2),
            sample("b1", 20, -2),
            sample("b2", 21, -2),
        ]
        short_discharge = run_by_id(
            process_cycles(envelope(make_run("short-discharge", [chunk("b", 0, short_discharge_rows)]), minimum="15")),
            "short-discharge",
        )
        cycle = short_discharge["cycles"][0]
        self.assertEqual(cycle["status"], "ineligible")
        self.assertEqual(cycle["reason"], "short_active_phase")
        self.assertEqual(cycle["short_phase_ids"], ["p2"])

    def test_zero_charge_energy_has_null_cycle_efficiency(self):
        rows = [
            sample("z0", 0, 1),
            sample("z1", 10, -1),
            sample("z2", 20, -1),
        ]
        run = run_by_id(
            process_cycles(envelope(make_run("zero-charge", [chunk("z", 0, rows)]), minimum="0")),
            "zero-charge",
        )
        cycle = run["cycles"][0]
        self.assertEqual(cycle["charge_wh"], "0")
        self.assertIsNone(cycle["round_trip_efficiency"])

    def test_conflicting_run_is_atomically_rejected_while_other_run_succeeds(self):
        good = make_run("a-good", self.baseline_chunks())

        rows = self.baseline_samples()
        bad_chunks = [
            chunk("same-id", 0, rows[:3]),
            chunk("same-id", 0, [sample("x0", 0, 2), sample("x1", 10, 2)]),
        ]
        bad = make_run("z-bad", bad_chunks, final_ordinal=0)
        data = envelope(bad, good)
        original = copy.deepcopy(data)
        result = process_cycles(data)
        self.assertEqual([r["run_id"] for r in result["runs"]], ["a-good", "z-bad"])

        rejected = result["runs"][1]
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["state"], "rejected")
        self.assertTrue(rejected["error_code"])
        self.assertEqual(rejected["error"]["code"], rejected["error_code"])
        self.assertTrue(rejected["error"]["message"])
        self.assertEqual(rejected["phases"], [])
        self.assertEqual(rejected["cycles"], [])
        self.assertEqual(rejected["orphan_phase_ids"], [])
        self.assertIsNone(rejected["pending_cycle"])
        self.assertIsNone(rejected["continuation"])

        accepted = result["runs"][0]
        self.assertEqual(accepted["status"], "closed")
        self.assertEqual(accepted["cycles"][0]["round_trip_efficiency"], "1/3")

        # The caller's nested objects are never modified.
        self.assertEqual(data, original)

        # Strict callers can use the same stable validation as an exception.
        with self.assertRaises(ValueError):
            process_cycles(envelope(bad), raise_on_run_error=True)
        with self.assertRaises(ValueError):
            process_run(bad, Fraction(1), Fraction(5))

    def test_run_and_chunk_permutations_have_identical_output(self):
        first = make_run("r1", self.baseline_chunks())
        rows2 = currents_to_samples([2, 0, -2], prefix="q")
        second = make_run("r2", [chunk("q0", 0, rows2)])

        variants = [
            envelope(first, second),
            envelope(second, first),
        ]
        results = [process_cycles(copy.deepcopy(item)) for item in variants]
        self.assertEqual(results[0], results[1])
        self.assertEqual([r["run_id"] for r in results[0]["runs"]], ["r1", "r2"])

        reversed_chunk_order = copy.deepcopy(first)
        reversed_chunk_order["chunks"].reverse()
        self.assertEqual(
            run_by_id(process_cycles(envelope(reversed_chunk_order)), "r1"),
            run_by_id(process_cycles(envelope(first)), "r1"),
        )

    def test_exact_decimal_rational_energy(self):
        rows = [
            sample("d0", "0", "0.1", voltage_v="10.0"),
            sample("d1", "3600", "0.3", voltage_v="10.0"),
            sample("d2", "7200", "-0.1", voltage_v="10.0"),
            sample("d3", "10800", "-0.3", voltage_v="10.0"),
        ]
        run = make_run("decimals", [chunk("d", 0, rows)])
        result = run_by_id(
            process_cycles(envelope(run, active="0.05", minimum="0")),
            "decimals",
        )
        cycle = result["cycles"][0]
        # Charge has two left-charge intervals: 2 Wh plus 1 Wh = 3 Wh.
        self.assertEqual(cycle["charge_wh"], "3")
        # Discharge magnitude: avg -0.2 A * 10 V * 1 h = 2 Wh.
        self.assertEqual(cycle["discharge_wh"], "2")
        self.assertEqual(cycle["round_trip_efficiency"], "2/3")

    def test_top_level_invalid_fields_raise_value_error(self):
        valid = envelope(make_run("r", [chunk("c", 0, currents_to_samples([2, -2]))]))

        unknown = copy.deepcopy(valid)
        unknown["unexpected"] = True
        with self.assertRaises((ValueError, InputError)):
            process_cycles(unknown)

        missing = copy.deepcopy(valid)
        del missing["runs"]
        with self.assertRaises(ValueError):
            process_cycles(missing)

        with self.assertRaises(ValueError):
            process_cycles({"active_current_a": True, "min_phase_seconds": "1", "runs": []})

        too_many = copy.deepcopy(valid)
        too_many["runs"] = [copy.deepcopy(valid["runs"][0]) for _ in range(65)]
        # Give unique IDs while constructing the over-limit envelope.
        for index, item in enumerate(too_many["runs"]):
            item["run_id"] = f"r{index}"
        with self.assertRaises(ValueError):
            process_cycles(too_many)

    def test_run_contract_errors_raise_wholesale_but_are_isolated_in_a_batch(self):
        rows = self.baseline_samples()

        def assert_whole_request_error(bad_run, code_fragment):
            with self.assertRaises(ValueError) as caught:
                process_cycles(envelope(copy.deepcopy(bad_run)))
            self.assertIn(code_fragment, caught.exception.code)

            companion = make_run("zz-companion", self.baseline_chunks())
            batch = process_cycles(envelope(copy.deepcopy(bad_run), companion))
            rejected = run_by_id(batch, bad_run["run_id"])
            self.assertEqual(rejected["status"], "rejected")
            self.assertIn(code_fragment, rejected["error_code"])
            self.assertEqual(run_by_id(batch, "zz-companion")["status"], "closed")

            with self.assertRaises(ValueError):
                process_cycles(envelope(copy.deepcopy(bad_run)), raise_on_run_error=True)

            forced = process_cycles(
                envelope(copy.deepcopy(bad_run)), raise_on_run_error=False
            )["runs"][0]
            self.assertEqual(forced["status"], "rejected")
            self.assertIn(code_fragment, forced["error_code"])

        missing_ordinal = make_run(
            "missing-ordinal",
            [{"chunk_id": "c0", "samples": rows[:2]}],
            final_ordinal=0,
        )
        assert_whole_request_error(missing_ordinal, "ordinal")

        bad_boundary = [
            chunk("c0", 0, rows[:3]),
            chunk("c1", 1, [sample("s2", 20, 1), *rows[3:]]),
        ]
        assert_whole_request_error(make_run("bad-boundary", bad_boundary), "boundary")

        duplicate_id_rows = [
            sample("a", 0, 0),
            sample("b", 10, 0),
            sample("c", 20, 0),
            sample("b", 10, 0),  # identical sample re-appears non-adjacently
        ]
        assert_whole_request_error(
            make_run("dup-id", [chunk("c0", 0, duplicate_id_rows)]),
            "duplicate",
        )

        missing_final = make_run(
            "missing-final",
            self.baseline_chunks(),
            final_ordinal=2,
        )
        assert_whole_request_error(missing_final, "missing_final")

        bool_ordinal = make_run(
            "bool-ordinal",
            [{"chunk_id": "bool", "ordinal": True, "samples": rows[:2]}],
            final_ordinal=1,
        )
        assert_whole_request_error(bool_ordinal, "ordinal")

        # Identity conflicts remain ordinary isolatable rejected runs even in a
        # one-run request.
        same_ordinal = make_run(
            "ordinal-conflict",
            [
                chunk("c0", 0, rows[:2]),
                chunk("c1", 0, [sample("z0", 0, 0), sample("z1", 10, 0)]),
            ],
            final_ordinal=0,
        )
        rejected = process_cycles(envelope(same_ordinal))["runs"][0]
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["error_code"], "ordinal_conflict")

    def test_count_limits_near_boundaries(self):
        runs64 = [
            make_run(f"r{index:02d}", [chunk("c0", 0, currents_to_samples([0], prefix=f"x{index}"))])
            for index in range(64)
        ]
        self.assertEqual(len(process_cycles(envelope(*runs64))["runs"]), 64)

        chunk_5000 = [sample(f"s{i}", i, 0) for i in range(5000)]
        accepted = process_cycles(
            envelope(make_run("size5000", [chunk("big", 0, chunk_5000)]))
        )["runs"][0]
        self.assertEqual(accepted["status"], "closed")

        chunk_5001 = [sample(f"t{i}", i, 0) for i in range(5001)]
        with self.assertRaises(ValueError):
            process_cycles(
                envelope(make_run("size5001", [chunk("big", 0, chunk_5001)]))
            )

    def test_run_sample_limit_100000_boundary(self):
        def run_for(total):
            all_samples = [sample(f"s{i}", i, 0) for i in range(total)]
            chunks = []
            ordinal = 0
            start = 0
            while start < total:
                end = min(start + 5000, total)
                chunks.append(chunk(f"c{ordinal}", ordinal, all_samples[start:end]))
                if end == total:
                    break
                start = end - 1
                ordinal += 1
            return make_run("limit", chunks, final_ordinal=ordinal)

        accepted = run_by_id(process_cycles(envelope(run_for(100000))), "limit")
        self.assertEqual(accepted["status"], "closed", accepted.get("error_message"))

        with self.assertRaises(ValueError):
            process_cycles(envelope(run_for(100001)))


class ExistingProcessRegressionTests(unittest.TestCase):
    def test_process_remains_available_with_baseline_shape(self):
        data = {
            "active_current_a": "1",
            "min_phase_seconds": "5",
            "samples": [
                sample("s0", 0, 2),
                sample("s1", 10, 2),
                sample("s2", 20, 0),
                sample("s3", 30, -1),
                sample("s4", 40, -1),
            ],
        }
        result = process(data)
        self.assertEqual(result["charge_wh"], "1/12")
        self.assertEqual(result["discharge_wh"], "1/36")
        self.assertEqual(result["round_trip_efficiency"], "1/3")


if __name__ == "__main__":
    unittest.main()
