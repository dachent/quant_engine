"""Run a numerical producer under work counters without editing its bytes.

The attended release builder launches `portfolio_decomposition.py` and
`portfolio_correlation.py` as subprocesses, and those producers report only a
`reused` label. A label is a code-path claim, not a measurement of work done,
and a content-addressed rebuild that writes identical bytes leaves no trace in
a file count either. This wrapper imports the producer, counts the calls that
do the numerical work, runs the producer's own `main` unchanged (its stdout is
the producer's stdout), and writes the counts to the path named by
`QE_NUMERICAL_WORK_COUNTERS`.

Nothing here is hashed into any producer identity: the producers' identities
bind their own file bytes and their consumed helpers, never the launcher's
argv, so launching through this wrapper moves no generation id.

Usage: portfolio_counted_producer.py <producer file name> <producer args...>
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
import contextlib
import importlib
import json
import os
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, cast

SCHEMA_VERSION = 1
COUNTER_ENV = "QE_NUMERICAL_WORK_COUNTERS"
WORK_COUNTER_KEYS = (
    "build_calls",
    "correlation_build_calls",
    "grid_search_calls",
    "model_fit_calls_dynamic",
    "model_fit_calls_static",
)
COUNTED_PRODUCERS = {
    "portfolio_correlation.py": "portfolio_correlation",
    "portfolio_decomposition.py": "portfolio_decomposition",
}


class CountedProducerError(ValueError):
    """The counting contract was broken: missing, malformed, or inconsistent counts."""


def zero_counters() -> dict[str, int]:
    return {key: 0 for key in WORK_COUNTER_KEYS}


def sum_work_counters(rows: Iterable[Mapping[str, int]]) -> dict[str, int]:
    total = zero_counters()
    for row in rows:
        for key in WORK_COUNTER_KEYS:
            total[key] += row[key]
    return total


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def read_work_counters(path: Path, *, producer: str) -> dict[str, int]:
    """Read one counter file, refusing anything the wrapper did not write.

    A missing file is the signature of a producer launched around the wrapper,
    so it is an error and never an implied zero.
    """

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CountedProducerError(
            f"{producer} did not write work counters; it bypassed the counting wrapper"
        ) from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CountedProducerError(f"{producer} work counters are not JSON") from exc
    expected = {"producer", "schema_version", *WORK_COUNTER_KEYS}
    if not isinstance(value, dict) or set(value) != expected:
        raise CountedProducerError(f"{producer} work counters have unexpected keys")
    if value["schema_version"] != SCHEMA_VERSION or value["producer"] != producer:
        raise CountedProducerError(f"{producer} work counters name another producer")
    if not all(_is_count(value[key]) for key in WORK_COUNTER_KEYS):
        raise CountedProducerError(f"{producer} work counters are not counts")
    return {key: cast(int, value[key]) for key in WORK_COUNTER_KEYS}


def reconcile_reuse_label(
    counters: Mapping[str, int],
    reused: object,
    *,
    producer: str,
    operation: str,
) -> None:
    """Refuse a reuse label that disagrees with the counted work.

    `reused: true` means no numerical work ran; `reused: false` means the
    producer rebuilt. Work the wrapper never saw under a recomputed label means
    the producer did its work outside the functions the wrapper counts.
    """

    if reused is True and any(counters[key] for key in WORK_COUNTER_KEYS):
        raise CountedProducerError(
            f"{producer} {operation} reported reuse but counted numerical work"
        )
    if reused is False and counters["build_calls"] == 0:
        raise CountedProducerError(
            f"{producer} {operation} reported a rebuild but counted no build; "
            "its work bypassed the counting wrapper"
        )


def _install(
    module: ModuleType, name: str, counters: dict[str, int]
) -> list[tuple[str, Any]]:
    """Wrap the module-level callables that do the work; return what to restore."""

    saved: list[tuple[str, Any]] = []

    def patch(attribute: str, replacement: Callable[..., Any]) -> None:
        saved.append((attribute, getattr(module, attribute)))
        setattr(module, attribute, replacement)

    if name == "portfolio_decomposition":
        original_fit = module._fit
        original_dynamic_fit = module._dynamic_fit
        original_build = module.build_decomposition

        def counted_fit(*args: object, **kwargs: object) -> Any:
            counters["model_fit_calls_static"] += 1
            return original_fit(*args, **kwargs)

        def counted_dynamic_fit(*args: object, **kwargs: object) -> Any:
            counters["model_fit_calls_dynamic"] += 1
            return original_dynamic_fit(*args, **kwargs)

        def counted_build_decomposition(
            spec_path: Path, *args: object, **kwargs: object
        ) -> Any:
            counters["build_calls"] += 1
            # Same unit as the verifier: models re-searched on a rebuild.
            counters["grid_search_calls"] += len(module._load_spec(spec_path).models)
            return original_build(spec_path, *args, **kwargs)

        patch("_fit", counted_fit)
        patch("_dynamic_fit", counted_dynamic_fit)
        patch("build_decomposition", counted_build_decomposition)
    else:
        original_correlation = module.build_correlation

        def counted_build_correlation(*args: object, **kwargs: object) -> Any:
            counters["build_calls"] += 1
            counters["correlation_build_calls"] += 1
            return original_correlation(*args, **kwargs)

        patch("build_correlation", counted_build_correlation)
    return saved


@contextlib.contextmanager
def counting(module: ModuleType, producer: str) -> Iterator[dict[str, int]]:
    """Count the producer's numerical work for the duration of the block."""

    counters = zero_counters()
    saved = _install(module, COUNTED_PRODUCERS[producer], counters)
    try:
        yield counters
    finally:
        for attribute, original in reversed(saved):
            setattr(module, attribute, original)


def _write_counters(path: Path, producer: str, counters: Mapping[str, int]) -> None:
    payload = {"producer": producer, "schema_version": SCHEMA_VERSION, **counters}
    content = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    with path.open("xb") as handle:
        handle.write(content.encode("utf-8"))


def run_counted(
    producer: str,
    argv: Sequence[str],
    counters_path: Path,
    *,
    module: ModuleType | None = None,
) -> int:
    """Run `producer`'s own `main(argv)` and record the counted work."""

    if producer not in COUNTED_PRODUCERS:
        raise CountedProducerError(f"{producer} is not a counted producer")
    if module is None:
        module = importlib.import_module(COUNTED_PRODUCERS[producer])
    with counting(module, producer) as counters:
        try:
            return int(module.main(list(argv)))
        finally:
            _write_counters(counters_path, producer, counters)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in COUNTED_PRODUCERS:
        print(
            "usage: portfolio_counted_producer.py "
            f"{{{'|'.join(sorted(COUNTED_PRODUCERS))}}} <producer arguments>",
            file=sys.stderr,
        )
        return 2
    destination = os.environ.get(COUNTER_ENV)
    if not destination:
        print(f"{COUNTER_ENV} must name the work counter file", file=sys.stderr)
        return 2
    producer, *producer_arguments = arguments
    # The producer's argparse reports its own name, as it would when launched bare.
    sys.argv[0] = str(Path(__file__).resolve().with_name(producer))
    return run_counted(producer, producer_arguments, Path(destination))


if __name__ == "__main__":
    raise SystemExit(main())
