"""Offline metrological audit and access-credential API.

This module never re-derives energy from caller-supplied numbers.  Every
measurement evidence is located in the output of :func:`process_cycles` for
the exact ``process_cycles`` input handed in with the audit request.  All
thresholds, calibration gain-error intervals and reported bounds use exact
:class:`~fractions.Fraction` arithmetic; new numeric inputs are finite
decimal strings.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from fractions import Fraction
from typing import Any

from .cycles import InputError, process_cycles

STATE_FORMAT = "battery-cycles-metrology-1"

# Finite decimal notation only: optional sign, digits and at most one dot.
# Exponents, fractions, NaN and infinity are rejected.
_DECIMAL_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")

AUDIT_FIELDS = frozenset(
    {
        "batch_id",
        "threshold",
        "charge_channel",
        "discharge_channel",
        "process_input",
        "samples",
    }
)
SAMPLE_FIELDS = frozenset({"sample_id", "run_id", "cycle_id"})
INTERVAL_FIELDS = frozenset({"lo", "hi"})

# Stable reason codes for samples that cannot be judged at all.
REASON_RUN_NOT_FOUND = "run_not_found"
REASON_RUN_NOT_CLOSED = "run_not_closed"
REASON_CYCLE_NOT_FOUND = "cycle_not_found"
REASON_CYCLE_INELIGIBLE = "cycle_ineligible"
REASON_NON_POSITIVE_ENERGY = "non_positive_energy"
REASON_CHANNEL_NOT_REGISTERED = "channel_not_registered"


class MetrologyInputError(InputError):
    """An audit/calibration/credential request violates the API contract."""

    code = "invalid_metrology_input"


class CalibrationError(MetrologyInputError):
    """A calibration correction batch is not wholly legal."""

    code = "invalid_calibration"


class CredentialError(MetrologyInputError):
    """A credential cannot be issued or reused."""

    code = "credential_rejected"


class CredentialNotFoundError(MetrologyInputError):
    """The referenced credential id was never issued by this store."""

    code = "credential_not_found"


class VersionNotFoundError(MetrologyInputError):
    """The referenced calibration version does not exist."""

    code = "version_not_found"


def _text(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def _decimal(value: object, name: str) -> Fraction:
    if not isinstance(value, str) or not value or len(value) > 32:
        raise MetrologyInputError(f"{name} must be a finite decimal string")
    if not _DECIMAL_RE.match(value):
        raise MetrologyInputError(f"{name} must be a finite decimal string")
    return Fraction(value)


def _nonempty_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise MetrologyInputError(f"{name} must be a nonempty string")
    return value


def _margin_bounds(
    nominal_charge: Fraction,
    nominal_discharge: Fraction,
    threshold: Fraction,
    intervals: dict[str, tuple[Fraction, Fraction]],
    charge_channel: str,
    discharge_channel: str,
) -> tuple[Fraction, Fraction]:
    """Exact min/max of actual discharge Wh - T * actual charge Wh.

    A channel carries one unknown gain error ``g`` shared by every energy it
    meters.  Coefficients of the same channel are combined first, so a
    channel used for both charge and discharge is handled as one unknown.
    Each independent unknown is swept over its closed interval.
    """

    base = nominal_discharge - threshold * nominal_charge
    coefficients: dict[str, Fraction] = {}
    coefficients[discharge_channel] = (
        coefficients.get(discharge_channel, Fraction(0)) + nominal_discharge
    )
    coefficients[charge_channel] = (
        coefficients.get(charge_channel, Fraction(0)) - threshold * nominal_charge
    )

    lower = base
    upper = base
    for channel, coefficient in coefficients.items():
        lo, hi = intervals[channel]
        lower += min(coefficient * lo, coefficient * hi)
        upper += max(coefficient * lo, coefficient * hi)
    return lower, upper


def _verdict(lower: Fraction, upper: Fraction) -> str:
    if lower >= 0:
        return "pass"
    if upper < 0:
        return "fail"
    return "uncertain"


def build_evidence_index(process_input: dict[str, Any]) -> dict[str, Any]:
    """Run ``process_cycles`` in isolated mode and index its cycle evidence.

    Unrelated rejected or open runs stay in the index as non-closed runs;
    they never corrupt evidence located in other runs.
    """

    processed = process_cycles(process_input, raise_on_run_error=False)
    runs: dict[str, dict[str, Any]] = {}
    cycles: dict[tuple[str, str], dict[str, Any]] = {}
    for run in processed["runs"]:
        run_id = run["run_id"]
        runs[run_id] = {"status": run["status"]}
        for cycle in run["cycles"]:
            cycles[(run_id, cycle["cycle_id"])] = {
                "status": cycle["status"],
                "charge_wh": Fraction(cycle["charge_wh"]),
                "discharge_wh": Fraction(cycle["discharge_wh"]),
            }
    return {"runs": runs, "cycles": cycles}


def _resolve_evidence(
    index: dict[str, Any], run_id: str, cycle_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    run = index["runs"].get(run_id)
    if run is None:
        return None, REASON_RUN_NOT_FOUND
    if run["status"] != "closed":
        return None, REASON_RUN_NOT_CLOSED
    cycle = index["cycles"].get((run_id, cycle_id))
    if cycle is None:
        return None, REASON_CYCLE_NOT_FOUND
    if cycle["status"] != "eligible":
        return None, REASON_CYCLE_INELIGIBLE
    if cycle["charge_wh"] <= 0 or cycle["discharge_wh"] <= 0:
        return None, REASON_NON_POSITIVE_ENERGY
    return cycle, None


def _validate_request(request: object) -> tuple[str, Fraction, str, str, dict[str, Any], list[dict[str, str]]]:
    if not isinstance(request, dict):
        raise MetrologyInputError("audit request must be an object")
    unknown = sorted(set(request) - AUDIT_FIELDS)
    missing = sorted(AUDIT_FIELDS - set(request))
    if unknown:
        raise MetrologyInputError(f"audit request has unknown field(s): {', '.join(unknown)}")
    if missing:
        raise MetrologyInputError(f"audit request is missing field(s): {', '.join(missing)}")

    batch_id = _nonempty_str(request["batch_id"], "batch_id")
    threshold = _decimal(request["threshold"], "threshold")
    if threshold <= 0:
        raise MetrologyInputError("threshold must be positive")
    charge_channel = _nonempty_str(request["charge_channel"], "charge_channel")
    discharge_channel = _nonempty_str(request["discharge_channel"], "discharge_channel")

    process_input = request["process_input"]
    if not isinstance(process_input, dict):
        raise MetrologyInputError("process_input must be a process_cycles input object")

    raw_samples = request["samples"]
    if not isinstance(raw_samples, list) or not raw_samples:
        raise MetrologyInputError("samples must be a nonempty list")

    samples: list[dict[str, str]] = []
    seen_sample_ids: set[str] = set()
    seen_refs: set[tuple[str, str]] = set()
    for position, raw in enumerate(raw_samples):
        if not isinstance(raw, dict):
            raise MetrologyInputError(f"samples[{position}] must be an object")
        sample_unknown = sorted(set(raw) - SAMPLE_FIELDS)
        sample_missing = sorted(SAMPLE_FIELDS - set(raw))
        if sample_unknown:
            raise MetrologyInputError(
                f"samples[{position}] has unknown field(s): {', '.join(sample_unknown)}"
            )
        if sample_missing:
            raise MetrologyInputError(
                f"samples[{position}] is missing field(s): {', '.join(sample_missing)}"
            )
        sample_id = _nonempty_str(raw["sample_id"], f"samples[{position}].sample_id")
        run_id = _nonempty_str(raw["run_id"], f"samples[{position}].run_id")
        cycle_id = _nonempty_str(raw["cycle_id"], f"samples[{position}].cycle_id")
        if sample_id in seen_sample_ids:
            raise MetrologyInputError(f"sample_id {sample_id} is not unique in this audit")
        ref = (run_id, cycle_id)
        if ref in seen_refs:
            raise MetrologyInputError(
                f"samples[{position}] reuses cycle {run_id}/{cycle_id} as another sample"
            )
        seen_sample_ids.add(sample_id)
        seen_refs.add(ref)
        samples.append(
            {"sample_id": sample_id, "run_id": run_id, "cycle_id": cycle_id}
        )

    return batch_id, threshold, charge_channel, discharge_channel, process_input, samples


def _calibration_basis(
    intervals: dict[str, tuple[Fraction, Fraction]],
    charge_channel: str,
    discharge_channel: str,
) -> dict[str, dict[str, str]]:
    basis: dict[str, dict[str, str]] = {}
    for channel in (charge_channel, discharge_channel):
        if channel not in basis:
            lo, hi = intervals[channel]
            basis[channel] = {"lo": _text(lo), "hi": _text(hi)}
    return basis


def audit_batch(
    request: dict[str, Any],
    intervals: dict[str, tuple[Fraction, Fraction]],
) -> dict[str, Any]:
    """Audit one batch request against one calibration version.

    ``intervals`` maps every registered channel id to its closed gain-error
    interval.  Samples with no locatable eligible evidence or with an
    unregistered channel are reported as ``unverifiable``; they are never
    silently skipped.  The result is JSON-safe: every rational is an exact
    fraction/decimal string.
    """

    (
        batch_id,
        threshold,
        charge_channel,
        discharge_channel,
        process_input,
        samples,
    ) = _validate_request(request)

    index = build_evidence_index(process_input)
    used_channels = {charge_channel, discharge_channel}
    channels_registered = all(channel in intervals for channel in used_channels)

    results: list[dict[str, Any]] = []
    for sample in samples:
        common = {
            "sample_id": sample["sample_id"],
            "run_id": sample["run_id"],
            "cycle_id": sample["cycle_id"],
        }
        evidence, reason = _resolve_evidence(
            index, sample["run_id"], sample["cycle_id"]
        )
        if evidence is None:
            results.append(
                {
                    **common,
                    "status": "unverifiable",
                    "reason": reason,
                    "nominal_charge_wh": None,
                    "nominal_discharge_wh": None,
                    "margin_min": None,
                    "margin_max": None,
                    "calibration_basis": None,
                }
            )
            continue
        if not channels_registered:
            missing = sorted(
                channel for channel in used_channels if channel not in intervals
            )
            results.append(
                {
                    **common,
                    "status": "unverifiable",
                    "reason": REASON_CHANNEL_NOT_REGISTERED,
                    "reason_detail": {"channels": missing},
                    "nominal_charge_wh": None,
                    "nominal_discharge_wh": None,
                    "margin_min": None,
                    "margin_max": None,
                    "calibration_basis": None,
                }
            )
            continue

        nominal_charge = evidence["charge_wh"]
        nominal_discharge = evidence["discharge_wh"]
        lower, upper = _margin_bounds(
            nominal_charge,
            nominal_discharge,
            threshold,
            intervals,
            charge_channel,
            discharge_channel,
        )
        results.append(
            {
                **common,
                "status": _verdict(lower, upper),
                "reason": None,
                "reason_detail": None,
                "nominal_charge_wh": _text(nominal_charge),
                "nominal_discharge_wh": _text(nominal_discharge),
                "margin_min": _text(lower),
                "margin_max": _text(upper),
                "calibration_basis": _calibration_basis(
                    intervals, charge_channel, discharge_channel
                ),
            }
        )

    statuses = [item["status"] for item in results]
    if "unverifiable" in statuses:
        verdict = "unverifiable"
    elif "fail" in statuses:
        verdict = "fail"
    elif "uncertain" in statuses:
        verdict = "uncertain"
    else:
        verdict = "pass"

    return {
        "batch_id": batch_id,
        "threshold": _text(threshold),
        "charge_channel": charge_channel,
        "discharge_channel": discharge_channel,
        "samples": results,
        "verdict": verdict,
    }


def _parse_correction(correction: object) -> dict[str, tuple[Fraction, Fraction]]:
    if not isinstance(correction, dict) or not correction:
        raise CalibrationError("a correction must be a nonempty channel mapping")
    parsed: dict[str, tuple[Fraction, Fraction]] = {}
    for channel, raw_interval in correction.items():
        if not isinstance(channel, str) or not channel:
            raise CalibrationError("calibration channel ids must be nonempty strings")
        if not isinstance(raw_interval, dict):
            raise CalibrationError(f"channel {channel}: interval must be an object")
        unknown = sorted(set(raw_interval) - INTERVAL_FIELDS)
        missing = sorted(INTERVAL_FIELDS - set(raw_interval))
        if unknown:
            raise CalibrationError(f"channel {channel}: unknown interval field(s): {', '.join(unknown)}")
        if missing:
            raise CalibrationError(f"channel {channel}: missing interval field(s): {', '.join(missing)}")
        lo = _decimal(raw_interval["lo"], f"channel {channel}.lo")
        hi = _decimal(raw_interval["hi"], f"channel {channel}.hi")
        if lo <= -1:
            raise CalibrationError(f"channel {channel}: lo must be strictly greater than -1")
        if lo > hi:
            raise CalibrationError(f"channel {channel}: lo must be <= hi")
        parsed[channel] = (lo, hi)
    return parsed


def _dump_intervals(intervals: dict[str, tuple[Fraction, Fraction]]) -> dict[str, dict[str, str]]:
    return {
        channel: {"lo": _text(lo), "hi": _text(hi)}
        for channel, (lo, hi) in sorted(intervals.items())
    }


def _load_intervals(payload: dict[str, Any]) -> dict[str, tuple[Fraction, Fraction]]:
    return {
        channel: (Fraction(entry["lo"]), Fraction(entry["hi"]))
        for channel, entry in payload.items()
    }


class MetrologyStore:
    """Persistent local archive of calibration versions and credentials.

    The whole state lives in one JSON file written atomically (temp file +
    ``os.replace``) on every mutation, so a correction and the credential
    revocations it causes become visible together.  Reopening the path never
    re-evaluates lazily: every revocation is decided and persisted when the
    correction is applied.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = os.fspath(path)
        self._closed = False
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if os.path.exists(self._path):
            self._load()
        else:
            self._versions: list[dict[str, Any]] = []
            self._intervals: dict[str, tuple[Fraction, Fraction]] = {}
            self._credentials: dict[str, dict[str, Any]] = {}
            self._save()

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        with open(self._path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict) or payload.get("format") != STATE_FORMAT:
            raise MetrologyInputError("unknown metrology store format")
        try:
            self._versions = []
            for version_entry in payload["versions"]:
                self._versions.append(
                    {
                        "version": version_entry["version"],
                        "intervals": _load_intervals(version_entry["intervals"]),
                    }
                )
            self._credentials = {
                credential_id: self._credential_from_json(entry)
                for credential_id, entry in payload["credentials"].items()
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise MetrologyInputError("metrology store payload is corrupt") from exc
        if self._versions:
            self._intervals = dict(self._versions[-1]["intervals"])
        else:
            self._intervals = {}

    def _credential_from_json(self, entry: dict[str, Any]) -> dict[str, Any]:
        samples = []
        for sample in entry["samples"]:
            samples.append(
                {
                    "sample_id": sample["sample_id"],
                    "run_id": sample["run_id"],
                    "cycle_id": sample["cycle_id"],
                    "nominal_charge_wh": Fraction(sample["nominal_charge_wh"]),
                    "nominal_discharge_wh": Fraction(sample["nominal_discharge_wh"]),
                    "margin_min": Fraction(sample["margin_min"]),
                    "margin_max": Fraction(sample["margin_max"]),
                    "calibration_basis": sample["calibration_basis"],
                }
            )
        return {
            "credential_id": entry["credential_id"],
            "batch_id": entry["batch_id"],
            "issued_at_version": entry["issued_at_version"],
            "threshold": Fraction(entry["threshold"]),
            "charge_channel": entry["charge_channel"],
            "discharge_channel": entry["discharge_channel"],
            "samples": samples,
            "status": entry["status"],
            "first_failed_version": entry["first_failed_version"],
            "first_failed_sample": entry["first_failed_sample"],
        }

    def _credential_to_json(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "credential_id": entry["credential_id"],
            "batch_id": entry["batch_id"],
            "issued_at_version": entry["issued_at_version"],
            "threshold": _text(entry["threshold"]),
            "charge_channel": entry["charge_channel"],
            "discharge_channel": entry["discharge_channel"],
            "samples": [
                {
                    "sample_id": sample["sample_id"],
                    "run_id": sample["run_id"],
                    "cycle_id": sample["cycle_id"],
                    "nominal_charge_wh": _text(sample["nominal_charge_wh"]),
                    "nominal_discharge_wh": _text(sample["nominal_discharge_wh"]),
                    "margin_min": _text(sample["margin_min"]),
                    "margin_max": _text(sample["margin_max"]),
                    "calibration_basis": sample["calibration_basis"],
                }
                for sample in entry["samples"]
            ],
            "status": entry["status"],
            "first_failed_version": entry["first_failed_version"],
            "first_failed_sample": entry["first_failed_sample"],
        }

    def _save(self) -> None:
        payload = {
            "format": STATE_FORMAT,
            "versions": [
                {"version": entry["version"], "intervals": _dump_intervals(entry["intervals"])}
                for entry in self._versions
            ],
            "credentials": {
                credential_id: self._credential_to_json(entry)
                for credential_id, entry in sorted(self._credentials.items())
            },
        }
        directory = os.path.dirname(os.path.abspath(self._path))
        fd, tmp_name = tempfile.mkstemp(prefix=".metrology-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self._path)
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    def _ensure_open(self) -> None:
        if self._closed:
            raise MetrologyInputError("metrology store is closed")

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "MetrologyStore":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # calibration versions
    # ------------------------------------------------------------------ #

    @property
    def current_version(self) -> int:
        """Version number of the active calibration; 0 for the empty archive."""
        self._ensure_open()
        return len(self._versions)

    def apply_correction(
        self, correction: dict[str, dict[str, str]]
    ) -> dict[str, Any]:
        """Register or replace a batch of channel intervals as one new version.

        The whole batch is validated before any state changes.  Unmentioned
        channels inherit their previous intervals.  Every active credential
        is re-judged against the new full version in the same atomic write.
        """

        self._ensure_open()
        parsed = _parse_correction(correction)
        next_intervals = dict(self._intervals)
        next_intervals.update(parsed)
        next_number = len(self._versions) + 1

        revocations: list[dict[str, Any]] = []
        for entry in self._credentials.values():
            if entry["status"] != "active":
                continue
            failed_sample = self._first_failing_sample(entry, next_intervals)
            if failed_sample is not None:
                entry["status"] = "revoked"
                entry["first_failed_version"] = next_number
                entry["first_failed_sample"] = failed_sample
                revocations.append(
                    {
                        "credential_id": entry["credential_id"],
                        "first_failed_sample": failed_sample,
                    }
                )

        self._versions.append({"version": next_number, "intervals": next_intervals})
        self._intervals = next_intervals
        self._save()
        return {
            "version": next_number,
            "intervals": _dump_intervals(next_intervals),
            "revoked_credentials": revocations,
        }

    def _first_failing_sample(
        self,
        credential: dict[str, Any],
        intervals: dict[str, tuple[Fraction, Fraction]],
    ) -> str | None:
        for sample in credential["samples"]:
            lower, _upper = _margin_bounds(
                sample["nominal_charge_wh"],
                sample["nominal_discharge_wh"],
                credential["threshold"],
                intervals,
                credential["charge_channel"],
                credential["discharge_channel"],
            )
            if lower < 0:
                return sample["sample_id"]
        return None

    def list_versions(self) -> list[dict[str, Any]]:
        self._ensure_open()
        return [
            {"version": entry["version"], "intervals": _dump_intervals(entry["intervals"])}
            for entry in self._versions
        ]

    def get_version(self, version: int) -> dict[str, Any]:
        self._ensure_open()
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise VersionNotFoundError("version must be a positive integer")
        if version > len(self._versions):
            raise VersionNotFoundError(f"calibration version {version} does not exist")
        entry = self._versions[version - 1]
        return {"version": entry["version"], "intervals": _dump_intervals(entry["intervals"])}

    def _version_intervals(
        self, version: int | None
    ) -> tuple[int, dict[str, tuple[Fraction, Fraction]]]:
        if version is None:
            return len(self._versions), dict(self._intervals)
        self.get_version(version)
        return version, dict(self._versions[version - 1]["intervals"])

    # ------------------------------------------------------------------ #
    # audits and credentials
    # ------------------------------------------------------------------ #

    def audit(
        self, request: dict[str, Any], *, version: int | None = None
    ) -> dict[str, Any]:
        """Audit a request against the current (or a named) calibration."""
        self._ensure_open()
        version_number, intervals = self._version_intervals(version)
        result = audit_batch(request, intervals)
        result["calibration_version"] = version_number
        return result

    def issue_credential(
        self, credential_id: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        """Issue a credential only when every sample passes right now.

        Nothing is persisted until all checks succeed, so a duplicate id, an
        active credential for the same batch or a failing audit leaves no
        partial state behind.
        """

        self._ensure_open()
        credential_id = _nonempty_str(credential_id, "credential_id")
        if credential_id in self._credentials:
            raise CredentialError(f"credential_id {credential_id} was already issued")

        audit = self.audit(request)
        if audit["verdict"] != "pass":
            raise CredentialError(
                f"batch {audit['batch_id']} cannot be certified: {audit['verdict']}"
            )
        for existing in self._credentials.values():
            if (
                existing["batch_id"] == audit["batch_id"]
                and existing["status"] == "active"
            ):
                raise CredentialError(
                    f"batch {audit['batch_id']} already has active credential "
                    f"{existing['credential_id']}"
                )

        record = {
            "credential_id": credential_id,
            "batch_id": audit["batch_id"],
            "issued_at_version": audit["calibration_version"],
            "threshold": Fraction(audit["threshold"]),
            "charge_channel": audit["charge_channel"],
            "discharge_channel": audit["discharge_channel"],
            "samples": [
                {
                    "sample_id": sample["sample_id"],
                    "run_id": sample["run_id"],
                    "cycle_id": sample["cycle_id"],
                    "nominal_charge_wh": Fraction(sample["nominal_charge_wh"]),
                    "nominal_discharge_wh": Fraction(sample["nominal_discharge_wh"]),
                    "margin_min": Fraction(sample["margin_min"]),
                    "margin_max": Fraction(sample["margin_max"]),
                    "calibration_basis": sample["calibration_basis"],
                }
                for sample in audit["samples"]
            ],
            "status": "active",
            "first_failed_version": None,
            "first_failed_sample": None,
        }
        self._credentials[credential_id] = record
        self._save()
        return self.get_credential(credential_id)

    def _stored(self, credential_id: str) -> dict[str, Any]:
        record = self._credentials.get(credential_id)
        if record is None:
            raise CredentialNotFoundError(
                f"credential {credential_id} was never issued"
            )
        return record

    def get_credential(self, credential_id: str) -> dict[str, Any]:
        """Return the immutable issuance archive plus lifecycle metadata."""
        self._ensure_open()
        record = self._stored(credential_id)
        return {
            "credential_id": record["credential_id"],
            "batch_id": record["batch_id"],
            "issued_at_version": record["issued_at_version"],
            "threshold": _text(record["threshold"]),
            "charge_channel": record["charge_channel"],
            "discharge_channel": record["discharge_channel"],
            "samples": [
                {
                    "sample_id": sample["sample_id"],
                    "run_id": sample["run_id"],
                    "cycle_id": sample["cycle_id"],
                    "nominal_charge_wh": _text(sample["nominal_charge_wh"]),
                    "nominal_discharge_wh": _text(sample["nominal_discharge_wh"]),
                    "margin_min": _text(sample["margin_min"]),
                    "margin_max": _text(sample["margin_max"]),
                    "calibration_basis": sample["calibration_basis"],
                }
                for sample in record["samples"]
            ],
            "status": record["status"],
            "first_failed_version": record["first_failed_version"],
            "first_failed_sample": record["first_failed_sample"],
        }

    def check_access(self, credential_id: str) -> dict[str, Any]:
        """Current downstream usability of a credential."""
        self._ensure_open()
        record = self._stored(credential_id)
        return {
            "credential_id": record["credential_id"],
            "batch_id": record["batch_id"],
            "usable": record["status"] == "active",
            "status": record["status"],
            "issued_at_version": record["issued_at_version"],
            "current_version": len(self._versions),
            "first_failed_version": record["first_failed_version"],
            "first_failed_sample": record["first_failed_sample"],
        }

    def list_credentials(self, *, batch_id: str | None = None) -> list[dict[str, Any]]:
        self._ensure_open()
        records = [
            self.get_credential(credential_id)
            for credential_id in sorted(self._credentials)
        ]
        if batch_id is not None:
            records = [record for record in records if record["batch_id"] == batch_id]
        return records
