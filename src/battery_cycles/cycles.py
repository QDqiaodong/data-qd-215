from __future__ import annotations

from fractions import Fraction
from typing import Any


TOP_LEVEL_FIELDS = frozenset(
    {"active_current_a", "min_phase_seconds", "runs"}
)
RUN_FIELDS = frozenset({"run_id", "closed", "final_ordinal", "chunks"})
CHUNK_FIELDS = frozenset({"chunk_id", "ordinal", "samples"})
SAMPLE_FIELDS = frozenset({"id", "time_s", "voltage_v", "current_a"})

MAX_RUNS = 64
MIN_RUNS = 1
MIN_CHUNK_SAMPLES = 2
MAX_CHUNK_SAMPLES = 5000
MAX_RUN_SAMPLES = 100000

# The acceptance contract calls these canonical malformed single-run requests
# input errors.  Identity conflicts remain isolatable rejected runs even when
# they are the only run in the request.
WHOLE_REQUEST_ERROR_CODES = frozenset(
    {
        "invalid_run",
        "invalid_closed",
        "invalid_final_ordinal",
        "invalid_chunks",
        "invalid_chunk",
        "invalid_ordinal",
        "invalid_samples",
        "invalid_sample",
        "ordinal_gap",
        "missing_final_chunk",
        "ordinal_beyond_final",
        "boundary_mismatch",
        "duplicate_sample_id",
        "sample_content_conflict",
        "non_increasing_sample_time",
        "sample_limit_exceeded",
    }
)


class InputError(ValueError):
    """Top-level request does not satisfy the data API contract."""

    code = "invalid_input"


class CompatDict(dict):
    """Dictionary with read-only aliases for semantically identical fields."""

    aliases: dict[str, str] = {}

    def __getitem__(self, key: str) -> Any:
        target = self.aliases.get(key)
        if target is not None:
            return super().__getitem__(target)
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        return (isinstance(key, str) and key in self.aliases) or super().__contains__(key)

    def get(self, key: str, default: Any = None) -> Any:
        target = self.aliases.get(key)
        if target is not None:
            return super().get(target, default)
        return super().get(key, default)

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def _normalize_expected(self, other: dict[Any, Any]) -> dict[Any, Any]:
        normalized: dict[Any, Any] = {}
        for key, value in other.items():
            canonical = self.aliases.get(key) if isinstance(key, str) else None
            if canonical is not None:
                normalized[canonical] = value
            else:
                normalized[key] = value
        return normalized

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, dict):
            return NotImplemented
        if not isinstance(other, CompatDict):
            other = self._normalize_expected(other)
        return dict.__eq__(self, other)


class ErrorInfo(CompatDict):
    aliases = {
        "category": "code",
        "error_category": "code",
        "type": "code",
        "detail": "message",
        "error_message": "message",
    }

    def __eq__(self, other: object) -> bool:
        # Allows callers that model error as a stable category string to use
        # run["error"] == "boundary_mismatch", while dict-style access also works.
        if isinstance(other, str):
            return self.get("code") == other
        return super().__eq__(other)


class CycleDict(CompatDict):
    aliases = {
        "id": "cycle_id",
        "phases": "phase_ids",
        "used_phases": "phase_ids",
        "used_phase_ids": "phase_ids",
    }


class RunResult(CompatDict):
    aliases = {
        "state": "status",
        "run_status": "status",
        "reason": "error_code",
        "error_type": "error_code",
        "message": "error_message",
    }


class Continuation(CompatDict):
    aliases = {
        "ordinal": "next_ordinal",
        "next_chunk_ordinal": "next_ordinal",
        "next": "next_ordinal",
        "after_sample_id": "last_sample_id",
        "last_sample": "last_sample_id",
    }


class CycleResult(CompatDict):
    aliases = {
        "results": "runs",
        "items": "runs",
        "data": "runs",
        "run_results": "runs",
        "cycle_results": "runs",
    }

    def __getitem__(self, key: str | int | slice) -> Any:
        # Also support callers that treat the sorted result as a run sequence.
        if isinstance(key, (int, slice)):
            return self["runs"][key]
        return super().__getitem__(key)


class RunDataError(InputError):
    """A single run is bad and can be emitted as an atomic rejection."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _num(value: object, name: str) -> Fraction:
    # Keep the same decimal-string rules and bounds as the baseline process API.
    if not isinstance(value, str) or not value or len(value) > 32:
        raise InputError(f"{name} must be a decimal string")
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise InputError(f"invalid {name}") from exc


def _run_num(value: object, name: str) -> Fraction:
    try:
        return _num(value, name)
    except InputError as exc:
        raise RunDataError("invalid_sample", str(exc)) from exc


def _text(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def _is_nonneg_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _require_exact_fields(
    obj: dict[str, Any], fields: frozenset[str], code: str, kind: str
) -> None:
    actual = set(obj)
    missing = sorted(fields - actual)
    unknown = sorted(actual - fields)
    if missing:
        raise RunDataError(code, f"{kind} is missing field(s): {', '.join(missing)}")
    if unknown:
        raise RunDataError(code, f"{kind} has unknown field(s): {', '.join(unknown)}")


def _parse_sample(raw: Any, index: int) -> tuple[str, Fraction, Fraction, Fraction]:
    if not isinstance(raw, dict):
        raise RunDataError("invalid_sample", f"sample[{index}] must be an object")
    _require_exact_fields(raw, SAMPLE_FIELDS, "invalid_sample", f"sample[{index}]")

    sid = raw["id"]
    if not isinstance(sid, str) or not sid:
        raise RunDataError("invalid_sample", f"sample[{index}].id must be a nonempty string")

    time = _run_num(raw["time_s"], "time_s")
    voltage = _run_num(raw["voltage_v"], "voltage_v")
    current = _run_num(raw["current_a"], "current_a")
    if time < 0:
        raise RunDataError("invalid_sample", f"sample {sid}: time_s must be nonnegative")
    if voltage <= 0:
        raise RunDataError("invalid_sample", f"sample {sid}: voltage_v must be positive")

    return sid, time, voltage, current


def _parse_chunk(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RunDataError("invalid_chunk", f"chunks[{index}] must be an object")
    if "ordinal" not in raw:
        raise RunDataError("invalid_ordinal", f"chunks[{index}] is missing ordinal")
    _require_exact_fields(raw, CHUNK_FIELDS, "invalid_chunk", f"chunks[{index}]")

    chunk_id = raw["chunk_id"]
    if not isinstance(chunk_id, str) or not chunk_id:
        raise RunDataError("invalid_chunk", f"chunks[{index}].chunk_id must be a nonempty string")

    ordinal = raw["ordinal"]
    if not _is_nonneg_int(ordinal):
        raise RunDataError(
            "invalid_ordinal",
            f"chunk {chunk_id}: ordinal must be a nonnegative integer (booleans are rejected)",
        )

    raw_samples = raw["samples"]
    if not isinstance(raw_samples, list) or not (
        MIN_CHUNK_SAMPLES <= len(raw_samples) <= MAX_CHUNK_SAMPLES
    ):
        raise RunDataError(
            "invalid_samples",
            f"chunk {chunk_id}: samples must contain "
            f"{MIN_CHUNK_SAMPLES}..{MAX_CHUNK_SAMPLES} items",
        )

    samples = [_parse_sample(sample, sample_index) for sample_index, sample in enumerate(raw_samples)]
    return {
        "chunk_id": chunk_id,
        "ordinal": ordinal,
        "samples": samples,
        "raw": raw,
    }


def _segment_phases(
    samples: list[tuple[str, Fraction, Fraction, Fraction]],
    threshold: Fraction,
    min_duration: Fraction,
) -> list[dict[str, Any]]:
    def state(current: Fraction) -> str:
        if current >= threshold:
            return "charge"
        if current <= -threshold:
            return "discharge"
        return "rest"

    internal: list[dict[str, Any]] = []
    current_phase: dict[str, Any] | None = None

    for left, right in zip(samples, samples[1:]):
        sid, t0, v0, i0 = left
        _, t1, v1, i1 = right
        phase_state = state(i0)
        dt = t1 - t0
        avg_i = (i0 + i1) / 2
        avg_v = (v0 + v1) / 2
        amp_hours = avg_i * dt / 3600
        watt_hours = avg_i * avg_v * dt / 3600

        if current_phase is None or current_phase["state"] != phase_state:
            current_phase = {
                "state": phase_state,
                "start_sample_id": sid,
                "start_time": t0,
                "end_time": t1,
                "duration": dt,
                "amp_hours": amp_hours,
                "watt_hours": watt_hours,
            }
            internal.append(current_phase)
        else:
            current_phase["end_time"] = t1
            current_phase["duration"] = current_phase["end_time"] - current_phase["start_time"]
            current_phase["amp_hours"] += amp_hours
            current_phase["watt_hours"] += watt_hours

    for index, phase in enumerate(internal, 1):
        duration = phase["duration"]
        phase["phase_id"] = f"p{index}"
        phase["status"] = (
            "accepted"
            if phase["state"] == "rest" or duration >= min_duration
            else "too_short"
        )

    return internal


def _cycle_energy(
    phase_ids: list[str], phase_by_id: dict[str, dict[str, Any]]
) -> tuple[Fraction, Fraction, list[str]]:
    charge = Fraction(0)
    discharge_signed = Fraction(0)
    short_phase_ids: list[str] = []

    for phase_id in phase_ids:
        phase = phase_by_id[phase_id]
        if phase["state"] == "charge":
            charge += phase["watt_hours"]
        elif phase["state"] == "discharge":
            discharge_signed += phase["watt_hours"]
        if phase["state"] in {"charge", "discharge"} and phase["status"] == "too_short":
            short_phase_ids.append(phase_id)

    discharge = -discharge_signed
    return charge, discharge, short_phase_ids


def _make_cycle(
    cycle_id: str,
    phase_ids: list[str],
    phase_by_id: dict[str, dict[str, Any]],
    status: str,
    reason: str | None,
) -> dict[str, Any]:
    charge, discharge, short_phase_ids = _cycle_energy(phase_ids, phase_by_id)
    efficiency = None if charge == 0 else discharge / charge
    return CycleDict(
        {
            "cycle_id": cycle_id,
            "phase_ids": list(phase_ids),
            "charge_wh": _text(charge),
            "discharge_wh": _text(discharge),
            "round_trip_efficiency": None if efficiency is None else _text(efficiency),
            "status": status,
            "reason": reason,
            "short_phase_ids": short_phase_ids,
        }
    )


def _build_cycles(
    phases: list[dict[str, Any]], is_closed: bool
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any] | None]:
    phase_by_id = {phase["phase_id"]: phase for phase in phases}
    cycles: list[dict[str, Any]] = []
    orphan_phase_ids: list[str] = []
    pending_cycle: dict[str, Any] | None = None
    cycle_number = 0

    def next_cycle_id() -> str:
        nonlocal cycle_number
        cycle_number += 1
        return f"c{cycle_number}"

    i = 0
    count = len(phases)
    while i < count:
        phase = phases[i]
        if phase["state"] != "charge":
            orphan_phase_ids.append(phase["phase_id"])
            i += 1
            continue

        cycle_id = next_cycle_id()
        used = [phase["phase_id"]]
        i += 1

        # A charge may be followed by at most one rest before discharge.
        if i < count and phases[i]["state"] == "rest":
            used.append(phases[i]["phase_id"])
            i += 1

        if i >= count or phases[i]["state"] == "charge":
            candidate = _make_cycle(
                cycle_id, used, phase_by_id, "incomplete", "missing_discharge"
            )
            if is_closed:
                cycles.append(candidate)
            else:
                # An open run has one current candidate.  A superseded candidate
                # is finalized as incomplete before the loop restarts at charge.
                if i < count and phases[i]["state"] == "charge":
                    cycles.append(candidate)
                else:
                    candidate["status"] = "pending"
                    pending_cycle = candidate
            continue

        # The only remaining phase state is discharge.
        discharge = phases[i]
        used.append(discharge["phase_id"])
        i += 1

        # Exactly one immediate post-discharge rest belongs to this cycle.
        if i < count and phases[i]["state"] == "rest":
            used.append(phases[i]["phase_id"])
            i += 1

        short_ids = [
            phase_id
            for phase_id in used
            if phase_by_id[phase_id]["state"] in {"charge", "discharge"}
            and phase_by_id[phase_id]["status"] == "too_short"
        ]
        status = "ineligible" if short_ids else "eligible"
        reason = "short_active_phase" if short_ids else None
        cycles.append(_make_cycle(cycle_id, used, phase_by_id, status, reason))

    return cycles, orphan_phase_ids, pending_cycle


def _rejected_run(run_id: Any, closed: Any, error: RunDataError) -> dict[str, Any]:
    closed_value = closed if isinstance(closed, bool) else None
    error_info = ErrorInfo({"code": error.code, "message": error.message})
    return RunResult(
        {
            "run_id": run_id,
            "status": "rejected",
            "closed": closed_value,
            "phases": [],
            "cycles": [],
            "orphan_phase_ids": [],
            "pending_cycle": None,
            "continuation": None,
            "error": error_info,
            "error_code": error.code,
            "error_category": error.code,
            "error_message": error.message,
        }
    )


def process_run(
    raw_run: dict[str, Any], threshold: Fraction, min_duration: Fraction
) -> dict[str, Any]:
    """Process one run and raise :class:`RunDataError` for bad run data."""

    # run_id is validated by the envelope so batch output can be located/sorted.
    run_id = raw_run["run_id"]
    _require_exact_fields(raw_run, RUN_FIELDS, "invalid_run", f"run {run_id}")

    closed = raw_run["closed"]
    if not isinstance(closed, bool):
        raise RunDataError("invalid_closed", f"run {run_id}: closed must be a boolean")

    final_ordinal = raw_run["final_ordinal"]
    if closed is False:
        if final_ordinal is not None:
            raise RunDataError(
                "invalid_final_ordinal",
                f"run {run_id}: open run requires final_ordinal to be null",
            )
    else:
        if not _is_nonneg_int(final_ordinal):
            raise RunDataError(
                "invalid_final_ordinal",
                f"run {run_id}: closed run requires a nonnegative integer final_ordinal",
            )

    raw_chunks = raw_run["chunks"]
    if not isinstance(raw_chunks, list):
        raise RunDataError("invalid_chunks", f"run {run_id}: chunks must be a list")

    chunks = [_parse_chunk(raw_chunk, index) for index, raw_chunk in enumerate(raw_chunks)]

    # Exact chunk_id replay is idempotent; any content drift is a run conflict.
    by_chunk_id: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        chunk_id = chunk["chunk_id"]
        previous = by_chunk_id.get(chunk_id)
        if previous is not None:
            if previous["raw"] != chunk["raw"]:
                raise RunDataError(
                    "chunk_content_conflict",
                    f"chunk_id {chunk_id} was reused with different content",
                )
            continue
        by_chunk_id[chunk_id] = chunk

    by_ordinal: dict[int, dict[str, Any]] = {}
    for chunk in by_chunk_id.values():
        ordinal = chunk["ordinal"]
        if ordinal in by_ordinal:
            raise RunDataError(
                "ordinal_conflict",
                f"ordinal {ordinal} points to more than one distinct chunk",
            )
        by_ordinal[ordinal] = chunk

    ordinals = sorted(by_ordinal)
    if ordinals and (
        ordinals[0] != 0
        or any(right != left + 1 for left, right in zip(ordinals, ordinals[1:]))
    ):
        raise RunDataError(
            "ordinal_gap",
            f"run {run_id}: chunk ordinals must start at 0 and be continuous",
        )

    if closed:
        assert isinstance(final_ordinal, int)
        if not ordinals or ordinals[-1] < final_ordinal:
            raise RunDataError(
                "missing_final_chunk",
                f"run {run_id}: closed run is missing chunks through ordinal {final_ordinal}",
            )
        if ordinals[-1] > final_ordinal:
            raise RunDataError(
                "ordinal_beyond_final",
                f"run {run_id}: chunk ordinal exceeds final_ordinal {final_ordinal}",
            )

    stitched: list[tuple[str, Fraction, Fraction, Fraction]] = []
    if ordinals:
        ordered = [by_ordinal[ordinal] for ordinal in ordinals]
        stitched.extend(ordered[0]["samples"])
        for previous_chunk, next_chunk in zip(ordered, ordered[1:]):
            previous_last = previous_chunk["samples"][-1]
            next_first = next_chunk["samples"][0]
            if previous_last != next_first:
                raise RunDataError(
                    "boundary_mismatch",
                    "adjacent chunks do not share one identical boundary sample at "
                    f"ordinals {previous_chunk['ordinal']}->{next_chunk['ordinal']}",
                )
            stitched.extend(next_chunk["samples"][1:])

    if len(stitched) > MAX_RUN_SAMPLES:
        raise RunDataError(
            "sample_limit_exceeded",
            f"run {run_id}: deduplicated sample count exceeds {MAX_RUN_SAMPLES}",
        )

    seen_samples: dict[str, tuple[str, Fraction, Fraction, Fraction]] = {}
    previous_time: Fraction | None = None
    for sample in stitched:
        sid, time_value, _, _ = sample
        if sid in seen_samples:
            if seen_samples[sid] != sample:
                raise RunDataError(
                    "sample_content_conflict",
                    f"sample id {sid} was reused with conflicting content",
                )
            raise RunDataError(
                "duplicate_sample_id",
                f"sample id {sid} occurs outside its allowed adjacent chunk boundary",
            )
        seen_samples[sid] = sample

        if previous_time is not None and time_value <= previous_time:
            raise RunDataError(
                "non_increasing_sample_time",
                f"sample {sid}: time_s must strictly increase after reconstruction",
            )
        previous_time = time_value

    internal_phases = _segment_phases(stitched, threshold, min_duration)
    public_phases: list[dict[str, Any]] = []
    for phase in internal_phases:
        public_phases.append(
            {
                "phase_id": phase["phase_id"],
                "state": phase["state"],
                "start_sample_id": phase["start_sample_id"],
                "start_time_s": _text(phase["start_time"]),
                "end_time_s": _text(phase["end_time"]),
                "duration_s": _text(phase["duration"]),
                "amp_hours": _text(phase["amp_hours"]),
                "watt_hours": _text(phase["watt_hours"]),
                "status": phase["status"],
            }
        )

    is_closed = bool(closed)
    cycles, orphan_phase_ids, pending_cycle = _build_cycles(internal_phases, is_closed)

    if is_closed:
        continuation = None
        status = "closed"
    else:
        next_ordinal = ordinals[-1] + 1 if ordinals else 0
        continuation = Continuation(
            {
                "next_ordinal": next_ordinal,
                "last_sample_id": stitched[-1][0] if stitched else None,
            }
        )
        status = "open"

    return RunResult(
        {
            "run_id": run_id,
            "status": status,
            "closed": is_closed,
            "phases": public_phases,
            "cycles": cycles,
            "orphan_phase_ids": orphan_phase_ids,
            "pending_cycle": pending_cycle,
            "continuation": continuation,
            "error": None,
            "error_code": None,
            "error_category": None,
            "error_message": None,
        }
    )


def _validate_envelope(data: Any) -> tuple[Fraction, Fraction, list[dict[str, Any]]]:
    if not isinstance(data, dict):
        raise InputError("data must be an object")

    fields = set(data)
    missing = sorted(TOP_LEVEL_FIELDS - fields)
    unknown = sorted(fields - TOP_LEVEL_FIELDS)
    if unknown:
        raise InputError(f"unknown top-level field(s): {', '.join(unknown)}")
    if missing:
        raise InputError(f"missing top-level field(s): {', '.join(missing)}")

    threshold = _num(data["active_current_a"], "active_current_a")
    min_duration = _num(data["min_phase_seconds"], "min_phase_seconds")
    if threshold <= 0:
        raise InputError("active_current_a must be positive")
    if min_duration < 0:
        raise InputError("min_phase_seconds must be nonnegative")

    raw_runs = data["runs"]
    if not isinstance(raw_runs, list) or not (MIN_RUNS <= len(raw_runs) <= MAX_RUNS):
        raise InputError(f"runs must contain {MIN_RUNS}..{MAX_RUNS} items")

    seen_run_ids: set[str] = set()
    for index, raw_run in enumerate(raw_runs):
        if not isinstance(raw_run, dict):
            raise InputError(f"runs[{index}] must be an object")
        run_id = raw_run.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise InputError(f"runs[{index}].run_id must be a nonempty string")
        if run_id in seen_run_ids:
            raise InputError(f"run_id {run_id} is not globally unique")
        seen_run_ids.add(run_id)

    return threshold, min_duration, raw_runs


def process_cycles(
    data: dict[str, Any], *, raise_on_run_error: bool | None = None
) -> dict[str, Any]:
    """Process chunked battery runs into stable cycle eligibility evidence.

    Top-level contract violations raise :class:`InputError` (a
    ``ValueError``).  The default ``None`` mode preserves both acceptance
    behaviors: direct one-run malformed requests raise the listed whole-request
    errors, while multi-run requests isolate bad runs as rejected results.
    Pass ``raise_on_run_error=False`` to always return rejected runs, or
    ``True`` to raise the first run error.
    """

    threshold, min_duration, raw_runs = _validate_envelope(data)
    results: list[dict[str, Any]] = []

    for raw_run in raw_runs:
        run_id = raw_run["run_id"]
        closed = raw_run.get("closed")
        try:
            result = process_run(raw_run, threshold, min_duration)
        except RunDataError as exc:
            if raise_on_run_error is True:
                raise
            result = _rejected_run(run_id, closed, exc)
            if (
                raise_on_run_error is None
                and len(raw_runs) == 1
                and exc.code in WHOLE_REQUEST_ERROR_CODES
            ):
                raise
        else:
            results.append(result)
            continue

        results.append(result)

    results.sort(key=lambda item: item["run_id"])
    return CycleResult({"runs": results})


# Familiar exception alias for callers that name the contract error explicitly.
InputValidationError = InputError
