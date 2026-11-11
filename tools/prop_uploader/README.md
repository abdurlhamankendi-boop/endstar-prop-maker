# Send to Endless

Sends a 3D model (from Blender, or one you found on the web) to your own Endless prop library. No AI inside: plain code.

## What it does

You choose a model, give it a name, press Upload. The tool then:

1. gets the model ready in Blender (no window): real size in metres, standing on the ground, centred, pieces joined, its colour pictures kept;
2. puts it into Unity's Endstar SDK project and makes Endstar materials from the model's own colours and pictures;
3. makes the prop, restarts Unity (this stops Unity freezing), and uploads it with the official Endstar SDK;
4. tells you plainly: `Done: <name> is in your Endless library (private). In the Endstar editor: Props, +, Search For Prop.`

A typical upload takes about 5 to 10 minutes, most of it Unity starting and the SDK upload.

## Install once

- Blender 5.2 (`C:\Program Files\Blender Foundation\Blender 5.2\`).
- Unity 2022.3.62f2 with the Endstar SDK project (`C:\Endstar-SDK`).
- Open that project in Unity once and sign in to Endless there (tick Remember Me). The tool never types a password.
- Make one prop by hand in the Prop SDK window (Endstar SDK > Prop SDK, File > New Prop): that makes your props folder (`Assets\User Data\<your name>\Props`), where the tool puts its props.
- Python 3 with tkinter (from python.org, tick "Add python.exe to PATH"). `Send to Endless.cmd` checks this and says so if it is missing. No other Python packages, no uv, no Claude.
- Folders somewhere else? Put them in `paths.json` next to `README.md` (open it in Notepad; one backslash or two both work). A Blender that is not in the usual folder is looked for under `C:\Program Files\Blender Foundation` (the newest one).
- The tool checks paths.json, Blender, Unity and the SDK project folder first, and names any that is missing and where it looked. It copies its own helper scripts into the SDK project.

## Use it

- Double-click `Send to Endless.cmd` (or `Send to Endless.pyw`), press **Choose a model...**; or drag the model file onto `Send to Endless.cmd`.
- A small **picture** of the model appears on the right (it takes a few seconds; the window stays usable). It shows the model as it will be sent, on a one-metre grid, with a grey 1.8 m person beside it, and its size in metres under it. It changes when you change **Size** or **Turn**. If the picture cannot be made, the window says so in one line and you can still upload.
- Check the **Name** (the tool suggests one) and type a short **Description**.
- **Size**: "as it is", or "fit to N metres" (the largest side becomes N metres). One metre is one Endstar cell, and a person is about 1.8 m.
- **Turn**: use it if the front of the model faces the wrong way.
- **Unity will be closed and opened again during the upload: save anything open in Unity first.**
- Press **Upload**. Five steps light up as it goes: 1 Get it ready in Blender, 2 Put it into Unity, 3 Make the prop, 4 Restart Unity and sign in, 5 Upload. Each is waiting, working, done or failed. Tick **Show details** to see the full text log. Wait for the big green or red line. Closing the window mid-upload asks "An upload is running. Close anyway?".
- **Signing in after the restart.** Unity restarts before the export. If you ticked **Remember Me** when you signed in, Unity signs you in again by itself when it reopens (seen 10 Nov): just press **Carry on** when the window asks. Without Remember Me the sign-in is lost on every restart (seen 9 Nov) and you sign in again. The window then says "Unity has restarted. In Unity, sign in to Endless (the Endless window), then press Carry on." A yellow box called "Your turn" appears (the window also beeps). Sign in, then press the green **I have signed in: carry on** button. On the command line it waits for Enter; `--signed-in` skips that wait. If Endless still says "Not authenticated", it asks once more and retries only the upload step. The tool never types or stores your password.
- A name already in use is refused. Tick "Replace the old one" to send a new version, but only of a prop this tool sent itself under that same name; your other props are never replaced.
- The tool never overwrites a model file it did not make. It keeps a list of what it sent in `uploads.json` next to the tool. A name with no English letters or digits (Greek, Japanese, emoji) is stored under `prop_<8 letters>`; two names that would share a file name ("Chair" and "Chair!") are refused, so pick another.

## Past uploads

Every upload attempt, finished or failed, is written to `history.json` next to the tool (newest first) and shown in the **Past uploads** table at the bottom of the window: date, name, size and result. Click a row to see its description, how many minutes it took, triangles, colours and, for a failure, the one sentence saying why. The model's full path is there too.

- The Result column says **Uploaded**, **Failed** (it started and went wrong), **Refused** (a check said no before anything started, such as a name already used, another upload running, a missing tool or model file: no log is made) or **Stopped** (the window was closed or Ctrl+C pressed during the upload: what was known so far is kept). A row shows "took N min (plus M min waiting for you to sign in)": the waiting for your sign-in is counted apart (`sign_in_wait_minutes`). No picture is made while an upload runs; it comes back after. `logs\` and `history.json` hold local paths and are not committed (listed in `.gitignore`).
- **Open log** opens the full text log of that attempt (saved in the `logs` folder).
- **Send a new version** fills the form from that row (model, name, description, size, turn) and ticks "Replace the old one". A double-click on a row does the same. You still press Upload; the usual guard decides whether replacing is allowed.
- The command line writes the same history. If `history.json` is damaged, the tool keeps it aside as `history.json.damaged`, starts a new one, and the upload carries on. A history that cannot be written never stops an upload.

From a command line: `python upload_prop.py model.glb "Prop Name" "description" --fit 2 --update`.

Models that work: `.blend .fbx .obj .glb .gltf .dae .stl .ply`.

## What it cannot do

- Placing the prop in a level is by hand (Endstar editor: Props, +, Search For Prop).
- It sends the model's colour picture only. Normal, roughness and see-through maps are dropped, and the tool says so. Rigs and animation are dropped; the model is sent in its saved pose.
- Models with many hundreds of thousands of triangles will be slow in a level; the tool warns about that.
- It cannot tell which way a model's front faces: use Turn.
- Needs this PC's Unity and Blender; no other uploading route exists on purpose.

## Privacy and rules

The prop stays private in your own account. Nothing here publishes, and nothing here talks to Endless's servers: the only way up is the Endstar SDK's own export inside Unity. Only upload models you made or may use: Endless's terms say "Your Content is original to you" (ask before publishing anything you did not make).

## If something goes wrong

Every failure says in one sentence what went wrong and what to do. If Unity is frozen, close Unity and try again.

## Files

`prepare_model.py` (Blender step, and the picture: add `--preview PICTURE.png` and it makes the picture instead of the FBX) · `history.json` and `logs\` (past uploads) · `upload_prop.py` (the whole pipeline) · `Send to Endless.pyw` / `.cmd` (the window) · `test_upload_prop.py` (offline tests: `python -m unittest test_upload_prop.py`) · the Unity side is `unity\Editor\PropMaterials.cs` (bridge command `propmaterials`). Working files go in `work\`; the list of what was sent is `uploads.json`. If an upload crashes, its lock file (`work\.running`, holding the process id) is noticed as stale and removed by the next upload. Blender runs with `--disable-autoexec`, so scripts inside a downloaded .blend never run. A .blend's own Unit Scale (for example centimetres) is converted to real metres and shown in the report. The test model is `art\test_models\upload_test_crate.blend` (made by `make_test_crate.py`).
