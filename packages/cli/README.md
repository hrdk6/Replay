# replay-cli

`replay` runs a Replay experiment for the change in your working tree (or pull request) and turns the
verdict into an exit code for CI. Standard library only (Python 3.11+).

```bash
pip install replay-cli          # or: pip install ./packages/cli
export REPLAY_API_KEY=rk_...     # project API key; never put it in the config file
replay init                      # writes an example replay.toml
replay datasets                  # find dataset ids
replay judges                    # find judge ids
replay check --base-ref origin/main
```

| Exit code | Meaning |
|---|---|
| 0 | Passed the policy, or skipped because no trigger path changed |
| 1 | Blocked by the policy (for example, the verdict is UNSAFE) |
| 2 | Error (configuration, network, timeout) |

`[candidate]` describes what the change does: a system prompt or prompt template read from a file,
a model, or parameters. `[baseline]` reads the same files at the PR's base commit, so the experiment compares
"before" and "after" on the same frozen traces. Omit `[baseline]` to replay the recorded configuration.

In GitHub Actions, use the composite action in `action/` (see `examples/github-workflow.yml`). It writes the
verdict to the job summary, posts or updates one PR comment, and sets the `verdict` and `experiment-url` outputs.
