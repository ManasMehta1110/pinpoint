# Pinpoint

An AI overseer for agents that doesn't just flag misbehavior: it pinpoints the
exact step that proves it.

Built solo for the Meta Global AI Developer Hackathon (Indie track),
October 27 to November 4, 2026.

## Built during the hackathon

Work in progress. Each component is listed here as it lands, and the commit
history shows when. The full build log is in [DEVLOG.md](DEVLOG.md).

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
