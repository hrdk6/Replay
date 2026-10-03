# Replay and divergence

Replay re-runs a recorded trace with a candidate change. **LLM calls are re-executed**, using the org's
own provider keys. **Tool and retrieval results always come from the recording**, so replay never runs
user code and never touches outside systems.

## What gets recorded

A dataset item freezes a trace's *recording*:
- the LLM calls in order, each with canonical messages, tool definitions, model, provider and parameters,
  plus the output;
- the tool and retrieval events, each with name, arguments and result;
- the final output and the prompt variables, when recorded.

Accepted input formats:
- our SDK, plus OpenAI and Anthropic request and response shapes;
- the OpenTelemetry GenAI conventions (`gen_ai.input.messages`, `gen_ai.output.messages`, tool-call
  attributes, and the older `gen_ai.*.message` events);
- OpenLLMetry indexed attributes;
- OpenInference attributes.

Tool results that appear only inside a later LLM call's message history are recovered as well, so
instrumenting only LLM calls is often enough.

Span order uses start time, then the SDK's sequence number, then message-history length. That keeps
agent steps in order even when clock resolution makes their timestamps tie.

## Modes

| Mode | What runs | Can diverge? |
|---|---|---|
| `single_turn` (default) | The **final** LLM call only. Its recorded input, including recorded tool results, gets the candidate's overrides. | Only if the candidate asks for a tool where the recording produced an answer (`unexpected_tool_call`). |
| `full_agent` | The whole loop, starting from the first LLM call's input. Each tool call the candidate makes is matched against the recording. | Yes: no matching recording means `DIVERGED` at that step, and the partial output is kept. Exceeding `max_steps` (default `min(25, max(4, 2×steps+2))`) also diverges. |

Use `single_turn` for prompt and model changes. Use `full_agent` when the change affects which tools get
called; divergence is then a meaningful outcome.

## Candidate changes

Unset fields keep the recorded values. An empty candidate is therefore a faithful baseline.

| Field | Effect |
|---|---|
| `provider`, `model` | Which model answers |
| `system_prompt` | Replaces the system message |
| `prompt_template` `{template, role}` | Re-renders the user (or system) message from recorded variables (`replay.prompt_variables(...)` in the SDK). If no variables were recorded, the run fails with an explanation. |
| `params` | `temperature`, `top_p`, `max_tokens`, `seed`, `stop`, `effort`. Values the model rejects are dropped and noted on the run. |
| `retrieval.top_k` | Truncates recorded retrieval results. It can only shrink them, has no effect in `single_turn`, and is noted when larger than what was recorded. |
| `pricing` | Overrides the per-MTok price for cost reporting and budgets |
| `provider_key_id` | Uses a specific stored key |

## Matching tool calls (`full_agent`)

Implemented in `backend/src/replay_api/replay/matching.py` and measured in the Phase 0.5 spike.

1. **Normalize both sides.** Keys are case-folded and sorted, nulls dropped, strings whitespace-collapsed
   and case-folded. Dates in common formats become ISO; plain numeric strings become numbers (IDs with
   leading zeros are left alone). Arguments equal to the tool schema's `default` are dropped.
2. **Exact.** Same tool name and same normalized hash. Unconsumed recordings are preferred, in recorded
   order.
3. **Fuzzy (typed).** Same name and similarity ≥ `fuzzy_threshold` (default 0.85). Only free-text values
   (2+ words) may differ; identifiers, dates, numbers and single tokens must be equal.
4. **Reuse.** With `allow_reuse` (the default), a recorded result can answer an identical repeated call.
   This is reported per step.
5. **Otherwise the run diverges.** The run stops, and the report shows the requested call and the best
   similarity it found.

## How divergence is reported

Divergence is never dropped silently. Every report shows:
- the divergence rate per arm, with an item-level bootstrap CI;
- **two analyses**: completed runs only, and diverged runs counted as the worst score;
- a refusal to return SAFE when candidate divergence exceeds `max_divergence_rate` (20%), or when the two
  analyses disagree.

The verdict headline names the blocker, for example "SAFE on completed runs, but not when diverged runs
count as failures".

## Repeats and nondeterminism

`repeats` (1–10) runs each item that many times per arm. Item scores average the repeats, and the
bootstrap resamples items, so repeats are treated as clustered rather than independent. The report shows
the mean within-item standard deviation per arm. Recorded temperature and seed are reused unless the
candidate overrides them. Some newer models ignore or reject sampling parameters; that is recorded.
