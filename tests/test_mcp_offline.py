"""Offline tests for tools/endstar_unity_mcp.py and tools/unity_cmd.py. A fake bridge thread answers a
temporary inbox the way EndstarBridge.cs + PropMaker.cs do, so nothing here needs Unity or touches
the real inbox, the SDK project or the running editor.

    uv run --no-project --with "mcp>=1.10,<2" python -m unittest tests.test_mcp_offline -v
"""
import asyncio
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import endstar_unity_mcp as m  # noqa: E402
import unity_cmd  # noqa: E402

REAL_INBOX, REAL_OUTBOX, REAL_SDK = m.INBOX, m.OUTBOX, m.SDK_PROJECT
FAKE_PID = 4242
STATE_BUSY = "exporting Big Example Prop since 17:58:07, stages 6/14"
STAGE = "EndlessEditor.ExportUtility.ExportStage"
NL = chr(10)   # a newline, for building cell-list text in the tests
EXE = '"C:\\Program Files\\Unity\\Hub\\Editor\\2022.3.62f2\\Editor\\Unity.exe"'


def crash_text(prop):
    return (f'FileNotFoundException: Could not find file "C:\\Users\\x\\AppData\\LocalLow\\DefaultCompany\\Endstar SDK'
            f'\\AssetBundles\\{prop}\\{prop}-StandaloneWindows64"\n'
            "  at System.IO.FileStream..ctor (System.String path) [0x0019e] in <1071>:0\n"
            "  at EndlessEditor.PropSDK.PropExporterV2.UploadExports (System.String unityPackageName) [0x0036c] in <988e>:0\n")


def old_cid(seconds_ago, n=0):
    t = datetime.now() - timedelta(seconds=seconds_ago)
    return t.strftime("%Y%m%d-%H%M%S") + f"-{n % 1000:03d}"


def fake_subprocess(stdout="", code=0, error=None):
    """Stands in for the subprocess module in log_from_command_line and project_unity_pids."""
    def run(*args, **kwargs):
        if error:
            raise error
        return subprocess.CompletedProcess(args, code, stdout, "boom" if code else "")
    return types.SimpleNamespace(run=run, CREATE_NO_WINDOW=0, TimeoutExpired=subprocess.TimeoutExpired)


class FakeBridge(threading.Thread):
    """Mirrors EndstarBridge.cs + PropMaker.cs: claims <id>.cmd by renaming it to <id>.run, refuses
    commands older than 150 s, runs one command per tick, holds one export lock and one prop's pending cells,
    and ends the lock with the same "lock released: <why>" lines. `main` stands for Unity's main thread:
    commands, export callbacks and log events never interleave."""

    def __init__(self, inbox, outbox, editor_log):
        super().__init__(daemon=True)
        self.inbox, self.outbox, self.editor_log = inbox, outbox, editor_log
        self.mode = "answer"   # "answer", "pause" (Unity asleep) or "swallow" (claims commands, never answers)
        self.main = threading.RLock()
        self.exporting = None   # the export lock: the prop being exported
        self.started = None
        self.stages_total, self.stages_done = -1, 0
        self.export_file = None
        self.export_script = "ok"   # see export()
        self.validation_error = ""   # what the SDK's checks say against the prop (the export is then refused)
        self.export_note = ""   # an SDK check Unity could not run: it goes under "export started"
        self.upload_delay = 0.0
        self.pending = None   # the prop whose cells are being generated
        self.cells = {}   # setcells: prop name -> the cells it was given
        self.local_only = True
        self.version = "7"
        self.ping_log = True
        self.navtests = []
        self.ran = []
        self.halt = threading.Event()

    def run(self):
        while not self.halt.is_set():
            if self.mode != "pause":
                self.poll()
            time.sleep(0.05)

    def poll(self):
        for cmd in sorted(self.inbox.glob("*.cmd")):
            line = self.claim(cmd)
            if line is None:
                continue
            if self.mode == "swallow":
                return
            if self.answer(cmd.stem, line):
                return   # one command per tick

    def claim(self, cmd):
        """Bridge.Poll's claim: rename <id>.cmd to <id>.run, read it, delete it. None when Python cancelled it."""
        run = cmd.with_suffix(".run")
        try:
            os.rename(cmd, run)
        except OSError:   # FileNotFoundError / IOException: Python renamed it to .cancelled first
            return None
        line = run.read_text(encoding="utf-8").strip()
        run.unlink()
        return line

    def answer(self, cid, line):
        """Runs one claimed command and writes its answer; False when it was refused as stale (nothing ran)."""
        sent = datetime.strptime(cid, "%Y%m%d-%H%M%S-%f")
        if datetime.now() - sent > timedelta(seconds=150):
            self.write(cid, f"ERROR stale command (sent {sent:%H:%M:%S}), not run")
            return False
        with self.main:
            self.ran.append(line)
            cmd, _, arg = line.partition(" ")
            result = self.handle(cmd, arg.strip())
        self.write(cid, result)
        return True

    def write(self, cid, text):
        try:
            (self.outbox / f"{cid}.txt").write_text(text, encoding="utf-8")
        except OSError:
            pass   # the test has ended and removed its folder

    def handle(self, cmd, arg):
        if cmd == "ping":
            return "2022.3.62f2 C:/fake/Endstar-SDK/Assets" + (f" log={self.editor_log}" if self.ping_log else "")
        if cmd == "exportstate":
            if self.exporting:
                total = "?" if self.stages_total < 0 else self.stages_total
                return f"exporting {self.exporting} since {self.started:%H:%M:%S}, stages {self.stages_done}/{total}"
            return f"cells pending for {self.pending}" if self.pending else "idle"
        if cmd == "makeprop":
            if self.pending:   # every makeprop waits: the SDK's cell manager has one active prop
                return f"ERROR cells for {self.pending} are still being generated"
            p = arg.split("|")
            self.pending = p[1]
            threading.Timer(0.6, self.cells_ready).start()
            return f"reusing profile Assets/User Data/x/Props/{p[1]}\nbase type {p[3]}\ncells pending"
        if cmd == "exportprop":
            if self.exporting:
                return f"ERROR export of {self.exporting} still running since {self.started:%H:%M:%S}"
            if self.pending == arg:
                return f"ERROR cells for {arg} are still being generated"
            if self.validation_error:   # PropMaker.ExportChecks: before the lock is taken and before any log is started
                return "ERROR validation failed, nothing exported: " + self.validation_error
            self.exporting, self.started = arg, datetime.now()
            self.stages_total, self.stages_done = -1, 0
            self.export_file = self.outbox / f"export-{arg.replace(' ', '_')}.log"
            self.export_file.write_text(f"{datetime.now():%H:%M:%S} export of {arg} started\n", encoding="utf-8")
            threading.Thread(target=self.export, args=(arg,), daemon=True).start()
            return f"export started, log {self.export_file}" + (f"\nnote: {self.export_note}" if self.export_note else "")
        if cmd == "propinfo":
            return (f"profile Assets/User Data/x/Props/{arg}/{arg}.asset\nname {arg}  id 1234  version '{self.version}'\n"
                    f"cells 12: (0, 0, 0)\nicon Assets/User Data/x/Props/{arg}/{arg} Icon.png  local only {self.local_only}")
        if cmd == "setcells":
            return self.set_cells(*arg.split("|"))
        if cmd == "navtest":
            name, *points = arg.split("|")
            self.navtests.append((name, points))
            return f"NAVTEST {name}: a SCREEN, NOT PROOF.\n1 -> 2: CONNECTED, 3.0 m along the path, 2 corners"
        return "ERROR unknown command " + cmd

    def set_cells(self, name, file):
        """PropMaker.SetCells: reads a plain file name in the cells folder and refuses what ParseCells refuses."""
        path = m.CELLS_FOLDER / file
        if file != Path(file).name or not path.exists():
            return f"ERROR no cells file {path}"
        cells = []
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            parts = raw.split("#")[0].replace(",", " ").split()
            if not parts:
                continue
            if len(parts) != 3 or not all(p.lstrip("+-").isdigit() for p in parts):
                return f"ERROR {file}: line {number}: want three whole numbers x y z, got '{raw.strip()}'"
            cells.append(tuple(map(int, parts)))
        if not cells or len(cells) != len(set(cells)) or any(c[1] < 0 for c in cells):
            return f"ERROR {file}: not a list the bridge takes"
        self.cells[name] = cells
        return f"cells set {name} {len(cells)} cells (was 12) from {file}, x offset False, z offset False"

    def cells_ready(self):
        """PropMaker.FinishCells: clears the pending prop, then writes its .ready marker."""
        with self.main:
            name, self.pending = self.pending, None
            try:
                (self.outbox / f"makeprop-{name.replace(' ', '_')}.ready").write_text(f"ready {name} 12 cells",
                                                                                       encoding="utf-8")
            except OSError:
                pass

    # The export lock -------------------------------------------------------------------------

    def log(self, text):
        """PropMaker.Append: one timed line in the running export's log, never throwing."""
        try:
            with open(self.export_file, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%H:%M:%S} {text}\n")
        except OSError:
            pass

    def release(self, why):
        """PropMaker.ReleaseExport."""
        if self.exporting:
            self.log(f"lock released: {why}")
            self.exporting = None

    def unity_log(self, condition, stack="", kind="Log"):
        """One entry in Unity's log, then PropMaker.OnLog as Application.logMessageReceived calls it."""
        with self.main:
            try:
                with open(self.editor_log, "a", encoding="utf-8") as f:
                    f.write(condition + "\n" + stack)
            except OSError:
                return
            if not self.exporting:
                return
            # PropMaker.OnLog listens for exceptions only: the SDK writes "Prop Asset Created" and "Prop Update Complete."
            # before its own error check, so no log line says "uploaded".
            if kind == "Exception" and re.search("PropExporterV2|AssetExporter", condition + "\n" + stack):
                bundle = re.search(r"AssetBundles[\\/]([^\\/\"]+)[\\/]", condition)
                if bundle and bundle.group(1).casefold() != self.exporting.casefold():
                    return   # another prop's bundle: not this export's failure
                self.log(f"FAILED (Unity exception) {condition.splitlines()[0].strip()}")
                self.release("exporter exception")

    def exception(self, text):
        condition, _, stack = text.partition("\n")
        self.unity_log(condition, stack, "Exception")

    def stage_done(self):
        """One 'stage done' callback (the caller holds `main`)."""
        self.log(f"stage done {STAGE}")
        self.stages_done += 1

    def export(self, name):
        """The SDK exporter's run, by export_script: "ok" (two stages with the upload between them; a first upload ends
        when the prop stops being local only, a re-export when its last stage is marked), "fail" (the SDK's FAILED
        callback), "crash" (another prop's exception, then this prop's), "other_crash" (another prop's exception, then a
        normal export), "upload_crash" (this prop's exception during the upload, so before the last stage is marked),
        "update_then_fail" (a re-export: the SDK logs its success words "Prop Update Complete." and then reports the
        failure), "create_then_fail" (the same for a first upload: "Prop Asset Created", then the failure),
        "unconfirmed" (all stages done but the last-stage release did not happen: Unity's 3-minute rule),
        "silent" (all stages done, then nothing), "gave_up" (Unity's 20-minute rule before the last stage) or "hang"
        (nothing after the stages line)."""
        script = self.export_script
        with self.main:
            self.stages_total = 2
            self.log(f"stages [{STAGE}, {STAGE}]")
        time.sleep(0.2)
        if script == "hang":
            return
        if script == "fail":
            with self.main:   # the FAILED callback writes the line and releases the lock in one call
                self.log("FAILED Cannot build asset bundles while a build is in progress")
                self.release("the exporter reported a failure")
            return
        if script == "gave_up":
            with self.main:
                self.release("20 minutes after the start")
            return
        if script in ("crash", "other_crash"):
            self.exception(crash_text("Example Car"))   # OnLog leaves another prop's bundle alone
            time.sleep(0.4)
        if script == "crash":
            self.exception(crash_text(name))
            return
        with self.main:
            self.stage_done()
        time.sleep(self.upload_delay)   # the upload: the SDK marks its last stage only after it
        if script == "upload_crash":
            self.exception(crash_text(name))
            return
        if script in ("update_then_fail", "create_then_fail"):
            # The SDK's success words, written before its own error check; then it reports the failure.
            self.unity_log("Prop Update Complete." if script == "update_then_fail" else "Prop Asset Created")
            time.sleep(0.4)   # a slow poll could land between the two, as one can in Unity
            with self.main:
                self.log("FAILED An error occured while exporting the prop.")
                self.release("the exporter reported a failure")
            return
        with self.main:
            self.stage_done()
            if script == "silent":
                return
            if script == "unconfirmed":
                self.release("3 minutes after the last stage")
            elif self.local_only:   # a first upload: WatchExport sees IsOnlyLocal turn false
                self.local_only = False
                self.release("uploaded")
            else:   # a re-export: the flag was false all along, so the last stage itself ends the lock
                self.version = str(int(self.version) + 1)
                self.release("uploaded")


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="endstar-mcp-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.inbox, self.outbox = self.tmp / "inbox", self.tmp / "outbox"
        self.inbox.mkdir()
        self.outbox.mkdir()
        self.editor_log = self.tmp / "fake-editor.log"
        self.editor_log.write_text("Unity started\n", encoding="utf-8")
        fakes = {
            "INBOX": self.inbox, "OUTBOX": self.outbox, "EDITOR_LOG": self.tmp / "editor.log",
            "DEFAULT_LOG": self.tmp / "Editor.log", "SDK_PROJECT": self.tmp / "sdk", "CELLS_FOLDER": self.tmp / "cells",
            # No window messages to the real editor, ever.
            "nudge": lambda: None, "confirm_export_success": lambda: [], "unity_window": lambda: None,
            "unity_pid": lambda: FAKE_PID, "log_from_command_line": lambda pid: None,
            "POLL_SECONDS": 0.05, "READY_SECONDS": 5, "_log_cache": {},
        }
        for name, value in fakes.items():
            patcher = mock.patch.object(m, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertNotEqual(m.INBOX, REAL_INBOX)
        self.assertNotEqual(m.OUTBOX, REAL_OUTBOX)
        self.bridge = FakeBridge(self.inbox, self.outbox, self.editor_log)
        self.bridge.start()
        self.addCleanup(self.bridge.join, 2)
        self.addCleanup(self.bridge.halt.set)

    def leftovers(self, *patterns):
        return sorted(p.name for pattern in patterns for p in self.inbox.glob(pattern))

    # send() -----------------------------------------------------------------------------------

    def test_send_returns_the_answer(self):
        self.assertTrue(m.send("ping", timeout=5).startswith("2022.3.62f2 "))
        self.assertEqual(self.leftovers("*.cmd", "*.run", "*.id", "*.tmp"), [])

    def test_send_can_leave_the_export_box_alone(self):
        pressed = []
        with mock.patch.object(m, "confirm_export_success", lambda: pressed.append(True) or []):
            self.assertTrue(m.send("ping", timeout=5, press_confirm=False).startswith("2022.3.62f2 "))
            self.assertEqual(pressed, [])
            m.send("ping", timeout=5)
            self.assertNotEqual(pressed, [])

    def test_send_reserves_its_id_until_it_ends(self):
        taken, free = old_cid(0, 1), old_cid(0, 2)
        (self.inbox / f"{taken}.id").write_text("", encoding="utf-8")   # another caller holds this id
        ids = iter([taken, free])
        held = []
        with mock.patch.object(m, "new_cid", lambda: next(ids)), \
                mock.patch.object(m, "nudge", lambda: held.append((self.inbox / f"{free}.id").exists())):
            self.assertTrue(m.send("ping", timeout=5).startswith("2022.3.62f2 "))
        self.assertTrue(held and all(held), held)   # reserved all the while it waited
        self.assertTrue((self.outbox / f"{free}.txt").exists())
        self.assertFalse((self.outbox / f"{taken}.txt").exists())
        self.assertEqual(self.leftovers("*.id"), [f"{taken}.id"])   # only the other caller's is left

    def test_old_leftover_command_is_refused_not_run(self):
        leftover = old_cid(200)
        (self.inbox / f"{leftover}.cmd").write_text("exportprop Traffic Cone", encoding="utf-8")
        self.assertTrue(m.send("ping", timeout=5).startswith("2022.3.62f2 "))
        answer = (self.outbox / f"{leftover}.txt").read_text(encoding="utf-8")
        self.assertRegex(answer, r"^ERROR stale command \(sent \d\d:\d\d:\d\d\), not run$")
        self.assertNotIn("exportprop Traffic Cone", self.bridge.ran)

    def test_stale_answer_is_passed_through(self):
        ids = (old_cid(200, n) for n in range(1000))
        with mock.patch.object(m, "new_cid", lambda: next(ids)):
            answer = m.send("exportprop Traffic Cone", timeout=5)
        self.assertTrue(answer.startswith("ERROR stale command"), answer)
        self.assertEqual(self.bridge.ran, [])

    def test_timeout_cancels_the_command(self):
        self.bridge.mode = "pause"
        with self.assertRaises(TimeoutError) as caught:
            m.send("exportprop Traffic Cone", timeout=1)
        self.assertIn("cancelled, so it will not run later", str(caught.exception))
        self.assertEqual(self.leftovers("*.cmd", "*.run", "*.id"), [])
        self.assertEqual(len(self.leftovers("*.cancelled")), 1)
        self.bridge.mode = "answer"   # Unity wakes up: the cancelled command must never run
        time.sleep(0.3)
        self.assertEqual(self.bridge.ran, [])

    def test_timeout_after_unity_took_the_command(self):
        self.bridge.mode = "swallow"
        with self.assertRaises(TimeoutError) as caught:
            m.send("ping", timeout=1)
        self.assertIn("taken by Unity but not answered", str(caught.exception))
        self.assertEqual(self.leftovers("*.cancelled", "*.id"), [])

    def test_cancel_loses_to_unity_claiming_first(self):
        """Unity claims the command (renames it to .run) just before the timeout's cancel: the result must say
        Unity took it, never "cancelled", because the command still runs."""
        self.bridge.mode = "pause"
        real_replace = os.replace
        claimed = []

        def replace(src, dst):
            if str(dst).endswith(".cancelled"):
                line = self.bridge.claim(Path(src))   # Unity's rename lands first
                claimed.append(line)
                threading.Timer(0.3, self.bridge.answer, args=(Path(src).stem, line)).start()
            return real_replace(src, dst)

        with mock.patch.object(m.os, "replace", replace):
            with self.assertRaises(TimeoutError) as caught:
                m.send("propinfo Traffic Cone", timeout=1)
        self.assertIn("taken by Unity but not answered", str(caught.exception))
        self.assertNotIn("cancelled", str(caught.exception))
        self.assertEqual(claimed, ["propinfo Traffic Cone"])
        self.assertEqual(self.leftovers("*.cancelled", "*.cmd", "*.id"), [])
        time.sleep(0.6)
        self.assertEqual(self.bridge.ran, ["propinfo Traffic Cone"])   # it did run, as the message warns

    def test_answer_that_lands_while_cancelling_is_returned(self):
        self.bridge.mode = "pause"
        real_replace = os.replace

        def replace(src, dst):
            if str(dst).endswith(".cancelled"):   # Unity claims and answers just before the cancel
                self.bridge.answer(Path(src).stem, self.bridge.claim(Path(src)))
            return real_replace(src, dst)

        with mock.patch.object(m.os, "replace", replace):
            self.assertTrue(m.send("ping", timeout=1).startswith("2022.3.62f2 "))
        self.assertEqual(self.leftovers("*.cancelled", "*.cmd", "*.id"), [])

    def test_unity_cmd_is_a_wrapper_round_send(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["unity_cmd.py", "--timeout", "5", "ping"]), contextlib.redirect_stdout(out):
            self.assertEqual(unity_cmd.main(), 0)
        self.assertIn("2022.3.62f2", out.getvalue())
        self.bridge.mode = "pause"
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["unity_cmd.py", "--timeout", "1", "exportprop", "Traffic Cone"]), \
                contextlib.redirect_stderr(err):
            self.assertEqual(unity_cmd.main(), 1)
        self.assertIn("NO ANSWER", err.getvalue())
        self.assertIn("cancelled", err.getvalue())
        self.assertEqual(self.leftovers("*.cmd"), [])

    def test_unity_cmd_says_makeprop_does_not_wait(self):
        out, err = io.StringIO(), io.StringIO()
        argv = ["unity_cmd.py", "--timeout", "5", "makeprop", f"Cone|Traffic Cone|x|{m.STATIC_PROP}"]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(unity_cmd.main(), 0)
        self.assertTrue(out.getvalue().strip().endswith("cells pending"), out.getvalue())
        self.assertIn("note: the cells are not ready yet", err.getvalue())
        self.assertIn("endstar_make_prop", err.getvalue())

    # make_prop ----------------------------------------------------------------------------------

    def test_make_prop_waits_for_the_ready_marker(self):
        m.ready_marker("Traffic Cone").write_text("ready Traffic Cone 99 cells", encoding="utf-8")   # an old run's
        started = time.time()
        result = m.make_prop("Cone", "Traffic Cone", "An orange cone")
        self.assertGreater(time.time() - started, 0.5)
        self.assertIn("cells pending", result)
        self.assertTrue(result.endswith("ready Traffic Cone 12 cells"), result)
        self.assertNotIn("99 cells", result)

    def test_make_prop_grid_flag(self):
        m.make_prop("Cone", "Traffic Cone", "x", collider="each", grid=True)
        m.make_prop("Cone", "Traffic Cone", "x", collider="each")
        sent = [c for c in self.bridge.ran if c.startswith("makeprop")]
        self.assertTrue(sent[0].endswith("|each|grid"), sent[0])
        self.assertTrue(sent[1].endswith("|each"), sent[1])

    def test_make_prop_passes_a_refusal_through(self):
        for pending in ("Big Example Prop", "Traffic Cone"):   # every makeprop waits, the same prop's too
            self.bridge.pending = pending
            result = m.make_prop("Cone", "Traffic Cone", "An orange cone")
            self.assertEqual(result, f"ERROR cells for {pending} are still being generated")

    def test_make_prop_refuses_pipes(self):
        for args in (("Co|ne", "Traffic Cone", "x"), ("Cone", "Traffic|Cone", "x"), ("Cone", "Traffic Cone", "a | b")):
            with self.assertRaises(ValueError):
                m.make_prop(*args)
        self.assertEqual(list(self.inbox.iterdir()), [])
        self.assertEqual(self.bridge.ran, [])

    # export_prop ------------------------------------------------------------------------------

    def test_export_state(self):
        self.assertEqual(m.export_state(), "idle")
        self.bridge.pending = "Traffic Cone"
        self.assertEqual(m.export_state(), "cells pending for Traffic Cone")
        self.bridge.exporting, self.bridge.started = "Big Example Prop", datetime.now()
        self.assertRegex(m.export_state(), r"^exporting Big Example Prop since \d\d:\d\d:\d\d, stages 0/\?$")

    def test_export_busy_does_not_start(self):
        self.bridge.exporting, self.bridge.started = "Big Example Prop", datetime.now()
        result = m.export_prop("Traffic Cone")
        self.assertRegex(result, r"^BUSY: exporting Big Example Prop since \d\d:\d\d:\d\d, stages 0/\?$")
        self.assertEqual(self.bridge.ran, ["exportstate"])

    def test_export_busy_while_cells_are_pending(self):
        self.bridge.pending = "Traffic Cone"
        self.assertEqual(m.export_prop("Traffic Cone"), "BUSY: cells pending for Traffic Cone")
        self.assertEqual(self.bridge.ran, ["exportstate"])

    def test_export_refusal_is_not_started(self):
        self.bridge.pending = "Traffic Cone"   # a makeprop slipped in after exportstate said idle
        with mock.patch.object(m, "export_state", lambda timeout=60: "idle"):
            result = m.export_prop("Traffic Cone")
        self.assertEqual(result, "NOT STARTED: ERROR cells for Traffic Cone are still being generated")

    def test_export_old_bridge_is_not_started(self):
        with mock.patch.object(self.bridge, "handle", lambda cmd, arg: "ERROR unknown command " + cmd):
            result = m.export_prop("Traffic Cone")
        self.assertTrue(result.startswith("NOT STARTED: Unity could not report its export state"), result)
        self.assertIn("unity_restart", result)

    def test_export_uploads(self):
        result = m.export_prop("Traffic Cone")
        self.assertEqual(result, "UPLOADED Traffic Cone (version 7, 2 stages)")
        self.assertEqual(self.bridge.ran, ["exportstate", "ping", "exportprop Traffic Cone", "propinfo Traffic Cone"])
        self.assertEqual(m._log_cache[FAKE_PID], self.editor_log)
        self.assertRegex(m.export_log("Traffic Cone").read_text(encoding="utf-8"), r"lock released: uploaded\n$")

    def test_reexport_of_an_uploaded_prop_ends_at_its_last_stage(self):
        """A prop uploaded before is "local only False" from the start, so that flag says nothing, and the SDK never logs
        "Prop Asset Updated": the lock ends when the last stage is marked, and the result comes at once and not after
        the 3-minute fallback (out of date before 3 Nov: this test waited for a "Prop Asset Updated" log line)."""
        self.bridge.local_only = False
        self.bridge.upload_delay = 0.5
        started = time.time()
        with mock.patch.object(m, "UPLOAD_SECONDS", 5):   # the fallback must not be what ends it
            result = m.export_prop("Traffic Cone")
        self.assertEqual(result, "UPLOADED Traffic Cone (version 8, 2 stages)")
        self.assertLess(time.time() - started, 5)
        self.assertEqual(self.bridge.ran.count("propinfo Traffic Cone"), 1)
        self.assertNotIn("Prop Asset Updated", self.editor_log.read_text(encoding="utf-8"))
        self.assertRegex(m.export_log("Traffic Cone").read_text(encoding="utf-8"), r"lock released: uploaded\n$")

    def test_reexport_whose_update_fails_is_failed_not_uploaded(self):
        """The SDK logs "Prop Update Complete." before its error check, so that line also appears on a failure: only the
        last stage is a sign of success, and a failed update never marks it."""
        self.bridge.local_only = False
        self.bridge.export_script = "update_then_fail"
        result = m.export_prop("Traffic Cone")
        self.assertRegex(result, r"^FAILED after 1/2 stages: An error occured while exporting the prop")
        self.assertIn("Unity reports no export running.", result)
        self.assertIn("Prop Update Complete.", self.editor_log.read_text(encoding="utf-8"))
        self.assertNotIn("lock released: uploaded", m.export_log("Traffic Cone").read_text(encoding="utf-8"))

    def test_first_upload_whose_create_fails_is_failed_not_uploaded(self):
        """The SDK logs "Prop Asset Created" before its error check. Treating that line as the upload's end (as PropMaker.OnLog
        did until 3 Nov) released the lock as "uploaded" for a create that then failed, and a poll between the two lines
        read UPLOADED. The lock must never say so, and a failed first upload must not change the prop's local-only flag."""
        self.bridge.export_script = "create_then_fail"
        result = m.export_prop("Traffic Cone")
        self.assertRegex(result, r"^FAILED after 1/2 stages: An error occured while exporting the prop")
        self.assertIn("Prop Asset Created", self.editor_log.read_text(encoding="utf-8"))
        log = m.export_log("Traffic Cone").read_text(encoding="utf-8")
        self.assertNotIn("lock released: uploaded", log)
        self.assertRegex(log, r"lock released: the exporter reported a failure\n$")
        self.assertTrue(self.bridge.local_only)
        self.assertNotIn("propinfo Traffic Cone", self.bridge.ran)   # nothing to report a version for

    def test_the_lock_says_uploaded_only_from_the_last_stage(self):
        """The C# cannot run here, so its text is pinned: "uploaded" is released in exactly two places, both after every
        stage is done (the stage-done callback, and the fallback that also needs the prop to have left local-only), and
        never from a log line."""
        text = (ROOT / "unity" / "Editor" / "PropMaker.cs").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r'ReleaseExport\("uploaded"\)', text)), 2)
        self.assertIn('if (stagesTotal > 0 && stagesDone >= stagesTotal) ReleaseExport("uploaded");', text)
        check = text[text.index("static void CheckExportLock()"):text.index("static bool NoLongerLocal()")]
        self.assertLess(check.index("stagesDone < stagesTotal) return;"), check.index('ReleaseExport("uploaded")'))
        self.assertIn("NoLongerLocal()", check)
        on_log = text[text.index("public static void OnLog("):text.index("// \"frame <Prop name>")]
        self.assertNotIn('ReleaseExport("uploaded")', on_log)
        self.assertNotIn("Prop Asset Created", on_log)   # the handler does not even look for the SDK's success words

    def test_a_prop_that_fails_the_sdk_checks_is_not_started_and_names_each_failure(self):
        self.bridge.validation_error = "Invalid shader found on MeshRenderer. Shader Name: Standard [Wall01]; no collider on the Default layer"
        result = m.export_prop("Traffic Cone")
        self.assertEqual(result, "NOT STARTED: ERROR validation failed, nothing exported: Invalid shader found on MeshRenderer. "
                                 "Shader Name: Standard [Wall01]; no collider on the Default layer")
        self.assertFalse(m.export_log("Traffic Cone").exists())   # no export began
        self.assertIsNone(self.bridge.exporting)
        self.assertEqual(self.bridge.ran, ["exportstate", "ping", "exportprop Traffic Cone"])

    def test_a_skipped_sdk_check_is_reported_with_the_result(self):
        """Unity runs the export when an SDK check cannot run, and says which under "export started": that note is not lost."""
        self.bridge.export_note = "EndlessShaderValidator could not run: Object reference not set to an instance of an object"
        result = m.export_prop("Traffic Cone")
        self.assertTrue(result.startswith("UPLOADED Traffic Cone (version 7, 2 stages)"), result)
        self.assertTrue(result.endswith("note from Unity before the export: " + self.bridge.export_note), result)
        self.bridge.export_script = "fail"   # and the same when the export then fails
        failed = m.export_prop("Traffic Cone")
        self.assertTrue(failed.startswith("FAILED after 0/2 stages"), failed)
        self.assertIn(self.bridge.export_note, failed)

    def test_reexport_whose_upload_fails_is_failed(self):
        """The upload runs before the SDK marks its last stage, so an upload that crashes leaves it unmarked (1/2 here;
        out of date before 3 Nov: this test expected 2/2, with the upload after the last stage)."""
        self.bridge.local_only = False
        self.bridge.export_script = "upload_crash"
        result = m.export_prop("Traffic Cone")
        self.assertRegex(result, r"^FAILED after 1/2 stages.*FileNotFoundException")
        self.assertIn("AssetBundles\\Traffic Cone\\", result)
        self.assertIn("Unity reports no export running.", result)

    def test_reexport_never_confirmed(self):
        self.bridge.local_only = False
        self.bridge.export_script = "unconfirmed"
        result = m.export_prop("Traffic Cone")
        self.assertTrue(result.startswith("stages finished but Endstar never confirmed the upload"), result)
        self.assertIn("3 minutes after the last stage", result)
        self.assertNotIn("propinfo Traffic Cone", self.bridge.ran)

    def test_no_verdict_after_the_stages(self):
        self.bridge.export_script = "silent"
        with mock.patch.object(m, "UPLOAD_SECONDS", 0.5):
            result = m.export_prop("Traffic Cone")
        self.assertTrue(result.startswith("stages finished but Unity did not release the export lock within"), result)
        self.assertIn("Unity still reports the export as running (exporting Traffic Cone since", result)

    def test_export_lock_given_up_before_the_last_stage(self):
        self.bridge.export_script = "gave_up"
        result = m.export_prop("Traffic Cone")
        self.assertTrue(result.startswith("UNFINISHED after 0/2 stages: Unity released the export lock "
                                          "(20 minutes after the start)"), result)

    def test_export_failed_releases_the_lock(self):
        self.bridge.export_script = "fail"
        result = m.export_prop("Traffic Cone")
        self.assertTrue(result.startswith("FAILED after 0/2 stages: Cannot build asset bundles"), result)
        self.assertIn("Unity reports no export running.", result)   # the FAILED callback released it

    def test_export_crash_in_unity_log_is_only_this_props(self):
        self.bridge.export_script = "crash"
        result = m.export_prop("Traffic Cone")
        # Unity writes the exception to its log and "FAILED (Unity exception)" to the export log: either may be seen first.
        self.assertRegex(result, r"^FAILED after 0/2 stages( \(Unity log\))?: (\(Unity exception\) )?FileNotFoundException")
        self.assertIn("AssetBundles\\Traffic Cone\\", result)
        self.assertNotIn("Example Car", result)
        self.assertIn("Unity reports no export running.", result)
        log = m.export_log("Traffic Cone").read_text(encoding="utf-8")
        self.assertEqual(log.count("FAILED"), 1)   # the other prop's exception wrote nothing
        self.assertRegex(log, r"lock released: exporter exception\n$")

    def test_another_props_exception_is_ignored(self):
        self.bridge.export_script = "other_crash"
        result = m.export_prop("Traffic Cone")
        self.assertEqual(result, "UPLOADED Traffic Cone (version 7, 2 stages)")
        self.assertIn("AssetBundles\\Example Car\\", self.editor_log.read_text(encoding="utf-8"))
        log = m.export_log("Traffic Cone").read_text(encoding="utf-8")
        self.assertNotIn("FAILED", log)
        self.assertRegex(log, r"lock released: uploaded\n$")

    def test_export_timeout_says_log_is_quiet_and_unity_still_exports(self):
        self.bridge.export_script = "hang"
        result = m.export_prop("Traffic Cone", timeout_minutes=0.02)
        self.assertTrue(result.startswith("TIMED OUT after 0.02 min at 0/2 stages."), result)
        self.assertIn("has not grown since the export started", result)
        self.assertIn("Unity still reports the export as running", result)

    # setcells and navtest -----------------------------------------------------------------------

    def test_set_cells_writes_the_list_and_sends_the_command(self):
        cells = [(1, 1, 0), (0, 0, 0), (1, 0, 0), (-2, 0, 3)]
        answer = m.set_cells("Cells Test Prop", cells)
        self.assertEqual(answer, "cells set Cells Test Prop 4 cells (was 12) from Cells_Test_Prop.txt, x offset False, z offset False")
        self.assertEqual(self.bridge.ran, ["setcells Cells Test Prop|Cells_Test_Prop.txt"])
        text = (m.CELLS_FOLDER / "Cells_Test_Prop.txt").read_text(encoding="utf-8")
        self.assertEqual(text.splitlines(), ["# 4 claimed cells for Cells Test Prop: x y z, one per line",
                                             "-2 0 3", "0 0 0", "1 0 0", "1 1 0"])   # sorted, one per line, noted
        self.assertEqual(sorted(self.bridge.cells["Cells Test Prop"]), sorted(cells))
        self.assertEqual(sorted(p.name for p in m.CELLS_FOLDER.iterdir()), ["Cells_Test_Prop.txt"])   # no .tmp left over

    def test_set_cells_refuses_what_the_bridge_would_refuse_before_writing_anything(self):
        wide = [(0, 0, 0), (m.MAX_SPAN, 0, 0)]
        bad = {
            "none": [], "twice": [(0, 0, 0), (0, 0, 0)], "below the ground": [(0, -1, 0)], "not whole": [(0, 0.5, 0)],
            "two numbers": [(0, 0)], "a flag": [(0, True, 0)], "wider than a level": wide,
        }
        for label, cells in bad.items():
            with self.subTest(label), self.assertRaises(ValueError):
                m.set_cells("Traffic Cone", cells)
        with mock.patch.object(m, "MAX_CELLS", 3), self.assertRaises(ValueError):
            m.set_cells("Traffic Cone", [(i, 0, 0) for i in range(4)])
        with self.assertRaises(ValueError):
            m.set_cells("Traffic|Cone", [(0, 0, 0)])
        m.set_cells("Traffic Cone", [(0, 0, 0), (m.MAX_SPAN - 1, 0, 0)])   # exactly a level wide is fine
        self.assertEqual(len(self.bridge.ran), 1)   # only the good one reached Unity
        self.assertEqual([p.name for p in m.CELLS_FOLDER.iterdir()], ["Traffic_Cone.txt"])

    def test_the_bridge_answer_for_a_bad_file_is_passed_through(self):
        m.CELLS_FOLDER.mkdir()
        (m.CELLS_FOLDER / "Bad.txt").write_text("0 0 0" + NL + "abc 1 2" + NL, encoding="utf-8")
        self.assertEqual(m.send("setcells Traffic Cone|Bad.txt", timeout=5),
                         "ERROR Bad.txt: line 2: want three whole numbers x y z, got 'abc 1 2'")
        self.assertTrue(m.send("setcells Traffic Cone|../Bad.txt", timeout=5).startswith("ERROR no cells file"))

    def test_parse_cells_reads_what_the_writer_writes(self):
        text = NL.join(["# a note", "0 0 0   # here", "", "1, 2, 3", "-4 5 +6"])
        self.assertEqual(m.parse_cells(text), [(0, 0, 0), (1, 2, 3), (-4, 5, 6)])
        for text in ("1 2", "1 2 3 4", "a b c", "1.5 2 3"):
            with self.subTest(text), self.assertRaisesRegex(ValueError, r"^line 1: want three whole numbers x y z"):
                m.parse_cells(text)
        with self.assertRaisesRegex(ValueError, r"^line 3:"):
            m.parse_cells(NL.join(["0 0 0", "1 1 1", "x"]))
        cells = [(3, 0, -1), (0, 2, 2)]
        m.write_cells_file("A B", cells)
        self.assertEqual(sorted(m.parse_cells((m.CELLS_FOLDER / "A_B.txt").read_text(encoding="utf-8"))), sorted(cells))

    def test_nav_test_sends_the_points_and_says_it_is_a_screen(self):
        answer = m.nav_test("Stair Prop", ["0,-0.5,0", (1, 0.25, 3), "2.5,1,3"])
        self.assertEqual(self.bridge.navtests, [("Stair Prop", ["0.0000,-0.5000,0.0000", "1.0000,0.2500,3.0000", "2.5000,1.0000,3.0000"])])
        self.assertIn("a SCREEN, NOT PROOF", answer)

    def test_nav_test_refuses_a_bad_walk_before_asking_unity(self):
        for name, points in (("Cone", ["0,0,0"]), ("Cone", []), ("Cone", ["0,0", "1,1,1"]), ("Cone", ["a,b,c", "1,1,1"]),
                             ("Co|ne", ["0,0,0", "1,1,1"])):
            with self.subTest(points=points), self.assertRaises(ValueError):
                m.nav_test(name, points)
        self.assertEqual(self.bridge.ran, [])

    # Unity's log --------------------------------------------------------------------------------

    def test_log_path_comes_from_ping_and_is_cached(self):
        self.assertEqual(m.unity_log_path(), self.editor_log)
        self.assertEqual(m.unity_log_path(), self.editor_log)
        self.assertEqual(self.bridge.ran.count("ping"), 1)
        self.assertTrue(m.editor_log(5).startswith(f"(log: {self.editor_log}, last written"))
        self.assertIn("Unity started", m.editor_log(5))

    def test_log_path_fallbacks(self):
        self.bridge.ping_log = False   # an old bridge: ping has no "log="
        with mock.patch.object(m, "log_from_command_line", lambda pid: Path(r"C:\x\from-command-line.log")):
            self.assertEqual(m.unity_log_path(), Path(r"C:\x\from-command-line.log"))
        m._log_cache.clear()
        m.EDITOR_LOG.write_text("old", encoding="utf-8")
        m.DEFAULT_LOG.write_text("new", encoding="utf-8")
        os.utime(m.EDITOR_LOG, (time.time() - 600, time.time() - 600))
        self.assertEqual(m.unity_log_path(), m.DEFAULT_LOG)   # nothing better known: the newer file
        with mock.patch.object(m, "unity_pid", lambda: None):
            os.utime(m.DEFAULT_LOG, (time.time() - 900, time.time() - 900))
            self.assertEqual(m.unity_log_path(), m.EDITOR_LOG)

    def test_hub_launch_is_cached_so_an_old_bridge_is_pinged_once(self):
        self.bridge.ping_log = False
        with mock.patch.object(m, "log_from_command_line", lambda pid: m.DEFAULT_LOG):   # no -logFile
            self.assertEqual(m.unity_log_path(), m.DEFAULT_LOG)
            self.assertEqual(m.unity_log_path(), m.DEFAULT_LOG)
        self.assertEqual(self.bridge.ran.count("ping"), 1)
        self.assertEqual(m._log_cache, {FAKE_PID: m.DEFAULT_LOG})

    def test_log_path_when_the_ping_times_out(self):
        self.bridge.mode = "pause"   # Unity asleep: the ping inside unity_log_path times out
        from_command_line = Path(r"C:\x\from-command-line.log")
        with mock.patch.object(m, "PING_SECONDS", 1):
            with mock.patch.object(m, "log_from_command_line", lambda pid: from_command_line):
                self.assertEqual(m.unity_log_path(), from_command_line)
            self.assertEqual(m._log_cache, {FAKE_PID: from_command_line})
            m._log_cache.clear()
            m.DEFAULT_LOG.write_text("new", encoding="utf-8")
            self.assertEqual(m.unity_log_path(), m.DEFAULT_LOG)   # command line unreadable too: the newer file
            self.assertEqual(m._log_cache, {})   # a guess by modified time is never cached
        self.assertEqual(len(self.leftovers("*.cancelled")), 2)   # both pings were cancelled
        self.assertEqual(self.leftovers("*.cmd", "*.id"), [])
        self.bridge.mode = "answer"
        time.sleep(0.3)
        self.assertEqual(self.bridge.ran, [])

    def test_status_pings_once(self):
        with mock.patch.object(m, "unity_window", lambda: 0xBEEF), mock.patch.object(m, "window_pid", lambda hwnd: FAKE_PID):
            self.bridge.ping_log = False   # an old bridge: no "log=" in its answer
            old = m.status()
            self.assertEqual(self.bridge.ran.count("ping"), 1)
            self.assertIn(f"editor log: {m.EDITOR_LOG}", old)
            self.bridge.ping_log = True
            new = m.status()
        self.assertEqual(self.bridge.ran.count("ping"), 2)
        self.assertIn("ping: 2022.3.62f2 ", new)
        self.assertIn(f"editor log: {self.editor_log}", new)


class ParsingTests(unittest.TestCase):
    def test_log_field(self):
        self.assertEqual(m.log_field("2022.3.62f2 C:/p/Assets log=C:/Users/x/AppData/Local/Unity/Editor/Editor.log\n"),
                         Path("C:/Users/x/AppData/Local/Unity/Editor/Editor.log"))
        self.assertEqual(m.log_field("2026-10-27T18:34:27 compiling=False updating=False log=C:\\a b\\editor.log"),
                         Path("C:\\a b\\editor.log"))
        self.assertIsNone(m.log_field("2022.3.62f2 C:/p/Assets"))

    def test_log_file_arg(self):
        self.assertEqual(m.log_file_arg(f"{EXE} -projectPath C:\\p -logFile C:\\endstar-unity-mcp\\unity\\editor.log"),
                         Path(r"C:\endstar-unity-mcp\unity\editor.log"))
        self.assertEqual(m.log_file_arg(f'{EXE} -logfile "C:\\a b\\x.log" -projectPath C:\\p'), Path(r"C:\a b\x.log"))
        self.assertIsNone(m.log_file_arg(f"{EXE} -projectPath C:\\p"))
        self.assertIsNone(m.log_file_arg(f"{EXE} -logFile - -projectPath C:\\p"))
        self.assertIsNone(m.log_file_arg(""))

    def test_log_from_command_line(self):
        cases = [
            (fake_subprocess(f"{EXE} -projectPath C:\\p -logFile C:\\x\\editor.log\r\n"), Path(r"C:\x\editor.log")),
            (fake_subprocess(f"{EXE} -projectPath C:\\p\r\n"), m.DEFAULT_LOG),   # a Hub launch: the default log
            (fake_subprocess(f"{EXE} -logFile - -projectPath C:\\p"), None),   # standard output: no file
            (fake_subprocess(""), None),   # gone, or not ours to read
            (fake_subprocess("x", code=1), None),
            (fake_subprocess(error=OSError("no powershell")), None),
        ]
        for sub, expected in cases:
            with self.subTest(expected=expected), mock.patch.object(m, "subprocess", sub):
                self.assertEqual(m.log_from_command_line(FAKE_PID), expected)

    def test_exporter_exception_skips_other_props(self):
        text = "noise\n" + crash_text("Example Car") + "more noise\n"
        self.assertIsNone(m.exporter_exception(text, "Traffic Cone"))
        self.assertIn("Example Car", m.exporter_exception(text))
        self.assertIn("Traffic Cone", m.exporter_exception(text + crash_text("Traffic Cone"), "Traffic Cone"))
        self.assertIn("traffic cone", m.exporter_exception(crash_text("traffic cone"), "Traffic Cone"))   # any case
        nameless = "NullReferenceException: Object reference not set\n  at EndlessEditor.PropSDK.PropExporterV2.Export ()\n"
        self.assertTrue(m.exporter_exception(nameless, "Traffic Cone").startswith("NullReferenceException"))

    def test_pids_on_project(self):
        project = Path(r"C:\Endstar-SDK")
        rows = [
            (15736, f"{EXE} -projectPath C:\\Endstar-SDK -logFile C:\\x\\editor.log"),
            (44216, f'{EXE} "-batchMode" "-name" "AssetImportWorker0" "-projectPath" '
                    '"C:/Endstar-SDK" "-logFile" "Logs/AssetImportWorker0.log"'),
            (3416, '"C:\\Program Files\\Unity Hub\\resources\\unity.exe" serve'),
            (7000, f"{EXE} -projectPath C:\\Endstar-SDK-old"),   # another project
            (7001, f"{EXE} -projectPath C:\\Endstar-SDK\\"),
            (7002, None),   # a command line we may not read
        ]
        self.assertEqual(m.pids_on_project(rows, project), [7001, 15736, 44216])

    def test_project_unity_pids_reads_the_process_list(self):
        line = f"{EXE} -projectPath {m.SDK_PROJECT}"
        one = json.dumps({"ProcessId": 15736, "CommandLine": line})
        many = json.dumps([{"ProcessId": 1, "CommandLine": line}, {"ProcessId": 2, "CommandLine": "unity.exe serve"}])
        for stdout, expected in ((one, [15736]), (many, [1]), ("", [])):
            with self.subTest(stdout=stdout), mock.patch.object(m, "subprocess", fake_subprocess(stdout)):
                self.assertEqual(m.project_unity_pids(), expected)
        for sub in (fake_subprocess(code=1), fake_subprocess("not json"), fake_subprocess(error=OSError("x"))):
            with mock.patch.object(m, "subprocess", sub), self.assertRaises(OSError):
                m.project_unity_pids()


class Win32Tests(unittest.TestCase):
    """Read-only Win32 calls: they list windows and wait on our own child processes, nothing more."""

    def test_enum_windows_and_pid_with_64_bit_handles(self):
        with mock.patch.object(m, "WINDOW_PREFIX", ""):   # any top-level window will do
            hwnd = m.unity_window()
        self.assertIsInstance(hwnd, int)
        self.assertIsInstance(m.window_pid(hwnd), int)

    def test_wait_for_exit(self):
        quick = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
        slow = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            for proc, seconds, expected in ((quick, 10, True), (slow, 1, False)):
                handle = m.kernel32.OpenProcess(m.SYNCHRONIZE, False, proc.pid)
                self.assertTrue(handle)
                try:
                    self.assertIs(m.wait_for_exit(handle, seconds), expected)
                finally:
                    m.kernel32.CloseHandle(handle)
        finally:
            slow.kill()
            slow.wait()
            quick.wait()


class RestartTests(unittest.TestCase):
    """restart_unity with every outside effect faked: no window message, no process, no SDK project."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="endstar-restart-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "outbox").mkdir()
        (self.tmp / "sdk" / "Assets" / "Editor" / "SendToEndless").mkdir(parents=True)
        self.calls = []
        self.launches = []
        test = self

        class FakePopen:
            pid = 5151

            def __init__(self, args):
                test.launches.append(args)
                log = Path(args[args.index("-logFile") + 1])

                def start_unity():   # Unity logs a compile error, then the bridge writes ready.txt
                    time.sleep(0.3)
                    log.write_text("Assets/Editor/X.cs(1,1): error CS0103: nope\n", encoding="utf-8")
                    (test.tmp / "outbox" / "ready.txt").write_text("2022.3.62f2 now", encoding="utf-8")

                threading.Thread(target=start_unity, daemon=True).start()

        user32 = types.SimpleNamespace(PostMessageW=lambda *a: self.calls.append(("PostMessageW",) + a) or 1)
        kernel32 = types.SimpleNamespace(OpenProcess=lambda *a: self.calls.append(("OpenProcess",) + a) or 77,
                                         CloseHandle=lambda h: self.calls.append(("CloseHandle", h)) or 1,
                                         WaitForSingleObject=lambda h, ms: 0)
        fast_time = types.SimpleNamespace(time=time.time, sleep=lambda s: time.sleep(min(s, 0.01)),
                                          strftime=time.strftime, localtime=time.localtime)
        fakes = {
            "INBOX": self.tmp / "inbox", "OUTBOX": self.tmp / "outbox", "EDITOR_LOG": self.tmp / "editor.log",
            "SDK_PROJECT": self.tmp / "sdk", "UNITY_EXE": self.tmp / "no-unity.exe", "user32": user32,
            "kernel32": kernel32, "subprocess": types.SimpleNamespace(Popen=FakePopen), "time": fast_time,
            "unity_window": lambda: 0xBEEF, "window_pid": lambda hwnd: FAKE_PID, "_log_cache": {},
            # Unity answers "idle", and no Unity.exe runs on the project once the window has closed.
            "export_state": lambda timeout=60: "idle", "project_unity_pids": lambda: [],
            "unity_dialogs": lambda: [],   # no box waits when the new Unity is up (11 Nov: the restart closes the SDK's notice)
        }
        for name, value in fakes.items():
            patcher = mock.patch.object(m, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertNotEqual(m.SDK_PROJECT, REAL_SDK)
        self.assertNotEqual(m.INBOX, REAL_INBOX)

    def test_restart_waits_for_the_process_and_survives_a_locked_log(self):
        m.EDITOR_LOG.write_text("old session", encoding="utf-8")
        old_ready = m.OUTBOX / "ready.txt"
        old_ready.write_text("old", encoding="utf-8")
        os.utime(old_ready, (time.time() - 600, time.time() - 600))   # must not pass for this start
        with open(m.EDITOR_LOG, "rb"):   # the old Unity still holds its log: the rename fails
            result = m.restart_unity(wait_minutes=1)
        self.assertTrue(result.startswith("Unity restarted; bridge ready; compile errors 1;"), result)
        self.assertIn("note: editor.log could not be moved", result)
        self.assertEqual([c[0] for c in self.calls], ["OpenProcess", "PostMessageW", "CloseHandle"])
        self.assertEqual(self.calls[0][1:], (m.SYNCHRONIZE, False, FAKE_PID))
        self.assertEqual(self.calls[1][1:], (0xBEEF, m.WM_CLOSE, 0, 0))
        launched_log = Path(self.launches[0][self.launches[0].index("-logFile") + 1])
        self.assertNotEqual(launched_log, m.EDITOR_LOG)
        self.assertEqual(m._log_cache[5151], launched_log)
        installed = m.SDK_PROJECT / "Assets" / "Editor" / "SendToEndless"
        # PropMaker is one class in several files: all of them go, or Unity cannot compile the rest.
        self.assertEqual(sorted(p.name for p in installed.glob("*.cs")), m.bridge_files())
        self.assertIn("PropMaker.cs", m.bridge_files())
        self.assertEqual((installed / "bridge-root.txt").read_text(encoding="utf-8"), str(m.ROOT))   # where the inbox is

    def test_install_stops_when_an_older_copy_sits_in_another_folder(self):
        old = m.SDK_PROJECT / "Assets" / "Editor" / "OlderBridge"
        old.mkdir(parents=True)
        (old / "bridge-root.txt").write_text("C:/old", encoding="utf-8")
        problems = m.install_bridge()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(str(old), problems[0])
        self.assertIn("delete that folder", problems[0])
        target = m.SDK_PROJECT / "Assets" / "Editor" / "SendToEndless"
        self.assertEqual(list(target.glob("*.cs")), [])   # nothing copied beside the old copy
        self.assertTrue((old / "bridge-root.txt").exists())   # and the old copy is left alone

    def test_restart_refuses_while_unity_is_busy(self):
        for state in (STATE_BUSY, "cells pending for Traffic Cone"):
            with self.subTest(state=state), mock.patch.object(m, "export_state", lambda timeout=60, s=state: s):
                self.assertEqual(m.restart_unity(), f"BUSY: {state}; not restarting")
        self.assertEqual(self.calls, [])   # no WM_CLOSE, no process handle
        self.assertEqual(self.launches, [])

    def test_restart_goes_ahead_when_unity_cannot_say(self):
        def no_answer(timeout=60):
            raise TimeoutError("Unity did not answer 'exportstate' within 20 s")

        for asked in (lambda timeout=60: "ERROR unknown command exportstate", no_answer):   # an old bridge, asleep
            (m.OUTBOX / "ready.txt").unlink(missing_ok=True)
            with self.subTest(asked=asked), mock.patch.object(m, "export_state", asked):
                result = m.restart_unity(wait_minutes=1)
                self.assertTrue(result.startswith("Unity restarted; bridge ready;"), result)
        self.assertEqual(len(self.launches), 2)

    def test_restart_reports_a_dialog_and_does_not_start_a_second_unity(self):
        boxes = [(1, "Save Scene", ["Do you want to save the changes?"], {"Save": 2, "Don't Save": 3})]
        with mock.patch.object(m, "wait_for_exit", lambda handle, seconds: False), \
                mock.patch.object(m, "unity_dialogs", lambda: boxes):
            result = m.restart_unity()
        self.assertEqual(result, "Unity did not close within 60 s; it is asking: Save Scene: Do you want to save the changes?")
        self.assertEqual(self.launches, [])

    def test_restart_waits_for_a_lingering_process(self):
        windows = iter([0xBEEF, None])
        with mock.patch.object(m, "wait_for_exit", lambda handle, seconds: False), \
                mock.patch.object(m, "unity_window", lambda: next(windows)):
            result = m.restart_unity()
        self.assertIn(f"process ({FAKE_PID}) is still running", result)
        # The next call finds no window, but the process still runs on the project: still no second Unity.
        with mock.patch.object(m, "unity_window", lambda: None), \
                mock.patch.object(m, "project_unity_pids", lambda: [FAKE_PID]):
            again = m.restart_unity()
        self.assertEqual(again, f"Unity.exe still runs on the SDK project without a window (process {FAKE_PID}); "
                                "not starting a second one")
        self.assertEqual(self.launches, [])

    def test_restart_refuses_a_unity_without_a_window_on_the_project(self):
        asked = []
        with mock.patch.object(m, "unity_window", lambda: None), \
                mock.patch.object(m, "project_unity_pids", lambda: [9999, 9998]), \
                mock.patch.object(m, "export_state", lambda timeout=60: asked.append(timeout) or "idle"):
            result = m.restart_unity()
        self.assertEqual(result, "Unity.exe still runs on the SDK project without a window (process 9999, 9998); "
                                 "not starting a second one")
        self.assertEqual((self.calls, self.launches, asked), ([], [], []))   # no window: nothing closed or asked

    def test_restart_after_an_import_worker_ends(self):
        pids = iter([[44216], [44216], []])   # trails the main window by two checks
        with mock.patch.object(m, "project_unity_pids", lambda: next(pids)):
            result = m.restart_unity(wait_minutes=1)
        self.assertTrue(result.startswith("Unity restarted; bridge ready;"), result)
        self.assertEqual(len(self.launches), 1)

    def test_restart_refuses_when_the_process_list_cannot_be_read(self):
        def unreadable():
            raise OSError("the process list could not be read: access denied")

        with mock.patch.object(m, "unity_window", lambda: None), mock.patch.object(m, "project_unity_pids", unreadable):
            result = m.restart_unity()
        self.assertTrue(result.startswith("Could not check whether Unity still runs on the SDK project"), result)
        self.assertEqual(self.launches, [])


class ServerTests(unittest.TestCase):
    def test_tools_listed(self):
        try:
            server = m.build_server()
        except ImportError:
            self.skipTest("mcp is not installed (run with uv as the docstring says)")
        names = {t.name for t in asyncio.run(server.list_tools())}
        self.assertTrue({"unity_status", "endstar_make_prop", "endstar_export_prop", "unity_export_state",
                         "unity_dialogs", "unity_restart", "unity_editor_log", "endstar_set_cells", "endstar_nav_test"} <= names)

    def test_set_cells_tool_reads_only_files_inside_the_project(self):
        try:
            server = m.build_server()
        except ImportError:
            self.skipTest("mcp is not installed (run with uv as the docstring says)")
        with tempfile.TemporaryDirectory(prefix="endstar-cells-") as tmp:
            outside = Path(tmp) / "cells.txt"
            outside.write_text("0 0 0", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "must be inside"):
                asyncio.run(server.call_tool("endstar_set_cells", {"prop_name": "Traffic Cone", "cells_file": str(outside)}))

    def test_new_tools_say_what_they_are_not(self):
        try:
            server = m.build_server()
        except ImportError:
            self.skipTest("mcp is not installed (run with uv as the docstring says)")
        tools = {t.name: t.description for t in asyncio.run(server.list_tools())}
        self.assertIn("SCREEN, not proof", tools["endstar_nav_test"])
        self.assertIn("CALIBRATE", tools["endstar_nav_test"])
        self.assertIn("footprint", tools["endstar_set_cells"])   # the footprint rule for a placed piece


HOST_CS = """
// Runs the helper functions of PropMaker that need no editor, from the compiled editor scripts.
using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using UnityEngine;

static class Host
{
    static Type T;
    static int fails;

    static object Call(string name, params object[] args)
    {
        var m = T.GetMethod(name, BindingFlags.NonPublic | BindingFlags.Public | BindingFlags.Static);
        if (m == null) throw new Exception("no method " + name);
        return m.Invoke(null, args);
    }

    static void Check(string what, object got, object want)
    {
        var ok = Equals(got, want);
        Console.WriteLine((ok ? "ok   " : "FAIL ") + what + (ok ? "" : ": got '" + got + "', wanted '" + want + "'"));
        if (!ok) fails++;
    }

    static string Parse(out List<Vector3Int> cells, params string[] lines)
    {
        cells = new List<Vector3Int>();
        return (string)Call("ParseCells", (IEnumerable<string>)lines, cells);
    }

    static int Main(string[] args)
    {
        // Unity's libraries are only read, from where Unity keeps them. Set up before any method that uses them is compiled.
        AppDomain.CurrentDomain.AssemblyResolve += (sender, e) =>
        {
            var file = Path.Combine(args[1], new AssemblyName(e.Name).Name + ".dll");
            return File.Exists(file) ? Assembly.LoadFrom(file) : null;
        };
        return Run(args[0]);
    }

    [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.NoInlining)]
    static int Run(string bridge)
    {
        T = Assembly.LoadFrom(bridge).GetType("PropMaker");
        List<Vector3Int> c;
        Check("good list", Parse(out c, "# note", "0 0 0", "1,2,3  # more", "", "-4 5 +6"), null);
        Check("good list count", c.Count, 3);
        Check("good list order", c[2].ToString(), "(-4, 5, 6)");
        Check("two numbers", Parse(out c, "0 0"), "line 1: want three whole numbers x y z, got '0 0'");
        Check("a word", Parse(out c, "0 0 0", "a 1 2"), "line 2: want three whole numbers x y z, got 'a 1 2'");
        Check("a decimal", Parse(out c, "1.5 2 3"), "line 1: want three whole numbers x y z, got '1.5 2 3'");
        Check("below ground", Parse(out c, "0 -1 0"), "line 1: y is below the ground cell: 0 -1 0");
        Check("twice", Parse(out c, "0 0 0", "1 0 0", "0 0 0"), "line 3: the cell 0 0 0 is listed twice");
        Check("empty", Parse(out c, "# nothing"), "no cells in the file");
        Check("exactly 101 wide", Parse(out c, "0 0 0", "100 0 0"), null);
        Check("102 wide", Parse(out c, "0 0 0", "101 0 0"), "the cells span 102 x 1 x 1, more than the 101 cells a level fits on one axis");

        var a = (Guid)Call("StableGuid", "Spinner Blades", "Model/BladeA");
        Check("stable id repeats", a, (Guid)Call("StableGuid", "Spinner Blades", "Model/BladeA"));
        Check("stable id differs by path", a != (Guid)Call("StableGuid", "Spinner Blades", "Model/BladeB"), true);
        Check("stable id differs by prop", a != (Guid)Call("StableGuid", "Other", "Model/BladeA"), true);
        Check("stable id is not empty", a != Guid.Empty, true);

        // The nav screen's claimed-cell rule: the floor of the bottom cell is y = -0.5; a standing point counts for the cell it is in.
        var cells = new[] { new Vector3(0, 0, 0), new Vector3(1, 0, 0), new Vector3(1, 1, 0) };
        Check("standing in a claimed cell", Call("NearClaimedCell", new Vector3(0, -0.5f, 0), cells), true);
        Check("standing above a claimed cell", Call("NearClaimedCell", new Vector3(0, 0.6f, 0), cells), true);
        Check("two cells above is too far", Call("NearClaimedCell", new Vector3(0, 1.6f, 0), cells), false);
        Check("beside the claimed cells", Call("NearClaimedCell", new Vector3(3, -0.5f, 0), cells), false);
        Check("standing below a claimed cell", Call("NearClaimedCell", new Vector3(1, -1.4f, 0), cells), true);

        var pts = new List<Vector3>();
        Check("points parse", Call("ParsePoints", new[] { "x", "0,-0.5,0", "1.5,0.25,-3" }, pts), null);
        Check("points count", pts.Count, 2);
        Check("second point", pts[1].x + " " + pts[1].y + " " + pts[1].z, "1.5 0.25 -3");
        Check("a bad point", Call("ParsePoints", new[] { "x", "0,0" }, new List<Vector3>()), "point 1: want x,y,z in metres, got '0,0'");
        Console.WriteLine(fails == 0 ? "ALL OK" : fails + " FAILED");
        return fails;
    }
}
"""


class EditorScriptTests(unittest.TestCase):
    """The Unity scripts cannot run here, but their text can be checked, and Unity's own C# compiler can be run on them
    (it reads Unity's install folder and writes to a temporary one; it never starts Unity)."""

    editor = ROOT / "unity" / "Editor"

    def test_every_script_is_installed_together(self):
        """PropMaker is one class in several files. A file left out of the install would not compile in Unity."""
        names = sorted(p.name for p in self.editor.glob("*.cs"))
        self.assertEqual(m.bridge_files(), names)
        for path in self.editor.glob("*.cs"):
            text = path.read_text(encoding="utf-8")
            if re.search(r"^(static )?(partial )?class PropMaker\b", text, re.M):
                self.assertRegex(text, r"(?m)^static partial class PropMaker\b", f"{path.name} must declare PropMaker partial")

    def test_every_command_the_tools_send_is_in_the_bridge(self):
        sent = set(re.findall(r'send\(f?"(\w+)', (ROOT / "tools" / "endstar_unity_mcp.py").read_text(encoding="utf-8")))
        known = set(re.findall(r'case "(\w+)":', (self.editor / "EndstarBridge.cs").read_text(encoding="utf-8")))
        self.assertTrue({"setcells", "navtest", "makeprop", "exportprop"} <= sent)
        self.assertEqual(sent - known, set())

    def test_the_cells_folder_matches_the_bridge(self):
        """Python writes the lists where the bridge reads them."""
        bridge = (self.editor / "EndstarBridge.cs").read_text(encoding="utf-8")
        self.assertIn('Path.Combine(Root, "unity", "cells")', bridge)
        self.assertEqual(m.CELLS_FOLDER, m.ROOT / "unity" / "cells")

    def test_nothing_changes_a_prop_while_it_is_being_exported_or_its_cells_are_made(self):
        """The C# cannot run here, so its guards are pinned by text: a prop being exported cannot be built again or have its
        cells set, and the nav screen (which reads the cells) waits for both an export and a cells run."""
        def body(file, start, end):
            text = (self.editor / file).read_text(encoding="utf-8")
            return text[text.index(start):text.index(end, text.index(start))]

        make = body("PropMaker.cs", "public static string Make(", "static string ReadyPath(")
        self.assertIn("if (exporting == name) return", make)
        self.assertLess(make.index("CheckExportLock();"), make.index("if (exporting == name)"))   # an old lock is dropped first
        cells = body("PropCells.cs", "public static string SetCells(", "var profile = FindProfile(name);")
        self.assertIn("if (pendingCells != null) return", cells)
        self.assertIn("if (exporting == name) return", cells)
        nav = body("PropNav.cs", "public static string NavTest(", "var profile = FindProfile(")
        self.assertIn("if (pendingCells != null) return", nav)
        self.assertIn("if (exporting != null) return", nav)

    def csharp(self):
        """(dotnet, csc, the -r: options, Unity's library folder) of Unity 2022.3's own compiler, or a skipped test on a PC without it."""
        data = m.UNITY_EXE.parent / "Data"
        dotnet, csc = data / "NetCoreRuntime" / "dotnet.exe", data / "DotNetSdkRoslyn" / "csc.dll"
        api = data / "UnityReferenceAssemblies" / "unity-4.8-api"
        if "2022.3" not in str(m.UNITY_EXE) or not (dotnet.exists() and csc.exists() and api.exists()):
            self.skipTest("Unity 2022.3's own compiler is not on this PC")
        refs = [f"-r:{dll}" for dll in sorted((data / "Managed" / "UnityEngine").glob("*.dll"))]
        refs += [f"-r:{api / dll}" for dll in ("mscorlib.dll", "System.dll", "System.Core.dll", "System.Xml.dll", "System.Data.dll")]
        refs.append(f"-r:{api / 'Facades' / 'netstandard.dll'}")
        return dotnet, csc, refs, data / "Managed" / "UnityEngine"

    def compile(self, target, out, sources, tools):
        dotnet, csc, refs, _ = tools
        return subprocess.run([str(dotnet), str(csc), "-nologo", "-nostdlib+", "-noconfig", f"-target:{target}", "-langversion:9.0",
                               "-define:UNITY_EDITOR", f"-out:{out}", *refs, *map(str, sources)],
                              capture_output=True, text=True, errors="replace", timeout=300, creationflags=subprocess.CREATE_NO_WINDOW)

    def test_every_reflected_constructor_gets_its_arguments_in_a_list(self):
        """A Unity object turns into a bool by itself, so CreateInstance(type, prefab) quietly calls the empty constructor"""
        for path in sorted(self.editor.glob("*.cs")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if "Activator.CreateInstance(" in line and "," in line.split("Activator.CreateInstance(", 1)[1]:
                    self.assertIn("new object[]", line, f"{path.name}: {line.strip()}")

    def test_the_scripts_compile(self):
        tools = self.csharp()
        with tempfile.TemporaryDirectory(prefix="endstar-csc-") as out:
            run = self.compile("library", Path(out) / "bridge.dll", sorted(self.editor.glob("*.cs")), tools)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn("error CS", run.stdout + run.stderr)

    def test_the_pure_functions_run(self):
        """Runs PropMaker's helpers that need no editor (the cell-list reader, the stable mesh id, the claimed-cell rule of
        the nav screen, the point reader) on the .NET that ships with Unity, with Unity's libraries only read."""
        tools = self.csharp()
        with tempfile.TemporaryDirectory(prefix="endstar-csc-") as out:
            out = Path(out)
            built = self.compile("library", out / "bridge.dll", sorted(self.editor.glob("*.cs")), tools)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            (out / "Host.cs").write_text(HOST_CS, encoding="utf-8")
            host = self.compile("exe", out / "Host.exe", [out / "Host.cs"], tools)
            self.assertEqual(host.returncode, 0, host.stdout + host.stderr)
            (out / "Host.runtimeconfig.json").write_text(json.dumps({"runtimeOptions": {
                "tfm": "net6.0", "framework": {"name": "Microsoft.NETCore.App", "version": "6.0.0"}, "rollForwardOnNoCandidateFx": 2}}))
            run = subprocess.run([str(tools[0]), "exec", "--runtimeconfig", str(out / "Host.runtimeconfig.json"), str(out / "Host.exe"),
                                  str(out / "bridge.dll"), str(tools[3])], capture_output=True, text=True, errors="replace",
                                 timeout=120, creationflags=subprocess.CREATE_NO_WINDOW, cwd=out)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("ALL OK", run.stdout)
        self.assertNotIn("FAIL", run.stdout)


class PathsFileTest(unittest.TestCase):
    """paths.json lets someone on another PC set the folders without environment variables; an environment variable wins."""

    def test_paths_file_then_environment_then_default(self):
        with tempfile.TemporaryDirectory(prefix="endstar-paths-") as tmp:
            f = Path(tmp) / "paths.json"
            self.assertEqual(m.read_paths_file(f), {})
            f.write_text("not json", encoding="utf-8")
            self.assertEqual(m.read_paths_file(f), {})
            f.write_text(json.dumps({"BLENDER_EXE": r"D:\Blender\blender.exe", "UNITY_EXE": "  ", "X": 3}), encoding="utf-8")
            paths = m.read_paths_file(f)
            self.assertEqual(paths, {"BLENDER_EXE": r"D:\Blender\blender.exe"})
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("BLENDER_EXE", None)
                os.environ.pop("UNITY_EXE", None)
                self.assertEqual(m.setting("BLENDER_EXE", "default", paths), r"D:\Blender\blender.exe")
                self.assertEqual(m.setting("UNITY_EXE", "default", paths), "default")
            with mock.patch.dict(os.environ, {"BLENDER_EXE": "E:\b.exe"}):
                self.assertEqual(m.setting("BLENDER_EXE", "default", paths), "E:\b.exe")

    def test_one_backslash_or_two_both_read_as_typed(self):
        with tempfile.TemporaryDirectory(prefix="endstar-paths-") as tmp:
            f = Path(tmp) / "paths.json"
            single = '{\n  "ENDSTAR_SDK_PROJECT": "C:' + "\\" + 'Endstar-SDK",\n  "BLENDER_EXE": "D:' + "\\" + "tools" + "\\" \
                + "blender" + "\\" + "new" + "\\" + 'b.exe"\n}\n'
            f.write_text(single, encoding="utf-8")
            self.assertEqual(m.read_paths_file(f), {"ENDSTAR_SDK_PROJECT": r"C:\Endstar-SDK",
                                                    "BLENDER_EXE": r"D:\tools\blender\new\b.exe"})   # no tab, newline or backspace
            self.assertEqual(m.PATHS_PROBLEM, "")
            f.write_text(json.dumps({"ENDSTAR_SDK_PROJECT": r"E:\Games\Endstar-SDK"}), encoding="utf-8")   # two backslashes
            self.assertIn(r"E:\\Games", f.read_text(encoding="utf-8"))
            self.assertEqual(m.read_paths_file(f), {"ENDSTAR_SDK_PROJECT": r"E:\Games\Endstar-SDK"})
            f.write_text("\ufeff" + json.dumps({"UNITY_EXE": r"C:\U\Unity.exe"}), encoding="utf-8")   # Notepad's byte order mark
            self.assertEqual(m.read_paths_file(f), {"UNITY_EXE": r"C:\U\Unity.exe"})

    def test_a_broken_paths_file_is_said_not_ignored(self):
        with tempfile.TemporaryDirectory(prefix="endstar-paths-") as tmp:
            f = Path(tmp) / "paths.json"
            f.write_text('{"ENDSTAR_SDK_PROJECT": "C:/Endstar-SDK"', encoding="utf-8")   # no closing brace
            self.assertEqual(m.read_paths_file(f), {})
            self.assertIn("paths.json could not be read", m.PATHS_PROBLEM)
            f.write_text('["C:/x"]', encoding="utf-8")
            self.assertEqual(m.read_paths_file(f), {})
            self.assertIn("{ }", m.PATHS_PROBLEM)
            f.unlink()
            self.assertEqual(m.read_paths_file(f), {})
            self.assertEqual(m.PATHS_PROBLEM, "")   # no file is fine: the usual folders are used

    def test_the_newest_blender_is_found_when_the_usual_one_is_missing(self):
        with tempfile.TemporaryDirectory(prefix="endstar-blender-") as tmp:
            root = Path(tmp)
            self.assertIsNone(m.newest_blender(root))
            for name in ("Blender 4.2", "Blender 5.10", "Blender 5.2", "Blender 3.6"):
                (root / name).mkdir()
                (root / name / "blender.exe").write_bytes(b"x")
            (root / "Blender 6.0").mkdir()   # a folder without blender.exe does not count
            self.assertEqual(m.newest_blender(root), root / "Blender 5.10" / "blender.exe")


class NoticeBoxTest(unittest.TestCase):
    """11 Nov: the SDK's "Update Available!" notice held the bridge after a restart (export NOT STARTED); its Ok is pressed now."""

    def presses(self, boxes):
        sent = []
        with mock.patch.object(m, "unity_dialogs", lambda: boxes),                 mock.patch.object(m.user32, "PostMessageW", lambda hwnd, msg, w, l: sent.append((hwnd, msg)) or 1):
            others = m.confirm_export_success()
        return sent, others

    def test_the_update_notice_is_closed_with_its_ok(self):
        sent, others = self.presses([(1, "Update Available!", ["Static"], {"Ok": 7})])
        self.assertEqual(sent, [(7, m.BM_CLICK)])
        self.assertEqual(others, [])

    def test_a_notice_with_more_than_ok_is_left_for_a_person(self):
        sent, others = self.presses([(1, "Update Available!", ["Update now?"], {"Ok": 7, "Update": 8})])
        self.assertEqual(sent, [])
        self.assertEqual(len(others), 1)

    def test_export_success_is_still_confirmed_and_other_boxes_reported(self):
        sent, others = self.presses([(1, "Export Success!", [], {"Confirm": 5}), (2, "Save Scene?", ["Save?"], {"Save": 9, "Don't Save": 10})])
        self.assertEqual(sent, [(5, m.BM_CLICK)])
        self.assertEqual(len(others), 1)


if __name__ == "__main__":
    unittest.main()
