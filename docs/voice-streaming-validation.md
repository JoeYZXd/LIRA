# Voice streaming validation

## Status and collection

U1 adds software measurements only. Board acoustic acceptance and comparative p50/p95 sampling remain pending. No deployment or default backend change was performed.

Enable `LIRA_VOICE_METRICS=1` before starting the process. The default is disabled. In a diagnostic harness, read `runtime.voice_metrics.snapshot()` for raw numeric stage records or `.summary()` for per-utterance timings. Export outside playback callbacks. Rings retain at most 2048 records, dropping the oldest; `dropped_events` counts records skipped to avoid lock contention. Export frequently enough to retain complete samples; incomplete stages yield null, never zero. Do not treat missing measurements as success.

Only numeric fields and structural utterance/turn/owner identifiers are accepted. No text, PCM, credentials or persistent second audio log is added. Privacy activation clears the ring, advances its epoch, and rejects events from outstanding old traces even after privacy is disabled. Events are captured with their originating trace instead of a global current owner.

## Measurement contract

- `asr_submit`: dispatch acceptance timestamp, not the human speech endpoint.
- `llm_request` / `llm_content`: model request and first nonblank visible text. Empty and role-only deltas do not count. Repeated model requests remain visible in raw records.
- `segment_submit`, `synthesis_start`, `synthesis_end`, `pcm_ready`: enqueue and real worker execution. Synthesis timing includes the complete generation call, including prefetched calls; awaiting an already running prefetch is separately named `chunk_wait`, never interpreted as generation latency. RTF is compute seconds divided by generated audio seconds.
- `output_consumed`: first HAL callback consuming nonempty PCM. This estimates audio device submission, not measured sound at the speaker. Wake chime and progress/ack speech have answer=false; streamed answer and final answer use answer=true.
- `output_drained` / `cancel_requested` / `cancel_confirmed`: output lifecycle and cancellation. Confirmation of a cancelled synthesis-only request means no audio output remains, not that native compute was preempted.
- `buffer` supports buffer frames, device underflow, producer starvation duration/count as separate measurements. Current one-shot playback records device underflow; producer starvation cannot yet be inferred from this driver. U3/U4 must populate starvation using continuous producer/consumer state. Audio-content silence must not be classified as producer starvation.

At each phase record device and OS, selected model/backend, model revision, sample rate, TTS threads, network configuration excluding credentials, warm/cold status, fixed sample identifiers, failure counts, CPU/RSS and all raw numeric samples. Current repository defaults are not deployment evidence.

Use the same device/network and fixed corpus: at least 30 ordinary questions, 10 search/clarification questions, and five continuous reading samples of at least two minutes per configuration. Report p50/p95 with sample sizes and failures, startup separately, device underflows separately from producer starvation count/duration, longest nonsemantic pause, cancellation-to-silence, and acoustic loopback or external recording calibration. Human speech-end-to-first-sound requires an external timestamp. Software timestamp tests do not replace acoustic testing.

## Limited pre-change board observations

Read-only service journal inspection on 2026-10-15 (22:44–22:45 local journal labels) found `llm first_token` values 7.804/8.453 seconds and successful `llm.chat` totals 15.440/17.748 seconds. Existing `tts chunk_ready` reported 2.030/2.046 seconds for 14 characters, 1.818 seconds for 12 and 2.763 seconds for 19. These old TTS values can represent only remaining prefetch wait and are not reliable compute-time or RTF measurements. The board was reachable as aarch64 with its service active. These are unmatched log excerpts, not a fixed-corpus baseline, percentile result, or physical audio measurement. Actual backend/configuration inventory and matched-turn sampling remain pending.

## Host evidence

Proof-first: initial `tests/test_voice_metrics.py` collection failed because the new module did not exist. After implementation the initial three behavioral tests passed. Extended tests cover cross-turn/owner binding, privacy epochs, missing stages, prompt exclusion, first visible model content and complete worker synthesis timing. Run the voice, LLM, TTS and state-machine gates in the project environment; parent integration work records final counts. Physical device, continuous producer starvation and acoustic acceptance remain pending.
