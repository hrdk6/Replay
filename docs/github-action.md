# Gating pull requests

The `replay` CLI runs an experiment for the change in a pull request and turns the verdict into a
check result. The GitHub Action in `action/` wraps it.

## One-time setup

1. **Capture traffic and freeze a dataset.** Dashboard → Datasets → New. Use a fixed seed and a few
   hundred items.
2. **Create a judge, then calibrate it.** Label at least 30 of its decisions in the Labeling queue,
   then press Recompute.
3. **Create an API key** for the project (Project settings) and save it as the repository secret
   `REPLAY_API_KEY`.
4. **Add `replay.toml`** to the app repository (`replay init` writes an example):

```toml
[replay]
api_url = "https://api.your-domain"
dataset_id = "…"            # replay datasets
judge_id = "…"              # replay judges
mode = "single_turn"
budget_usd = 5.0

[candidate]                 # read from the PR's working tree
system_prompt_file = "prompts/system.txt"

[baseline]                  # same file, read at the PR's base commit
system_prompt_file = "prompts/system.txt"

[policy]
fail_on = ["UNSAFE"]        # add "INCONCLUSIVE" to require positive evidence
non_inferiority_margin = 5
require_calibrated_judge = true
fail_on_slice_regression = false

[trigger]
paths = ["prompts/**", "src/agent/**"]
```

5. **Add the workflow.** See `examples/github-workflow.yml`. It needs `fetch-depth: 0` so the CLI can
   diff against the base branch and read base-commit files.

## What happens on a PR

1. If no changed file matches `trigger.paths`, the check passes and is marked as skipped.
2. **Building the arms.**
   - The candidate config is built from `[candidate]`, with files read from the PR head.
   - The baseline is built from `[baseline]`, with files read at `origin/<base>`.
   - Omitting `[baseline]` replays the recorded production configuration instead.
3. `POST /v1/ci/experiments` starts the run. The CLI polls until it finishes, with a 30-minute timeout
   by default.
4. The verdict is written to the job summary and to a single PR comment, which is updated on each run.
5. The exit code sets the check status:

| Exit code | Meaning |
|---|---|
| 0 | Allowed by policy, or skipped |
| 1 | Blocked by policy |
| 2 | Error: configuration, network or timeout |

CI-created candidates are hidden from the dashboard's candidate list but appear on experiments, tagged
with the PR number.
