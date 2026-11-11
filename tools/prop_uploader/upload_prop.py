"""Sends a 3D model to the Endless library as a private prop. Plain Python, no AI.

    python upload_prop.py MODEL "Prop Name" ["short description"] [--fit METRES] [--rotate DEGREES] [--update]

The only way it uploads is the official Endstar SDK export inside Unity (tools\\endstar_unity_mcp.py does the talking):
  1. Blender (no window) gets the model ready: real size, standing on the ground, one mesh, its pictures saved
  2. the model goes into the SDK project and gets Endstar materials made from its own materials (bridge command propmaterials)
  3. the prop is made (static, one box collider)
  4. Unity is restarted (an export straight after making a prop froze Unity twice)
  5. the SDK exports and uploads it; the prop stays private to the signed-in account
Every failure ends as one UploadError whose text says in plain words what went wrong and what to do.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "tools"))

WORK = HERE / "work"
MODEL_FORMATS = (".blend", ".fbx", ".obj", ".glb", ".gltf", ".dae", ".stl", ".ply")
BLENDER_SECONDS = 900
NAME_LIMIT = 40
LOCK = WORK / ".running"
RECORD = HERE / "uploads.json"   # which SDK file names this tool made, and for which prop name
HISTORY = HERE / "history.json"  # every upload attempt, newest first (shown in the window as "Past uploads")
LOGS = HERE / "logs"             # the saved log of each attempt
HISTORY_LIMIT = 500
PREVIEW_SECONDS = 180


class UploadError(Exception):
    """A failure with a plain-words message: what went wrong and what to do."""


class Refused(UploadError):
    """Nothing was started because a check said no (another upload running, name already used, a tool missing, the model
    file not found). It is not a failure of an upload, so the history lists it as "refused" and makes no log for it."""


def default_eum():
    import endstar_unity_mcp
    return endstar_unity_mcp


# Checks (no Unity, no Blender) ------------------------------------------------------------------------

def slugify(name):
    """The model's file name in the SDK project: letters, digits and underscores only. A name with no Latin letter or
    digit (Greek, Japanese, emoji...) gets prop_ plus 8 letters made from the name, so different names never share a file."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    if not re.search(r"[A-Za-z0-9]", slug):
        slug = "prop_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return slug


def load_record():
    """What this tool has uploaded: {lower-case slug: {"slug": ..., "name": ...}}. Kept in tools\\prop_uploader\\uploads.json."""
    try:
        data = json.loads(Path(RECORD).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_record(slug, name):
    record = load_record()
    record[slug.lower()] = {"slug": slug, "name": name}
    Path(RECORD).parent.mkdir(parents=True, exist_ok=True)
    Path(RECORD).write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")


def models_folder(eum):
    return Path(eum.SDK_PROJECT) / "Assets" / "User Data" / "SendToEndless" / "Models"


def existing_model_file(slug, eum):
    """The file in the SDK's Models folder that Windows would treat as <slug>.fbx (case does not matter), or None."""
    folder = models_folder(eum)
    if folder.is_dir():
        for f in folder.iterdir():
            if f.name.lower() == f"{slug}.fbx".lower():
                return f
    return None


def check_can_write(name, slug, update, eum):
    """Refuses (in plain words) to overwrite anything that is not this tool's own earlier file for this same prop."""
    entry = load_record().get(slug.lower())
    mine = bool(entry) and entry.get("name") == name
    if entry and not mine:
        raise Refused(f"'{name}' would use the same file name as your prop '{entry.get('name')}', which this tool already sent. "
                      "Pick another name.")
    clash = existing_model_file(slug, eum)
    if clash is not None and not mine:
        raise Refused(f"A model file called '{clash.name}' is already in your Endstar project and was not made by this tool, "
                      "so it will not be overwritten. Pick another name.")
    if update and not mine and prop_exists(name, eum.SDK_PROJECT):
        raise Refused(f"'{name}' was not made by this tool, so it will not be replaced. Pick another name.")


def check_name(name):
    name = " ".join((name or "").split())
    if not name:
        raise UploadError("Type a name for the prop first.")
    if len(name) > NAME_LIMIT:
        raise UploadError(f"The name is too long ({len(name)} letters). Use at most {NAME_LIMIT}.")
    if re.search(r'[|\\/:*?"<>]', name):
        raise UploadError("The name may not hold any of these: | \\ / : * ? \" < >. Please change it.")
    return name


def check_description(text):
    text = " ".join((text or "").split()) or "Uploaded with Send to Endless."
    if "|" in text:
        raise UploadError("The description may not hold the letter |. Please take it out.")
    return text[:200]


def check_model_file(path):
    path = Path(path)
    if not path.is_file():
        raise Refused(f"The file '{path}' was not found. Choose the model again.")
    if path.suffix.lower() not in MODEL_FORMATS:
        raise Refused(f"'{path.suffix}' files cannot be sent. Use one of: {', '.join(MODEL_FORMATS)}.")
    return path.resolve()


def props_folders(sdk_project):
    return sorted((Path(sdk_project) / "Assets" / "User Data").glob("*/Props"))


def prop_exists(name, sdk_project):
    """True when a prop of this name is already in the SDK project (the folder the prop maker makes for it)."""
    return any((folder / name).is_dir() for folder in props_folders(sdk_project))


def check_unused_name(name, update, eum):
    if update:
        return   # check_can_write decides whether replacing is allowed
    if prop_exists(name, eum.SDK_PROJECT):
        raise Refused(f"A prop called '{name}' already exists. Choose another name, or tick 'Replace the old one' "
                          "(Endless then keeps it as a new version).")


def check_tools(eum):
    """Before anything else: Blender, Unity and the SDK project must be where the tool expects them."""
    if getattr(eum, "PATHS_PROBLEM", ""):
        raise Refused(f"{eum.PATHS_PROBLEM}. Open paths.json (next to README.md) in Notepad: each line is "
                      '"NAME": "C:\\\\folder", with quotes and commas as in the example.')
    blender = Path(eum.BLENDER_EXE)
    if not blender.is_file():
        raise Refused(f"Blender is not installed where this tool looks for it ({blender}). Install Blender 5.2 from blender.org, or put your blender.exe in paths.json.")
    unity = getattr(eum, "UNITY_EXE", None)
    if unity is not None and not Path(unity).is_file():
        raise Refused(f"Unity is not installed where this tool looks for it ({unity}). Install Unity 2022.3.62f2 with Unity Hub, or put your Unity.exe in paths.json.")
    if not Path(eum.SDK_PROJECT).is_dir():
        raise Refused(f"The Endstar SDK project folder is missing ({eum.SDK_PROJECT}). Set up the SDK first (README), or if it is in another folder, put that folder in paths.json.")


# Blender -------------------------------------------------------------------------------------------------

def run_blender(model, out_fbx, report_json, fit, rotate, eum):
    """Runs prepare_model.py in Blender with no window. Returns the report (a dict). Raises UploadError."""
    blender = Path(eum.BLENDER_EXE)
    if not blender.is_file():
        raise UploadError(f"Blender is not installed where this tool looks for it ({blender}). Install Blender 5.2 from blender.org.")
    cmd = [str(blender), "-b", "--factory-startup", "--disable-autoexec", "-P", str(HERE / "prepare_model.py"), "--", str(model), str(out_fbx), str(report_json)]
    if fit:
        cmd += ["--fit", str(fit)]
    if rotate:
        cmd += ["--rotate", str(rotate)]
    try:
        out = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                             timeout=BLENDER_SECONDS, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise UploadError("Blender took more than 15 minutes on this model and was given up on. Try a simpler model.") from None
    except OSError as e:
        raise UploadError(f"Blender could not be started ({e}).") from None
    try:
        report = json.loads(Path(report_json).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        lines = [l for l in (out.stdout + out.stderr).strip().splitlines() if l.strip()][-10:]
        try:
            Path(report_json).with_name("blender-output.txt").write_text("\n".join(lines), encoding="utf-8")
        except OSError:
            pass
        telling = next((l for l in reversed(lines) if re.search(r"error|exception|failed|traceback", l, re.I)),
                       lines[-1] if lines else "no message")
        raise UploadError(f"Blender stopped before it finished ({telling.strip()[:200]}). Try again; "
                          "if it repeats, the file may be unsupported or damaged.") from None
    if not report.get("ok"):
        raise UploadError(f"The model could not be prepared: {report.get('error', 'unknown problem')}.")
    if not Path(out_fbx).is_file():
        raise UploadError("Blender finished but made no FBX file. Please try again.")
    return report


# Unity ----------------------------------------------------------------------------------------------------

def restart_checked(eum, log):
    """Restarts Unity (or starts it) and stops with a plain message unless it is up with a working bridge."""
    result = eum.restart_unity()
    first = result.splitlines()[0] if result else ""
    if not first.startswith("Unity restarted"):
        if first.startswith("BUSY"):
            raise UploadError("Unity is busy with another export. Wait until it is done, then try again.")
        raise UploadError(f"Unity could not be restarted: {first}. Close Unity yourself, then try again.")
    errors = re.search(r"compile errors (\d+)", result)
    offline = re.search(r"server connection errors (\d+)", result)
    if errors and int(errors.group(1)):
        raise UploadError("Unity found errors in its scripts after starting. Ask whoever set the tool up to look at unity\\editor.log.")
    if offline and int(offline.group(1)):
        raise UploadError("Unity is not connected to Endless. Open Unity, sign in to Endless there once (tick Remember Me), then try again.")
    log("Unity is up.")


def bridge_has_command(eum, patient=False):
    """True when the open Unity knows the propmaterials command. patient=True keeps asking for up to 4 minutes (a Unity
    that has just started is still importing and answers late)."""
    tries = 4 if patient else 1
    for attempt in range(tries):
        if eum.unity_window() is None:
            if attempt + 1 < tries:
                time.sleep(15)
            continue
        try:
            answer = eum.send("propmaterials", timeout=60)
        except TimeoutError:
            continue
        return answer.startswith("ERROR usage")
    return False


def check_answer(what, answer):
    text = (answer or "").strip()
    if text.startswith(("ERROR", "NOT READY", "BUSY")) or "\nERROR" in text or "\nNOT READY" in text:
        first = next((l for l in text.splitlines() if l.startswith(("ERROR", "NOT READY", "BUSY"))), text.splitlines()[0] if text else "")
        raise UploadError(f"Unity could not {what}: {first[:300]}")
    return text


def clear_work(folder):
    """Empties one work folder. Only ever deletes inside tools\\prop_uploader\\work."""
    folder = Path(folder).resolve()
    if WORK.resolve() not in folder.parents:
        raise UploadError(f"Refusing to clear a folder outside the tool's work folder: {folder}")
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)


def pid_running(pid):
    """True when a process with this id is running."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(ok) and code.value == 259   # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def lock_is_live():
    """True when the lock file belongs to an upload that is really still running. A stale lock is removed."""
    if not LOCK.exists():
        return False
    try:
        pid = int(LOCK.read_text().strip().split()[0])
    except (OSError, ValueError, IndexError):
        pid = None
    if pid is None:   # an old-style lock with no process id: trust it for an hour
        live = time.time() - LOCK.stat().st_mtime < 3600
    else:
        live = pid == os.getpid() or pid_running(pid)
    if not live:
        LOCK.unlink(missing_ok=True)
    return live


def release_lock():
    """Lets go of the lock if this process holds it (also called when the window closes)."""
    try:
        if LOCK.exists() and LOCK.read_text().strip().split()[0] == str(os.getpid()):
            LOCK.unlink(missing_ok=True)
    except (OSError, IndexError):
        pass


class Lock:
    """One upload at a time (two exports at once break each other). The lock file holds this process's id, so a lock
    left by a crash is noticed and removed."""
    def __enter__(self):
        WORK.mkdir(parents=True, exist_ok=True)
        if lock_is_live():
            raise Refused("Another upload is running. Wait until it is done.")
        LOCK.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        release_lock()


# The whole thing --------------------------------------------------------------------------------------------

def _upload(model, name, description="", fit=None, rotate=0, update=False, log=print, eum=None, blender=run_blender,
            wait_for_sign_in=None, signed_in=False):
    """Does everything; returns the final "Done: ..." line, or raises UploadError.
    wait_for_sign_in(text): called to make the person sign in to Endless in Unity (it blocks until they say they have);
    signed_in=True skips the first wait (someone is watching). A failed export for 'Not authenticated' asks once more
    and retries only the export."""
    eum = eum or default_eum()
    started = time.time()
    check_tools(eum)
    model = check_model_file(model)
    name = check_name(name)
    description = check_description(description)
    if fit is not None and not (0.05 <= float(fit) <= 100):
        raise UploadError("'Fit to' must be between 0.05 and 100 metres.")
    check_unused_name(name, update, eum)
    slug = slugify(name)
    check_can_write(name, slug, update, eum)
    work = WORK / slug
    with Lock():
        log(f"1/6  Getting '{model.name}' ready in Blender...")
        clear_work(work)
        fbx, report_json = work / f"{slug}.fbx", work / "report.json"
        report = blender(model, fbx, report_json, fit, rotate, eum)
        w, d, h = report["size_m"]
        log(f"     Size: {w:g} x {d:g} x {h:g} metres, {report['triangles']:,} triangles, "
            f"{len(report['materials'])} material(s), {report.get('textured_materials', 0)} with pictures.")
        for line in report.get("warnings", []):
            log("     Note: " + line)

        log("2/6  Starting Unity (the first start can take a few minutes)...")
        problems = eum.install_bridge()
        if problems:
            raise UploadError("The Unity helper scripts could not be copied: " + "; ".join(problems))
        if not bridge_has_command(eum):
            restart_checked(eum, log)
            if not bridge_has_command(eum, patient=True):
                raise UploadError("Unity started but does not know the upload step. Please try again; if it repeats, ask for help.")
        else:
            log("Unity is already open.")

        log("3/6  Putting the model and its materials into Unity...")
        target = models_folder(eum)
        target.mkdir(parents=True, exist_ok=True)
        check_can_write(name, slug, update, eum)   # once more, now that this upload holds the lock
        save_record(slug, name)
        shutil.copy2(fbx, target / f"{slug}.fbx")
        check_answer("refresh its files", eum.send("refresh", timeout=300))
        answer = check_answer("make the materials", eum.send(f"propmaterials {slug}|{report_json}", timeout=300))
        for line in answer.splitlines():
            log("     " + line)

        log("4/6  Making the prop...")
        lift = report.get("lift", -0.5)
        made = check_answer("make the prop", eum.make_prop(slug, name, description, kind="static", lift=lift, collider="one", grid=False))
        log("     " + (made.splitlines()[-1] if made else "made"))

        log("5/6  Restarting Unity before the upload (this stops it freezing)...")
        restart_checked(eum, log)
        if wait_for_sign_in and not signed_in:
            log(SIGN_IN_PROMPT)
            wait_for_sign_in(SIGN_IN_PROMPT)

        log("6/6  Uploading with the Endstar SDK (a few minutes)...")
        result = eum.export_prop(name)
        if not result.startswith("UPLOADED") and wait_for_sign_in and is_sign_in_failure(result, eum):
            log("Endless said you are not signed in yet.")
            log(SIGN_IN_PROMPT)
            wait_for_sign_in(SIGN_IN_PROMPT)
            log("6/6  Trying the upload again...")
            result = eum.export_prop(name)   # only the export: the prop is already made
        if not result.startswith("UPLOADED"):
            raise UploadError(explain_export(result, signed_out(eum)))
        released = eum.export_log(name).read_text(encoding="utf-8", errors="replace") if eum.export_log(name).exists() else ""
        if "lock released: uploaded" not in released:
            raise UploadError("The upload was reported done but Unity's log does not confirm it. Look for the prop in Endstar before relying on it.")
        log(f"     {result}")
    minutes = (time.time() - started) / 60
    log(f"     Took {minutes:.1f} minutes.")
    return f"Done: {name} is in your Endless library (private). In the Endstar editor: Props, +, Search For Prop."


def upload(model, name, description="", fit=None, rotate=0, update=False, log=print, eum=None, blender=run_blender,
           wait_for_sign_in=None, signed_in=False, attempt=None):
    """The same as _upload (see above), and it also writes one entry in the history and saves this run's log, whether
    the upload worked, failed, was refused by a check, or was stopped (Ctrl+C). The history can never stop or hide an
    upload: if it cannot be written, that is skipped.
    attempt: an optional dict that this call fills with what is known so far, so that a window that is closed in the
    middle of the upload can still file a "stopped" entry with record_stopped(attempt)."""
    started = time.time()
    lines, info = [], {"wait": 0.0, "wait_since": None}
    attempt = attempt if attempt is not None else {}
    attempt.update(model=model, name=name, description=description, fit=fit, rotate=rotate, update=update,
                   started=started, lines=lines, info=info, recorded=False)

    def tee(text):
        lines.append(str(text))
        log(text)

    def spy(*args, **kwargs):
        info["report"] = blender(*args, **kwargs)
        return info["report"]

    def waiting(text):
        """The time spent waiting for the person to sign in is kept apart from the upload's own time."""
        info["wait_since"] = time.time()
        try:
            return wait_for_sign_in(text)
        finally:
            info["wait"] += time.time() - info["wait_since"]
            info["wait_since"] = None

    def file_it(result, reason):
        if attempt["recorded"]:
            return
        attempt["recorded"] = True
        record_attempt(model, name, description, fit, rotate, update, started, lines, info.get("report"), result, reason,
                       wait_seconds=_waited(info))

    try:
        result = _upload(model, name, description, fit, rotate, update, log=tee, eum=eum, blender=spy,
                         wait_for_sign_in=waiting if wait_for_sign_in else None, signed_in=signed_in)
    except Refused as e:
        lines.append("REFUSED: " + str(e))
        file_it("refused", str(e))
        raise
    except UploadError as e:
        lines.append("FAILED: " + str(e))
        file_it("failed", str(e))
        raise
    except KeyboardInterrupt:
        lines.append("STOPPED: " + STOPPED_BY_KEYS)
        file_it("stopped", STOPPED_BY_KEYS)
        raise
    except Exception as e:   # not an UploadError: still part of the history, then it goes on up
        reason = f"Something unexpected went wrong: {e}"
        lines.append("FAILED: " + reason)
        file_it("failed", reason)
        raise
    lines.append(result)
    file_it("uploaded", "")
    return result


STOPPED_BY_KEYS = "It was stopped (Ctrl+C) before it finished. It may have been left half done."
STOPPED_BY_WINDOW = "The window was closed before it finished. It may have been left half done."


def _waited(info):
    """Seconds spent waiting for the person to sign in so far (counting a wait that is still going on)."""
    seconds = info.get("wait", 0.0)
    if info.get("wait_since"):
        seconds += time.time() - info["wait_since"]
    return seconds


def record_stopped(attempt, reason=STOPPED_BY_WINDOW):
    """Files a "stopped" entry for an upload that is still running (the window was closed): what is known so far and the
    log so far. Does nothing when that upload has already filed its own entry. Never raises."""
    try:
        if not attempt or attempt.get("recorded") or "started" not in attempt:
            return
        attempt["recorded"] = True
        lines = list(attempt["lines"]) + ["STOPPED: " + reason]
        info = attempt["info"]
        record_attempt(attempt["model"], attempt["name"], attempt["description"], attempt["fit"], attempt["rotate"],
                       attempt["update"], attempt["started"], lines, info.get("report"), "stopped", reason,
                       wait_seconds=_waited(info))
    except Exception:
        pass


# History and saved logs ---------------------------------------------------------------------------------------

def load_history():
    """The past attempts, newest first: a list of dicts. A missing or damaged file gives an empty list."""
    try:
        data = json.loads(Path(HISTORY).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def save_run_log(name, started, lines):
    """Saves one run's log as logs\\<date>-<slug>.log. Returns its path, or "" when it could not be saved."""
    try:
        folder = Path(LOGS)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.fromtimestamp(started).strftime("%Y-%m-%d_%H%M%S")
        path, n = folder / f"{stamp}-{slugify(name or 'prop')}.log", 2
        while path.exists():
            path = folder / f"{stamp}-{slugify(name or 'prop')}-{n}.log"
            n += 1
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)
    except (OSError, ValueError):
        return ""


def write_history(entries):
    """Writes the history file whole, by way of a temporary file, so a crash never leaves half a file."""
    path = Path(HISTORY)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(entries[:HISTORY_LIMIT], indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def add_history(entry):
    """Puts one entry at the top of the history. A damaged history file is kept aside as history.json.damaged and a
    fresh one is started. Never raises."""
    try:
        path = Path(HISTORY)
        entries = load_history()
        if path.exists() and not entries:
            try:
                json.loads(path.read_text(encoding="utf-8"))   # a valid empty list needs no rescue
            except (OSError, ValueError):
                shutil.copy2(path, path.with_name(path.name + ".damaged"))
        write_history([entry] + entries)
    except Exception:
        pass


def record_attempt(model, name, description, fit, rotate, update, started, lines, report, result, reason, wait_seconds=0.0):
    """Builds the history entry for one attempt and files it. Never raises. result is uploaded, failed, refused (a check
    said no: no log is saved) or stopped. "minutes" is the upload's own time; the time spent waiting for the person to
    sign in is "sign_in_wait_minutes"."""
    try:
        report = report or {}
        materials = report.get("materials") or []
        one_line = " ".join((reason or "").split())
        wait_seconds = max(0.0, min(float(wait_seconds or 0), time.time() - started))
        working = time.time() - started - wait_seconds
        entry = {
            "when": datetime.datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M"),
            "name": name,
            "description": description,
            "model": str(Path(str(model)).resolve()) if model else "",
            "size_m": report.get("size_m"),
            "triangles": report.get("triangles"),
            "materials": len(materials) if report else None,
            "textured_materials": report.get("textured_materials", 0) if report else None,
            "result": result,
            "reason": one_line,
            "minutes": round(working / 60, 1),
            "sign_in_wait_minutes": round(wait_seconds / 60, 1),
            "log": "" if result == "refused" else save_run_log(name, started, lines),
            "fit": fit,
            "turn": rotate or 0,
            "replace": bool(update),
        }
        add_history(entry)
    except Exception:
        pass


def size_text(size_m):
    """'1 x 1 x 1 m' from a size list in metres, or '' when there is none."""
    try:
        return " x ".join(f"{float(v):g}" for v in size_m) + " m"
    except (TypeError, ValueError):
        return ""


# The five steps of the window, driven by the numbered lines the upload prints (1/6 ... 6/6) ----------------------

STEP_TITLES = ("Get it ready in Blender", "Put it into Unity", "Make the prop", "Restart Unity and sign in", "Upload")
_STEP_OF_LINE = {1: 0, 2: 1, 3: 1, 4: 2, 5: 3, 6: 4}


def step_for_line(text):
    """Which of the five steps (0 to 4) a log line starts, or None when the line does not start one."""
    m = re.match(r"\s*([1-6])/6\b", text or "")
    return _STEP_OF_LINE[int(m.group(1))] if m else None


# The picture before uploading ----------------------------------------------------------------------------------

def run_preview(model, out_png, fit=None, rotate=0, eum=None):
    """Makes a small picture of the model as it would be sent (same size, same turn) with Blender and no window. It
    sends nothing anywhere. Returns the report (a dict). Raises UploadError with one plain sentence on any failure."""
    eum = eum or default_eum()
    blender = Path(eum.BLENDER_EXE)
    if not blender.is_file():
        raise UploadError(f"Blender is not installed where this tool looks for it ({blender}), so no picture can be made.")
    out_png = Path(out_png)
    report_json = out_png.with_suffix(".json")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    for stale in (out_png, report_json):
        stale.unlink(missing_ok=True)
    cmd = [str(blender), "-b", "--factory-startup", "--disable-autoexec", "-P", str(HERE / "prepare_model.py"), "--",
           str(model), str(report_json), "--preview", str(out_png)]
    if fit:
        cmd += ["--fit", str(fit)]
    if rotate:
        cmd += ["--rotate", str(rotate)]
    try:
        subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                       timeout=PREVIEW_SECONDS, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise UploadError("Making the picture took too long, so there is no picture. You can still upload.") from None
    except OSError as e:
        raise UploadError(f"Blender could not be started to make a picture ({e}). You can still upload.") from None
    try:
        report = json.loads(report_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UploadError("Blender could not make a picture of this model. You can still upload.") from None
    if not report.get("ok"):
        raise UploadError(f"No picture: {str(report.get('error', 'unknown problem'))[:150]}. You can still upload.")
    if not out_png.is_file():
        raise UploadError("Blender made no picture of this model. You can still upload.")
    return report


SIGN_IN_PROMPT = ("Unity has restarted. In Unity, sign in to Endless (the Endless window), then press Carry on. "
                  "This tool never types your password.")


def is_sign_in_failure(result, eum):
    first = (result or "").strip().splitlines()[0] if result else ""
    return "Not authenticated" in first or signed_out(eum)


SIGN_IN_MESSAGE = ("Unity is not signed in to Endless (Endless answered 'Not authenticated'), so nothing was uploaded. "
                   "Open Unity, sign in to Endless there once (tick Remember Me), then try again. This tool never types your password.")


def signed_out(eum):
    """True when the end of Unity's own editor log (the file the tool started Unity with) shows Endless refusing the sign-in."""
    try:
        log_file = Path(eum.EDITOR_LOG)
        size = log_file.stat().st_size
        with open(log_file, "rb") as f:
            f.seek(max(0, size - 400000))
            return b"NOT_AUTHENTICATED" in f.read()
    except (AttributeError, OSError):
        return False


def explain_export(result, signed_out_flag=False):
    first = (result or "").strip().splitlines()[0] if result else "no answer"
    if signed_out_flag or "Not authenticated" in first:
        return SIGN_IN_MESSAGE
    if first.startswith("BUSY"):
        return "Unity is busy with another export. Wait until it is done, then try again."
    if "validation failed" in first or "NOT STARTED: ERROR" in first:
        said = re.sub(r"validation", "check", re.sub(r"\bSDK\b", "Endstar", first[:300]), flags=re.I)
        return f"Endstar's own upload tool refused the prop: {said}. The model may break one of its rules; try 'fit to' a different size."
    if first.startswith(("TIMED OUT", "NO ANSWER")):
        return (f"Unity stopped answering during the upload ({first[:150]}). It may be frozen: close Unity, open the tool again and retry.")
    if first.startswith("FAILED"):
        return f"The upload failed: {first[:300]}. Check that you are signed in to Endless in Unity, then try again."
    return f"The upload did not finish: {first[:300]}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Send a 3D model to your private Endless prop library.")
    ap.add_argument("model")
    ap.add_argument("name")
    ap.add_argument("description", nargs="?", default="")
    ap.add_argument("--fit", type=float, help="make the largest side this many metres")
    ap.add_argument("--rotate", type=float, default=0, help="turn the model this many degrees around the up axis")
    ap.add_argument("--update", action="store_true", help="replace a prop of the same name (a new version)")
    ap.add_argument("--signed-in", action="store_true", help="do not wait for you to sign in to Endless in Unity (use when you are watching)")
    args = ap.parse_args(argv)
    try:
        line = upload(args.model, args.name, args.description, args.fit, args.rotate, args.update, log=lambda t: print(t, flush=True),
                    wait_for_sign_in=lambda t: input("Press Enter when you have signed in: "), signed_in=args.signed_in)
    except UploadError as e:
        print(("NOT SENT: " if isinstance(e, Refused) else "FAILED: ") + str(e), flush=True)
        return 1
    except KeyboardInterrupt:
        print("STOPPED: " + STOPPED_BY_KEYS, flush=True)
        return 130
    print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
