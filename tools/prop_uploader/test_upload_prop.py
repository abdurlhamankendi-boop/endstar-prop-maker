"""Offline tests for upload_prop.py: no Blender, no Unity. Run:  python -m unittest tools\\prop_uploader\\test_upload_prop.py -v"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import upload_prop as up


class FakeEum:
    """Stands in for endstar_unity_mcp: records every call; answers can be changed per test."""
    def __init__(self, sdk, outbox, window=True, has_command=True):
        self.SDK_PROJECT = sdk
        self.BLENDER_EXE = Path(sdk).parent / "blender.exe"
        self.UNITY_EXE = Path(sdk).parent / "Unity.exe"
        self.calls = []
        self.window = window
        self.has_command = has_command
        self.restart_result = "Unity restarted; bridge ready; compile errors 0; server connection errors 0"
        self.make_result = "prefab x\ncells 1"
        self.export_result = "UPLOADED Upload Test Crate (version 1.0.1, 14 stages)"
        self.materials_answer = "material Planks: picture planks\nUpload_Test_Crate.fbx uses: Planks"
        self.outbox = Path(outbox)

    def install_bridge(self):
        self.calls.append("install_bridge")
        return []

    def unity_window(self):
        return 1 if self.window else None

    def send(self, command, timeout=180, press_confirm=True):
        word = command.split()[0]
        self.calls.append(word)
        if word == "propmaterials":
            if command.strip() == "propmaterials":
                return "ERROR usage: propmaterials <Model>|<manifest.json>" if self.has_command else "ERROR unknown command propmaterials"
            return self.materials_answer
        return "ok"

    def make_prop(self, *a, **k):
        self.calls.append("make_prop")
        self.make_args = (a, k)
        return self.make_result

    def restart_unity(self, wait_minutes=6):
        self.calls.append("restart")
        self.window = True
        self.has_command = True
        return self.restart_result

    def export_prop(self, name, timeout_minutes=15):
        self.calls.append("export")
        return self.export_result

    def export_log(self, name):
        return self.outbox / f"export-{name.replace(' ', '_')}.log"


def fake_blender_ok(model, fbx, report_json, fit, rotate, eum):
    Path(fbx).write_bytes(b"fbx")
    report = {"ok": True, "size_m": [1, 1, 1], "triangles": 12, "lift": -0.5, "warnings": ["a warning"],
              "materials": [{"name": "Planks", "color": [1, 1, 1, 1], "texture": "textures/p.png"}], "textured_materials": 1}
    Path(report_json).write_text(json.dumps(report))
    return report


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.sdk = t / "sdk"
        (self.sdk / "Assets" / "User Data" / "ME" / "Props").mkdir(parents=True)
        self.out = t / "outbox"
        self.out.mkdir()
        self.model = t / "crate.blend"
        self.model.write_bytes(b"x")
        self.eum = FakeEum(self.sdk, self.out)
        up.WORK = t / "work"
        up.LOCK = up.WORK / ".running"
        up.RECORD = t / "uploads.json"
        up.HISTORY = t / "history.json"
        up.LOGS = t / "logs"
        self.eum.BLENDER_EXE.write_bytes(b"x")
        self.eum.UNITY_EXE.write_bytes(b"x")
        self.log = []
        (self.out / "export-Upload_Test_Crate.log").write_text("stage done\nlock released: uploaded\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_upload(self, **kw):
        args = dict(model=self.model, name="Upload Test Crate", description="a crate", log=self.log.append,
                    eum=self.eum, blender=fake_blender_ok)
        args.update(kw)
        return up.upload(**args)

    # names and files
    def test_slug(self):
        self.assertEqual(up.slugify("Upload Test Crate!"), "Upload_Test_Crate")
        self.assertTrue(up.slugify("***").startswith("prop_"))
        greek, emoji = up.slugify("καρέκλα"), up.slugify("😀")
        self.assertRegex(greek, r"^prop_[0-9a-f]{8}$")
        self.assertNotEqual(greek, emoji)
        self.assertEqual(greek, up.slugify("καρέκλα"))   # the same name always gives the same slug

    def test_bad_names(self):
        for bad in ("", "   ", "a|b", "x" * 41, "a/b"):
            with self.assertRaises(up.UploadError, msg=bad):
                up.check_name(bad)
        self.assertEqual(up.check_name("  Old   Crate "), "Old Crate")

    def test_description(self):
        with self.assertRaises(up.UploadError):
            up.check_description("a|b")
        self.assertTrue(up.check_description(""))

    def test_wrong_file(self):
        with self.assertRaises(up.UploadError):
            up.check_model_file(self.model.parent / "nothing.fbx")
        bad = self.model.parent / "x.txt"
        bad.write_text("x")
        with self.assertRaises(up.UploadError):
            up.check_model_file(bad)

    # the pipeline
    def test_happy_path_order(self):
        line = self.run_upload()
        self.assertEqual(line, "Done: Upload Test Crate is in your Endless library (private). In the Endstar editor: Props, +, Search For Prop.")
        calls = self.eum.calls
        # make the prop, then restart, then export, in that order
        self.assertLess(calls.index("make_prop"), calls.index("restart"))
        self.assertLess(calls.index("restart"), calls.index("export"))
        self.assertLess(calls.index("propmaterials"), calls.index("make_prop"))
        args, kw = self.eum.make_args
        self.assertEqual(args[:3], ("Upload_Test_Crate", "Upload Test Crate", "a crate"))
        self.assertEqual((kw["kind"], kw["collider"], kw["grid"], kw["lift"]), ("static", "one", False, -0.5))
        self.assertTrue((self.sdk / "Assets/User Data/SendToEndless/Models/Upload_Test_Crate.fbx").exists())
        self.assertFalse(up.LOCK.exists())

    def test_warnings_are_shown(self):
        self.run_upload()
        self.assertTrue(any("a warning" in l for l in self.log))

    def test_unity_closed_starts_it_first(self):
        self.eum.window = False
        self.eum.has_command = False
        self.run_upload()
        self.assertEqual(self.eum.calls.count("restart"), 2)   # once to start with the new bridge, once before the export

    def test_old_bridge_is_restarted(self):
        self.eum.has_command = False
        self.run_upload()
        self.assertEqual(self.eum.calls.count("restart"), 2)

    def test_name_in_use_refused_unless_update(self):
        (self.sdk / "Assets/User Data/ME/Props/Upload Test Crate").mkdir()
        with self.assertRaises(up.UploadError) as cm:
            self.run_upload()
        self.assertIn("already exists", str(cm.exception))
        self.assertNotIn("restart", self.eum.calls)
        with self.assertRaises(up.UploadError):
            self.run_upload(update=True)   # not made by this tool: never replaced
        up.save_record("Upload_Test_Crate", "Upload Test Crate")   # now it is this tool's own
        self.run_upload(update=True)
        self.assertIn("export", self.eum.calls)

    # overwrites and replacing
    def models(self):
        d = self.sdk / "Assets/User Data/SendToEndless/Models"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_existing_file_with_other_case_is_not_overwritten(self):
        (self.models() / "ArrowTest.fbx").write_bytes(b"original")
        with self.assertRaises(up.UploadError) as cm:
            self.run_upload(name="Arrowtest")
        self.assertIn("will not be overwritten", str(cm.exception))
        self.assertEqual((self.models() / "ArrowTest.fbx").read_bytes(), b"original")
        self.assertEqual(self.eum.calls, [])

    def test_two_names_one_slug_refused_but_same_name_allowed_again(self):
        (self.out / "export-Chair.log").write_text("lock released: uploaded\n")
        self.eum.export_result = "UPLOADED Chair (version 1.0.1)"
        self.run_upload(name="Chair")
        with self.assertRaises(up.UploadError) as cm:
            self.run_upload(name="Chair!")
        self.assertIn("same file name", str(cm.exception))
        self.run_upload(name="Chair", update=True)   # its own file for the same name

    def test_non_latin_names_do_not_share_a_file(self):
        for n in ("καρέκλα", "κρεβάτι"):
            self.assertTrue(up.slugify(n).startswith("prop_"))
        self.assertNotEqual(up.slugify("καρέκλα"), up.slugify("κρεβάτι"))

    def test_record_is_kept(self):
        self.run_upload()
        self.assertEqual(up.load_record()["upload_test_crate"]["name"], "Upload Test Crate")

    def test_replace_refused_for_a_prop_this_tool_did_not_make(self):
        (self.sdk / "Assets/User Data/ME/Props/Lamp").mkdir()
        with self.assertRaises(up.UploadError) as cm:
            self.run_upload(name="Lamp", update=True)
        self.assertIn("was not made by this tool, so it will not be replaced", str(cm.exception))
        self.assertEqual(self.eum.calls, [])

    def test_replace_allowed_for_this_tools_own_prop(self):
        self.run_upload()
        (self.sdk / "Assets/User Data/ME/Props/Upload Test Crate").mkdir()
        with self.assertRaises(up.UploadError):
            self.run_upload()   # without Replace: refused
        self.run_upload(update=True)

    def test_a_paths_file_that_cannot_be_read_stops_with_how_to_fix_it(self):
        self.eum.PATHS_PROBLEM = "paths.json could not be read (Expecting ','), so its folders were not used"
        with self.assertRaises(up.UploadError) as cm:
            self.run_upload()
        self.assertIn("paths.json could not be read", str(cm.exception))
        self.assertIn("Notepad", str(cm.exception))
        self.assertNotIn("install_bridge", self.eum.calls)   # nothing ran

    # tools are checked first
    def test_missing_tools_are_named_before_anything_runs(self):
        for attr, word in (("BLENDER_EXE", "Blender"), ("UNITY_EXE", "Unity")):
            getattr(self.eum, attr).unlink()
            with self.assertRaises(up.UploadError) as cm:
                self.run_upload()
            self.assertIn(word, str(cm.exception))
            self.assertIn(str(getattr(self.eum, attr)), str(cm.exception))
            getattr(self.eum, attr).write_bytes(b"x")
        import shutil
        shutil.rmtree(self.sdk)
        with self.assertRaises(up.UploadError) as cm:
            self.run_upload()
        self.assertIn("SDK project folder", str(cm.exception))
        self.assertEqual(self.eum.calls, [])
        self.assertFalse(up.WORK.exists())   # Blender step never started

    def test_unity_message_does_not_claim_sign_in(self):
        self.run_upload()
        self.assertFalse(any("signed in" in l for l in self.log))

    def test_blender_stopped_message(self):
        import subprocess
        from unittest import mock
        done = subprocess.CompletedProcess([], 1, stdout="a\nb\nTraceback x\nRuntimeError: boom\n", stderr="")
        with mock.patch.object(up.subprocess, "run", return_value=done):
            with self.assertRaises(up.UploadError) as cm:
                up.run_blender(self.model, Path(self.tmp.name) / "o.fbx", Path(self.tmp.name) / "r.json", None, 0, self.eum)
        self.assertIn("Blender stopped before it finished", str(cm.exception))
        self.assertIn("RuntimeError: boom", str(cm.exception))

    def test_blender_gets_disable_autoexec(self):
        import subprocess
        from unittest import mock
        done = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        with mock.patch.object(up.subprocess, "run", return_value=done) as run:
            with self.assertRaises(up.UploadError):
                up.run_blender(self.model, Path(self.tmp.name) / "o.fbx", Path(self.tmp.name) / "r.json", None, 0, self.eum)
        self.assertIn("--disable-autoexec", run.call_args[0][0])

    # sign in after the restart
    def test_waits_for_sign_in_after_restart_before_export(self):
        order = []
        self.run_upload(wait_for_sign_in=lambda t: order.append((t, list(self.eum.calls))))
        self.assertEqual(len(order), 1)
        text, calls_then = order[0]
        self.assertIn("sign in to Endless", text)
        self.assertIn("never types your password", text)
        self.assertNotIn("export", calls_then)
        self.assertIn("restart", calls_then)

    def test_signed_in_flag_skips_the_wait(self):
        waits = []
        self.run_upload(wait_for_sign_in=waits.append, signed_in=True)
        self.assertEqual(waits, [])

    def test_not_authenticated_waits_again_and_retries_only_the_export(self):
        answers = ["FAILED after 6/14 stages: Not authenticated", "UPLOADED Upload Test Crate (version 1.0.1)"]
        self.eum.export_prop = lambda name, timeout_minutes=15: (self.eum.calls.append("export"), answers.pop(0))[1]
        waits = []
        self.run_upload(wait_for_sign_in=waits.append)
        self.assertEqual(len(waits), 2)                      # once after the restart, once after the refusal
        self.assertEqual(self.eum.calls.count("export"), 2)
        self.assertEqual(self.eum.calls.count("make_prop"), 1)   # the pipeline was not run again

    def test_still_not_authenticated_after_retry_fails_plainly(self):
        self.eum.export_result = "FAILED after 6/14 stages: Not authenticated"
        with self.assertRaises(up.UploadError) as cm:
            self.run_upload(wait_for_sign_in=lambda t: None)
        self.assertIn("not signed in", str(cm.exception))
        self.assertEqual(self.eum.calls.count("export"), 2)

    def test_blender_failure_stops_early(self):
        def broken(*a):
            raise up.UploadError("The model could not be prepared: no solid shape.")
        with self.assertRaises(up.UploadError):
            self.run_upload(blender=broken)
        self.assertEqual(self.eum.calls, [])
        self.assertFalse(up.LOCK.exists())   # the lock is let go on failure

    def test_materials_error_stops(self):
        self.eum.materials_answer = "ERROR shader not found: x"
        with self.assertRaises(up.UploadError) as cm:
            self.run_upload()
        self.assertIn("materials", str(cm.exception))
        self.assertNotIn("make_prop", self.eum.calls)

    def test_make_prop_not_ready_stops(self):
        self.eum.make_result = "x\nNOT READY: no cells"
        with self.assertRaises(up.UploadError):
            self.run_upload()
        self.assertNotIn("export", self.eum.calls)

    def test_not_signed_in_stops(self):
        self.eum.restart_result = "Unity restarted; bridge ready; compile errors 0; server connection errors 3"
        with self.assertRaises(up.UploadError) as cm:
            self.run_upload()
        self.assertIn("sign in", str(cm.exception))
        self.assertNotIn("export", self.eum.calls)

    def test_compile_errors_stop(self):
        self.eum.restart_result = "Unity restarted; bridge ready; compile errors 2; server connection errors 0"
        with self.assertRaises(up.UploadError):
            self.run_upload()

    def test_busy_restart(self):
        self.eum.restart_result = "BUSY: exporting x"
        with self.assertRaises(up.UploadError) as cm:
            self.run_upload()
        self.assertIn("busy", str(cm.exception))

    def test_export_failures_are_explained(self):
        for result, word in (("TIMED OUT after 15 min at 0/14 stages.", "frozen"),
                             ("FAILED after 3/14 stages: x", "failed"),
                             ("NOT STARTED: ERROR validation failed (no collider)", "refused"),
                             ("BUSY: exporting y", "busy")):
            self.eum.export_result = result
            with self.assertRaises(up.UploadError) as cm:
                self.run_upload()
            self.assertIn(word, str(cm.exception).lower(), result)

    def test_signed_out_is_explained(self):
        log = Path(self.tmp.name) / "editor.log"
        log.write_text('"custom_code": "NOT_AUTHENTICATED"')
        self.eum.EDITOR_LOG = log
        self.eum.export_result = "FAILED after 6/14 stages: Unable to communicate with servers."
        with self.assertRaises(up.UploadError) as cm:
            self.run_upload()
        self.assertIn("not signed in", str(cm.exception))
        self.assertIn("never types your password", str(cm.exception))

    def test_log_must_confirm_upload(self):
        (self.out / "export-Upload_Test_Crate.log").write_text("stage done\n")
        with self.assertRaises(up.UploadError):
            self.run_upload()

    def test_second_upload_blocked_while_first_runs(self):
        up.WORK.mkdir(parents=True)
        up.LOCK.write_text(str(os.getpid()))   # a live process: this one
        with self.assertRaises(up.UploadError):
            self.run_upload()
        up.LOCK.unlink()

    def test_lock_of_dead_process_is_removed(self):
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        up.WORK.mkdir(parents=True)
        up.LOCK.write_text(str(proc.pid))
        self.assertFalse(up.pid_running(proc.pid))
        self.run_upload()   # not blocked
        self.assertFalse(up.LOCK.exists())

    def test_lock_holds_our_process_id_and_is_released(self):
        with up.Lock():
            self.assertEqual(up.LOCK.read_text().strip(), str(os.getpid()))
        self.assertFalse(up.LOCK.exists())
        with up.Lock():
            up.release_lock()
            self.assertFalse(up.LOCK.exists())

    def test_fit_range(self):
        with self.assertRaises(up.UploadError):
            self.run_upload(fit=500)

    def test_clear_work_only_inside_work(self):
        with self.assertRaises(up.UploadError):
            up.clear_work(Path(self.tmp.name) / "elsewhere")

    # history
    def test_history_written_on_success(self):
        self.run_upload(description="a crate", fit=2, rotate=90, update=False)
        entries = up.load_history()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual((e["name"], e["description"], e["result"], e["reason"]), ("Upload Test Crate", "a crate", "uploaded", ""))
        self.assertEqual(e["model"], str(self.model.resolve()))
        self.assertEqual((e["size_m"], e["triangles"], e["materials"], e["textured_materials"]), ([1, 1, 1], 12, 1, 1))
        self.assertEqual((e["fit"], e["turn"], e["replace"]), (2, 90, False))
        self.assertRegex(e["when"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertIsInstance(e["minutes"], float)
        saved = Path(e["log"]).read_text(encoding="utf-8")
        self.assertTrue(Path(e["log"]).name.endswith("-Upload_Test_Crate.log"))
        self.assertIn("1/6", saved)
        self.assertIn("Done: Upload Test Crate", saved)

    def test_history_written_on_failure(self):
        self.eum.export_result = "FAILED after 3/14 stages: x"
        with self.assertRaises(up.UploadError):
            self.run_upload()
        e = up.load_history()[0]
        self.assertEqual(e["result"], "failed")
        self.assertIn("The upload failed", e["reason"])
        self.assertNotIn("\n", e["reason"])
        self.assertEqual(e["size_m"], [1, 1, 1])   # Blender had finished, so the size is known
        self.assertIn("FAILED:", Path(e["log"]).read_text(encoding="utf-8"))

    def test_history_failure_before_blender_has_no_size(self):
        with self.assertRaises(up.UploadError):
            self.run_upload(fit=500)
        e = up.load_history()[0]
        self.assertEqual(e["result"], "failed")
        self.assertIsNone(e["size_m"])
        self.assertIsNone(e["materials"])

    def test_history_is_newest_first(self):
        (self.out / "export-Chair.log").write_text("lock released: uploaded\n")
        self.eum.export_result = "UPLOADED Chair (version 1.0.1)"
        self.run_upload()
        self.run_upload(name="Chair")
        with self.assertRaises(up.UploadError):
            self.run_upload(name="Chair!")   # refused: the same file name as Chair
        entries = up.load_history()
        self.assertEqual([(e["name"], e["result"]) for e in entries],
                         [("Chair!", "refused"), ("Chair", "uploaded"), ("Upload Test Crate", "uploaded")])

    # refused, stopped, and the sign-in wait
    def test_refusals_are_recorded_as_refused_with_no_log(self):
        up.WORK.mkdir(parents=True)
        up.LOCK.write_text(str(os.getpid()))   # another upload is running
        with self.assertRaises(up.UploadError):
            self.run_upload()
        up.LOCK.unlink()
        (self.sdk / "Assets" / "User Data" / "ME" / "Props" / "Taken").mkdir()
        with self.assertRaises(up.UploadError):
            self.run_upload(name="Taken")                       # name already used
        with self.assertRaises(up.UploadError):
            self.run_upload(model=self.model.parent / "gone.fbx")   # model not found
        self.eum.BLENDER_EXE.unlink()
        with self.assertRaises(up.UploadError):
            self.run_upload()                                   # tools missing
        entries = up.load_history()
        self.assertEqual([e["result"] for e in entries], ["refused"] * 4)
        self.assertTrue(all(e["log"] == "" and e["reason"] for e in entries))
        self.assertFalse(up.LOGS.exists() and any(up.LOGS.iterdir()))

    def test_real_failures_stay_failed(self):
        with self.assertRaises(up.UploadError):
            self.run_upload(fit=500)
        self.eum.export_result = "FAILED after 3/14 stages: x"
        with self.assertRaises(up.UploadError):
            self.run_upload()
        self.assertEqual([e["result"] for e in up.load_history()], ["failed", "failed"])

    def test_ctrl_c_writes_a_stopped_entry_with_the_log_so_far(self):
        def interrupted(*a, **k):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_upload(blender=interrupted)
        e = up.load_history()[0]
        self.assertEqual(e["result"], "stopped")
        self.assertIn("Ctrl+C", e["reason"])
        saved = Path(e["log"]).read_text(encoding="utf-8")
        self.assertIn("1/6", saved)
        self.assertIn("STOPPED:", saved)
        self.assertFalse(up.LOCK.exists())

    def test_closing_the_window_mid_upload_files_a_stopped_entry_once(self):
        attempt = {}

        def stuck_blender(*a, **k):
            up.record_stopped(attempt)   # the window is closed while Blender is working
            return fake_blender_ok(*a, **k)
        self.run_upload(blender=stuck_blender, attempt=attempt)   # the thread would normally be killed; here it ends
        entries = up.load_history()
        self.assertEqual(len(entries), 1)   # the late finish does not file a second entry
        self.assertEqual(entries[0]["result"], "stopped")
        self.assertIn("window was closed", entries[0]["reason"])
        self.assertIn("1/6", Path(entries[0]["log"]).read_text(encoding="utf-8"))
        up.record_stopped({})   # nothing known: nothing written
        self.assertEqual(len(up.load_history()), 1)

    def test_sign_in_wait_is_kept_apart_from_the_upload_time(self):
        from unittest import mock
        clock = [1000.0]
        real_time = up.time.time

        def waits(text):
            clock[0] += 600   # ten minutes waiting for the person
        with mock.patch.object(up.time, "time", side_effect=lambda: clock[0]), mock.patch.object(up.time, "sleep"):
            def blender(*a, **k):
                clock[0] += 120   # two minutes of work
                return fake_blender_ok(*a, **k)
            self.run_upload(blender=blender, wait_for_sign_in=waits)
        e = up.load_history()[0]
        self.assertEqual(e["minutes"], 2.0)
        self.assertEqual(e["sign_in_wait_minutes"], 10.0)
        self.assertIsNotNone(real_time)

    def test_stop_while_waiting_for_sign_in_counts_the_wait(self):
        from unittest import mock
        clock = [5000.0]

        def waits(text):
            clock[0] += 300
            raise KeyboardInterrupt
        with mock.patch.object(up.time, "time", side_effect=lambda: clock[0]):
            with self.assertRaises(KeyboardInterrupt):
                self.run_upload(wait_for_sign_in=waits)
        e = up.load_history()[0]
        self.assertEqual((e["result"], e["sign_in_wait_minutes"], e["minutes"]), ("stopped", 5.0, 0.0))

    def test_two_runs_in_one_second_keep_separate_logs(self):
        self.run_upload()
        self.run_upload(update=True)
        logs = {e["log"] for e in up.load_history()}
        self.assertEqual(len(logs), 2)
        self.assertTrue(all(Path(l).is_file() for l in logs))

    def test_damaged_history_does_not_stop_an_upload(self):
        up.HISTORY.write_text("{ this is not json", encoding="utf-8")
        line = self.run_upload()
        self.assertTrue(line.startswith("Done:"))
        self.assertEqual(len(up.load_history()), 1)
        self.assertEqual(up.HISTORY.with_name("history.json.damaged").read_text(encoding="utf-8"), "{ this is not json")

    def test_history_that_cannot_be_written_does_not_stop_an_upload(self):
        up.HISTORY.mkdir()   # a folder where the file should be: writing fails
        line = self.run_upload()
        self.assertTrue(line.startswith("Done:"))
        self.assertTrue(up.HISTORY.is_dir())

    def test_failure_with_damaged_history_still_raises_the_real_error(self):
        up.HISTORY.write_text("[1, 2", encoding="utf-8")
        with self.assertRaises(up.UploadError) as cm:
            self.run_upload(fit=500)
        self.assertIn("between 0.05 and 100", str(cm.exception))

    def test_valid_empty_history_is_not_called_damaged(self):
        up.HISTORY.write_text("[]", encoding="utf-8")
        self.run_upload()
        self.assertFalse(up.HISTORY.with_name("history.json.damaged").exists())

    def test_export_failure_words_are_plain(self):
        self.eum.export_result = "NOT STARTED: ERROR SDK validation failed (no collider)"
        with self.assertRaises(up.UploadError) as cm:
            self.run_upload()
        text = str(cm.exception)
        self.assertNotIn("SDK", text)
        self.assertNotIn("validation", text.lower())
        self.assertIn("refused", text)

    def test_size_text(self):
        self.assertEqual(up.size_text([1, 2.5, 0.75]), "1 x 2.5 x 0.75 m")
        self.assertEqual(up.size_text(None), "")

    # the five steps
    def test_step_for_line(self):
        self.assertEqual([up.step_for_line(f"{n}/6  something") for n in range(1, 7)], [0, 1, 1, 2, 3, 4])
        self.assertEqual(up.step_for_line("6/6  Trying the upload again..."), 4)
        for other in ("Unity is up.", "     Size: 1 x 1 x 1 metres", "", "10/6 x", None):
            self.assertIsNone(up.step_for_line(other))
        self.assertEqual(len(up.STEP_TITLES), 5)

    def test_the_real_upload_prints_lines_for_every_step(self):
        self.run_upload()
        self.assertEqual(sorted({up.step_for_line(l) for l in self.log} - {None}), [0, 1, 2, 3, 4])

    # the picture before uploading
    def preview_paths(self):
        return Path(self.tmp.name) / "pv" / "p.png"

    def test_preview_asks_blender_for_the_same_preparation(self):
        import subprocess
        from unittest import mock
        png = self.preview_paths()

        def fake_run(cmd, **kw):
            png.write_bytes(b"png")
            png.with_suffix(".json").write_text(json.dumps({"ok": True, "size_m": [1, 1, 1], "triangles": 12}))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        with mock.patch.object(up.subprocess, "run", side_effect=fake_run) as run:
            report = up.run_preview(self.model, png, fit=2, rotate=90, eum=self.eum)
        cmd = run.call_args[0][0]
        for word in ("-b", "--factory-startup", "--disable-autoexec", "--preview", "--fit", "--rotate"):
            self.assertIn(word, cmd)
        self.assertEqual(cmd[cmd.index("--fit") + 1], "2")
        self.assertEqual(cmd[cmd.index("--rotate") + 1], "90")
        self.assertEqual(report["size_m"], [1, 1, 1])
        self.assertEqual(self.eum.calls, [])   # nothing was sent to Unity

    def test_preview_failure_messages_are_plain_and_allow_uploading(self):
        import subprocess
        from unittest import mock
        png = self.preview_paths()
        crash = subprocess.CompletedProcess([], 1, stdout="Traceback\nboom", stderr="")
        with mock.patch.object(up.subprocess, "run", return_value=crash):
            with self.assertRaises(up.UploadError) as cm:
                up.run_preview(self.model, png, eum=self.eum)   # Blender wrote no report
        self.assertIn("You can still upload", str(cm.exception))

        def says_no(cmd, **kw):
            png.with_suffix(".json").write_text(json.dumps({"ok": False, "error": "no solid shape was found"}))
            return crash
        with mock.patch.object(up.subprocess, "run", side_effect=says_no):
            with self.assertRaises(up.UploadError) as cm:
                up.run_preview(self.model, png, eum=self.eum)
        self.assertIn("no solid shape was found", str(cm.exception))
        self.assertIn("You can still upload", str(cm.exception))

        def no_picture(cmd, **kw):
            png.with_suffix(".json").write_text(json.dumps({"ok": True, "size_m": [1, 1, 1]}))
            return crash
        with mock.patch.object(up.subprocess, "run", side_effect=no_picture):
            with self.assertRaises(up.UploadError) as cm:
                up.run_preview(self.model, png, eum=self.eum)
        self.assertIn("made no picture", str(cm.exception))

        with mock.patch.object(up.subprocess, "run", side_effect=subprocess.TimeoutExpired("blender", 1)):
            with self.assertRaises(up.UploadError) as cm:
                up.run_preview(self.model, png, eum=self.eum)
        self.assertIn("took too long", str(cm.exception))

        with mock.patch.object(up.subprocess, "run", side_effect=OSError("denied")):
            with self.assertRaises(up.UploadError) as cm:
                up.run_preview(self.model, png, eum=self.eum)
        self.assertIn("could not be started", str(cm.exception))

    def test_preview_without_blender_is_one_plain_line(self):
        self.eum.BLENDER_EXE.unlink()
        with self.assertRaises(up.UploadError) as cm:
            up.run_preview(self.model, self.preview_paths(), eum=self.eum)
        self.assertIn("Blender is not installed", str(cm.exception))

    def test_an_old_picture_is_never_mistaken_for_a_new_one(self):
        import subprocess
        from unittest import mock
        png = self.preview_paths()
        png.parent.mkdir(parents=True)
        png.write_bytes(b"old")
        png.with_suffix(".json").write_text(json.dumps({"ok": True, "size_m": [9, 9, 9]}))
        with mock.patch.object(up.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="")):
            with self.assertRaises(up.UploadError):
                up.run_preview(self.model, png, eum=self.eum)

    # check_answer
    def test_check_answer(self):
        self.assertEqual(up.check_answer("x", "fine"), "fine")
        with self.assertRaises(up.UploadError):
            up.check_answer("x", "line\nERROR boom")


if __name__ == "__main__":
    unittest.main()
