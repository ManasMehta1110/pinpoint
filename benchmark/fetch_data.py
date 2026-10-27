"""Fetch GroundingBench's held-out evaluation traces at a pinned commit.

GroundingBench (prior work, https://github.com/ManasMehta1110/GroundingBench)
supplies the traces Pinpoint is evaluated on. They are downloaded rather than
vendored, from an immutable commit, and every file is checked against a known
SHA-256 and trace count before it is accepted. A partial or tampered download
is never left on disk: each file is written to a temporary path in the same
directory and moved into place only once both checks pass.

Usage:
    python -m benchmark.fetch_data            # fetch anything missing or corrupt
    python -m benchmark.fetch_data --force    # re-download everything
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import os
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

GROUNDINGBENCH_REPO = "ManasMehta1110/GroundingBench"
# Must equal the commit pinned for grounding-bench in pyproject.toml; a test
# enforces this so the traces and the grader can never come from different
# versions of GroundingBench.
GROUNDINGBENCH_COMMIT = "5fd7325d807e0128e330391244ee36023056e012"

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

TIMEOUT_S = 30
MAX_ATTEMPTS = 3
CHUNK_BYTES = 1 << 16


@dataclass(frozen=True)
class TraceFile:
    """A file to fetch, with the checks it must pass to be accepted."""

    name: str
    sha256: str
    n_traces: int


# Hashes are of the files exactly as served from the pinned commit (LF line
# endings). A Windows checkout with core.autocrlf=true holds CRLF copies with
# different hashes but identical content; those are not what this checks.
TRACE_FILES: tuple[TraceFile, ...] = (
    TraceFile(
        name="eval_traces.jsonl",
        sha256="ae5888af0c832b7213d9f5d43676968f4ca488c6d2f91548893f6d7b2e53456e",
        n_traces=120,
    ),
    TraceFile(
        name="eval_traces_realistic.jsonl",
        sha256="690320eaaf91d40c7b2029491fcb77700dc77329f89c613dbe770c84bb01cb6c",
        n_traces=600,
    ),
)

Downloader = Callable[[str, Path], None]

# Failures worth retrying: connection setup (URLError), and a stream that
# drops or stalls partway (IncompleteRead, timeouts, resets, TLS errors).
_TRANSIENT_ERRORS = (
    urllib.error.URLError,
    http.client.HTTPException,
    TimeoutError,
    ConnectionError,
    ssl.SSLError,
)


class FetchError(RuntimeError):
    """A trace file could not be fetched or failed verification."""


def source_url(name: str) -> str:
    """Return the raw URL for ``data/<name>`` at the pinned commit."""
    return (
        f"https://raw.githubusercontent.com/{GROUNDINGBENCH_REPO}/"
        f"{GROUNDINGBENCH_COMMIT}/data/{name}"
    )


def sha256_of(path: Path) -> str:
    """Return the hex SHA-256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def count_traces(path: Path) -> int:
    """Return the number of non-blank lines in a JSONL file."""
    with path.open("rb") as fh:
        return sum(1 for line in fh if line.strip())


def download(url: str, dest: Path) -> None:
    """Download ``url`` to ``dest``, retrying transient failures.

    Client errors (4xx) mean the URL or commit is wrong, so they fail at once
    instead of being retried.
    """
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT_S) as resp, dest.open("wb") as out:
                while chunk := resp.read(CHUNK_BYTES):
                    out.write(chunk)
            return
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                raise FetchError(f"HTTP {exc.code} fetching {url}") from exc
            last_error = exc
        except _TRANSIENT_ERRORS as exc:
            last_error = exc
        if attempt < MAX_ATTEMPTS:
            time.sleep(2**attempt)
    raise FetchError(
        f"giving up after {MAX_ATTEMPTS} attempts fetching {url}: {last_error}"
    ) from last_error


def fetch_one(
    spec: TraceFile,
    data_dir: Path,
    *,
    force: bool = False,
    downloader: Downloader | None = None,
) -> str:
    """Make sure ``data_dir/spec.name`` exists and passes verification.

    Returns ``"present"`` if a valid copy was already there, ``"downloaded"``
    if the file was absent, or ``"replaced"`` if an existing copy was invalid
    or ``force`` was set. On any failure the previous file, if any, is left
    untouched and no temporary file remains.

    ``downloader`` defaults to the module's ``download``, looked up at call
    time so tests can substitute it without touching the network.
    """
    if downloader is None:
        downloader = download
    target = data_dir / spec.name
    if target.exists():
        if not force and sha256_of(target) == spec.sha256:
            return "present"
        status = "replaced"
    else:
        status = "downloaded"

    data_dir.mkdir(parents=True, exist_ok=True)
    # The temp file lives in data_dir so os.replace stays on one filesystem
    # and is therefore atomic.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{spec.name}.", suffix=".part", dir=data_dir)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        downloader(source_url(spec.name), tmp)
        actual = sha256_of(tmp)
        if actual != spec.sha256:
            raise FetchError(f"{spec.name}: sha256 mismatch (expected {spec.sha256}, got {actual})")
        n_traces = count_traces(tmp)
        if n_traces != spec.n_traces:
            raise FetchError(f"{spec.name}: expected {spec.n_traces} traces, found {n_traces}")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--force", action="store_true", help="re-download even valid files")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="destination directory")
    args = parser.parse_args(argv)

    try:
        for spec in TRACE_FILES:
            status = fetch_one(spec, args.data_dir, force=args.force)
            print(f"{status:>10}  {spec.name}  ({spec.n_traces} traces, sha256 {spec.sha256[:12]})")
    except (FetchError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
