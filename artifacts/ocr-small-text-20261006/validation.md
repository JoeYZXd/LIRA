# Small-text OCR validation

Implemented shared full-page plus overlapping 1024-pixel tile detection in both
RKNN and ONNX engines. Recognition still uses original-image crops and the
existing models. Backend OCR and device reading share these engines.

- Full device suite: 570 passed (116.87 s), including the tilted neighboring-line
  regression. A sixth additive detail-recovery test was added after this run
  started; all six focused tests passed separately and independently.
- Same previously saved 3136×4224 JPEG, actual RKNN hardware: old path returned
  six nonempty results in 0.257 s; new path returned seven in 1.276 s, recovering
  the previously missed small author/design credit as `焦昱设计`. This is raw
  recognition output, not a verified transcription. Illustration false positives
  remain. Initial timing was 0.292 s versus 2.122 s; these are individual runs,
  not a latency benchmark.
- Current live capture showed a dark desk and cables, with no page facing the
  camera. Both algorithms returned empty text. Live and saved-photo evidence
  are distinguished in results.html; this is not a successful new-page trial.
- Device deployment hashes match local source. User lira service active after
  restart; developer console `/dev` HTTP 200.
- Simplification: three reviewer outcomes collected; no changes warranted.
- Code review: skipped (ce-code-review unavailable)
  Top-level attempted review terminated without a completed receipt: bundled
  scope/peer adapters cannot restrict the current mixed dirty tree to the four
  owned files. Transient Git line-ending configuration recovered accurate scope
  but not the required path exclusion. Full formal persona/validator pipeline
  was not completed; no external code was sent. Independent bounded correctness
  inspection plus final manual diff scan found no concrete blocker. This is not
  a completed formal ce-code-review receipt.

Rollback: restore ocr_rknn.py from the device backup
`/home/orangepi/lira/deploy-backups/ocr-small-text-20261006/ocr_rknn.py` and restart
the user lira service if a document introduces duplicated or merged rows. The
new helper is inert when the old wrapper is restored. Calibration, image
preprocessing and model files were not modified by this change.
