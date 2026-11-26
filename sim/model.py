"""Builds BITCH's MuJoCo model (MJCF) from sim/params.py.

The MJCF is the master copy. Everything else (URDF, meshes, the web viewer)
is exported from the compiled MuJoCo model in sim/build.py, so the formats can
never disagree with each other.

Body tree (the same chain as the real robot):
    world      shoebox, popsicle rails, lazy Susan base, S1 servo
    turntable  joint "base" (S1)   lazy Susan top plate, turning base, S2 servo + bracket
    arm        joint "arm"  (S2)   the popsicle-stick arm, with the S3 servo glued to its top
    neck       joint "neck" (S3)   L-shaped cardboard neck (side + front panel), S4 servo
    head       joint "head" (S4)   head panel and googly eyes
"""
from math import pi, sqrt, cos, sin
import numpy as np
from . import params as P

MM, G = 1e-3, 1e-3          # mm -> m, g -> kg


# ---------------------------------------------------------------- small geometry helpers
def _n(v):
    v = np.asarray(v, float); return v / np.linalg.norm(v)


def frame(z, x):
    """Rotation whose local z points along `z` and local x as close to `x` as possible."""
    z = _n(z); x = np.asarray(x, float); x = _n(x - x.dot(z) * z)
    return np.column_stack([x, np.cross(z, x), z])


def quat(R):
    """3x3 rotation -> MuJoCo quaternion (w x y z)."""
    R = np.asarray(R, float); t = np.trace(R)
    if t > 0:
        s = 2 * sqrt(t + 1); q = [s / 4, (R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s, (R[1, 0] - R[0, 1]) / s]
    else:
        i = int(np.argmax(np.diag(R))); j, k = (i + 1) % 3, (i + 2) % 3
        s = 2 * sqrt(1 + R[i, i] - R[j, j] - R[k, k]); q = [0, 0, 0, 0]
        q[0] = (R[k, j] - R[j, k]) / s; q[i + 1] = s / 4
        q[j + 1] = (R[j, i] + R[i, j]) / s; q[k + 1] = (R[k, i] + R[i, k]) / s
    q = np.array(q); q = q if q[0] >= 0 else -q
    return q / np.linalg.norm(q)


I3 = np.eye(3)
X, Y, Z = (1, 0, 0), (0, 1, 0), (0, 0, 1)
ALONG_X, ALONG_Y = frame(X, Y), frame(Y, Z)      # point a cylinder's axis along x / y


class Part:
    """One primitive. dims in mm: box = full size (x,y,z); cylinder = (radius, length) along local z;
    ellipsoid = radii (x,y,z); sphere = (radius,). center in mm, in the body's frame."""
    def __init__(self, name, kind, dims, center, R=I3, mat='kraft', grams=0.0, role='detail'):
        self.name, self.kind, self.dims = name, kind, tuple(float(d) for d in dims)
        self.center, self.R, self.mat, self.grams, self.role = np.asarray(center, float), np.asarray(R, float), mat, grams, role

    def moved(self, p, R):
        """Same part expressed in a parent frame placed at p with rotation R."""
        return Part(self.name, self.kind, self.dims, p + R @ self.center, R @ self.R, self.mat, self.grams, self.role)

    def xml(self):
        d = self.dims
        size = {'box': lambda: [v / 2 for v in d], 'cylinder': lambda: [d[0], d[1] / 2],
                'ellipsoid': lambda: list(d), 'sphere': lambda: [d[0]]}[self.kind]()
        cls = {'solid': ' class="solid"', 'hidden': ' class="hidden"'}.get(self.role, '')
        q = quat(self.R); qs = '' if np.allclose(q, [1, 0, 0, 0]) else ' quat="%s"' % _f(q)
        return (f'<geom name="{self.name}" type="{self.kind}" size="{_f(np.array(size) * MM)}" '
                f'pos="{_f(self.center * MM)}"{qs} material="{self.mat}" mass="{max(self.grams, 0.01) * G:.6g}"{cls}/>')


def _f(v):
    return ' '.join(f'{x:.6g}' for x in np.asarray(v, float).ravel())


def place(parts, p, R):
    return [q.moved(np.asarray(p, float), R) for q in parts]


# ---------------------------------------------------------------- MG90S servo and horn
def servo(tag):
    """A servo in its own frame: origin at the tip of the output shaft, z out of the shaft,
    x along the case (the case sits toward +x because the shaft is near one end)."""
    top = P.SERVO_SHAFT_TOP; cx = P.SERVO_LEN / 2 - P.SERVO_SHAFT_OFFSET
    tab_mid = (P.SERVO_TAB_Z[0] + P.SERVO_TAB_Z[1]) / 2 - top
    return [
        Part(f'{tag}_case', 'box', (P.SERVO_LEN, P.SERVO_WID, P.SERVO_CASE), (cx, 0, P.SERVO_CASE / 2 - top), mat='servo', grams=11.0),
        Part(f'{tag}_pcb', 'box', (P.SERVO_LEN - 5, P.SERVO_WID - 2.5, 1.2), (cx, 0, 3 - top), mat='pcb', grams=0.5),
        Part(f'{tag}_tabs', 'box', (P.SERVO_TAB_SPAN, P.SERVO_WID, P.SERVO_TAB_Z[1] - P.SERVO_TAB_Z[0]), (cx, 0, tab_mid), mat='servo', grams=0.6),
        Part(f'{tag}_label', 'box', (14, 0.3, 9), (cx, P.SERVO_WID / 2 + 0.15, 11 - top), mat='label', grams=0.01),
        Part(f'{tag}_dome', 'cylinder', (6.0, P.SERVO_DOME_TOP - P.SERVO_CASE), (0, 0, (P.SERVO_CASE + P.SERVO_DOME_TOP) / 2 - top), mat='servo', grams=0.8),
        Part(f'{tag}_shaft', 'cylinder', (2.4, top - P.SERVO_DOME_TOP), (0, 0, (P.SERVO_DOME_TOP - top) / 2), mat='steel', grams=0.3),
        Part(f'{tag}_cable', 'box', (6, 3.6, 1.4), (cx + P.SERVO_LEN / 2 + 3, 0, 4 - top), mat='cable', grams=0.2),
    ]


def horn(tag):
    """The horn in the servo's frame. Whatever it drives is glued to its face at z = +HORN_THICK."""
    h = P.HORN_THICK
    return [
        Part(f'{tag}_hub', 'cylinder', (3.5, h + 3), (0, 0, (h - 3) / 2), mat='horn', grams=0.4),
        Part(f'{tag}_horn', 'box', (P.HORN_LEN + 4, 6, h), (P.HORN_LEN / 2 - 2, 0, h / 2), mat='horn', grams=0.6),
    ]


def plate_with_hole(tag, size, hole, thick, z, mat, grams):
    """Square plate with a square hole, made of four strips (primitives can't have holes)."""
    w = (size - hole) / 2; m = grams / 4
    return [Part(f'{tag}_f', 'box', (w, size, thick), (size / 2 - w / 2, 0, z), mat=mat, grams=m),
            Part(f'{tag}_b', 'box', (w, size, thick), (-size / 2 + w / 2, 0, z), mat=mat, grams=m),
            Part(f'{tag}_l', 'box', (hole, w, thick), (0, size / 2 - w / 2, z), mat=mat, grams=m),
            Part(f'{tag}_r', 'box', (hole, w, thick), (0, -size / 2 + w / 2, z), mat=mat, grams=m)]


def cardboard(area_mm2):
    return area_mm2 * 1e-6 * P.CARDBOARD_GSM


# ---------------------------------------------------------------- the robot, body by body
LID = P.BOX_HEIGHT
TURN_Z = LID + P.RAIL_HEIGHT + P.SUSAN_HEIGHT        # underside of the turning base (13.7 above the lid)
HT = P.HORN_THICK
ARM_Y = 0.0                                          # the arm's center plane
FACE = ARM_Y + P.S2_SIDE * P.ARM_THICK / 2           # arm face the horns are glued to (-y side)
TIP = FACE + P.S2_SIDE * HT                          # servo shaft tips sit one horn thickness further out


def world_parts():
    bw, bd, lid_d, lip = P.BOX_WIDTH, P.BOX_DEPTH, P.LID_DEPTH, P.LID_LIP
    parts = [
        Part('box', 'box', (bd - 2 * lip, bw - 2 * lip, LID - lid_d + 2), (0, 0, (LID - lid_d + 2) / 2), mat='box', role='solid'),
        Part('lid', 'box', (bd, bw, lid_d), (0, 0, LID - lid_d / 2), mat='lid', role='solid'),
        Part('cable_hole', 'cylinder', (P.CABLE_HOLE[2], 0.4), (P.CABLE_HOLE[0], P.CABLE_HOLE[1], LID + 0.2), mat='hole'),
        # inside the box (hidden by default; geom group 4)
        Part('pi4', 'box', (85, 56, 17), (40, -95, P.BOX_WALL + 8.5), mat='pcb', role='hidden'),
        Part('pca9685', 'box', (62.5, 25.4, 11), (40, 70, P.BOX_WALL + 5.5), mat='pcb_blue', role='hidden'),
    ]
    for i, x in enumerate((P.RAIL_X, -P.RAIL_X)):
        parts.append(Part(f'rail{i}', 'box', (P.RAIL_WIDTH, P.RAIL_LENGTH, P.RAIL_HEIGHT), (x, 0, LID + P.RAIL_HEIGHT / 2), mat='wood'))
    z0 = LID + P.RAIL_HEIGHT
    parts += plate_with_hole('susan_bottom', P.SUSAN_SIZE, P.SUSAN_HOLE, P.SUSAN_PLATE, z0 + P.SUSAN_PLATE / 2, 'steel', 35)
    ball_z = z0 + P.SUSAN_HEIGHT / 2
    for k in range(16):
        a = 2 * pi * k / 16
        parts.append(Part(f'ball{k}', 'sphere', (2.4,), (P.SUSAN_RACE_R * cos(a), P.SUSAN_RACE_R * sin(a), ball_z), mat='steel', grams=0.1))
    # S1: dropped through a hole in the lid, held by its tabs, shaft up into the lazy Susan
    parts += place(servo('s1'), (0, 0, TURN_Z - HT), frame(Z, (-1, 0, 0)))
    return parts


def turntable_parts():
    tx, ty, tz = P.TURN_PLATE
    parts = place(horn('s1'), (0, 0, -HT), frame(Z, X))
    parts += plate_with_hole('susan_top', P.SUSAN_SIZE, P.SUSAN_HOLE, P.SUSAN_PLATE, -P.SUSAN_PLATE / 2, 'steel', P.SUSAN_TOP_MASS)
    parts.append(Part('turn_plate', 'box', (tx, ty, tz), (0, 0, tz / 2), mat='kraft', grams=P.TURN_PLATE_MASS + P.GLUE_PER_JOINT, role='solid'))
    s2 = (0, TIP, tz + P.S2_PIVOT_HEIGHT)
    case_mid = TIP + P.S2_SIDE * (P.SERVO_SHAFT_TOP - P.SERVO_CASE / 2)       # middle of the S2 case, left-right
    bx, by, bz = P.BRACKET
    parts.append(Part('s2_bracket', 'box', (bx, by, bz), (-P.SERVO_WID / 2 - bx / 2, case_mid, tz + bz / 2), mat='kraft',
                      grams=cardboard(by * bz) + P.GLUE_PER_JOINT))
    parts += place(servo('s2'), s2, frame((0, -P.S2_SIDE, 0), (0, 0, -1)))   # shaft near the top, case hangs down to the plate
    return parts


def arm_parts():
    L = P.ARM_LENGTH; n = L + P.STICK_BELOW + P.STICK_ABOVE
    stick_g = P.ARM_WIDTH * P.ARM_THICK * n * 1e-3 * P.STICK_DENSITY
    parts = [Part('arm_stick', 'box', (P.ARM_WIDTH, P.ARM_THICK, n), (0, ARM_Y, (L + P.STICK_ABOVE - P.STICK_BELOW) / 2),
                  mat='wood', grams=stick_g + 2 * P.GLUE_PER_JOINT, role='solid')]
    parts += place(horn('s2'), (0, TIP, 0), frame((0, -P.S2_SIDE, 0), Z))            # S2 horn glued to the bottom of the stick
    # S3 rides on the arm: case bottom glued to the stick's right face, shaft near the top, pointing right
    parts += place(servo('s3'), (0, S3_TIP, L), frame((0, P.S2_SIDE, 0), (0, 0, -1)))
    return parts


# S3's shaft tip: one case height out from the stick's right face
S3_TIP = FACE + P.S2_SIDE * P.SERVO_SHAFT_TOP
SIDE_IN = S3_TIP + P.S2_SIDE * HT                    # inner face of the neck's side panel (S3 horn glued here)


def s4_y():
    """S4 lies on its side with its case reaching back toward the side panel; shaft at the far end."""
    return SIDE_IN - P.S2_SIDE * (P.S4_GAP + P.SERVO_LEN - P.SERVO_SHAFT_OFFSET)


def neck_front():
    """x of the front panel's front face: S4's tabs rest against it."""
    return P.NECK_LENGTH - (P.SERVO_SHAFT_TOP - P.SERVO_TAB_Z[0])


def neck_parts():
    parts = place(horn('s3'), (0, S3_TIP, 0), frame((0, P.S2_SIDE, 0), X))
    sl, st, sh = P.NECK_SIDE; ft, fw, fh = P.NECK_FRONT
    top = P.NECK_RISE + P.NECK_TOP; front = neck_front()
    side_y = SIDE_IN + P.S2_SIDE * st / 2
    parts.append(Part('neck_side', 'box', (sl, st, sh), (P.NECK_SIDE_BACK + sl / 2, side_y, top - sh / 2), mat='kraft',
                      grams=cardboard(sl * sh) + P.GLUE_PER_JOINT, role='solid'))
    outer = SIDE_IN + P.S2_SIDE * st                                       # the L's outside corner
    parts.append(Part('neck_front', 'box', (ft, fw, fh), (front - ft / 2, outer - P.S2_SIDE * fw / 2, top - fh / 2), mat='kraft',
                      grams=cardboard(fw * fh) + P.GLUE_PER_JOINT, role='solid'))
    parts += place(servo('s4'), (P.NECK_LENGTH, s4_y(), P.NECK_RISE), frame(X, (0, P.S2_SIDE, 0)))
    return parts


def head_parts():
    t, w, h = P.HEAD
    cz = P.HEAD_TOP - h / 2
    parts = place(horn('s4'), (0, 0, 0), frame(X, Z))
    parts.append(Part('head_panel', 'box', (t, w, h), (HT + t / 2, 0, cz), mat='kraft', grams=cardboard(w * h) + P.GLUE_PER_JOINT, role='solid'))
    ez = cz - h / 2 + P.EYE_HEIGHT; r = P.EYE_DIAMETER / 2; front = HT + t
    for side, y in (('l', P.EYE_SPACING / 2), ('r', -P.EYE_SPACING / 2)):
        # googly eye: white backing disc, loose black pupil (resting a little low), clear dome
        parts.append(Part(f'eye_{side}', 'cylinder', (r, 1.2), (front + 0.6, y, ez), ALONG_X, mat='eye', grams=P.EYE_MASS * 0.5, role='solid'))
        parts.append(Part(f'pupil_{side}', 'cylinder', (r * 0.52, 0.8), (front + 1.6, y, ez - r * 0.22), ALONG_X, mat='pupil', grams=P.EYE_MASS * 0.2))
        parts.append(Part(f'dome_{side}', 'ellipsoid', (P.EYE_DEPTH / 2, r, r), (front + 1.2, y, ez), mat='dome', grams=P.EYE_MASS * 0.3))
    return parts, (front + P.EYE_DEPTH / 2, 0, ez), (front, 0, cz + h / 2)


# ---------------------------------------------------------------- the MJCF document
def _cam(name, pos, target, fovy=40):
    pos, target = np.asarray(pos, float), np.asarray(target, float)
    f = _n(target - pos); x = _n(np.cross(f, Z)); y = np.cross(x, f)
    return f'<camera name="{name}" pos="{_f(pos * MM)}" xyaxes="{_f(x)} {_f(y)}" fovy="{fovy}"/>'


def servo_dynamics():
    """Joint damping, armature and actuator gains from the MG90S datasheet.
    The motor's back-EMF makes speed fall off with load, which behaves like damping:
    stall torque / no-load speed. The position gain gives full torque at about 3 degrees of error."""
    w_max = (pi / 3) / P.SERVO_SPEED                  # rad/s with no load (~10.5)
    damping = P.SERVO_STALL_TORQUE / w_max
    kp = P.SERVO_STALL_TORQUE / (3 * pi / 180)
    return dict(w_max=w_max, damping=damping, armature=5e-5, kp=kp, torque=P.SERVO_STALL_TORQUE)


def mjcf():
    d = servo_dynamics()
    reach = [(lo * u * pi / 180, hi * u * pi / 180) for (lo, hi), u in zip(P.REACH, P.DEG_PER_UNIT)]
    head, eye_pt, head_top = head_parts()
    tz = P.TURN_PLATE[2]
    mats = dict(kraft='.71 .55 .37 1', wood='.89 .77 .57 1', box='.64 .66 .68 1', lid='.7 .72 .74 1',
                servo='.18 .2 .2 .82', pcb='.12 .42 .2 1', pcb_blue='.12 .25 .55 1', label='.62 .1 .12 1',
                steel='.62 .64 .67 1', horn='.08 .08 .09 1', cable='.55 .25 .1 1', hole='.05 .05 .06 1',
                eye='.97 .97 .96 1', pupil='.03 .03 .03 1', dome='.9 .95 1 .14')
    spec_mat = {'steel': ' specular=".7" shininess=".8" reflectance=".05"', 'servo': ' specular=".4" shininess=".6"',
                'dome': ' specular=".9" shininess=".95"', 'pupil': ' specular=".3"'}
    js = ['base', 'arm', 'neck', 'head']
    rest = P.to_rad(P.REST)
    keys = {'rest': rest, 'upright': [0, 0, 0, 0], 'curious': P.to_rad((0, -0.45, -0.35, 0.55)),
            'bedtime': P.to_rad((0, 1.1, -1.0, 0))}

    out = [f'<mujoco model="BITCH">',
           '  <!-- Generated by sim/build.py from sim/params.py. Edit params.py, not this file. -->',
           '  <compiler angle="radian" autolimits="true"/>',
           '  <option timestep="0.002" integrator="implicitfast"/>',
           '  <statistic center="0 0 .19" extent=".35"/>',
           '  <visual><global offwidth="1600" offheight="1200" azimuth="150" elevation="-18"/>'
           '<quality shadowsize="4096" offsamples="8"/><headlight ambient=".22 .22 .22" diffuse=".3 .3 .3" specular=".1 .1 .1"/></visual>',
           '  <asset>',
           '    <texture name="sky" type="skybox" builtin="gradient" rgb1=".96 .94 .9" rgb2=".86 .85 .83" width="512" height="512"/>',
           '    <texture name="desk" type="2d" builtin="flat" rgb1=".93 .93 .91" width="64" height="64"/>',
           '    <material name="desk" texture="desk" specular=".1"/>']
    out += [f'    <material name="{k}" rgba="{v}"{spec_mat.get(k, "")}/>' for k, v in mats.items()]
    out += ['  </asset>',
            '  <default>',
            f'    <joint damping="{d["damping"]:.5g}" armature="{d["armature"]:.3g}" frictionloss="0.002"/>',
            '    <geom contype="0" conaffinity="0" group="1"/>',
            '    <default class="solid"><geom contype="1" conaffinity="1" group="0" friction=".6 .005 .0001"/></default>',
            '    <default class="hidden"><geom group="4"/></default>',
            f'    <position kp="{d["kp"]:.4g}" forcerange="{-d["torque"]} {d["torque"]}"/>',
            '  </default>',
            '  <worldbody>',
            '    <light name="key" pos=".35 .25 .9" dir="-.35 -.25 -1" directional="true" diffuse=".55 .55 .53" castshadow="true"/>',
            '    <light name="fill" pos="-.4 -.3 .6" dir=".4 .3 -.6" directional="true" diffuse=".25 .25 .27" castshadow="false"/>',
            '    <geom name="desk" type="plane" size="1 1 .01" material="desk" contype="1" conaffinity="1" group="0"/>',
            '    ' + _cam('front', (520, 0, 230), (0, 0, 175), 38),
            '    ' + _cam('side', (0, 560, 210), (10, 0, 175), 38),
            '    ' + _cam('three_quarter', (430, 380, 330), (5, 0, 165), 36),
            '    ' + _cam('top', (1, 0, 700), (0, 0, 130), 40)]
    out += ['    ' + p.xml() for p in world_parts()]
    out += [f'    <body name="turntable" pos="0 0 {TURN_Z * MM:.6g}">',
            f'      <joint name="base" axis="0 0 1" range="{reach[0][0]:.5g} {reach[0][1]:.5g}"/>']
    out += ['      ' + p.xml() for p in turntable_parts()]
    out += [f'      <body name="arm" pos="0 0 {(tz + P.S2_PIVOT_HEIGHT) * MM:.6g}">',
            f'        <joint name="arm" axis="0 1 0" range="{reach[1][0]:.5g} {reach[1][1]:.5g}"/>']
    out += ['        ' + p.xml() for p in arm_parts()]
    out += [f'        <body name="neck" pos="0 0 {P.ARM_LENGTH * MM:.6g}">',
            f'          <joint name="neck" axis="0 -1 0" range="{reach[2][0]:.5g} {reach[2][1]:.5g}"/>']
    out += ['          ' + p.xml() for p in neck_parts()]
    out += [f'          <body name="head" pos="{_f(np.array([P.NECK_LENGTH, s4_y(), P.NECK_RISE]) * MM)}">',
            f'            <joint name="head" axis="1 0 0" range="{reach[3][0]:.5g} {reach[3][1]:.5g}"/>']
    out += ['            ' + p.xml() for p in head]
    out += [f'            <site name="eyes" pos="{_f(np.array(eye_pt) * MM)}" size=".003" rgba="1 .2 .2 .6" group="5"/>',
            f'            <site name="head_top" pos="{_f(np.array(head_top) * MM)}" size=".003" rgba=".2 .2 1 .6" group="5"/>',
            f'            <camera name="eye" pos="{_f((np.array(eye_pt) + (2, 0, 0)) * MM)}" xyaxes="0 -1 0 0 0 1" fovy="62"/>',
            '          </body>', '        </body>', '      </body>', '    </body>',
            '    <site name="target" pos=".35 0 .2" size=".012" rgba="1 .35 .2 .9" group="5"/>',
            '  </worldbody>',
            '  <actuator>']
    out += [f'    <position name="{j}" joint="{j}" ctrlrange="{lo:.5g} {hi:.5g}"/>' for j, (lo, hi) in zip(js, reach)]
    out += ['  </actuator>', '  <sensor>']
    out += [f'    <jointpos name="{j}_pos" joint="{j}"/>' for j in js]
    out += [f'    <jointvel name="{j}_vel" joint="{j}"/>' for j in js]
    out += [f'    <actuatorfrc name="{j}_torque" actuator="{j}"/>' for j in js]
    out += ['    <framepos name="eyes_pos" objtype="site" objname="eyes"/>',
            '    <framexaxis name="eyes_forward" objtype="site" objname="eyes"/>',
            '  </sensor>', '  <keyframe>']
    out += [f'    <key name="{k}" qpos="{_f(q)}" ctrl="{_f(q)}"/>' for k, q in keys.items()]
    out += ['  </keyframe>', '</mujoco>', '']
    return '\n'.join(out)
