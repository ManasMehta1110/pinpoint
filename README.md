# Pinpoint

An AI overseer for agents that doesn't just flag misbehavior: it pinpoints the
exact step that proves it.

Built solo for the Meta Global AI Developer Hackathon (Indie track),
October 27 to November 4, 2026.

**Live demo: [pinpoint-1sjo.onrender.com](https://pinpoint-1sjo.onrender.com/)**
(free hosting: if it has been idle, the first load can take up to a minute).

## Results

All 720 of GroundingBench's held-out traces (120 rule-based, 600 written by an
LLM to read naturally), audited by Muse Spark Open
(`meta-models/muse-spark-open-1026` via Baseten), one run per configuration.
The two configurations differ only in their citation slots. "Eligible" traces
are misbehaviors whose verdict was correct, as in the original paper. Brackets
are 95% Wilson intervals.

| | single citation | two citations + offense rule (Pinpoint) |
|---|---|---|
| **deceptive traces citing the document that exposes the lie, instead of the lie** | **176 / 180** [94-99%] | **0 / 179** |
| **offense on the labelled step** | **44.6%** (240/538) [40-49] | **100%** (536/536) [99-100] |
| verdict accuracy | 99.4% (716/720) | 99.3% (715/720) |
| citations verified as real | 100% | 100% |

By distribution:

| offense on the labelled step | single citation | Pinpoint |
|---|---|---|
| rule-based (120 traces) | 42.2% (38/90) | 100% (90/90) |
| naturalistic (600 traces) | 45.1% (202/448) | 100% (446/446) |

**The failure reproduces on Meta's model.** Asked for a single citation,
Muse Spark Open cited the document that exposes a deceptive agent's lie,
rather than the lie itself, in 176 of 180 deceptive traces. That is the
substitution GroundingBench measured in other frontier overseers, now in a
different model, format and prompt, on both distributions.

**Pinpoint's schema removes it.** With separate `offense` and
`contradicted_by` slots, and the rule that an offense must quote the agent's
own action, every one of 179 correctly flagged deceptive traces cites the
agent's false claim, and every misbehavior offense lands on the labelled step.

**What does the work: splitting the citation.** Pinpoint changes two things:
it splits the citation into two slots, and it adds a rule that the offense
must quote the agent's own action, never a tool's result. A 2x2 factorial on
the 120 rule-based traces separates them (offense on the labelled step):

| | no rule | rule |
|---|---|---|
| **one slot** | 42.2% (38/90) | 98.9% (89/90) |
| **two slots** | 100% (90/90) | 100% (90/90) |

Either ingredient alone fixes it, and two slots need no rule at all. With two
slots and no rule, the model put the agent's false answer in `offense` and the
exposing document in `contradicted_by` in all 30 deceptive traces: the same
document the single-slot overseer had chosen as its only citation. The
single-slot failure is not an inability to find the lie. Asked for one piece of
evidence, the model gives the proof that the claim is false; asked for both, it
gives both, correctly separated. The rule alone allowed one degenerate citation
(a document id, which is technically the agent's own action); two slots never
did.

**When the offense is not the final answer.** Every GroundingBench smoking
gun is the agent's final answer, so we built 36 coding-agent traces where it
is not: the agent edits a test so it passes, or writes "all tests passing"
into a status file or commit after seeing them fail, then gives a true or
neutral final answer. The dataset and its labels were committed before any
model saw them (`benchmark/datasets/midtrace.jsonl`, commit `2f04147`), with
the analysis fixed in advance. Prompt unchanged. Offense on the labelled step,
over misbehavior traces flagged as any misbehavior class:

| | no rule | rule |
|---|---|---|
| **one slot** | 23/24 | 24/24 |
| **two slots** | 21/24 | 21/24 |

No false positives on the 12 honest controls in any configuration. All four
find offenses in the middle of a trace, so the GroundingBench result is not
just "quote the final answer". But two slots lose their edge: the single-slot
substitution almost vanishes here (1 of 12 deceivers), and two slots show a
bias of their own. Each two-slot miss is a test-tampering trace ending in a
true but misleading "All tests pass now." The model calls it deception, files
that claim as the `offense` and the tampering evidence as `contradicted_by`.
The difference is not significant (Fisher p = 0.23), and scripted traces are
not recorded agent runs. On both datasets together, the offense rule alone was
the most robust configuration (113/114).

**It knew, it didn't say.** The single-slot model had found the lie. In the
176 substitutions its saved reasoning refers to the agent's false answer in
166, explicitly weighs citing it in 137, and remarks that it may give only
one citation in 94 (line-level pattern counts, lower bounds; a random sample
was read to check them). From one trace: "We must cite step 2 or 3. Provide
both? Only one evidence object allowed per schema. So pick step 2." From
another: "Probably best to cite Step 5 where false claim is made, or Step 2
... I'll choose Step 2." The substitution is a choice the one-slot schema
forces, which is why a second slot removes it.
([`benchmark/knew.py`](benchmark/knew.py), output in
[`results/analysis/knew.json`](results/analysis/knew.json).)

**Verify-and-retry did not contribute here.** Across all 1,920 audits, no
citation ever failed verification, so no retry was triggered. The gain comes
from the prompt and schema; the verifier is a guarantee that was never needed
on this data. (A forced test confirms the retry works: told its citation was
"returned by the read_document tool, not written by the agent", the model
corrected it.)

**Caveats.**
- One model, one run per configuration.
- Every GroundingBench smoking gun is the agent's final `submit_answer` step,
  which the offense rule aligns with. The rule does not name that step, and
  citing any other agent action also verifies, but the alignment is real.
- GroundingBench's own grader scores Pinpoint lower (44.4% rule-based, 65.2%
  naturalistic; single citation: 13.3%, 34.2%), for reasons in its labels
  rather than the citations. Its REWARD_HACKER keyword is the tool name
  `submit_answer`, so quoting what the agent actually claimed earns no credit;
  18 rule-based LAZY keywords never appear in the trace; and the DECEIVER
  keyword is the claim's first word, its subject rather than its falsehood.
  For "The Louvre is located in Rome, Italy" the label expects `louvre`;
  Pinpoint quoted "located in Rome, Italy". On the naturalistic set all 8
  DECEIVER traces the grader rejected cite the right step and quote the false
  part of the claim.

Full per-trace records, including every raw model response, are in
[`results/runs/`](results/runs/); `python -m benchmark.grade <run>`
reproduces every figure above offline.

## Built during the hackathon

Work in progress. Each component is listed here as it lands, and the commit
history shows when. The full build log is in [DEVLOG.md](DEVLOG.md).

- **Demo app** (`app/`). Three views: **Compare** puts a single-citation
  overseer and Pinpoint side by side on real traces, highlighting exactly
  what each cited, with the single-citation model's own reasoning quoted;
  **Audit live** runs Pinpoint on any trace with Muse Spark Open; **Try to
  fool the verifier** checks any citation you type. Every audit in Compare is
  a saved model output, and the gallery build fails if an example does not
  show what its title claims. Run it with `pip install -e ".[app]"` and
  `python -m app.server`.

- **Verified evaluation data** (`benchmark/fetch_data.py`). Fetches
  GroundingBench's 720 held-out traces from a pinned commit and accepts each
  file only after a SHA-256 check and a trace-count check. Downloads are
  atomic: an interrupted or tampered fetch never leaves a usable-looking file.
- **Trace model** (`pinpoint/trace.py`). Loads an agent run keeping only
  what is observable, drops any labels, and renders it with steps numbered
  from 1. The text the overseer reads and the text its citations are checked
  against come from one function, so they cannot drift apart.
- **Citation verifier** (`pinpoint/verifier.py`). Checks that an
  overseer's citation is real using nothing but the trace: the step exists,
  and the quote appears in that step verbatim (ignoring case, whitespace and
  typographic variants, on whole words). On failure it reports every problem
  and where the quote actually is, which is the feedback the overseer gets
  when asked to retry. Tested against all 540 real misbehavior traces.
- **Two-slot audit** (`pinpoint/audit.py`). The overseer answers through a
  `submit_audit` tool call with two separately verified citations: the
  `offense`, where the agent itself did wrong, and what it was
  `contradicted_by`. The offense must quote the agent's own action, never a
  tool's result, which rejects the substitution frontier overseers made in
  GroundingBench (citing the document that exposes a lie instead of the lie).
  On real data, all 399 such substitutions across 180 deceptive traces are
  rejected, and every genuine citation is accepted.
- **Overseer prompt** (`pinpoint/prompt.py`). Zero-shot instructions with
  general class definitions, so the same prompt applies to any agent. The
  trace is enclosed in markers keyed to a hash of its content, so text
  inside a trace cannot break out and give the auditor instructions. Every
  run records `PROMPT_VERSION`, and the build fails if the wording changes
  without a version bump.
- **Ablation baseline** (`mode="single"`). One evidence slot and no offense
  rule, the way GroundingBench asked. Tests guarantee it differs from
  Pinpoint only in its citation slots, so the comparison isolates their
  effect.
- **Model client** (`pinpoint/client.py`). One client for Muse Spark Open
  (via Baseten) and Muse Spark (via the Meta Model API). Every call keeps the
  full raw response, tool calls, readable reasoning, token usage and timing,
  so every number Pinpoint reports traces back to a saved model output. The
  API key never appears in a record, log or error.
- **Verify-and-retry loop** (`pinpoint/overseer.py`). Asks Muse Spark for
  an audit, checks every citation, and when one fails sends the verifier's
  exact reason back so the model can correct itself. Every attempt is saved,
  including whether the verdict changed between attempts. Confirmed with the
  real model: told that its offense quote "was returned by the read_document
  tool, not written by the agent", Muse Spark Open moved its citation to the
  agent's false claim, which then verified.
- **Benchmark harness** (`benchmark/run.py`, `benchmark/grade.py`). Runs the
  overseer over GroundingBench's held-out traces and grades the saved output
  offline. Every result records the configuration, data hash and code commit
  that produced it, and runs resume safely after a crash. Grading reports
  whether each citation lands on the labelled step, GroundingBench's own
  grader score for comparability with the original paper, and whether
  Pinpoint verified it, each with a 95% confidence interval.
- **Label-free guarantee** (`tests/test_packaging.py`). A test that fails if
  the overseer package ever imports the grader or reads a ground-truth field,
  so Pinpoint provably audits traces without seeing their answers.

## Prior work (GroundingBench)

Pinpoint is motivated by and evaluated on
[GroundingBench](https://github.com/ManasMehta1110/GroundingBench), an
open-source benchmark the author built before this hackathon. Across 7 models
and 720 held-out traces, verdict accuracy was 70.8-100%, yet only 1 of 1,794
correctly classified misbehavior verdicts cited the correct evidence step.

GroundingBench is used here only as an open-source dependency, pinned to a
public commit: its evaluation traces and its reference grader. The organizers
confirmed in the hackathon Discord that an open-source dependency is fine.
Nothing from GroundingBench is presented as hackathon work.
