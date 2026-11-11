# Endstar Prop Maker

Get your own 3D models into Endstar as props, two ways:

- **Send to Endless** (no AI, no code): a small window. Choose a model you made in Blender or found on the web
  (`.blend .fbx .obj .glb .gltf .dae .stl .ply`), give it a name, press Upload. It makes the model real size,
  stands it on the ground, keeps its colours and pictures, and uploads it to your own Endstar account (private).
  Guide: `tools\prop_uploader\README.md`. To try it, send `art\showcase\first_villa.blend` (a villa with a garden
  and a pool) or the 1 m test crate `art\test_models\upload_test_crate.blend`.
- **Ask Claude** (the `endstar-unity` MCP): you describe a prop ("a wooden crate, 1 x 1 x 1 m", "a stone arch two
  cells wide"); Claude builds it in Blender, puts it into the official Endstar SDK in Unity, makes its claimed cells
  and icon, and uploads it.

Then you place it in your level in the Endstar editor like any other prop.

It only uses the official Endstar SDK, through Unity. It never talks to Endless Studios' servers itself and never
opens the game program. Only upload models you made or are allowed to use.

## Send to Endless in short

No Claude, no uv, no extra Python packages. Once:

1. Install **Unity Hub** and **Unity 2022.3.62f2**, then the **Endstar SDK** (links in "What you need", steps 2 and 3).
2. Install **Blender** (made and tested with 5.2; if it is not in the usual folder, the newest Blender found under
   `C:\Program Files\Blender Foundation` is used) and **Python 3** from python.org (tick "Add python.exe to PATH").
3. Open the SDK project in Unity. Menu **Endstar SDK > Show Login**: sign in yourself and tick **Remember Me**.
4. Make **one** prop by hand: menu **Endstar SDK > Prop SDK**, File > New Prop. That makes your props folder, which
   the tool needs (it says so if it is missing).
5. If your SDK folder is not `C:\Endstar-SDK`, or Unity or Blender are somewhere else, open `paths.json` in Notepad
   and put your folders there (one backslash or two both work).

Then each time: double-click `tools\prop_uploader\Send to Endless.cmd` (or drag a model file onto it), check the name,
press Upload. The tool copies all 8 scripts of `unity\Editor` into the SDK project itself, restarts Unity when it needs to and
uploads; you only press **Carry on** if Unity asks you to sign in again.

## What you need (once)

1. **Windows** (the tool drives the Unity window).
2. **Unity Hub**, then **Unity 2022.3.62f2** with the build modules the Endstar wiki lists:
   https://endstar.wiki.gg/wiki/Unity_HUB_Install
3. **The Endstar SDK**: download and open it as the wiki says: https://endstar.wiki.gg/wiki/Endstar_SDK_Install
   Remember the folder you put it in (for example `C:\Endstar-SDK`).
4. **Blender** 4.x or 5.x: https://www.blender.org/download/
5. **Claude Code** (only for Ask Claude): https://claude.com/claude-code
6. **uv** (only for Ask Claude; it runs the MCP's Python): https://docs.astral.sh/uv/getting-started/installation/

## Set up (about 10 minutes)

1. Download this folder (GitHub: Code > Download ZIP, or `git clone`) and put it somewhere, for example
   `C:\endstar-unity-mcp`.
2. Open `.mcp.json` in Notepad and fix the three paths so they match your PC:
   - `ENDSTAR_SDK_PROJECT`: your Endstar SDK folder (the one holding `Assets`)
   - `UNITY_EXE`: usually already right (`C:\Program Files\Unity\Hub\Editor\2022.3.62f2\Editor\Unity.exe`)
   - `BLENDER_EXE`: your `blender.exe` (the version number in the folder name may differ)
   Use double backslashes, as in the file.
3. Install the Unity side: open a terminal in this folder and run
   `uv run --no-project python tools\install_bridge.py`
   It copies all 8 scripts of `unity\Editor` into your SDK project (`Assets\Editor\SendToEndless`).
4. Open the SDK project in Unity (from Unity Hub).
5. In Unity: menu **Endstar SDK > Show Login**, sign in to your Endstar account and tick **Remember Me**.
   Sign in yourself: never give your password to Claude.
6. In Unity, make **one** prop by hand in the Prop SDK window (menu **Endstar SDK > Prop SDK**, File > New Prop).
   That creates your props folder (`Assets\User Data\<your name>\Props`); the tool puts its props there too.
7. Open Claude Code in this folder (`claude` in a terminal here). When it asks, allow the `endstar-unity` MCP server.
8. Ask Claude: **"Check unity_status"**. It should say the Unity window is open and the ping answers.

## Make a prop

Just ask, for example:

> Make an Endstar prop: a wooden crate, 1 x 1 x 1 m, with darker planks on the edges. Upload it.

Claude then: writes the model in `art\blender\make_props.py` -> builds it in Blender (a picture lands in
`art\preview`) -> imports it into Unity -> gives it Endstar materials -> makes the prop (claimed cells, icon) ->
uploads it. It takes a few minutes; the upload alone is 1-3 minutes. Then, in the Endstar editor: Prop Tool ->
**+** -> Search For Prop -> your prop's name.

`art\blender\make_props.py` already holds some examples (an arrow test block, a lamp that starts dark, a pillar,
cars, a traffic cone, a sign, a wall light, a parking bay, a barrier): ask Claude to make one of them first to check
everything works, e.g. "Make and upload the arrow test block". The lamp is the one to look at for glow maps.

## Good to know

- **One cell is 1 metre.** A prop's origin is the middle of its bottom cell. Models are built standing on the
  ground, centred, front facing -Y in Blender.
- **Colours** come from a fixed list (`PALETTE` in `make_props.py`, and the same list in
  `unity\Editor\EndstarBridge.cs`). To add a colour, ask Claude: it adds it to both and restarts Unity.
- **One upload at a time.** The tool waits and says BUSY if Unity is still busy.
- **Unity must stay open** while Claude works. Claude can restart it itself (`unity_restart`), for example after
  changing the Unity scripts; Unity may then ask you to sign in again.
- Big props made on a grid (buildings, walled courtyards) that are an even number of cells wide need the `grid` option
  and a half-cell shift; Claude knows how (see `CLAUDE.md`).
- **Tile sets are not finished.** The bridge has four commands for them (`tileparent`, `tilepick`, `tileeditor`,
  `tileinfo`); they are experiments, so leave them alone.
- Props show as **Read Only** in Unity? Ask Claude to run `unity_restart`.
- The MCP tools do not show up in Claude? In `.mcp.json`, replace `tools/endstar_unity_mcp.py` with its full path.

## Lights and glow maps

Endstar's shader shows a part's **glow map** times its emission colour as light. A script's `Visuals:SetEmissiveColor`
sets that colour on every material of a prop at once, so to make only some parts glow (a bulb but not its holder) the
model needs a glow map: a grey picture where white glows and black does not. Three ways to use it:

- **A material whose picture is also its glow map.** Paint the picture (a window, a sign, a screen) and mark its
  material in Blender with the custom properties `picture` (the file) and `glow` (true). The picture shows as paint,
  and also as the glow map: the bright parts of the picture light up when a script sets a colour.
- **A lamp's glow map.** `art\textures\light_glow.png` (made by `art\light_glow.py`) is a small ready-made map in eight
  bands: a white band for a flame or bulb, a black band for a holder, soft discs for a pool of light, and more. A part's
  UVs point at the band it should look like (`uv_band` in `make_props.py` does it for a whole part). Give the part the
  material `iron` (or `glow_plaster` / `glow_stone` for a pool on a wall): `unity_set_materials` and the upload copy the
  map onto exactly those materials. `glow_lamp` in `make_props.py` is a complete example.
- **Props that start dark.** A material with a glow map starts with its emission colour black: nothing glows until a
  script calls `SetEmissiveColor` on the prop (make it with `kind="scripted"`, so it has Visuals). That is how a lamp can
  be switched on and off, and how a prop can change colour in the game.

Without a glow map a prop simply glows one flat colour, the whole prop together.

## Files

| | |
|---|---|
| `tools\endstar_unity_mcp.py` | the MCP server (14 tools: unity_status, blender_make_models, unity_import_models, unity_set_materials, endstar_make_prop, endstar_prop_info, endstar_set_cells, endstar_nav_test, endstar_export_prop, unity_export_state, endstar_frame_prop, unity_dialogs, unity_restart, unity_editor_log) |
| `unity\Editor\*.cs` | the Unity side: takes commands through files in `unity\inbox` and `unity\outbox` |
| `tools\install_bridge.py` | copies the Unity side into the SDK project |
| `tools\unity_cmd.py` | sends one command by hand (for quick looks) |
| `art\blender\make_props.py` | the Blender models: add your props here |
| `art\light_glow.py`, `art\textures\light_glow.png` | the glow map for lamps and the maths that points a part at it (see "Lights and glow maps") |
| `tests\` | `test_mcp_offline.py` (no Unity needed) and `test_mcp.py` (asks the running Unity, changes nothing) |
| `tools\prop_uploader\` | Send to Endless: the window (`Send to Endless.cmd` / `.pyw`), the pipeline (`upload_prop.py`), the Blender step (`prepare_model.py`), its tests and guide |
| `paths.json` | where your Endstar SDK project, Unity and Blender are (used when no environment variable says otherwise) |
| `art\showcase\` | a showcase villa ready to send, its picture, and the script that made it ready |
| `art\test_models\` | a 1 m test crate and the script that makes it |
| `CLAUDE.md` | instructions Claude reads in this folder |

Tests: `uv run --no-project --with "mcp>=1.10,<2" python -m unittest tests.test_mcp_offline -v`

Made with Claude Code and shared as is.
