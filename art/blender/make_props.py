"""Builds example props in Blender by script, exports each one as FBX for the
Endstar SDK (Unity) and renders a preview picture next to a 1.8 m person on a 1 m grid
(one Endstar cell is one metre).

    blender -b --factory-startup -P art/blender/make_props.py -- arrow pillar ...

Outputs: art/export/<Name>.fbx and art/preview/<name>.png. Models face -Y in Blender (the
"front" view) with the pivot on the ground at the middle of the footprint.
"""
import math
import sys
from pathlib import Path

import bmesh
import bpy
import mathutils

ROOT = Path(__file__).resolve().parents[2]
EXPORT = ROOT / "art" / "export"
PREVIEW = ROOT / "art" / "preview"

# Bright, chunky colours to sit next to Endstar's own props.
PALETTE = {
    "signal_red": (0.78, 0.08, 0.06, 1),
    "white": (0.92, 0.92, 0.9, 1),
    "glass": (0.04, 0.09, 0.14, 1),
    "metal": (0.32, 0.33, 0.36, 1),
    "dark": (0.05, 0.05, 0.06, 1),
    "concrete": (0.55, 0.55, 0.53, 1),
    "concrete_dark": (0.38, 0.38, 0.37, 1),
    "hazard_yellow": (0.95, 0.72, 0.05, 1),
    "cone_orange": (0.98, 0.33, 0.03, 1),
    "sign_green": (0.05, 0.45, 0.2, 1),
    "car_blue": (0.1, 0.32, 0.7, 1),
    "car_teal": (0.05, 0.55, 0.5, 1),
    "car_yellow": (0.95, 0.75, 0.12, 1),
    "tyre": (0.04, 0.04, 0.045, 1),
    "light": (1.0, 0.95, 0.75, 1),
    # Dark stone, plaster, wood and iron tones for scenes lit by lamps (the same names and values as the bridge's palette).
    # `iron` is nearly black: a part on it shows no glow until a script lights it (see glow_lamp below).
    "stone": (0.045, 0.034, 0.020, 1),
    "stone_dark": (0.022, 0.016, 0.010, 1),
    "stone_floor": (0.030, 0.026, 0.021, 1),
    "plaster": (0.050, 0.044, 0.032, 1),
    "glow_plaster": (0.050, 0.044, 0.032, 1),   # a lamp's pool on a wall: plaster plus the glow map in Unity
    "glow_stone": (0.045, 0.034, 0.020, 1),     # the same on stone
    "wood": (0.117, 0.048, 0.018, 1),
    "iron": (0.034, 0.034, 0.04, 1),
}


# Helpers ------------------------------------------------------------------------------------

def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    for block in (bpy.data.meshes, bpy.data.materials, bpy.data.cameras, bpy.data.lights):
        for item in list(block):
            block.remove(item)


def new_material(name):
    mat = bpy.data.materials.new(name)
    try:
        mat.use_nodes = True   # always on in Blender 5, needed in 4.x
    except (AttributeError, TypeError):
        pass
    return mat


def material(name, roughness=0.6, metallic=0.0, emission=0.0):
    if name in bpy.data.materials:
        return bpy.data.materials[name]
    mat = new_material(name)
    colour = PALETTE[name]
    mat.diffuse_color = colour
    nodes = mat.node_tree.nodes
    bsdf = nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = colour
    bsdf.inputs["Roughness"].default_value = roughness
    bsdf.inputs["Metallic"].default_value = metallic
    if emission:
        bsdf.inputs["Emission Color"].default_value = colour
        bsdf.inputs["Emission Strength"].default_value = emission
    return mat


def finish(obj, mat, smooth=False, bevel=0.0, subsurf=0):
    obj.data.materials.append(mat)
    if bevel:
        mod = obj.modifiers.new("Bevel", "BEVEL")
        mod.width = bevel
        mod.segments = 2
        mod.limit_method = "ANGLE"
    if subsurf:
        mod = obj.modifiers.new("Subsurf", "SUBSURF")
        mod.levels = subsurf
        mod.render_levels = subsurf
    if smooth:
        for poly in obj.data.polygons:
            poly.use_smooth = True
    return obj


def box(name, size, loc, mat, rot=(0, 0, 0), bevel=0.03, smooth=False):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc, rotation=rot)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return finish(obj, mat, smooth=smooth, bevel=bevel)


def cylinder(name, radius, depth, loc, mat, rot=(0, 0, 0), verts=20, radius2=None, bevel=0.02):
    if radius2 is None:
        bpy.ops.mesh.primitive_cylinder_add(vertices=verts, radius=radius, depth=depth, location=loc, rotation=rot)
    else:
        bpy.ops.mesh.primitive_cone_add(vertices=verts, radius1=radius, radius2=radius2, depth=depth,
                                        location=loc, rotation=rot)
    obj = bpy.context.active_object
    obj.name = name
    return finish(obj, mat, smooth=True, bevel=bevel)


def sphere(name, scale, loc, mat, segments=24, rings=12):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=segments, ring_count=rings, radius=1, location=loc)
    obj = bpy.context.active_object
    obj.name = name
    obj.scale = scale
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return finish(obj, mat, smooth=True)


def tube(name, a, b, radius, mat, verts=12):
    """A rod from point a to point b."""
    ax, ay, az = a
    bx, by, bz = b
    dx, dy, dz = bx - ax, by - ay, bz - az
    length = math.sqrt(dx * dx + dy * dy + dz * dz)
    mid = ((ax + bx) / 2, (ay + by) / 2, (az + bz) / 2)
    bpy.ops.mesh.primitive_cylinder_add(vertices=verts, radius=radius, depth=length, location=mid)
    obj = bpy.context.active_object
    obj.name = name
    # Point the cylinder's Z axis along a->b.
    obj.rotation_mode = "QUATERNION"
    obj.rotation_quaternion = mathutils.Vector((dx, dy, dz)).to_track_quat("Z", "Y")
    return finish(obj, mat, smooth=True, bevel=radius * 0.3)


def join(name, objects, origin=(0, 0, 0)):
    """Applies modifiers and joins objects into one mesh named `name` with its origin at `origin`."""
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    bpy.ops.object.convert(target="MESH")
    if len(objects) > 1:
        bpy.ops.object.join()
    obj = bpy.context.active_object
    obj.name = name
    obj.data.name = name
    bpy.context.scene.cursor.location = origin
    bpy.ops.object.origin_set(type="ORIGIN_CURSOR")
    bpy.context.scene.cursor.location = (0, 0, 0)
    return obj


def triangles(objects):
    total = 0
    deps = bpy.context.evaluated_depsgraph_get()
    for obj in objects:
        mesh = obj.evaluated_get(deps).to_mesh()
        total += sum(len(p.vertices) - 2 for p in mesh.polygons)
        obj.evaluated_get(deps).to_mesh_clear()
    return total


def export(objects, name):
    EXPORT.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    bpy.ops.export_scene.fbx(
        filepath=str(EXPORT / f"{name}.fbx"), use_selection=True, object_types={"MESH", "EMPTY"},
        apply_unit_scale=True, apply_scale_options="FBX_SCALE_ALL", axis_forward="-Z", axis_up="Y",
        bake_space_transform=True, use_mesh_modifiers=True, mesh_smooth_type="FACE", add_leaf_bones=False)


def preview(name, objects, distance, height=None, angle=-35):
    """Renders the piece on a 1 m checker floor next to a 1.8 m person (not exported)."""
    PREVIEW.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    bpy.context.view_layer.update()   # moved objects' matrices are stale until this runs
    corners = [obj.matrix_world @ mathutils.Vector(c) for obj in objects for c in obj.bound_box]
    lo = [min(c[i] for c in corners) for i in range(3)]
    hi = [max(c[i] for c in corners) for i in range(3)]
    # Floor with a 1 m checker.
    bpy.ops.mesh.primitive_plane_add(size=60, location=(0, 0, 0))
    floor = bpy.context.active_object
    mat = new_material("PreviewFloor")
    nodes = mat.node_tree.nodes
    checker = nodes.new("ShaderNodeTexChecker")
    checker.inputs["Scale"].default_value = 30.0   # 60 m plane / 30 = 2 m per checker pair = 1 m squares
    checker.inputs["Color1"].default_value = (0.62, 0.62, 0.6, 1)
    checker.inputs["Color2"].default_value = (0.5, 0.5, 0.48, 1)
    mat.node_tree.links.new(checker.outputs["Color"], nodes["Principled BSDF"].inputs["Base Color"])
    floor.data.materials.append(mat)
    # A 1.8 m person beside the piece.
    px = hi[0] + 1.2
    bpy.ops.mesh.primitive_cylinder_add(vertices=16, radius=0.28, depth=1.3, location=(px, lo[1] + 0.5, 0.65))
    body = bpy.context.active_object
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.25, location=(px, lo[1] + 0.5, 1.55))
    head = bpy.context.active_object
    pmat = new_material("PreviewPerson")
    pmat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.15, 0.45, 0.95, 1)
    for p in (body, head):
        p.data.materials.append(pmat)
        for poly in p.data.polygons:
            poly.use_smooth = True
    # Camera at a three-quarter view from the front-left, looking at the middle of the piece.
    centre = mathutils.Vector(((lo[0] + hi[0]) / 2 + 0.6, (lo[1] + hi[1]) / 2, (hi[2]) * 0.4))
    a = math.radians(angle)
    h = height if height is not None else distance * 0.55
    cam_loc = centre + mathutils.Vector((math.sin(a) * distance, -math.cos(a) * distance, h))
    bpy.ops.object.camera_add(location=cam_loc)
    cam = bpy.context.active_object
    cam.rotation_mode = "QUATERNION"
    cam.rotation_quaternion = (centre - cam_loc).to_track_quat("-Z", "Y")
    cam.data.lens = 40
    scene.camera = cam
    bpy.ops.object.light_add(type="SUN", location=(0, 0, 10), rotation=(math.radians(50), math.radians(10), math.radians(-40)))
    bpy.context.active_object.data.energy = 3.5
    world = scene.world or bpy.data.worlds.new("World")
    scene.world = world
    world.color = (0.55, 0.62, 0.72)
    if world.node_tree:
        bg = world.node_tree.nodes.get("Background")
        if bg:
            bg.inputs["Color"].default_value = (0.55, 0.62, 0.72, 1)
            bg.inputs["Strength"].default_value = 0.9
    for engine in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH"):
        try:
            scene.render.engine = engine
            break
        except TypeError:
            continue
    # Blender's default AgX view fades colours; Standard shows them as Unity will.
    scene.view_settings.view_transform = "Standard"
    scene.render.resolution_x = 1100
    scene.render.resolution_y = 800
    scene.render.filepath = str(PREVIEW / f"{name}.png")
    bpy.ops.render.render(write_still=True)
    print(f"PREVIEW {scene.render.filepath} engine={scene.render.engine}")


# Pieces -------------------------------------------------------------------------------------


def text_mesh(name, text, size, loc, mat, rot=(math.radians(90), 0, 0), depth=0.012):
    """Flat 3D lettering in Blender's built-in font, turned into a mesh."""
    bpy.ops.object.text_add(location=loc, rotation=rot)
    obj = bpy.context.active_object
    obj.name = name
    obj.data.body = text
    obj.data.size = size
    obj.data.extrude = depth
    obj.data.align_x = "CENTER"
    obj.data.align_y = "CENTER"
    bpy.ops.object.convert(target="MESH")
    obj = bpy.context.active_object
    obj.data.materials.append(mat)
    return obj


def pillar():
    """A 0.8 m square concrete pillar, 3 m tall, with a hazard
    band at the bottom and a blue "A1" plate on two sides."""
    concrete, yellow, dark = material("concrete", 0.85), material("hazard_yellow", 0.6), material("dark", 0.7)
    blue, white = material("car_blue", 0.5), material("white", 0.5)
    parts = [box("Column", (0.8, 0.8, 3.0), (0, 0, 1.5), concrete, bevel=0.04)]
    for i, z in enumerate((0.075, 0.225, 0.375, 0.525)):
        parts.append(box(f"Band{i}", (0.82, 0.82, 0.15), (0, 0, z), yellow if i % 2 == 0 else dark, bevel=0.01))
    for side in (-1, 1):
        # s = +1 when the reader's right is +X (the -Y face), -1 on the +Y face.
        s, y = -side, side * 0.43
        parts.append(box(f"Plate{side}", (0.5, 0.03, 0.4), (0, side * 0.41, 1.9), blue, bevel=0.015))
        parts.append(text_mesh(f"LabelA{side}", "A", 0.28, (-0.08 * s, y, 1.9), white,
                               rot=(math.radians(90), 0, 0 if side < 0 else math.radians(180))))
        # The font's "1" is a plain bar that reads as "I", so the 1 is built with a flag and a base.
        parts.append(box(f"OneStem{side}", (0.045, 0.012, 0.2), (0.09 * s, y, 1.9), white, bevel=0))
        parts.append(box(f"OneFlag{side}", (0.075, 0.012, 0.035), ((0.09 - 0.035) * s, y, 1.972), white, bevel=0,
                         rot=(0, math.radians(-35 * s), 0)))
        parts.append(box(f"OneBase{side}", (0.12, 0.012, 0.03), (0.09 * s, y, 1.815), white, bevel=0))
    return "ConcretePillar", [join("ConcretePillar", parts)], 6


def car(colour):
    """A small cartoon car, 4 m long and 1.8 m wide (fits a 2 x 4 cell bay), nose toward -Y."""
    def build():
        paint, glass, tyre = material(colour, 0.35, 0.1), material("glass", 0.08, 0.3), material("tyre", 0.8)
        metal, light, red, dark = material("metal", 0.4, 0.6), material("light", 0.3, 0, 2.0), \
            material("signal_red", 0.4), material("dark", 0.7)
        white = material("white", 0.5)
        parts = [box("Body", (1.8, 3.8, 0.7), (0, 0, 0.62), paint, bevel=0.2),
                 box("Cabin", (1.56, 2.1, 0.56), (0, 0.25, 1.22), glass, bevel=0.16),
                 box("Roof", (1.42, 1.88, 0.08), (0, 0.25, 1.48), paint, bevel=0.03)]
        for side in (-1, 1):
            parts.append(box(f"Pillar{side}", (0.07, 0.12, 0.56), (side * 0.78, 0.25, 1.22), paint, bevel=0.02))
            for y in (-1.22, 1.22):
                parts.append(cylinder(f"Wheel{side}{y}", 0.36, 0.28, (side * 0.82, y, 0.36), tyre,
                                      rot=(0, math.radians(90), 0), verts=20, bevel=0.06))
                parts.append(cylinder(f"Hub{side}{y}", 0.17, 0.04, (side * 0.97, y, 0.36), metal,
                                      rot=(0, math.radians(90), 0), verts=16, bevel=0.01))
            parts.append(sphere(f"Headlight{side}", (0.18, 0.06, 0.12), (side * 0.58, -1.9, 0.72), light, 16, 8))
            parts.append(box(f"Taillight{side}", (0.32, 0.06, 0.14), (side * 0.6, 1.9, 0.74), red, bevel=0.02))
        parts.append(box("FrontBumper", (1.7, 0.16, 0.18), (0, -1.9, 0.38), dark, bevel=0.06))
        parts.append(box("RearBumper", (1.7, 0.16, 0.18), (0, 1.9, 0.38), dark, bevel=0.06))
        parts.append(box("Plate", (0.5, 0.03, 0.14), (0, 1.96, 0.56), white, bevel=0.01))
        name = "Car" + colour.split("_")[1].capitalize()
        return name, [join(name, parts)], 9
    return build


def cone():
    """A 0.7 m traffic cone with two white bands on a square foot."""
    orange, white, dark = material("cone_orange", 0.5), material("white", 0.5), material("dark", 0.7)
    r_bottom, r_top, base_h, h = 0.24, 0.045, 0.05, 0.68

    def radius(z):
        return r_bottom - (r_bottom - r_top) * (z - base_h) / h

    parts = [box("Foot", (0.56, 0.56, base_h), (0, 0, base_h / 2), dark, bevel=0.02),
             cylinder("Cone", r_bottom, h, (0, 0, base_h + h / 2), orange, radius2=r_top, verts=24, bevel=0.01)]
    for i, (z0, z1) in enumerate(((0.36, 0.44), (0.52, 0.58))):
        parts.append(cylinder(f"Band{i}", radius(z0) + 0.006, z1 - z0, (0, 0, (z0 + z1) / 2), white,
                              radius2=radius(z1) + 0.006, verts=24, bevel=0))
    return "TrafficCone", [join("TrafficCone", parts)], 3


def exit_sign():
    """A green standing sign, 2.4 m tall, reading EXIT with an up arrow, on both faces."""
    green, white, metal = material("sign_green", 0.5), material("white", 0.5), material("metal", 0.4, 0.6)
    dark = material("dark", 0.7)
    parts = [cylinder("Post", 0.05, 2.0, (0, 0, 1.0), metal, verts=12),
             box("Foot", (0.4, 0.4, 0.06), (0, 0, 0.03), dark, bevel=0.02),
             box("Panel", (1.3, 0.06, 0.5), (0, 0, 2.2), green, bevel=0.03)]
    for side in (-1, 1):
        # Side -1 faces -Y (the front), side +1 faces +Y; on each face the arrow sits on the
        # reader's left and the word on the right.
        y = side * 0.035
        rot_z = 0 if side < 0 else math.radians(180)
        parts.append(text_mesh(f"Text{side}", "EXIT", 0.24, (0.12 * -side, y, 2.2), white,
                               rot=(math.radians(90), 0, rot_z)))
        ax = 0.42 * side
        parts.append(box(f"ArrowStem{side}", (0.07, 0.02, 0.2), (ax, y, 2.13), white, bevel=0))
        parts.append(triangle_plate(f"ArrowHead{side}", [(ax - 0.13, 2.22), (ax + 0.13, 2.22), (ax, 2.37)], y, 0.02,
                                    white))
    return "ExitSign", [join("ExitSign", parts)], 5


def triangle_plate(name, points, y, thickness, mat):
    """A flat triangle standing in the XZ plane at depth y, `thickness` thick."""
    h = thickness / 2
    verts = [(x, y - h, z) for x, z in points] + [(x, y + h, z) for x, z in points]
    faces = [(0, 2, 1), (3, 4, 5), (0, 1, 4, 3), (1, 2, 5, 4), (2, 0, 3, 5)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(mat)
    return obj


def strip_light():
    """A wall-mounted strip light, 1.2 m wide. An Endstar prop sits centred in its cell, so the
    fixture is built with its back on the cell's back face (y = +0.5) and 0.8 m above the cell
    floor: placed in the cell next to a wall, facing away from it, it hangs on that wall."""
    metal, light = material("metal", 0.4, 0.6), material("light", 0.2, 0, 4.0)
    parts = [box("Housing", (1.2, 0.18, 0.14), (0, 0.41, 0.8), metal, bevel=0.03),
             box("Tube", (1.1, 0.1, 0.06), (0, 0.38, 0.72), light, bevel=0.02)]
    return "StripLight", [join("StripLight", parts)], 3


def parking_bay():
    """White lines for one parking bay, 2.5 m wide and 5 m deep, open at the front."""
    white = material("white", 0.7)
    parts = [box("LineLeft", (0.12, 5.0, 0.012), (-1.25, 0, 0.006), white, bevel=0),
             box("LineRight", (0.12, 5.0, 0.012), (1.25, 0, 0.006), white, bevel=0),
             box("LineBack", (2.62, 0.12, 0.012), (0, 2.5, 0.006), white, bevel=0)]
    return "ParkingBay", [join("ParkingBay", parts)], 7


def barrier():
    """A 2 m concrete road barrier (wide foot, narrow top) with two yellow reflector strips."""
    concrete, yellow = material("concrete", 0.85), material("hazard_yellow", 0.4)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0.4))
    obj = bpy.context.active_object
    obj.name = "Barrier"
    obj.scale = (2.0, 0.6, 0.8)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    for v in obj.data.vertices:
        if v.co.z > 0:
            v.co.y *= 0.4
    finish(obj, concrete, bevel=0.03)
    parts = [obj]
    for side in (-1, 1):
        parts.append(box(f"Strip{side}", (1.6, 0.02, 0.1), (0, side * 0.135, 0.62), yellow, bevel=0,
                         rot=(math.radians(-side * 14), 0, 0)))
    return "ConcreteBarrier", [join("ConcreteBarrier", parts)], 4


def arrow_test():
    """A one-cell test block: an arrow on top pointing to its front, FRONT on the front face and R
    on its own right side. Placed in Endstar it shows at once whether direction, size and
    mirroring survived the trip through Unity."""
    concrete, yellow, red = material("concrete", 0.85), material("hazard_yellow", 0.5), material("signal_red", 0.4)
    parts = [box("Block", (0.96, 0.96, 0.96), (0, 0, 0.48), concrete, bevel=0.03),
             box("ArrowStem", (0.14, 0.42, 0.02), (0, 0.12, 0.965), yellow, bevel=0)]
    verts = [(-0.27, -0.08, 0.955), (0.27, -0.08, 0.955), (0, -0.42, 0.955),
             (-0.27, -0.08, 0.975), (0.27, -0.08, 0.975), (0, -0.42, 0.975)]
    faces = [(0, 2, 1), (3, 4, 5), (0, 1, 4, 3), (1, 2, 5, 4), (2, 0, 3, 5)]
    mesh = bpy.data.meshes.new("ArrowHead")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    head = bpy.data.objects.new("ArrowHead", mesh)
    bpy.context.collection.objects.link(head)
    head.data.materials.append(yellow)
    parts.append(head)
    # FRONT faces -Y; the block's own right side is -X (it faces -Y), so R goes there.
    parts.append(text_mesh("FrontText", "FRONT", 0.2, (0, -0.485, 0.5), red))
    parts.append(text_mesh("RightText", "R", 0.36, (-0.485, 0, 0.5), red,
                           rot=(math.radians(90), 0, math.radians(-90))))
    return "ArrowTest", [join("ArrowTest", parts)], 4


# Glow example ---------------------------------------------------------------------------------
# A part's glow comes from the glow map (art/textures/light_glow.png, made by art/light_glow.py): a grey picture in eight bands, bottom
# to top, where white glows and black does not. A part's UVs say which band it sits on (v is the middle of the band, u the place along it).
# A lamp needs only two: FLAME (all white) and DARK (all black). Every part uses the one material `iron`, which is nearly black until a
# script gives the prop a colour with Visuals:SetEmissiveColor; the map then decides which parts show that colour.
V_FLAME, V_DARK = 0.0625, 0.1875


def uv_band(obj, v, u=0.5):
    """Points every corner of `obj` at the glow map's band at height v, so the whole part shows that band's brightness."""
    layer = obj.data.uv_layers.active or obj.data.uv_layers.new(name="UVMap")
    for loop in obj.data.loops:
        layer.data[loop.index].uv = (u, v)
    return obj


def glow_lamp():
    """A standing lamp that starts dark: a 1.5 m post on a round foot with a ball on top, 1.8 m tall in all. The foot and the post sit on the
    glow map's DARK band and the ball on its FLAME band, so a script's Visuals:SetEmissiveColor lights the ball and leaves the rest black.
    Make it with endstar_make_prop(..., kind="scripted") so a script can reach its Visuals."""
    iron = material("iron", 0.5, 0.3)
    foot = uv_band(cylinder("Foot", 0.28, 0.08, (0, 0, 0.04), iron, verts=24, bevel=0.01), V_DARK)
    post = uv_band(cylinder("Post", 0.04, 1.5, (0, 0, 0.83), iron, verts=12, bevel=0), V_DARK)
    ball = uv_band(sphere("Ball", (0.2, 0.2, 0.2), (0, 0, 1.6), iron, 24, 12), V_FLAME)
    return "GlowLamp", [join("GlowLamp", [foot, post, ball])], 5


PIECES = {
    "arrow": arrow_test,
    "glow_lamp": glow_lamp,
    "pillar": pillar,
    "car_blue": car("car_blue"),
    "car_teal": car("car_teal"),
    "car_yellow": car("car_yellow"),
    "cone": cone,
    "sign": exit_sign,
    "light": strip_light,
    "bay": parking_bay,
    "barrier": barrier,
}


def upload_manifest(model, objs):
    """The material list of a built model for the bridge command `propmaterials <Model>|<this file>`, with its pictures beside it:
    tools/prop_uploader/work/<Model>/REPORT.json (written for every model main() builds, before its export: a builder may swap in
    preview-only materials after it). With it, a model whose material names are not in the bridge's own palette gets Endstar
    materials of its own. Entries: a colour (Blender's linear RGBA; white for a picture, so the picture shows as painted), a picture
    for _Albedo, `glow` (the picture is the glow map too) and `emission` (a glow map alone: art/textures/light_glow.png for `iron` and
    every `glow_*` material, as the palette command gives them, so a script's SetEmissiveColor lights the parts whose UVs point at it: see uv_band)."""
    import json
    import shutil
    work = ROOT / "tools" / "prop_uploader" / "work" / model
    (work / "textures").mkdir(parents=True, exist_ok=True)
    entries = {}
    for obj in objs:
        if obj.type != "MESH":
            continue
        for mat in obj.data.materials:
            if mat is None or mat.name in entries:
                continue
            picture = mat.get("picture")
            colour = PALETTE.get(mat.name, tuple(mat.diffuse_color))
            entry = {"name": mat.name, "color": [1.0, 1.0, 1.0, 1.0] if picture else [float(c) for c in colour], "texture": "",
                     "glow": bool(mat.get("glow", False)), "emission": ""}
            if picture:
                shutil.copy2(picture, work / "textures" / Path(picture).name)
                entry["texture"] = "textures/" + Path(picture).name
            if mat.name == "iron" or mat.name.startswith("glow_"):
                shutil.copy2(ROOT / "art" / "textures" / "light_glow.png", work / "textures" / "light_glow.png")
                entry["emission"] = "textures/light_glow.png"
            entries[mat.name] = entry
    (work / "REPORT.json").write_text(json.dumps({"materials": list(entries.values())}, indent=1), encoding="utf-8")


def lineup():
    """Every example piece in a row in one picture, for judging them side by side."""
    clear_scene()
    objects, x = [], 0.0
    for key in ("pillar", "car_blue", "car_teal", "cone", "sign", "barrier", "bay"):
        _, objs, _ = PIECES[key]()
        corners = [o.matrix_world @ mathutils.Vector(c) for o in objs for c in o.bound_box]
        width = max(c.x for c in corners) - min(c.x for c in corners)
        for o in objs:
            o.location.x += x + width / 2
        objects += objs
        x += width + 0.8
    light_objs = PIECES["light"]()[1]
    for o in light_objs:
        o.location = (0.4, -0.4, 2.5)   # on the front face of the pillar
    objects += light_objs
    for o in objects:
        o.location.x -= x / 2
    preview("lineup", objects, distance=23, height=8, angle=-12)


def main():
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else list(PIECES)
    for key in args:
        if key == "lineup":
            lineup()
            continue
        clear_scene()
        name, objects, distance = PIECES[key]()
        print("TRIS", name, triangles(objects))
        upload_manifest(name, objects)   # before export: a piece's own export wrapper may swap in preview-only materials after it
        export(objects, name)
        if not getattr(PIECES[key], "no_preview", False):
            preview(key, objects, distance=distance)



# Your own pieces, optional: every art/blender/pieces/*.py runs here, in this script's own names, so it can use box(), cylinder(),
# material(), uv_band(), join() and the rest, and registers itself with PIECES["key"] = builder. One file a prop. This package
# has no such folder, so the loop finds nothing until you make one.
for _piece in sorted((ROOT / "art" / "blender" / "pieces").glob("*.py")):
    exec(compile(_piece.read_text(encoding="utf-8"), str(_piece), "exec"), globals())

main()
