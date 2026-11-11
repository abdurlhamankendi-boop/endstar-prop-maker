"""Makes a house from the AutoCAD villa pipeline ready for Send to Endless (Blender, no window):

    blender -b --factory-startup --disable-autoexec -P make_showcase_house.py -- IN.glb OUT.blend

The pipeline's villa.glb has parts under the ground (a basement, the pool shells). Send to Endless stands a model's
LOWEST point on the floor, so those parts would lift the whole house 2-4 m into the air. This script:
  - cuts away everything below the bottom of the garden ground (the "terrain" part), so the plot sits flat on the floor
  - names each colour after the part that uses it (walls, glass, water...) instead of mat_0, mat_1...
  - keeps the parts as separate objects (Send to Endless joins them itself), in metres, and saves a .blend
It prints the cut height, the size and the triangle count.
"""
import sys
from pathlib import Path

import bmesh
import bpy
import mathutils

args = sys.argv[sys.argv.index("--") + 1:]
src, out = Path(args[0]), Path(args[1])

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=str(src))
scene = bpy.context.scene
scene.unit_settings.system = "METRIC"
scene.unit_settings.scale_length = 1.0

meshes = [ob for ob in scene.objects if ob.type == "MESH"]
for ob in list(scene.objects):
    if ob.type != "MESH":
        bpy.data.objects.remove(ob, do_unlink=True)


def world_z(ob):
    return [(ob.matrix_world @ mathutils.Vector(c)).z for c in ob.bound_box]


terrain = next((ob for ob in meshes if ob.name.lower().startswith("terrain")), None)
if terrain is None:
    raise SystemExit("no part called terrain: cannot tell where the ground is")
cut = min(world_z(terrain))

removed = []
for ob in meshes:
    # bake the object's place into its points so the cut is in world height
    ob.data.transform(ob.matrix_world)
    ob.matrix_world = mathutils.Matrix.Identity(4)
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    geom = bm.verts[:] + bm.edges[:] + bm.faces[:]
    bmesh.ops.bisect_plane(bm, geom=geom, plane_co=(0, 0, cut), plane_no=(0, 0, 1), clear_inner=True)
    bm.to_mesh(ob.data)
    bm.free()
    if not ob.data.polygons:
        removed.append(ob.name)
        bpy.data.objects.remove(ob, do_unlink=True)

named = set()
for ob in scene.objects:
    if ob.type != "MESH":
        continue
    for slot in ob.material_slots:
        m = slot.material
        if m and m.name.startswith("mat_") and m.name not in named:
            part = ob.name.split(".")[0]
            new = part if part not in bpy.data.materials else f"{part}_{m.name}"
            m.name = new
            named.add(new)

tris = 0
lo = mathutils.Vector((1e9, 1e9, 1e9))
hi = -lo
for ob in scene.objects:
    if ob.type != "MESH":
        continue
    ob.data.calc_loop_triangles()
    tris += len(ob.data.loop_triangles)
    for v in ob.data.vertices:
        lo = mathutils.Vector(map(min, lo, v.co))
        hi = mathutils.Vector(map(max, hi, v.co))
size = hi - lo
out.parent.mkdir(parents=True, exist_ok=True)
bpy.ops.wm.save_as_mainfile(filepath=str(out), compress=True)
print(f"RESULT cut at z={cut:.2f} m; removed parts: {removed or 'none'}; size {size.x:.1f} x {size.y:.1f} x {size.z:.1f} m; "
      f"{tris} triangles; materials: {sorted(m.name for m in bpy.data.materials if m.users)}")
