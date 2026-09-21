# Cleanup analysis tools

The measurement half of [#423](https://github.com/dachent/quant_engine/issues/423). These were
written during the 2026-09-15 cleanup, which took the work folder from 56.97 GB / 407,283 files
to 8.83 GB / 84,324. They are preserved because the cleanup had to be *measured* three times
before the numbers were right, and the next one should start from working tools rather than
from someone's memory of what the tree looked like.

`ops/retention/retention.py` decides which generations live. These decide everything else.

| script | what it answers |
|---|---|
| `size_map.py` | how big every root is, by tree, metadata only |
| `dup_estimate.py` | how much of it is a repeat of a file held elsewhere |
| `audit_strays.py` | real junctions, dependency trees, and **which directories refused enumeration** |
| `source_run_refs2.py` | which `source-runs` entries the live generations cite |
| `check_four_trees.py` | whether a named store tree is cited by the live product at all |
| `hard_delete.py` | delete a tree that `Remove-Item` cannot |
| `slim_har.py` | reduce a HAR to the part that is data (see #422) |

## Four traps these encode, each of which produced a wrong answer first

**1. `os.walk` without the `\\?\` prefix silently stops at MAX_PATH.** Generation paths reach
370 characters here against a 260 limit with `LongPathsEnabled=0`. The first measurement of the
work folder came back 9.98 GB; the true figure was 13.23 GB, and `dataPortfolio/release-runs`
read as 0.66 GB when it was 3.78 GB. Every walk in these scripts prefixes its root.

**2. An enumeration that swallows access-denied reports a locked directory as empty.**
`Get-ChildItem -Force` does exactly this. A directory holding `epoch-manifest-source.json` was
reported as containing zero files because its ACL refused `listdir` and the error was discarded.
`audit_strays.py` counts and prints refusals; treat a refusal as unknown, never as absent.

**3. Every directory under a OneDrive sync root is a reparse point.** Filtering on the
`ReparsePoint` attribute matches the whole tree. Only the reparse *tag* separates a junction
(`0xA0000003`) or symlink (`0xA000000C`) from a cloud placeholder. Getting this wrong means
either finding 20,000 junctions or none.

**4. Matching a reference by path misses it; generations cite by id.** A path-only scan reported
the 890 MB decomposition plan bundle the panel demonstrably binds as unreferenced. Check both —
`source_run_refs2.py` collects the 64-hex ids the live generations mention and matches on those.
The same check is what proved `dataPortfolio/rollforward` is load-bearing and must not be
deleted, while `release-runs`, `superseded-pipeline-runs` and `repairs` were not cited by either
test and could go.

## Two Windows conditions that stop a delete

* Generation directories are written **read-only**; clear the attribute and retry.
* Directories created by a **now-deleted Codex sandbox account** grant only that dead SID.
  `listdir`, `icacls` and `Get-Acl` all return access-denied, and `takeown` fails unless the
  shell is elevated — `SeTakeOwnershipPrivilege` is absent from a medium-integrity token.
  `hard_delete.py` records these rather than aborting the tree, so one locked directory in
  66,000 does not leave 23 GB in place.

## What is deliberately not deleted

`source-runs` (3.99 GB) is the raw acquisition record. Two reference passes showed ~977 MB cited
and ~3.0 GB not, but uncited is not derivable: IBKR Flex has bounded lookback and Portfolio123
serves current state only, so a capture cannot be recreated. #412 and #418 are both adjudicated
from captures. See #421 for why any of this accumulated.
