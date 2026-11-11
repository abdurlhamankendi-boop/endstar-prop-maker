"""Endstar Unity MCP: tools for making Endstar props in the Endstar SDK (Unity) and Blender.

It talks to the open Unity editor through the Unity bridge
(unity/Editor/EndstarBridge.cs + PropMaker.cs in the SDK project): commands go into
unity/inbox, answers come back in unity/outbox. While it waits it posts WM_NULL to the Unity
window, because a background Unity can otherwise sit idle for many minutes.

A command's id is its send time; the bridge refuses commands more than 150 s old. send() reserves
the id with inbox/<id>.id (made exclusively, deleted when send() ends). The bridge claims a command
by renaming <id>.cmd to <id>.run before reading it; a command that times out here is renamed to
<id>.cancelled instead, so exactly one side gets it and a cancelled command can never run later.

Unity holds the export lock: endstar_export_prop asks "exportstate" first and answers BUSY instead of
starting a second export. Unity ends the lock by appending one "lock released: <why>" line to
outbox/export-<name>.log (uploaded / the exporter reported a failure / exporter exception / 3 minutes
after the last stage / 20 minutes after the start), and wait_for_export takes its verdict from that line.
"uploaded" comes when the SDK marks its last stage, which it does only after its own error check, for a first
upload and a re-export alike. Before it starts, Unity runs the checks the Prop SDK window's Export button runs;
a prop that fails them is answered "NOT STARTED: ERROR validation failed ..." and nothing is exported.
unity_restart refuses while an export or a prop's cells are pending, and never starts a second Unity
on the SDK project.

Two more bridge commands: setcells (a prop's claimed cells from a list the tools made, unity/cells/<name>.txt, in
place of the SDK's voxelizer) and navtest (a screen of whether a character could walk between points on a prop).

Run by Claude Code from .mcp.json:  uv run --no-project --with "mcp>=1.10,<2" python tools/endstar_unity_mcp.py
(pinned below 2: mcp 2.x renamed FastMCP to MCPServer and changed other APIs)
The functions below the tools can also be imported and called directly from Python (tools/unity_cmd.py does).
"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "unity" / "inbox"
OUTBOX = ROOT / "unity" / "outbox"
EDITOR_LOG = ROOT / "unity" / "editor.log"   # where restart_unity tells Unity to log
# Where Unity logs when it is opened any other way (Unity Hub, a shortcut).
DEFAULT_LOG = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Unity" / "Editor" / "Editor.log"
# On another PC each can be set in .mcp.json ("env", for Claude) or in paths.json in the tool's folder (for the Send
# to Endless window, so nobody has to set environment variables); an environment variable wins. The defaults are this PC's.
PATHS_FILE = ROOT / "paths.json"


PATHS_PROBLEM = ""   # set when paths.json is there but cannot be read, so the window says so instead of ignoring it


def path_backslashes(text):
    """Every lone backslash doubled, so C:\\Endstar-SDK typed with one backslash reads as typed (JSON alone refuses
    it, or turns \\t and \\b in a path into other characters); a doubled one stays as it is."""
    return re.sub(r"\\+", lambda m: m.group(0) + "\\" * (len(m.group(0)) % 2), text)


def read_paths_file(path=PATHS_FILE):
    """paths.json as a dict of names to folders, or {} when it is missing or broken (then PATHS_PROBLEM says why).
    Paths may be written with one backslash or two (10 Nov: the shipped file had one, and was silently ignored)."""
    global PATHS_PROBLEM
    PATHS_PROBLEM = ""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    try:
        data = json.loads(path_backslashes(text))
    except ValueError as e:
        PATHS_PROBLEM = f"paths.json could not be read ({e}), so its folders were not used"
        return {}
    if not isinstance(data, dict):
        PATHS_PROBLEM = "paths.json should hold names and folders in { }, so its folders were not used"
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str) and v.strip()}


def newest_blender(root=None):
    """The newest blender.exe under Program Files\\Blender Foundation (Blender 4.2, Blender 5.2, ...), or None."""
    root = Path(root or Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Blender Foundation")
    found = [(tuple(int(n) for n in re.findall(r"\d+", exe.parent.name)), exe) for exe in root.glob("Blender*/blender.exe")]
    return max(found)[1] if found else None


def setting(name, default, paths=None):
    """An environment variable, else the same name in paths.json, else this PC's default."""
    paths = read_paths_file() if paths is None else paths
    return os.environ.get(name) or paths.get(name) or default


_PATHS = read_paths_file()
SDK_PROJECT = Path(setting("ENDSTAR_SDK_PROJECT", r"C:\Endstar-SDK", _PATHS))
UNITY_EXE = Path(setting("UNITY_EXE", r"C:\Program Files\Unity\Hub\Editor\2022.3.62f2\Editor\Unity.exe", _PATHS))
BLENDER_EXE = Path(setting("BLENDER_EXE", r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe", _PATHS))
if not BLENDER_EXE.is_file() and not os.environ.get("BLENDER_EXE") and "BLENDER_EXE" not in _PATHS:
    BLENDER_EXE = newest_blender() or BLENDER_EXE   # another version in the usual place (only a path set by hand is kept)
BLENDER_SCRIPT = Path(os.environ.get("BLENDER_SCRIPT") or ROOT / "art" / "blender" / "make_props.py")
WINDOW_PREFIX = f"{SDK_PROJECT.name} - "   # Unity's window title starts with the project's folder name
CELLS_FOLDER = ROOT / "unity" / "cells"   # claimed-cell lists for the bridge's setcells command
MAX_CELLS = 20000   # the same limits as PropMaker.cs, so a bad list stops here and not in Unity
MAX_SPAN = 101      # a level fits a box of 101 cells on each axis

STATIC_PROP = "b9de8a1e-00b9-44a3-84ac-5936d1a6a5e5"
BASIC_PROP = "831286a7-0999-41e7-ae45-1e432da318eb"
VISUALS = "cb841704-b362-4d8e-99be-3ba474902340"
SECRET = re.compile(r"token|bearer|authorization|password", re.I)

POLL_SECONDS = 3      # how often an export's progress is checked
READY_SECONDS = 120   # how long make_prop waits for Unity to finish a prop's cells
UPLOAD_SECONDS = 240  # after the last stage: Unity normally answers at once, and gives up after 3 min, so 4 min covers it
PING_SECONDS = 20     # how long a log-path ping may take

# Win32 ----------------------------------------------------------------------------------------
# Own WinDLL copies, so the argtypes below do not change ctypes.windll for other code in the process.
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
# HWND, LPARAM and HANDLE are pointer-sized on 64-bit: without argtypes ctypes would cut them to 32 bits.
user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.EnumChildWindows.argtypes = [wintypes.HWND, EnumWindowsProc, wintypes.LPARAM]
user32.EnumChildWindows.restype = wintypes.BOOL
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

WM_NULL, WM_CLOSE, BM_CLICK = 0x0000, 0x0010, 0x00F5
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0


# Plumbing -----------------------------------------------------------------------------------

def unity_window():
    found = []

    def check(hwnd, _):
        buf = ctypes.create_unicode_buffer(300)
        user32.GetWindowTextW(hwnd, buf, 300)
        if buf.value.startswith(WINDOW_PREFIX):
            found.append(hwnd)
            return False
        return True

    proc = EnumWindowsProc(check)   # held in a variable so it stays alive while Windows calls it
    user32.EnumWindows(proc, 0)
    return found[0] if found else None


def window_pid(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value or None


def unity_pid():
    hwnd = unity_window()
    return window_pid(hwnd) if hwnd else None


def nudge():
    hwnd = unity_window()
    if hwnd:
        user32.PostMessageW(hwnd, WM_NULL, 0, 0)   # does nothing, but wakes the editor loop


def unity_dialogs():
    """Visible message boxes (#32770) owned by the Unity editor: [(hwnd, title, texts, {button text: hwnd})]."""
    main = unity_window()
    if not main:
        return []
    pid = window_pid(main)
    dialogs = []

    def text_of(hwnd):
        buf = ctypes.create_unicode_buffer(500)
        user32.GetWindowTextW(hwnd, buf, 500)
        return buf.value

    def check(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        cls = ctypes.create_unicode_buffer(100)
        user32.GetClassNameW(hwnd, cls, 100)
        if owner.value == pid and cls.value == "#32770" and user32.IsWindowVisible(hwnd):
            texts, buttons, progress = [], {}, []

            def child(c, _):
                kind = ctypes.create_unicode_buffer(100)
                user32.GetClassNameW(c, kind, 100)
                if kind.value == "Button":
                    buttons[text_of(c).replace("&", "")] = c
                elif kind.value == "msctls_progress32":
                    progress.append(c)
                elif text_of(c):
                    texts.append(text_of(c))
                return True

            child_proc = EnumWindowsProc(child)   # kept alive for the whole EnumChildWindows call
            user32.EnumChildWindows(hwnd, child_proc, 0)
            # Unity's progress windows ("Compiling shader", "Exporting Package", "Importing") are message
            # boxes too, with Cancel / Skip Transcoding buttons: they close by themselves.
            if not progress and not set(buttons) <= {"Cancel", "Skip Transcoding"}:
                dialogs.append((hwnd, text_of(hwnd), texts, buttons))
        return True

    proc = EnumWindowsProc(check)
    user32.EnumWindows(proc, 0)
    return dialogs


NOTICE_BOXES = ("Update Available!",)   # the SDK's notice of a newer SDK: its one button, Ok, only closes it


def confirm_export_success():
    """Presses Confirm on the SDK's "Export Success!" box, which otherwise freezes the editor until someone
    clicks it, and Ok on the SDK's "Update Available!" notice when Ok is its only button (it blocks the bridge
    the same way: 10 and 11 Nov, after restarts; Ok updates nothing). Any other dialog is left alone and
    returned as text."""
    others = []
    for hwnd, title, texts, buttons in unity_dialogs():
        if title == "Export Success!" and "Confirm" in buttons:
            user32.PostMessageW(buttons["Confirm"], BM_CLICK, 0, 0)
        elif title in NOTICE_BOXES and set(buttons) == {"Ok"}:
            user32.PostMessageW(buttons["Ok"], BM_CLICK, 0, 0)
        elif buttons:
            others.append(f"{title}: {' / '.join(texts)} [buttons: {', '.join(buttons)}]")
    return others


def new_cid():
    """A command id is its send time (yyyyMMdd-HHmmss-fff): the bridge uses it to refuse old commands."""
    now = time.time()
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"


def post(command):
    """Puts one command in the inbox and returns its id. The id stays reserved by INBOX/<id>.id until the
    caller deletes that file (send() does when it ends)."""
    INBOX.mkdir(parents=True, exist_ok=True)
    OUTBOX.mkdir(parents=True, exist_ok=True)
    while True:
        cid = new_cid()
        if any(p.exists() for p in (INBOX / f"{cid}.id", INBOX / f"{cid}.cmd", INBOX / f"{cid}.run",
                                    INBOX / f"{cid}.cancelled", OUTBOX / f"{cid}.txt")):
            time.sleep(0.002)   # another caller used this millisecond
            continue
        reserve = INBOX / f"{cid}.id"
        try:
            # "x": only one caller can make it, and it outlives the post, so no second caller can slip
            # in between this check and the .cmd appearing.
            open(reserve, "x").close()
        except FileExistsError:
            time.sleep(0.002)
            continue
        try:
            tmp = INBOX / f"{cid}.tmp"
            tmp.write_text(command, encoding="utf-8")
            os.replace(tmp, INBOX / f"{cid}.cmd")   # appears whole, never half-written
        except BaseException:
            reserve.unlink(missing_ok=True)
            raise
        return cid


def read_answer(path):
    for _ in range(20):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:   # Unity is still writing it
            time.sleep(0.1)
    return path.read_text(encoding="utf-8")


def cancel(cid):
    """Renames an unanswered <id>.cmd to <id>.cancelled and says what became of it. The bridge claims a command
    by renaming it to <id>.run, so exactly one rename wins: FileNotFoundError here means Unity has it."""
    cmd = INBOX / f"{cid}.cmd"
    for _ in range(20):
        try:
            os.replace(cmd, cmd.with_suffix(".cancelled"))
            return "cancelled, so it will not run later"
        except FileNotFoundError:
            return "taken by Unity but not answered (it may still be running)"
        except PermissionError:   # the bridge's own rename is under way: look again
            time.sleep(0.05)
    return "neither cancelled nor taken (the file stayed locked), so it may still run later"


def send(command, timeout=180, press_confirm=True):
    """Sends one bridge command and returns its answer (or raises TimeoutError). press_confirm=False leaves an "Export Success!"
    box alone while it waits, for a look that must press nothing (the start-up check)."""
    cid = post(command)
    try:
        answer = OUTBOX / f"{cid}.txt"
        name = (command.split() or ["?"])[0]
        end = time.time() + timeout
        while time.time() < end:
            nudge()
            if press_confirm:
                confirm_export_success()
            if answer.exists():
                time.sleep(0.2)
                return read_answer(answer)
            time.sleep(0.5)
        fate = cancel(cid)   # never let an unanswered command run later, out of order
        if answer.exists():   # answered while we were cancelling
            time.sleep(0.2)
            return read_answer(answer)
        raise TimeoutError(f"Unity did not answer '{name}' within {timeout:g} s; the command was {fate}")
    finally:
        try:
            (INBOX / f"{cid}.id").unlink(missing_ok=True)
        except OSError:
            pass   # a stray .id only blocks its own (past) millisecond


# Unity's log ---------------------------------------------------------------------------------

_log_cache = {}   # editor process id -> the log file that process writes


def log_field(text):
    """The path after " log=" at the end of a ping answer or heartbeat, or None."""
    m = re.search(r" log=(.+)$", text.strip())
    return Path(m.group(1).strip()) if m else None


def log_file_arg(command_line):
    """The -logFile value in a Unity command line, or None (no flag, or "-" for standard output)."""
    m = re.search(r'(?:^|\s)-logFile\s+(?:"([^"]*)"|(\S+))', command_line or "", re.I)
    if not m:
        return None
    value = m.group(1) if m.group(1) is not None else m.group(2)
    return Path(value) if value and not value.startswith("-") else None


def log_from_bridge():
    try:
        return log_field(send("ping", timeout=PING_SECONDS))
    except TimeoutError:
        return None


def log_from_command_line(pid):
    """The editor's -logFile; DEFAULT_LOG when its command line has no -logFile (a Hub launch logs there);
    None when the command line could not be read or sends the log to standard output ("-logFile -")."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                             capture_output=True, text=True, errors="replace", timeout=30,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = (out.stdout or "").strip()
    if out.returncode != 0 or not line:   # gone, or not ours to read
        return None
    if not re.search(r"(?:^|\s)-logFile(?:\s|$)", line, re.I):
        return DEFAULT_LOG
    return log_file_arg(line)


def unity_log_path(ask_bridge=True):
    """The log file the running editor writes: what the bridge says (ping's "log="), else the editor's
    command line (its -logFile, or the default Editor.log when it has none), else a guess: whichever of
    unity/editor.log and the default Editor.log changed last. Only the guess is never cached."""
    pid = unity_pid()
    if pid and pid in _log_cache:
        return _log_cache[pid]
    if pid:
        path = (log_from_bridge() if ask_bridge else None) or log_from_command_line(pid)
        if path:
            _log_cache[pid] = path
            return path
    existing = [p for p in (EDITOR_LOG, DEFAULT_LOG) if p.exists()]
    return max(existing, key=lambda p: p.stat().st_mtime) if existing else EDITOR_LOG


def log_size(path=None):
    try:
        return (path or unity_log_path()).stat().st_size
    except OSError:
        return 0


def log_since(offset, path=None):
    try:
        with open(path or unity_log_path(), "rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def exporter_exception(text, prop_name=None):
    """The first exception in `text` whose stack passes through the SDK's exporter, or None. With prop_name,
    an exception about another prop's bundle (its AssetBundles path names that prop) is skipped."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(\w+(?:\.\w+)*Exception)\b[:\s](.*)", line)
        if not m or not any(re.search(r"PropExporterV2|AssetExporter", l) for l in lines[i:i + 25]):
            continue
        bundle = re.search(r"AssetBundles[\\/]([^\\/\"]+)[\\/]", m.group(2))
        if prop_name and bundle and bundle.group(1).casefold() != prop_name.casefold():
            continue   # another prop's upload failed, not this one
        return f"{m.group(1)}: {m.group(2).strip()[:200]}"
    return None


def safe_text(text):
    return "\n".join(l for l in text.splitlines() if not SECRET.search(l))


def read_or_empty(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# Work -----------------------------------------------------------------------------------------

def status():
    beat = OUTBOX / "heartbeat.txt"
    hwnd = unity_window()
    lines = [f"Unity window open: {hwnd is not None}"]
    if beat.exists():
        age = time.time() - beat.stat().st_mtime
        lines.append(f"bridge heartbeat {age:.0f} s ago: {read_or_empty(beat)}")
    if hwnd:
        try:
            answer = send("ping", timeout=60)
            lines.append("ping: " + answer)
            pid, path = window_pid(hwnd), log_field(answer)
            if pid and path:
                _log_cache[pid] = path
        except TimeoutError as e:
            lines.append(str(e))
    # The ping above already said all the bridge can say (or timed out): never ping a second time.
    lines.append(f"editor log: {unity_log_path(ask_bridge=False)}")
    return "\n".join(lines)


def blender_models(pieces):
    """Builds pieces with the Blender script (BLENDER_SCRIPT, art/blender/make_props.py by default) into art/export.
    Blender gets no stdin: started from this server it inherited the MCP's own stdin pipe and never ran
    (27 Oct: 0.2 s of CPU in 13 minutes)."""
    out = subprocess.run([str(BLENDER_EXE), "-b", "--factory-startup", "--disable-autoexec", "-P", str(BLENDER_SCRIPT), "--", *pieces],
                         stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=1800,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    keep = [l for l in (out.stdout + out.stderr).splitlines() if re.search(r"TRIS|PREVIEW|Error|Traceback|line \d+", l)]
    return "\n".join(keep) or f"blender exited with code {out.returncode}"


def ready_marker(prop_name):
    return OUTBOX / f"makeprop-{prop_name.replace(' ', '_')}.ready"


def make_prop(model, prop_name, description, kind="static", lift=-0.5, collider="one", grid=False):
    """Builds or refreshes a prop, then waits until Unity has generated its cells (the bridge writes
    outbox/makeprop-<name>.ready with the real count) and returns the report plus that line. grid=True keeps
    the SDK's half-cell X/Z offsets off, for models built on the cell grid (buildings, walled courtyards)."""
    for label, value in (("model", model), ("prop_name", prop_name), ("description", description)):
        if "|" in value:
            raise ValueError(f"{label} may not contain '|' (the makeprop bridge splits on it)")
    base, comps = (BASIC_PROP, VISUALS) if kind == "scripted" else (STATIC_PROP, "")
    if collider not in ("one", "each", "anchor"):
        raise ValueError(f"collider must be one, each or anchor, not {collider!r}")
    each = "" if collider == "one" else collider
    marker = ready_marker(prop_name)
    marker.unlink(missing_ok=True)   # an old marker would pass for this run's cells
    on_grid = "|grid" if grid else ""
    report = send(f"makeprop {model}|{prop_name}|{description}|{base}|{lift}|{comps}|{each}{on_grid}", timeout=600)
    lines = report.strip().splitlines()
    if not lines or lines[-1].strip() != "cells pending":
        return report   # an ERROR: nothing is being generated
    end = time.time() + READY_SECONDS
    while time.time() < end:
        nudge()   # Unity's watcher runs on the editor loop
        if marker.exists():
            time.sleep(0.2)
            return report.rstrip() + "\n" + read_answer(marker).strip()
        time.sleep(0.5)
    return (report.rstrip() + f"\nNOT READY: Unity gave no cell count for {prop_name} within {READY_SECONDS} s; "
            "check endstar_prop_info before exporting")


def parse_cells(text):
    """The cells in a list file: one "x y z" per line (commas work too), "#" starts a note. Raises ValueError with the
    line number for anything else, as the bridge does."""
    cells = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].replace(",", " ").split()
        if not line:
            continue
        if len(line) != 3 or not all(re.fullmatch(r"[+-]?[0-9]+", part) for part in line):
            raise ValueError(f"line {number}: want three whole numbers x y z, got '{raw.strip()}'")
        cells.append(tuple(int(part) for part in line))
    return cells


def check_cells(cells):
    """Raises ValueError for a list the bridge's setcells would refuse (the same rules as PropMaker.ParseCells): none,
    too many, one listed twice, one below the ground cell, or a span wider than a level."""
    if not cells:
        raise ValueError("no cells")
    if len(cells) > MAX_CELLS:
        raise ValueError(f"more than {MAX_CELLS} cells")
    if len(set(cells)) != len(cells):
        raise ValueError(f"{len(cells) - len(set(cells))} cells are listed twice")
    below = next((c for c in cells if c[1] < 0), None)
    if below:
        raise ValueError(f"a cell is below the ground cell (y < 0): {below}")
    span = [max(c[i] for c in cells) - min(c[i] for c in cells) + 1 for i in range(3)]
    if max(span) > MAX_SPAN:
        raise ValueError(f"the cells span {span[0]} x {span[1]} x {span[2]}, more than the {MAX_SPAN} cells a level fits on one axis")


def cells_file_name(prop_name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", prop_name).strip("._") + ".txt"


def write_cells_file(prop_name, cells):
    """Writes the list setcells reads to unity/cells/<name>.txt (sorted, one "x y z" per line) and returns the file's
    name. Raises ValueError, before anything is written, for a list the bridge would refuse."""
    cells = [tuple(c) for c in cells]
    if any(len(c) != 3 or not all(type(v) is int for v in c) for c in cells):
        raise ValueError("every cell is three whole numbers x y z")
    check_cells(cells)
    name = cells_file_name(prop_name)
    CELLS_FOLDER.mkdir(parents=True, exist_ok=True)
    body = f"# {len(cells)} claimed cells for {prop_name}: x y z, one per line\n" + "".join(f"{x} {y} {z}\n" for x, y, z in sorted(cells))
    tmp = CELLS_FOLDER / (name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, CELLS_FOLDER / name)   # appears whole, never half-written
    return name


def set_cells(prop_name, cells, timeout=120):
    """Gives a prop exactly these claimed cells (x, y, z whole numbers, in the prop's own cells) instead of the ones the
    SDK's voxelizer finds, and makes its icon again. For pieces built on a grid, whose cells the tools know. The prop
    must already exist (endstar_make_prop). Answers at once; returns Unity's answer ("cells set ...") or its ERROR."""
    if "|" in prop_name:
        raise ValueError("prop_name may not contain '|' (the setcells bridge command splits on it)")
    name = write_cells_file(prop_name, cells)
    return send(f"setcells {prop_name}|{name}", timeout=timeout)


def nav_test(prop_name, points, timeout=120):
    """A quick SCREEN, not proof, of whether a character could walk between points on a prop: Unity builds a NavMesh
    from the prop's colliders with the game's walking agent and asks whether each point reaches the next. Points are
    "x,y,z" in metres in the prefab's own space (as endstar_prop_info shows its mesh extents; the floor of the bottom
    cell is at y = -0.5), standing on the surface. Calibrate before trusting it (see the endstar_nav_test tool)."""
    if "|" in prop_name:
        raise ValueError("prop_name may not contain '|' (the navtest bridge command splits on it)")
    spots = []
    for point in points:
        try:
            x, y, z = (float(v) for v in (point.split(",") if isinstance(point, str) else point))
        except (TypeError, ValueError):
            raise ValueError(f"a point is x,y,z in metres, got {point!r}") from None
        spots.append(f"{x:.4f},{y:.4f},{z:.4f}")
    if len(spots) < 2:
        raise ValueError("a walk needs at least two points")
    return send("navtest " + "|".join([prop_name] + spots), timeout=timeout)


def export_log(prop_name):
    return OUTBOX / f"export-{prop_name.replace(' ', '_')}.log"


def export_state(timeout=60):
    """Unity's export lock: "idle", "exporting <name> since HH:mm:ss, stages <done>/<total>", or
    "cells pending for <name>" while a makeprop's cells are being generated and no export runs."""
    return send("exportstate", timeout=timeout).strip()


def still_running_note():
    """One exportstate call, as a sentence for a result that leaves an export behind."""
    try:
        state = export_state(timeout=20)
    except TimeoutError:
        return "Unity did not say whether the export is still running; check unity_export_state before exporting again."
    if state == "idle":
        return "Unity reports no export running."
    if state.startswith("exporting"):
        return f"Unity still reports the export as running ({state}); wait for it before exporting again."
    if state.startswith("cells pending"):
        return f"Unity reports no export running ({state})."
    return f"Unity's export state: {state}"


def export_prop(prop_name, timeout_minutes=15):
    """Exports one prop with the SDK's exporter and waits until it is uploaded or has failed. Does not start
    while Unity reports another export running (BUSY)."""
    try:
        state = export_state()
    except TimeoutError as e:
        return f"NOT STARTED: {e}"
    if state.startswith("ERROR"):
        hint = " (an old bridge: run unity_restart)" if "unknown command" in state else ""
        return f"NOT STARTED: Unity could not report its export state: {state}{hint}"
    if state != "idle":
        return "BUSY: " + state
    export_log(prop_name).unlink(missing_ok=True)
    log_file = unity_log_path()   # fixed here, so the offset and every later read use the same file
    start = log_size(log_file)
    try:
        reply = send(f"exportprop {prop_name}", timeout=180)
    except TimeoutError as e:
        return f"NO ANSWER: {e}\n{still_running_note()}"
    if not reply.startswith("export started"):
        return "NOT STARTED: " + reply
    # Unity may add "note: ..." under its answer: an SDK check it could not run before the export. It goes with the
    # result, whatever the result is, so a skipped check is never lost.
    note = reply.partition("\nnote: ")[2].strip()
    result = wait_for_export(prop_name, start, timeout_minutes, log_file)
    return result + (f"\nnote from Unity before the export: {note}" if note else "")


def released(prop_name, why, done, expected):
    """The result for Unity's "lock released: <why>" line. Unity decides "uploaded", and only from the SDK's last stage
    (the SDK marks it only after its error check, so this holds for a first upload and for a re-export of a prop
    uploaded before, whose local-only flag is already False when the export starts), or, once every stage is done,
    for a first upload that stopped being local only. The SDK's own log lines ("Prop Asset Created", "Prop Update
    Complete.") are written before its error check, so they never count."""
    stages = f"{done}/{expected if expected is not None else '?'}"
    if why.startswith("uploaded"):
        try:
            version = re.search(r"version '([^']*)'", send(f"propinfo {prop_name}", timeout=120))
        except TimeoutError:
            version = None
        return f"UPLOADED {prop_name} (version {version.group(1) if version else '?'}, {expected} stages)"
    if why.startswith(("the exporter reported a failure", "exporter exception")):
        return f"FAILED after {stages} stages: {why} (see unity_editor_log)\n{still_running_note()}"
    if expected is not None and done >= expected:
        return (f"stages finished but Endstar never confirmed the upload (Unity released the lock: {why}); "
                f"check the prop in Endstar before using it, or export it again")
    return f"UNFINISHED after {stages} stages: Unity released the export lock ({why}); the export may still be running"


def wait_for_export(prop_name, log_offset=0, timeout_minutes=15, log_file=None):
    """Waits for a running export: confirms the SDK's "Export Success!" box, stops on any failure, and after
    the last stage waits (up to UPLOAD_SECONDS) for Unity's "lock released: <why>" verdict on the upload."""
    log = export_log(prop_name)
    log_file = log_file or unity_log_path()
    start = log_offset
    end = time.time() + timeout_minutes * 60
    upload_end = None   # set once every stage is done
    expected = None
    while True:
        nudge()
        others = confirm_export_success()
        if others:
            return "STOPPED: Unity is showing a dialog: " + " | ".join(others) + "\n" + still_running_note()
        text = read_or_empty(log)
        if expected is None:
            m = re.search(r"stages \[(.*)\]", text)
            if m:
                expected = m.group(1).count("ExportStage")
        done = text.count("stage done")
        failed = re.search(r"FAILED (.*)", text)
        if failed:
            return f"FAILED after {done}/{expected} stages: {failed.group(1)}\n{still_running_note()}"
        crash = exporter_exception(log_since(start, log_file), prop_name)
        if crash:   # Unity writes its own FAILED line for these too; whichever is seen first
            where = "in the upload " if upload_end else ""
            return f"FAILED after {done}/{expected} stages {where}(Unity log): {crash}\n{still_running_note()}"
        release = re.search(r"lock released: (.*)", text)
        if release:
            return released(prop_name, release.group(1).strip(), done, expected)
        if upload_end is None and expected and done >= expected:
            upload_end = time.time() + UPLOAD_SECONDS   # the upload runs after the last stage
        if upload_end is not None and time.time() >= upload_end:
            return (f"stages finished but Unity did not release the export lock within {UPLOAD_SECONDS / 60:g} min\n"
                    f"{still_running_note()}")
        if upload_end is None and time.time() >= end:
            quiet = f" Unity's log ({log_file}) has not grown since the export started." if log_size(log_file) <= start else ""
            return f"TIMED OUT after {timeout_minutes} min at {done}/{expected} stages.{quiet}\n{still_running_note()}"
        time.sleep(POLL_SECONDS)


def rotate_editor_log(tries=10, delay=1.0):
    """Moves unity/editor.log to editor-previous.log and returns (log file for the new Unity, problem or "").
    If the old file stays locked, a new name is used, so a stuck rename never keeps Unity closed."""
    if not EDITOR_LOG.exists():
        return EDITOR_LOG, ""
    error = None
    for attempt in range(tries):
        try:
            EDITOR_LOG.replace(EDITOR_LOG.with_name("editor-previous.log"))
            return EDITOR_LOG, ""
        except OSError as e:
            error = e
            if attempt < tries - 1:
                time.sleep(delay)
    new = EDITOR_LOG.with_name(f"editor-{time.strftime('%Y%m%d-%H%M%S')}.log")
    return new, f"editor.log could not be moved ({error}); this session logs to {new.name}"


def wait_for_exit(handle, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if kernel32.WaitForSingleObject(handle, 1000) == WAIT_OBJECT_0:
            return True
    return False


def pids_on_project(rows, project=None):
    """The process ids among [(pid, command line)] whose command line names the SDK project (either slash,
    any case, quoted or not), but not a longer folder name that merely starts the same way."""
    def norm(text):
        return text.replace("/", "\\").casefold()

    path = re.escape(norm(str(project or SDK_PROJECT)).rstrip("\\"))
    return sorted(int(pid) for pid, line in rows
                  if pid is not None and line and re.search(path + r'(?=$|[\\"\s])', norm(line)))


def project_unity_pids():
    """Every Unity.exe whose command line names the SDK project, window or not (its import workers too).
    Raises OSError when Windows could not be asked."""
    script = ("Get-CimInstance Win32_Process -Filter 'Name=''Unity.exe''' | "
              "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, errors="replace", timeout=60,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OSError(f"the process list could not be read: {e}") from e
    if out.returncode != 0:
        raise OSError(f"the process list could not be read: {out.stderr.strip()[:200]}")
    try:
        found = json.loads(out.stdout) if out.stdout.strip() else []
    except ValueError as e:
        raise OSError(f"the process list could not be read: {e}") from e
    found = [found] if isinstance(found, dict) else found   # one process comes back as an object, not a list
    return pids_on_project((p.get("ProcessId"), p.get("CommandLine")) for p in found)


def bridge_files():
    """The bridge scripts: every .cs file in unity/Editor. PropMaker is one class spread over several files, so
    installing only some of them would not compile in Unity."""
    return sorted(p.name for p in (ROOT / "unity" / "Editor").glob("*.cs"))


GLOW_TEXTURE = "Assets/User Data/SendToEndless/Textures/light_glow.png"


def copy_glow_texture():
    """Copies the glow map into the SDK project and returns its asset path; the materials command's
    refresh imports it."""
    target = SDK_PROJECT / GLOW_TEXTURE
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "art" / "textures" / "light_glow.png", target)
    return GLOW_TEXTURE


def install_bridge():
    """Copies the bridge scripts into the SDK project (Assets/Editor/SendToEndless) with bridge-root.txt, which
    tells them where this folder's unity/inbox and unity/outbox are. Returns the problems, if any. Unity loads the
    scripts when it starts (unity_restart calls this first)."""
    target = SDK_PROJECT / "Assets" / "Editor" / "SendToEndless"
    # An older copy under another folder name holds the same classes:
    # two copies would stop Unity compiling. It is the person's own project, so it is never deleted here.
    for marker in sorted((SDK_PROJECT / "Assets" / "Editor").glob("*/bridge-root.txt")):
        if marker.parent.resolve() != target.resolve():
            return [f"an older copy of the bridge is in {marker.parent}: close Unity, delete that folder, then try again"]
    problems = []
    try:
        target.mkdir(parents=True, exist_ok=True)
        (target / "bridge-root.txt").write_text(str(ROOT), encoding="utf-8")
    except OSError as e:
        return [f"the bridge folder could not be written: {e}"]
    for name in bridge_files():
        try:
            shutil.copy2(ROOT / "unity" / "Editor" / name, target / name)
        except OSError as e:
            problems.append(f"{name} not copied: {e}")
    return problems


def restart_unity(wait_minutes=6):
    """Closes the editor normally and opens the SDK project again (needed after editor-script changes).
    Refuses (BUSY) while Unity exports a prop or makes a prop's cells, and never starts a second Unity on the
    project while one runs, window or not."""
    hwnd = unity_window()
    if hwnd:
        # A restart in the middle of an export kills the build or the upload; in the middle of a makeprop the
        # cells and the .ready marker never come. Only a window can answer, so only then is it asked.
        try:
            state = export_state(timeout=20)
        except TimeoutError:
            state = None   # asleep or stuck: that is what a restart is for
        if state and state != "idle" and not state.startswith("ERROR"):   # ERROR: an old bridge without exportstate
            return f"BUSY: {state}; not restarting"
        pid = window_pid(hwnd)
        # Opened before WM_CLOSE, so the wait follows this very process even if its id is reused later.
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid) if pid else None
        try:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)   # like the close button
            if handle:
                closed = wait_for_exit(handle, 60)
            else:   # no handle to wait on: fall back to the window, plus time for the shutdown after it
                closed = False
                for _ in range(60):
                    time.sleep(1)
                    if unity_window() is None:
                        closed = True
                        time.sleep(5)
                        break
        finally:
            if handle:
                kernel32.CloseHandle(handle)
        if not closed:
            if unity_window():   # the window only tells us whether a dialog holds the close
                boxes = [f"{title}: {' / '.join(texts)}" for _, title, texts, _ in unity_dialogs()]
                asking = f"; it is asking: {' | '.join(boxes)}" if boxes else " (a save dialog may be open)"
                return f"Unity did not close within 60 s{asking}"
            return f"Unity's window closed but its process ({pid}) is still running after 60 s; not starting a second one"
    # Window or not: a second editor on the same project only gets Unity's "already open" box. Import workers
    # and a slow shutdown can trail the window by a few seconds, so look again up to 30 times, a second apart.
    try:
        pids = project_unity_pids()
        for _ in range(30):
            if not pids:
                break
            time.sleep(1)
            pids = project_unity_pids()
    except OSError as e:
        return f"Could not check whether Unity still runs on the SDK project ({e}); not starting a second one"
    if pids:
        return (f"Unity.exe still runs on the SDK project without a window (process {', '.join(map(str, pids))}); "
                "not starting a second one")
    problems = install_bridge()
    log_file, problem = rotate_editor_log()
    if problem:
        problems.append(problem)
    ready = OUTBOX / "ready.txt"
    launched = time.time()
    try:
        proc = subprocess.Popen([str(UNITY_EXE), "-projectPath", str(SDK_PROJECT), "-logFile", str(log_file)])
    except OSError as e:
        return f"Unity is closed and could not be started again: {e}"
    _log_cache[proc.pid] = log_file
    notes = "".join(f"\nnote: {p}" for p in problems)
    end = time.time() + wait_minutes * 60
    while time.time() < end:
        # Judged by time, not by the file's presence, so an old ready.txt can never pass for this start.
        if ready.exists() and ready.stat().st_mtime >= launched:
            time.sleep(8)
            confirm_export_success()   # the SDK's "Update Available!" notice comes up at the start and holds the bridge
            text = log_since(0, log_file)
            errors = len(re.findall(r"error CS\d+", text))
            offline = text.count("GraphQl was not initialized")
            return f"Unity restarted; bridge ready; compile errors {errors}; server connection errors {offline}{notes}"
        time.sleep(2)
    return f"Unity started but the bridge was not ready within {wait_minutes} min{notes}"


def editor_log(lines=60, contains=""):
    path = unity_log_path()
    text = safe_text(log_since(max(0, log_size(path) - 4_000_000), path))
    rows = [l for l in text.splitlines() if contains.lower() in l.lower()] if contains else text.splitlines()
    try:
        age = f"last written {time.time() - path.stat().st_mtime:.0f} s ago"
    except OSError:
        age = "missing"
    return f"(log: {path}, {age})\n" + "\n".join(rows[-lines:])


# MCP --------------------------------------------------------------------------------------------

def build_server():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("endstar-unity")

    @mcp.tool()
    def unity_status() -> str:
        """Is the Endstar SDK's Unity editor open, is the bridge alive (heartbeat), does it answer a ping, and
        which log file the editor writes."""
        return status()

    @mcp.tool()
    def blender_make_models(pieces: list[str]) -> str:
        """Builds models with the Blender script (art/blender/make_props.py) and exports FBX to art/export, plus a
        preview picture in art/preview. Pieces are the keys of PIECES in that script (e.g. arrow, glow_lamp,
        pillar, cone, sign, barrier); add a function there to make a new one."""
        return blender_models(pieces)

    @mcp.tool()
    def unity_import_models() -> str:
        """Copies every art/export/*.fbx into the SDK project (Assets/User Data/SendToEndless/Models)."""
        return send("import", timeout=600)

    @mcp.tool()
    def unity_set_materials() -> str:
        """Gives every model an Endstar material (Endless_Shader_NoFade) in the palette colours, with the glow map
        (art/textures/light_glow.png, copied into the SDK project first) on iron and the glow_* materials."""
        glow = copy_glow_texture()
        return send(f"materials Shader Graphs/Endless_Shader_NoFade|Albedo_Tint|_EmissionColor|{glow}", timeout=600)

    @mcp.tool()
    def endstar_make_prop(model: str, prop_name: str, description: str = "", kind: str = "static",
                          lift: float = -0.5, collider: str = "one", grid: bool = False) -> str:
        """Builds or refreshes an Endstar prop from an imported model: prefab with the model and collision, base
        type, claimed cells and icon, all through the SDK's own classes. Waits (up to 2 min) until Unity has
        generated the cells and ends with "ready <name> <n> cells". kind: "static" (StaticProp) or "scripted"
        (BasicProp + Visuals, for props a Lua script moves). lift: model height shift (props sit -0.5 m: the
        origin is the middle of the bottom cell). collider: "one" box round everything, "each" mesh its own
        box (walls made of separate slabs), or "anchor": one 0.25 m box in the anchor cell only (wall lights whose floor pools must not
        block a corridor). grid: true for models built on the cell grid (buildings, walled courtyards): keeps the
        SDK's half-cell X/Z offsets off, which it otherwise turns on when it rebuilds an even-sized prop. No '|'
        in model, prop_name or description."""
        return make_prop(model, prop_name, description or prop_name, kind, lift, collider, grid)

    @mcp.tool()
    def endstar_prop_info(prop_name: str) -> str:
        """Claimed cells, X/Z offsets, mesh extents, icon and upload state (local only) of a prop."""
        return send(f"propinfo {prop_name}", timeout=120)

    @mcp.tool()
    def endstar_set_cells(prop_name: str, cells_file: str) -> str:
        """Gives an existing prop exactly the claimed cells listed in a text file (one "x y z" of whole numbers per
        line, "#" starts a note; write the file with a small script of your own) instead of
        the ones the SDK's voxelizer finds, and makes its icon again. Answers at once with "cells set <name> <n>
        cells (was <m>)". The prop must have been made first (endstar_make_prop). Before a re-export of a placed
        prop, check that its footprint stays the same by comparing the old and the new cells file. The file must be inside this
        project's folder."""
        path = Path(cells_file).resolve()
        if ROOT not in path.parents:
            raise ValueError(f"the cells file must be inside {ROOT}, not {path}")
        return set_cells(prop_name, parse_cells(path.read_text(encoding="utf-8")))

    @mcp.tool()
    def endstar_nav_test(prop_name: str, points: list[str]) -> str:
        """A quick SCREEN, not proof, of whether a character could walk between points on a prop, before any export:
        Unity builds a NavMesh from the prop's colliders with the game's walking agent (radius 0.1, height 1.0,
        slope 47, climb 0.27) and says for each point whether it is on the NavMesh and whether it connects to the next
        point. points: "x,y,z" in metres in the prefab's own space (the numbers endstar_prop_info shows as mesh
        extents; the floor of the bottom cell is y = -0.5), on the walkable surface, e.g. the foot and the top of a
        stair. The game bakes its NavMesh with settings that cannot be read locally, so CALIBRATE FIRST: (1) a stair
        that a character really climbed in the game must say CONNECTED, (2) a copy with a 0.3 m riser must say NOT CONNECTED, (3) at 0.25 m and
        0.27 m riser find where it flips (the climb is rounded to the voxel height). Trust no result until those
        three were run."""
        return nav_test(prop_name, points)

    @mcp.tool()
    def endstar_export_prop(prop_name: str, timeout_minutes: int = 15) -> str:
        """Exports a prop to the signed-in Endstar account with the SDK's exporter and waits until it is uploaded
        or has failed (watches the export stages and Unity's log; the lock ends when the SDK marks its last stage,
        a re-export too; up to 4 min more as the fallback). First runs the checks the Prop SDK window's Export
        button runs and answers NOT STARTED, naming each failure, if the prop fails them. Answers BUSY without
        starting while Unity reports an export running or a prop's cells pending; a FAILED / STOPPED / TIMED OUT
        result says whether Unity still is exporting."""
        return export_prop(prop_name, timeout_minutes)

    @mcp.tool()
    def unity_export_state() -> str:
        """What Unity is busy with: "idle", "exporting <name> since HH:mm:ss, stages <done>/<total>", or
        "cells pending for <name>" (a makeprop's cells are still being generated)."""
        return export_state()

    @mcp.tool()
    def endstar_frame_prop(prop_name: str, view: str = "top") -> str:
        """Opens the prop like the Prop SDK sidebar does (claimed-cell boxes on) and points the Scene view at it
        from the top or the front, ready for a screenshot."""
        return send(f"frame {prop_name}|{view}", timeout=120)

    @mcp.tool()
    def unity_dialogs() -> str:
        """Pop-ups open in Unity. Presses Confirm on the SDK's "Export Success!" box (it freezes the editor until
        clicked) and lists any other box with buttons, untouched."""
        others = confirm_export_success()
        return "\n".join(others) if others else "no dialogs waiting (an Export Success! box, if any, was confirmed)"

    @mcp.tool()
    def unity_restart() -> str:
        """Closes Unity normally and reopens the SDK project with the latest bridge code. Needed after editing
        unity/Editor/*.cs: reloading scripts in a running editor cuts the SDK off from Endstar's servers.
        Answers BUSY without closing while an export or a prop's cells are pending, and never starts a second
        Unity while one still runs on the project (window or not)."""
        return restart_unity()

    @mcp.tool()
    def unity_editor_log(lines: int = 60, contains: str = "") -> str:
        """The end of the log the running Unity editor writes (first line names the file; lines with sign-in
        secrets removed), optionally only lines containing a word."""
        return editor_log(lines, contains)

    return mcp


if __name__ == "__main__":
    build_server().run()
