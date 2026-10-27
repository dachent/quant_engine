"""Governed kernel execution entry: PORT-HWM-4 on real governed inputs.

Hand-worked micro-fixture (Task 5 criterion 4): two members over six days.
Member A trades daily; member B is a session source whose Fri->Mon weekend
is an accepted interval whose economics land on Monday's row. Composite
weights are beginning-capital based; interior gap days carry B at zero
derived from the accepted calendar authority.

P1-hardened contract (Codex #370):
- P1-1 generation embeds an independent upstream admission-receipt anchor;
- P1-2 calculation consumes ONLY the hash-verified generation payload;
- P1-3 policy-referenced authority artifacts validated every execution;
- P1-4 interval grid bound to the calendar authority root + member anchor;
- P1-5 whole arithmetic pipeline inside one explicit decimal context.
"""

import hashlib
import json
from decimal import Decimal, getcontext
from pathlib import Path

import pytest

from quant_engine.portfolio_ledger.hwm import (
    AS_HELD_HWM_RETURN_BASIS,
    HwmCoverageError,
    _canonical_json_bytes,
)
from quant_engine.portfolio_ledger.hwm_governed_execution import (
    compute_governed_as_held_composite_hwm,
    verify_governed_hwm_result,
)

REPO = Path(__file__).resolve().parents[2]
PRODUCT_CONTRACT_BYTES = (
    REPO / "contracts" / "portfolio-financial-mvp-source-v1.json"
).read_bytes()
PERFORMANCE_POLICY_BYTES = (
    REPO / "contracts" / "portfolio-performance-policy-v1.json"
).read_bytes()
LEGACY_POLICY_BYTES = (REPO / "contracts" / "portfolio-hwm-policy-v3.json").read_bytes()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _policy_bytes(ready=True, mode="flipped"):
    """Real policy template; 'flipped' swaps the seam to the TEST id,
    'committed' returns the production file BYTES exactly as committed
    (identity checks hash these bytes, not a re-serialization)."""
    if ready and mode == "committed":
        return (REPO / "contracts" / "portfolio-hwm-policy-v4.json").read_bytes()
    policy = json.loads(
        (REPO / "contracts" / "portfolio-hwm-policy-v4.json").read_text(
            encoding="utf-8"
        )
    )
    cp = policy["calendar_policy"]
    cp["release_authority_status"] = "READY" if ready else "REQUIRED_NOT_READY"
    cp["accepted_governed_calendar_policy_ids"] = ["GOV-CAL-TEST"] if ready else []
    return json.dumps(policy, indent=2, sort_keys=True).encode()


def _receipt_bytes():
    """Kernel-visible admission anchor tying contract bytes to policy pins.

    The full provider-ledger admit-or-declare receipt carries far more
    content; the kernel binds the identity fields that make the
    contract<->receipt<->policy triangle verifiable without staging access.
    """
    return json.dumps(
        {
            # Mirrors the real admit-or-declare receipt's identity fields.
            "admission_gate_status": "PASS",
            "contract_raw_sha256": hashlib.sha256(PRODUCT_CONTRACT_BYTES).hexdigest(),
            # Reuse the repo's own canonicalization, never a hand-rolled one.
            "contract_semantic_sha256": hashlib.sha256(
                _canonical_json_bytes(json.loads(PRODUCT_CONTRACT_BYTES))
            ).hexdigest(),
        },
        sort_keys=True,
    ).encode()


def _authority_doc(policy_id="GOV-CAL-TEST"):
    """Two sources: A always-on; B session source with one accepted weekend."""
    doc = {
        "schema_version": 2,
        "authority_class": "ACCEPTED_GOVERNED",
        "calendar_policy_id": policy_id,
        "portfolio_calendar": "ALWAYS_ON",
        "owner_decision_evidence": "owner-comment",
        "hwm_policy_sha256": "0" * 64,
        "source_calendars": [
            {
                "strategy_id": "a-daily",
                "calendar_id": "a:OBSERVED:2026-01-02",
                "calendar_kind": "ALWAYS_ON",
                "calendar_evidence_sha256": _sha("a-evidence"),
            },
            {
                "strategy_id": "b-session",
                "calendar_id": "b:OBSERVED:2026-01-02",
                "calendar_kind": "DECLARED_SESSION",
                "calendar_evidence_sha256": _sha("b-evidence"),
            },
        ],
        "closed_source_dates": [],
        "accepted_source_intervals": [
            {
                "strategy_id": "b-session",
                "calendar_id": "b:OBSERVED:2026-01-02",
                "prior_observation_date": "2026-01-02",
                "next_observation_date": "2026-01-05",
                "binding_return_date": "2026-01-05",
                "binding_return_value": "0.04",
                "interval_evidence_sha256": hashlib.sha256(
                    "\n".join(
                        [
                            "b-session",
                            "b:OBSERVED:2026-01-02",
                            "2026-01-02",
                            "2026-01-05",
                            "0.04",
                        ]
                    ).encode()
                ).hexdigest(),
                "treatment": "ACCEPTED_MULTI_DAY_RETURN_BINDING",
            }
        ],
    }
    canonical = json.dumps(
        {k: v for k, v in doc.items() if k != "authority_evidence_sha256"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    doc["authority_evidence_sha256"] = hashlib.sha256(canonical).hexdigest()
    return doc


def _period(sid, start, end, ret, cap):
    return {
        "strategy_id": sid,
        "interval_start": start,
        "interval_end": end,
        "beginning_capital_usd": cap,
        "net_return": ret,
        "return_basis": AS_HELD_HWM_RETURN_BASIS,
        "return_evidence_sha256": _sha(f"{sid}:{start}:{end}:{ret}"),
        "membership_evidence_sha256": _sha(f"m-{sid}"),
        "policy_binding_sha256": "0" * 64,
        "calendar_sha256": "0" * 64,
        "source_generation_sha256": "",
    }


def _generation(periods, *, policy_binding, calendar_binding, receipt_binding):
    gen = {
        "schema_version": 1,
        "builder": "portfolio-hwm-inputs-governed",
        "builder_version": 1,
        "authority_profile": "ACCEPTED_GOVERNED",
        "memberships": [
            {
                "strategy_id": "a-daily",
                "effective_start": "2026-01-02",
                "membership_evidence_sha256": _sha("m-a-daily"),
            },
            {
                "strategy_id": "b-session",
                "effective_start": "2026-01-02",
                "membership_evidence_sha256": _sha("m-b-session"),
            },
        ],
        "component_periods": periods,
        "policy_binding_sha256": policy_binding,
        "calendar_sha256": calendar_binding,
        "upstream_receipt_sha256": receipt_binding,
        "evidence_sha256s": sorted({_sha("m-a-daily"), _sha("m-b-session")}),
    }
    canonical = json.dumps(
        {k: v for k, v in gen.items() if k != "generation_evidence_sha256"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    gen["generation_evidence_sha256"] = hashlib.sha256(canonical).hexdigest()
    return gen


DAYS = [
    "2026-01-02",
    "2026-01-03",
    "2026-01-04",
    "2026-01-05",
    "2026-01-06",
    "2026-01-07",
]


def _fixture(tmp_path, ready=True, mode="flipped"):
    policy_bytes = _policy_bytes(ready=ready, mode=mode)
    if mode == "committed":
        committed_policy = json.loads(
            (REPO / "contracts" / "portfolio-hwm-policy-v4.json").read_text(
                encoding="utf-8"
            )
        )
        accepted_ids = committed_policy["calendar_policy"][
            "accepted_governed_calendar_policy_ids"
        ]
        cal = _authority_doc(policy_id=accepted_ids[0])
    else:
        cal = _authority_doc()
    # Bind policy sha into the doc like the real derivation does.
    cal["hwm_policy_sha256"] = hashlib.sha256(policy_bytes).hexdigest()
    canonical = json.dumps(
        {k: v for k, v in cal.items() if k != "authority_evidence_sha256"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    cal["authority_evidence_sha256"] = hashlib.sha256(canonical).hexdigest()
    cal_root = cal["authority_evidence_sha256"]
    cal_bytes = json.dumps(cal, sort_keys=True).encode()

    receipt = _receipt_bytes()

    periods = [
        # A: five one-day rows (weekend included: always-on source).
        _period("a-daily", "2026-01-02", "2026-01-03", "0.01", "60"),
        _period("a-daily", "2026-01-03", "2026-01-04", "0.02", "61.2"),
        _period("a-daily", "2026-01-04", "2026-01-05", "-0.01", "62.424"),
        _period("a-daily", "2026-01-05", "2026-01-06", "0.03", "61.79976"),
        _period("a-daily", "2026-01-06", "2026-01-07", "0.005", "63.653748"),
        # B: Friday one-day row, then the REAL embedded-span shape: the
        # Mon row spans Fri->Mon (declared interval), then one-day rows.
        _period("b-session", "2026-01-02", "2026-01-03", "0.005", "40"),
        _period("b-session", "2026-01-02", "2026-01-05", "0.04", "40.2"),
        _period("b-session", "2026-01-05", "2026-01-06", "0.002", "41.808"),
        _period("b-session", "2026-01-06", "2026-01-07", "0.01", "41.891616"),
    ]
    gen = _generation(
        periods,
        policy_binding=hashlib.sha256(policy_bytes).hexdigest(),
        calendar_binding=cal_root,
        receipt_binding=hashlib.sha256(receipt).hexdigest(),
    )
    return {
        "accepted_intervals": [
            {
                "interval_id": f"i{index}",
                "interval_start": start,
                "interval_end": DAYS[index + 1],
                "interval_evidence_sha256": hashlib.sha256(
                    f"{start}|{DAYS[index + 1]}|{cal_root}".encode()
                ).hexdigest(),
            }
            for index, start in enumerate(DAYS[:-1])
        ],
        "memberships": gen["memberships"],
        "component_periods": periods,
        "generation": gen,
        "policy_bytes": policy_bytes,
        "product_contract_bytes": PRODUCT_CONTRACT_BYTES,
        "performance_policy_bytes": PERFORMANCE_POLICY_BYTES,
        "legacy_policy_bytes": LEGACY_POLICY_BYTES,
        "calendar_authority_bytes": cal_bytes,
        "input_receipt_bytes": receipt,
        "calendar_accepted_sha256": cal_root,
        "evaluated_on": "2026-01-07",
        "complete_through": "2026-01-07",
        "maximum_age_days": 30,
        "assurance_profile": "WORKSTATION_QC",
    }


def _run(
    fx,
    tmp_path=None,
    *,
    accepted_intervals=None,
    memberships=None,
    component_periods=None,
    **overrides,
):
    """Execute the kernel with fixture defaults plus per-call overrides."""
    generation = overrides.pop("generation", fx["generation"])
    kwargs = dict(
        policy_bytes=fx["policy_bytes"],
        product_contract_bytes=fx["product_contract_bytes"],
        performance_policy_bytes=fx["performance_policy_bytes"],
        legacy_policy_bytes=fx["legacy_policy_bytes"],
        calendar_authority_bytes=fx["calendar_authority_bytes"],
        input_generation_bytes=json.dumps(generation, sort_keys=True).encode(),
        input_receipt_bytes=fx["input_receipt_bytes"],
        calendar_accepted_sha256=fx["calendar_accepted_sha256"],
        evaluated_on=fx["evaluated_on"],
        complete_through=fx["complete_through"],
        maximum_age_days=fx["maximum_age_days"],
        assurance_profile=fx["assurance_profile"],
    )
    kwargs.update(overrides)
    return compute_governed_as_held_composite_hwm(
        fx["accepted_intervals"] if accepted_intervals is None else accepted_intervals,
        fx["memberships"] if memberships is None else memberships,
        fx["component_periods"] if component_periods is None else component_periods,
        **kwargs,
    )


def test_governed_entry_computes_hand_worked_composite(tmp_path):
    fx = _fixture(tmp_path)
    result = _run(fx)
    assert result["status"] == "AVAILABLE"
    assert result["input_authority_class"] == "ACCEPTED_GOVERNED"
    assert result["current_index"] > Decimal("100")
    # Hand-check the first interval composite: (60*.01 + 40*.005)/100 = 0.008
    first = result["periods"][0]
    assert first["composite_return"] == Decimal("0.008")
    # Saturday: B inside accepted span -> kernel-derived zero; A keeps trading.
    sat = result["periods"][1]
    b_sat = [c for c in sat["components"] if c["strategy_id"] == "b-session"]
    assert b_sat[0]["period_return"] == Decimal("0")
    # The span's economics land on the grid interval ENDING at the binding
    # day: Sun->Mon carries B's bound 0.04.
    sun_to_mon = result["periods"][2]
    b_sm = [c for c in sun_to_mon["components"] if c["strategy_id"] == "b-session"]
    assert b_sm[0]["period_return"] == Decimal("0.04")
    # One-day rows resume after the binding day (Mon->Tue .002).
    mon = result["periods"][3]
    b_mon = [c for c in mon["components"] if c["strategy_id"] == "b-session"]
    assert b_mon[0]["period_return"] == Decimal("0.002")


def test_governed_entry_rejects_account_live_net_basis_mutant(tmp_path):
    fx = _fixture(tmp_path)
    mutant = json.loads(json.dumps(fx["generation"], sort_keys=True))
    for row in mutant["component_periods"]:
        row["return_basis"] = "ACCOUNT_LIVE_NET"
    canonical = json.dumps(
        {k: v for k, v in mutant.items() if k != "generation_evidence_sha256"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    mutant["generation_evidence_sha256"] = hashlib.sha256(canonical).hexdigest()
    with pytest.raises(HwmCoverageError, match="governed return basis differs"):
        _run(fx, generation=mutant)


def test_governed_entry_requires_ready_seam(tmp_path):
    from quant_engine.portfolio_ledger.hwm_governed import (
        GovernedHwmAuthorityError,
    )

    fx = _fixture(tmp_path, ready=False)
    with pytest.raises(
        (HwmCoverageError, GovernedHwmAuthorityError), match="release-ready"
    ):
        _run(fx)


def test_governed_entry_requires_real_authority_bytes(tmp_path):
    """P1-3: placeholder b'{}' authorities fail closed before any math."""
    fx = _fixture(tmp_path)
    with pytest.raises(HwmCoverageError, match="authority bytes differ"):
        _run(fx, product_contract_bytes=b"{}")


def test_governed_entry_rejects_unsigned_generation_drift(tmp_path):
    """Self-hash still guards against unsigned tampering of the payload."""
    fx = _fixture(tmp_path)
    drifted = json.loads(json.dumps(fx["generation"], sort_keys=True))
    drifted["component_periods"][0]["net_return"] = "0.5"
    with pytest.raises(HwmCoverageError, match="identity differs"):
        _run(fx, generation=drifted)


def test_governed_entry_rejects_resigned_span_drift(tmp_path):
    """A re-signed generation cannot alter DECLARED span economics: the
    calendar authority's binding_return_value still pins those rows."""
    fx = _fixture(tmp_path)
    resigned = json.loads(json.dumps(fx["generation"], sort_keys=True))
    for row in resigned["component_periods"]:
        if (
            row["interval_start"] == "2026-01-02"
            and row["interval_end"] == "2026-01-05"
        ):
            row["net_return"] = "0.05"
            row["return_evidence_sha256"] = _sha("forged-span")
    canonical = json.dumps(
        {k: v for k, v in resigned.items() if k != "generation_evidence_sha256"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    resigned["generation_evidence_sha256"] = hashlib.sha256(canonical).hexdigest()
    with pytest.raises(HwmCoverageError, match="differs from accepted binding"):
        _run(fx, generation=resigned)


def test_governed_entry_ignores_forged_caller_rows(tmp_path):
    """P1-2: caller-supplied rows differing from the verified payload are
    ignored — the calculation must be identical to the honest invocation."""
    fx = _fixture(tmp_path)
    honest = _run(fx)
    forged_rows = json.loads(json.dumps(fx["component_periods"]))
    forged_rows[0]["net_return"] = "0.9"
    forged = _run(fx, component_periods=forged_rows)
    assert forged["receipt_sha256"] == honest["receipt_sha256"]
    # A different COUNT still fails closed (caller/generation divergence).
    with pytest.raises(HwmCoverageError, match="coverage differs"):
        _run(fx, component_periods=fx["component_periods"][:-1])


def test_governed_entry_requires_upstream_receipt_anchor(tmp_path):
    """P1-1: a generation without the verified receipt hash fails closed."""
    fx = _fixture(tmp_path)
    unanchored = json.loads(json.dumps(fx["generation"]))
    unanchored["upstream_receipt_sha256"] = _sha("some-other-receipt")
    canonical = json.dumps(
        {k: v for k, v in unanchored.items() if k != "generation_evidence_sha256"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    unanchored["generation_evidence_sha256"] = hashlib.sha256(canonical).hexdigest()
    with pytest.raises(HwmCoverageError, match="upstream receipt"):
        _run(fx, generation=unanchored)


def test_governed_entry_rejects_grid_not_bound_to_calendar(tmp_path):
    """P1-4: dropping an early interval must fail closed, not restart base-100."""
    fx = _fixture(tmp_path)
    with pytest.raises(HwmCoverageError, match="member-anchored"):
        _run(fx, accepted_intervals=fx["accepted_intervals"][1:])
    tampered = [dict(row) for row in fx["accepted_intervals"]]
    tampered[2]["interval_evidence_sha256"] = _sha("forged-evidence")
    with pytest.raises(HwmCoverageError, match="calendar identity differs"):
        _run(fx, accepted_intervals=tampered)


def test_governed_entry_is_decimal_context_independent(tmp_path):
    """P1-5: an exotic ambient context must not perturb the receipt."""
    fx = _fixture(tmp_path)
    baseline = _run(fx)
    original_prec = getcontext().prec
    try:
        getcontext().prec = 4
        perturbed = _run(fx)
    finally:
        getcontext().prec = original_prec
    assert perturbed["receipt_sha256"] == baseline["receipt_sha256"]


def test_verify_governed_hwm_result_accepts_and_rejects(tmp_path):
    """Report/panel parity: verifier recomputes and binds the receipt."""
    from quant_engine.portfolio_ledger.hwm_governed_execution import (
        verify_governed_hwm_result,
    )

    fx = _fixture(tmp_path)
    result = _run(fx)
    # Honest verification succeeds and returns the recomputed result.
    verified = verify_governed_hwm_result(
        json.loads(json.dumps(result, default=str)),
        input_generation_bytes=json.dumps(fx["generation"], sort_keys=True).encode(),
        calendar_bytes=fx["calendar_authority_bytes"],
        input_receipt_bytes=fx["input_receipt_bytes"],
        policy_bytes=fx["policy_bytes"],
        product_contract_bytes=fx["product_contract_bytes"],
        performance_policy_bytes=fx["performance_policy_bytes"],
        legacy_policy_bytes=fx["legacy_policy_bytes"],
    )
    assert verified["receipt_sha256"] == result["receipt_sha256"]
    # Any tampering with the presented receipt fails field-for-field binding.
    forged = json.loads(json.dumps(result, default=str))
    forged["current_index"] = "999"
    with pytest.raises(HwmCoverageError, match="diverges from recomputation"):
        verify_governed_hwm_result(
            forged,
            input_generation_bytes=json.dumps(
                fx["generation"], sort_keys=True
            ).encode(),
            calendar_bytes=fx["calendar_authority_bytes"],
            input_receipt_bytes=fx["input_receipt_bytes"],
            policy_bytes=fx["policy_bytes"],
            product_contract_bytes=fx["product_contract_bytes"],
            performance_policy_bytes=fx["performance_policy_bytes"],
            legacy_policy_bytes=fx["legacy_policy_bytes"],
        )


def test_verifier_decimal_equivalence_keeps_metadata_and_types_exact():
    """Only recomputed Decimal leaves permit persisted scale variance."""
    from quant_engine.portfolio_ledger.hwm_governed_execution import (
        _equivalent_result_value,
    )

    assert _equivalent_result_value("1", Decimal("1.00"))
    assert _equivalent_result_value("0E-29", Decimal("0E-29"))
    assert not _equivalent_result_value("2", Decimal("1.00"))
    assert not _equivalent_result_value("NaN", Decimal("0"))
    assert not _equivalent_result_value("1_0", Decimal("10"))
    assert not _equivalent_result_value(False, 0)
    assert not _equivalent_result_value("1", "1.0")


def test_verifier_rejects_bool_for_zero_integer_metadata(tmp_path):
    """JSON bool cannot impersonate the recomputed integer age count."""
    fx = _fixture(tmp_path)
    result = _run(fx)
    assert result["calculation_age_days"] == 0
    forged = json.loads(json.dumps(result, default=str))
    forged["calculation_age_days"] = False

    with pytest.raises(HwmCoverageError, match="diverges from recomputation"):
        verify_governed_hwm_result(
            forged,
            input_generation_bytes=json.dumps(
                fx["generation"], sort_keys=True
            ).encode(),
            calendar_bytes=fx["calendar_authority_bytes"],
            input_receipt_bytes=fx["input_receipt_bytes"],
            policy_bytes=fx["policy_bytes"],
            product_contract_bytes=fx["product_contract_bytes"],
            performance_policy_bytes=fx["performance_policy_bytes"],
            legacy_policy_bytes=fx["legacy_policy_bytes"],
        )


def test_verifier_accepts_persisted_equivalent_decimal_text(tmp_path, monkeypatch):
    """JSON readback remains valid when recomputation has trailing zeros."""
    import quant_engine.portfolio_ledger.hwm_governed_execution as governed
    from quant_engine.portfolio_ledger.hwm import _json_safe

    original_policy_bytes = _policy_bytes

    def bound_policy_bytes(*args, **kwargs):
        policy = json.loads(original_policy_bytes(*args, **kwargs))
        policy["product_authority"]["raw_sha256"] = hashlib.sha256(
            PRODUCT_CONTRACT_BYTES
        ).hexdigest()
        policy["product_authority"]["semantic_digest"] = hashlib.sha256(
            _canonical_json_bytes(json.loads(PRODUCT_CONTRACT_BYTES))
        ).hexdigest()
        return json.dumps(policy, indent=2, sort_keys=True).encode()

    monkeypatch.setitem(globals(), "_policy_bytes", bound_policy_bytes)
    fx = _fixture(tmp_path)
    result = _run(fx)
    persisted = json.loads(json.dumps(_json_safe(result)))
    original_compute = governed.compute_governed_as_held_composite_hwm

    def recompute_with_equivalent_decimal_text(*args, **kwargs):
        recomputed = original_compute(*args, **kwargs)
        recomputed["current_index"] = Decimal(f"{recomputed['current_index']}0")
        return recomputed

    monkeypatch.setattr(
        governed,
        "compute_governed_as_held_composite_hwm",
        recompute_with_equivalent_decimal_text,
    )

    verified = governed.verify_governed_hwm_result(
        persisted,
        input_generation_bytes=json.dumps(fx["generation"], sort_keys=True).encode(),
        calendar_bytes=fx["calendar_authority_bytes"],
        input_receipt_bytes=fx["input_receipt_bytes"],
        policy_bytes=fx["policy_bytes"],
        product_contract_bytes=fx["product_contract_bytes"],
        performance_policy_bytes=fx["performance_policy_bytes"],
        legacy_policy_bytes=fx["legacy_policy_bytes"],
    )
    assert _json_safe(verified)["current_index"] == persisted["current_index"]

    changed = json.loads(json.dumps(persisted))
    changed["current_index"] = "999"
    with pytest.raises(HwmCoverageError, match="diverges from recomputation"):
        governed.verify_governed_hwm_result(
            changed,
            input_generation_bytes=json.dumps(
                fx["generation"], sort_keys=True
            ).encode(),
            calendar_bytes=fx["calendar_authority_bytes"],
            input_receipt_bytes=fx["input_receipt_bytes"],
            policy_bytes=fx["policy_bytes"],
            product_contract_bytes=fx["product_contract_bytes"],
            performance_policy_bytes=fx["performance_policy_bytes"],
            legacy_policy_bytes=fx["legacy_policy_bytes"],
        )


def test_verifier_accepts_member_coverage_beyond_common_complete_boundary(
    tmp_path, monkeypatch
):
    """A longer member tail does not move the accepted common boundary."""
    from quant_engine.portfolio_ledger.hwm import _json_safe

    original_policy_bytes = _policy_bytes

    def bound_policy_bytes(*args, **kwargs):
        policy = json.loads(original_policy_bytes(*args, **kwargs))
        policy["product_authority"]["raw_sha256"] = hashlib.sha256(
            PRODUCT_CONTRACT_BYTES
        ).hexdigest()
        policy["product_authority"]["semantic_digest"] = hashlib.sha256(
            _canonical_json_bytes(json.loads(PRODUCT_CONTRACT_BYTES))
        ).hexdigest()
        return json.dumps(policy, indent=2, sort_keys=True).encode()

    monkeypatch.setitem(globals(), "_policy_bytes", bound_policy_bytes)
    fx = _fixture(tmp_path)
    tail = _period("a-daily", "2026-01-07", "2026-01-08", "0.01", "64")
    fx["component_periods"].append(tail)
    fx["generation"]["component_periods"].append(tail)
    canonical = json.dumps(
        {
            key: value
            for key, value in fx["generation"].items()
            if key != "generation_evidence_sha256"
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    fx["generation"]["generation_evidence_sha256"] = hashlib.sha256(
        canonical
    ).hexdigest()

    result = _run(fx)
    persisted = json.loads(json.dumps(_json_safe(result)))
    verified = verify_governed_hwm_result(
        persisted,
        input_generation_bytes=json.dumps(fx["generation"], sort_keys=True).encode(),
        calendar_bytes=fx["calendar_authority_bytes"],
        input_receipt_bytes=fx["input_receipt_bytes"],
        policy_bytes=fx["policy_bytes"],
        product_contract_bytes=fx["product_contract_bytes"],
        performance_policy_bytes=fx["performance_policy_bytes"],
        legacy_policy_bytes=fx["legacy_policy_bytes"],
    )
    assert verified["complete_through"] == "2026-01-07"


def test_verifier_rejects_truncated_presented_grid(tmp_path):
    """P1-B: dropping a grid suffix (with recomputed hash) fails because the
    presented grid must agree with the generation's period coverage."""
    fx = _fixture(tmp_path)
    result = _run(fx)
    truncated = json.loads(json.dumps(result, default=str))
    truncated["input_evidence"]["accepted_intervals"] = truncated["input_evidence"][
        "accepted_intervals"
    ][2:]
    canonical = json.dumps(
        {k: v for k, v in truncated.items() if k != "receipt_sha256"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    from quant_engine.portfolio_ledger.hwm import _json_safe

    normalized = _json_safe(truncated)
    canonical = json.dumps(
        {k: v for k, v in normalized.items() if k != "receipt_sha256"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    truncated["receipt_sha256"] = hashlib.sha256(canonical).hexdigest()
    with pytest.raises(HwmCoverageError):
        verify_governed_hwm_result(
            truncated,
            input_generation_bytes=json.dumps(
                fx["generation"], sort_keys=True
            ).encode(),
            calendar_bytes=fx["calendar_authority_bytes"],
            input_receipt_bytes=fx["input_receipt_bytes"],
            policy_bytes=fx["policy_bytes"],
            product_contract_bytes=fx["product_contract_bytes"],
            performance_policy_bytes=fx["performance_policy_bytes"],
            legacy_policy_bytes=fx["legacy_policy_bytes"],
        )


# --- FXB-1 / H1: the executor contract the governed HWM preparation relies on. ----
#
# When one member's USD value basis ends at the admitted FX end while the others run
# to the cutoff, preparation ends the governed grid at the common complete date. The
# executor is unchanged: these tests pin that it still fails closed on the full grid
# and accepts the truncated one. The grid comes from preparation's own
# ``_accepted_intervals`` so the producer and the real executor meet here.


def _early_ending_member(tmp_path, *, ends="2026-01-05"):
    """The default fixture with member B's periods cut off after ``ends``.

    Member A keeps trading to the end of the fixture, as every other strategy does
    when one strategy's USD value basis stops at the admitted FX end.
    """
    fx = _fixture(tmp_path)
    periods = [
        row
        for row in fx["component_periods"]
        if row["strategy_id"] == "a-daily" or row["interval_end"] <= ends
    ]
    generation = _generation(
        periods,
        policy_binding=hashlib.sha256(fx["policy_bytes"]).hexdigest(),
        calendar_binding=fx["calendar_accepted_sha256"],
        receipt_binding=hashlib.sha256(fx["input_receipt_bytes"]).hexdigest(),
    )
    return fx, periods, generation


def _verify(fx, generation, result):
    from quant_engine.portfolio_ledger.hwm import _json_safe

    return verify_governed_hwm_result(
        json.loads(json.dumps(_json_safe(result))),
        input_generation_bytes=json.dumps(generation, sort_keys=True).encode(),
        calendar_bytes=fx["calendar_authority_bytes"],
        input_receipt_bytes=fx["input_receipt_bytes"],
        policy_bytes=fx["policy_bytes"],
        product_contract_bytes=fx["product_contract_bytes"],
        performance_policy_bytes=fx["performance_policy_bytes"],
        legacy_policy_bytes=fx["legacy_policy_bytes"],
    )


def test_a_member_ending_early_fails_the_full_grid_closed(tmp_path):
    """The grid to the cutoff leaves B's last days uncovered, so the executor raises."""
    fx, periods, generation = _early_ending_member(tmp_path)

    with pytest.raises(
        HwmCoverageError,
        match=(
            r"membership coverage differs; uncovered governed member-period: "
            r"b-session 2026-01-05\.\.2026-01-06"
        ),
    ):
        _run(fx, component_periods=periods, generation=generation)


def test_the_grid_truncated_at_the_common_complete_date_is_accepted(tmp_path):
    """The grid to B's last day computes, ages from that day, and verifies."""
    import portfolio_attended_preparation as preparation

    fx, periods, generation = _early_ending_member(tmp_path)
    calendar = json.loads(fx["calendar_authority_bytes"])
    grid = preparation._accepted_intervals(generation, calendar, "2026-01-05")
    assert [row["interval_end"] for row in grid] == [
        "2026-01-03",
        "2026-01-04",
        "2026-01-05",
    ]

    result = _run(
        fx,
        accepted_intervals=grid,
        component_periods=periods,
        generation=generation,
        complete_through="2026-01-05",
        evaluated_on="2026-01-07",
        maximum_age_days=1,
    )

    assert result["status"] == "AVAILABLE"
    assert result["complete_through"] == "2026-01-05"
    assert result["calculation_age_days"] == 2
    assert result["age_status"] == "COMPLETE_DATA_AGE_WARNING"
    assert result["warnings"] == ["HWM_COMPLETE_DATA_AGE"]
    assert [row["interval_end"] for row in result["periods"]] == [
        "2026-01-03",
        "2026-01-04",
        "2026-01-05",
    ]
    # A's rows after the boundary stay in the generation, outside the grid.
    assert max(row["interval_end"] for row in generation["component_periods"]) == (
        "2026-01-07"
    )
    verified = _verify(fx, generation, result)
    assert verified["receipt_sha256"] == result["receipt_sha256"]


def test_the_truncated_grid_does_not_age_when_the_data_is_current(tmp_path):
    """Within the maximum age the truncated result is CURRENT, not a warning."""
    import portfolio_attended_preparation as preparation

    fx, periods, generation = _early_ending_member(tmp_path)
    calendar = json.loads(fx["calendar_authority_bytes"])
    grid = preparation._accepted_intervals(generation, calendar, "2026-01-05")

    result = _run(
        fx,
        accepted_intervals=grid,
        component_periods=periods,
        generation=generation,
        complete_through="2026-01-05",
        evaluated_on="2026-01-07",
        maximum_age_days=2,
    )

    assert result["age_status"] == "CURRENT"
    assert result["warnings"] == []


def test_an_interior_gap_inside_the_truncated_grid_still_fails_closed(tmp_path):
    """Truncating at the common date never excuses a member's missing day inside it."""
    import portfolio_attended_preparation as preparation

    fx, periods, _generation_unused = _early_ending_member(tmp_path)
    gapped = [
        row
        for row in periods
        if not (row["strategy_id"] == "a-daily" and row["interval_end"] == "2026-01-04")
    ]
    generation = _generation(
        gapped,
        policy_binding=hashlib.sha256(fx["policy_bytes"]).hexdigest(),
        calendar_binding=fx["calendar_accepted_sha256"],
        receipt_binding=hashlib.sha256(fx["input_receipt_bytes"]).hexdigest(),
    )
    calendar = json.loads(fx["calendar_authority_bytes"])
    grid = preparation._accepted_intervals(generation, calendar, "2026-01-05")

    with pytest.raises(
        HwmCoverageError,
        match=(
            r"membership coverage differs; uncovered governed member-period: "
            r"a-daily 2026-01-03\.\.2026-01-04"
        ),
    ):
        _run(
            fx,
            accepted_intervals=grid,
            component_periods=gapped,
            generation=generation,
            complete_through="2026-01-05",
        )


def test_the_executor_still_requires_the_grid_to_end_at_complete_through(tmp_path):
    """complete_through is the grid's own end: a cutoff label cannot hide a short grid."""
    import portfolio_attended_preparation as preparation

    fx, periods, generation = _early_ending_member(tmp_path)
    calendar = json.loads(fx["calendar_authority_bytes"])
    grid = preparation._accepted_intervals(generation, calendar, "2026-01-05")

    with pytest.raises(HwmCoverageError, match="complete_through differs"):
        _run(
            fx,
            accepted_intervals=grid,
            component_periods=periods,
            generation=generation,
            complete_through="2026-01-07",
        )


def _without_friday_row(fx):
    """B has no Friday one-day row: its first period is the Fri->Mon accepted span."""
    periods = [
        row
        for row in fx["component_periods"]
        if not (
            row["strategy_id"] == "b-session"
            and row["interval_start"] == "2026-01-02"
            and row["interval_end"] == "2026-01-03"
        )
    ]
    old = fx["generation"]
    generation = _generation(
        periods,
        policy_binding=old["policy_binding_sha256"],
        calendar_binding=old["calendar_sha256"],
        receipt_binding=old["upstream_receipt_sha256"],
    )
    return periods, generation


def test_member_opening_before_a_weekend_takes_the_opening_capital_on_its_first_day(
    tmp_path,
):
    """The first accepted span starts at the membership start: its capital anchors the zero days."""
    fx = _fixture(tmp_path)
    periods, generation = _without_friday_row(fx)
    result = _run(fx, component_periods=periods, generation=generation)
    saturday = result["periods"][0]
    sunday = result["periods"][1]
    b_sat = [c for c in saturday["components"] if c["strategy_id"] == "b-session"][0]
    b_sun = [c for c in sunday["components"] if c["strategy_id"] == "b-session"][0]
    # the capital of the period starting at the membership start (the span's)
    assert b_sat["beginning_capital_usd"] == Decimal("40.2")
    assert b_sat["period_return"] == Decimal("0")
    assert b_sat["source"] == "AUTHORITY_DERIVED_ZERO"
    # the next day carries the previous day's end capital (a zero return keeps it)
    assert b_sun["beginning_capital_usd"] == Decimal("40.2")
    assert b_sun["source"] == "AUTHORITY_DERIVED_ZERO"


def test_no_period_at_the_membership_start_still_refuses_the_zero_day(tmp_path):
    """A member whose first period does not start at its membership start has no anchor."""
    fx = _fixture(tmp_path)
    periods, generation = _without_friday_row(fx)
    # B becomes a member on Saturday 2026-01-03; its only span starts on Friday
    memberships = [
        {**m, "effective_start": "2026-01-03"} if m["strategy_id"] == "b-session" else m
        for m in fx["memberships"]
    ]
    generation = {**generation, "memberships": memberships}
    generation.pop("generation_evidence_sha256")
    canonical = json.dumps(generation, sort_keys=True, separators=(",", ":")).encode()
    generation["generation_evidence_sha256"] = hashlib.sha256(canonical).hexdigest()
    with pytest.raises(
        HwmCoverageError, match="derived-zero day lacks an opening anchor"
    ):
        _run(
            fx,
            memberships=memberships,
            component_periods=periods,
            generation=generation,
        )
