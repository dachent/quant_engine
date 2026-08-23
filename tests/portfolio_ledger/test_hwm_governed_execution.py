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
    HwmCoverageError,
    _canonical_json_bytes,
)
from quant_engine.portfolio_ledger.hwm_governed_execution import (
    compute_governed_as_held_composite_hwm,
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


def _policy_bytes(ready=True):
    """Real policy template with only the governed calendar seam flipped."""
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


def _authority_doc():
    """Two sources: A always-on; B session source with one accepted weekend."""
    doc = {
        "schema_version": 2,
        "authority_class": "ACCEPTED_GOVERNED",
        "calendar_policy_id": "GOV-CAL-TEST",
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
        "return_basis": "ACCOUNT_LIVE_NET",
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


def _fixture(tmp_path, ready=True):
    policy_bytes = _policy_bytes(ready=ready)
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
