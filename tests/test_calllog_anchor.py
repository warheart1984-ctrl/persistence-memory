"""Call-log head anchors (issue #74): the call log's head is kept outside its directory, in every backup set's anchors.

The call log is a hash chain, but anyone who can write its directory can cut trailing lines off, or rebuild the whole chain, and it
still verifies.  These tests prove the anchor catches both, using the real script (deploy/mint/bin/calllog_anchor.py) and the real
bash gate (anchors.sh) on a real log directory and a real data.tar."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import call_log

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "deploy" / "mint" / "bin"
SCRIPT = BIN / "calllog_anchor.py"
ANCHORS = BIN / "anchors.sh"
Z = "0" * 64


def fields(i: int = 0):
    return call_log.make_fields(transport="http-tool", tool="emr_latest", args={"i": i}, outcome="ok", started=time.perf_counter())


def make_log(directory: Path, n: int, *, start: int = 0, day: int = 11) -> call_log.CallLog:
    log = call_log.CallLog(directory, clock=lambda: datetime(2026, 10, day, 12, tzinfo=timezone.utc))
    for i in range(start, start + n):
        log.append(fields(i))
    return log


def run(*args: object) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True, timeout=60)


def head_line(directory: Path) -> str:
    r = run("head", directory)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def anchors_file(tmp_path: Path, name: str, line: str | None, extra: tuple[str, ...] = ()) -> Path:
    p = tmp_path / name
    p.write_text("\n".join([*extra, *([line] if line else [])]) + "\n")
    return p


def rebuild(directory: Path, n: int, *, salt: str) -> None:
    """What an attacker with write access does: write a different history with a perfectly valid chain."""
    for f in directory.glob("calls-*.jsonl"):
        f.unlink()
    make_log(directory, n, start=1000 + len(salt))


# ----------------------------------------------------------------------------------------------- the head line

def test_head_prints_the_seq_and_hash_the_log_itself_reports(tmp_path):
    log = make_log(tmp_path / "log", 5)
    head = log.verify()["head"]
    assert head_line(tmp_path / "log") == f"calllog|{head['seq']}|{head['entry_hash']}" and head["seq"] == 5


def test_no_log_means_no_line_and_no_error(tmp_path):
    r = run("head", tmp_path / "nothing-here")
    assert r.returncode == 0 and r.stdout == ""
    (tmp_path / "empty").mkdir()
    assert run("head", tmp_path / "empty").stdout == ""


def test_reading_the_head_never_writes_even_when_the_log_is_degraded(tmp_path):
    """CallLog.verify() flushes (it can append a gap entry), which would move the head; the anchor reader must not."""
    d = tmp_path / "log"
    make_log(d, 3)
    (d / "DEGRADED").write_text(json.dumps({"since": "2026-10-11T12:00:00Z", "reason": "disk full", "unlogged": 2}))
    before = {p.name: p.read_bytes() for p in d.iterdir()}
    line = head_line(d)
    assert line.startswith("calllog|3|")
    assert {p.name: p.read_bytes() for p in d.iterdir()} == before


def test_a_broken_chain_still_names_its_head_but_exits_nonzero(tmp_path):
    d = tmp_path / "log"
    make_log(d, 4)
    f = next(d.glob("calls-*.jsonl"))
    lines = f.read_text().splitlines()
    doctored = json.loads(lines[1])
    doctored["tool"] = "something_else"
    lines[1] = json.dumps(doctored, sort_keys=True, separators=(",", ":"))
    f.write_text("\n".join(lines) + "\n")
    r = run("head", d)
    assert r.returncode == 3 and r.stdout.startswith("calllog|4|") and "edited" in r.stderr


# ------------------------------------------------------------------------------------------- successor checks

def test_a_log_that_grew_still_contains_the_anchored_head(tmp_path):
    d = tmp_path / "log"
    log = make_log(d, 4)
    anchored = anchors_file(tmp_path, "set1.anchors", head_line(d))
    for i in range(10, 13):
        log.append(fields(i))
    assert run("check", d, anchored).returncode == 0


def test_a_log_with_its_tail_cut_off_is_caught(tmp_path):
    d = tmp_path / "log"
    make_log(d, 6)
    anchored = anchors_file(tmp_path, "set1.anchors", head_line(d))
    f = next(d.glob("calls-*.jsonl"))
    f.write_text("\n".join(f.read_text().splitlines()[:4]) + "\n")        # the chain still verifies; only the anchor can tell
    assert run("head", d).returncode == 0
    r = run("check", d, anchored)
    assert r.returncode == 1 and "beyond this log's head" in r.stderr


def test_a_log_rebuilt_with_a_valid_chain_is_caught(tmp_path):
    d = tmp_path / "log"
    make_log(d, 6)
    anchored = anchors_file(tmp_path, "set1.anchors", head_line(d))
    rebuild(d, 8, salt="x")                                              # longer than before, valid chain, different history
    assert run("head", d).returncode == 0
    r = run("check", d, anchored)
    assert r.returncode == 1 and "rebuilt or edited" in r.stderr


def test_a_line_removed_from_the_middle_is_caught(tmp_path):
    d = tmp_path / "log"
    make_log(d, 6)
    anchored = anchors_file(tmp_path, "set1.anchors", head_line(d))
    f = next(d.glob("calls-*.jsonl"))
    lines = f.read_text().splitlines()
    del lines[2]
    f.write_text("\n".join(lines) + "\n")
    assert run("check", d, anchored).returncode != 0


def test_every_earlier_set_is_checked_not_only_the_newest(tmp_path):
    d = tmp_path / "log"
    log = make_log(d, 3)
    a1 = anchors_file(tmp_path, "a1.anchors", head_line(d))
    for i in range(10, 12):
        log.append(fields(i))
    a2 = anchors_file(tmp_path, "a2.anchors", head_line(d))
    assert run("check", d, a1, a2).returncode == 0
    f = next(d.glob("calls-*.jsonl"))
    lines = f.read_text().splitlines()
    doctored = json.loads(lines[1])                     # edit an entry between the two anchored heads, re-chain everything after it
    doctored["tool"] = "forged"
    prev = None
    out = []
    for i, line in enumerate(lines):
        e = json.loads(line) if i != 1 else doctored
        if prev is not None:
            e["prev_hash"] = prev
        e["entry_hash"] = call_log.entry_hash_of(e)
        prev = e["entry_hash"]
        out.append(json.dumps(e, sort_keys=True, separators=(",", ":")))
    f.write_text("\n".join(out) + "\n")
    assert run("head", d).returncode == 0              # a perfectly valid chain...
    r = run("check", d, a1, a2)
    assert r.returncode == 1 and "a1.anchors" in r.stderr and "a2.anchors" in r.stderr     # both anchored heads no longer match


def set_named(tmp_path: Path, days_ago: float, line: str) -> Path:
    """An anchors file named like a real backup set, taken `days_ago` days ago."""
    taken = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return anchors_file(tmp_path, f"jarvis-{taken.strftime('%Y%m%dT%H%M%SZ')}.anchors", line)


def retained_log(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    monkeypatch.setenv("JARVIS_CALL_LOG_RETAIN_DAYS", "2")
    d = tmp_path / "log"
    day = [1]
    log = call_log.CallLog(d, clock=lambda: datetime(2026, 10, day[0], 12, tzinfo=timezone.utc))
    log.append(fields(0))
    first = head_line(d)                                              # head at seq 1 on day 1
    for day[0] in range(2, 8):
        log.append(fields(day[0]))
    assert len(list(d.glob("calls-*.jsonl"))) <= 3 and (d / "anchor.json").exists()
    return d, first


def test_an_anchor_older_than_the_retention_window_is_not_a_false_alarm(tmp_path, monkeypatch):
    d, first = retained_log(tmp_path, monkeypatch)
    old = set_named(tmp_path, 30, first)                              # taken long before the (2 day) window: its entry was pruned on purpose
    recent = set_named(tmp_path, 0.01, head_line(d))                  # and a recent one that really is in the log
    assert run("head", d).returncode == 0 and run("check", d, old, recent).returncode == 0


def test_a_recent_anchor_is_never_excused_by_the_retention_anchor(tmp_path, monkeypatch):
    """anchor.json is in the directory an attacker can write: a forged high one must not waive genuine external anchors."""
    d = tmp_path / "log"
    make_log(d, 6)
    genuine = set_named(tmp_path, 0.01, head_line(d))
    rebuild(d, 9, salt="zz")                                            # a different history with a valid chain...
    first = next(iter(sorted(d.glob("calls-*.jsonl"))))
    entries = [json.loads(x) for x in first.read_text().splitlines()]
    # ...started after an invented high retention anchor, so every genuine anchor has seq <= anchor.seq
    first.write_text("")
    (d / "anchor.json").write_text(json.dumps({"seq": 500, "entry_hash": "f" * 64}))
    prev = "f" * 64
    out = []
    for i, e in enumerate(entries):
        e = {**e, "seq": 501 + i, "prev_hash": prev}
        e["entry_hash"] = call_log.entry_hash_of(e)
        prev = e["entry_hash"]
        out.append(json.dumps(e, sort_keys=True, separators=(",", ":")))
    first.write_text("\n".join(out) + "\n")
    assert run("head", d).returncode == 0                               # the forged log verifies on its own
    r = run("check", d, genuine)
    assert r.returncode == 1 and "missing" in r.stderr


def test_old_anchors_are_not_excused_when_no_recent_anchor_could_be_compared(tmp_path, monkeypatch):
    """Backups interrupted for longer than the window: every external anchor is old, so a forged anchor.json must not excuse them all."""
    d = tmp_path / "log"
    make_log(d, 6)
    only_old = set_named(tmp_path, 200, head_line(d))
    rebuild(d, 9, salt="qq")
    first = next(iter(sorted(d.glob("calls-*.jsonl"))))
    entries = [json.loads(x) for x in first.read_text().splitlines()]
    (d / "anchor.json").write_text(json.dumps({"seq": 500, "entry_hash": "f" * 64}))
    prev, out = "f" * 64, []
    for i, e in enumerate(entries):
        e = {**e, "seq": 501 + i, "prev_hash": prev}
        e["entry_hash"] = call_log.entry_hash_of(e)
        prev = e["entry_hash"]
        out.append(json.dumps(e, sort_keys=True, separators=(",", ":")))
    first.write_text("\n".join(out) + "\n")
    r = run("check", d, only_old)
    assert r.returncode == 1 and "cannot be excused" in r.stderr


def test_old_anchors_are_excused_alongside_one_that_matched(tmp_path, monkeypatch):
    d, first = retained_log(tmp_path, monkeypatch)
    old = set_named(tmp_path, 30, first)
    recent = set_named(tmp_path, 0.01, head_line(d))
    assert run("check", d, old, recent).returncode == 0


def test_a_forged_retention_anchor_does_not_excuse_old_anchors_with_the_wrong_hash(tmp_path, monkeypatch):
    d, first = retained_log(tmp_path, monkeypatch)
    seq = json.loads((d / "anchor.json").read_text())["seq"]
    wrong = set_named(tmp_path, 30, f"calllog|{seq}|{'e' * 64}")       # old enough to be excused, but names the retention seq with another hash
    r = run("check", d, wrong)
    assert r.returncode == 1 and "retention anchor" in r.stderr


def test_a_set_with_no_call_log_line_is_not_checked(tmp_path):
    d = tmp_path / "log"
    make_log(d, 2)
    assert run("check", d, anchors_file(tmp_path, "pre.anchors", None, (f"counter|t|5", f"head|t|mem-a|5|{'a' * 64}|f"))).returncode == 0


# --------------------------------------------------------------------------- the bash gate on a real data.tar

def make_tar(log_dir: Path | None, tar_path: Path) -> Path:
    with tarfile.open(tar_path, "w") as tar:
        tar.add(tar_path.parent, arcname=".", recursive=False)
        if log_dir is not None:
            tar.add(log_dir, arcname="./call-log")
    return tar_path


def gate(tar: Path, set_anchors: Path, *earlier: Path) -> subprocess.CompletedProcess:
    args = " ".join(f'"{p}"' for p in earlier)
    return subprocess.run(["bash", "-c", f'set -u; source "{ANCHORS}"; anchors_calllog_gate "{tar}" "{set_anchors}" {args}'],
                          capture_output=True, text=True, timeout=60)


def test_the_gate_passes_for_a_set_whose_archive_holds_its_anchor_and_every_earlier_one(tmp_path):
    d = tmp_path / "log"
    log = make_log(d, 3)
    earlier = anchors_file(tmp_path, "e.anchors", head_line(d))
    log.append(fields(9))
    mine = anchors_file(tmp_path, "m.anchors", head_line(d))
    assert gate(make_tar(d, tmp_path / "m.tar"), mine, earlier).returncode == 0


def test_the_gate_catches_a_rebuilt_log_in_the_archive_against_an_earlier_set(tmp_path):
    d = tmp_path / "log"
    make_log(d, 3)
    earlier = anchors_file(tmp_path, "e.anchors", head_line(d))
    rebuild(d, 5, salt="yy")
    mine = anchors_file(tmp_path, "m.anchors", head_line(d))                 # the attacker's set anchors its own, consistent log...
    r = gate(make_tar(d, tmp_path / "m.tar"), mine, earlier)                 # ...but the earlier set (and the offsite copy) disagree
    assert r.returncode == 1 and "e.anchors" in r.stderr


def test_the_gate_catches_an_archive_that_lost_its_call_log(tmp_path):
    d = tmp_path / "log"
    make_log(d, 3)
    mine = anchors_file(tmp_path, "m.anchors", head_line(d))
    r = gate(make_tar(None, tmp_path / "m.tar"), mine)
    assert r.returncode == 1 and "no call log" in r.stdout


def test_the_gate_passes_when_nothing_was_ever_anchored(tmp_path):
    assert gate(make_tar(None, tmp_path / "m.tar"), anchors_file(tmp_path, "m.anchors", None, ("counter|t|1",))).returncode == 0


# ------------------------------------------------------------- the database anchors are untouched by the extra line

def bash(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f'set -u; source "{ANCHORS}"; {script}'], capture_output=True, text=True, timeout=60)


def test_the_call_log_line_is_not_part_of_the_database_comparison(tmp_path):
    p = anchors_file(tmp_path, "a", f"calllog|7|{'c' * 64}", ("counter|t|5", f"head|t|mem-a|5|{'a' * 64}|f"))
    r = bash(f'anchors_db_part "{p}"')
    assert r.stdout.splitlines() == ["counter|t|5", f"head|t|mem-a|5|{'a' * 64}|f"]


def test_anchors_check_ignores_the_call_log_line(tmp_path):
    base = ("counter|t|5", f"head|t|mem-a|5|{'a' * 64}|f")
    prev = anchors_file(tmp_path, "p", f"calllog|7|{'c' * 64}", base)
    new = anchors_file(tmp_path, "n", f"calllog|9|{'d' * 64}", base)
    assert bash(f'anchors_check "{prev}" "{new}"').returncode == 0
    assert bash(f'anchors_check "{prev}" "{anchors_file(tmp_path, "z", None, base)}"').returncode == 0


def test_earlier_sets_are_the_ones_before_this_one_oldest_first(tmp_path):
    for ts in ("20261011T010000Z", "20261011T020000Z", "20261011T030000Z"):
        (tmp_path / f"jarvis-{ts}.anchors").write_text("")
    r = bash(f'anchors_earlier_sets "{tmp_path}" jarvis-20261011T030000Z')
    assert [Path(x).name for x in r.stdout.split()] == ["jarvis-20261011T010000Z.anchors", "jarvis-20261011T020000Z.anchors"]


def test_the_shell_scripts_still_parse():
    for name in ("anchors.sh", "backup.sh", "restore.sh", "drill.sh"):
        assert subprocess.run(["bash", "-n", str(BIN / name)], capture_output=True).returncode == 0, name


def test_the_script_has_no_side_effects_on_import_path_or_environment():
    assert os.access(SCRIPT, os.R_OK) and run().returncode == 2


# ------------------------------------------------------- the real step 6a of backup.sh, run with stubbed log/notify

def run_backup_step(tmp_path: Path, tar: Path, prev: Path | None) -> tuple[subprocess.CompletedProcess, Path]:
    text = (BIN / "backup.sh").read_text()
    block = text[text.index("# 6a. the call log's head"):text.index("# 6b. custody scan")]
    tmp = tmp_path / "tmp"
    tmp.mkdir(exist_ok=True)
    (tmp / "set.anchors").write_text("counter|t|5\n" + f"head|t|mem-a|5|{'a' * 64}|f\n")
    (tmp / "set.data.tar").write_bytes(tar.read_bytes())
    script = f'''set -Eeuo pipefail
source "{ANCHORS}"
tmp="{tmp}"; base=set; prev_anchors="{prev if prev else ""}"
log() {{ echo "LOG $*"; }}
notify() {{ echo "NOTIFY $*"; }}
{block}
echo DONE'''
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60), tmp / "set.anchors"


def test_backup_adds_the_head_line_and_keeps_the_anchors_sorted(tmp_path):
    d = tmp_path / "log"
    make_log(d, 4)
    r, anchors = run_backup_step(tmp_path, make_tar(d, tmp_path / "t.tar"), None)
    assert r.returncode == 0 and "DONE" in r.stdout and "NOTIFY" not in r.stdout, r.stdout + r.stderr
    lines = anchors.read_text().splitlines()
    assert lines == sorted(lines) and any(x.startswith("calllog|4|") for x in lines) and "counter|t|5" in lines


def test_backup_raises_the_alarm_but_still_publishes_when_the_log_was_cut_short(tmp_path):
    d = tmp_path / "log"
    make_log(d, 6)
    prev = anchors_file(tmp_path, "prev.anchors", head_line(d), ("counter|t|5",))
    f = next(d.glob("calls-*.jsonl"))
    f.write_text("\n".join(f.read_text().splitlines()[:3]) + "\n")
    r, anchors = run_backup_step(tmp_path, make_tar(d, tmp_path / "t.tar"), prev)
    assert r.returncode == 0 and "DONE" in r.stdout                      # not a die(): the ledger backup still goes out
    assert "NOTIFY Jarvis ledger: CALL LOG DOES NOT MATCH ITS ANCHOR" in r.stdout and "beyond this log's head" in r.stdout
    assert any(x.startswith("calllog|3|") for x in anchors.read_text().splitlines())


def test_backup_with_no_call_log_in_the_volume_adds_nothing(tmp_path):
    r, anchors = run_backup_step(tmp_path, make_tar(None, tmp_path / "t.tar"), None)
    assert r.returncode == 0 and "calllog" not in anchors.read_text() and "NOTIFY" not in r.stdout


def test_backup_publishes_a_clean_set_after_a_clean_set(tmp_path):
    d = tmp_path / "log"
    log = make_log(d, 3)
    prev = anchors_file(tmp_path, "prev.anchors", head_line(d))
    for i in range(20, 24):
        log.append(fields(i))
    r, anchors = run_backup_step(tmp_path, make_tar(d, tmp_path / "t.tar"), prev)
    assert "NOTIFY" not in r.stdout and "call log head anchored: calllog|7|" in r.stdout and "calllog|7|" in anchors.read_text()


def test_backup_raises_the_alarm_when_the_directory_vanished_after_an_earlier_anchor(tmp_path):
    d = tmp_path / "log"
    make_log(d, 3)
    prev = anchors_file(tmp_path, "prev.anchors", head_line(d))
    r, anchors = run_backup_step(tmp_path, make_tar(None, tmp_path / "t.tar"), prev)
    assert r.returncode == 0 and "DONE" in r.stdout and "NOTIFY Jarvis ledger: CALL LOG DOES NOT MATCH ITS ANCHOR" in r.stdout
    assert "calllog" not in anchors.read_text()
