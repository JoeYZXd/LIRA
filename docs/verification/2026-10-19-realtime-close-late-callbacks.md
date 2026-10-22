# Realtime close: cancellation completion after branch awaits

The independent review's remaining race was reproduced before the fix. A receiver already inside an event branch can finish an await normally after close cancels it. Checking closed only after recv does not cover that case.

Proof-first regression: response.output_text.done consumes cancellation in the interrupt send and previously emitted interrupted plus assistant_text after close. response.output_audio.done consumes cancellation in enqueue and previously emitted audio_done. Both failed on their late-event assertions before the fix.

A second controlled regression covers response.output_audio.started and delta. Player.stop consumes receiver cancellation while _start_output is suspended; both cases previously started a fresh player after close. These two cases failed before adding the output-start guard. The tests retain real close task gathering and assert receiver termination, no remaining session tasks, no new playback or queue, and no late callbacks; the completion tests also assert player/socket cleanup.

The fix adds closed checks after the relevant awaits in interrupt, _start_output, and receive's started/delta/done/text branches. It does not change timeouts, wire events, role instructions, classic audio, or teaching behavior.

Validation: WSL Python pytest tests/test_realtime_capabilities.py tests/test_realtime_speech.py -q: **40 passed in 4.01s**. The four new parameterized cases were witnessed red before their corresponding guards. Final doubao_realtime.py SHA256: f5354685f06dd93a3a895b433462fc353f942ba9282b2d6a9bc2345061fb8bc4.

No board files, service state, staging, or commits were changed by this follow-up. Real cloud verification belongs to the subsequent root run against this final source.
