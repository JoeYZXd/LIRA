# Camera diagnostics review resolution

Review receipt: [completed review](2026-10-05-camera-quality-review.json), run `20261005-004409-e888832e`.

- Finding #1 (P1): applied in commit `080c195`. Entire subprocess teardown is shielded against repeated cancellation before restoration and lock release. Real SIGTERM-resistant subprocess regression failed before the fix and passes afterward.
- Finding #2 (P2): applied in commit `080c195`. Auxiliary ROI errors preserve valid primary OCR scores and denominators, while recording auxiliary failure and returning an overall failure exit. Invalid ROI, region inference failure and export failure regressions failed before the fix and pass afterward.

Host verification: 45 focused tests and 423 full device tests pass. Ruff passes for the diagnostics and their tests; project-wide Ruff reports existing issues outside scope. No type checker configured. No justified review findings remain unresolved.

Hardware acceptance remains distinct: controlled capture and restoration, nine parameter combinations, three distances, stage replay and real RKNN OCR were exercised. Three independent alternating baseline/candidate repetitions at the restored 20 cm position were completed on 2026-10-05 morning; the candidate failed against the contemporaneous baseline and was rejected. Lower-gain and alternating-width probes did not establish an improved configuration. A4 ordinary-body acceptance remains incomplete. The two-page pinyin book and body-only reference cannot establish final A4 acceptance. Details and sample directories are in the experiment record.

Simplification pass applied one reuse change and two efficiency changes; no quality changes or safety-check removals. Review's external cross-model pass did not run because its whole-base transport could not isolate the authorized diff; local adversarial fallback and independent finding validation completed. No external code transmission occurred.
