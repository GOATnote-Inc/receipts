# Active

## Phase
Phase 0 — substrate. 24 tasks total (#1–#24).

## Next pick-up
Task #1 (V1: init repo + pytest scaffold) — IN PROGRESS.

Once #1 completes, the following are claimable in parallel:
- #2 (L1: Postgres schema + alembic)
- #8 (J1: κ calculator + Wilson CI)
- #9 (J2: L0 deterministic scorer)
- #10 (J3: L1 structural scorer)
- #14 (J7: judge replay mode)
- #15 (S1: revised-spec drafter + validator)
- #22 (V3: pass^k regression script)

After #2 completes: #21 (V2: synthetic eng-week fixture) unblocks.

## Exit criteria (Phase 0 done)
- All 24 substrate tasks status=completed
- `make test` green with substrate test coverage
- pass^k ≥ 0.95 on substrate fixture (V3 gate)
- κ ≥ 0.40 on dual-judge fixture (V4 gate)
- Merkle chain verifies on synthetic week (V5)
- Substrate E2E test (#24) green

## Verify command
`make venv && make lint && make test`
