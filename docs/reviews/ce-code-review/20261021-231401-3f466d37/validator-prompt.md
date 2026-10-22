# Validator Batch Prompt Template

Use one fresh validator subagent for one batch of already-merged findings. Eight findings is the normal cap. When more than eight P0/P1 findings survive, expand that same batch to include every surviving P0/P1; never omit a blocker or split the work into another batch. The validator is independent of the originating reviewers and the orchestrator.

```
You are the independent validation gate for the code-review findings below. Evaluate each finding separately under fresh inspection. False positives are common; reject a finding when the cited code does not prove it, it predates and is unaffected by this diff, surrounding code handles it, or it is only an unsupported preference.

Do not let one valid or invalid finding influence another. Do not invent new findings.

<findings-to-validate>
[
  {
    "#": 1,
    "autofix_class": "gated_auto",
    "confidence": 75,
    "file": "device/lira/dictation_tools.py",
    "first_evidence": "device/lira/dictation_tools.py:11:  'pending不是成功，不可用pause_agent_task代替电视暂停。写好了或下一词用confirm，重播用replay，上一词用previous，结束用cancel。手动模式每次只播报一遍，等待用户控制，不自行重播或推进。')",
    "independent_reviewers": [
      "correctness"
    ],
    "line": 11,
    "owner": "downstream-resolver",
    "pre_existing": false,
    "requires_verification": true,
    "reviewers": [
      "correctness"
    ],
    "severity": "P2",
    "suggested_fix": "把新增的上一词/上一个和下一词请求纳入可信输入分类：上一词作为非提问控制请求处理；下一词与下一个一样设置 child_ack。增加 observe_user 后成功 previous 再提问暂停并恢复的回归用例，确保不会残留 question/answer_pending。",
    "title": "上一词请求被误记为未回答的提问",
    "why_it_matters": "新支持的正常语音请求“上一词”经 observe_user 时未命中控制短语白名单，被设置为 question 和 answer_pending。随后 previous 可以成功切换当前词，但 question 仍绑定旧词；allow_output 无法匹配当前项目，后续 pause 又以旧 question 项目为 target 返回 context_changed，resume 因 answer_pending 返回 answer_not_drained。一次正常上一词请求会污染后续辅导和恢复流程，通常只能结束任务清除。新提示的“下一词”也未被识别为 child_ack，confirm 会被拒绝。",
    "evidence": [
      "device/lira/dictation_tools.py:11:  'pending不是成功，不可用pause_agent_task代替电视暂停。写好了或下一词用confirm，重播用replay，上一词用previous，结束用cancel。手动模式每次只播报一遍，等待用户控制，不自行重播或推进。')",
      "device/lira/dictation_tools.py:56-59: waiting_child 的确认短语仅包含下一个等，不包含下一词；控制白名单不包含上一词，随后执行 self.question=(self.stamp(),turn);self.pause_command=None;self.answer_pending=True。",
      "device/lira/dictation_tools.py:110: target=self.question[0] if action=='pause' and self.question else self.stamp(); previous 完成后项目已经变化，same_item 校验与旧 question 不符。",
      "device/lira/dictation_tools.py:106: if self.speaking or self.answer_pending:return dict(status='answer_not_drained')"
    ]
  },
  {
    "#": 2,
    "autofix_class": "gated_auto",
    "confidence": 75,
    "file": "device/lira/llm/chinese_dictation_skill.md",
    "first_evidence": "device/lira/llm/chinese_dictation_skill.md:15: 当绑定流程的 manual_navigation=true 时，四会词每次只播报一次，结束后保持当前词等待遥控操作：左键返回上一词并播放，右键下一词并播放，确认键重播当前词。首词左键重播首词，末词右键结束；不循环、不计时重播或自动跳词。模型不能自行推进；收到孩子明确“下一个／写好了”才调用确认下一项，重播或上一词也须有对应请求。认读仍用确认键翻页；暂停后的确认键恢复当前词，不能绕过提问解释的暂停与继续确认。",
    "independent_reviewers": [
      "agent-native"
    ],
    "line": 15,
    "owner": "downstream-resolver",
    "pre_existing": false,
    "requires_verification": true,
    "reviewers": [
      "agent-native"
    ],
    "severity": "P2",
    "suggested_fix": "Add the bound task's pinned playback settings, including manual_navigation, to model_instructions and the dialogue agent's system context; include the same setting in get_dictation_task_status so the agent can refresh it after adoption. Read from the task snapshot, not the latest configuration revision.",
    "title": "Expose the bound manual mode to the agent",
    "why_it_matters": "The new instructions depend on manual_navigation, but subsequent dialogue turns receive only the skill text and a status tool result without the task playback settings. After preparation or task adoption, the agent cannot reliably know whether to apply manual-mode navigation instructions to the bound task.",
    "evidence": [
      "device/lira/llm/chinese_dictation_skill.md:15: 当绑定流程的 manual_navigation=true 时，四会词每次只播报一次，结束后保持当前词等待遥控操作：左键返回上一词并播放，右键下一词并播放，确认键重播当前词。首词左键重播首词，末词右键结束；不循环、不计时重播或自动跳词。模型不能自行推进；收到孩子明确“下一个／写好了”才调用确认下一项，重播或上一词也须有对应请求。认读仍用确认键翻页；暂停后的确认键恢复当前词，不能绕过提问解释的暂停与继续确认。",
      "device/lira/dictation_tools.py:45 returns INSTRUCTIONS plus skill content; lines 203-208 add skill and current_item to the status response, but no playback configuration; line 305 builds the dialogue system message without playback configuration."
    ]
  }
]
</findings-to-validate>

<diff>
D:\Projects\LIRA\docs\reviews\ce-code-review\20261021-231401-3f466d37\full.diff
</diff>

<scope-context>
Caller-exact snapshot scope in D:/Projects/LIRA; current code frozen after caller fixes during review. Do not inspect whole git diff. No secrets/network/services/model calls/mutations.
</scope-context>

For local-aligned scope, inspect the cited files, callers, guards, project contracts, and targeted history with read-only tools. For pr-remote or branch-remote scope, use the provided diff and reviewed head ref, never the unrelated workspace copy.

Return exactly one JSON object:
{
  "verdicts": [
    {
      "#": <input stable number>,
      "validated": true | false,
      "reason": "<one sentence grounded in inspected evidence>"
    }
  ]
}

Return one verdict for every input # exactly once. No prose outside JSON. Do not edit, commit, push, or mutate files.
```

These are historical candidates raised earlier during this same review. Caller reports fixing both: voice previous/next intent classification plus pendinganswer control gates; pinned playback injected into model_instructions/status/dialogue from frozen task, with tests in device/tests/test_dictation_dialogue.py. Independently inspect CURRENT source/tests and return validated:false if resolved, true only if a concrete remaining defect persists. Do not invent new findings. No project writes.