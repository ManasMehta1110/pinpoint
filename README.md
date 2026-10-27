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
