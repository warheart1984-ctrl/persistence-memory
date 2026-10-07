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


@pytest.fixture
def box(tmp_path):
    """A fake machine: a HOME, a fake docker engine (state in files), a fake ss, and a log of every command."""
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    for name in ("containers", "volumes", "networks", "images", "ports"):
        (state / name).write_text("")
    for d in ("labels", "mounts", "nets"):
        (state / d).mkdir()
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
  "inspect -f")
    fmt="$3"; shift 3
    for n in "$@"; do
      case "$fmt" in
        *config_files*) cat {state}/labels/"$n" 2>/dev/null || echo ;;
        *Mounts*) cat {state}/mounts/"$n" 2>/dev/null || echo ;;
        *NetworkSettings*) cat {state}/nets/"$n" 2>/dev/null || echo ;;
        *rehearsal.dir*) cat {state}/pc_label 2>/dev/null || echo ;;
        *.Image*) echo "sha256:img-$n" ;;
      esac
    done ;;
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
    systemctl = bindir / "systemctl"            # a fake first on PATH: a test must never reach the real one
    systemctl.write_text(f"""#!/usr/bin/env bash\necho "systemctl $*" >> {log}\nexit 0\n""")
    for f in (docker, ss, systemctl):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return type("Box", (), {"home": home, "state": state, "log": log, "bindir": bindir, "tmp": tmp_path})


def run(box, phase, *, guard_only=True, extra_env=None):
    env = dict(os.environ, HOME=str(box.home), PATH=f"{box.bindir}:{os.environ['PATH']}", REHEARSAL_GUARD_ONLY="1" if guard_only else "0",
               SRC_FROM=str(SCENARIO.parents[3]))
    for name in ("XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "JARVIS_HOME", "JARVIS_APP_PORT"):      # nothing of the machine running the tests may leak into the script
        env.pop(name, None)
    env.update(extra_env or {})
    return subprocess.run(["bash", str(SCENARIO), phase], capture_output=True, text=True, env=env, timeout=60, cwd=box.home)


def calls(box):
    return box.log.read_text().splitlines() if box.log.exists() else []


def assert_nothing_destructive_ran(box):
    """Only the docker SUBCOMMAND counts: `inspect -f ...compose...` is a question, `rm`, `kill`, `compose down` and the like are not."""
    bad = []
    for c in calls(box):
        if not c.startswith("docker "):
            continue
        words = c.split()[1:]
        sub = words[0] if words else ""
        if sub in {"kill", "rm", "rmi", "stop", "run", "compose", "system", "restart", "start", "exec", "build"} or (sub in {"volume", "network", "image"} and len(words) > 1 and words[1] in {"rm", "prune"}):
            bad.append(c)
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
    for needle in ("docker kill jarvis-db", "docker rm -f jarvis-db jarvis-app jarvis-migrate", "systemctl --user disable --now"):
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


# --- the later phases tie what they remove to THIS rehearsal's compose file ------------------------------------------------------------------

def rehearsal_compose(box):
    return str(box.home / "jarvis-rehearsal" / "src" / "deploy" / "mint" / "docker-compose.yml")


MOUNTS = {"jarvis-db": "jarvis-ledger_pgdata", "jarvis-app": "jarvis-ledger_appdata"}


def containers(box, labels, mounts=None, nets="jarvis-ledger_ledger"):
    """containers on the fake engine: {name: compose-file label}; each mounts its own data volume and sits on the ledger network unless told otherwise"""
    (box.state / "containers").write_text("".join(f"{n}\n" for n in labels))
    for n, label in labels.items():
        (box.state / "labels" / n).write_text(label + "\n")
        (box.state / "mounts" / n).write_text((mounts or MOUNTS).get(n, "") + "\n")
        (box.state / "nets" / n).write_text(nets + "\n")


LIVE_COMPOSE = "/home/jon/jarvis-ledger-src/deploy/mint/docker-compose.yml"


@pytest.mark.parametrize("phase", ["phase-b", "teardown"])
def test_containers_created_by_another_compose_file_are_never_touched_whatever_the_stamp_says(box, phase):
    """The reviewer's case: a fresh stamp, the engine since given to the live ledger with a custom JARVIS_HOME and port (so no ~/jarvis-ledger, nothing
    on 8011), live containers present."""
    write_stamp(box)
    (box.state / "ports").write_text("9999\n")
    containers(box, {"jarvis-db": LIVE_COMPOSE, "jarvis-app": LIVE_COMPOSE, "jarvis-migrate": LIVE_COMPOSE})
    (box.state / "volumes").write_text("jarvis-ledger_pgdata\njarvis-ledger_appdata\n")
    refused(run(box, phase), "was not created by this rehearsal's compose file")
    assert_nothing_destructive_ran(box)


@pytest.mark.parametrize("mixed", [
    {"jarvis-db": "OWN", "jarvis-app": LIVE_COMPOSE},
    {"jarvis-db": LIVE_COMPOSE, "jarvis-app": "OWN"},
    {"jarvis-db": "OWN", "jarvis-app": "OWN", "jarvis-migrate": ""},          # a container with no compose label at all
])
def test_one_container_that_is_not_the_rehearsals_is_enough_to_refuse(box, mixed):
    write_stamp(box)
    containers(box, {n: (rehearsal_compose(box) if v == "OWN" else v) for n, v in mixed.items()})
    refused(run(box, "teardown"), "was not created by this rehearsal's compose file")


@pytest.mark.parametrize("phase", ["phase-b", "teardown"])
def test_containers_created_by_this_rehearsals_compose_file_are_accepted(box, phase):
    write_stamp(box)
    containers(box, {n: rehearsal_compose(box) for n in ("jarvis-db", "jarvis-app", "jarvis-migrate")})
    (box.state / "volumes").write_text("jarvis-ledger_pgdata\njarvis-ledger_appdata\n")
    (box.state / "networks").write_text("jarvis-ledger_ledger\n")
    r = run(box, phase)
    assert r.returncode == 0 and "guard passed" in r.stdout, r.stderr


@pytest.mark.parametrize("state_file,content,why", [("volumes", "jarvis-ledger_pgdata\n", "its container jarvis-db is not an authenticated container"),
                                                    ("volumes", "jarvis-ledger_appdata\n", "its container jarvis-app is not an authenticated container"),
                                                    ("networks", "jarvis-ledger_ledger\n", "no authenticated container of this rehearsal is attached"),
                                                    ("volumes", "jarvis-drill-x\n", "is not one this rehearsal creates"),
                                                    ("volumes", "jarvis-ledger_other\n", "is not one this rehearsal creates"),
                                                    ("networks", "jarvis-ledger_other\n", "is not one this rehearsal creates")])
def test_volumes_and_networks_of_a_ledger_with_no_container_to_vouch_for_them_are_refused(box, state_file, content, why):
    """The live stack taken down but its volumes kept: nothing on the engine proves they are the rehearsal's."""
    write_stamp(box)
    (box.state / state_file).write_text(content)
    refused(run(box, "teardown"), why)
    assert_nothing_destructive_ran(box)


def own_containers(box, names=("jarvis-db", "jarvis-app", "jarvis-migrate"), **kw):
    containers(box, {n: rehearsal_compose(box) for n in names}, **kw)


def test_one_authenticated_container_does_not_vouch_for_a_volume_it_does_not_own(box):
    """The review's case: jarvis-migrate is the rehearsal's, but it mounts no data volume; the live stack's volumes must not ride on it."""
    write_stamp(box)
    own_containers(box, names=("jarvis-migrate",))
    (box.state / "volumes").write_text("jarvis-ledger_pgdata\njarvis-ledger_appdata\n")
    refused(run(box, "teardown"), "its container jarvis-db is not an authenticated container")
    own_containers(box, names=("jarvis-migrate", "jarvis-db"))
    refused(run(box, "teardown"), "its container jarvis-app is not an authenticated container")      # db vouches for pgdata only
    assert_nothing_destructive_ran(box)


@pytest.mark.parametrize("mounts", [{}, {"jarvis-db": "somebody-elses-volume", "jarvis-app": "jarvis-ledger_appdata"}, {"jarvis-db": "jarvis-ledger_appdata", "jarvis-app": "jarvis-ledger_pgdata"}])
def test_a_volume_must_actually_be_mounted_by_the_authenticated_container_that_owns_it(box, mounts):
    write_stamp(box)
    own_containers(box, mounts=mounts or {"jarvis-db": "x", "jarvis-app": "y"})
    (box.state / "volumes").write_text("jarvis-ledger_pgdata\njarvis-ledger_appdata\n")
    refused(run(box, "teardown"), "is not mounted by")


def test_the_ledger_network_needs_an_authenticated_container_attached_to_it(box):
    write_stamp(box)
    own_containers(box, nets="some-other-network")
    (box.state / "networks").write_text("jarvis-ledger_ledger\n")
    refused(run(box, "teardown"), "no authenticated container of this rehearsal is attached")
    own_containers(box)
    assert run(box, "teardown").returncode == 0


def test_a_drill_container_or_network_does_not_block_and_is_never_removed_by_name(box):
    """drill.sh creates and removes them itself and they carry no identity: the guard lets teardown proceed, and teardown leaves them alone."""
    write_stamp(box)
    own_containers(box)
    (box.state / "containers").write_text((box.state / "containers").read_text() + "jarvis-drill-db\n")
    (box.state / "networks").write_text("jarvis-ledger_ledger\njarvis-drill-net\n")
    assert run(box, "teardown").returncode == 0
    text = SCENARIO.read_text()
    teardown = text[text.index("teardown() {"):text.index('case "$PHASE" in phase-a|phase-b')]
    removals = [l for l in teardown.splitlines() if re.search(r"docker (rm|network rm|volume rm|rmi)\b", l)]
    assert removals and not any("jarvis-drill" in l for l in removals), removals
    assert "left in place" in teardown


def test_the_pc_stand_in_is_labelled_when_created_and_removed_only_if_the_label_matches():
    text = SCENARIO.read_text()
    assert text.count('com.jarvis.rehearsal.dir=$R') == 2                         # the image build and the container run
    teardown = text[text.index("teardown() {"):text.index('case "$PHASE" in phase-a|phase-b')]
    assert '"com.jarvis.rehearsal.dir"' in teardown and 'pc_owned="$PC"' in teardown
    assert 'docker rm -f "$PC"' not in teardown and 'docker rmi -f "$PC"' not in teardown    # never by its bare name


# --- the systemd units teardown deletes are the rehearsal's own ------------------------------------------------------------------------------------

def write_units(box, directory=None, *, deploy=None, home=None, names=("backup", "drill")):
    d = directory or (box.home / ".config" / "systemd" / "user")
    d.mkdir(parents=True, exist_ok=True)
    deploy = deploy or str(box.home / "jarvis-rehearsal" / "src" / "deploy" / "mint")
    home = home or str(box.home / "jarvis-rehearsal" / "home")
    for n in names:
        (d / f"jarvis-{n}.service").write_text(f"[Service]\nEnvironment=JARVIS_HOME={home}\nExecStart={deploy}/bin/{n}.sh\n")
        (d / f"jarvis-{n}.timer").write_text("[Timer]\nOnCalendar=hourly\n")
    return d


def test_the_rehearsals_own_units_pass_in_either_unit_directory(box):
    write_stamp(box)
    write_units(box)
    assert run(box, "teardown").returncode == 0
    xdg = box.tmp / "xdg"
    write_units(box, xdg / "systemd" / "user")
    assert run(box, "teardown", extra_env={"XDG_CONFIG_HOME": str(xdg)}).returncode == 0


@pytest.mark.parametrize("kwargs,why", [
    ({"deploy": "/home/jon/jarvis-ledger-src/deploy/mint"}, "does not run this rehearsal's checkout"),
    ({"home": "/home/jon/jarvis-ledger"}, "does not use this rehearsal's home"),
])
@pytest.mark.parametrize("phase", ["phase-b", "teardown"])
def test_units_that_belong_to_another_deployment_stop_the_later_phases(box, kwargs, why, phase):
    """The review's case: a valid stamp, the Docker stack gone or stopped, a production deployment's units installed in the same account."""
    write_stamp(box)
    d = write_units(box, **kwargs)
    refused(run(box, phase), why)
    assert (d / "jarvis-backup.service").exists()
    assert_nothing_destructive_ran(box)


def test_one_foreign_unit_among_the_rehearsals_own_is_enough_to_refuse(box):
    write_stamp(box)
    d = write_units(box)
    (d / "jarvis-seal.service").write_text("[Service]\nEnvironment=JARVIS_HOME=/home/jon/jarvis-ledger\nExecStart=/home/jon/jarvis-ledger-src/deploy/mint/bin/seal.sh\n")
    refused(run(box, "teardown"), "jarvis-seal.service does not run this rehearsal's checkout")


def test_a_timer_without_a_service_of_the_rehearsal_beside_it_is_refused(box):
    write_stamp(box)
    d = box.home / ".config" / "systemd" / "user"
    d.mkdir(parents=True)
    (d / "jarvis-seal.timer").write_text("[Timer]\nOnCalendar=hourly\n")
    refused(run(box, "teardown"), "has no service of this rehearsal beside it")


def test_the_default_unit_directory_is_checked_even_when_xdg_config_home_points_elsewhere(box):
    write_stamp(box)
    foreign = write_units(box, deploy="/home/jon/jarvis-ledger-src/deploy/mint")
    other = box.tmp / "elsewhere"
    other.mkdir()
    refused(run(box, "teardown", extra_env={"XDG_CONFIG_HOME": str(other)}), "does not run this rehearsal's checkout")
    assert foreign.exists()


@pytest.mark.parametrize("failing,why", [("ps -a", "docker ps failed"), ("inspect -f", "cannot inspect"), ("volume ls", "docker volume ls failed"),
                                         ("network ls", "docker network ls failed")])
def test_later_phases_refuse_when_a_probe_of_ownership_fails(box, failing, why):
    write_stamp(box)
    containers(box, {"jarvis-db": rehearsal_compose(box)})
    (box.state / "fail").write_text(failing)
    refused(run(box, "teardown"), why)
    assert_nothing_destructive_ran(box)


def test_teardown_removes_images_by_the_id_of_this_rehearsals_containers_never_by_tag():
    text = SCENARIO.read_text()
    teardown = text[text.index("teardown() {"):text.index('case "$PHASE" in phase-a|phase-b')]
    assert "jarvis-ledger-app:local" not in teardown and "jarvis-ledger-db:16" not in teardown
    assert "docker inspect -f '{{.Image}}' jarvis-db jarvis-app jarvis-migrate" in teardown and "docker rmi -f $pc_owned $imgs" in teardown
    assert teardown.index("imgs=") < teardown.index("docker rm -f")      # read before the containers are removed


# --- teardown itself, run for real against the fakes: exactly what it removes -------------------------------------------------------------------------

def full_rehearsal(box, pc_label=None):
    write_stamp(box)
    own_containers(box)
    (box.state / "containers").write_text((box.state / "containers").read_text() + "jarvis-rehearsal-pc\njarvis-drill-db\n")
    (box.state / "volumes").write_text("jarvis-ledger_pgdata\njarvis-ledger_appdata\n")
    (box.state / "networks").write_text("jarvis-ledger_ledger\njarvis-drill-net\n")
    (box.state / "pc_label").write_text((pc_label or str(box.home / "jarvis-rehearsal")) + "\n")
    write_units(box)


def test_teardown_removes_what_was_authenticated_and_nothing_else(box):
    full_rehearsal(box)
    r = run(box, "teardown", guard_only=False)
    assert r.returncode == 0 and "rehearsal environment removed" in r.stdout, r.stderr
    docker_calls = [c for c in calls(box) if c.startswith("docker ") and c.split()[1] in {"rm", "rmi", "network", "volume", "compose"}]
    assert "docker rm -f jarvis-rehearsal-pc" in docker_calls                                            # labelled with this rehearsal's directory
    assert "docker rm -f jarvis-db jarvis-app jarvis-migrate" in docker_calls
    assert "docker network rm jarvis-ledger_ledger" in docker_calls and "docker volume rm -f jarvis-ledger_pgdata jarvis-ledger_appdata" in docker_calls
    rmi = next(c for c in docker_calls if c.startswith("docker rmi"))
    assert rmi.startswith("docker rmi -f jarvis-rehearsal-pc ") and "sha256:img-jarvis-db" in rmi and "sha256:img-jarvis-app" in rmi and "jarvis-ledger-" not in rmi      # by id
    assert not any("jarvis-drill" in c for c in docker_calls)                                                   # a drill's scratch resources are left alone
    assert "left in place" in r.stderr and "jarvis-drill-db" in r.stderr
    assert not (box.home / "jarvis-rehearsal").exists()
    assert not list((box.home / ".config" / "systemd" / "user").glob("jarvis-*"))                             # its own units are gone


def test_teardown_leaves_a_pc_container_that_does_not_carry_this_rehearsals_label(box):
    full_rehearsal(box, pc_label="/home/someone/else/jarvis-rehearsal")
    r = run(box, "teardown", guard_only=False)
    assert r.returncode == 0
    docker_calls = [c for c in calls(box) if c.startswith("docker ") and c.split()[1] in {"rm", "rmi"}]
    assert not any("jarvis-rehearsal-pc" in c for c in docker_calls), docker_calls
    assert "docker rm -f jarvis-db jarvis-app jarvis-migrate" in docker_calls


def test_teardown_refuses_before_removing_anything_when_one_resource_is_not_the_rehearsals(box):
    full_rehearsal(box)
    (box.state / "labels" / "jarvis-app").write_text(LIVE_COMPOSE + "\n")
    r = run(box, "teardown", guard_only=False)
    assert r.returncode == 2 and "REFUSED" in r.stderr
    assert_nothing_destructive_ran(box)
    assert not any(c.startswith("systemctl") for c in calls(box))                                           # no unit was touched either
    assert (box.home / "jarvis-rehearsal").exists()


def test_the_pc_ownership_test_is_in_the_script_verbatim():
    assert '[ "$(docker inspect -f \'{{index .Config.Labels "com.jarvis.rehearsal.dir"}}\' "$PC" 2>/dev/null)" = "$R" ] && pc_owned="$PC"' in SCENARIO.read_text()


def test_the_machine_running_the_tests_cannot_leak_its_unit_directory_into_the_script(box, monkeypatch):
    """CI sets XDG_CONFIG_HOME; a developer's machine may hold real jarvis units there.  The fake account must be the only one the script sees."""
    elsewhere = box.tmp / "real-xdg" / "systemd" / "user"
    elsewhere.mkdir(parents=True)
    (elsewhere / "jarvis-backup.service").write_text("[Service]\nExecStart=/real/live/deploy/mint/bin/backup.sh\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(box.tmp / "real-xdg"))
    full_rehearsal(box)
    r = run(box, "teardown", guard_only=False)
    assert r.returncode == 0, r.stderr
    assert (elsewhere / "jarvis-backup.service").exists()                                  # untouched
    assert not list((box.home / ".config" / "systemd" / "user").glob("jarvis-*"))          # the fake account's own units were the ones removed
