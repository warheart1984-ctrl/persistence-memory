"""deploy/mint/rehearse/scenario.sh kills and removes the ledger's REAL container names and deletes the user's jarvis-*.timer units.  It must
refuse to start anywhere but a blank, dedicated Docker engine, and run no docker command before it has decided.  Everything here runs the
real script with FAKE `docker` and `ss` commands that only record what they were asked: nothing real is ever touched."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

SCENARIO = Path(__file__).resolve().parents[1] / "deploy" / "mint" / "rehearse" / "scenario.sh"
DESTRUCTIVE = re.compile(r"\b(kill|rm|rmi|stop|down|volume rm|network rm|system prune|run|compose)\b")


@pytest.fixture
def box(tmp_path):
    """A fake machine: a HOME, a fake docker engine (state in files), a fake ss, and a log of every command."""
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    for name in ("containers", "volumes", "networks", "images", "ports"):
        (state / name).write_text("")
    (state / "engine_id").write_text("ENGINE-A\n")
    log = tmp_path / "calls.log"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(f"""#!/usr/bin/env bash
echo "docker $*" >> {log}
[ -f {state}/fail ] && [ "$(cat {state}/fail)" = "$1 $2" ] && exit 1      # a probe that fails while the engine itself answers
case "$1 $2" in
  "info --format") cat {state}/engine_id ;;
  "ps -a") cat {state}/containers ;;
  "volume ls") cat {state}/volumes ;;
  "network ls") cat {state}/networks ;;
  "image ls") cat {state}/images ;;
  *) ;;
esac
exit 0
""")
    ss = bindir / "ss"
    ss.write_text(f"""#!/usr/bin/env bash
echo "ss $*" >> {log}
[ -f {state}/fail ] && [ "$(cat {state}/fail)" = "ss" ] && exit 1
echo "State Recv-Q Send-Q Local Address:Port Peer Address:Port"
while read -r port; do [ -n "$port" ] && echo "LISTEN 0 4096 127.0.0.1:$port 0.0.0.0:*"; done < {state}/ports
exit 0
""")
    for f in (docker, ss):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return type("Box", (), {"home": home, "state": state, "log": log, "bindir": bindir, "tmp": tmp_path})


def run(box, phase, *, guard_only=True, extra_env=None):
    env = dict(os.environ, HOME=str(box.home), PATH=f"{box.bindir}:{os.environ['PATH']}", REHEARSAL_GUARD_ONLY="1" if guard_only else "0",
               SRC_FROM=str(SCENARIO.parents[3]))
    env.update(extra_env or {})
    return subprocess.run(["bash", str(SCENARIO), phase], capture_output=True, text=True, env=env, timeout=60, cwd=box.home)


def calls(box):
    return box.log.read_text().splitlines() if box.log.exists() else []


def assert_nothing_destructive_ran(box):
    bad = [c for c in calls(box) if c.startswith("docker ") and DESTRUCTIVE.search(c.split(" ", 1)[1])]
    assert bad == [], bad


def refused(r, why):
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "REFUSED" in r.stderr and why in r.stderr, r.stderr
    assert "scripts/chaos/throwaway_stack.sh" in r.stderr      # it says what to use instead


def test_a_blank_engine_passes_the_guard_and_only_asks_questions(box):
    r = run(box, "phase-a")
    assert r.returncode == 0 and "guard passed (phase-a)" in r.stdout, r.stderr
    assert_nothing_destructive_ran(box)
    assert all(c.startswith(("docker info", "docker ps", "docker volume ls", "docker network ls", "docker image ls", "ss ")) for c in calls(box)), calls(box)


@pytest.mark.parametrize("state_file,content,why", [
    ("containers", "jarvis-db\n", "containers with the ledger's names"),
    ("containers", "jarvis-app\njarvis-migrate\n", "containers with the ledger's names"),
    ("containers", "jarvis-drill-db\n", "containers with the ledger's names"),
    ("volumes", "jarvis-ledger_pgdata\n", "volumes of a ledger"),
    ("volumes", "jarvis-drill-x\n", "volumes of a ledger"),
    ("networks", "jarvis-ledger_ledger\n", "networks of a ledger"),
    ("images", "jarvis-ledger-app:local\n", "images of a ledger build"),
    ("images", "jarvis-ledger-db:16\n", "images of a ledger build"),
    ("ports", "8011\n", "something is listening"),
    ("ports", "8001\n", "something is listening"),
    ("ports", "18001\n", "something is listening"),
])
def test_an_engine_that_has_any_trace_of_the_ledger_is_refused_before_anything_destructive(box, state_file, content, why):
    (box.state / state_file).write_text(content)
    refused(run(box, "phase-a"), why)
    assert_nothing_destructive_ran(box)


def test_other_peoples_containers_do_not_block_a_blank_rehearsal(box):
    (box.state / "containers").write_text("grok-bot\npostgres-other\n")
    (box.state / "ports").write_text("5432\n8000\n")
    assert run(box, "phase-a").returncode == 0


def test_an_account_that_runs_the_live_ledger_is_refused(box):
    (box.home / "jarvis-ledger").mkdir()
    refused(run(box, "phase-a"), "runs (or ran) the live ledger")
    assert_nothing_destructive_ran(box)


@pytest.mark.parametrize("unit", ["jarvis-backup.timer", "jarvis-seal.timer", "jarvis-sign.service", "jarvis-watchdog.timer"])
def test_an_account_with_the_ledgers_systemd_units_is_refused(box, unit):
    d = box.home / ".config" / "systemd" / "user"
    d.mkdir(parents=True)
    (d / unit).write_text("[Timer]\n")
    refused(run(box, "phase-a"), "already has the ledger's systemd units")
    assert (d / unit).exists()


@pytest.mark.parametrize("failing,why", [("ps -a", "docker ps failed"), ("volume ls", "docker volume ls failed"), ("network ls", "docker network ls failed"),
                                         ("image ls", "docker image ls failed"), ("ss", "ss failed")])
def test_a_probe_that_fails_while_the_engine_answers_is_a_refusal_not_a_blank_engine(box, failing, why):
    """`docker info` works but one question fails (a daemon hiccup): the empty answer must never be read as "nothing of the ledger is here"."""
    hidden = {"ps -a": ("containers", "jarvis-db\n"), "volume ls": ("volumes", "jarvis-ledger_pgdata\n"), "network ls": ("networks", "jarvis-ledger_ledger\n"),
              "image ls": ("images", "jarvis-ledger-app:local\n"), "ss": ("ports", "8011\n")}
    name, trace = hidden[failing]
    (box.state / name).write_text(trace)                       # the live trace IS there; only the failing probe hides it
    (box.state / "fail").write_text(failing)
    r = run(box, "phase-a")
    refused(r, why)
    assert_nothing_destructive_ran(box)


def test_an_unreadable_engine_is_refused(box):
    (box.bindir / "docker").write_text("#!/usr/bin/env bash\nexit 1\n")
    refused(run(box, "phase-a"), "cannot read this Docker engine's identity")
    assert_nothing_destructive_ran(box)


def test_a_machine_with_no_docker_at_all_is_refused(box):
    """A PATH that holds the shell tools the script needs and nothing else (a real docker on the test machine must not matter)."""
    import shutil

    tools = box.tmp / "tools"
    tools.mkdir()
    for name in ("bash", "env", "id", "cat", "grep", "awk", "paste", "ls", "dirname", "mkdir", "rm", "tr", "sed", "sort", "tail", "head", "date"):
        found = shutil.which(name)
        if found:
            (tools / name).symlink_to(found)
    assert not (tools / "docker").exists()
    r = run(box, "phase-a", extra_env={"PATH": str(tools)})
    refused(r, "docker is not installed here")


@pytest.mark.parametrize("phase", ["phase-b", "teardown"])
def test_later_phases_refuse_without_the_stamp_phase_a_leaves(box, phase):
    refused(run(box, phase), "phase-a never passed on this engine")
    assert_nothing_destructive_ran(box)


@pytest.mark.parametrize("phase", ["phase-b", "teardown"])
def test_later_phases_refuse_on_a_different_engine_than_the_one_that_passed(box, phase):
    (box.home / "jarvis-rehearsal").mkdir()
    (box.home / "jarvis-rehearsal" / "engine.ok").write_text("ENGINE-OTHER\n")   # stamped by some other daemon
    refused(run(box, phase), "not the one that passed the blank-engine check")
    assert_nothing_destructive_ran(box)


@pytest.mark.parametrize("phase", ["phase-b", "teardown"])
def test_later_phases_proceed_on_the_engine_that_passed(box, phase):
    (box.home / "jarvis-rehearsal").mkdir()
    (box.home / "jarvis-rehearsal" / "engine.ok").write_text("ENGINE-A\n")
    r = run(box, phase)
    assert r.returncode == 0 and f"guard passed ({phase})" in r.stdout, r.stderr


def test_a_live_looking_engine_cannot_be_unlocked_by_hand_writing_a_stamp_on_phase_a(box):
    (box.state / "containers").write_text("jarvis-db\n")
    (box.home / "jarvis-rehearsal").mkdir()
    (box.home / "jarvis-rehearsal" / "engine.ok").write_text("ENGINE-A\n")
    refused(run(box, "phase-a"), "containers with the ledger's names")   # phase-a always demands a blank engine, whatever stamp exists


def test_an_unknown_phase_is_a_usage_error_and_runs_no_docker_command(box):
    r = run(box, "nonsense")
    assert r.returncode == 2 and "usage" in r.stderr
    assert calls(box) == []


def test_the_guard_runs_before_any_other_command_in_the_script():
    text = SCENARIO.read_text()
    dispatch = text.index('case "$PHASE" in phase-a|phase-b|teardown) require_isolated_engine "$PHASE" ;; esac')
    assert text.index("\ncase \"$PHASE\" in\n  phase-a)") > dispatch
    # no docker command is executed at the top level before the dispatcher (functions are only defined there)
    top_level = [l for l in text[:dispatch].splitlines() if re.match(r"docker\b", l)]
    assert top_level == []
    # the destructive commands exist only inside functions the dispatcher calls after the guard
    for needle in ("docker kill jarvis-db", "docker rm -f \"$PC\" jarvis-db", "systemctl --user disable --now"):
        pos = text.index(needle)
        assert text.rfind("\n}\n", 0, pos) < text.rfind("() {", 0, pos), f"{needle!r} is not inside a function"


def test_phase_a_writes_the_engine_stamp_only_after_it_has_wiped_its_own_directory():
    text = SCENARIO.read_text()
    body = text[text.index("prepare() {"):text.index("phase_a() {")]
    assert body.index('rm -rf "$R"') < body.index('> "$R/engine.ok"')


# --- review follow-ups: stale stamps, the XDG unit directory, a failed stamp write ----------------------------------------------------------

def stamp_path(box):
    return box.home / "jarvis-rehearsal" / "engine.ok"


def write_stamp(box, engine="ENGINE-A", age_minutes=0):
    stamp = stamp_path(box)
    stamp.parent.mkdir(exist_ok=True)
    stamp.write_text(engine + "\n")
    if age_minutes:
        old = stamp.stat().st_mtime - age_minutes * 60
        os.utime(stamp, (old, old))
    return stamp


def test_a_refused_phase_a_removes_an_old_stamp_so_a_later_teardown_cannot_use_it(box):
    """An engine that passed once, was left with KEEP=1 or after an interruption, and was then given to the live ledger: the next driver run's
    phase-a refuses, and its EXIT cleanup (teardown) must not be authorized by the old stamp."""
    stamp = write_stamp(box)
    (box.state / "containers").write_text("jarvis-db\njarvis-app\n")          # the live ledger is on this engine now
    refused(run(box, "phase-a"), "containers with the ledger's names")
    assert not stamp.exists()
    refused(run(box, "teardown"), "phase-a never passed on this engine")
    assert_nothing_destructive_ran(box)


@pytest.mark.parametrize("make_refuse", [
    lambda b: (b.state / "ports").write_text("8011\n"),
    lambda b: (b.home / "jarvis-ledger").mkdir(),
    lambda b: (b.state / "fail").write_text("ps -a"),
])
def test_phase_a_removes_the_old_stamp_whatever_it_is_refused_for(box, make_refuse):
    stamp = write_stamp(box)
    make_refuse(box)
    assert run(box, "phase-a").returncode == 2
    assert not stamp.exists()


def test_a_stamp_expires(box):
    write_stamp(box, age_minutes=24 * 60 + 5)
    for phase in ("phase-b", "teardown"):
        refused(run(box, phase), "is older than")
    write_stamp(box, age_minutes=60)
    assert run(box, "teardown").returncode == 0


@pytest.mark.parametrize("phase", ["phase-b", "teardown"])
def test_a_valid_stamp_does_not_authorize_anything_on_an_engine_that_serves_the_live_ledger(box, phase):
    write_stamp(box)
    (box.home / "jarvis-ledger").mkdir()
    refused(run(box, phase), "runs (or ran) the live ledger")
    (box.home / "jarvis-ledger").rmdir()
    (box.state / "ports").write_text("8011\n")
    refused(run(box, phase), "something is listening on a ledger port")
    (box.state / "ports").write_text("8001\n")
    refused(run(box, phase), "something is listening on a ledger port")
    (box.state / "ports").write_text("18001\n")                                # the rehearsal's own port is expected during a rehearsal
    assert run(box, phase).returncode == 0
    assert_nothing_destructive_ran(box)


def test_later_phases_refuse_when_ss_fails(box):
    write_stamp(box)
    (box.state / "fail").write_text("ss")
    refused(run(box, "teardown"), "ss failed")


@pytest.mark.parametrize("unit", ["jarvis-backup.timer", "jarvis-seal.timer", "jarvis-sign.service"])
def test_the_xdg_unit_directory_is_checked_where_install_units_puts_the_units(box, unit):
    xdg = box.tmp / "xdgconfig"
    (xdg / "systemd" / "user").mkdir(parents=True)
    (xdg / "systemd" / "user" / unit).write_text("[Timer]\n")
    refused(run(box, "phase-a", extra_env={"XDG_CONFIG_HOME": str(xdg)}), "already has the ledger's systemd units")
    assert (xdg / "systemd" / "user" / unit).exists()
    # and the default location is still checked when XDG_CONFIG_HOME points somewhere empty
    empty = box.tmp / "emptyxdg"
    empty.mkdir()
    d = box.home / ".config" / "systemd" / "user"
    d.mkdir(parents=True)
    (d / unit).write_text("[Timer]\n")
    refused(run(box, "phase-a", extra_env={"XDG_CONFIG_HOME": str(empty)}), "already has the ledger's systemd units")


def test_teardown_removes_units_only_from_the_directory_install_units_used():
    text = SCENARIO.read_text()
    assert 'rm -f "$UNIT_DIR"/jarvis-*.service "$UNIT_DIR"/jarvis-*.timer' in text
    assert 'UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"' in text
    assert 'dest="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"' in (SCENARIO.parents[1] / "bin" / "install-units.sh").read_text()


def test_the_stamp_records_the_id_the_guard_already_validated_and_a_failed_write_is_a_refusal(box):
    text = SCENARIO.read_text()
    assert "engine_id > " not in text                                           # no second question to the engine
    assert 'ENGINE_ID_VALIDATED="$eid"' in text and 'printf \'%s\\n\' "$ENGINE_ID_VALIDATED" > "$R/engine.ok" || refuse' in text
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    box.home.chmod(0o555)                                                       # prepare cannot create ~/jarvis-rehearsal
    try:
        r = run(box, "phase-a", guard_only=False)
    finally:
        box.home.chmod(0o755)
    assert r.returncode == 2 and "cannot record the engine stamp" in r.stderr, (r.returncode, r.stderr)
    assert_nothing_destructive_ran(box)
