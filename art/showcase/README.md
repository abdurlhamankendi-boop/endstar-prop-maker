# Showcase house

`first_villa.blend` is a two-storey villa with a garden, a pool and a boundary wall (24 x 29 x 10.6 m, 5,540 triangles),
designed from a written brief with an AutoCAD drawing pipeline and made ready for **Send to Endless**.
`first_villa_preview.png` is the upload tool's own picture of it (the grey figure is a 1.8 m person).

Upload it: drag `first_villa.blend` onto `tools\prop_uploader\Send to Endless.cmd`, call it for example "Showcase Villa", keep
**Size: as it is** (it is real size already: 1 m is one Endstar cell) and press Upload. Place it on an empty flat area of about
24 x 29 cells (Endstar editor: Props, +, Search For Prop).

`make_showcase_house.py` is how it was made from the pipeline's `villa.glb`: the upload tool stands a model's lowest point on the
floor, so everything under the garden ground (the pool's shell) is cut away first; otherwise the house would float 2 m up.

    blender -b --factory-startup --disable-autoexec -P make_showcase_house.py -- villa.glb first_villa.blend

Limits: the prop gets one box collider round the whole plot, so players walk round the wall but not into the garden or the
house; glass is solid; colours only.
