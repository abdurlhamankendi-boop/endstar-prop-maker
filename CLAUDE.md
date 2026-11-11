# Endstar Prop Maker

This folder makes Endstar props for the person using it: a model in Blender, then the official Endstar SDK in
Unity (claimed cells, icon), then an upload to their Endstar account, through the `endstar-unity` MCP
(`.mcp.json`, `tools\endstar_unity_mcp.py`). README.md is the setup guide for the person; this file is for you.

## Hard rules

- Endstar levels and assets live on Endless Studios' servers. Never call their backend yourself, never decompile
  or open the game program or the SDK's DLLs: their terms forbid it. Only the official SDK, inside Unity, uploads.
- The person signs in to Endstar inside Unity (Endstar SDK > Show Login, Remember Me). Never type a password.
- Export one prop at a time.

## Making a prop

1. Add a function to `art\blender\make_props.py` that builds the model from the helpers there (`box`,
   `cylinder`, `sphere`, `tube`, `join`, `material`) and returns `(Name, [objects], camera distance)`; add it
   to `PIECES`. One Blender unit = 1 m = 1 Endstar cell. Model standing on the ground (z = 0), centred on the
   origin, front facing -Y. Colours only from `PALETTE` (to add one, add it to PALETTE and to `Palette` in
   `unity\Editor\EndstarBridge.cs` with the same name and value, then unity_restart).
2. `blender_make_models([key])` builds `art\export\<Name>.fbx` and `art\preview\<key>.png`: look at the picture.
3. `unity_import_models`, then `unity_set_materials` (maps every model's colours; running it twice is fine).
4. `endstar_make_prop(model=Name, prop_name=..., description=...)`: waits for the claimed cells and ends with
   "ready <name> <n> cells". `kind="scripted"` for props a Lua script moves (BasicProp + Visuals).
   `collider="each"` gives every mesh object its own box collider (for walls, stairs: one object per box).
5. `endstar_prop_info` to check the cells, then `endstar_export_prop(prop_name)`: waits for the upload and answers
   UPLOADED / FAILED / BUSY. After the first upload the person finds it in the Endstar editor: Prop Tool -> + ->
   Search For Prop.

## Facts that cost time to learn

- A prop's origin is the middle of its bottom cell: endstar_make_prop lowers the model 0.5 m (`lift`).
- Unity gives each mesh object one box collider around it, so a slope must be steps (one object each), and an
  opening (a door, a gate) must not sit inside any object's box.
- Big props built on a cell grid that are an even number of cells wide: move the model half a cell in x and y in
  Blender and make it with `grid=True` (keeps the SDK's half-cell X/Z offsets off). Otherwise the claimed cells
  can come out half a cell off. Claimed cells are stored in the profile's `.asset` (`propLocationOffsets`).
- After editing `unity\Editor\*.cs`, call unity_restart: reloading scripts in a running editor leaves the SDK
  cut off from Endstar's servers (props show as Read Only). The person may have to sign in again.
- A background Unity can idle for minutes; the MCP nudges it while it waits. unity_dialogs confirms the SDK's
  "Export Success!" box and lists other pop-ups.
- The bridge refuses commands older than 150 s; a command that times out is cancelled, never run late.
- `tools\unity_cmd.py <command>` is for quick looks only (ping, propinfo, exportstate): it does not wait.
- Unity's "Prop Asset Created" log line (the upload's end) names no prop, so keep exports one at a time.
- Glow: `Visuals:SetEmissiveColor` sets the emission colour on every material of a prop, so which parts glow is decided
  by the glow map (`art\textures\light_glow.png`, made by `art\light_glow.py`; README "Lights and glow maps"). Parts that
  should glow on a script's word use the material `iron` (or `glow_*`) with UVs on the map's bands (`uv_band`,
  `glow_lamp` in `make_props.py`), and the prop is made with `kind="scripted"`. A material with `picture` and `glow`
  custom properties uses its picture as its own glow map.
- Tile sets are not finished: the bridge commands `tileparent`, `tilepick`, `tileeditor` and `tileinfo` are experiments.

Tests: `uv run --no-project --with "mcp>=1.10,<2" python -m unittest tests.test_mcp_offline -v` (no Unity) and
`uv run --no-project --with "mcp>=1.10,<2" python tests\test_mcp.py` (the running Unity, read-only).
