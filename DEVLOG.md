# Development log

A chronological record of what was built during the hackathon, what was
checked, and what was decided. Times are IST (UTC+5:30); the hackathon's
official times are US Eastern. Commit hashes link each entry to the history.

---

## 2026-10-27

### Before the first commit: research only (≈08:00–08:40 IST)

No code written. Read the official documentation to settle API details
before building:

- **Meta Model API**: OpenAI-SDK compatible at `https://api.meta.ai/v1`,
  model `muse-spark-1.3`, `tool_choice` restricted to `"auto"`, raw reasoning
  not returned, 504 on long non-streaming calls.
- **Baseten**: `https://inference.baseten.co/v1`, model
  `meta-models/muse-spark-open-1026`. Muse Spark Open has **no structured
  output**, so both models will return their final answer through a tool call.
  Its reasoning is readable via `reasoning_content` and always on, which can
  exhaust the default 4096 `max_tokens`.
- Organizers confirmed in Discord that using an open-source dependency
  (GroundingBench) is fine.

The organizers announced "Hacking has officially started!" in the hackathon
Discord before the first commit below.

### 08:44 · Skeleton · `1cd51f4`

`LICENSE` (MIT), `.gitignore`, `.gitattributes`, `.env.example`, `README.md`.

- `.gitignore` committed before any key exists on disk. Audited with
  `git check-ignore`: 11 secret/cache paths confirmed ignored (`.env`,
  `.env.local`, `.env.production`, `.venv/`, `data/`, caches), 8 source paths
  confirmed tracked, including `.env.example`.
- `.gitattributes` forces LF line endings so Windows editors can't introduce
  CRLF noise into a public repo.
- This commit was made by Claude. From here on Manas makes every commit.

### 08:50 · Packaging · `c01f3bd` (pushed to GitHub)

`pyproject.toml`, `pinpoint/__init__.py`, `tests/test_packaging.py`.

- GroundingBench is an **optional** `eval` extra pinned to commit
  `5fd7325`, not a core dependency, so the `pinpoint` package cannot import
  the grader even by accident.
- `tests/test_packaging.py` enforces the label-free rule by parsing every
  source file under `pinpoint/`. Mutation-tested: a planted
  `from env.reward import ...`, `r["smoking_gun_step"]`, and `t.policy_class`
  were each caught; a legitimate relative import was correctly allowed.
- Verified pip installed GroundingBench at exactly `5fd7325`, and that its
  grader reproduces the paper's worked example (grounded 1.5, ungrounded 1.0).
- ruff clean, mypy `--strict` clean, 3 tests pass.

### 08:56 · Evaluation data fetcher · `a9bc817`

`benchmark/__init__.py`, `benchmark/fetch_data.py`, `tests/test_fetch_data.py`.

Downloads the two held-out trace files from GroundingBench at the pinned
commit. Each file must pass a SHA-256 check and a trace-count check; it is
written to a temporary file and moved into place only after both pass, so an
interrupted or tampered download never leaves a plausible-looking file.

**Findings**
- The SHA-256 of the GitHub files differed from the local GroundingBench
  copies. Cause: `core.autocrlf=true` on the Windows checkout stored CRLF
  line endings. After stripping `\r`, the hashes are identical. The script
  pins the canonical LF hashes.
- The pinned commit `5fd7325` is one commit ahead of the local GroundingBench
  checkout; the only difference is the LICENSE copyright line.
  `env/reward.py`, `env/parse.py` and `env/trace_format.py` are byte-identical,
  so benchmark numbers are comparable with the paper's.

**Bugs found in review, fixed before testing**
- The `downloader` default was bound at import time, so tests could never
  redirect `main()` away from the network. Now resolved at call time.
- A connection dropping mid-download (`IncompleteRead`, `SSLError`) was not
  retried and would have surfaced as a raw traceback. Now retried.

**Audit**
- Python floor raised to 3.11 (tests use `tomllib`); mypy now covers
  `pinpoint/`, `benchmark/` and `tests/`.
- ruff clean, mypy `--strict` clean on 5 files, **34 tests pass**, no network
  used by any test.
- Mutation testing: 7 of 7 planted bugs caught (skipped hash check, skipped
  count check, no temp-file cleanup, installing before verifying, retrying
  4xx, trusting an existing file without hashing it, ignoring `--force`).
- Real run: both files downloaded and verified; second run made no network
  calls; a deliberately corrupted file was detected and replaced. All 720
  traces present, classes balanced (30×4 and 150×4).

### Docs · `5a6d668`

Added `CLAUDE.md` (rules, audit protocol, layout, commands, API facts) and
this log. README now lists shipped components.

### 09:46 · Trace model and renderer · `8d59055`

`pinpoint/trace.py`, `tests/test_trace.py`.

Loads an agent trace keeping only what is observable (question, steps,
claimed answer, an id) and renders it for the overseer. Every other key in an
input record, including GroundingBench's labels, is dropped on load.

**Design**
- Steps are numbered from 1 everywhere, matching GroundingBench's
  `smoking_gun_step`. `get_step` rejects `True` (which Python treats as 1)
  and negative indices (which Python lists count from the end).
- The text the overseer reads for a step and the text a citation is checked
  against both come from `Step.text()`. A quote the model genuinely copied
  can never fail verification because of formatting drift.
- Validation names the offending field (e.g. `steps[1].tool must be a single
  line`). Multi-line tool names are rejected because one could inject a fake
  `Step N:` header into the prompt.

**Bug found in review, fixed before testing**
- Arguments were copied shallowly and results not at all. Search results are
  lists, so mutating the input record after loading would have changed the
  audited trace. Now deep-copied.

**Audit**
- ruff clean, mypy `--strict` clean on 7 files, **87 tests pass** (53 new).
- All 720 real traces load, render, and round-trip through `get_step`.
- Mutation testing: 10 of 10 planted bugs caught (0-based lookup, bool as
  index, negative index wrap, 0-based rendering, render drifting from text,
  shallow copy, string accepted as a step list, multi-line tool name, a label
  field added to `Trace`, unsorted JSON keys).

**Finding: GroundingBench's DECEIVER keyword can be uninformative.**
In one realistic trace two documents give 1066 and the agent submits 1016,
but the ground-truth keyword is "battle", the claim's first content word,
which also appears in the question and two other steps. The reference grader
therefore credits "cited step 5 and mentioned *battle*", not "identified the
false date". This is the limitation the GroundingBench paper already states.
Consequences: report benchmark grounding numbers with this caveat, and choose
demo traces where the false fact is unmistakable.

### 10:49 · Label-free citation verifier · `bbd02bb`

`pinpoint/verifier.py`, `tests/test_verifier.py`.

Checks that an overseer's citation (verdict, step, quote) is *real*, using
only the trace: valid verdict; the step is a true integer that exists; the
quote is specific and pinpoint-sized; the quote appears in that step ignoring
case, whitespace, curly quotes and dash styles, on word boundaries. Every
problem is reported together, plus where the quote does appear, so the
overseer can be told exactly what to fix.

**Design decision: no class-consistency check.** An earlier plan had the
verifier require, e.g., that a DECEIVER citation point at the
`submit_answer` step. Dropped: that is GroundingBench's answer-key convention
for where evidence lives, and building it into a "label-free" verifier would
inflate every grounding number reported. The verifier judges whether a
citation is real, never whether it is the best one.

**Bugs found in review, fixed before testing**
- Punctuation outside quote marks (`"European Union".`) was not stripped, so
  faithful quotes failed. Stripping now repeats until stable.
- Substring matching accepted word fragments (`016` in `1016`). Matches must
  now fall on word boundaries; underscores count as boundaries.
- An invalid step with a real quote produced a misleading `quote_not_in_step`
  issue. The location hint now rides on the step error instead.

**Audit**
- ruff clean, mypy `--strict` clean on 9 files, sources ASCII-only,
  **183 tests pass** (96 new).
- Property tests on all 540 real misbehavior traces: every ground-truth
  citation whose keyword is in its labelled step verifies (522 of 522), and
  the same quote cited at a step that lacks it is rejected.
- Mutation testing: 14 of 14 planted bugs caught.

**Finding: 18 GroundingBench labels cannot be quoted.** In the original
distribution, 18 of 30 LAZY traces carry the keyword "unclear", but the
labelled step never says "unclear"; it says e.g. "I couldn't find a
definitive answer." The keyword is a class tag rather than text from the
trace, so the reference grader can only credit an overseer that writes the
word "unclear" itself: describing earns credit, quoting does not. The
realistic distribution has none of these (0 of 150). Relevant to how the
paper's LAZY-original numbers are read. Pinned by an exact-count test.

**Finding: `question_id` is not unique.** Each question appears once per
class (`q_0103` occurs 4 times). Results must be keyed by file and line, never
by `question_id`, or three of every four rows would be overwritten.

**Finding: real is not the same as decisive.** Citing the corroborating
document ("occurred in 1066", step 2) instead of the false claim (step 5)
verifies, correctly, because it is real. This is the failure mode the
GroundingBench paper measured, so verify-and-retry alone will not fix it; it
catches fabricated and misplaced citations. Addressing "decisive" is a
citation-schema and prompt question, taken up next.

### 11:01 · Two-slot audit and the offense rule · (uncommitted)

`pinpoint/audit.py`, `tests/test_audit.py`; `pinpoint/trace.py` and
`pinpoint/verifier.py` extended.

**Decision: two citation slots.** An audit now carries an `offense`
(required unless HONEST: where the agent itself did the wrong thing) and an
optional `contradicted_by` (what shows it is wrong). Asked for one piece of
evidence, frontier overseers cited the document exposing a lie instead of the
lie; with a slot for each, the two no longer compete. Two slots make the right
answer easier, not forced, so this will be measured: single vs two slots,
with and without verify-and-retry, on the 120-trace set.

**Decision: the offense rule.** A step has two parts, what the agent did
(tool and arguments) and what the tool returned. Misbehavior is an action by
the agent, so the offense must quote the agent's action; `contradicted_by`
may quote anything. `Step.action_text()` / `result_text()` split each step,
and `Step.text()` is now built from the two, keeping a single source.
`verify(..., scope="action")` enforces the rule and explains failures
precisely (returned by which tool; or the agent wrote that text in step N;
or it appears only in tool results). Label-free, valid for any agent trace.

Premise checked on real data before building: all 522 quotable ground-truth
citations lie in the agent's action; none lie only in a tool result.

**Disclosure for any reported result.** Every GroundingBench smoking gun is
the `submit_answer` step, for all three classes. The offense rule does not
hand the model that step (search queries and document ids are agent actions
too, and citing them verifies), but the benchmark's design aligns with the
rule. Results must say so.

**The `submit_audit` tool.** Final answers come back as a tool call
(verdict, confidence, offense, contradicted_by, rationale), because Muse
Spark Open on Baseten has no structured output. `parse_audit` is lenient
("Step 5" -> 5, "deceiver" -> DECEIVER, 90 -> 0.9, empty optional slot ->
absent); `verify_audit` is strict. Only structurally broken arguments (not a
JSON object) raise; every content problem becomes retry feedback. An audit
verifies only if its offense and any contradiction both verify.

**Audit**
- ruff clean, mypy `--strict` clean on 11 files, sources ASCII-only,
  **270 tests pass** (87 new).
- Real data: all 522 quotable ground-truth audits verify end to end through
  JSON -> `parse_audit` -> `verify_audit`; 399 of 399 attempts to cite an
  exposing document as the offense, across all 180 DECEIVER traces, are
  rejected with `quote_from_tool_result`.
- Mutation testing: 12 of 13 caught. The 13th (removing the bool guard in
  `_coerce_step`) was an equivalent mutant: the next line already returned
  `True` unchanged. The redundant guard was deleted rather than tested; the
  mutation that would actually turn `True` into step 1 is caught.

---

## Next

- `pinpoint/prompt.py`: system prompt defining the four classes and the two
  slots, with the trace delimited as untrusted data.
- Once the Meta API key is live: `pinpoint/client.py`, then one trace end to
  end with the raw response saved.
