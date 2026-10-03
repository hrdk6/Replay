# Phase 0.5: divergence spike report

**Status:** synthetic study done; real-model run pending provider keys (one command, below).
**Code:** `spike/divergence/spike.py` (research quality). It uses the production matcher from
`replay_api.replay.matching`, so the numbers describe what the product actually does.

## Question

When we replay a recorded agent run with a different model, how often do its tool calls fail to match
the recording (*divergence*)? And how often does a fuzzy match quietly return the **wrong** recorded result?
The second failure is worse, because nothing flags it.

## Method

- **Tools.** Four deterministic local tools (weather, flights, help-center search, order lookup). Each one
  canonicalizes its arguments the way real APIs do: city aliases, several date formats, case-insensitive IDs,
  and schema defaults.
- **Live runs.** Model A runs each task live, and those runs become the recording. Model B then runs the
  same task live, receiving the *true* tool results. That means B's trajectory is exactly what B would do
  in production.
- **Shadow check.** At every tool call B makes, we ask the matcher, under several settings, what replay
  would have served.
- **Oracle.** Because the tools are deterministic, we can check whether the served recorded result equals
  the true result for B's arguments. One pass yields both the divergence rate and the wrong-result rate for
  every setting.
- **Workload.** 400 tasks across weather, flights, flight+weather, help-center and order+policy questions,
  with 1–2 tool calls each.

### Assumption you should know about

No LLM keys were available here, so models A and B are **synthetic**:
- **Model A** issues canonical calls.
- **Model B ("other family")** re-phrases them with stated probabilities:
  - aliases ("nyc") 35%
  - different date format 30%
  - case changes 30%
  - extra words in search queries 35%
  - explicitly passing a default argument 25%
  - re-ordered calls 15%
  - an extra call 8%
  - a genuinely different city 3%
  - an adjacent date 3%

These probabilities are educated guesses, not measurements. The *mechanisms* found below are robust; the
*rates* are not, and must be re-measured with real models.

## Results (3 seeds × 400 tasks)

**Matcher as first written (v1).** These numbers are from the run before any change. Untyped string
similarity was used everywhere, with no date or default normalization (seed 7):

| v1 matching | Divergent runs | Silently wrong runs |
|---|---|---|
| exact only | 60% | 0% |
| fuzzy ≥ 0.95 | 59% | 1.2% |
| fuzzy ≥ 0.85 (v1 default) | 57% | 1.2% |
| fuzzy ≥ 0.70 | 45% | 2.5% |

**Matcher after the changes (v2, now the default).** Ranges cover seeds 7, 11 and 23. The "v1-style"
rows include the new date and number normalization, so they isolate the effect of typing:

| Matching | Divergent runs | Wrong results / matched calls | Silently wrong runs |
|---|---|---|---|
| untyped fuzzy 0.85 (v1-style) | 46–50% | 1.3–2.7% | 1.2–2.2% |
| untyped fuzzy 0.70 (v1-style) | 32–35% | 2.1–3.4% | 2.5–3.8% |
| typed exact only + defaults | 36–42% | 0% | 0% |
| **typed fuzzy 0.85 + defaults (new default)** | **35–38%** | **0%** | **0%** |
| typed fuzzy 0.70 + defaults | 29–32% | 0–0.4% | 0–0.5% |
| typed fuzzy 0.60 + defaults | 15–16% | 1.1–1.6% | 1.5–2.2% |

## Findings

1. **Untyped fuzzy matching is unsafe.** String similarity treats `2026-10-05` vs `2026-10-06` (ratio 0.90),
   `A100` vs `A101`, and `100` vs `101` as near-identical. Replay then serves the wrong day's weather or the
   wrong order without flagging anything. This silent error is worse than divergence.
2. **Most divergence comes from harmless formatting, not real behavior change.** That includes date formats,
   explicitly passed defaults, case, and re-ordering. Normalizing these removes about a third of all
   divergence with zero wrong results.
3. **Free-text fuzzy matching helps only a little, and fails semantically.** "cancel policy" vs "refund
   policy" differ by one word and mean different things. Below about 0.7, wrong results appear.
4. **Some divergence is irreducible without new information.** Aliases ("nyc" → "new york") need domain
   knowledge. Extra calls and genuinely different arguments mean the candidate asked something the recording
   cannot answer. Divergence should be reported, never hidden.
5. **With a model swap in full-agent mode, expect substantial divergence.** That's 35–38% here under these
   assumptions. Single-turn mode avoids tool-call divergence entirely. It replays only the final LLM call,
   with recorded tool results already in its context.

## Changes made (shipped in this build)

- **Typed fuzzy matching (default).**
  - Only free-text leaves (strings of 2+ words) may differ.
  - IDs, dates, numbers, enums and single tokens must be equal.
  - The old behavior is still available as `settings.typed_matching = false`.
- **Value normalization.**
  - Common date formats become ISO dates.
  - Plain numeric strings become numbers. Leading-zero IDs like `"007"` are left alone.
- **Schema-default normalization.** An argument equal to the tool's JSON-schema `default` counts as absent.
  The schemas come from the recorded LLM calls' tool definitions.
- **Default threshold stays 0.85.** It had zero wrong results in every seed.
- **Divergence handling unchanged.** The verdict still reports divergence per arm, analyzes with and without
  diverged runs, and refuses SAFE above `max_divergence_rate` (20% by default).

## Recommended next steps (not built; they need your decision)

1. **Re-run with real models** before trusting these rates. Each run costs about 1–3k LLM calls:
   ```bash
   OPENAI_API_KEY=... ANTHROPIC_API_KEY=... uv run python spike/divergence/spike.py \
       --tasks 100 --model-a gpt-4o-mini --model-b claude-haiku-4-5
   ```
2. **Per-tool normalizers** (v1.1): let users declare alias maps or "ignore this argument" rules per tool, in
   project settings. This targets finding 4 without unsafe similarity.
3. **Opt-in live execution for read-only tools** (later): a user-hosted webhook could answer unmatched calls.
   It cuts divergence sharply, but it means calling customer systems, so it should be a deliberate product
   decision.
4. **Steer users to `single_turn`** for prompt and model changes. Keep `full_agent` for changes to tool-use
   behavior, with divergence prominent on the report. The dashboard already defaults to `single_turn`.
