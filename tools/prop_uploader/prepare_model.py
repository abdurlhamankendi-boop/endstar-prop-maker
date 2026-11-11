"""Gets any 3D model ready for an Endstar prop. Run by Blender with no window:

    "C:\\Program Files\\Blender Foundation\\Blender 5.2\\blender.exe" -b --factory-startup --disable-autoexec -P prepare_model.py -- IN OUT.fbx REPORT.json [--fit METRES] [--rotate DEGREES]

IN is a .blend or a .fbx .obj .glb .gltf .dae .stl .ply file. What it does, in this order, with no guessing in silence:
  - removes cameras and lights; bakes modifiers and curves into plain meshes; drops rigs and empties (it says so)
  - applies every transform and joins the pieces into ONE mesh
  - makes it real size: one Blender unit is 1 m (one Endstar cell). Over 60 m or under 3 cm it is rescaled and says so;
    --fit METRES makes the largest side that many metres
  - stands it on the ground (lowest point z = 0) and centres it on the middle of its footprint
  - writes the model's colour pictures next to the FBX (textures\\<material>.png) and exports the FBX with them embedded
  - writes REPORT.json: size in metres, triangles, materials (colour, picture), warnings

Preview only (what the window uses to show a small picture before uploading; it makes no FBX and sends nothing):
    blender -b --factory-startup --disable-autoexec -P prepare_model.py -- IN REPORT.json --preview PICTURE.png [--fit METRES] [--rotate DEGREES]
It runs exactly the same preparation, then renders the model on a ground grid with a 1.8 m grey stand-in person beside it.

The report's "materials" list is what the Unity bridge command `propmaterials` reads.
"""
import json
import math
import re
import sys
import traceback
from pathlib import Path

import bpy
import mathutils

MAX_SIZE = 60.0      # metres: bigger than this is rescaled
MIN_SIZE = 0.03      # metres: smaller than this is rescaled
MAX_SPAN_CELLS = 100  # a level fits 101 cells; stay inside
BIG_TRIS = 30000
MAX_TEXTURE = 2048
SUPPORTED = {".blend", ".fbx", ".obj", ".glb", ".gltf", ".dae", ".stl", ".ply"}

warnings = []
notes = []


def warn(text):
    warnings.append(text)
    print("WARNING:", text)


def note(text):
    notes.append(text)
    print("NOTE:", text)


def parse_args(argv):
    """Returns (IN, OUT.fbx, REPORT.json, flags). In preview mode there is no FBX: OUT.fbx is None."""
    args = argv[argv.index("--") + 1:] if "--" in argv else []
    flags, rest, i = {}, [], 0
    while i < len(args):
        if args[i] in ("--fit", "--rotate") and i + 1 < len(args):
            flags[args[i][2:]] = float(args[i + 1])
            i += 2
        elif args[i] == "--preview" and i + 1 < len(args):
            flags["preview"] = Path(args[i + 1]).resolve()
            i += 2
        else:
            rest.append(args[i])
            i += 1
    if flags.get("preview"):
        if len(rest) != 2:
            raise SystemExit("usage: -- IN REPORT.json --preview PICTURE.png [--fit METRES] [--rotate DEGREES]")
        return Path(rest[0]).resolve(), None, Path(rest[1]).resolve(), flags
    if len(rest) != 3:
        raise SystemExit("usage: -- IN OUT.fbx REPORT.json [--fit METRES] [--rotate DEGREES]")
    return Path(rest[0]).resolve(), Path(rest[1]).resolve(), Path(rest[2]).resolve(), flags


# Loading ---------------------------------------------------------------------------------------

def run_import(candidates, **kwargs):
    for dotted in candidates:
        group, _, name = dotted.partition(".")
        op = getattr(getattr(bpy.ops, group, None), name, None)
        if op is None:
            continue
        try:
            op.poll()
        except Exception:
            continue
        return op(**kwargs)
    raise RuntimeError("this Blender has no importer for the file type")


def load(path):
    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        raise RuntimeError(f"'{ext}' files are not supported (use .blend .fbx .obj .glb .gltf .dae .stl .ply)")
    if ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=str(path))
        return
    bpy.ops.wm.read_factory_settings(use_empty=True)
    f = str(path)
    if ext == ".fbx":
        run_import(["import_scene.fbx", "wm.fbx_import"], filepath=f)
    elif ext == ".obj":
        run_import(["wm.obj_import", "import_scene.obj"], filepath=f)
    elif ext in (".glb", ".gltf"):
        run_import(["import_scene.gltf"], filepath=f)
    elif ext == ".dae":
        run_import(["wm.collada_import"], filepath=f)
    elif ext == ".stl":
        run_import(["wm.stl_import", "import_mesh.stl"], filepath=f)
    elif ext == ".ply":
        run_import(["wm.ply_import", "import_mesh.ply"], filepath=f)


# Cleaning ----------------------------------------------------------------------------------------

def deselect_all():
    bpy.context.view_layer.update()
    for o in list(bpy.context.scene.objects):
        try:
            o.select_set(False)
        except RuntimeError:
            pass


def bake_to_meshes():
    """Every visible mesh-like object becomes a plain mesh (modifiers applied, curves and text converted); everything
    else is deleted. Returns the list of mesh objects, in world space after the caller clears parents."""
    scene = bpy.context.scene
    deps = bpy.context.evaluated_depsgraph_get()
    keep, hidden, dropped = [], 0, {}
    for obj in list(scene.objects):
        if obj.type in ("CAMERA", "LIGHT"):
            dropped["camera/light"] = dropped.get("camera/light", 0) + 1
            continue
        if obj.type in ("MESH", "CURVE", "SURFACE", "META", "FONT"):
            if obj.hide_render or not obj.visible_get():
                hidden += 1
                continue
            keep.append(obj)
        elif obj.type == "ARMATURE":
            dropped["rig"] = dropped.get("rig", 0) + 1
        elif obj.type != "EMPTY":
            dropped[obj.type.lower()] = dropped.get(obj.type.lower(), 0) + 1
    if hidden:
        warn(f"{hidden} hidden object(s) were left out of the prop.")
    if dropped.get("rig"):
        warn("The model has a rig (bones): it is sent in the pose it was saved in, with no animation.")
    for kind, count in dropped.items():
        if kind not in ("camera/light", "rig"):
            warn(f"{count} {kind} object(s) cannot be sent and were left out.")
    baked = []
    for obj in keep:
        try:
            ev = obj.evaluated_get(deps)
            mesh = bpy.data.meshes.new_from_object(ev, preserve_all_data_layers=True, depsgraph=deps)
        except Exception as e:
            warn(f"'{obj.name}' could not be turned into a mesh and was left out ({e}).")
            continue
        if len(mesh.polygons) == 0:
            bpy.data.meshes.remove(mesh)
            continue
        new = bpy.data.objects.new(obj.name, mesh)
        scene.collection.objects.link(new)
        new.matrix_world = obj.matrix_world.copy()
        baked.append(new)
    # Everything the scene had before goes away; only the baked copies stay.
    for obj in list(scene.objects):
        if obj not in baked:
            bpy.data.objects.remove(obj, do_unlink=True)
    return baked


def join_all(meshes):
    deselect_all()
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.object.make_single_user(object=True, obdata=True)
    if len(meshes) > 1:
        bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active
    obj.name = "prop"
    return obj


def apply_transforms(obj):
    deselect_all()
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)


def bounds(obj):
    pts = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
    lo = mathutils.Vector([min(p[i] for p in pts) for i in range(3)])
    hi = mathutils.Vector([max(p[i] for p in pts) for i in range(3)])
    return lo, hi


def vertex_bounds(obj):
    vs = [obj.matrix_world @ v.co for v in obj.data.vertices]
    lo = mathutils.Vector([min(v[i] for v in vs) for i in range(3)])
    hi = mathutils.Vector([max(v[i] for v in vs) for i in range(3)])
    return lo, hi


def scale_by(obj, factor):
    obj.scale = (factor, factor, factor)
    apply_transforms(obj)


def choose_rescale(longest):
    """The factor for a model that is absurdly big or tiny, and what it supposes. Returns (factor, why)."""
    if longest > MAX_SIZE:
        for factor, unit in ((0.01, "centimetres"), (0.001, "millimetres"), (0.1, "decimetres")):
            if 0.2 <= longest * factor <= 30:
                return factor, f"it looked like it was made in {unit}"
        return 2.0 / longest, "it was far too big to guess its unit"
    for factor, unit in ((100.0, "centimetres"), (1000.0, "millimetres"), (10.0, "decimetres")):
        if 0.2 <= longest * factor <= 30:
            return factor, f"it looked like it was made in {unit}"
    return 1.0 / longest, "it was too small to guess its unit"


def make_real_size(obj, fit):
    lo, hi = vertex_bounds(obj)
    size = hi - lo
    longest = max(size)
    if longest <= 0:
        raise RuntimeError("the model has no size (all its points are in one place)")
    if fit:
        factor = fit / longest
        scale_by(obj, factor)
        note(f"Made its largest side {fit:g} m as asked (it was {longest:.3g} m).")
    elif longest > MAX_SIZE or longest < MIN_SIZE:
        factor, why = choose_rescale(longest)
        scale_by(obj, factor)
        warn(f"The model was {longest:.3g} m on its largest side, so it was resized to {longest * factor:.3g} m ({why}). "
             "If that is wrong, choose 'fit to ... metres'.")
    lo, hi = vertex_bounds(obj)
    if max(hi - lo) > MAX_SPAN_CELLS:
        raise RuntimeError(f"the model is {max(hi - lo):.0f} m wide; a level fits only about {MAX_SPAN_CELLS} m. Choose 'fit to ... metres'")


def stand_and_centre(obj):
    lo, hi = vertex_bounds(obj)
    obj.location = (-(lo.x + hi.x) / 2, -(lo.y + hi.y) / 2, -lo.z)
    apply_transforms(obj)


# Materials -----------------------------------------------------------------------------------------

def safe_name(text, used):
    base = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_") or "material"
    name, n = base, 2
    while name.lower() in used:   # Windows file names ignore case; `used` holds lower-case stems
        name = f"{base}_{n}"
        n += 1
    used.add(name.lower())
    return name


def find_image(socket):
    """The image node feeding a socket, looking through simple nodes (mix, colour adjust). Returns (image, complex)."""
    seen, stack, complex_ = set(), [socket], False
    while stack:
        s = stack.pop()
        for link in s.links:
            node = link.from_node
            if node.name in seen:
                continue
            seen.add(node.name)
            if node.type == "TEX_IMAGE" and node.image:
                return node.image, complex_
            complex_ = True
            for inp in node.inputs:
                if inp.is_linked:
                    stack.append(inp)
    return None, complex_


def principled(mat):
    if not mat.node_tree:
        return None
    for node in mat.node_tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    return None


def save_image(image, path):
    """Writes an image as a PNG with its own pixels (no colour-view change), at most MAX_TEXTURE wide."""
    img = image
    if image.size[0] == 0 or image.size[1] == 0:
        raise RuntimeError("the picture is empty or its file is missing")
    if max(image.size) > MAX_TEXTURE:
        img = image.copy()
        k = MAX_TEXTURE / max(image.size)
        img.scale(max(1, int(image.size[0] * k)), max(1, int(image.size[1] * k)))
        warn(f"A picture of {image.size[0]} x {image.size[1]} was made smaller (at most {MAX_TEXTURE} pixels a side).")
    old_path, old_fmt = img.filepath_raw, img.file_format
    img.filepath_raw = str(path)
    img.file_format = "PNG"
    img.save()
    img.filepath_raw, img.file_format = old_path, old_fmt
    if img is not image:
        bpy.data.images.remove(img)


def process_materials(obj, out_dir):
    """Gives every material a safe unique name and writes its colour picture. Returns the manifest list."""
    mesh = obj.data
    if not mesh.materials or all(m is None for m in mesh.materials):
        mat = bpy.data.materials.new("plain")
        mat.diffuse_color = (0.6, 0.6, 0.6, 1)
        mesh.materials.clear()
        mesh.materials.append(mat)
        warn("The model has no colours at all; it was given plain grey.")
    else:
        for i, m in enumerate(mesh.materials):
            if m is None:
                mat = bpy.data.materials.new("plain")
                mat.diffuse_color = (0.6, 0.6, 0.6, 1)
                mesh.materials[i] = mat
                warn("Part of the model had no material; it was given plain grey.")
    # drop material slots no face uses, so they are not sent
    deselect_all()
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    used_idx = {p.material_index for p in mesh.polygons}
    unused = [i for i in range(len(mesh.materials)) if i not in used_idx]
    if unused:
        bpy.ops.object.material_slot_remove_unused()
    tex_dir = out_dir / "textures"
    names, items, pictures = set(), [], {}
    for slot in obj.material_slots:
        mat = slot.material
        new = safe_name(mat.name, names)
        entry = {"name": new, "color": [0.8, 0.8, 0.8, 1.0], "texture": "", "texture_from": ""}
        bsdf = principled(mat)
        if bsdf is None:
            c = list(mat.diffuse_color)
            entry["color"] = [c[0], c[1], c[2], 1.0]
            warn(f"Material '{mat.name}' is not a standard material; only its base colour was used.")
        else:
            c = bsdf.inputs["Base Color"].default_value
            entry["color"] = [c[0], c[1], c[2], 1.0]
            sock = bsdf.inputs["Base Color"]
            if sock.is_linked:
                image, complex_ = find_image(sock)
                if image is None:
                    warn(f"Material '{mat.name}' makes its colour with a node setup that has no picture; it was given plain grey.")
                    entry["color"] = [0.5, 0.5, 0.5, 1.0]
                else:
                    if complex_:
                        warn(f"Material '{mat.name}' mixes its picture with other nodes; only the picture was used.")
                    key = image.name
                    if key not in pictures:
                        tex_dir.mkdir(parents=True, exist_ok=True)
                        file_name = safe_name(Path(image.name).stem, {Path(v).stem.lower() for v in pictures.values() if v}) + ".png"
                        try:
                            save_image(image, tex_dir / file_name)
                            pictures[key] = file_name
                        except Exception as e:
                            warn(f"The picture of material '{mat.name}' could not be saved ({e}); it was left plain.")
                            pictures[key] = ""
                    if pictures[key]:
                        entry["texture"] = "textures/" + pictures[key]
                        entry["texture_from"] = image.name
                        entry["color"] = [1.0, 1.0, 1.0, 1.0]
                        if not mesh.uv_layers:
                            warn(f"Material '{mat.name}' has a picture but the model has no UV map, so the picture will not show right.")
            for label, key in (("normal/bump", "Normal"), ("rough/metal", "Roughness")):
                if bsdf.inputs[key].is_linked:
                    note(f"'{mat.name}' also has a {label} map; only the colour picture goes to Endless.")
            alpha = bsdf.inputs["Alpha"]
            if alpha.is_linked or alpha.default_value < 0.99:
                warn(f"Material '{mat.name}' is see-through; Endless props are solid, so it will look solid.")
        mat.name = new
        items.append(entry)
    return items


def count_triangles(obj):
    obj.data.calc_loop_triangles()
    return len(obj.data.loop_triangles)


# The preview picture ---------------------------------------------------------------------------------------

PERSON_HEIGHT = 1.8
PICTURE_SIZE = (720, 480)   # the window shows it at half size (360 x 240), which keeps the edges smooth


def flat_material(name, colour):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (colour[0], colour[1], colour[2], 1.0)
    return mat


def show_colours(obj, materials):
    """Workbench shows a material's viewport colour, not its node colour: copy the real colours over, and make each
    picture the active one so it shows."""
    by_name = {m["name"]: m for m in materials}
    for mat in obj.data.materials:
        entry = by_name.get(mat.name if mat else "")
        if not entry:
            continue
        c = entry["color"]
        mat.diffuse_color = (c[0], c[1], c[2], 1.0)
        if entry["texture"] and mat.node_tree:
            for node in mat.node_tree.nodes:
                if node.type == "TEX_IMAGE" and node.image and node.image.name == entry["texture_from"]:
                    mat.node_tree.nodes.active = node
                    break


def add_mesh(name, verts, faces, material):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def add_ground(half):
    """A flat floor with a one-metre grid (one Endstar cell) drawn on it."""
    floor = flat_material("floor", (1.0, 1.0, 1.0))
    lines = flat_material("grid", (0.62, 0.68, 0.76))
    add_mesh("floor", [(-half, -half, -0.004), (half, -half, -0.004), (half, half, -0.004), (-half, half, -0.004)],
             [(0, 1, 2, 3)], floor)
    w = 0.012 * max(1.0, half / 6)
    verts, faces = [], []
    for k in range(-half, half + 1):
        for a, b in (((k - w, -half, -0.002), (k + w, half, -0.002)), ((-half, k - w, -0.002), (half, k + w, -0.002))):
            base = len(verts)
            verts += [(a[0], a[1], a[2]), (b[0], a[1], a[2]), (b[0], b[1], a[2]), (a[0], b[1], a[2])]
            faces.append((base, base + 1, base + 2, base + 3))
    add_mesh("grid", verts, faces, lines)


def add_person(x, y):
    """A plain grey stand-in person, 1.8 m tall: a body and a head."""
    import bmesh
    grey = flat_material("person", (0.62, 0.64, 0.70))
    mesh = bpy.data.meshes.new("person")
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, cap_ends=True, segments=24, radius1=0.24, radius2=0.17, depth=1.45,
                          matrix=mathutils.Matrix.Translation((x, y, 0.725)))
    bmesh.ops.create_uvsphere(bm, u_segments=24, v_segments=14, radius=0.15,
                              matrix=mathutils.Matrix.Translation((x, y, PERSON_HEIGHT - 0.15)))
    bm.to_mesh(mesh)
    bm.free()
    mesh.materials.append(grey)
    obj = bpy.data.objects.new("person", mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def render_preview(obj, materials, out_png):
    """Renders the prepared model (standing at the origin) beside a 1.8 m person, from a three-quarter view."""
    scene = bpy.context.scene
    show_colours(obj, materials)
    lo, hi = vertex_bounds(obj)
    px = lo.x - 0.55 - 0.24
    add_person(px, 0.0)
    half = int(math.ceil(max(abs(px) + 1.5, hi.x + 1.5, abs(lo.y) + 1.5, hi.y + 1.5, 3)))
    add_ground(half)
    corners = [mathutils.Vector((x, y, z)) for x in (min(lo.x, px - 0.24), hi.x) for y in (min(lo.y, -0.24), max(hi.y, 0.24))
               for z in (0.0, max(hi.z, PERSON_HEIGHT))]
    centre = sum(corners, mathutils.Vector()) / len(corners)
    direction = mathutils.Vector((1.0, -1.25, 0.9)).normalized()
    rot = (-direction).to_track_quat("-Z", "Y")
    right, up = rot @ mathutils.Vector((1, 0, 0)), rot @ mathutils.Vector((0, 1, 0))
    us, vs = [(c - centre).dot(right) for c in corners], [(c - centre).dot(up) for c in corners]
    shift = right * ((max(us) + min(us)) / 2) + up * ((max(vs) + min(vs)) / 2)
    aspect = PICTURE_SIZE[0] / PICTURE_SIZE[1]
    cam_data = bpy.data.cameras.new("cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = max(max(us) - min(us), (max(vs) - min(vs)) * aspect) * 1.18
    cam_data.clip_start, cam_data.clip_end = 0.1, 2000
    cam = bpy.data.objects.new("cam", cam_data)
    scene.collection.objects.link(cam)
    cam.location = centre + shift + direction * 400
    cam.rotation_mode = "QUATERNION"
    cam.rotation_quaternion = rot
    scene.camera = cam
    world = bpy.data.worlds.new("sky")
    world.color = (0.93, 0.95, 0.98)
    scene.world = world
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x, scene.render.resolution_y = PICTURE_SIZE
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.filepath = str(out_png)
    try:
        scene.view_settings.view_transform = "Standard"
    except TypeError:
        pass
    shading = scene.display.shading
    shading.light = "STUDIO"
    shading.color_type = "TEXTURE"
    shading.show_object_outline = True
    shading.show_shadows = True
    scene.display.render_aa = "16"
    out_png.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.render.render(write_still=True)
    if not out_png.is_file():
        raise RuntimeError("the picture was not written")


# Main ---------------------------------------------------------------------------------------------

def export_fbx(obj, out):
    deselect_all()
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.export_scene.fbx(
        filepath=str(out), use_selection=True, object_types={"MESH"}, apply_unit_scale=True,
        apply_scale_options="FBX_SCALE_ALL", axis_forward="-Z", axis_up="Y", bake_space_transform=True,
        use_mesh_modifiers=True, mesh_smooth_type="FACE", add_leaf_bones=False,
        path_mode="COPY", embed_textures=True)


def main():
    src, out_fbx, report_path, flags = parse_args(sys.argv)
    report = {"ok": False, "input": str(src), "warnings": warnings, "notes": notes}
    try:
        preview = flags.get("preview")
        out_dir = (out_fbx or report_path).parent
        out_dir.mkdir(parents=True, exist_ok=True)
        load(src)
        unit_scale = float(bpy.context.scene.unit_settings.scale_length or 1.0)
        report["unit_scale"] = unit_scale
        meshes = bake_to_meshes()
        if not meshes:
            raise RuntimeError("no solid shape was found in the file (only cameras, lights, rigs or hidden things)")
        deselect_all()
        for o in meshes:
            o.parent = None
        obj = join_all(meshes)
        if flags.get("rotate"):
            obj.rotation_euler[2] += math.radians(flags["rotate"])
        apply_transforms(obj)
        if abs(unit_scale - 1.0) > 1e-9:
            # the file counts in its own units (scale_length metres each): make the numbers real metres
            scale_by(obj, unit_scale)
            bpy.context.scene.unit_settings.scale_length = 1.0
            note(f"The file's unit scale is {unit_scale:g} (one unit = {unit_scale:g} m), so its size was converted to real metres.")
        make_real_size(obj, flags.get("fit"))
        stand_and_centre(obj)
        lo, hi = vertex_bounds(obj)
        size = hi - lo
        tris = count_triangles(obj)
        if tris > BIG_TRIS:
            warn(f"The model has {tris:,} triangles, which is a lot for a game prop; it may be slow in a level.")
        materials = process_materials(obj, out_dir)
        if preview:
            render_preview(obj, materials, preview)
        else:
            export_fbx(obj, out_fbx)
        report.update({
            "ok": True, "fbx": str(out_fbx) if out_fbx else "", "preview": str(preview or ""), "size_m": [round(size.x, 3), round(size.y, 3), round(size.z, 3)],
            "triangles": tris, "lift": -0.5, "materials": materials,
            "textured_materials": sum(1 for m in materials if m["texture"]),
        })
        print(f"{'PREVIEW' if preview else 'PREPARED'} size {size.x:.2f} x {size.y:.2f} x {size.z:.2f} m, {tris} triangles, {len(materials)} materials "
              f"({report['textured_materials']} with pictures)")
    except SystemExit:
        raise
    except Exception as e:
        traceback.print_exc()
        report["error"] = str(e) or e.__class__.__name__
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not report["ok"]:
        print("FAILED:", report.get("error"))
        sys.exit(1)


main()
