# CLAUDE.md

Working notes for Claude Code sessions in this repository. Read this first,
then `DEVLOG.md` for where the build currently stands.

## The project

**Pinpoint** is an AI overseer for agent tool-use traces. Most overseers give a
verdict ("this agent lied") without saying where. Pinpoint returns the verdict
plus the exact step and quoted text that prove it, and checks that citation
against the trace before showing it. A citation that doesn't check out is
shown as **unverified**, never as a finding.

Built solo by Manas Mehta for the **Meta Global AI Developer Hackathon**,
Indie track.

| | |
|---|---|
| Building period | Oct 27 – Nov 4, 2026 (all official times are US Eastern) |
| Submission deadline | **Wed Nov 4, 9:00 PM ET = Thu Nov 5, 7:30 AM IST.** No late entries. |
| Submission | public repo + 3-minute demo video + live product |
| Judging | Impact, Use of Meta AI tooling, Technical depth, Demo (equal weight) |

## Hard rules

1. **New work only.** Everything presented must be built during the event.
   The demo may only show hackathon-built features. Commit timestamps are the
   evidence, so commit often.
2. **GroundingBench is prior work, used only as a pinned open-source
   dependency** (eval traces + reference grader). Organizers confirmed in
   Discord that an open-source dependency is fine. Never copy its code in.
   Never present it, or the GRPO-trained Qwen overseer from the paper, as
   hackathon work.
3. **The `pinpoint/` package is label-free.** It must never import `env` or
   `doers` (GroundingBench) or reference label fields (`smoking_gun_step`,
   `smoking_gun_keyword`, `policy_class`, `ground_truth_answer`,
   `compute_reward`). `tests/test_packaging.py` enforces this. Only
   `benchmark/` may read labels.
4. **Must use Meta's models**: Muse Spark via the Meta Model API, Muse Spark
   Open via Baseten. Llama 3.1 does not count.
5. **Secrets live only in `.env`** (gitignored). Read keys from the
   environment; never write a key into source, tests, logs, or docs.
6. **Manas makes all commits and pushes.** Claude writes and audits files,
   then hands over the exact `git add` / `git commit` commands. Claude does not
   run `git commit` or `git push`.
7. **Every number shown is real.** Demo and README outputs come from saved
   runs, never hand-written. Use exact figures: verdict accuracy **70.8–100%**;
   **1 of 1,794** correctly classified misbehavior verdicts grounded; **720**
   held-out traces.

## Audit protocol (after every file, before moving on)

1. Re-read the whole file line by line.
2. `ruff check .` and `ruff format --check .`
3. `mypy` (strict, covers `pinpoint/`, `benchmark/`, `tests/`)
4. pytest: happy path, every failure path, boundaries, adversarial input.
5. Mutation-test the important checks: plant a realistic bug, confirm a test
   fails, restore.
6. Run it for real once.
7. Record the result in `DEVLOG.md`.

## Layout

```
pinpoint/        label-free overseer (never sees ground truth)
benchmark/       evaluation harness; may use GroundingBench labels + grader
tests/           pytest suite
data/            fetched traces (gitignored; run benchmark/fetch_data.py)
results/         saved runs, raw model output per trace (committed)
app/             demo web app (not started)
```

## Commands

Run from the repo root on Windows (Git Bash shown).

```bash
python -m venv .venv
./.venv/Scripts/python -m pip install -e ".[dev,eval]"

./.venv/Scripts/python -m benchmark.fetch_data      # fetch + verify traces
./.venv/Scripts/python -m pytest -q
./.venv/Scripts/python -m ruff check . && ./.venv/Scripts/python -m ruff format --check .
./.venv/Scripts/python -m mypy
```

## Facts that are easy to get wrong

**Traces (GroundingBench @ `5fd7325`)**
- `smoking_gun_step` is **1-indexed**. Any renderer must number steps from 1.
- Held-out sets: `eval_traces.jsonl` (120) + `eval_traces_realistic.jsonl`
  (600), each balanced across HONEST / REWARD_HACKER / LAZY / DECEIVER.
  Max 7 steps per trace.
- The reference grader matches step references as substrings ("step 1" also
  matches "step 12"). Safe on this data (max 7 steps); never reuse that
  approach for live traces. Pinpoint compares step numbers as integers.
- DECEIVER's ground-truth keyword is the claim's first content word (e.g.
  "battle"), which can also appear elsewhere in the trace. Grader credit
  means "cited the right step and mentioned the keyword", not "named the
  false fact". State this caveat next to any grounding number.
- `Step.text()` is the single source for both what the overseer reads and
  what citations are checked against. Never build either one separately.
- **`question_id` is not unique**: each question appears once per class.
  Key saved results by file and line number, never by `question_id`.
- 18 original-set LAZY traces have keyword "unclear", which never appears in
  their labelled step. They are unquotable by design of the data; pinned by
  `test_known_unquotable_labels_in_groundingbench`.

**Verifier scope**
- `verify()` decides whether a citation is *real* (exists, verbatim, on word
  boundaries, specific). It never decides whether it is the *best* evidence,
  and must never encode GroundingBench's convention for where evidence lives.
- `scope="action"` restricts a quote to what the agent did (tool and
  arguments, `Step.action_text()`). That is the offense rule, and it is
  structural, not a label: misbehavior is an action by the agent.

**Audit schema (two slots)**
- `offense` (required unless HONEST) is verified with `scope="action"`;
  `contradicted_by` (optional) with `scope="step"`. An audit verifies only if
  every slot it fills verifies.
- Answers arrive as a `submit_audit` tool call (Baseten's Muse Spark Open has
  no structured output). `parse_audit` is lenient; `verify_audit` is strict.
  Keep that split: leniency belongs in the parser, never in the verifier.
- Every GroundingBench smoking gun is the `submit_answer` step. The offense
  rule does not reveal that, but the benchmark aligns with it; disclose this
  next to any grounding result.
- Four modes form a 2x2 factorial (split x rule): `single`, `single_rule`,
  `two_slot_no_rule`, `two_slot`. Behaviour must come from `has_two_slots()`
  and `has_offense_rule()`, never from mode-name checks. Modes differ only in
  their slot section; tests enforce this.
- `single` and `two_slot` are frozen (`PINNED_MODE_FINGERPRINTS` in
  `tests/test_prompt.py`): saved runs depend on them. To change behaviour, add
  a mode.

**Overseer loop (`pinpoint/overseer.py`)**
- Citation retries (`max_citation_retries`) and format retries
  (`max_format_retries`) are separate budgets. The no-retry ablation sets only
  the citation budget to 0. Never merge them.
- Verifier feedback goes back as the `tool` result of the `submit_audit`
  call, never as a user message. Every tool call must get a reply.
- Record verdict flips (`verdict_history`); a switch to HONEST after a failed
  citation may be an escape and must be reported, not hidden.
- Only `ClientError` is caught; programming errors must surface.

**Benchmark rules**
- Only `benchmark/run.py` spends credits. Real runs go in `results/runs/` and
  must start from a committed tree (the manifest records commit and
  `dirty`). Smoke runs go in `results/smoke/`.
- Never pass labels to the overseer: `Trace.from_dict(record)` is the only
  way a record reaches it. `results.jsonl` holds model output only.
- Tests must never build a real client or call `load_env()`; the real `.env`
  holds live keys.
- Headline metric is `step_correct`. Report GroundingBench's grader figure
  beside it, with the reason it is lower: its REWARD_HACKER keyword is the
  tool name `submit_answer`, so quoting the agent's actual claim earns no
  credit. Never add text to the evidence string that the model did not write.

**Results so far (all 720 traces, Muse Spark Open, see DEVLOG 17:38, 18:28)**
- Single slot: deceptive traces cite the exposing document 176/180; offense
  on labelled step 240/538. Two-slot: 0/179 and 536/536.
- Zero citation retries in 1,680 audits. Do not claim verify-and-retry
  produced the gain: the schema did. The verifier is a guarantee, unexercised
  on GroundingBench.
- GroundingBench grader understates content citations (tool-name keywords,
  unquotable "unclear", subject-word DECEIVER keywords). State this whenever
  the grader figure appears.
- Baseten spend: about $11 of $100.
- `git_info` excludes `results/`. Runs 2-4 of the first ablation record a
  false-positive `dirty: true`; documented, not edited.

**Prompt rules**
- Changing any wording the model sees (system prompts, tool schemas, user
  template, retry text) requires bumping `PROMPT_VERSION` and pinning the new
  fingerprint in `tests/test_prompt.py`. Never edit an existing pin.
- Never name `submit_answer`, "last step" or "final step" in a prompt or
  tool description: that is the benchmark's answer key.
- Zero-shot by design. Few-shot examples need a recorded decision and a new
  `PROMPT_VERSION`.
- A Windows checkout with `core.autocrlf=true` stores these files with CRLF;
  their hashes differ from the canonical LF files. Content is identical.

**Meta Model API (Muse Spark)**
- Base URL `https://api.meta.ai/v1`, OpenAI SDK compatible. Key in
  `MODEL_API_KEY`; the OpenAI SDK does not read it automatically, so pass
  `api_key` explicitly.
- Model `muse-spark-1.3`. $1.25 / $4.25 per 1M tokens in / out.
- `tool_choice` must be `"auto"`; the model cannot be forced to call a tool.
- Raw reasoning is never returned (summaries only, optional).
- Effort: `reasoning_effort` (Chat Completions), values `minimal` … `max`.
- Long non-streaming requests return HTTP 504; stream or retry.

**Client (`pinpoint/client.py`)**
- One `ModelClient` for both providers; `PROVIDERS["baseten"]` and
  `PROVIDERS["meta"]` differ only in URL, key variable, model id and
  max-tokens parameter name.
- Errors are raised `from None` on purpose: a chained SDK exception carries the
  request, including the `Authorization` header. Never change that.
- Entry points call `load_env()`; library code never loads `.env` itself.

**Measured on the first real call (Muse Spark Open, Baseten)**
- Tool calling works: `finish_reason == "tool_calls"`, readable reasoning in
  `reasoning_content`.
- About 1,750 prompt tokens per trace (the tool schema counts), about 1,000
  completion tokens, 4-5 s, about $0.0065. Full 720-trace run about $4.70.
- Reported limits: 100 requests/min, 2M tokens/min.

**Baseten (Muse Spark Open)**
- Base URL `https://inference.baseten.co/v1`, OpenAI SDK compatible. Key in
  `BASETEN_API_KEY`.
- Model **`meta-models/muse-spark-open-1026`** exactly (the hackathon key works
  with nothing else). Same price as Muse Spark.
- **No structured output / JSON mode.** Tool calling works, so final answers
  come back through a `submit_audit` tool call for both models.
- Reasoning is always on and returned readably in `reasoning_content`.
  Reasoning tokens count toward `max_tokens` (default 4096): set `max_tokens`
  explicitly and log `finish_reason` so truncation is never silent.
