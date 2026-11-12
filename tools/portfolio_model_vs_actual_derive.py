"""Derive the model-vs-actual audit input artifact from hash-pinned inputs.

Reads a declared-inputs spec (every input path with its expected sha256,
plus the recipe that maps each strategy's windows and components), refuses on
any hash mismatch, builds the ``model-vs-actual-audit-bridges`` artifact that
``tools/portfolio_model_vs_actual.py`` consumes, gates it on the consumer's
own validator and on per-window closure, and writes the artifact and a
manifest as LF bytes. Stdlib only; no network; no wall clock: the audit date
is a spec field. Key order is inherited from the carried previous artifact
(insertion order), so a re-run on the same inputs is byte-identical.
"""

from __future__ import annotations

import argparse
import bisect
import copy
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sys
from typing import Any
import zipfile


import portfolio_model_vs_actual as mva  # noqa: E402

SPEC_ARTIFACT = "model-vs-actual-derive-spec"
MANIFEST_ARTIFACT = "model-vs-actual-audit-bridges-derive-manifest"
SCHEMA_VERSION = 1
RUN_ARTIFACT = "audit-input.json"
RUN_MANIFEST = "audit-input.manifest.json"


class DeriveError(ValueError):
    """An input, the recipe or the derived artifact violates the contract."""


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class Inputs:
    """Declared inputs, each verified against its pin before any parse."""

    def __init__(self, declared: dict[str, Any], root: Path) -> None:
        self.raw: dict[str, bytes] = {}
        self.meta: dict[str, dict[str, Any]] = {}
        self._json: dict[str, Any] = {}
        for key in sorted(declared):
            entry = declared[key]
            path = Path(entry["path"])
            path = path if path.is_absolute() else root / path
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise DeriveError(f"input {key} cannot be read") from exc
            if sha256_hex(raw) != entry["sha256"]:
                raise DeriveError(f"input {key} sha256 differs from its pin")
            self.raw[key] = raw
            self.meta[key] = entry

    def need(self, key: Any) -> bytes:
        if key not in self.raw:
            raise DeriveError(f"recipe names an undeclared input {key!r}")
        return self.raw[key]

    def json(self, key: str) -> Any:
        raw = self.need(key)
        if key not in self._json:
            try:
                self._json[key] = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise DeriveError(f"input {key} is not valid JSON") from exc
        return self._json[key]

    def ref(self, key: str) -> tuple[str, str]:
        self.need(key)
        entry = self.meta[key]
        return entry.get("ref_label") or Path(entry["path"]).name, entry["sha256"]


def _comp(
    cid: str,
    label: str,
    pp: float,
    ref: tuple[str, str],
    cols: list[str],
    tag: str = "D",
    est: bool = False,
) -> dict[str, Any]:
    return {
        "component_id": cid,
        "label": label,
        "pp": round(pp, 4),
        "usd": None,
        "origin_tag": tag,
        "estimated": est,
        "uncertainty_pp_1sigma": None,
        "source_ref": {"file": ref[0], "sha256": ref[1], "columns": cols},
        "replacing_pull": None,
        "sub_components": [],
    }


def _days(a: str, b: str) -> int:
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days + 1


class _Ctx:
    def __init__(self, inputs: Inputs, recipe: dict[str, Any]) -> None:
        self.inputs = inputs
        self.recipe = recipe
        self._ledger: Any = None

    def ledger(self) -> Any:
        if self._ledger is None:
            self._ledger = self.inputs.json(self.recipe["ledger_input"])
        return self._ledger

    def avg_nav(self, account: str, a: str, b: str) -> float:
        vals = [
            float(r["nav"]["value"])
            for r in self.ledger()["nav_observations"]
            if r["account_id"] == account
            and r["nav"].get("value")
            and a <= r["as_of_date"] <= b
        ]
        if not vals:
            raise DeriveError(f"no ledger NAV for {account} in {a}..{b}")
        return sum(vals) / len(vals)

    def returns(self, strategy_id: str) -> dict[str, float]:
        return {
            x["date"]: float(x["return"])
            for x in self.ledger()["strategy_returns"]
            if x["strategy_id"] == strategy_id
        }


def _window(s: dict[str, Any], window_id: str) -> dict[str, Any]:
    for w in s["windows"]:
        if w["window_id"] == window_id:
            found: dict[str, Any] = w
            return found
    raise DeriveError(f"window {window_id} not found for {s['strategy_id']}")


def _op_window_bridge(c: _Ctx, s: dict[str, Any], op: dict[str, Any]) -> None:
    bridge = c.inputs.json(op["input"])
    ref = c.inputs.ref(op["input"])
    drop = set(op.get("drop_windows", []))
    s["windows"] = [w for w in s["windows"] if w["window_id"] not in drop]
    for spec in op["windows"]:
        if spec["key"] not in bridge:
            raise DeriveError(f"bridge has no window {spec['key']!r}")
        w = bridge[spec["key"]]
        cp = w["components_pp"]
        comps = []
        for cs in op["components"]:
            missing = [k for k in cs["sources"] if k not in cp]
            if missing:
                raise DeriveError(f"bridge component {missing[0]!r} is missing")
            comps.append(
                _comp(
                    cs["component_id"],
                    cs["label"],
                    sum(cp[k] for k in cs["sources"]),
                    ref,
                    list(cs["sources"]),
                )
            )
        a, b = spec["start"], spec["end"]
        s["windows"].append(
            {
                "window_id": spec["window_id"],
                "label": f"{spec['label']}, {w['days']} days",
                "kind": spec["kind"],
                "start": a,
                "end": b,
                "days": w["days"],
                "account_return_pct": round(w["account_pct"], 4),
                "model_return_pct": round(w[op["model_key"]], 4),
                "model_currency": "USD",
                "account_currency": "USD",
                "audit_gap_pp": round(w["gap_pp"], 4),
                "view": op["view"],
                "memo": [],
                "components": comps,
                "capital_base_usd": round(c.avg_nav(op["account_id"], a, b), 3),
                "capital_base_basis": op["capital_basis"],
            }
        )


def _op_ln_waterfall(c: _Ctx, s: dict[str, Any], op: dict[str, Any]) -> None:
    rb = c.inputs.json(op["input"])
    ref = c.inputs.ref(op["input"])
    old = s["windows"][0]
    gap = rb["acct_simple"] - rb["gross_simple"]
    lnsum = sum(rb[k] for k in op["scale_terms"])
    if lnsum == 0:
        raise DeriveError("ln waterfall scale terms sum to zero")
    k = gap / lnsum
    comps = [
        _comp(
            cs["component_id"],
            cs["label"],
            sum(rb[t] for t in cs["sources"]) * k * 100,
            ref,
            list(cs["sources"]),
        )
        for cs in op["components"]
    ]
    a, b = op["start"], op["end"]
    win = {
        "window_id": op["window_id"],
        "label": f"{op['label']}, {rb['days']} days",
        "kind": op["kind"],
        "start": a,
        "end": b,
        "days": rb["days"],
        "account_return_pct": round(rb["acct_simple"] * 100, 4),
        "model_return_pct": round(rb["gross_simple"] * 100, 4),
        "model_currency": "USD",
        "account_currency": "USD",
        "audit_gap_pp": round(gap * 100, 4),
        "view": old.get("view", op["default_view"]),
        "memo": [],
        "components": comps,
        "capital_base_usd": round(c.avg_nav(op["account_id"], a, b), 3),
        "capital_base_basis": op["capital_basis"],
    }
    s["windows"] = [win]


def _op_adjclose_extension(c: _Ctx, s: dict[str, Any], op: dict[str, Any]) -> None:
    zref = c.inputs.ref(op["input"])
    try:
        with zipfile.ZipFile(io.BytesIO(c.inputs.need(op["input"]))) as z:
            chart = json.loads(z.read(op["member"]).decode("utf-8"))
    except (KeyError, zipfile.BadZipFile, ValueError) as exc:
        raise DeriveError("price archive member cannot be read") from exc
    res = chart["chart"]["result"][0]
    stamps = [
        dt.datetime.fromtimestamp(x, dt.timezone.utc).date().isoformat()
        for x in res["timestamp"]
    ]
    adj = dict(zip(stamps, res["indicators"]["adjclose"][0]["adjclose"]))
    keys = sorted(adj)

    def at(day: str) -> float:
        i = bisect.bisect_right(keys, day)
        if i == 0:
            raise DeriveError(f"no price on or before {day}")
        return float(adj[keys[i - 1]])

    lo, hi = op["after"], op["end"]
    rets = c.returns(s["strategy_id"])
    acc = 1.0
    for d in sorted(rets):
        if lo < d <= hi:
            acc *= 1 + rets[d]
    acc -= 1
    mod = at(hi) / at(lo) - 1
    base = _window(s, op["base_window"])
    acc_full = (1 + base["account_return_pct"] / 100) * (1 + acc) - 1
    mod_full = (1 + base["model_return_pct"] / 100) * (1 + mod) - 1
    pull = op["replacing_pull"]

    def vcomp(cid: str, label: str, pp: float, cols: list[str]) -> dict[str, Any]:
        comp = _comp(cid, label, pp, zref, cols, tag="I", est=True)
        comp["replacing_pull"] = pull
        return comp

    gap3 = (acc - mod) * 100
    n3 = len([d for d in rets if lo < d <= hi])
    w3 = {
        "window_id": op["new_window"],
        "label": f"{op['new_label']}",
        "kind": "EPOCH",
        "start": op["start"],
        "end": hi,
        "days": n3,
        "account_return_pct": round(acc * 100, 4),
        "model_return_pct": round(mod * 100, 4),
        "model_currency": "USD",
        "account_currency": "USD",
        "audit_gap_pp": round(gap3, 4),
        "view": base.get("view"),
        "memo": [],
        "components": [
            vcomp("other", op["epoch_label"], gap3, [op["column"]]),
        ],
        "capital_base_usd": base.get("capital_base_usd"),
        "capital_base_basis": base.get("capital_base_basis"),
    }
    gapf = (acc_full - mod_full) * 100
    bc = copy.deepcopy(base["components"])
    lk = gapf - sum(x["pp"] for x in bc) - gap3
    full = copy.deepcopy(base)
    full.update(
        window_id=op["full_window"],
        label=f"{op['full_label']}, {base['days'] + n3} days",
        end=hi,
        days=base["days"] + n3,
        account_return_pct=round(acc_full * 100, 4),
        model_return_pct=round(mod_full * 100, 4),
        audit_gap_pp=round(gapf, 4),
        components=bc
        + [
            vcomp("other", op["full_epoch_label"], gap3, [op["column"]]),
            vcomp("linking", op["linking_label"], lk, list(op["linking_columns"])),
        ],
    )
    s["windows"].append(w3)
    s["windows"].append(full)
    s["primary_window_id"] = op["full_window"]
    s["unavailable"] = []


def _epoch_components(
    c: _Ctx, op: dict[str, Any], epoch: dict[str, Any], ref: tuple[str, str]
) -> list[dict[str, Any]]:
    spec = op["components"]
    rows = epoch[spec["list_key"]]
    out: dict[str, dict[str, Any]] = {}
    for x in rows:
        raw_id = x[spec["id_key"]]
        tag = x[spec["tag_key"]]
        if "id_map" in spec:
            if raw_id not in spec["id_map"]:
                raise DeriveError(f"unknown epoch component {raw_id!r}")
            cid, label = spec["id_map"][raw_id]
            cols = [spec["columns"]]
        else:
            cid = raw_id
            label = spec["label_prefix"] + raw_id
            cols = [spec["columns_prefix"] + raw_id]
        if cid in out:
            out[cid]["pp"] = round(out[cid]["pp"] + x[spec["pp_key"]], 4)
        else:
            out[cid] = _comp(
                cid, label, x[spec["pp_key"]], ref, cols, tag=tag, est=(tag == "I")
            )
    return list(out.values())


def _op_extend_epoch(c: _Ctx, s: dict[str, Any], op: dict[str, Any]) -> None:
    src = c.inputs.json(op["input"])
    ref = c.inputs.ref(op["input"])
    epoch = src
    for part in op["epoch_path"].split("."):
        epoch = epoch[part]
    comps = _epoch_components(c, op, epoch, ref)
    a, b = op["start"], op["end"]
    days = epoch["days"] if op.get("days_from_source") else _days(a, b)
    acc_pct, mod_pct = epoch[op["account_key"]], epoch[op["model_key"]]
    acc, mod = acc_pct / 100, mod_pct / 100
    old = _window(s, op["base_window"])
    new = {
        "window_id": op["new_window"],
        "label": f"{op['label']}, {days} days",
        "kind": "EPOCH",
        "start": a,
        "end": b,
        "days": days,
        "account_return_pct": round(acc_pct, 4),
        "model_return_pct": round(mod_pct, 4),
        "model_currency": "USD",
        "account_currency": "USD",
        "audit_gap_pp": round((acc - mod) * 100, 4),
        "view": old.get("view"),
        "memo": [],
        "components": comps,
        "capital_base_usd": old.get("capital_base_usd"),
        "capital_base_basis": old.get("capital_base_basis"),
    }
    old_days = old.get("days") or _days(old["start"], old["end"])
    acc_f = (1 + old["account_return_pct"] / 100) * (1 + acc) - 1
    mod_f = (1 + old["model_return_pct"] / 100) * (1 + mod) - 1
    gap_f = (acc_f - mod_f) * 100
    oc = copy.deepcopy(old["components"])
    lk = gap_f - sum(x["pp"] for x in oc) - sum(x["pp"] for x in comps)
    full = copy.deepcopy(old)
    full.update(
        window_id=op["full_window"],
        label=f"Aligned window to cutoff, {old_days} days + {days} days",
        end=b,
        days=old_days + days,
        account_return_pct=round(acc_f * 100, 4),
        model_return_pct=round(mod_f * 100, 4),
        audit_gap_pp=round(gap_f, 4),
        components=oc
        + copy.deepcopy(comps)
        + [_comp("linking", op["linking_label"], lk, ref, ["linking"], tag="D")],
    )
    s["windows"].append(new)
    s["windows"].append(full)
    s["primary_window_id"] = op["full_window"]
    s["unavailable"] = []


def _op_set_notes(c: _Ctx, s: dict[str, Any], op: dict[str, Any]) -> None:
    s["notes"] = list(op["notes"])


def _op_append_notes(c: _Ctx, s: dict[str, Any], op: dict[str, Any]) -> None:
    s["notes"] = list(s["notes"]) + list(op["notes"])


def _op_clear_unavailable(c: _Ctx, s: dict[str, Any], op: dict[str, Any]) -> None:
    s["unavailable"] = []


_OPS = {
    "window_bridge": _op_window_bridge,
    "ln_waterfall": _op_ln_waterfall,
    "adjclose_extension": _op_adjclose_extension,
    "extend_epoch": _op_extend_epoch,
    "set_notes": _op_set_notes,
    "append_notes": _op_append_notes,
    "clear_unavailable": _op_clear_unavailable,
}


def _clear_pull(obj: Any, done: set[str]) -> None:
    if isinstance(obj, dict):
        if obj.get("replacing_pull") in done:
            obj["replacing_pull"] = None
        for v in obj.values():
            _clear_pull(v, done)
    elif isinstance(obj, list):
        for v in obj:
            _clear_pull(v, done)


def check_closure(artifact: dict[str, Any]) -> None:
    """Refuse a window whose components do not sum to its gap within tolerance."""

    tol = float(artifact["closure_tolerance_pp"])
    for s in artifact["strategies"]:
        for w in s["windows"]:
            gap = w["account_return_pct"] - w["model_return_pct"]
            staged = w.get("audit_gap_pp")
            if staged is not None and abs(gap - staged) > tol:
                raise DeriveError(
                    f"window {s['strategy_id']}/{w['window_id']} audit gap "
                    "differs from account minus model"
                )
            resid = gap - sum(x["pp"] for x in w["components"])
            if abs(resid) > tol:
                raise DeriveError(
                    f"window {s['strategy_id']}/{w['window_id']} does not close: "
                    f"residual {resid:.4f} pp exceeds {tol} pp"
                )


def derive(spec: dict[str, Any], root: Path) -> tuple[bytes, dict[str, Any]]:
    """Return (artifact bytes, manifest) for a declared-inputs spec."""

    if (
        spec.get("artifact") != SPEC_ARTIFACT
        or spec.get("schema_version") != SCHEMA_VERSION
    ):
        raise DeriveError("spec artifact or schema_version is wrong")
    inputs = Inputs(spec["inputs"], root)
    recipe = spec["recipe"]
    ctx = _Ctx(inputs, recipe)
    new = copy.deepcopy(inputs.json(recipe["base_input"]))
    new["audit_written"] = spec["audit_written"]
    if "as_of" in spec:
        try:
            dt.date.fromisoformat(spec["as_of"])
        except (TypeError, ValueError) as exc:
            raise DeriveError("spec as_of is not an ISO date") from exc
        new["as_of"] = spec["as_of"]
    by_id = {s["strategy_id"]: s for s in new["strategies"]}
    for entry in recipe["strategies"]:
        sid = entry["strategy_id"]
        if sid not in by_id:
            raise DeriveError(f"recipe names an unknown strategy {sid!r}")
        for op in entry["ops"]:
            fn = _OPS.get(op["op"])
            if fn is None:
                raise DeriveError(f"unknown recipe op {op['op']!r}")
            fn(ctx, by_id[sid], op)
    done = set(recipe.get("completed_pulls", []))
    new["pulls"] = [p for p in new["pulls"] if p["pull_id"] not in done]
    _clear_pull(new["strategies"], done)
    check_closure(new)
    raw = json.dumps(new, indent=1).encode("utf-8")
    try:
        mva.parse_audit_input(raw, sha256_hex(raw))
    except mva.ModelVsActualError as exc:
        raise DeriveError(f"derived artifact refused by the consumer: {exc}") from exc
    manifest = {
        "artifact": MANIFEST_ARTIFACT,
        "schema_version": SCHEMA_VERSION,
        "audit_written": spec["audit_written"],
        "inputs": {
            k: {"ref_label": inputs.ref(k)[0], "sha256": inputs.ref(k)[1]}
            for k in sorted(inputs.raw)
        },
        **({"as_of": spec["as_of"]} if "as_of" in spec else {}),
        "output": {"bytes": len(raw), "sha256": sha256_hex(raw)},
        "pass_flags": "--model-vs-actual-input <artifact> "
        "--model-vs-actual-input-sha256 " + sha256_hex(raw),
    }
    return raw, manifest


def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--spec", required=True, type=Path)
    p.add_argument("--root", type=Path, help="base for relative input paths")
    p.add_argument("--out", type=Path)
    p.add_argument("--manifest", type=Path)
    p.add_argument(
        "--run-dir",
        type=Path,
        help="write audit-input.json and audit-input.manifest.json here",
    )
    args = p.parse_args(argv)
    if args.run_dir is not None:
        if args.out or args.manifest:
            p.error("--run-dir excludes --out and --manifest")
        args.run_dir.mkdir(parents=True, exist_ok=True)
        args.out = args.run_dir / RUN_ARTIFACT
        args.manifest = args.run_dir / RUN_MANIFEST
    elif not (args.out and args.manifest):
        p.error("give --run-dir, or both --out and --manifest")
    try:
        spec = json.loads(args.spec.read_bytes().decode("utf-8"))
        root = args.root or args.spec.resolve().parent
        raw, manifest = derive(spec, root)
    except (DeriveError, KeyError, OSError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    args.out.write_bytes(raw)
    args.manifest.write_bytes(manifest_bytes(manifest))
    print(manifest["output"]["sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
