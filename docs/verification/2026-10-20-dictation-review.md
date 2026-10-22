# Dictation review — 2026-10-20

## Skill outcome

`compound-engineering:ce-code-review mode:agent`: **failed at scope resolution**, not a completed skill review.

The eight caller-selected files are all untracked: `device/lira/dictation.py`, `device/lira/textbook_practice.py`, `device/lira/textbook_import.py`, `device/lira/tv_voice.py`, `device/lira/conversation_paths.py`, `device/lira/audio/doubao_realtime.py`, `device/lira/ui/static/textbooks.js`, and `device/tests/test_dictation.py`. The skill's `references/scope.md` explicitly excludes untracked paths unless staged. Staging is prohibited by this invocation; reviewing the tracked branch diff would expand into unrelated work and omit the intended implementation. Therefore no compliant intended-scope diff exists. No unavailable external CLI was asserted, no external model was invoked, and no staging or checkout mutations were performed. `.compound-engineering/config.yaml` is absent; the default artifact root is `docs`.

## Supplemental bounded manual scan

This is a read-only source scan of the selected files, with immediate supporting reads of the existing exact-speech implementation. It is not a completed CE receipt. No household hardware or cloud requests were made, and no tests were executed by this reviewer.

### P2 — Cancelling dictation does not cancel its in-flight television send

Evidence: `device/lira/dictation.py:99` clears materials and cancels the speech task for `停止`, `结束默写`, and `不默写了`, but does not cancel television work. `device/lira/tv_voice.py:129` only cancels an active sender task for a different set of exact phrases; those three phrases are absent. Ready-banner and confirmed pinyin sends run in `self.tv.task` (`tv_voice.py:198`, `tv_voice.py:300`).

When a sender is awaiting a network response and the child says one of those stop phrases, the send remains active and can complete after dictation is ended. Pending consent itself is correctly cleared by the generic non-yes branch; this finding concerns the already running send. Cancel only the TV operation owned by the dictation task, and test with a delayed fake sender. Do not claim that already transmitted remote content can necessarily be retracted.

### P2 — Four-character playback has no way to apply a confirmed polyphonic reading

Evidence: `device/lira/dictation.py:194` sends only the isolated character repeated three times to speech, regardless of any confirmed pronunciation. Heteronym detection and the `pinyin` choice are implemented only after the four-character branch (`dictation.py:200`). The instructions nevertheless require confirmation of ambiguous readings.

For a reviewed word list containing a character such as `长`, the selected lesson's intended pronunciation and any spoken confirmation do not influence the exact playback text. An isolated-character TTS default can therefore dictate a different reading three times. Detect ambiguity before four-character playback, retain an approved reading with the lesson/item, and use a supported pronunciation mechanism or return an explicit limitation rather than silently selecting the TTS default. Verify by capturing the actual synthesized audio; a text assertion alone cannot establish pronunciation.

## Covered observations

- Draft lesson review is read independently of whole-book publication; selected rows are checked before material generation and rechecked before presentation.
- The catalog exposes source, lesson review, and import-time metadata for model-led selection. Multiple-book retrieval requires an observed later-turn choice.
- TV probe and ready-banner methods attempt actual sender operations and preserve unavailable/failed outcomes.
- Four-character output uses the existing insertion-speech response barrier; completion delegates to drained playback evidence rather than the queued acknowledgement.
- Two-character pages are limited to eight readings, require content-specific TV consent, and expose ambiguous readings for confirmation.
- A subsequent item is blocked within the same user turn or without a recognized next/repeat utterance. No automatic iteration or timer is present in the scanned dictation implementation.
- The new tools are routed through the existing realtime tool surface, independently of the disabled broad extensions. This source scan does not certify cloud-model adherence or hardware performance.

## Coverage limits

No formal multi-persona CE pipeline, validators, independent peer, git-diff attribution, real cloud interaction, microphone test, acoustic pronunciation check, television rendering test, or deploy verification was completed by this reviewer. Existing dirty/untracked state prevents attributing every inspected line exclusively to this turn. Findings describe the observed file contents at review time; the parent may subsequently fix them.


## Caller follow-up

Code review: skipped (ce-code-review unavailable) — the attempted skill terminated at its untracked-file scope gate; staging or widening into unrelated WIP was not authorized. Supplemental bounded source review and the three ce-simplify-code reviewer passes were completed instead; this is not a full CE receipt.

Both retained findings have been addressed locally:

- Dictation records ownership of the ready-banner task and confirmed pinyin send, cancels only those sends when stopped, and exposes cancel_dictation. A delayed-sender regression verifies cancellation. Already transmitted remote content is not represented as retractable.
- Four-character heteronyms require an observed confirmation of a pronunciation hint (for example 长江的长) before that hint is repeated three times. Two-character heteronyms likewise require confirmation of the page's pinyin choices. Regression tests verify the refusal and confirmed speech text. Human listening to physical speaker pronunciation remains part of the user's formal test.

Simplification applied: removed redundant outer validation/import, avoided catalog reads in speech callbacks, reused one catalog snapshot within material lookup, bounded advancement records alongside the eight cached materials, and placed TV task operations behind TvVoiceTools methods. The stored item offset is retained for current-item evidence. All other pre-existing WIP and the Git index remain untouched.
