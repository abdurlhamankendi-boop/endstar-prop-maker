"""Send to Endless: the window for sending a 3D model to your private Endless prop library.
Open it, or drag a model file onto "Send to Endless.cmd". No installs beyond Python (tkinter comes with it)."""
import atexit
import datetime
import os
from fractions import Fraction
import queue
import re
import shutil
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import upload_prop as up
    START_ERROR = None
except Exception as _e:   # shown in a window by the bottom of this file
    up = None
    START_ERROR = _e

TYPES = [("3D models", "*.blend *.fbx *.obj *.glb *.gltf *.dae *.stl *.ply"), ("All files", "*.*")]
FONT = "Segoe UI"
GREEN, RED, BLUE, GREY, INK = "#0a7d2c", "#b00020", "#1a73e8", "#8a9097", "#202124"
PICTURE_W, PICTURE_H = 360, 240   # at 100% screen scaling; the window multiplies them on a scaled screen
RESULT_WORDS = {"uploaded": "Uploaded", "failed": "Failed", "refused": "Refused", "stopped": "Stopped"}
PREVIEW_FOLDER = Path(tempfile.gettempdir()) / "send-to-endless-preview"


def pretty_name(path):
    return re.sub(r"[_\-]+", " ", Path(path).stem).strip().title()[:40]


def nice_when(text):
    try:
        d = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
        return f"{d.day} {d.strftime('%b %Y, %H:%M')}"
    except (TypeError, ValueError):
        return str(text or "")


def detail_word(triangles):
    """'small detail', 'medium detail' or 'large detail' from a triangle count (the count itself is only for Show details)."""
    try:
        n = int(triangles)
    except (TypeError, ValueError):
        return ""
    return "small detail" if n < 5000 else "medium detail" if n < 50000 else "large detail"


def took_text(entry):
    """'took 6 min (plus 2 min waiting for you to sign in)', or '' when the time is not known."""
    if entry.get("minutes") is None:
        return ""
    text = f"took {entry['minutes']:g} min"
    waited = entry.get("sign_in_wait_minutes")
    if waited:
        text += f" (plus {waited:g} min waiting for you to sign in)"
    return text


def entry_facts(entry):
    """One plain line about a past upload: how long it took, its size (left to right x front to back x height), how
    detailed it is, and how many colour pictures it has."""
    parts = []
    if entry.get("result") in ("uploaded", "failed", "stopped"):
        parts.append(took_text(entry))
    if entry.get("size_m"):
        parts.append(up.size_text(entry["size_m"]) + " (left to right x front to back x height)")
    parts.append(detail_word(entry.get("triangles")))
    if entry.get("materials") is not None:
        pictures = entry.get("textured_materials") or 0
        parts.append(f"{pictures} colour picture{'' if pictures == 1 else 's'}" if pictures else "no colour pictures")
    return "  |  ".join(p for p in parts if p)


class StepBar(ttk.Frame):
    """The five steps of an upload, each waiting, working, done or failed."""
    LOOK = {"waiting": GREY, "working": BLUE, "done": GREEN, "failed": RED}

    def __init__(self, parent, titles, bg):
        super().__init__(parent)
        self.bg = bg
        self.states = ["waiting"] * len(titles)
        self.notes = [""] * len(titles)
        self.canvases, self.state_labels = [], []
        self.dots = 0
        for i, title in enumerate(titles):
            self.columnconfigure(i, weight=1, uniform="step")
            cell = ttk.Frame(self)
            cell.grid(row=0, column=i, sticky="n", padx=4)
            cv = tk.Canvas(cell, width=38, height=38, bg=bg, highlightthickness=0)
            cv.pack()
            ttk.Label(cell, text=title, wraplength=124, justify="center", anchor="center").pack(pady=(4, 0))
            state = tk.Label(cell, text="", bg=bg, font=(FONT, 9))
            state.pack()
            self.canvases.append(cv)
            self.state_labels.append(state)
            self.draw(i)

    def draw(self, i):
        state = self.states[i]
        colour = self.LOOK[state]
        cv = self.canvases[i]
        cv.delete("all")
        if state == "waiting":
            cv.create_oval(3, 3, 35, 35, outline=colour, width=2, fill=self.bg)
            cv.create_text(19, 19, text=str(i + 1), fill=colour, font=(FONT, 12, "bold"))
        else:
            cv.create_oval(3, 3, 35, 35, outline=colour, fill=colour)
            glyph = {"working": str(i + 1), "done": "✓", "failed": "✗"}[state]
            cv.create_text(19, 19, text=glyph, fill="white", font=(FONT, 13, "bold"))
        word = self.notes[i] or {"waiting": "waiting", "working": "working", "done": "done", "failed": "failed"}[state]
        if state == "working" and not self.notes[i]:
            word += "." * self.dots
        self.state_labels[i].configure(text=word, fg=colour)

    def set_state(self, i, state, note=""):
        self.states[i], self.notes[i] = state, note
        self.draw(i)

    def progress(self, current, note=""):
        """Steps before `current` are done, `current` is working, the rest wait."""
        for i in range(len(self.states)):
            self.set_state(i, "done" if i < current else "working" if i == current else "waiting", note if i == current else "")

    def finish(self):
        for i in range(len(self.states)):
            self.set_state(i, "done")

    def fail_current(self):
        for i, s in enumerate(self.states):
            if s == "working":
                self.set_state(i, "failed")
                return

    def reset(self):
        for i in range(len(self.states)):
            self.set_state(i, "waiting")

    def tick(self):
        self.dots = (self.dots + 1) % 4
        for i, s in enumerate(self.states):
            if s == "working" and not self.notes[i]:
                self.draw(i)


class App(tk.Tk):
    def __init__(self, first_file=None):
        super().__init__()
        self.scale = max(1.0, self.winfo_fpixels("1i") / 96.0)   # 1 on a normal screen, 1.5 on a 150% screen
        self.pic_w, self.pic_h = round(PICTURE_W * self.scale), round(PICTURE_H * self.scale)
        self.title("Send to Endless")
        self.minsize(800, 640)
        self.lines = queue.Queue()
        self.running = False
        self.go = threading.Event()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.path = tk.StringVar()
        self.path_name = tk.StringVar()   # just the file name, which is what a person needs to see
        self.name = tk.StringVar()
        self.desc = tk.StringVar()
        self.size_mode = tk.StringVar(value="asis")
        self.fit = tk.StringVar(value="1")
        self.turn = tk.StringVar(value="0")
        self.replace = tk.BooleanVar(value=False)
        self.show_details = tk.BooleanVar(value=False)
        self.history = []
        self.picture = None
        self.preview_job = None
        self.preview_busy = False
        self.preview_dirty = False
        self.preview_count = 0
        self.preview_serial = 0   # goes up whenever the form changes: a picture made for an older number is thrown away
        self.attempt = None       # what the running upload knows so far (so closing the window can file a "stopped" entry)
        self.bg = self.setup_look()
        self.configure(bg=self.bg)
        self.build()
        self.path.trace_add("write", lambda *_: self.path_name.set(Path(self.path.get()).name if self.path.get() else ""))
        for var in (self.path, self.size_mode, self.fit, self.turn):
            var.trace_add("write", self.preview_wanted)
        if first_file:
            self.set_file(first_file)
        self.reload_history()
        self.after(200, self.pump)
        self.after(450, self.animate)

    # Look -----------------------------------------------------------------------------------------------

    def setup_look(self):
        style = ttk.Style(self)
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
            tkfont.nametofont(name).configure(family=FONT, size=10)
        tkfont.nametofont("TkFixedFont").configure(family="Consolas", size=9)
        bg = style.lookup("TFrame", "background") or "#f0f0f0"
        style.configure("Title.TLabel", font=(FONT, 18, "bold"))
        style.configure("Hint.TLabel", foreground="#5f6368")
        style.configure("Section.TLabel", font=(FONT, 11, "bold"))
        style.configure("Warn.TLabel", foreground="#8a4b00")
        style.configure("Big.TButton", font=(FONT, 12, "bold"), padding=(10, 8))
        style.configure("TButton", padding=(10, 4))
        style.configure("Treeview", rowheight=26, font=(FONT, 10))
        style.configure("Treeview.Heading", font=(FONT, 10, "bold"))
        return bg

    def build(self):
        pad = 18
        m = ttk.Frame(self, padding=(pad, 14, pad, 14))
        m.pack(fill="both", expand=True)
        m.columnconfigure(0, weight=1)
        self.main = m

        ttk.Label(m, text="Send a 3D model to Endless", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(m, text="It stays private in your own library.", style="Hint.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))

        # the form (left) and the picture (right)
        form = ttk.Frame(m)
        form.grid(row=2, column=0, sticky="nsew", padx=(0, 18))
        form.columnconfigure(1, weight=1)
        ttk.Button(form, text="Choose a model...", command=self.choose).grid(row=0, column=0, sticky="w")
        ttk.Label(form, textvariable=self.path_name, wraplength=300, style="Hint.TLabel").grid(row=0, column=1, sticky="w", padx=10)
        ttk.Label(form, text="Name").grid(row=1, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(form, textvariable=self.name).grid(row=1, column=1, sticky="ew", pady=(12, 0), padx=10)
        ttk.Label(form, text="Description").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(form, textvariable=self.desc).grid(row=2, column=1, sticky="ew", pady=(8, 0), padx=10)
        ttk.Label(form, text="Size").grid(row=3, column=0, sticky="nw", pady=(10, 0))
        box = ttk.Frame(form)
        box.grid(row=3, column=1, sticky="w", pady=(10, 0), padx=10)
        ttk.Radiobutton(box, text="as it is", variable=self.size_mode, value="asis").grid(row=0, column=0, sticky="w")
        row2 = ttk.Frame(box)
        row2.grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Radiobutton(row2, text="fit to", variable=self.size_mode, value="fit").pack(side="left")
        ttk.Entry(row2, textvariable=self.fit, width=6).pack(side="left", padx=6)
        ttk.Label(row2, text="metres (its largest side)").pack(side="left")
        ttk.Label(form, text="Turn").grid(row=4, column=0, sticky="nw", pady=(10, 0))
        box2 = ttk.Frame(form)
        box2.grid(row=4, column=1, sticky="w", pady=(10, 0), padx=10)
        ttk.Combobox(box2, textvariable=self.turn, values=["0", "90", "180", "270"], width=5, state="readonly").pack(side="left")
        ttk.Label(box2, text="degrees", style="Hint.TLabel").pack(side="left", padx=8)
        ttk.Label(form, text="Use Turn if the front of the model faces the wrong way.", style="Hint.TLabel").grid(
            row=5, column=1, sticky="w", padx=10)
        ttk.Checkbutton(form, text="Replace the old one if the name is already used\n(Endless keeps a new version)",
                        variable=self.replace).grid(row=6, column=0, columnspan=2, sticky="w", pady=(12, 0))

        pic = ttk.Frame(m)
        pic.grid(row=2, column=1, sticky="n")
        self.picture_frame = tk.Frame(pic, width=self.pic_w, height=self.pic_h, bg="#e8eaed", highlightthickness=1,
                                      highlightbackground="#c4c7c5")
        self.picture_frame.pack_propagate(False)
        self.picture_frame.pack()
        self.picture_label = tk.Label(self.picture_frame, text="Choose a model to see\na picture of it here.", bg="#e8eaed",
                                      fg="#5f6368", font=(FONT, 10), wraplength=self.pic_w - 30)
        self.picture_label.pack(expand=True, fill="both")
        self.caption = ttk.Label(pic, text="", wraplength=self.pic_w, justify="center", anchor="center", style="Hint.TLabel")
        self.caption.pack(pady=(6, 0), fill="x")

        # go
        self.button = ttk.Button(m, text="Upload", command=self.start, style="Big.TButton")
        self.button.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(14, 4))
        ttk.Label(m, text="Unity will be closed and opened again during the upload: save anything open in Unity first.",
                  wraplength=720, style="Warn.TLabel").grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self.steps = StepBar(m, up.STEP_TITLES if up else ("1", "2", "3", "4", "5"), self.bg)
        self.steps.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        self.result = tk.Label(m, text="", font=(FONT, 13, "bold"), wraplength=740, justify="left", anchor="w", bg=self.bg)
        self.result.grid(row=6, column=0, columnspan=2, sticky="ew")

        # the one step a person must do by hand
        self.callout = tk.Frame(m, bg="#fff4d6", highlightthickness=2, highlightbackground="#e0a100", padx=14, pady=10)
        tk.Label(self.callout, text="Your turn", bg="#fff4d6", fg="#7a4f00", font=(FONT, 11, "bold")).pack(anchor="w")
        tk.Label(self.callout, text="In Unity, sign in to Endless (the Endless window). When you are signed in, press the green button.\n"
                                    "This tool never types your password.", bg="#fff4d6", fg=INK, font=(FONT, 10), justify="left",
                 wraplength=700).pack(anchor="w", pady=(2, 8))
        self.carry = tk.Button(self.callout, text="I have signed in: carry on", font=(FONT, 14, "bold"), bg=GREEN, fg="white",
                               activebackground="#086824", activeforeground="white", relief="flat", cursor="hand2",
                               command=self.carry_on)
        self.carry.pack(fill="x", ipady=8)

        ttk.Checkbutton(m, text="Show details", variable=self.show_details, command=self.toggle_details).grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.log_frame = ttk.Frame(m)
        self.log = tk.Text(self.log_frame, height=9, wrap="word", state="disabled", font=("Consolas", 9), relief="solid", borderwidth=1)
        scroll = ttk.Scrollbar(self.log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # past uploads
        ttk.Label(m, text="Past uploads", style="Section.TLabel").grid(row=10, column=0, columnspan=2, sticky="w", pady=(12, 4))
        table = ttk.Frame(m)
        table.grid(row=11, column=0, columnspan=2, sticky="nsew")
        table.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=("when", "name", "size", "result"), show="headings", height=3, selectmode="browse")
        for col, text, width in (("when", "Date", 150), ("name", "Name", 230), ("size", "Size", 150), ("result", "Result", 160)):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=width, anchor="w")
        self.tree.tag_configure("uploaded", foreground=GREEN)
        self.tree.tag_configure("failed", foreground=RED)
        self.tree.tag_configure("refused", foreground="#8a4b00")
        self.tree.tag_configure("stopped", foreground="#8a4b00")
        tscroll = ttk.Scrollbar(table, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tscroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self.show_entry)
        self.tree.bind("<Double-1>", self.double_click)
        bar = ttk.Frame(m)
        bar.grid(row=12, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.open_log_button = ttk.Button(bar, text="Open log", command=self.open_log)
        self.open_log_button.pack(side="left")
        self.new_version_button = ttk.Button(bar, text="Send a new version", command=self.new_version)
        self.new_version_button.pack(side="left", padx=8)
        self.entry_note = ttk.Label(m, text="", wraplength=740, justify="left", style="Hint.TLabel")
        self.entry_note.grid(row=13, column=0, columnspan=2, sticky="w", pady=(6, 0))
        m.rowconfigure(11, weight=1)

    def toggle_details(self):
        if self.show_details.get():
            self.log_frame.grid(row=9, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
        else:
            self.log_frame.grid_remove()

    # The model and its picture ------------------------------------------------------------------------

    def set_file(self, path):
        self.path.set(str(path))
        self.name.set(pretty_name(path))

    def choose(self):
        picked = filedialog.askopenfilename(title="Choose a 3D model", filetypes=TYPES)
        if picked:
            self.set_file(picked)

    def preview_wanted(self, *_):
        """The model, its size or its turn changed: make a new picture soon (a pause lets a person finish typing)."""
        self.preview_serial += 1   # a picture that is being made right now is for the old form: it will be thrown away
        if self.preview_job is not None:
            self.after_cancel(self.preview_job)
        self.preview_job = self.after(700, self.start_preview)

    def show_picture(self, image, caption):
        """A picture with its caption under it; or, with no picture, the words (or the plain placeholder) in its place."""
        self.picture = image
        if image is None:
            self.picture_label.configure(image="", text=caption or "Choose a model to see\na picture of it here.")
            self.caption.configure(text="", foreground=GREY)
        else:
            self.picture_label.configure(image=image, text="")
            self.caption.configure(text=caption, foreground=INK)

    def start_preview(self):
        # Any picture that is still being made is for an older form: mark it stale first, whatever happens below.
        self.preview_serial += 1
        serial = self.preview_serial
        self.preview_job = None
        if up is None:
            return
        # The picture on show must always belong to what the form says now, so each case below clears it and says why.
        if self.running:
            self.show_picture(None, "A picture is made after the upload is finished.")
            return
        path = self.path.get().strip()
        if not path:
            self.show_picture(None, "")
            return
        if not Path(path).is_file():
            self.show_picture(None, "That model file was not found, so there is no picture. Choose the model again.")
            return
        fit = None
        if self.size_mode.get() == "fit":
            try:
                fit = float(self.fit.get().replace(",", "."))
            except ValueError:
                fit = -1
            if not 0.05 <= fit <= 100:
                self.show_picture(None, "Type a size between 0.05 and 100 metres to see the picture.")
                return
        self.show_picture(None, "Making a picture...")
        if self.preview_busy:
            self.preview_dirty = True   # one Blender at a time: the next picture starts when this one ends
            return
        self.preview_busy = True
        self.preview_count += 1
        folder = PREVIEW_FOLDER / f"{os.getpid()}-{self.preview_count}"
        threading.Thread(target=self.make_picture, args=(serial, path, fit, float(self.turn.get()), folder), daemon=True).start()

    def make_picture(self, serial, model, fit, turn, folder):
        try:
            report = up.run_preview(model, folder / "preview.png", fit=fit, rotate=turn)
            self.lines.put(("picture", (serial, folder, report, None)))
        except up.UploadError as e:
            self.lines.put(("picture", (serial, folder, None, str(e))))
        except Exception as e:   # a picture must never stop an upload
            self.lines.put(("picture", (serial, folder, None, f"No picture could be made ({e}). You can still upload.")))

    def got_picture(self, serial, folder, report, error):
        self.preview_busy = False
        image = None
        if report is not None and serial == self.preview_serial and not self.preview_dirty and not self.running:
            try:
                big = tk.PhotoImage(file=str(folder / "preview.png"))
                ratio = Fraction(self.pic_w, big.width()).limit_denominator(8)   # the picture is made twice as big as it is shown
                image = big.zoom(ratio.numerator).subsample(ratio.denominator)
            except (tk.TclError, ZeroDivisionError):
                error = "The picture could not be shown. You can still upload."
        shutil.rmtree(folder, ignore_errors=True)
        if self.preview_dirty:
            self.preview_dirty = False
            if not self.running:
                self.start_preview()
            return
        if serial != self.preview_serial:
            return   # made for an older form; what is on show now was already cleared or is being made
        if image is not None:
            size = up.size_text(report.get("size_m"))
            self.show_picture(image, f"{size}  (left to right x front to back x height)\nA grey 1.8 m person stands beside it for scale.")
        else:
            self.show_picture(None, error or "No picture could be made. You can still upload.")

    def clean_previews(self):
        """Removes this window's temporary picture folders (and the parent folder when it is empty)."""
        for folder in PREVIEW_FOLDER.glob(f"{os.getpid()}-*"):
            shutil.rmtree(folder, ignore_errors=True)
        try:
            PREVIEW_FOLDER.rmdir()
        except OSError:
            pass

    # Upload ---------------------------------------------------------------------------------------------

    def on_close(self):
        if self.running and not messagebox.askyesno("Send to Endless", "An upload is running. Close anyway?", default="no"):
            return
        if self.running:
            up.record_stopped(self.attempt, up.STOPPED_BY_WINDOW)   # the history keeps what is known so far
        up.release_lock()
        self.clean_previews()
        self.destroy()

    def carry_on(self):
        self.callout.grid_remove()
        self.show_result("Working... please do not close this window.", ok=None)
        self.steps.progress(4)
        self.go.set()

    def ask_sign_in(self, text):
        """Runs in the upload thread: shows the sign-in request and waits for the button."""
        self.go.clear()
        self.lines.put(("signin", text))
        self.go.wait()

    def say(self, text):
        self.lines.put(("log", text))

    def start(self):
        try:
            fit = None
            if self.size_mode.get() == "fit":
                try:
                    fit = float(self.fit.get().replace(",", "."))
                except ValueError:
                    raise up.UploadError("'Fit to' needs a number of metres, like 1 or 2.5.")
            up.check_model_file(self.path.get())
            up.check_name(self.name.get())
        except up.UploadError as e:
            self.show_result(str(e), ok=False)
            return
        self.running = True
        self.attempt = {}
        self.preview_serial += 1   # no picture is made while the upload runs
        if self.preview_job is not None:
            self.after_cancel(self.preview_job)
            self.preview_job = None
        self.preview_dirty = False
        self.show_picture(None, "A picture is made after the upload is finished.")
        self.set_busy(True)
        self.steps.reset()
        self.show_result("Working... please do not close this window.", ok=None)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        args = (self.path.get(), self.name.get(), self.desc.get(), fit, float(self.turn.get()), self.replace.get())
        threading.Thread(target=self.work, args=args, daemon=True).start()

    def set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for b in (self.button, self.open_log_button, self.new_version_button):
            b.configure(state=state)
        if busy:
            self.button.configure(text="Uploading...")
        else:
            self.button.configure(text="Upload")

    def work(self, model, name, desc, fit, turn, replace):
        try:
            line = up.upload(model, name, desc, fit=fit, rotate=turn, update=replace, log=self.say, wait_for_sign_in=self.ask_sign_in, attempt=self.attempt)
            self.lines.put(("ok", line))
        except up.UploadError as e:
            self.lines.put(("fail", "Not sent. " + str(e)))
        except Exception as e:   # never leave the window hanging
            self.lines.put(("fail", f"Not sent. Something unexpected went wrong: {e}"))

    def show_result(self, text, ok):
        self.result.configure(text=text, fg={True: GREEN, False: RED, None: "#444444"}[ok])

    def animate(self):
        self.steps.tick()
        self.after(450, self.animate)

    def pump(self):
        try:
            while True:
                kind, payload = self.lines.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", payload + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                    step = up.step_for_line(payload)
                    if step is not None:
                        self.steps.progress(step)
                elif kind == "signin":
                    self.show_result("Waiting for you: sign in to Endless in Unity.", ok=None)
                    self.steps.progress(3, note="your turn")
                    self.callout.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(8, 0))
                    self.bell()
                    self.lift()
                elif kind == "picture":
                    self.got_picture(*payload)
                else:
                    ok = kind == "ok"
                    self.show_result(payload, ok=ok)
                    self.callout.grid_remove()
                    if ok:
                        self.steps.finish()
                    else:
                        self.steps.fail_current()
                    self.running = False
                    self.set_busy(False)
                    self.reload_history()
                    self.preview_wanted()   # the picture that was put away during the upload comes back
        except queue.Empty:
            pass
        self.after(200, self.pump)

    # Past uploads -----------------------------------------------------------------------------------------

    def reload_history(self):
        if up is None:
            return
        self.history = up.load_history()
        self.tree.delete(*self.tree.get_children())
        for i, e in enumerate(self.history):
            result = RESULT_WORDS.get(e.get("result"), "Failed")
            self.tree.insert("", "end", iid=str(i), tags=(e.get("result") or "failed",),
                             values=(nice_when(e.get("when")), e.get("name", ""), up.size_text(e.get("size_m")) or "-", result))
        self.entry_note.configure(text="" if self.history else "Nothing has been sent yet. Your uploads will be listed here.")
        has = bool(self.history)
        state = "normal" if has and not self.running else "disabled"
        self.open_log_button.configure(state=state)
        self.new_version_button.configure(state=state)
        if has:
            self.tree.selection_set("0")

    def selected(self):
        chosen = self.tree.selection()
        if not chosen:
            return None
        try:
            return self.history[int(chosen[0])]
        except (ValueError, IndexError):
            return None

    def show_entry(self, _event=None):
        e = self.selected()
        if not e:
            return
        lines = []
        if e.get("result") != "uploaded" and e.get("reason"):
            lines.append({"refused": "Why it was refused: ", "stopped": "Why it stopped: "}.get(e.get("result"), "Why it failed: ") + e["reason"])
        elif e.get("description"):
            lines.append("Description: " + e["description"])
        lines.append(entry_facts(e))
        if e.get("model"):
            lines.append("Model: " + e["model"])
        self.entry_note.configure(text="\n".join(l for l in lines if l))

    def open_log(self):
        e = self.selected()
        if not e:
            return
        log = e.get("log")
        if log and Path(log).is_file():
            os.startfile(log)
        elif e.get("result") == "refused":
            messagebox.showinfo("Send to Endless", "This one was refused before anything started, so it has no log.")
        else:
            messagebox.showinfo("Send to Endless", "No log was saved for this one.")

    def double_click(self, event):
        if self.tree.identify_row(event.y):
            self.new_version()

    def new_version(self):
        """Fills the form from the chosen past upload and ticks Replace. The upload itself still decides whether replacing is allowed."""
        e = self.selected()
        if not e or self.running:
            return
        self.path.set(e.get("model", ""))
        self.name.set(e.get("name", ""))
        self.desc.set(e.get("description", ""))
        if e.get("fit"):
            self.size_mode.set("fit")
            self.fit.set(f"{e['fit']:g}")
        else:
            self.size_mode.set("asis")
        turn = str(int(e.get("turn") or 0))
        self.turn.set(turn if turn in ("0", "90", "180", "270") else "0")
        self.replace.set(True)
        if not Path(e.get("model", "")).is_file():
            self.show_result("The model file of that upload is not there any more. Press Choose a model... and pick it again.", ok=False)
        else:
            self.show_result("The form is filled in from that upload. Change what you like, then press Upload.", ok=None)


def show_start_error(error):
    """The window could not start: say so in plain words instead of vanishing."""
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("Send to Endless", f"Send to Endless could not start.\n\n{error}\n\n"
                         "Check that the whole tool folder is still together, then try again.")
    root.destroy()


if __name__ == "__main__":
    try:
        if START_ERROR is not None:
            raise START_ERROR
        try:   # sharp text and picture on a scaled screen (must come before the window is made)
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        atexit.register(up.release_lock)
        App(sys.argv[1] if len(sys.argv) > 1 else None).mainloop()
    except Exception as e:
        show_start_error(e)
