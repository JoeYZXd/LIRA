# Realtime close race — local repair receipt (2026-10-19)

Scope: `device/lira/audio/doubao_realtime.py` and deterministic regression cases appended to `device/tests/test_realtime_capabilities.py`. No board, service, classic chain, role instruction, tool schema, protocol envelope, or original plan edits.

## Red evidence

Before source repair, three new regression cases failed (`3 failed, 16 deselected`, 2.32 s):

- A sender's in-flight send consumes a cancellation and returns normally. `close()` remained pending at its task gather while `_send_audio` re-entered `input.get()`.
- A controlled recv completion/cancellation race returns a late text event. `_receive` consumed it and re-entered recv, leaving `close()` pending.
- A tool task calls `close()` itself. Including the current tool task in cancellation/gather creates a self-dependency and recursive cancellation (`RecursionError`).

The first draft test cleanup also stalled because its deliberately cancellation-consuming fixture consumed cancellation repeatedly. That isolated local pytest process was terminated; fixtures were corrected to consume only the intended race and bounded cleanup. This was not a product timeout workaround.

## Repair

- Audio sender and receiver loops check `closed`, including after awaited input/send/recv boundaries. Sender still balances the dequeued frame with `task_done()` and close still drains queued input.
- A recv result arriving after closure is discarded before JSON parsing, diagnostics, or callbacks.
- A send returning after closure no longer publishes a sent diagnostic or marks delivery tickets. A late tool return no longer emits `tool_done`.
- Tool shutdown snapshots tasks and excludes the current task, matching the existing session-worker self exclusion.
- Shutdown continues to await actual worker termination and close the player/socket. No timeout pretending completion, orphan task detachment, or physical player bypass was added.

## Green evidence

Final source: WSL Python 3.11, command:

```text
cd /mnt/d/Projects/LIRA/device
.venv/bin/python -m pytest tests/test_realtime_capabilities.py tests/test_realtime_speech.py tests/test_agent_contract.py -q
43 passed in 4.16s
```

Four new regressions check: actual sender exit with queued input drained and no further sends; actual receiver exit with no late event callbacks; tool-task self close; session-worker self close. The cancellation-race tests use controlled await completions, not physical/cloud I/O. Real cloud revalidation remains owned by the parent task; this receipt does not mark that gate passed.
