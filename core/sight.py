"""Live anonymous tracking, attention, and camera-to-body alignment."""
from collections import deque
import math
from statistics import median
import threading
import time

import cv2

from . import config, voice
from .hub import hub
from .people import people
from .pointing import MODEL as POINTING_MODEL, Pointing

MAX_BASE = 0.55
MIN_NECK, MAX_NECK = -0.88, -0.43
REST_NECK = -0.8


def aim_at(x, y, samples):
    if not samples:
        base = (x - 0.5) * 1.16
        neck = -0.68 + (0.5 - y) * 0.62
    else:
        left, center, right = samples
        side = right if (x - center[0]) * (right[0] - center[0]) >= 0 else left
        fraction = (x - center[0]) / (side[0] - center[0])
        base = center[1] + fraction * (side[1] - center[1])
        if len(center) == 4 and len(side) == 4:
            blend = max(0, min(1, fraction))
            level = center[2] + blend * (side[2] - center[2])
            pitch = center[3] + blend * (side[3] - center[3])
            neck = pitch + (level - y) * 0.58
        else:
            neck = -0.68 + (0.5 - y) * 0.62
    return max(-MAX_BASE, min(MAX_BASE, base)), max(MIN_NECK, min(MAX_NECK, neck))


def center(box):
    x, y, width, height = box
    return x + width / 2, y + height / 2


class Attention:
    """Give a new person a moment to settle in, then hold gaze through brief occlusion."""
    def __init__(self):
        self.people = {}
        self.next_id = 1
        self.focus = None
        self.focus_since = 0

    def update(self, boxes, now):
        self.people = {key: person for key, person in self.people.items() if now - person['seen'] < 0.75}
        pairs = []
        for index, box in enumerate(boxes):
            for key, person in self.people.items():
                distance = math.dist(center(box), center(person['box']))
                limit = max(0.085, person['box'][2] * 0.65)
                if distance < min(0.19, limit):
                    pairs.append((distance, key, index))
        remaining = set(range(len(boxes)))
        matched = set()
        for distance, key, index in sorted(pairs):
            if index in remaining and key not in matched:
                person = self.people[key]
                weight = 0.67 if distance > 0.035 else 0.38
                person['box'] = tuple(weight * new + (1 - weight) * old
                                      for new, old in zip(boxes[index], person['box']))
                person['seen'] = now
                remaining.remove(index)
                matched.add(key)
        for index in sorted(remaining):
            self.people[self.next_id] = {'box': boxes[index], 'seen': now, 'first': now, 'attended': -math.inf}
            self.next_id += 1
        visible = {key: person for key, person in self.people.items() if person['seen'] == now}
        candidates = {key: person for key, person in visible.items() if now - person['first'] >= 0.28}
        if self.focus not in self.people:
            self.focus = max(candidates, key=lambda key: candidates[key]['box'][2], default=None)
            self.focus_since = now
        elif now - self.focus_since >= 8 and self.focus in visible:
            others = [key for key, person in candidates.items() if key != self.focus and now - person['first'] > 1.1]
            if others:
                self.focus = min(others, key=lambda key: (candidates[key]['attended'], -candidates[key]['box'][2]))
                self.focus_since = now
        if self.focus in self.people:
            self.people[self.focus]['attended'] = now
        return visible, self.focus


class HeadGesture:
    """Trigger one delayed echo of a sustained head tilt; rearm on neutral."""
    def __init__(self):
        self.focus = None
        self.direction = 0
        self.since = self.until = self.again = 0.
        self.armed = True
        self.reference = None
        self.mode = None

    def observe(self, focus, row, now, flipped=False):
        if focus != self.focus:
            self.__init__()
            self.focus = focus
        if row is None:
            self.since = now
            return
        dx, dy = float(row[6]-row[4]), float(row[7]-row[5])
        if abs(dx) < 8:
            self.since = now
            return
        roll = math.degrees(math.atan(dy/dx))
        threshold = 13.
        # Individual landmarks drift with viewpoint. Estimate rotation from the
        # whole five-point constellation relative to this person's neutral face.
        if len(row) >= 14:
            points = row[4:14].reshape(5, 2)
            points = points - points.mean(axis=0)
            if self.reference is None and abs(roll) < 9:
                self.reference = points.copy()
            if self.reference is not None:
                ref = self.reference
                cross = (ref[:, 0]*points[:, 1] - ref[:, 1]*points[:, 0]).sum()
                delta = math.degrees(math.atan2(cross, (ref*points).sum()))
                if abs(delta) > abs(roll):
                    roll = delta
                threshold = 10.
        if abs(roll) < 6:
            self.armed = True
        direction = (1 if roll > 0 else -1) if abs(roll) >= threshold else 0
        if self.direction != direction:
            self.direction, self.since = direction, now
        if direction and self.armed and now >= self.again and now-self.since >= .44:
            self.mode = 'mirror-right' if (direction > 0) != flipped else 'mirror-left'
            self.until, self.again = now+2.25, now+9.
            self.armed = False


class Sight:
    def __init__(self):
        self.lock = threading.Lock()
        self.frame_ready = threading.Condition(self.lock)
        self.input_ready = threading.Event()
        self.latest = None
        self.target_source = None
        self.attention = Attention()
        self.target = None
        self.frame = None
        self.frame_number = 0
        self.sample_window = deque(maxlen=8)
        self.setup = None
        self.drive = {'mode': 'Paused'}
        self.face_at = 0.0
        self.met = {}
        self.recognition_until = 0.0
        self.gesture = HeadGesture()
        self.pointing = Pointing()
        self.direction = None

    def start(self):
        model = config.ROOT / 'mac' / POINTING_MODEL
        threading.Thread(target=self.pointing.run, args=(model,), daemon=True).start()
        threading.Thread(target=self.detect, daemon=True).start()
        threading.Thread(target=self.guide, daemon=True).start()

    def feed(self, source, image):
        with self.lock:
            received = time.monotonic()
            self.latest = source, received, image
            self.input_ready.set()
        if self.pointing.ready:
            self.pointing.feed(source, received, image)

    def detect(self):
        model = config.ROOT / 'mac' / 'face_detection_yunet_2023mar.onnx'
        try:
            detector = cv2.FaceDetectorYN.create(str(model), '', (320, 240), score_threshold=0.78) if model.exists() else None
        except cv2.error:
            detector = None
        cascade = None
        if detector is None and hasattr(cv2, 'CascadeClassifier'):
            cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        if detector is None and cascade is None:
            hub.note('camera', 'Face detector missing. Run mac/setup.sh to enable tracking.')
        while True:
            self.input_ready.wait()
            with self.lock:
                frame, self.latest = self.latest, None
                self.input_ready.clear()
            source, received, image = frame
            if not hub.accepts(source, 'eyes') or time.monotonic() - received > 0.7:
                continue
            h, w = image.shape[:2]
            width = min(640, w)
            height = max(1, round(h * width / w))
            preview = cv2.resize(image, (width, height)) if width != w else image
            detection = cv2.resize(preview, (min(384, width), max(1, round(height * min(384, width) / width))))
            dh, dw = detection.shape[:2]
            if detector is not None:
                detector.setInputSize((dw, dh))
                _, faces = detector.detect(detection)
                rows = [] if faces is None else faces[:12]
                boxes = [row[:4] for row in rows]
            elif cascade is not None:
                rows = []
                gray = cv2.cvtColor(detection, cv2.COLOR_BGR2GRAY)
                boxes = cascade.detectMultiScale(gray, 1.2, 5, minSize=(26, 26))[:12]
            else:
                rows = []
                boxes = []
            boxes = [tuple(max(0.0, min(1.0, float(value) / scale)) for value, scale in zip(box, (dw, dh, dw, dh)))
                     for box in boxes]
            ok, jpeg = cv2.imencode('.jpg', preview, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                continue
            with self.frame_ready:
                if not hub.accepts(source, 'eyes'):
                    continue
                if self.target_source != source:
                    self.attention = Attention()
                    self.gesture = HeadGesture()
                    self.sample_window.clear()
                self.target_source = source
                visible, focus = self.attention.update(boxes, received)
                if len(rows) and received >= self.face_at and people.needed('face') and visible:
                    key = min(visible, key=lambda identifier: visible[identifier].get('face_at', 0))
                    person = visible[key]
                    index = min(range(len(boxes)), key=lambda i: math.dist(center(boxes[i]), center(person['box'])))
                    person['face_at'] = received
                    self.face_at = received + 0.30
                    try:
                        name = people.face(detection, rows[index], source, key, alone=len(boxes) == 1)
                        if person.get('last_name') == name:
                            person['name'] = name
                        if name and name == person.get('name'):
                            person['confirmed_at'] = received
                        person['last_name'] = name
                    except (cv2.error, RuntimeError, OSError, AttributeError, ValueError):
                        self.face_at = received + 2
                allowed_names = people.names('face')
                for person in visible.values():
                    if person.get('name') not in allowed_names:
                        person['name'] = None
                target = self.attention.people.get(focus)
                focused_row = None
                if focus in visible and len(rows):
                    index = min(range(len(boxes)), key=lambda i: math.dist(center(boxes[i]), center(target['box'])))
                    focused_row = rows[index]
                alignment = config.get('camera_calibration').get(source)
                flipped = bool(alignment and alignment[-1][0] < alignment[0][0])
                self.gesture.observe(focus, focused_row, received, flipped)
                recognized = target and target.get('name')
                if recognized:
                    previous = self.met.get(recognized, -math.inf)
                    if received - previous >= 18:
                        self.recognition_until = received + 1.55
                    self.met[recognized] = received
                self.target = (received, *center(target['box'])) if target else None
                if len(boxes) == 1:
                    self.sample_window.append((*center(boxes[0]), received))
                else:
                    self.sample_window.clear()
                self.frame_number += 1
                self.frame = (received, self.frame_number, jpeg.tobytes(), {
                    'size': [width, height],
                    'people': [{'id': key, 'box': [round(n, 4) for n in person['box']], 'name': person.get('name')}
                               for key, person in visible.items()],
                    'focus': focus,
                })
                self.frame_ready.notify_all()

    def solo_recognized(self):
        with self.lock:
            now = time.monotonic()
            if not self.frame or now - self.frame[0] > .5 or not hub.accepts(self.target_source, 'eyes'):
                return None
            visible = self.frame[3]['people']
            if len(visible) != 1:
                return None
            entry = visible[0]
            person = self.attention.people.get(entry['id'], {})
            name = entry['name']
            if name and now - person.get('confirmed_at', -math.inf) < .8 and name in people.names('face'):
                return name
        return None

    def direction_scene(self):
        with self.lock:
            if not self.frame or time.monotonic() - self.frame[0] > .65 or not hub.accepts(self.target_source, 'eyes'):
                return None
            received, _, jpeg, scene = self.frame
            visible = scene['people']
            face = center(visible[0]['box']) if len(visible) == 1 else None
            return self.target_source, received, jpeg, face, len(visible)

    def direct(self, camera, received, point, kind):
        robot = hub.get('robot')
        if not robot or not robot.online or not robot.motion_enabled:
            return 'offline'
        with self.lock:
            now = time.monotonic()
            if (now - received > 7.5 or not self.frame or now - self.frame[0] > .65
                    or self.target_source != camera or not hub.accepts(camera, 'eyes')
                    or self.setup and now < self.setup['until']):
                return 'stale'
            self.direction = camera, point, now + (5.0 if kind == 'describe' else 3.6)
        return 'accepted'

    def clear_direction(self):
        with self.lock:
            self.direction = None

    def current_scene(self):
        with self.lock:
            if self.frame and time.monotonic()-self.frame[0] < .95 and hub.accepts(self.target_source, 'eyes'):
                return self.frame[2], len(self.frame[3]['people'])
        return None, None

    def picture(self, after=0):
        deadline = time.monotonic() + 0.65
        with self.frame_ready:
            while hub.selected('eyes'):
                now = time.monotonic()
                if self.frame and (self.frame[1] > after or after > self.frame_number) and now - self.frame[0] < 0.9 and hub.accepts(self.target_source, 'eyes'):
                    _, sequence, image, info = self.frame
                    return sequence, image, {**info, 'drive': self.drive}
                if deadline <= now:
                    break
                self.frame_ready.wait(deadline - now)
        return None

    def guide(self):
        last_camera = None
        last_focus = None
        last_aim = None
        noticed_until = 0.
        while True:
            robot = hub.get('robot')
            now = time.monotonic()
            camera = hub.selected('eyes')
            idle = config.get('idle')
            brain = hub.brain
            phase = hub.hearing.phase() if hub.hearing else None
            listening = bool(brain and brain.followup_seconds()) or phase == 'Listening'
            energy, chirping, speech_pitch = voice.articulation()
            speaking = bool(brain and brain.busy.is_set() and hub.status == 'Speaking')
            reaction = {'Answering': 'notice', 'Thinking': 'ponder', 'Speaking': 'speaking'}.get(hub.status)
            social = reaction if brain and brain.busy.is_set() and reaction else 'attend' if listening else None
            if phase == 'Listening':
                social = 'listen'
                energy = min(1., hub.level) if now - hub.hearing.activity_at < .25 else 0.
            if phase == 'Understanding':
                social = 'ponder'
            if chirping:
                social = 'notice'
            elif social in ('ponder', 'speaking') and brain.expression and now < brain.expression[1]:
                social = brain.expression[0]
            if speaking and not chirping:
                delivery = 'farewell' if brain.close_turn else brain.delivery
                social = 'speaking' if delivery == 'neutral' else 'speak-' + delivery
            if brain and brain.testing_output:
                social = None
            with self.lock:
                setup = self.setup
                direction = self.direction
                cue = (direction[1] if direction and camera and camera.id == direction[0]
                       and now < direction[2] else None)
                if direction and cue is None:
                    self.direction = None
                aligned_source = camera and camera.id == self.target_source
                preview = setup and camera and setup['camera'] == camera.id and now < setup['until']
                target = self.target if aligned_source and self.target and now - self.target[0] < 0.9 else None
                focus = self.attention.focus if target else None
                aim = setup['aim'] if preview else None
                if not social and idle and target and now < self.gesture.until:
                    social = self.gesture.mode
                if not social and idle and target and now < self.recognition_until:
                    social = 'recognize'
            camera_id = camera.id if camera else None
            if camera_id != last_camera or preview or not idle:
                last_camera, last_focus, last_aim, noticed_until = camera_id, None, None, 0.
            calibration = config.get('camera_calibration').get(camera_id) if camera_id else None
            attention_aim = aim_at(*target[1:], calibration) if target else None
            if target and idle and not preview:
                if focus != last_focus:
                    # Reserve the large double take for a new or returning target after a break.
                    if not last_aim or now - last_aim[2] > 1.15:
                        noticed_until = now + 1.45
                    last_focus = focus
                last_aim = (*attention_aim, now)
                if not social and now < noticed_until:
                    social = 'double-take'
            elif last_aim and not social and now - last_aim[2] < 2.25:
                social = 'seek'
                attention_aim = last_aim[:2]
                last_focus = None
            elif not target:
                last_focus = None
            result = None
            if not robot or not robot.online:
                drive = {'mode': 'Pi offline'}
            elif preview:
                base, neck = aim
                result = robot.look('aim', base, neck)
                drive = {'mode': 'Aligning' if result else 'Pi did not accept aim'}
                with self.lock:
                    if self.setup is setup and setup['aim'] == aim:
                        setup['settled'] = bool(result and abs(result['base'] - base) <= 0.025
                                                and abs(result.get('neck', REST_NECK) - neck) <= 0.031)
            elif cue:
                base, neck = aim_at(*cue, calibration)
                mode = 'look-target' if robot.joint_attention else 'watch'
                result = robot.look(mode, base, neck)
                drive = {'mode': 'Looking where you asked' if result else 'Pi did not accept gaze'}
                if result:
                    drive['point'] = [round(value, 3) for value in cue]
            elif social:
                base, neck = attention_aim or (0, REST_NECK)
                mode = social
                accent = speech_pitch if speaking and not chirping else 0.
                if not robot.performance:
                    mode = {
                        'double-take': 'notice', 'seek': 'ponder', 'listen': 'attend',
                        'speak-warm': 'speaking', 'speak-farewell': 'speaking',
                        'speak-gentle': 'sad', 'speak-upbeat': 'happy',
                        'speak-emphatic': 'proud', 'speak-playful': 'recognize',
                    }.get(mode, mode)
                    neck = max(MIN_NECK, min(MAX_NECK, neck + accent))
                    accent = 0.
                if not robot.expressive:
                    mode = {'mirror-left': 'attend', 'mirror-right': 'attend', 'happy': 'recognize',
                            'scared': 'notice', 'sad': 'ponder', 'proud': 'recognize'}.get(mode, mode)
                result = robot.look(mode, base, neck, energy, accent)
                labels = {
                    'notice': 'Noticed you', 'double-take': 'Double take', 'seek': 'Looking for you',
                    'ponder': 'Thinking', 'attend': 'Your turn', 'listen': 'Listening',
                    'recognize': 'Recognizing', 'mirror-left': 'Mirroring your tilt', 'mirror-right': 'Mirroring your tilt',
                    'happy': 'Happy', 'sad': 'Sympathetic', 'proud': 'Proud', 'scared': 'Startled',
                }
                label = 'Speaking' if speaking and not chirping else labels.get(social, 'Speaking')
                drive = {'mode': label if result else 'Pi did not accept conversation pose'}
            elif not idle:
                robot.look('park')
                drive = {'mode': 'Idle motion off'}
            elif target:
                base, neck = attention_aim
                result = robot.look('watch', base, neck)
                drive = {'mode': 'Watching' if result else 'Pi did not accept tracking'}
            else:
                result = robot.look('scan')
                drive = {'mode': 'Looking around' if result else 'Pi did not accept motion'}
            if robot and robot.online and (preview or idle or social or cue) and result:
                if result.get('beat'):
                    drive['gesture'] = result['beat']
                    drive['pose'] = result.get('pose')
                drive['base'] = round(result['base'], 2)
                if 'neck' in result:
                    drive['neck'] = round(result['neck'], 2)
                if preview or target or social or cue:
                    drive['aim'] = [round(base, 2), round(neck, 2)]
            with self.lock:
                self.drive = drive
            time.sleep(.045 if hub.status == 'Speaking' else .08 if social else .13)

    def calibration(self, action, owner, joint=None, neck=REST_NECK, *, track=False):
        camera = hub.selected('eyes')
        robot = hub.get('robot')
        if action != 'cancel':
            if not camera:
                raise ValueError('Choose a camera first.')
            if not robot or not robot.online:
                raise ValueError('Connect the Pi before aligning.')
        now = time.monotonic()
        with self.lock:
            if self.setup and self.setup['until'] <= now:
                self.setup = None
            if action == 'cancel':
                if self.setup and self.setup['owner'] == owner:
                    self.setup = None
                return []
            if self.setup and self.setup['owner'] != owner:
                raise ValueError('Another device is calibrating.')
            if action == 'start':
                self.setup = {'owner': owner, 'camera': camera.id, 'samples': [], 'aim': (0, REST_NECK),
                              'aimed_at': now, 'settled': False}
            if not self.setup or self.setup['camera'] != camera.id:
                raise ValueError('Start alignment for this camera first.')
            setup = self.setup
            setup['until'] = now + 90
            samples = setup['samples']
            if action in ('aim', 'sample'):
                if joint is None or not math.isfinite(joint) or abs(joint) > MAX_BASE or not MIN_NECK <= neck <= MAX_NECK:
                    raise ValueError('Aim within the marked range.')
                if setup['aim'] != (joint, neck):
                    setup.update(aim=(joint, neck), aimed_at=now, settled=False)
                if action == 'sample':
                    if len(samples) == 3:
                        raise ValueError('Save the three points or start over.')
                    if not setup['settled']:
                        raise ValueError('The robot is still moving to that aim. Wait a moment, then capture.')
                    positions = [(x, y) for x, y, at in self.sample_window if now - at < 0.8 and at >= setup['aimed_at']]
                    if self.target_source != camera.id or len(positions) < 4:
                        raise ValueError('Stand alone in the frame and hold still until the face box settles.')
                    xs, ys = zip(*positions)
                    if max(xs) - min(xs) > 0.05 or max(ys) - min(ys) > 0.05:
                        raise ValueError('Hold still for a moment before capturing.')
                    point = [median(xs), joint, median(ys), neck]
                    if samples:
                        dx, db = point[0] - samples[-1][0], point[1] - samples[-1][1]
                        if abs(dx) < 0.045 or abs(db) < 0.045:
                            raise ValueError('Move sideways and turn the base farther before capturing the next point.')
                        if len(samples) == 2 and (dx * (samples[1][0] - samples[0][0]) <= 0 or db * (samples[1][1] - samples[0][1]) <= 0):
                            raise ValueError('Use the physical left, center and right of the robot in that order.')
                    samples.append(point)
            if action == 'save':
                if len(samples) != 3:
                    raise ValueError('Capture left, center and right first.')
                changes = {'camera_calibration': {**config.get('camera_calibration'), camera.id: samples}}
                if track:
                    changes['idle'] = True
                config.update(changes)
                self.setup = None
            return samples[:]
