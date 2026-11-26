"""Compare the model with the real robot.

    python -m sim.check

Prints the heights the model predicts next to Brandon's tape-measure numbers,
the mass of each moving part, and how hard gravity makes each servo work.
When a number here disagrees with the robot, fix it in sim/params.py.
"""
import numpy as np
import mujoco
from . import params as P
from .model import mjcf, servo_dynamics


def main():
    m = mujoco.MjModel.from_xml_string(mjcf()); d = mujoco.MjData(m)

    def pose(units, settle=False):
        d.qpos[:] = P.to_rad(units); d.ctrl[:] = d.qpos; d.qvel[:] = 0
        if settle:
            for _ in range(1000): mujoco.mj_step(m, d)
        mujoco.mj_forward(m, d)

    print('Heights, top of head above the box lid (mm)')
    for name, units, real in (('upright', (0, 0, 0, 0), P.MEASURED_HEIGHT_UPRIGHT), ('rest', P.REST, P.MEASURED_HEIGHT_REST)):
        pose(units); h = d.site('head_top').xpos[2] * 1000 - P.BOX_HEIGHT
        print(f'  {name:8} model {h:6.1f}   measured {real:6.1f}   difference {h - real:+5.1f}')
    # what arm lean would make the rest height match (neck keeps the head level)
    up = P.MEASURED_HEIGHT_UPRIGHT; lean = np.degrees(np.arccos(1 - (up - P.MEASURED_HEIGHT_REST) / P.ARM_LENGTH))
    print(f'  The measured heights imply the arm leans back about {lean:.0f} deg at rest; the code assumes '
          f'{-P.REST[1] * P.DEG_PER_UNIT[1]:.0f} deg. Check with a protractor (see sim/README.md).')

    print('\nMoving parts')
    for b in range(1, m.nbody):
        print(f'  {m.body(b).name:10} {m.body_mass[b] * 1000:5.1f} g   (everything above it: {m.body_subtreemass[b] * 1000:5.1f} g)')

    s = servo_dynamics()
    print(f'\nServo model: {s["torque"]} N*m stall, {np.degrees(s["w_max"]):.0f} deg/s no-load, '
          f'damping {s["damping"]:.4f}, gain {s["kp"]:.2f} N*m/rad')
    print('Holding torque against gravity (share of stall torque)')
    for name, units in (('rest', P.REST), ('upright', (0, 0, 0, 0)), ('leaning in', (0, -0.15, -0.5, 0)), ('bedtime', (0, 1.1, -1.0, 0))):
        pose(units, settle=True)
        share = np.abs(d.actuator_force) / s['torque'] * 100
        print(f'  {name:11} ' + '  '.join(f'{n} {v:4.0f}%' for n, v in zip(P.NAMES, share)))


if __name__ == '__main__':
    main()
