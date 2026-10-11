"""Server-side tool-call log (the "call witness"): a tamper-evident, append-only record of every tool call the server received.

The evidence that an agent called a tool should come from the server, not from the agent's own report.  Each entry records who
asked (self-reported client name and version, the tenant), what (tool and a hash of the canonical arguments), and what happened
(outcome, the result_digest the tool returned, duration).  Raw arguments, API keys, headers and tokens are never stored.

Storage: daily JSONL files ``calls-YYYYMMDD.jsonl`` (UTC) in ``JARVIS_CALL_LOG_DIR``, mode 600, one hash chain across all files
(each entry carries the previous entry's hash, so the first entry of a new day carries the previous day's final hash).  It is NOT
a ledger record: this module imports nothing from the store, so logging can never change the ledger's state root, history or
receipts.

Concurrency: ``append`` holds an in-process lock and an ``fcntl`` file lock (``msvcrt`` on Windows) across "read the head, assign
seq, write, fsync", so seq and prev_hash stay correct across threads and processes.

Failure policy (decided explicitly):
* writes to the ledger (``emr_remember``, ``emr_upsert``, ``POST/PATCH/DELETE /api/jarvis/memory*``) fail CLOSED: a preflight
  (lock, read the head, directory writable, free space) must pass before the call runs, or the call is refused with 503.
* every other call fails OPEN: the call is served, the failure is logged at ERROR and the log is flagged degraded.
* if the append fails after a write has already committed, the real result is still returned (it cannot be undone and the
  ledger's own history records it); the log is flagged degraded and every write refuses until the log recovers.  On recovery a
  ``gap`` entry records how many calls were not logged and since when, so ``verify`` shows the hole instead of hiding it.

Known limits (see docs/call_log.md): anyone who can write the directory can rebuild a whole chain, and trailing entries can be
truncated without breaking the chain.  ``verify`` returns the head hash so it can be recorded elsewhere.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

try:  # POSIX
    import fcntl

    def _lock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _unlock(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

except ImportError:  # Windows
    import msvcrt

    def _lock(fh) -> None:
        fh.seek(0)
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                time.sleep(0.01)

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


_log = logging.getLogger("jarvis.call_log")

GENESIS = "0" * 64
FILE_RE = re.compile(r"^calls-(\d{8})\.jsonl$")
MIN_FREE_BYTES = 1 << 20
WRITE_TOOLS = frozenset({"emr_remember", "emr_upsert"})
TRANSPORTS = ("mcp-stdio", "mcp-http", "http-tool", "http-api")
OUTCOMES = ("ok", "denied", "error", "denied_suppressed", "read_suppressed", "gap")


class CallLogError(Exception):
    """The log could not be read or written."""


# ----------------------------------------------------------------------------------------------------------- config


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    """JARVIS_CALL_LOG_ENABLED wins; unset means on in dev and test, off when JARVIS_ENV=production (the deploy turns it on)."""
    raw = os.environ.get("JARVIS_CALL_LOG_ENABLED")
    if raw is not None and raw.strip() != "":
        return _flag(raw)
    return (os.environ.get("JARVIS_ENV") or "").strip().lower() != "production"


def log_dir() -> Path:
    explicit = (os.environ.get("JARVIS_CALL_LOG_DIR") or "").strip()
    if explicit:
        return Path(explicit)
    store = (os.environ.get("JARVIS_STORE_PATH") or "").strip()
    return (Path(store).parent if store else Path("data")) / "call-log"


def retain_days() -> int:
    try:
        return max(1, int((os.environ.get("JARVIS_CALL_LOG_RETAIN_DAYS") or "").strip() or 90))
    except ValueError:
        return 90


def denied_cap_per_minute() -> int:
    try:
        return max(1, int((os.environ.get("JARVIS_CALL_LOG_DENIED_CAP_PER_MIN") or "").strip() or 60))
    except ValueError:
        return 60


# The REST routes that write ledger records (create_memory / update_memory / delete_memory in app/main.py): POST /memory, the promotion of an nx-search
# result, the memory pipeline that materialises a record, and PATCH/PUT/DELETE of a record. POST /memory/external-search is NOT here: it writes only
# when its auto_promote flag is set, which the log (it stores no arguments) cannot see, and without the flag it only reads.
LEDGER_WRITE_ROUTES = frozenset({
    ("POST", "/api/jarvis/memory"), ("POST", "/api/jarvis/memory/promote"), ("POST", "/api/jarvis/memory/pipeline"),
    ("PATCH", "/api/jarvis/memory/{id}"), ("PUT", "/api/jarvis/memory/{id}"), ("DELETE", "/api/jarvis/memory/{id}"),
})


def _is_rest_write(fields: dict[str, Any]) -> bool:
    """A ledger write over REST (POST /memory, PATCH/PUT/DELETE /memory/{id}), whatever transport carried it: a stdio adapter sends it with the
    stdio label, and it is as much an ordering boundary as a plain HTTP one. It is matched by route, not by method alone: many POST endpoints
    (graph/bfs, rag/query, the tool routes) only read, and treating them as writes would let mixed traffic write a summary per read."""
    return (fields.get("method"), fields.get("route")) in LEDGER_WRITE_ROUTES


def read_cap_per_minute() -> int:
    """GET reads of /api/jarvis/memory/* are witnessed one by one up to this many a minute (per process); the rest are rolled into one summary."""
    try:
        return max(1, int((os.environ.get("JARVIS_CALL_LOG_READ_CAP_PER_MIN") or "").strip() or 600))
    except ValueError:
        return 600


def reads_enabled() -> bool:
    """Whether memory GET reads are witnessed at all (JARVIS_CALL_LOG_READS, default on whenever the call log is on)."""
    return (os.environ.get("JARVIS_CALL_LOG_READS") or "1").strip().lower() not in ("0", "false", "no", "off")


# --------------------------------------------------------------------------------------------------------- helpers


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str | bytes) -> str:
    return hashlib.sha256(text.encode("utf-8") if isinstance(text, str) else text).hexdigest()


def args_hash(args: Any) -> str:
    """sha256 of the canonical JSON of the arguments (key order and whitespace do not matter)."""
    return sha256_hex(canonical(args))


def entry_hash_of(entry: dict[str, Any]) -> str:
    return sha256_hex(canonical({k: v for k, v in entry.items() if k != "entry_hash"}))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def clean_client(value: str | None, limit: int = 128) -> str | None:
    """Self-reported client strings are untrusted input: strip control characters, bound the length."""
    if not value:
        return None
    return _CTRL.sub("", str(value)).strip()[:limit] or None


def split_client(header: str | None) -> tuple[str | None, str | None]:
    """'name/version' (or just 'name') from X-Jarvis-MCP-Client or a User-Agent."""
    text = clean_client(header, 256)
    if not text:
        return None, None
    first = text.split()[0]
    name, _, version = first.partition("/")
    return clean_client(name), clean_client(version, 64)


# -------------------------------------------------------------------------------------------------------- the log


def _pin_by_handle() -> bool:
    """Linux keeps each snapshotted file open (an unlinked file stays readable); Windows cannot, so it copies the bytes instead."""
    return os.name != "nt"


class Snapshot:
    """The daily files of a log at one instant: (path, size, open handle). `close()` releases the handles."""

    def __init__(self) -> None:
        self.files: list[tuple[Path, int, Any]] = []

    def close(self) -> None:
        for _, _, fh in self.files:
            if fh is not None:  # (always set today; the guard keeps close() safe whatever a snapshot holds)
                with contextlib.suppress(OSError):
                    fh.close()
        self.files = []


class CallLog:
    def __init__(self, directory: Path, *, clock=_utc_now, file_prefix: str = "calls"):
        self.dir = Path(directory)
        self._prefix = file_prefix  # daily files are "<prefix>-YYYYMMDD.jsonl"; the default keeps the call log's names exactly as they were
        self._file_re = re.compile(rf"^{re.escape(file_prefix)}-(\d{{8}})\.jsonl$")
        self._clock = clock
        self._tlock = threading.RLock()
        self._outage: dict[str, Any] | None = None  # unlogged/refused calls kept in memory, so a gap can be recorded even if the DEGRADED file cannot be written
        self._suppressed: dict[str, Any] | None = None  # the open denied-flood window for this process
        self._denied_window: tuple[str, int] = ("", 0)
        self._read_suppressed: dict[tuple[str, str], dict[str, Any]] = {}  # open read-flood summaries, one per (tenant, transport)
        self._read_window: tuple[str, dict[str, int]] = ("", {})  # the minute, and how many reads each tenant has had in it

    # ----- paths
    @property
    def lock_path(self) -> Path:
        return self.dir / (".lock" if self._prefix == "calls" else f".{self._prefix}.lock")

    @property
    def degraded_path(self) -> Path:
        return self.dir / ("DEGRADED" if self._prefix == "calls" else f"DEGRADED-{self._prefix}")

    @property
    def anchor_path(self) -> Path:
        return self.dir / ("anchor.json" if self._prefix == "calls" else f"{self._prefix}-anchor.json")

    def _files(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        return sorted((p for p in self.dir.iterdir() if self._file_re.match(p.name)), key=lambda p: p.name)

    def _open_private(self, path: Path, flags: int) -> int:
        return os.open(path, flags, 0o600)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.dir, 0o700)
        with self._tlock:
            fd = self._open_private(self.lock_path, os.O_RDWR | os.O_CREAT)
            fh = os.fdopen(fd, "r+b")
            try:
                _lock(fh)
                yield
            finally:
                with contextlib.suppress(Exception):
                    _unlock(fh)
                fh.close()

    # ----- reading the head
    @staticmethod
    def _tail_lines(path: Path) -> list[str]:
        try:
            size = path.stat().st_size
        except OSError:
            return []
        if size == 0:
            return []
        with open(path, "rb") as fh:
            block = min(size, 65536)
            fh.seek(size - block)
            data = fh.read(block)
        return [ln.decode("utf-8", "replace") for ln in data.split(b"\n") if ln.strip()]

    @classmethod
    def _last_line(cls, path: Path) -> str | None:
        """The last line that parses as an entry (a torn final line from a crash mid-write is skipped, and verify reports it)."""
        lines = cls._tail_lines(path)
        for line in reversed(lines):
            try:
                json.loads(line)["entry_hash"]
                return line
            except (ValueError, KeyError, TypeError):
                continue
        if lines:
            raise CallLogError(f"no valid entry in the tail of {path.name}; run verify")
        return None

    def _head(self) -> dict[str, Any]:
        """The last entry's (seq, entry_hash); the anchor or genesis when there is none."""
        for path in reversed(self._files()):
            line = self._last_line(path)
            if line is None:
                continue
            entry = json.loads(line)
            return {"seq": int(entry["seq"]), "entry_hash": str(entry["entry_hash"]), "file": path.name, "ts": entry.get("ts")}
        try:
            anchor = json.loads(self.anchor_path.read_text(encoding="utf-8"))
            return {"seq": int(anchor["seq"]), "entry_hash": str(anchor["entry_hash"]), "file": None, "ts": anchor.get("deleted_at")}
        except (OSError, ValueError, KeyError):
            return {"seq": 0, "entry_hash": GENESIS, "file": None, "ts": None}

    def head(self) -> dict[str, Any]:
        with self._locked():
            return self._head()

    # ----- degraded latch and preflight
    def _file_state(self) -> dict[str, Any] | None:
        try:
            state = json.loads(self.degraded_path.read_text(encoding="utf-8"))
            return state if isinstance(state, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _combine(file_state: dict[str, Any] | None, delta: dict[str, Any] | None) -> dict[str, Any] | None:
        """The outage total: what is on disk PLUS what this process could not write there.  They are disjoint by construction (the memory part is
        only ever the increments that failed to persist), so they are added, never max'd."""
        parts = [p for p in (file_state, delta) if p]
        if not parts:
            return None
        return {
            "since": min(str(p.get("since")) for p in parts),
            "unlogged": sum(int(p.get("unlogged", 0)) for p in parts),
            "refused": sum(int(p.get("refused", 0)) for p in parts),
            "last_error": str(parts[-1].get("last_error", ""))[:300],
        }

    def degraded(self) -> dict[str, Any] | None:
        """The outage state: the DEGRADED file, plus this process's not-yet-persisted increments."""
        return self._combine(self._file_state(), self._outage)

    def _clear_degraded(self) -> None:
        with self._tlock:
            self._outage = None
            with contextlib.suppress(OSError):
                self.degraded_path.unlink()

    def _mark_degraded(self, error: str, *, unlogged: int = 1, refused: int = 0) -> None:
        """Record an outage.  ``unlogged``: calls that were served but could not be logged; ``refused``: writes refused because the log was unavailable.

        The DEGRADED file is the shared record (updated under the log's file lock, so workers add to one count instead of overwriting each other).
        ``self._outage`` is only the increments THIS process could not write there (the directory is what is unwritable); it is added to the file's
        count, and dropped as soon as it has been persisted, so no worker replays a stale copy after another has recorded the gap.  The whole
        read-modify-write runs under the in-process lock, so concurrent threads do not lose each other's counts.
        """
        with self._tlock:
            delta = dict(self._outage or {"since": _ts(self._clock()), "unlogged": 0, "refused": 0})
            delta["unlogged"] = int(delta.get("unlogged", 0)) + unlogged
            delta["refused"] = int(delta.get("refused", 0)) + refused
            delta["last_error"] = error[:300]
            try:
                with self._locked():
                    total = self._combine(self._file_state(), delta) or delta
                    tmp = self.degraded_path.with_suffix(".tmp")
                    fd = self._open_private(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(json.dumps(total))
                    os.replace(tmp, self.degraded_path)
                self._outage = None  # persisted: the file is the single record
            except (OSError, CallLogError):
                self._outage = delta  # could not be persisted: keep the increments so recovery can still record the gap
                total = self._combine(self._file_state(), delta) or delta
        _log.error("call log degraded: %s (served but unlogged: %s, writes refused: %s)", error, total["unlogged"], total["refused"])

    def preflight(self) -> None:
        """Raise CallLogError unless a write could be logged right now (used before running a ledger write)."""
        try:
            with self._locked():
                head = self._head()  # the log is readable and its tail parses
                if not os.access(self.dir, os.W_OK):
                    raise CallLogError("the call-log directory is not writable")
                today = self.dir / f"{self._prefix}-{self._clock():%Y%m%d}.jsonl"
                if today.exists() and not os.access(today, os.W_OK):
                    raise CallLogError("today's call-log file is not writable")
                if shutil.disk_usage(self.dir).free < MIN_FREE_BYTES:
                    raise CallLogError("the call-log volume is nearly full")
                state = self.degraded()
                if state:
                    self._append_locked(self._gap_entry(state), head)
                    self._clear_degraded()
        except CallLogError:
            raise
        except OSError as exc:
            raise CallLogError(f"call log unavailable: {exc}") from None

    # ----- appending
    def _gap_entry(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "transport": "http-api", "client_name": None, "client_version": None, "client_self_reported": True, "tenant": "operator",
            "tool": "(call-log)", "route": None, "method": None, "target": None,
            "args_sha256": args_hash({"since": state.get("since"), "unlogged": state.get("unlogged"), "refused": state.get("refused", 0)}),
            "outcome": "gap", "error_code": f"UNLOGGED_CALLS:{state.get('unlogged')};REFUSED_WRITES:{state.get('refused', 0)}", "status_code": None, "result_digest": None, "duration_ms": 0,
            "window_start": state.get("since"), "window_end": _ts(self._clock()), "count": state.get("unlogged"), "refused": state.get("refused", 0),
        }

    def _append_locked(self, fields: dict[str, Any], head: dict[str, Any]) -> dict[str, Any]:
        now = self._clock()
        entry = {"seq": head["seq"] + 1, "ts": _ts(now), "prev_hash": head["entry_hash"], **fields}
        entry["entry_hash"] = entry_hash_of(entry)
        self._rotate_and_retain(now)
        path = self.dir / f"{self._prefix}-{now:%Y%m%d}.jsonl"
        torn_prefix = b""
        with contextlib.suppress(OSError):
            if path.stat().st_size > 0:
                with open(path, "rb") as fh:
                    fh.seek(-1, os.SEEK_END)
                    torn_prefix = b"" if fh.read(1) == b"\n" else b"\n"  # a crash left a partial line: end it, verify reports it
        fd = self._open_private(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            os.write(fd, torn_prefix + (canonical(entry) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return entry

    def _rotate_and_retain(self, now: datetime) -> None:
        """Delete whole files older than the retention window; the last deleted entry's hash is kept as the chain's anchor."""
        cutoff = (now - timedelta(days=retain_days())).strftime("%Y%m%d")
        old = [p for p in self._files() if self._file_re.match(p.name).group(1) < cutoff]  # type: ignore[union-attr]
        files = self._files()
        if not old or len(old) >= len(files):
            return
        last_line = None
        for p in reversed(old):
            last_line = self._last_line(p)
            if last_line:
                break
        if last_line:
            last = json.loads(last_line)
            tmp = self.anchor_path.with_suffix(".tmp")
            fd = self._open_private(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"seq": last["seq"], "entry_hash": last["entry_hash"], "file": old[-1].name, "deleted_at": _ts(now)}))
            os.replace(tmp, self.anchor_path)
        for p in old:
            p.unlink()

    def append(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Append one entry (raises CallLogError on failure).  Denied floods are capped and rolled into 'denied_suppressed'."""
        try:
            with self._locked():
                head = self._head()
                state = self.degraded()
                if state:  # recovered: say how many calls were not logged before anything else
                    gap = self._append_locked(self._gap_entry(state), head)
                    head = {"seq": gap["seq"], "entry_hash": gap["entry_hash"]}
                    self._clear_degraded()
                if fields.get("outcome") == "denied":
                    window = self._clock().strftime("%Y-%m-%dT%H:%M")
                    if self._denied_window[0] != window:
                        head = self._flush_suppressed_locked(head)
                        self._denied_window = (window, 0)
                    count = self._denied_window[1] + 1
                    self._denied_window = (window, count)
                    if count > denied_cap_per_minute():
                        s = self._suppressed or {"window_start": _ts(self._clock()), "count": 0}
                        s["count"] += 1
                        s["window_end"] = _ts(self._clock())
                        self._suppressed = s
                        return {"suppressed": True}
                else:
                    head = self._flush_suppressed_locked(head)
                    if fields.get("method") == "GET" and fields.get("transport") != "http-tool" and fields.get("outcome") == "ok":
                        # a successful memory read: capped, never silently dropped. A failed read (404, 500) is evidence of something wrong and is never summarised.
                        window = self._clock().strftime("%Y-%m-%dT%H:%M")
                        if self._read_window[0] != window:
                            head = self._flush_reads_locked(head)
                            self._read_window = (window, {})
                        tenant = str(fields.get("tenant") or "operator")
                        counts = self._read_window[1]  # the cap is per tenant, so a busy tenant cannot use up the others' reads
                        counts[tenant] = counts.get(tenant, 0) + 1
                        if counts[tenant] > read_cap_per_minute():
                            key = (tenant, str(fields.get("transport") or "http-api"))  # and a summary never mixes tenants or transports
                            s = self._read_suppressed.get(key) or {"window_start": _ts(self._clock()), "count": 0}
                            s["count"] += 1
                            s["window_end"] = _ts(self._clock())
                            self._read_suppressed[key] = s
                            return {"suppressed": True}
                    elif fields.get("outcome") == "ok" and (fields.get("tool") in WRITE_TOOLS or _is_rest_write(fields)):
                        # a write is an ordering boundary: it closes the open read summary first, so the log shows how many reads came before it.
                        # Other calls (a read-only tool, say) do not: alternating them with reads would otherwise write one summary per read.
                        head = self._flush_reads_locked(head)
                return self._append_locked(fields, head)
        except CallLogError:
            raise
        except OSError as exc:
            raise CallLogError(f"call log write failed: {exc}") from None

    def _flush_suppressed_locked(self, head: dict[str, Any]) -> dict[str, Any]:
        s = self._suppressed
        if not s:
            return head
        fields = {
            "transport": "http-api", "client_name": None, "client_version": None, "client_self_reported": True, "tenant": "operator",
            "tool": "(denied calls)", "route": None, "method": None, "target": None, "args_sha256": args_hash({"suppressed": s["count"]}),
            "outcome": "denied_suppressed", "error_code": f"SUPPRESSED:{s['count']}", "status_code": None, "result_digest": None, "duration_ms": 0,
            "window_start": s["window_start"], "window_end": s["window_end"], "count": s["count"],
        }
        entry = self._append_locked(fields, head)
        self._suppressed = None  # only once the summary is durably in the log: a failed append keeps the count for the next flush
        return {"seq": entry["seq"], "entry_hash": entry["entry_hash"]}

    def flush_for_shutdown(self) -> None:
        """Last chance to write the open summaries. If that fails, the process is about to exit with their counts only in memory, so the
        counts are recorded as unlogged calls in the DEGRADED state; the next process then writes them into a gap entry."""
        self.flush()
        pending = (self._suppressed or {}).get("count", 0) + sum(s["count"] for s in self._read_suppressed.values())
        if pending:
            self._mark_degraded("shutdown: the open denied/read summaries could not be written", unlogged=pending)
            if self._outage:  # the DEGRADED file could not be written either (the directory or volume itself is failing): nothing on that volume can hold the count
                _log.error("call log shutdown: %s calls (denied/read summaries) could not be recorded anywhere on the log volume and are lost; this line is their only trace", pending)
            self._suppressed, self._read_suppressed = None, {}

    def _flush_reads_locked(self, head: dict[str, Any]) -> dict[str, Any]:
        for key in sorted(self._read_suppressed):
            s = self._read_suppressed[key]
            tenant, transport = key
            fields = {
                "transport": transport, "client_name": None, "client_version": None, "client_self_reported": True, "tenant": tenant,
                "tool": "(read calls)", "route": None, "method": "GET", "target": None, "args_sha256": args_hash({"suppressed": s["count"]}),
                "outcome": "read_suppressed", "error_code": f"SUPPRESSED:{s['count']}", "status_code": None, "result_digest": None, "duration_ms": 0,
                "window_start": s["window_start"], "window_end": s["window_end"], "count": s["count"],
            }
            entry = self._append_locked(fields, head)
            del self._read_suppressed[key]  # only once the summary is durably in the log (a failed append keeps the count)
            head = {"seq": entry["seq"], "entry_hash": entry["entry_hash"]}
        return head

    def flush(self) -> None:
        """Write any open 'denied_suppressed' / 'read_suppressed' summary now (the read endpoints call this so readers see it)."""
        try:
            with self._locked():
                self._flush_reads_locked(self._flush_suppressed_locked(self._head()))
        except (CallLogError, OSError) as exc:
            if self._suppressed or self._read_suppressed:
                # the counts are kept for the next flush, but the log is in trouble now: say so in verify/query (no unlogged calls are added; they are still pending)
                self._mark_degraded(f"could not write the open summaries: {exc}", unlogged=0)

    def record(self, fields: dict[str, Any]) -> dict[str, Any] | None:
        """Append, applying the failure policy: never raises.  Returns the entry, or None if it could not be written."""
        try:
            return self.append(fields)
        except CallLogError as exc:
            self._mark_degraded(str(exc), unlogged=1)
            return None

    # ----- reading
    def _snapshot(self) -> "Snapshot":
        """Each daily file, held open, and its size right now. Call it under the lock: the files only ever grow by whole lines, so the first `size` bytes of
        each are exactly the log as of this moment, and can be read later without the lock. The open handles matter: if a later append rotates the log and
        retention unlinks an older file, the handle still reads it, so the page is never missing a file the snapshot advertised. Close it when done."""
        snap = Snapshot()
        for path in self._files():
            try:
                fh = open(path, "rb")
                if not _pin_by_handle():
                    # Windows cannot unlink a file that is open (no delete-sharing for ordinary handles), so pinning would make retention fail inside an
                    # append. Copy the bytes while the lock is held instead: nothing is left open, nothing can be deleted from under the page, and no later
                    # reopen can fail. (The cost, reading under the lock, is the old behaviour; the servers run on Linux.)
                    with fh:
                        data = fh.read()
                    fh = io.BytesIO(data)
                snap.files.append((path, len(fh.getbuffer()) if isinstance(fh, io.BytesIO) else os.fstat(fh.fileno()).st_size, fh))
            except FileNotFoundError:
                continue  # retention removed it between listing and opening: it is simply not part of the log any more
            except OSError as exc:
                # anything else (descriptor limit, permissions) must NOT look like a missing file: the page would silently lack this file and every later one
                snap.close()
                raise CallLogError(f"could not snapshot the call log ({exc.__class__.__name__}: {exc})") from None
        return snap

    def entries_desc(self, snapshot: "Snapshot | None" = None) -> Iterator[dict[str, Any]]:
        """Entries newest first. With a snapshot, only the bytes that existed when it was taken (so a concurrent append is not seen)."""
        if snapshot is None:
            sources: list[tuple[Path, int | None, Any]] = [(p, None, None) for p in reversed(self._files())]
        else:
            sources = [(p, size, fh) for p, size, fh in reversed(snapshot.files)]
        for path, size, fh in sources:
            try:
                if fh is not None:
                    fh.seek(0)
                    lines = fh.read(size).decode("utf-8", "replace").splitlines()
                else:
                    lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                if snapshot is not None:
                    raise CallLogError(f"a file of the snapshot could not be read ({exc.__class__.__name__}); retry") from None
                continue
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue

    def query(self, *, tool: str | None = None, client: str | None = None, since: str | None = None, limit: int = 50, before_seq: int | None = None) -> dict[str, Any]:
        self.flush()
        return self._query_unlocked(tool=tool, client=client, since=since, limit=limit, before_seq=before_seq)

    def query_with_head(self, *, tool: str | None = None, client: str | None = None, since: str | None = None, limit: int = 50, before_seq: int | None = None) -> dict[str, Any]:
        """The page and the log's head from ONE snapshot: the head and the size of every file are taken together under the lock, and the page is read
        from exactly those bytes after the lock is released. So no append can fall between them (for an unfiltered request the head is always the newest
        entry of the first page, or the page is empty and the head is the genesis), and a large log never holds the writers' lock while it is parsed."""
        with self._locked():
            with contextlib.suppress(CallLogError, OSError):  # open denied/read summaries are written first, like flush(); a failure here changes nothing
                self._flush_reads_locked(self._flush_suppressed_locked(self._head()))
            head = self._head()                                 # read after the flush, in the same hold of the lock as the file sizes
            snapshot = self._snapshot()
        try:
            page = self._query_unlocked(tool=tool, client=client, since=since, limit=limit, before_seq=before_seq, snapshot=snapshot)
        finally:
            snapshot.close()
        return {**page, "head": head}

    def _query_unlocked(self, *, tool: str | None, client: str | None, since: str | None, limit: int, before_seq: int | None,
                        snapshot: "Snapshot | None" = None) -> dict[str, Any]:
        out: list[dict[str, Any]] = []
        more = False
        for e in (self.entries_desc() if snapshot is None else self.entries_desc(snapshot)):
            if before_seq is not None and e.get("seq", 0) >= before_seq:
                continue
            if since and str(e.get("ts", "")) < since:
                break  # entries are in ts order, newest first: everything further is older
            if tool and e.get("tool") != tool:
                continue
            if client and client.lower() not in str(e.get("client_name") or "").lower():
                continue
            if len(out) >= limit:
                more = True
                break
            out.append(e)
        return {"entries": out, "next_cursor": str(out[-1]["seq"]) if (more and out) else None, "degraded": self.degraded()}

    def verify(self) -> dict[str, Any]:
        """Walk the chain across files: seq contiguous, prev_hash links, entry_hash recomputes.  Never raises."""
        self.flush()
        problems: list[dict[str, Any]] = []
        total = 0
        prev_hash, prev_seq = GENESIS, 0
        anchor = None
        try:
            anchor = json.loads(self.anchor_path.read_text(encoding="utf-8"))
            prev_hash, prev_seq = str(anchor["entry_hash"]), int(anchor["seq"])
        except (OSError, ValueError, KeyError):
            anchor = None
        files = self._files()
        for path in files:
            try:
                raw = path.read_bytes()
            except OSError as exc:
                problems.append({"file": path.name, "line": None, "problem": f"unreadable: {exc}"})
                continue
            text = raw.decode("utf-8", "replace")
            lines = text.split("\n")
            torn = bool(raw) and not raw.endswith(b"\n")
            for n, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                is_last_torn = torn and n == len(lines)
                try:
                    e = json.loads(line)
                    seq, entry_hash, prev = int(e["seq"]), str(e["entry_hash"]), str(e["prev_hash"])
                except (ValueError, KeyError, TypeError):
                    problems.append({"file": path.name, "line": n, "problem": "torn final line (a crash mid-write)" if is_last_torn else "not a valid entry"})
                    continue
                if entry_hash_of(e) != entry_hash:
                    problems.append({"file": path.name, "line": n, "problem": f"entry {seq}: entry_hash does not match the content (edited)"})
                if seq != prev_seq + 1:
                    problems.append({"file": path.name, "line": n, "problem": f"entry {seq}: expected seq {prev_seq + 1} (a line was deleted, inserted or reordered)"})
                if prev != prev_hash:
                    problems.append({"file": path.name, "line": n, "problem": f"entry {seq}: prev_hash does not link to the previous entry (deleted, inserted or reordered)"})
                prev_hash, prev_seq = entry_hash, seq
                total += 1
        gaps = [e for e in self.entries_desc() if e.get("outcome") == "gap"]
        return {
            "ok": not problems, "entries": total, "files": [p.name for p in files], "problems": problems,
            "head": {"seq": prev_seq, "entry_hash": prev_hash}, "anchor": anchor, "degraded": self.degraded(),
            "gaps_recorded": len(gaps),
        }


# --------------------------------------------------------------------------------------------- process-wide access

_logs: dict[str, CallLog] = {}
_logs_lock = threading.Lock()


def get_call_log() -> CallLog:
    key = str(log_dir().resolve())
    with _logs_lock:
        if key not in _logs:
            _logs[key] = CallLog(Path(key))
        return _logs[key]


def reset_for_tests() -> None:
    with _logs_lock:
        _logs.clear()


# ----------------------------------------------------------------------------------------------------- entry building


def make_fields(*, transport: str, tool: str, args: Any, outcome: str, started: float, client_header: str | None = None,
                client_name: str | None = None, client_version: str | None = None, tenant: str | None = None,
                error_code: str | None = None, status_code: int | None = None, result_digest: str | None = None,
                route: str | None = None, method: str | None = None, target: str | None = None) -> dict[str, Any]:
    if client_header and not client_name:
        client_name, client_version = split_client(client_header)
    return {
        "transport": transport, "client_name": clean_client(client_name), "client_version": clean_client(client_version, 64),
        "client_self_reported": True, "tenant": tenant or "operator", "tool": tool, "route": route, "method": method, "target": target,
        "args_sha256": args_hash(args), "outcome": outcome, "error_code": error_code, "status_code": status_code,
        "result_digest": result_digest if isinstance(result_digest, str) and re.fullmatch(r"[0-9a-f]{64}", result_digest) else None,
        "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
    }
