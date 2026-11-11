"""Makes art/test_models/upload_test_crate.blend: a 1 m wooden crate with a plank picture made here (no downloaded models).
Run:  blender -b --factory-startup -P art/test_models/make_test_crate.py
The model is deliberately a little off the ground and off centre, so the uploader's standing and centring are tested too."""
import math
import random
from pathlib import Path

import bpy

OUT = Path(__file__).resolve().parent / "upload_test_crate.blend"
N = 256


def planks():
    """A brown plank picture: 4 horizontal boards with grain, dark gaps, and a bright orange stripe at the top-left corner
    so a flipped or wrong-way picture shows."""
    rnd = random.Random(7)
    px = [0.0] * (N * N * 4)
    for y in range(N):
        board = y * 4 // N
        base = [(0.62, 0.40, 0.20), (0.55, 0.35, 0.17), (0.68, 0.45, 0.24), (0.58, 0.37, 0.19)][board]
        gap = (y * 4 % N) < 6
        for x in range(N):
            grain = 0.08 * math.sin(x * 0.09 + board * 2.0 + math.sin(y * 0.3)) + rnd.uniform(-0.02, 0.02)
            r, g, b = (c + grain for c in base)
            if gap:
                r, g, b = r * 0.25, g * 0.25, b * 0.25
            if x < 24 and y > N - 24:   # top-left in the picture: bright orange marker
                r, g, b = 1.0, 0.45, 0.0
            i = (y * N + x) * 4
            px[i:i + 4] = [r, g, b, 1.0]
    return px


bpy.ops.wm.read_factory_settings(use_empty=True)
img = bpy.data.images.new("planks", N, N)
img.pixels = planks()
img.pack()

mat = bpy.data.materials.new("Planks")
mat.use_nodes = True
nodes = mat.node_tree.nodes
bsdf = next(n for n in nodes if n.type == "BSDF_PRINCIPLED")
tex = nodes.new("ShaderNodeTexImage")
tex.image = img
mat.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])

bpy.ops.mesh.primitive_cube_add(size=1.0, location=(3.0, 2.0, 5.0))
crate = bpy.context.active_object
crate.name = "Crate"
crate.data.materials.append(mat)
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.uv.cube_project(cube_size=1.0)
bpy.ops.object.mode_set(mode="OBJECT")

# a camera and a light, which the tool must drop
bpy.ops.object.camera_add(location=(4, -4, 4))
bpy.ops.object.light_add(type="POINT", location=(2, 2, 6))

bpy.ops.wm.save_as_mainfile(filepath=str(OUT))
print("saved", OUT)
