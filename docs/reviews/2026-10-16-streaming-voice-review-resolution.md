# Streaming voice review resolution

Scope: changes from `1b396cf` to the streaming voice implementation, excluding older commits on `feat/home-console-chat`. Plan: `docs/plans/2026-10-15-2300-refactor-streaming-voice-pipeline-plan.md`. No plan progress fields were edited.

The `ce-code-review mode:agent` receipt is complete, run `20261015-235432-13bdc92e`, with verdict **Ready with fixes** before caller-owned fixes. Its artifacts are under `C:/Users/Joey/AppData/Local/Temp/compound-engineering-197609/ce-code-review/20261015-235432-13bdc92e`. Seven reviewers completed: correctness, security, testing, reliability, maintainability, performance and adversarial. Both retained findings passed one independent validator batch. There were no rejected findings, malformed returns, confidence suppressions or validator shortcuts.

The Claude cross-model route failed twice before job creation because the Windows runner could not harden its jobs-root ACL. No peer job or external review started. The local adversarial reviewer covered that concern; no cross-model corroboration is claimed. Standards discovery found no applicable repository criteria files. Browser validation was not selected because no frontend files changed.

| Finding | Consequence and correction | Evidence | Resolution |
|---|---|---|---|
| #1, P1, privacy cleanup | A hardware-stop error could interrupt the sequential privacy broadcast before sensitive-state cleanup. Runtime wiring now clears telemetry, numeric metrics, dispatch generation, dialog owner and identity before awaiting fallible audio shutdown. The error remains visible and no success announcement follows it. | The runtime regression failed on retained telemetry before the change. Focused privacy/queued-IR checks: 6 passed, 44 deselected. | `f24da35` |
| #2, P2, online trace | An online answer queued behind progress speech inherited the drain worker's old trace and answer flag. Playback task creation now binds and resets the request's captured context. | Before the fix, the second consumed output carried turn 1/progress/answer=0; afterward it carries turn 2/answer/answer=1. | `e83f09b` |

Two testing gaps were also closed. A held-PCM test proves that failure after network receipt but before HAL consumption stops and discards queued PCM, then speaks the complete local fallback exactly once. Deterministic callback tests verify starvation episode counts, missing-frame duration, recovery, supplied silent PCM and finite EOF padding (`67010f8`). These test gaps required no additional production behavior changes.

The preceding simplification pass applied one quality cleanup: remove an unused synthesis-measurement argument (`b636722`), verified by 23 focused tests. Reuse and efficiency reviewers found no worthwhile changes; no safety checks were removed.

Caller-owned integration verification after all review fixes: **123 passed** across runtime/privacy, providers, continuous HAL, metrics, speech sessions and dialog streaming. The full pre-review host result was **3 failed, 1107 passed**. The final post-review result is **3 failed, 1113 passed in 260.97 seconds**; all failures are the exact pre-change KWS WAV cases. Source compilation and diff whitespace checks passed. Detailed evidence is recorded in `docs/voice-streaming-validation.md`.

No actionable review findings remain unresolved. Physical acoustic/echo/stop/OCR-load acceptance, matched same-device performance samples and live cloud account/voice compatibility remain unverified. These are intentional trial boundaries, not passing results. Deployment, service changes, paid-service enablement, push and PR creation were not performed.
