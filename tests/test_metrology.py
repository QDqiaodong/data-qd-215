from __future__ import annotations

import copy
import json
import pathlib
import sys
import tempfile
import unittest
from fractions import Fraction

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from battery_cycles import (  # noqa: E402
    CalibrationError,
    CredentialError,
    CredentialNotFoundError,
    InputError,
    MetrologyInputError,
    MetrologyStore,
    VersionNotFoundError,
    audit_batch,
    build_evidence_index,
    process_cycles,
)


# --------------------------------------------------------------------------- #
# Helpers building real process_cycles inputs.
# --------------------------------------------------------------------------- #


def sample(sid, time_s, current_a, voltage_v="1"):
    return {
        "id": sid,
        "time_s": str(time_s),
        "voltage_v": str(voltage_v),
        "current_a": str(current_a),
    }


def constant_phase_run(run_id, charge_current, discharge_current, *, closed=True, minimum="0"):
    """One charge/rest/discharge cycle built from constant-current intervals.

    The discharge phase has one full 1 h constant-current trapezoid (1 V),
    so its energy in Wh equals the discharge current in A.  The charge phase
    includes the 1 s switch trapezoid into the rest; tests read the actual
    computed energies through ``energies_of`` because the metrology contract
    always treats the warehouse-computed Wh as the nominal value.
    """

    rows = [
        sample("s0", 0, charge_current),
        sample("s1", 3600, charge_current),
        sample("s2", 3601, 0),
        sample("s3", 3602, 0),
        sample("s4", 7202, -discharge_current),
        sample("s5", 10802, -discharge_current),
    ]
    run = {
        "run_id": run_id,
        "closed": closed,
        "chunks": [{"chunk_id": "c0", "ordinal": 0, "samples": rows}],
    }
    if closed:
        run["final_ordinal"] = 0
    else:
        run["final_ordinal"] = None
    return run


def exact_energy_run(run_id, target_charge_wh, target_discharge_wh, *, closed=True):
    """Build a run whose warehouse-computed energies are exactly the targets.

    The 1 s charge->rest switch trapezoid adds ``Ic/7200`` Wh to the full
    1 h charge trapezoid, so the charge current is solved as
    ``Ic = target * 7200/7201``.  The discharge phase is a full constant
    interval after a zero-bounded rest, so its current equals its Wh target.
    """

    charge_current = Fraction(target_charge_wh) * Fraction(7200, 7201)
    rows = [
        sample("s0", 0, charge_current),
        sample("s1", 3600, charge_current),
        sample("s2", 3601, 0),
        sample("s3", 7201, 0),
        sample("s4", 7202, -target_discharge_wh),
        sample("s5", 10802, -target_discharge_wh),
    ]
    run = {
        "run_id": run_id,
        "closed": closed,
        "chunks": [{"chunk_id": "c0", "ordinal": 0, "samples": rows}],
    }
    if closed:
        run["final_ordinal"] = 0
    else:
        run["final_ordinal"] = None
    return run


def process_input(*runs, active="0.5", minimum="0"):
    return {
        "active_current_a": active,
        "min_phase_seconds": minimum,
        "runs": list(runs),
    }


def energies_of(data, run_id, cycle_id="c1"):
    """Return the exact (charge_wh, discharge_wh) the warehouse computes."""
    processed = process_cycles(data, raise_on_run_error=False)
    run = next(item for item in processed["runs"] if item["run_id"] == run_id)
    cycle = next(item for item in run["cycles"] if item["cycle_id"] == cycle_id)
    return Fraction(cycle["charge_wh"]), Fraction(cycle["discharge_wh"])


def spec(sample_id, run_id, cycle_id="c1"):
    return {"sample_id": sample_id, "run_id": run_id, "cycle_id": cycle_id}


def audit_request(batch_id, threshold, charge_ch, discharge_ch, data, *sample_specs):
    return {
        "batch_id": batch_id,
        "threshold": threshold,
        "charge_channel": charge_ch,
        "discharge_channel": discharge_ch,
        "process_input": data,
        "samples": list(sample_specs),
    }


def short_phase_run(run_id):
    rows = [
        sample("s0", 0, 10),
        sample("s1", 10, 0),
        sample("s2", 20, -9),
        sample("s3", 30, -9),
    ]
    return {
        "run_id": run_id,
        "closed": True,
        "final_ordinal": 0,
        "chunks": [{"chunk_id": "c0", "ordinal": 0, "samples": rows}],
    }


def malformed_run(run_id):
    # Ordinal gap makes this run rejected, but it must stay isolated.
    rows = [
        sample("m0", 0, 2),
        sample("m1", 3600, 2),
    ]
    return {
        "run_id": run_id,
        "closed": True,
        "final_ordinal": 2,
        "chunks": [{"chunk_id": "c0", "ordinal": 0, "samples": rows}],
    }


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "metrology.json"

    def tearDown(self):
        self.tmp.cleanup()

    def store(self):
        return MetrologyStore(self.path)


class SharedChannelExactPassTests(StoreTestCase):
    def test_nonzero_width_shared_interval_still_passes_exactly(self):
        data = process_input(exact_energy_run("run-shared", 10, 9))
        nominal_charge, nominal_discharge = energies_of(data, "run-shared")
        self.assertEqual(nominal_charge, 10)
        self.assertEqual(nominal_discharge, 9)
        request = audit_request(
            "batch-shared", "0.9", "ch-shared", "ch-shared", data,
            spec("sp-1", "run-shared"),
        )

        store = self.store()
        self.assertEqual(store.current_version, 0)
        result = store.apply_correction(
            {"ch-shared": {"lo": "-0.1", "hi": "0.1"}}
        )
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["revoked_credentials"], [])

        audit = store.audit(request)
        self.assertEqual(audit["verdict"], "pass")
        self.assertEqual(audit["calibration_version"], 1)
        sample_result = audit["samples"][0]
        # One shared unknown g multiplies the whole margin D - T*C.
        expected_margin = nominal_discharge - Fraction("0.9") * nominal_charge
        self.assertEqual(sample_result["status"], "pass")
        self.assertEqual(Fraction(sample_result["margin_min"]), expected_margin * Fraction("0.9"))
        self.assertEqual(Fraction(sample_result["margin_max"]), expected_margin * Fraction("1.1"))
        self.assertEqual(sample_result["nominal_charge_wh"], str(nominal_charge))
        self.assertEqual(sample_result["nominal_discharge_wh"], str(nominal_discharge))
        self.assertEqual(
            sample_result["calibration_basis"],
            {"ch-shared": {"lo": "-1/10", "hi": "1/10"}},
        )
        json.dumps(audit, allow_nan=False)

        credential = store.issue_credential("cred-shared", request)
        self.assertEqual(credential["status"], "active")
        self.assertEqual(credential["issued_at_version"], 1)
        self.assertTrue(store.check_access("cred-shared")["usable"])

    def test_exactly_ten_and_nine_shared_channel_nontrivial_interval_passes(self):
        # Headline contract case: nominal 10 Wh / 9 Wh, T=0.9, charge and
        # discharge share one channel carrying a real nonzero-width interval.
        # margin = (9 - 0.9*10)*(1+g) = 0 for every g, so it passes exactly.
        data = process_input(exact_energy_run("run-10-9", 10, 9))
        charge, discharge = energies_of(data, "run-10-9")
        self.assertEqual(charge, 10)
        self.assertEqual(discharge, 9)
        request = audit_request(
            "b-exact", "0.9", "m", "m", data, spec("s", "run-10-9")
        )
        result = audit_batch(
            request, {"m": (Fraction("-0.1"), Fraction("0.1"))}
        )
        self.assertEqual(result["verdict"], "pass")
        sample_result = result["samples"][0]
        self.assertEqual(sample_result["status"], "pass")
        self.assertEqual(sample_result["margin_min"], "0")
        self.assertEqual(sample_result["margin_max"], "0")
        self.assertEqual(sample_result["nominal_charge_wh"], "10")
        self.assertEqual(sample_result["nominal_discharge_wh"], "9")


class IndependentChannelCorrectionTests(StoreTestCase):
    def _data(self):
        return process_input(exact_energy_run("run-indep", 10, 9.2))

    def _request(self):
        return audit_request(
            "batch-indep", "0.9", "ch-c", "ch-d", self._data(),
            spec("sp-1", "run-indep"),
        )

    def test_pass_widen_to_uncertain_shrink_without_revival_and_reopen(self):
        data = self._data()
        nominal_charge, nominal_discharge = energies_of(data, "run-indep")
        self.assertEqual(nominal_charge, 10)
        self.assertEqual(nominal_discharge, Fraction("46/5"))
        request = self._request()
        store = self.store()
        store.apply_correction(
            {"ch-c": {"lo": "0", "hi": "0"}, "ch-d": {"lo": "0", "hi": "0"}}
        )
        self.assertEqual(store.audit(request)["verdict"], "pass")
        store.issue_credential("cred-indep", request)

        # First correction: only the discharge channel gets a wider interval.
        widened = store.apply_correction(
            {"ch-d": {"lo": "-0.03", "hi": "0.03"}}
        )
        self.assertEqual(widened["version"], 2)
        # Unmentioned charge channel inherits version 1.
        self.assertEqual(widened["intervals"]["ch-c"], {"lo": "0", "hi": "0"})

        audit_v2 = store.audit(request)
        sample_result = audit_v2["samples"][0]
        threshold = Fraction("0.9")
        # Charge channel still zero-width; only discharge sweeps +/-3%.
        expected_min = nominal_discharge * Fraction("0.97") - threshold * nominal_charge
        expected_max = nominal_discharge * Fraction("1.03") - threshold * nominal_charge
        self.assertEqual(Fraction(sample_result["margin_min"]), expected_min)
        self.assertEqual(Fraction(sample_result["margin_max"]), expected_max)
        self.assertLess(expected_min, 0)
        self.assertGreater(expected_max, 0)
        self.assertEqual(audit_v2["verdict"], "uncertain")

        # The old credential is deliberately not queried between corrections.
        # Second correction shrinks the discharge channel back.
        shrunk = store.apply_correction({"ch-d": {"lo": "0", "hi": "0"}})
        self.assertEqual(shrunk["version"], 3)
        self.assertEqual(store.audit(request)["verdict"], "pass")

        # Reopen local storage: the never-queried failure must not be lost.
        del store
        reopened = MetrologyStore(self.path)
        access = reopened.check_access("cred-indep")
        self.assertFalse(access["usable"])
        self.assertEqual(access["status"], "revoked")
        self.assertEqual(access["first_failed_version"], 2)
        self.assertEqual(access["first_failed_sample"], "sp-1")
        self.assertEqual(access["current_version"], 3)

        # The issuance archive itself never changes.
        archive = reopened.get_credential("cred-indep")
        self.assertEqual(archive["issued_at_version"], 1)
        self.assertEqual(archive["threshold"], "9/10")
        self.assertEqual(archive["samples"][0]["nominal_charge_wh"], "10")
        self.assertEqual(archive["samples"][0]["nominal_discharge_wh"], "46/5")
        self.assertEqual(
            archive["samples"][0]["margin_min"],
            archive["samples"][0]["margin_max"],
        )
        self.assertEqual(
            Fraction(archive["samples"][0]["margin_min"]),
            Fraction("1/5"),
        )
        self.assertEqual(
            archive["samples"][0]["calibration_basis"],
            {
                "ch-c": {"lo": "0", "hi": "0"},
                "ch-d": {"lo": "0", "hi": "0"},
            },
        )

        # Restored intervals cannot revive the old number; a new number works.
        with self.assertRaises(CredentialError):
            reopened.issue_credential("cred-indep", request)
        new_credential = reopened.issue_credential("cred-indep-v3", request)
        self.assertEqual(new_credential["issued_at_version"], 3)
        self.assertTrue(reopened.check_access("cred-indep-v3")["usable"])
        # Both archives remain readable; the revoked one stays revoked.
        self.assertFalse(reopened.check_access("cred-indep")["usable"])

    def test_widened_interval_that_crosses_zero_is_uncertain(self):
        # Equal nominal energies and a +/-50% discharge interval straddle the
        # pass boundary rather than failing outright.
        data = process_input(exact_energy_run("run-straddle", 10, 10))
        request = audit_request(
            "b-straddle", "0.9", "c", "d", data, spec("s", "run-straddle")
        )
        store = self.store()
        store.apply_correction(
            {"c": {"lo": "0", "hi": "0"}, "d": {"lo": "0", "hi": "0"}}
        )
        self.assertEqual(store.audit(request)["verdict"], "pass")
        store.apply_correction({"d": {"lo": "-0.5", "hi": "0.5"}})
        audit = store.audit(request)
        margin_min = Fraction(audit["samples"][0]["margin_min"])
        margin_max = Fraction(audit["samples"][0]["margin_max"])
        # min = 10*0.5 - 9 = -4 ; max = 10*1.5 - 9 = 6
        self.assertEqual(margin_min, -4)
        self.assertEqual(margin_max, 6)
        self.assertEqual(audit["verdict"], "uncertain")

    def test_revocation_survives_shared_channel_batch_staying_active(self):
        store = self.store()
        shared_data = process_input(exact_energy_run("run-shared", 10, 9))
        shared_request = audit_request(
            "batch-shared", "0.9", "ch-shared", "ch-shared",
            shared_data, spec("shared-1", "run-shared"),
        )
        store.apply_correction({"ch-shared": {"lo": "-0.1", "hi": "0.1"}})
        store.issue_credential("cred-shared", shared_request)

        store.apply_correction(
            {"ch-c": {"lo": "0", "hi": "0"}, "ch-d": {"lo": "0", "hi": "0"}}
        )
        store.issue_credential("cred-indep", self._request())
        store.apply_correction({"ch-d": {"lo": "-0.03", "hi": "0.03"}})

        self.assertFalse(store.check_access("cred-indep")["usable"])
        self.assertTrue(store.check_access("cred-shared")["usable"])

        del store
        reopened = MetrologyStore(self.path)
        self.assertTrue(reopened.check_access("cred-shared")["usable"])
        self.assertFalse(reopened.check_access("cred-indep")["usable"])


class StrongAndWeakSampleTests(StoreTestCase):
    def test_strong_sample_cannot_carry_weak_sample(self):
        data = process_input(
            exact_energy_run("run-strong", 10, 9.2),
            exact_energy_run("run-weak", 10, 8),
        )
        request = audit_request(
            "batch-mixed", "0.9", "ch-c", "ch-d", data,
            spec("strong", "run-strong"),
            spec("weak", "run-weak"),
        )
        store = self.store()
        store.apply_correction(
            {"ch-c": {"lo": "0", "hi": "0"}, "ch-d": {"lo": "0", "hi": "0"}}
        )
        audit = store.audit(request)
        statuses = {item["sample_id"]: item["status"] for item in audit["samples"]}
        self.assertEqual(statuses["strong"], "pass")
        self.assertEqual(statuses["weak"], "fail")
        # No averaging: the weak sample stands on its own margin 8 - 9 = -1.
        weak = next(item for item in audit["samples"] if item["sample_id"] == "weak")
        self.assertEqual(weak["margin_min"], "-1")
        self.assertEqual(weak["margin_max"], "-1")
        self.assertEqual(audit["verdict"], "fail")

        with self.assertRaises(CredentialError):
            store.issue_credential("cred-mixed", request)
        self.assertEqual(store.list_credentials(), [])
        self.assertEqual(store.current_version, 1)


class InvalidCorrectionTests(StoreTestCase):
    def test_bad_corrections_leave_archive_untouched(self):
        store = self.store()
        good = {"ch-a": {"lo": "0", "hi": "0"}}
        store.apply_correction(good)

        bad_batches = [
            {},  # empty correction
            {"ch-b": {"lo": "-1", "hi": "0"}},  # lo must be > -1
            {"ch-b": {"lo": "0.2", "hi": "0.1"}},  # lo > hi
            {"ch-b": {"lo": "1e-3", "hi": "0.1"}},  # exponent notation
            {"ch-b": {"lo": "0.01%", "hi": "0.1"}},  # not a decimal
            {"ch-b": {"lo": 0.01, "hi": 0.1}},  # float, not decimal string
            {"ch-b": [0, 0.1]},  # interval not an object
            {"ch-b": {"lo": "0"}},  # missing hi
            {"": {"lo": "0", "hi": "0"}},  # empty channel id
        ]
        for bad in bad_batches:
            with self.assertRaises(MetrologyInputError):
                store.apply_correction(bad)
            self.assertEqual(store.current_version, 1)

        # A partly-illegal batch creates no version at all.
        with self.assertRaises(CalibrationError):
            store.apply_correction(
                {
                    "ch-b": {"lo": "0", "hi": "0.1"},
                    "ch-c": {"lo": "-2", "hi": "0"},
                }
            )
        self.assertEqual(store.current_version, 1)
        self.assertEqual(
            store.list_versions(),
            [{"version": 1, "intervals": {"ch-a": {"lo": "0", "hi": "0"}}}],
        )

        del store
        reopened = MetrologyStore(self.path)
        self.assertEqual(reopened.current_version, 1)


class UnverifiableSampleTests(StoreTestCase):
    def test_missing_evidence_and_unregistered_channels_block_credentials(self):
        good = exact_energy_run("run-good", 10, 9.2)
        open_run = exact_energy_run("run-open", 10, 9.2, closed=False)
        # The strict min duration makes this run's active phases too short.
        short = short_phase_run("run-short")
        data = process_input(good, open_run, short, minimum="15")
        request = audit_request(
            "batch-gaps", "0.9", "ch-c", "ch-d", data,
            spec("ok", "run-good"),
            spec("missing-run", "run-ghost"),
            spec("open", "run-open"),
            spec("short", "run-short"),
            spec("missing-cycle", "run-good", cycle_id="c9"),
        )
        store = self.store()
        store.apply_correction(
            {"ch-c": {"lo": "0", "hi": "0"}, "ch-d": {"lo": "0", "hi": "0"}}
        )
        audit = store.audit(request)
        self.assertEqual(audit["verdict"], "unverifiable")
        by_id = {item["sample_id"]: item for item in audit["samples"]}
        self.assertEqual(by_id["ok"]["status"], "pass")
        self.assertEqual(by_id["missing-run"]["reason"], "run_not_found")
        self.assertEqual(by_id["open"]["reason"], "run_not_closed")
        self.assertEqual(by_id["short"]["reason"], "cycle_ineligible")
        self.assertEqual(by_id["missing-cycle"]["reason"], "cycle_not_found")
        for item in audit["samples"]:
            if item["status"] == "unverifiable":
                self.assertIsNone(item["margin_min"])
                self.assertIsNone(item["calibration_basis"])

        with self.assertRaises(CredentialError):
            store.issue_credential("cred-gaps", request)
        self.assertEqual(store.list_credentials(), [])

        # An unregistered channel is itself an unverifiable condition.
        no_channel_request = audit_request(
            "batch-noch", "0.9", "ch-unknown", "ch-d",
            process_input(constant_phase_run("run-good2", 10, 9.2)),
            spec("x", "run-good2"),
        )
        audit_no_channel = store.audit(no_channel_request)
        self.assertEqual(audit_no_channel["verdict"], "unverifiable")
        self.assertEqual(
            audit_no_channel["samples"][0]["reason"], "channel_not_registered"
        )
        with self.assertRaises(CredentialError):
            store.issue_credential("cred-noch", no_channel_request)

    def test_errors_in_unrelated_runs_do_not_corrupt_good_evidence(self):
        data = process_input(
            constant_phase_run("run-good", 10, 9.2), malformed_run("run-bad")
        )
        request = audit_request(
            "batch-isolated", "0.9", "ch-c", "ch-d", data,
            spec("only-good", "run-good"),
        )
        store = self.store()
        store.apply_correction(
            {"ch-c": {"lo": "0", "hi": "0"}, "ch-d": {"lo": "0", "hi": "0"}}
        )
        self.assertEqual(store.audit(request)["verdict"], "pass")
        credential = store.issue_credential("cred-isolated", request)
        self.assertEqual(credential["status"], "active")


class RequestValidationTests(unittest.TestCase):
    def _base(self):
        return audit_request(
            "b", "0.9", "c", "d",
            process_input(constant_phase_run("r", 10, 9)),
            spec("s", "r"),
        )

    def test_caller_supplied_energy_is_rejected(self):
        request = self._base()
        request["samples"][0]["charge_wh"] = "100"
        with self.assertRaises(MetrologyInputError):
            audit_batch(request, {"c": (Fraction(0), Fraction(0)),
                                  "d": (Fraction(0), Fraction(0))})

    def test_duplicate_sample_ids_and_cycle_reuse_are_rejected(self):
        data = process_input(
            constant_phase_run("r1", 10, 9), constant_phase_run("r2", 10, 9)
        )
        duplicated = audit_request(
            "b", "0.9", "c", "d", data,
            spec("same-id", "r1"), spec("same-id", "r2"),
        )
        with self.assertRaises(MetrologyInputError):
            audit_batch(duplicated, {})
        reused_cycle = audit_request(
            "b", "0.9", "c", "d", data,
            spec("s1", "r1"), spec("s2", "r1"),
        )
        with self.assertRaises(MetrologyInputError):
            audit_batch(reused_cycle, {})

    def test_threshold_and_structure_rules(self):
        with self.assertRaises(MetrologyInputError):
            audit_batch(
                audit_request(
                    "b", "0", "c", "d",
                    process_input(constant_phase_run("r", 10, 9)), spec("s", "r")
                ),
                {},
            )
        with self.assertRaises(MetrologyInputError):
            audit_batch(
                audit_request(
                    "b", 0.9, "c", "d",
                    process_input(constant_phase_run("r", 10, 9)), spec("s", "r")
                ),
                {},
            )
        with self.assertRaises(MetrologyInputError):
            audit_batch(
                audit_request(
                    "b", "0.9", "c", "d",
                    process_input(constant_phase_run("r", 10, 9)),
                ),
                {},
            )
        unknown = self._base()
        unknown["extra"] = 1
        with self.assertRaises(MetrologyInputError):
            audit_batch(unknown, {})

    def test_process_input_is_not_mutated(self):
        data = process_input(constant_phase_run("r", 10, 9))
        original = copy.deepcopy(data)
        audit_batch(
            audit_request("b", "0.9", "c", "d", data, spec("s", "r")),
            {"c": (Fraction(0), Fraction(0)), "d": (Fraction(0), Fraction(0))},
        )
        self.assertEqual(data, original)

    def test_evidence_index_exposes_only_warehouse_energies(self):
        data = process_input(
            exact_energy_run("r1", 10, 9), malformed_run("r-bad")
        )
        index = build_evidence_index(data)
        self.assertIn(("r1", "c1"), index["cycles"])
        self.assertEqual(index["runs"]["r-bad"]["status"], "rejected")
        self.assertNotIn(("r-bad", "c1"), index["cycles"])

    def test_rejected_run_is_isolated_not_bad_evidence_for_others(self):
        data = process_input(
            exact_energy_run("good", 10, 9), malformed_run("bad-run")
        )
        request = audit_request(
            "b", "0.9", "c", "d", data,
            spec("g", "good"), spec("b", "bad-run"),
        )
        result = audit_batch(
            request,
            {"c": (Fraction(0), Fraction(0)), "d": (Fraction(0), Fraction(0))},
        )
        by_id = {item["sample_id"]: item for item in result["samples"]}
        self.assertEqual(by_id["g"]["status"], "pass")
        self.assertEqual(by_id["b"]["status"], "unverifiable")
        self.assertEqual(by_id["b"]["reason"], "run_not_closed")
        self.assertEqual(result["verdict"], "unverifiable")

    def test_malformed_process_input_raises_contract_input_error(self):
        request = audit_request(
            "b", "0.9", "c", "d", {"not": "valid"}, spec("x", "r")
        )
        with self.assertRaises(InputError):
            audit_batch(request, {})


class CredentialLifecycleTests(StoreTestCase):
    def _request(self):
        return audit_request(
            "b1", "0.9", "c", "d",
            process_input(constant_phase_run("r", 10, 9.2)),
            spec("s", "r"),
        )

    def test_duplicate_id_and_single_active_per_batch(self):
        store = self.store()
        store.apply_correction(
            {"c": {"lo": "0", "hi": "0"}, "d": {"lo": "0", "hi": "0"}}
        )
        store.issue_credential("cred-1", self._request())
        with self.assertRaises(CredentialError):
            store.issue_credential("cred-1", self._request())
        with self.assertRaises(CredentialError):
            store.issue_credential("cred-2", self._request())
        # Failed issues leave no partial credentials.
        self.assertEqual([c["credential_id"] for c in store.list_credentials()],
                         ["cred-1"])

        # A new batch can still get its own credential.
        other = audit_request(
            "b2", "0.9", "c", "d",
            process_input(constant_phase_run("r2", 10, 9.2)),
            spec("s", "r2"),
        )
        store.issue_credential("cred-other", other)
        self.assertEqual(len(store.list_credentials()), 2)

        # After revocation the same batch may be re-issued with a new number.
        store.apply_correction({"d": {"lo": "-0.5", "hi": "0.5"}})
        store.apply_correction({"d": {"lo": "0", "hi": "0"}})
        self.assertFalse(store.check_access("cred-1")["usable"])
        store.issue_credential("cred-3", self._request())
        self.assertTrue(store.check_access("cred-3")["usable"])

    def test_unknown_credential_and_version_lookups(self):
        store = self.store()
        with self.assertRaises(CredentialNotFoundError):
            store.check_access("never-issued")
        with self.assertRaises(CredentialNotFoundError):
            store.get_credential("never-issued")
        with self.assertRaises(VersionNotFoundError):
            store.get_version(1)
        store.apply_correction({"c": {"lo": "0", "hi": "0"}})
        with self.assertRaises(VersionNotFoundError):
            store.get_version(7)
        with self.assertRaises(VersionNotFoundError):
            store.audit(self._request(), version=2)

    def test_historical_version_audit_uses_old_intervals(self):
        store = self.store()
        store.apply_correction(
            {"c": {"lo": "0", "hi": "0"}, "d": {"lo": "0", "hi": "0"}}
        )
        request = self._request()
        self.assertEqual(store.audit(request)["verdict"], "pass")
        store.apply_correction({"d": {"lo": "-0.5", "hi": "0.5"}})
        self.assertEqual(store.audit(request)["verdict"], "uncertain")
        old = store.audit(request, version=1)
        self.assertEqual(old["calibration_version"], 1)
        self.assertEqual(old["verdict"], "pass")

    def test_unrelated_channel_correction_keeps_credential_active(self):
        store = self.store()
        store.apply_correction(
            {"c": {"lo": "0", "hi": "0"}, "d": {"lo": "0", "hi": "0"}}
        )
        store.issue_credential("cred-1", self._request())
        store.apply_correction({"other": {"lo": "-0.5", "hi": "0.5"}})
        self.assertTrue(store.check_access("cred-1")["usable"])
        self.assertIsNone(store.get_credential("cred-1")["first_failed_version"])

    def test_persistence_payload_is_json_and_reopens(self):
        store = self.store()
        store.apply_correction(
            {"c": {"lo": "0", "hi": "0"}, "d": {"lo": "0", "hi": "0"}}
        )
        store.issue_credential("cred-1", self._request())
        store.apply_correction(
            {"c": {"lo": "-0.01", "hi": "0.01"},
             "d": {"lo": "-0.02", "hi": "0.02"}}
        )
        store.apply_correction({"d": {"lo": "-0.5", "hi": "0.5"}})
        raw = json.loads(pathlib.Path(self.path).read_text(encoding="utf-8"))
        self.assertEqual(raw["format"], "battery-cycles-metrology-1")
        self.assertEqual(len(raw["versions"]), 3)

        reopened = MetrologyStore(self.path)
        self.assertEqual(reopened.current_version, 3)
        access = reopened.check_access("cred-1")
        self.assertFalse(access["usable"])
        # First failure is recorded at the first version that broke it.
        self.assertIn(access["first_failed_version"], (2, 3))
        self.assertEqual(access["first_failed_sample"], "s")
        history = reopened.get_credential("cred-1")
        self.assertEqual(history["status"], "revoked")
        # Immutable issuance snapshot still carries the version-1 margins.
        self.assertEqual(history["issued_at_version"], 1)
        self.assertEqual(
            history["samples"][0]["calibration_basis"]["d"],
            {"lo": "0", "hi": "0"},
        )

    def test_closed_store_rejects_operations(self):
        store = self.store()
        store.close()
        with self.assertRaises(MetrologyInputError):
            store.current_version
        with self.assertRaises(MetrologyInputError):
            store.apply_correction({"c": {"lo": "0", "hi": "0"}})

    def test_context_manager_closes_store(self):
        with MetrologyStore(self.path) as store:
            store.apply_correction({"c": {"lo": "0", "hi": "0"}})
        with self.assertRaises(MetrologyInputError):
            store.current_version

    def test_first_failing_sample_is_stable_across_later_corrections(self):
        data = process_input(
            exact_energy_run("r1", 10, 10),
            exact_energy_run("r2", 10, 9.5),
        )
        request = audit_request(
            "b-multi", "0.9", "c", "d", data,
            spec("alpha", "r1"), spec("beta", "r2"),
        )
        store = self.store()
        store.apply_correction(
            {"c": {"lo": "0", "hi": "0"}, "d": {"lo": "0", "hi": "0"}}
        )
        store.issue_credential("cred-multi", request)
        # Zero-width margins: alpha = 1, beta = 0.5. An 8% negative discharge
        # error breaks beta while alpha still passes.
        store.apply_correction({"d": {"lo": "-0.08", "hi": "0"}})
        access = store.check_access("cred-multi")
        self.assertFalse(access["usable"])
        self.assertEqual(access["first_failed_version"], 2)
        self.assertEqual(access["first_failed_sample"], "beta")
        # A later, larger correction must not overwrite the first failure.
        store.apply_correction({"d": {"lo": "-0.9", "hi": "0.9"}})
        again = store.check_access("cred-multi")
        self.assertEqual(again["first_failed_version"], 2)
        self.assertEqual(again["first_failed_sample"], "beta")

    def test_returned_archives_are_copies_not_live_state(self):
        store = self.store()
        store.apply_correction(
            {"c": {"lo": "0", "hi": "0"}, "d": {"lo": "0", "hi": "0"}}
        )
        request = audit_request(
            "b1", "0.9", "c", "d",
            process_input(exact_energy_run("r", 10, 9.2)),
            spec("s", "r"),
        )
        archive = store.issue_credential("cred-1", request)
        archive["status"] = "tampered"
        archive["samples"][0]["margin_min"] = "999"
        # The stored record is unaffected by caller mutation of the snapshot.
        fresh = store.get_credential("cred-1")
        self.assertEqual(fresh["status"], "active")
        self.assertNotEqual(fresh["samples"][0]["margin_min"], "999")
        versions = store.list_versions()
        versions[0]["intervals"]["c"] = {"lo": "-9", "hi": "9"}
        self.assertEqual(
            store.get_version(1)["intervals"]["c"], {"lo": "0", "hi": "0"}
        )


class ExactBoundTests(unittest.TestCase):
    def test_zero_width_deterministic_fail(self):
        data = process_input(constant_phase_run("r", 10, 8))
        charge, discharge = energies_of(data, "r")
        request = audit_request(
            "b", "0.9", "c", "d", data, spec("s", "r")
        )
        result = audit_batch(
            request,
            {"c": (Fraction(0), Fraction(0)), "d": (Fraction(0), Fraction(0))},
        )
        margin = discharge - Fraction("0.9") * charge
        if margin < 0:
            self.assertEqual(result["verdict"], "fail")
            self.assertEqual(result["samples"][0]["status"], "fail")
            self.assertEqual(Fraction(result["samples"][0]["margin_min"]), margin)
            self.assertEqual(Fraction(result["samples"][0]["margin_max"]), margin)

    def test_independent_interval_endpoints(self):
        data = process_input(constant_phase_run("r", 10, 10))
        charge, discharge = energies_of(data, "r")
        request = audit_request(
            "b", "0.9", "c", "d", data, spec("s", "r")
        )
        result = audit_batch(
            request,
            {
                "c": (Fraction("-0.1"), Fraction("0.1")),
                "d": (Fraction("-0.1"), Fraction("0.1")),
            },
        )
        sample_result = result["samples"][0]
        expected_min = discharge * Fraction("0.9") - Fraction("0.9") * charge * Fraction("1.1")
        expected_max = discharge * Fraction("1.1") - Fraction("0.9") * charge * Fraction("0.9")
        self.assertEqual(Fraction(sample_result["margin_min"]), expected_min)
        self.assertEqual(Fraction(sample_result["margin_max"]), expected_max)

    def test_shared_channel_margin_is_scaled(self):
        data = process_input(exact_energy_run("r", 10, 9))
        charge, discharge = energies_of(data, "r")
        self.assertEqual((charge, discharge), (Fraction(10), Fraction(9)))
        request = audit_request(
            "b", "0.9", "m", "m", data, spec("s", "r")
        )
        result = audit_batch(
            request, {"m": (Fraction("-0.25"), Fraction("0.25"))}
        )
        # margin is identically zero for the 10/9/T=0.9 case under any g.
        sample_result = result["samples"][0]
        self.assertEqual(sample_result["margin_min"], "0")
        self.assertEqual(sample_result["margin_max"], "0")

    def test_shared_channel_nonzero_margin_scales_with_gain(self):
        data = process_input(exact_energy_run("r", 10, 10))
        charge, discharge = energies_of(data, "r")
        request = audit_request(
            "b", "0.9", "m", "m", data, spec("s", "r")
        )
        result = audit_batch(
            request, {"m": (Fraction("-0.25"), Fraction("0.25"))}
        )
        margin = discharge - Fraction("0.9") * charge
        sample_result = result["samples"][0]
        self.assertEqual(Fraction(sample_result["margin_min"]), margin * Fraction("0.75"))
        self.assertEqual(Fraction(sample_result["margin_max"]), margin * Fraction("1.25"))


if __name__ == "__main__":
    unittest.main()
