"""Every number that defines BITCH's body, in one place.

Change a number here, run  python -m sim.build , and every exported model
(MuJoCo, URDF, meshes, viewer) is rebuilt from it.

Units: millimetres and grams in this file (easy to measure with a ruler and a
kitchen scale). sim/model.py converts to metres and kilograms.

Each value is tagged:
  MEASURED  Brandon measured it on the real robot
  SPEC      from a datasheet or product listing
  CODE      copied from desk.py / actor.py (keep in sync if those change)
  FITTED    worked out so the model matches a measured total height
  ESTIMATE  a best guess from the photos; measure it to improve the model

Frame: x = forward (the way the eyes face at rest), y = BITCH's left, z = up.
The origin is on the desk, directly under the base servo's shaft.
"""
from math import pi

# ---------------------------------------------------------------- the shoebox (fixed base)
BOX_DEPTH = 250.0        # x  MEASURED (Nike Air Force 1 box)
BOX_WIDTH = 380.0        # y  MEASURED, long side faces the front
BOX_HEIGHT = 118.0       # z  MEASURED, with the lid on
LID_DEPTH = 30.0         # how far the lid comes down the sides  ESTIMATE
LID_LIP = 2.0            # lid is this much bigger than the box on each side  ESTIMATE
BOX_WALL = 2.0           # cardboard thickness  ESTIMATE
CABLE_HOLE = (-45.0, 10.0, 6.0)   # x, y, radius: wires drop into the box behind the bot  ESTIMATE

# ---------------------------------------------------------------- what sits on the lid
RAIL_HEIGHT = 5.0        # stacked popsicle sticks under the lazy Susan  MEASURED
RAIL_LENGTH = 150.0      # one jumbo craft stick  SPEC
RAIL_WIDTH = 18.0        # SPEC
RAIL_X = 38.0            # rails run left-right, one in front and one behind  ESTIMATE

SUSAN_SIZE = 94.0        # 4" lazy Susan, square  SPEC (product listing)
SUSAN_HEIGHT = 8.7       # SPEC
SUSAN_HOLE = 54.0        # center hole  SPEC
SUSAN_PLATE = 1.0        # steel plate thickness  ESTIMATE
SUSAN_RACE_R = 38.0      # ball race radius  ESTIMATE

TURN_PLATE = (70.0, 70.0, 7.0)   # turning base: x, y, thickness  MEASURED
# underside of the turning base sits on the lazy Susan: RAIL_HEIGHT + SUSAN_HEIGHT = 13.7 above the lid

# ---------------------------------------------------------------- MG90S metal-gear servo (all four are the same)
SERVO_LEN = 22.8         # case length  SPEC
SERVO_WID = 12.2         # case width  SPEC
SERVO_CASE = 22.5        # case height, bottom to top of case  SPEC
SERVO_TAB_SPAN = 32.5    # tip to tip of the mounting tabs  SPEC
SERVO_TAB_Z = (15.5, 18.0)   # tab bottom/top, measured from the case bottom  SPEC
SERVO_DOME_TOP = 26.0    # top of the round gear housing  SPEC
SERVO_SHAFT_TOP = 29.5   # top of the output spline  SPEC
SERVO_SHAFT_OFFSET = 5.9 # shaft center to the near end of the case  SPEC
HORN_LEN = 20.0          # horn arm length from the shaft  SPEC
HORN_THICK = 2.0         # SPEC
SERVO_MASS = 13.4        # grams, metal gears, includes the short cable stub  SPEC
HORN_MASS = 1.0          # SPEC

# Servo behaviour (MG90S at about 5 V)
SERVO_STALL_TORQUE = 0.20    # N*m  (about 2.0 kg*cm)  SPEC
SERVO_SPEED = 0.10           # seconds per 60 degrees with no load  SPEC
# derived in model.py: velocity limit, damping and reflected motor inertia (armature)

# ---------------------------------------------------------------- the arm (S2, then S3 on top of it)
# S2 is glued by its flat side to an upright cardboard tab on the turning base, on BITCH's right.
# Its shaft is near the top of the case and points left; the stick's bottom end is glued to its horn.
# S3 is glued to the top of the stick on the same (right) side: the case's bottom against the stick,
# shaft near the top of the case, pointing right into the neck's side panel. So S3 moves with the arm.
S2_PIVOT_HEIGHT = 20.0   # turning base top to the S2 shaft center  MEASURED (~)
S2_SIDE = -1             # S2 and S3 sit on BITCH's right (-y)  MEASURED
ARM_LENGTH = 148.0       # S2 shaft to S3 shaft  FITTED to the 235 mm upright height (tape measure: ~140)
ARM_WIDTH = 16.0         # MEASURED
ARM_THICK = 5.0          # two popsicle sticks glued together  MEASURED
STICK_BELOW = 2.0        # stick runs this far below the S2 shaft  ESTIMATE (photos)
STICK_ABOVE = 2.0        # and this far above the S3 shaft  ESTIMATE (photos)
BRACKET = (5.0, 24.0, 28.0)   # S2's upright cardboard tab, behind the servo: thickness (x), width (y), height  ESTIMATE

# ---------------------------------------------------------------- the neck (an L of cardboard) and head (S4)
# Side panel: S3's horn is hot-glued to it near its bottom back corner.
# Front panel: S4 is pushed through a hole and held by its tabs, lying on its side, shaft forward.
NECK_LENGTH = 70.0       # S3 shaft to the tip of S4's shaft, front to back  MEASURED (~)
NECK_RISE = 16.0         # S4 shaft above S3 shaft, with BITCH upright  MEASURED
NECK_SIDE = (65.0, 5.0, 30.0)    # side panel: length, thickness, height  MEASURED (70 x 30 x 5 incl. the front panel)
NECK_SIDE_BACK = -14.0   # side panel's back edge, behind the S3 shaft  ESTIMATE (photos)
NECK_FRONT = (5.0, 40.0, 30.0)   # front panel: thickness, width, height  ESTIMATE (back photo, scaled from S4's case)
NECK_TOP = 14.0          # top edges of both panels, above the S4 shaft  ESTIMATE (back photo)
S4_GAP = 7.0             # space between S4's case and the side panel  ESTIMATE (back photo)

HEAD = (3.0, 75.0, 68.0)  # flat cardboard panel: thickness, width, height  MEASURED
HEAD_TOP = 30.0           # S4 shaft center to the head's top edge  MEASURED
                          # the head is centered left-right on S4's shaft  (back photo)
EYE_DIAMETER = 20.0       # googly eyes  MEASURED
EYE_SPACING = 35.0        # center to center  MEASURED
EYE_HEIGHT = 45.0         # eye center above the head's bottom edge. Brandon measured 35 to the
                          # eye; the photos match 35 to the eye's lower edge, so center = 45.
EYE_DEPTH = 6.0           # how far the googly eye domes out  ESTIMATE

# ---------------------------------------------------------------- masses of the hand-made parts (grams)
CARDBOARD_GSM = 500.0     # corrugated board, grams per square metre  ESTIMATE
STICK_DENSITY = 0.45      # birch craft sticks, g/cm^3  SPEC
TURN_PLATE_MASS = 5.0     # two layers of cardboard  ESTIMATE
SUSAN_TOP_MASS = 35.0     # the turning steel plate rides on the base joint  ESTIMATE
GLUE_PER_JOINT = 1.5      # hot glue blobs  ESTIMATE
EYE_MASS = 0.8            # SPEC

# ---------------------------------------------------------------- joints, exactly as the code sees them
NAMES = ('base', 'arm', 'neck', 'head')
# desk.py drives the base with 500-2500 us (+-90 deg for +-1) and the others with 1000-2000 us
# (+-45 deg for +-1) on a 180-degree servo.  CODE.  If a protractor disagrees, change these.
DEG_PER_UNIT = (90.0, 45.0, 45.0, 45.0)
REST = (0.0, -0.8, -0.8, 0.0)          # CODE desk.REST
REACH = ((-1, 1), (-1.5, 1.5), (-1, 1), (-1, 1))   # CODE desk.REACH: hard limits
SAFE_LO = (-0.9, -1.3, -1.0, -0.85)    # CODE actor.Actor.LO: what the animation engine allows
SAFE_HI = (0.9, -0.15, -0.1, 0.85)     # CODE actor.Actor.HI
CONTROL_HZ = 50                        # desk.py and Actor both update every 20 ms  CODE
# Signs (MEASURED/CODE): +base turns left seen from above; +arm leans forward and down;
# +neck looks up; +head tilts (direction not yet checked on the robot).

# ---------------------------------------------------------------- measured totals the model is checked against
MEASURED_HEIGHT_UPRIGHT = 235.0   # lid to top of head, every joint at 0
MEASURED_HEIGHT_REST = 220.0      # lid to top of head, at REST


def to_rad(units):
    """desk.py positions (-1..1) -> joint angles in radians."""
    return [u * d * pi / 180 for u, d in zip(units, DEG_PER_UNIT)]


def to_units(rad):
    """joint angles in radians -> desk.py positions."""
    return [r * 180 / pi / d for r, d in zip(rad, DEG_PER_UNIT)]
