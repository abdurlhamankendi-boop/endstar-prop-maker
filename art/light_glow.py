"""The glow map for light props, and the maths that puts a light's parts on it. Plain Python, no Blender: a Blender script can import it
for UVs, and

    python art/light_glow.py

writes art/textures/light_glow.png (128 x 64, RGB: equal grey in all three, but for the fire's band, which is coloured).

Why a map: the Endstar shader (Endless_Shader_NoFade) shows  Emissive Map x _EmissionColor  as the glow, and the map is white
when nothing is set. A script's Visuals:SetEmissiveColor sets _EmissionColor on EVERY material of the prop's mesh, so one colour
drives every part of a light together: the glass, its pool of light on the wall, the flame. The map is what tells the parts apart:
glass bright, arm and holder black, pool soft. Give every part of a light the one material `iron` (nearly black until the script
gives it a colour), or `glow_plaster` / `glow_stone` for a pool on a wall (the wall's own colour plus this map), and point the
part's UVs at the band that suits it.

The map is eight bands of 8 rows, bottom to top (v = 0 is the bottom row):

    band 0, v 0.0625  FLAME   white, a flame or a bulb
    band 1, v 0.1875  DARK    black, a holder, a cage, an arm
    band 2, v 0.3125  POOL    left to right: bright to dark, for a disc of light on the floor
    band 3, v 0.4375  WASH    the same for a disc on the wall behind a flame
    band 4, v 0.5625  GLASS   a globe's frosted glass: u is the distance from the glow's middle inside the globe, GLASS_NEAR at
                              u = U_MIDDLE, GLASS_FAR at U_RIM: brightest low in the globe, dimmer towards its neck
    band 5, v 0.6875  GPOOL   a globe's warm pool on the wall behind it: point-light falloff (GPOOL_GAP / distance)^3, its
                              faint tail taken to exactly 0 at the disc's edge GPOOL_RADIUS
    band 6, v 0.8125  LIT     the light a lamp throws on the floor, the walls and the ceiling: u is how much light, as lit_u
                              says: LIT_PEAK at U_MIDDLE, nothing at U_RIM. A square law, so a straight line along u, which is
                              what a tile's corners give, looks like a soft fall of light and not a ramp
    band 7, v 0.9375  FIRE    fire's own colours: u is heat, white-hot at U_MIDDLE through yellow, orange and red to nearly
                              black at U_RIM. The only band with colour in it: a part on it is given white by its script, and
                              shows these

In the pool, wash and gpool bands u is the distance from the middle of the disc: U_MIDDLE at the middle, U_RIM at the
edge, and black from U_RIM on. The ends are kept off 0 and 1 because the texture repeats: at u = 1 the game would blend in
the bright first column. Import it with the defaults (sRGB on): the grey levels below are written for that.

Without the map assigned, nothing breaks: the whole light just glows one flat colour (a hard disc on the floor).
"""
import math
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PNG = ROOT / "art" / "textures" / "light_glow.png"

WIDTH, HEIGHT = 128, 64
BANDS = 8
V_FLAME, V_DARK, V_POOL, V_WASH, V_GLASS, V_GPOOL = (0.0625, 0.1875, 0.3125, 0.4375, 0.5625, 0.6875)   # band middles
V_LIT = 0.8125
V_FIRE = 0.9375
FIRE_RAMP = ((0.0, (1.0, 0.93, 0.70)), (0.18, (1.0, 0.78, 0.28)), (0.42, (1.0, 0.45, 0.06)), (0.68, (0.80, 0.16, 0.015)), (0.88, (0.32, 0.035, 0.006)),
             (1.0, (0.03, 0.002, 0.0)))     # (heat, linear colour): how the fire goes from its hot heart to the dark tips of its licks
LIT_PEAK = 0.85                                                 # brightness of a surface right by the bulb, flame = 1.0
U_MIDDLE, U_RIM = 0.012, 0.94                                   # where a disc's middle and its edge sit along a band
POOL_PEAK, WASH_PEAK = 0.55, 0.50                               # brightness at the middle of each disc, flame = 1.0
POOL_RADIUS, WASH_RADIUS = 1.4, 0.75                            # metres
GLASS_NEAR, GLASS_FAR = 0.0505, 0.0745                          # metres from the glow's middle to the globe's nearest and farthest glass
GLASS_RADIUS, GLASS_FLOOR = 0.075, 0.42                         # exp(-(d / radius)^2) lifted to floor, as the picture
GPOOL_GAP, GPOOL_RADIUS, GPOOL_PEAK, GPOOL_TAIL = 0.133, 0.47, 0.35, 0.02   # bulb's distance from the wall, disc radius,
                                                                # brightness at its middle, the tail taken to 0


def smoothstep(a, b, x):
    t = min(1.0, max(0.0, (x - a) / (b - a)))
    return t * t * (3 - 2 * t)


def profile(u, peak, power):
    """How bright a disc is at texture position u: `peak` in the middle, soft fall, exactly 0 from U_RIM on."""
    return peak * (1.0 - smoothstep(U_MIDDLE, U_RIM, u)) ** power


def glass_brightness(d):
    """The globe's frosted glass d metres from the glow's middle, 1.0 at the nearest glass."""
    def at(x):
        return GLASS_FLOOR + (1 - GLASS_FLOOR) * math.exp(-(x / GLASS_RADIUS) ** 2)
    return at(d) / at(GLASS_NEAR)


def glass_u(d):
    """The u for glass d metres from the glow's middle."""
    return U_MIDDLE + (U_RIM - U_MIDDLE) * min(1.0, max(0.0, (d - GLASS_NEAR) / (GLASS_FAR - GLASS_NEAR)))


def gpool_brightness(r):
    """The globe's pool on the wall r metres from its middle (straight behind the bulb): exactly 0 from GPOOL_RADIUS on."""
    fall = (1.0 + (r / GPOOL_GAP) ** 2) ** -1.5
    edge = (1.0 + (GPOOL_RADIUS / GPOOL_GAP) ** 2) ** -1.5
    tail = max(GPOOL_TAIL, edge)
    return GPOOL_PEAK * max(0.0, (fall - tail) / (1.0 - tail))


def glow(u, v):
    """The map's brightness (0 to 1, linear) at a point; the same numbers the PNG holds. What a UV picks."""
    band = min(BANDS - 1, int(v * BANDS))
    if band == 0:
        return 1.0
    if band == 1:
        return 0.0
    if band == 2:
        return profile(u, POOL_PEAK, 1.3)
    if band == 3:
        return profile(u, WASH_PEAK, 1.6)
    t = min(1.0, max(0.0, (u - U_MIDDLE) / (U_RIM - U_MIDDLE)))
    if band == 4:
        return glass_brightness(GLASS_NEAR + t * (GLASS_FAR - GLASS_NEAR))
    if band == 5:
        return 0.0 if u >= U_RIM else gpool_brightness(t * GPOOL_RADIUS)
    if band == 6:
        return 0.0 if u >= U_RIM else LIT_PEAK * (1 - t) ** 2
    return 0.0


def lit_u(level):
    """The u for a surface lit `level` of the way, 0 to 1 (band LIT)."""
    return U_MIDDLE + (U_RIM - U_MIDDLE) * (1 - math.sqrt(min(1.0, max(0.0, level))))


def disc_u(r, radius):
    """The u for a point r metres from a disc's middle."""
    return U_MIDDLE + (U_RIM - U_MIDDLE) * min(1.0, r / radius)


def fire_u(heat):
    """The u for fire at `heat`, 0 (white-hot) to 1 (all but black)."""
    return U_MIDDLE + (U_RIM - U_MIDDLE) * min(1.0, max(0.0, heat))


def fire_colour(u):
    """The linear colour of band FIRE at u."""
    heat = min(1.0, max(0.0, (u - U_MIDDLE) / (U_RIM - U_MIDDLE)))
    for (h0, c0), (h1, c1) in zip(FIRE_RAMP, FIRE_RAMP[1:]):
        if heat <= h1:
            share = (heat - h0) / (h1 - h0)
            return tuple(a + (b - a) * share for a, b in zip(c0, c1))
    return FIRE_RAMP[-1][1]


def srgb(linear):
    """Linear brightness to the 8-bit grey the file holds (the game reads the file as sRGB)."""
    value = 12.92 * linear if linear <= 0.0031308 else 1.055 * linear ** (1 / 2.4) - 0.055
    return max(0, min(255, round(value * 255)))


def pixels():
    """The rows of (red, green, blue) values, top row first."""
    rows = []
    for y in range(HEIGHT):                        # y = 0 is the top row of the picture
        v = 1.0 - (y + 0.5) / HEIGHT               # the picture's top is v = 1
        if int(v * BANDS) == 7:
            rows.append([tuple(srgb(c) for c in fire_colour((x + 0.5) / WIDTH)) for x in range(WIDTH)])
        else:
            rows.append([(srgb(glow((x + 0.5) / WIDTH, v)),) * 3 for x in range(WIDTH)])
    return rows


def png_bytes(rows):
    """An 8-bit RGB PNG (RGB even for the grey bands: Unity can read a one-channel PNG as red only), written by hand so this needs
    nothing installed."""
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(value for pixel in row for value in pixel) for row in rows)
    header = struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def write(path=PNG):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes(pixels()))
    return path


if __name__ == "__main__":
    print("wrote", write(Path(sys.argv[1]) if len(sys.argv) > 1 else PNG))
