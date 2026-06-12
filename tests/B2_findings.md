# B-2 Replay Findings

## B-2 HEAD: Non-deterministic engine output

Replay without PYTHONHASHSEED: 426 / 421 / 419 across 3 runs (diverges 7 entries).
Replay with PYTHONHASHSEED=0: 418 / 418 (stable).

Root cause hypothesis: engine uses set/dict iteration in member resolution
or location matching -- iteration order varies by hash seed, changing
which member/location is picked when multiple candidates tie.

All canonical numbers below are PYTHONHASHSEED=0.

## Stable baseline (PYTHONHASHSEED=0)

Replayed: 690 / Skipped(BATCH/HELP/NOMATCH): 72
Consistent: 418 / Divergent: 272

| Category    | Total | EDGE |
|-------------|-------|------|
| PATCH       |    12 |    3 |
| STATE-DRIFT |    61 |   21 |
| MISALIGN    |     0 |    0 |
| UNKNOWN     |   199 |   53 |

## Notes

- UNKNOWN 199: ~133 are double-checkmark diffs where loc/member differ
  (genuine parse-behavior change, not cost-only drift)
- PATCH 12: all are da9e9d5 friendly-hint behavior (expected)
- STATE-DRIFT 61: cost-only diffs on same loc+member (accumulated state)
- PYTHONHASHSEED=0 required for reproducible test runs
