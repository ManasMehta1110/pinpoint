"""Tests for benchmark.fetch_data. Hermetic: no test touches the network."""

from __future__ import annotations

import email.message
import hashlib
import http.client
import io
import re
import ssl
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from benchmark import fetch_data
from benchmark.fetch_data import FetchError, TraceFile, fetch_one

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTENT = b'{"question_id": "q1"}\n{"question_id": "q2"}\n'
TARGET = "traces.jsonl"


def _spec(content: bytes = CONTENT, n_traces: int = 2) -> TraceFile:
    return TraceFile(name=TARGET, sha256=hashlib.sha256(content).hexdigest(), n_traces=n_traces)


class FakeDownloader:
    """Writes fixed bytes to the destination, then optionally raises.

    Writing before raising mimics a connection that drops mid-download and
    leaves a partial file behind for the caller to clean up.
    """

    def __init__(self, content: bytes = CONTENT, error: BaseException | None = None) -> None:
        self.content = content
        self.error = error
        self.calls: list[str] = []

    def __call__(self, url: str, dest: Path) -> None:
        self.calls.append(url)
        dest.write_bytes(self.content)
        if self.error is not None:
            raise self.error


def _leftover_parts(data_dir: Path) -> list[Path]:
    return sorted(data_dir.glob("*.part"))


# --- pinning ---------------------------------------------------------------


def test_commit_matches_pyproject_pin() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    eval_deps = pyproject["project"]["optional-dependencies"]["eval"]
    pins = [
        match.group(1)
        for dep in eval_deps
        if (match := re.search(r"GroundingBench@([0-9a-f]{40})\b", dep))
    ]
    assert pins == [fetch_data.GROUNDINGBENCH_COMMIT]


def test_source_url_targets_pinned_commit() -> None:
    assert fetch_data.source_url("eval_traces.jsonl") == (
        "https://raw.githubusercontent.com/ManasMehta1110/GroundingBench/"
        f"{fetch_data.GROUNDINGBENCH_COMMIT}/data/eval_traces.jsonl"
    )


def test_trace_file_specs_are_well_formed() -> None:
    names = [spec.name for spec in fetch_data.TRACE_FILES]
    assert len(names) == len(set(names))
    for spec in fetch_data.TRACE_FILES:
        assert re.fullmatch(r"[0-9a-f]{64}", spec.sha256), spec.name
        assert spec.n_traces > 0
    # The README and paper describe a 720-trace held-out set.
    assert sum(spec.n_traces for spec in fetch_data.TRACE_FILES) == 720


# --- fetch_one ---------------------------------------------------------------


def test_downloads_missing_file(tmp_path: Path) -> None:
    fake = FakeDownloader()
    assert fetch_one(_spec(), tmp_path, downloader=fake) == "downloaded"
    assert (tmp_path / TARGET).read_bytes() == CONTENT
    assert len(fake.calls) == 1
    assert _leftover_parts(tmp_path) == []


def test_creates_missing_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "nested" / "data"
    fetch_one(_spec(), data_dir, downloader=FakeDownloader())
    assert (data_dir / TARGET).read_bytes() == CONTENT


def test_valid_existing_file_is_not_redownloaded(tmp_path: Path) -> None:
    (tmp_path / TARGET).write_bytes(CONTENT)
    fake = FakeDownloader()
    assert fetch_one(_spec(), tmp_path, downloader=fake) == "present"
    assert fake.calls == []


def test_corrupt_existing_file_is_replaced(tmp_path: Path) -> None:
    (tmp_path / TARGET).write_bytes(b"truncated")
    assert fetch_one(_spec(), tmp_path, downloader=FakeDownloader()) == "replaced"
    assert (tmp_path / TARGET).read_bytes() == CONTENT


def test_crlf_copy_is_replaced_with_canonical_bytes(tmp_path: Path) -> None:
    # A Windows checkout with core.autocrlf=true holds the same content with
    # CRLF endings. Its hash differs, so it is replaced, not trusted.
    (tmp_path / TARGET).write_bytes(CONTENT.replace(b"\n", b"\r\n"))
    assert fetch_one(_spec(), tmp_path, downloader=FakeDownloader()) == "replaced"
    assert (tmp_path / TARGET).read_bytes() == CONTENT


def test_force_redownloads_valid_file(tmp_path: Path) -> None:
    (tmp_path / TARGET).write_bytes(CONTENT)
    fake = FakeDownloader()
    assert fetch_one(_spec(), tmp_path, force=True, downloader=fake) == "replaced"
    assert len(fake.calls) == 1


def test_hash_mismatch_is_rejected_without_leaving_files(tmp_path: Path) -> None:
    with pytest.raises(FetchError, match="sha256 mismatch"):
        fetch_one(_spec(), tmp_path, downloader=FakeDownloader(content=b"tampered\n"))
    assert not (tmp_path / TARGET).exists()
    assert _leftover_parts(tmp_path) == []


def test_failed_refresh_keeps_previous_good_file(tmp_path: Path) -> None:
    (tmp_path / TARGET).write_bytes(CONTENT)
    with pytest.raises(FetchError):
        fetch_one(_spec(), tmp_path, force=True, downloader=FakeDownloader(content=b"bad\n"))
    assert (tmp_path / TARGET).read_bytes() == CONTENT
    assert _leftover_parts(tmp_path) == []


def test_trace_count_mismatch_is_rejected(tmp_path: Path) -> None:
    # Correct bytes but a wrong expected count: the second check must still fire.
    with pytest.raises(FetchError, match="expected 3 traces, found 2"):
        fetch_one(_spec(n_traces=3), tmp_path, downloader=FakeDownloader())
    assert not (tmp_path / TARGET).exists()
    assert _leftover_parts(tmp_path) == []


@pytest.mark.parametrize("error", [FetchError("connection dropped"), KeyboardInterrupt()])
def test_interrupted_download_leaves_no_partial_file(tmp_path: Path, error: BaseException) -> None:
    with pytest.raises(type(error)):
        fetch_one(_spec(), tmp_path, downloader=FakeDownloader(error=error))
    assert not (tmp_path / TARGET).exists()
    assert _leftover_parts(tmp_path) == []


def test_count_traces_ignores_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    path.write_bytes(b'{"a": 1}\n\n   \n{"a": 2}\n')
    assert fetch_data.count_traces(path) == 2


def test_count_traces_counts_final_line_without_newline(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    path.write_bytes(b'{"a": 1}\n{"a": 2}')
    assert fetch_data.count_traces(path) == 2


# --- download: retry policy --------------------------------------------------


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "err", email.message.Message(), None)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(time, "sleep", recorded.append)
    return recorded


def _patch_urlopen(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[bytes | BaseException]
) -> list[str]:
    """Make urlopen return or raise each outcome in turn; return the call log."""
    calls: list[str] = []

    def fake_urlopen(url: str, timeout: float) -> io.BytesIO:
        calls.append(url)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return io.BytesIO(outcome)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def test_download_writes_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    calls = _patch_urlopen(monkeypatch, [CONTENT])
    dest = tmp_path / "out"
    fetch_data.download("https://example/x", dest)
    assert dest.read_bytes() == CONTENT
    assert calls == ["https://example/x"]
    assert sleeps == []


@pytest.mark.parametrize("code", [400, 403, 404])
def test_client_error_fails_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float], code: int
) -> None:
    calls = _patch_urlopen(monkeypatch, [_http_error(code)])
    with pytest.raises(FetchError, match=f"HTTP {code}"):
        fetch_data.download("https://example/x", tmp_path / "out")
    assert len(calls) == 1
    assert sleeps == []


def test_server_error_is_retried_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    calls = _patch_urlopen(monkeypatch, [_http_error(503), _http_error(502), CONTENT])
    dest = tmp_path / "out"
    fetch_data.download("https://example/x", dest)
    assert dest.read_bytes() == CONTENT
    assert len(calls) == 3
    assert sleeps == [2, 4]


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("dns failure"),
        TimeoutError(),
        ConnectionResetError(),
        http.client.IncompleteRead(b"partial"),
        ssl.SSLError("bad record mac"),
    ],
)
def test_each_transient_error_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float], error: BaseException
) -> None:
    calls = _patch_urlopen(monkeypatch, [error, CONTENT])
    fetch_data.download("https://example/x", tmp_path / "out")
    assert len(calls) == 2
    assert sleeps == [2]


def test_retries_are_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    errors: list[bytes | BaseException] = [urllib.error.URLError("down")] * 3
    calls = _patch_urlopen(monkeypatch, errors)
    with pytest.raises(FetchError, match="giving up after 3 attempts"):
        fetch_data.download("https://example/x", tmp_path / "out")
    assert len(calls) == 3
    assert sleeps == [2, 4]


def test_retry_overwrites_partial_body_from_failed_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    dest = tmp_path / "out"
    dest.write_bytes(b"stale partial bytes from an earlier attempt")
    _patch_urlopen(monkeypatch, [_http_error(503), CONTENT])
    fetch_data.download("https://example/x", dest)
    assert dest.read_bytes() == CONTENT


# --- main --------------------------------------------------------------------


def _patch_specs(monkeypatch: pytest.MonkeyPatch, downloader: FakeDownloader) -> None:
    monkeypatch.setattr(fetch_data, "TRACE_FILES", (_spec(),))
    monkeypatch.setattr(fetch_data, "download", downloader)


def test_main_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_specs(monkeypatch, FakeDownloader())
    assert fetch_data.main(["--data-dir", str(tmp_path)]) == 0
    assert "downloaded" in capsys.readouterr().out


def test_main_reports_verification_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_specs(monkeypatch, FakeDownloader(content=b"bad\n"))
    assert fetch_data.main(["--data-dir", str(tmp_path)]) == 1
    assert "sha256 mismatch" in capsys.readouterr().err


def test_main_reports_unusable_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "data"
    blocker.write_text("a file where the directory should be", encoding="utf-8")
    _patch_specs(monkeypatch, FakeDownloader())
    assert fetch_data.main(["--data-dir", str(blocker)]) == 1
    assert capsys.readouterr().err.startswith("error:")
