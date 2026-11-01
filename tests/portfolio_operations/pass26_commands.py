"""Pass 26's run configuration, derived ids and recorded command lines, written to a directory.

Taken from the retained pass-26 scripts (evidence/releases/2026-10-26-r2/r26/scripts: chain_p26.sh,
run_attended.sh, qa_only.sh, resume.sh, t_tail1/2.sh, t_replica_copy/spec.sh, t_lanes.sh).
Built in code, not stored as tracked fixtures, because the data-boundary policy allows none.
"""

import json
from pathlib import Path


def write(OUT: Path) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    R = "C:/Dev/scratch/qe-p26b"
    C = R + "/c"
    RUN = C + "/out2"
    QA = C + "/qa2"
    Q = "C:/Users/BorisVaisman/V Capital/investing - Documents/_quant_engine"
    REPO = R + "/repo"
    PY = REPO + "/.venv/Scripts/python.exe"
    SHA = "1fa910efde7255244e7dee3d5c36ae880eadb562"
    LEDGER = "7cc8766d6bd63bf48e64bb007357ba2b16b51ae8749e94f9fc80b669ae65bfc1"
    BASE = "65148ce8472594f3bef2b737bed4845676ca710a1d4698ea1d85cda26975d16c"
    HIST = "0520f130f4f5566d706f60dc9c7d0687e7041a28c659336b89a6f01fade0eb69"
    ATT = "8eb16ae2684ed26aa733e81e55c90c84117341f59ae7dbaf785854f64cddef22"
    EPOCH = "e5ca0b23f30fd528c7084ce400fbae3a92482cf26358e9474d1f5731c62cb82f"
    PANEL = "6fc6b5cb888c2082a537db35e713ff1c5497190c9f4465450ed4bbb8c20bf02c"
    CAND = "873100ce6f49b5d2d89a0abe805aaea84b74d33b83ed68d236491f357f199bcc"

    config = {
        "repo_root": REPO,
        "python": PY,
        "cycle": C,
        "run": RUN,
        "qa": QA,
        "code_commit": SHA,
        "mode": "release",
        "pipeline_id": "r2-2026-10-27",
        "requested_as_of": "2026-09-17",
        "evaluated_on": "2026-10-27",
        "workspace_root": R + "/ws",
        "retained_cache_base": C + "/cachebase",
        "retained_cache_root": C + "/cache2",
        "preparation_spec": C + "/in/spec.json",
        "selected_inputs": C + "/in/selected-inputs.json",
        "history_store": Q
        + "/dataPortfolio/rollforward/2026-09-10-mvp2-live-only/layered-history",
        "expect_superseded": 13,
        "build_portfolio_root": R + "/ws/dataPortfolio",
        "portfolio_root": Q + "/dataPortfolio",
        "source_workspace": Q,
        "replica_workspace": R + "/replica/_quant_engine",
        "playwright_module": R + "/qa-node/node_modules/playwright/index.mjs",
        "playwright_executable": "C:/Users/BorisVaisman/AppData/Local/ms-playwright/chromium-1200/chrome-win64/chrome.exe",
        "evidence_dest": "C:/unused/evidence",
        "pre_chain_script": R + "/work/pre_chain.py",
        "pre_chain_sha256": "0" * 64,
    }
    ids = {
        "ledger_id": LEDGER,
        "epoch_id": EPOCH,
        "baseline_id": BASE,
        "history_id": HIST,
        "attribution_id": ATT,
        "cycle_ledger_id": LEDGER,
        "candidate_id": CAND,
        "panel_gid": PANEL,
        "spec_panel_gid": PANEL,
        "panel_root": Q + "/dataPortfolio/control-panel",
    }
    (OUT / "pass26-run.json").write_text(
        json.dumps(config, indent=1) + "\n", encoding="utf-8"
    )
    (OUT / "pass26-ids.json").write_text(
        json.dumps(ids, indent=1) + "\n", encoding="utf-8"
    )

    MAT = REPO + "/tools/portfolio_pipeline_materialize.py"
    PRODSPEC = C + "/production/production-spec.json"
    HWM = [
        "--hwm-governed-result",
        RUN + "/preparation/hwm-governed-result.json",
        "--hwm-governed-input-generation",
        RUN + "/preparation/hwm-input-generation.json",
        "--hwm-governed-calendar-authority",
        RUN + "/preparation/hwm-calendar-authority.json",
        "--hwm-governed-admission-receipt",
        RUN + f"/stages/ledger/generations/{LEDGER}/admit-or-declare-receipt.json",
    ]
    RECIPE_COMMON = [
        "--vertical-recipe",
        RUN + "/preparation/release-recipe.json",
        "--proof-root",
        RUN,
        "--qa-dir",
        QA,
        "--pipeline-id",
        "r2-2026-10-27",
        "--financial-mvp-contract",
        "contracts/portfolio-financial-mvp-source-v1.json",
        "--financial-mvp-source-matrix",
        "generated/financial-mvp/source-matrix.json",
    ]
    ATT_HEAD = [
        PY,
        MAT,
        "attended",
        "--repo-root",
        REPO,
        "--python",
        PY,
        "--workspace-root",
        R + "/ws",
        "--output-root",
        RUN,
    ]
    TIMES = [
        "--requested-as-of",
        "2026-09-17",
        "--evaluated-on",
        "2026-10-27",
        "--preparation-spec",
        C + "/in/spec.json",
    ]
    QA_BASE = ["--expected-calculation-cutoff", "2026-09-17"]
    HTML = RUN + f"/stages/panel/candidates/{CAND}/index.html"
    rows = {
        "pre-chain": (
            "chain_p26.sh/chain_a21.sh:12 (pre_chain.py <cycle> <selected> <BG> <evaluated-on> <sha>)",
            [
                PY,
                "pre_chain.py",
                C,
                C + "/in/selected-inputs.json",
                BASE,
                "2026-10-27",
                SHA,
            ],
        ),
        "attended": (
            "run_attended.sh (cold, --qa-hold)",
            ATT_HEAD
            + [
                "--retained-cache-base",
                C + "/cachebase",
                "--retained-cache-root",
                C + "/cache2",
            ]
            + TIMES
            + ["--execute", "--qa-hold"],
        ),
        "qa-browser": (
            "qa_only.sh browser",
            [
                "node",
                REPO + "/apps/portfolio-control-panel/browser_qa.mjs",
                "--html",
                HTML,
                "--output",
                QA,
            ]
            + QA_BASE,
        ),
        "qa-interaction": (
            "qa_only.sh interaction",
            [
                "node",
                REPO + "/apps/portfolio-control-panel/interaction_qa.mjs",
                "--html",
                HTML,
                "--output",
                QA + "/interaction-results.json",
            ]
            + QA_BASE,
        ),
        "qa-native": (
            "qa_only.sh native",
            [
                PY,
                REPO + "/tools/portfolio_control_panel.py",
                "qa",
                "--candidate-dir",
                RUN + f"/stages/panel/candidates/{CAND}",
                "--qa-dir",
                QA,
            ],
        ),
        "resume": (
            "resume.sh",
            ATT_HEAD
            + ["--retained-cache-root", C + "/cache2"]
            + TIMES
            + ["--qa-dir", QA, "--resume", "--execute"],
        ),
        "recipe-probe": (
            "t_tail1.sh 2a",
            [PY, MAT, "recipe"]
            + RECIPE_COMMON
            + ["--output", C + "/pipeline-recipe-probe.json"],
        ),
        "registries": (
            "t_tail1.sh 2b",
            [
                PY,
                REPO + "/ops/mvp2-release/scripts/draft_correction_registries.py",
                "--run",
                RUN,
                "--pipeline-recipe",
                C + "/pipeline-recipe-probe.json",
                "--history-store",
                Q
                + "/dataPortfolio/rollforward/2026-09-10-mvp2-live-only/layered-history",
                "--expect-superseded",
                "13",
                "--out",
                C + "/tail/ca",
            ],
        ),
        "recipe": (
            "t_tail1.sh 2c",
            [PY, MAT, "recipe"]
            + RECIPE_COMMON
            + [
                "--correction-registry",
                C + "/tail/ca/portfolio-correction-registry.json",
                "--rederive-receipt-registry",
                C + "/tail/ca/portfolio-rederive-receipt-registry.json",
                "--output",
                C + "/pipeline-recipe.json",
            ],
        ),
        "pipeline-spec": (
            "t_tail1.sh 3",
            [
                PY,
                MAT,
                "spec",
                "--recipe",
                C + "/pipeline-recipe.json",
                "--output-root",
                RUN,
                "--spec",
                RUN + "/pipeline-spec.json",
            ]
            + HWM[:6]
            + [
                "--invested-capital-result",
                RUN + "/preparation/invested-capital-result.json",
                "--invested-capital-input-generation",
                RUN + "/preparation/capital-input-generation.json",
                "--financial-input-contract",
                "contracts/portfolio-financial-mvp-source-v1.json",
                "--portfolio-root",
                R + "/ws/dataPortfolio",
                "--retained-cache-root",
                C + "/cache2",
            ]
            + [HWM[6], HWM[7]],
        ),
        "semantic": (
            "t_tail1.sh 4",
            [
                PY,
                MAT,
                "semantic",
                "--recipe",
                C + "/pipeline-recipe.json",
                "--output",
                RUN + "/evidence/post-build-semantic.json",
            ],
        ),
        "financial-artifacts": (
            "t_tail1.sh 5a",
            [
                PY,
                REPO + "/ops/mvp2-release/scripts/build_financial_artifacts.py",
                "--run",
                RUN,
                "--cycle",
                C,
                "--out",
                C + "/production/financial-artifacts.json",
            ],
        ),
        "production-spec": (
            "t_tail2.sh",
            [
                PY,
                MAT,
                "production-spec",
                "--recipe",
                C + "/pipeline-recipe.json",
                "--prepared-root",
                RUN,
                "--semantic-gate",
                RUN + "/evidence/post-build-semantic.json",
                "--portfolio-root",
                Q + "/dataPortfolio",
                "--state-root",
                C + "/cache2",
                "--spec",
                PRODSPEC,
                "--financial-artifacts",
                C + "/production/financial-artifacts.json",
            ]
            + HWM
            + ["--provider-generations", C + "/provider-generations.json"],
        ),
        "replica-copy": (
            "t_replica_copy.sh",
            [
                PY,
                REPO + "/tools/release_replica.py",
                "copy",
                "--spec",
                PRODSPEC,
                "--workspace-root",
                Q,
                "--replica-root",
                R + "/replica/_quant_engine",
                "--allow-state-root",
                C + "/cache2",
            ],
        ),
        "replica-spec": (
            "t_replica_spec.sh",
            [
                PY,
                MAT,
                "production-spec",
                "--recipe",
                C + "/pipeline-recipe.json",
                "--prepared-root",
                RUN,
                "--semantic-gate",
                RUN + "/evidence/post-build-semantic.json",
                "--portfolio-root",
                R + "/replica/_quant_engine/dataPortfolio",
                "--state-root",
                C + "/cache2",
                "--spec",
                C + "/replica-production/production-spec.json",
                "--financial-artifacts",
                C + "/production/financial-artifacts.json",
            ]
            + HWM
            + ["--provider-generations", C + "/replica-provider-generations.json"],
        ),
        "lanes": (
            "t_lanes.sh",
            [
                PY,
                REPO + "/tools/release_replica.py",
                "lanes",
                "--real-spec",
                PRODSPEC,
                "--replica-spec",
                C + "/replica-production/production-spec.json",
                "--out-dir",
                C + "/laneout",
                "--real-panel-dir",
                Q + f"/dataPortfolio/control-panel/generations/{PANEL}",
                "--replica-panel-dir",
                R
                + f"/replica/_quant_engine/dataPortfolio/control-panel/generations/{PANEL}",
                "--expected",
                "acceptance/expected-baseline.json",
                "--cutoff",
                "2026-09-17",
                "--crosswalk",
                RUN
                + f"/stages/epoch/generations/{EPOCH}/authority/account-assignment-crosswalk.json",
                "--harness-code-commit",
                SHA,
                "--rehearsal-root",
                C + "/replica-rehearsal",
                "--clean-room",
                C + "/clean-room",
            ],
        ),
    }
    recorded = {
        "source": "evidence/releases/2026-10-26-r2/r26/scripts (pass 26: chain_p26.sh, run_attended.sh, qa_only.sh, resume.sh, t_tail1/2.sh, t_replica_copy/spec.sh, t_lanes.sh)",
        "stages": {
            k: {"source": v[0], "argv": v[1], "known": []} for k, v in rows.items()
        },
    }
    (OUT / "pass26-commands.json").write_text(
        json.dumps(recorded, indent=1) + "\n", encoding="utf-8"
    )
