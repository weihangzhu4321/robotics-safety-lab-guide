#!/usr/bin/env python3
"""Pure offline checks for the separate lab-six controller copy.

Reads reviewed source files, runs bash syntax/help, and exercises real Python
methods with in-memory stand-ins. Never starts Docker, ROS, Tk, or a network
client. The output directory must be new or empty and outside the kit. Generator refusal
checks use disposable fixture copies, never the deployed pair.
"""

import argparse
import ast
import hashlib
import itertools
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
from datetime import datetime, timezone
from unittest.mock import patch

sys.dont_write_bytecode = True

# These are the reviewed 2026-10-22 kit originals and lab-six generator. Unknown
# revisions are recorded but never imported/executed; review them before updating.
REVIEWED = {
    "lab.sh": "015d04737a64cf2d8a34dccf1279263621c0a3bb94486a7065c426e1c0fe3e12",
    "scripts/teleop_ros1.py": "a28c793a8924b255b71fe59c457ce600cbae6f2c2906a7333efb8b43ab7b5ae0",
    "scripts/freshness.py": "8412bb48640d6cc7fa2320c7ee705a1cec6b84b09e69e233857dfdfcfe4fe718",
    "prepare_low_speed.py": "578e8149e2eea3c996abc1ea0ac2a826bab072f7793afd871b83ccff3016366c",
}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Checks:
    def __init__(self):
        self.rows = []

    def run(self, name, function):
        try:
            detail = function()
            self.rows.append({"name": name, "status": "PASS", "detail": detail})
            return True
        except Exception as exc:
            self.rows.append({"name": name, "status": "FAIL",
                              "detail": "%s: %s" % (type(exc).__name__, exc)})
            return False

    def skip(self, name, reason):
        self.rows.append({"name": name, "status": "SKIP", "detail": reason})


def clean_env():
    env = dict(os.environ)
    for key in ("BASH_ENV", "ENV", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME"):
        env.pop(key, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_command(argv, cwd=None):
    result = subprocess.run(argv, cwd=cwd, env=clean_env(), capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=15)
    detail = {"argv": [str(value) for value in argv], "returncode": result.returncode,
              "stdout": result.stdout, "stderr": result.stderr}
    require(result.returncode == 0, json.dumps(detail, ensure_ascii=False))
    return detail


class FakeRoot:
    def __init__(self):
        self.jobs, self.next_id, self.destroyed = {}, 0, False

    def schedule(self, kind, callback):
        self.next_id += 1
        self.jobs[self.next_id] = (kind, callback)
        return self.next_id

    def after(self, _delay, callback):
        return self.schedule("timer", callback)

    def after_idle(self, callback):
        return self.schedule("idle", callback)

    def after_cancel(self, handle):
        self.jobs.pop(handle, None)

    def idle(self):
        for handle, (kind, callback) in list(self.jobs.items()):
            if kind == "idle" and handle in self.jobs:
                self.jobs.pop(handle)
                callback()

    def destroy(self):
        self.destroyed = True


class FakeRos:
    class ROSException(Exception):
        pass

    def __init__(self):
        self.shutdown, self.shutdown_reasons, self.now = False, [], 100.0
        self.Time = types.SimpleNamespace(now=lambda: types.SimpleNamespace(to_sec=lambda: self.now))

    def is_shutdown(self):
        return self.shutdown

    def signal_shutdown(self, reason):
        self.shutdown = True
        self.shutdown_reasons.append(reason)


class GateMonitor:
    def __init__(self, ready=True):
        self.ready = ready

    def snapshot(self, _ros_now):
        return {"ready": self.ready, "failure_reasons": [] if self.ready else ["mock stale"],
                "warnings": []}


def fake_control(module):
    obj = module.KeyboardControl.__new__(module.KeyboardControl)
    obj.ros, obj.root, obj.monitor = FakeRos(), FakeRoot(), GateMonitor()
    obj.pressed, obj.blocked, obj.pending_releases = set(), set(), {}
    obj.focused, obj.closing, obj.commands, obj.status_rows = True, False, [], []
    obj.Twist = lambda: types.SimpleNamespace(linear=types.SimpleNamespace(x=0.0),
                                            angular=types.SimpleNamespace(z=0.0))
    obj.pub = types.SimpleNamespace(publish=lambda msg: obj.commands.append((msg.linear.x, msg.angular.z)))
    obj.status = types.SimpleNamespace(configure=lambda **kw: obj.status_rows.append(kw))
    obj.unregistered = []
    obj.subscribers = [types.SimpleNamespace(unregister=lambda index=index: obj.unregistered.append(index))
                       for index in range(2)]
    return obj


def event(key):
    return types.SimpleNamespace(keysym=key)


def load_reviewed_modules(source):
    fresh = types.ModuleType("freshness")
    exec(compile(source["scripts/freshness.py"], "reviewed/freshness.py", "exec"), fresh.__dict__)
    old = sys.modules.get("freshness")
    sys.modules["freshness"] = fresh
    try:
        low = types.ModuleType("checked_teleop_lab6")
        exec(compile(source["scripts/teleop_lab6_ros1.py"], "actual/teleop_lab6_ros1.py", "exec"), low.__dict__)
    finally:
        if old is None:
            sys.modules.pop("freshness", None)
        else:
            sys.modules["freshness"] = old
    return fresh, low


def exercise_behaviour(checks, fresh, low):
    def velocity_table():
        directions = "wasd"
        subsets = [set(items) for n in range(5) for items in itertools.combinations(directions, n)]
        count = 0
        for pressed, blocked, focused, ready in itertools.product(subsets, subsets, (False, True), (False, True)):
            active = pressed - blocked if focused and ready else set()
            expected = (0.01 * (("w" in active) - ("s" in active)),
                        0.10 * (("a" in active) - ("d" in active)))
            require(low.velocity_for_keys(pressed, blocked, focused, ready) == expected,
                    "unexpected velocity for %r" % ((pressed, blocked, focused, ready),))
            count += 1
        return {"combinations": count, "linear_limit": 0.01, "angular_limit": 0.10}
    checks.run("velocity.all_key_focus_sensor_latch_combinations", velocity_table)

    def press_release():
        obj = fake_control(low)
        obj.down(event("W")); obj.tick()
        require(obj.commands[-1] == (0.01, 0.0), "W not low speed")
        obj.up(event("w")); obj.root.idle()
        require(obj.commands[-1] == (0.0, 0.0) and not obj.pressed, "release did not zero")
        return "real down/tick/up/send methods; idle release sends zero immediately"
    checks.run("state.press_and_actual_release_zero", press_release)

    def diagonal_release():
        obj = fake_control(low)
        obj.down(event("w")); obj.down(event("a")); obj.tick()
        require(obj.commands[-1] == (0.01, 0.10), "diagonal mismatch")
        obj.up(event("w")); obj.root.idle()
        require(obj.commands[-1] == (0.0, 0.10), "release must preserve remaining turn")
        obj.up(event("a")); obj.root.idle()
        require(obj.commands[-1] == (0.0, 0.0), "last release did not zero")
    checks.run("state.partial_release_preserves_other_axis", diagonal_release)

    for key in ("space", "Escape"):
        def latch(key=key):
            obj = fake_control(low)
            obj.down(event("w")); obj.tick(); obj.down(event(key))
            require(obj.commands[-1] == (0.0, 0.0) and "w" in obj.blocked, "stop failed")
            obj.down(event("w")); obj.tick()
            require(obj.commands[-1] == (0.0, 0.0), "held/repeated key bypassed latch")
            obj.up(event("w")); obj.root.idle(); obj.down(event("w")); obj.tick()
            require(obj.commands[-1] == (0.01, 0.0), "release/repress failed to recover")
        checks.run("state.%s_latch_release_repress" % key.lower(), latch)

    def focus():
        obj = fake_control(low)
        obj.down(event("w")); obj.up(event("w")); obj.focus_out(None)
        require(not obj.pending_releases and not obj.pressed and not obj.blocked,
                "focus loss did not clear key state")
        require(obj.commands[-1] == (0.0, 0.0) and not obj.focused, "focus loss failed to stop")
        obj.root.idle(); obj.focus_in(None); obj.tick()
        require(obj.commands[-1] == (0.0, 0.0), "focus return resumed stale key")
        obj.down(event("w")); obj.tick()
        require(obj.commands[-1] == (0.01, 0.0), "new press after focus failed")
    checks.run("state.focus_loss_zero_and_no_automatic_resume", focus)

    def autorepeat():
        obj = fake_control(low)
        obj.down(event("w")); obj.tick(); obj.up(event("w")); obj.down(event("w"))
        obj.root.idle(); obj.tick()
        require(obj.commands[-1] == (0.01, 0.0) and "w" in obj.pressed and not obj.pending_releases,
                "repeat release/press lost held key")
        obj.up(event("w")); obj.root.idle()
        require(obj.commands[-1] == (0.0, 0.0), "final release failed")
    checks.run("state.x11_repeat_cancels_only_pending_release", autorepeat)

    def sensor_latch():
        obj = fake_control(low)
        obj.down(event("w")); obj.tick(); obj.monitor.ready = False; obj.tick()
        require(obj.commands[-1] == (0.0, 0.0) and "w" in obj.blocked, "sensor failure failed to latch")
        obj.monitor.ready = True; obj.tick()
        require(obj.commands[-1] == (0.0, 0.0), "sensor recovery automatically resumed held key")
        obj.up(event("w")); obj.root.idle(); obj.down(event("w")); obj.tick()
        require(obj.commands[-1] == (0.01, 0.0), "release/repress after sensor recovery failed")
    checks.run("state.sensor_failure_latches_until_release", sensor_latch)

    def close():
        obj = fake_control(low)
        with patch.object(low.time, "sleep", return_value=None):
            obj.close(); obj.close()
        require(obj.commands == [(0.0, 0.0)] * 3, "close did not send exactly three zeros")
        require(obj.ros.shutdown and len(obj.ros.shutdown_reasons) == 1, "shutdown not idempotent")
        require(obj.unregistered == [0, 1] and obj.root.destroyed, "close cleanup incomplete")
    checks.run("state.close_three_zeros_and_idempotent_cleanup", close)

    def shutdown_tick():
        obj = fake_control(low); obj.ros.shutdown = True
        with patch.object(low.time, "sleep", return_value=None):
            obj.tick()
        require(obj.closing and obj.commands == [(0.0, 0.0)] * 3, "ROS-shutdown tick did not close")
    checks.run("state.shutdown_tick_closes_without_nonzero", shutdown_tick)

    def fresh_state(stamp=100.0, receipt=10.0, ros_now=100.1, monotonic_now=10.1, omit=None):
        monitor = fresh.FreshnessMonitor()
        for name in ("scan", "odom"):
            if name != omit:
                monitor.receive(name, [stamp], received_at=receipt)
        return monitor.snapshot(ros_now, monotonic_now)

    checks.run("freshness.both_sensors_required", lambda: require(
        fresh_state()["ready"] and not fresh_state(omit="scan")["ready"]
        and not fresh_state(omit="odom")["ready"], "missing sensor accepted"))
    for label, kwargs in (
        ("receipt_one_second", {"monotonic_now": 11.0}),
        ("receipt_future", {"monotonic_now": 9.9}),
        ("header_past_half_second", {"ros_now": 100.5}),
        ("header_future_half_second", {"ros_now": 99.5}),
        ("header_zero", {"stamp": 0.0}),
        ("header_missing", {"stamp": None}),
        ("header_nan", {"stamp": float("nan")}),
        ("header_infinite", {"stamp": float("inf")}),
    ):
        checks.run("freshness.reject_" + label, lambda kwargs=kwargs: require(
            not fresh_state(**kwargs)["ready"], "invalid timestamp/freshness accepted"))
    checks.run("freshness.warning_is_not_failure", lambda: require(
        fresh_state(ros_now=100.4)["ready"] and fresh_state(ros_now=100.4)["warnings"],
        "warning band not preserved"))

    def real_gate():
        obj = fake_control(low); obj.monitor = fresh.FreshnessMonitor(); obj.ros.now = 100.1
        for name in ("scan", "odom"):
            obj.monitor.receive(name, [100.0], received_at=10.0)
        obj.down(event("w"))
        with patch.object(fresh.time, "monotonic", return_value=10.1):
            obj.tick()
        require(obj.commands[-1] == (0.01, 0.0), "real fresh gate did not permit press")
        with patch.object(fresh.time, "monotonic", return_value=11.0):
            obj.tick()
        require(obj.commands[-1] == (0.0, 0.0) and "w" in obj.blocked,
                "real stale gate failed to zero/latch")
    checks.run("state.real_freshness_monitor_integration", real_gate)


def generator_fixtures(checks, generator, source, output):
    with tempfile.TemporaryDirectory(prefix="fixture_", dir=output) as tmp:
        kit = Path(tmp) / "kit"; (kit / "scripts").mkdir(parents=True)
        for name in ("lab.sh", "scripts/teleop_ros1.py"):
            (kit / name).write_bytes(source[name])
        argv = [sys.executable, "-I", "-B", str(generator), str(kit)]
        checks.run("generator.fixture_create", lambda: run_command(argv))
        outputs = [kit / "lab6.sh", kit / "scripts/teleop_lab6_ros1.py"]
        baseline = [path.read_bytes() for path in outputs] if all(path.is_file() for path in outputs) else None
        if baseline is None:
            checks.skip("generator.fixture_idempotent", "fixture creation failed")
            checks.skip("generator.fixture_refuse_altered_pair", "fixture creation failed")
            checks.skip("generator.fixture_refuse_partial_pair", "fixture creation failed")
            return
        def idempotent():
            detail = run_command(argv)
            require("existing:" in detail["stdout"], "rerun was not explicitly existing")
            require([path.read_bytes() for path in outputs] == baseline, "rerun altered generated pair")
            return detail
        checks.run("generator.fixture_idempotent", idempotent)
        def altered():
            outputs[0].write_bytes(baseline[0] + b"\n# fixture alteration\n")
            before = [path.read_bytes() for path in outputs]
            result = subprocess.run(argv, env=clean_env(), capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=15)
            require(result.returncode == 2 and "refusing to overwrite" in result.stderr,
                    "altered pair was not refused")
            require([path.read_bytes() for path in outputs] == before, "altered pair was overwritten")
            return {"returncode": result.returncode, "stderr": result.stderr}
        checks.run("generator.fixture_refuse_altered_pair", altered)
        def partial():
            outputs[0].write_bytes(baseline[0]); outputs[1].unlink()
            result = subprocess.run(argv, env=clean_env(), capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=15)
            require(result.returncode == 2 and "refusing to overwrite" in result.stderr,
                    "partial pair was not refused")
            require(outputs[0].read_bytes() == baseline[0] and not outputs[1].exists(),
                    "partial pair was modified/completed")
            return {"returncode": result.returncode, "stderr": result.stderr}
        checks.run("generator.fixture_refuse_partial_pair", partial)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", required=True, help="actual robot_labs directory")
    parser.add_argument("--output", required=True, help="new or empty evidence directory outside the kit")
    args = parser.parse_args(argv)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        parser.error("run as the ordinary desktop user, never sudo")
    kit = Path(args.kit).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    if output == kit or kit in output.parents:
        parser.error("--output must be outside the kit")
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            parser.error("--output must be new or empty; existing evidence is never overwritten")
    else:
        output.mkdir(parents=True, exist_ok=False)
    checks, source, hashes = Checks(), {}, {}
    generator = Path(__file__).resolve().parent.parent / "工具" / "prepare_low_speed.py"
    paths = {name: kit / name for name in (
        "lab.sh", "lab6.sh", "scripts/teleop_ros1.py", "scripts/teleop_lab6_ros1.py", "scripts/freshness.py")}
    paths["prepare_low_speed.py"] = generator
    for name, path in paths.items():
        def read(name=name, path=path):
            require(not path.is_symlink() and path.is_file(), "not a regular non-symlink file: %s" % path)
            data = path.read_bytes(); data.decode("utf-8")
            source[name], hashes[name] = data, digest(data)
            return {"path": str(path), "bytes": len(data), "sha256": hashes[name]}
        checks.run("file." + name, read)
    trust = {}
    for name, expected in REVIEWED.items():
        trust[name] = checks.run("reviewed_source." + name, lambda name=name, expected=expected: require(
            hashes.get(name) == expected, "reviewed SHA256 mismatch; unknown source will not be executed"))
    lab_ok = teleop_ok = False
    if "lab.sh" in source and "lab6.sh" in source:
        def exact_lab():
            require(source["lab.sh"].count(b"/lab/scripts/teleop_ros1.py") == 1, "source path count changed")
            require(source["lab6.sh"] == source["lab.sh"].replace(
                b"/lab/scripts/teleop_ros1.py", b"/lab/scripts/teleop_lab6_ros1.py", 1),
                "lab6.sh has changes beyond the teleop target")
        lab_ok = checks.run("derived.lab6_only_teleop_target_changed", exact_lab)
    if "scripts/teleop_ros1.py" in source and "scripts/teleop_lab6_ros1.py" in source:
        def exact_teleop():
            text = source["scripts/teleop_ros1.py"].decode("utf-8")
            for old, new in (("0.05 *", "0.01 *"), ("0.35 *", "0.10 *"),
                             ('self.root.title("实验二/三 低速键盘控制")', 'self.root.title("实验六低速键盘控制")')):
                require(text.count(old) == 1, "expected one replacement: " + old)
                text = text.replace(old, new, 1)
            require(source["scripts/teleop_lab6_ros1.py"] == text.encode("utf-8"),
                    "low-speed copy has changes beyond speed constants/title")
        teleop_ok = checks.run("derived.teleop_only_limits_and_title_changed", exact_teleop)
    for name in ("scripts/teleop_ros1.py", "scripts/teleop_lab6_ros1.py", "scripts/freshness.py"):
        if name in source:
            checks.run("ast." + name, lambda name=name: (ast.parse(source[name], filename=str(paths[name])), "syntax valid")[1])
    bash = shutil.which("bash")
    if not bash and os.name == "nt":
        candidate = Path("C:/Program Files/Git/bin/bash.exe")
        if candidate.is_file():
            bash = str(candidate)
    checks.run("dependency.bash", lambda: require(bool(bash), "bash is not installed"))
    if bash:
        for name in ("lab.sh", "lab6.sh"):
            if name in source:
                checks.run("bash_syntax." + name, lambda name=name: run_command([bash, "--noprofile", "--norc", "-n", str(paths[name])]))
        if trust.get("lab.sh") and lab_ok:
            def help_check():
                detail = run_command([bash, "--noprofile", "--norc", str(paths["lab6.sh"]), "help"], cwd=kit)
                require("bash lab.sh teleop" in detail["stdout"], "help text missing expected command")
                (output / "lab6_help.txt").write_text(detail["stdout"], encoding="utf-8")
                return detail
            checks.run("entry.lab6_help_no_docker_or_ros", help_check)
        else:
            checks.skip("entry.lab6_help_no_docker_or_ros", "unreviewed/altered shell source")
    if teleop_ok and trust.get("scripts/teleop_ros1.py") and trust.get("scripts/freshness.py"):
        try:
            fresh, low = load_reviewed_modules(source)
            exercise_behaviour(checks, fresh, low)
        except Exception as exc:
            checks.run("behaviour.import_reviewed_modules", lambda: (_ for _ in ()).throw(exc))
    else:
        checks.skip("behaviour.all", "missing, unreviewed, or not-exactly-derived source")
    if trust.get("prepare_low_speed.py") and trust.get("lab.sh") and trust.get("scripts/teleop_ros1.py"):
        generator_fixtures(checks, generator, source, output)
    else:
        checks.skip("generator.fixtures", "unreviewed generator or source")
    after = {}
    for name, path in paths.items():
        def unchanged(name=name, path=path):
            require(name in hashes, "file was missing at initial read")
            after[name] = digest(path.read_bytes())
            require(after[name] == hashes[name], "source changed during check")
            return after[name]
        checks.run("unchanged." + name, unchanged)
    counts = {status: sum(row["status"] == status for row in checks.rows) for status in ("PASS", "FAIL", "SKIP")}
    report = {"checker": "controller_lab6_offline", "checker_sha256": digest(Path(__file__).read_bytes()),
              "finished_utc": datetime.now(timezone.utc).isoformat(), "kit": str(kit),
              "scope": "offline source/entry/function/state-machine checks only; no ROS/Tk/Docker/network/robot validation",
              "python": sys.version, "source_sha256_before": hashes, "source_sha256_after": after,
              "counts": counts, "checks": checks.rows}
    (output / "checks.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in checks.rows:
        print("%s %s" % (row["status"], row["name"]))
        if row["status"] != "PASS":
            print("  %s" % row["detail"])
    print("OFFLINE_CHECK %s: PASS=%d FAIL=%d SKIP=%d; %s" % (
        "PASS" if not counts["FAIL"] and not counts["SKIP"] else "INCOMPLETE",
        counts["PASS"], counts["FAIL"], counts["SKIP"], output / "checks.json"))
    return 0 if not counts["FAIL"] and not counts["SKIP"] else 2


if __name__ == "__main__":
    sys.exit(main())
