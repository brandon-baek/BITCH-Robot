"""Live anonymous tracking, attention, and camera-to-body alignment."""
from collections import deque
import math
from statistics import median
import threading
import time

import cv2

from . import config, gaze, helper, voice
from .hub import hub
from .people import people
from .rhythm import rhythm
from .pointing import MODEL as POINTING_MODEL, Pointing

MAX_BASE = 0.55
MIN_NECK, MAX_NECK = -0.88, -0.43
REST_NECK = -0.8
THINK_BEFORE_PONDER = .9   # seconds of thinking before the look-away ponder pose


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


def thumbnail(image, box):
    """A square, slightly padded 96 px crop of one face, as JPEG bytes."""
    h, w = image.shape[:2]
    x, y, bw, bh = box
    side = max(bw * w, bh * h) * 1.5
    cx, cy = (x + bw / 2) * w, (y + bh / 2) * h
    left, top = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
    crop = image[top:int(min(h, top + side)), left:int(min(w, left + side))]
    if crop.size == 0:
        return None
    ok, jpeg = cv2.imencode('.jpg', cv2.resize(crop, (96, 96)), [cv2.IMWRITE_JPEG_QUALITY, 78])
    return jpeg.tobytes() if ok else None


def center(box):
    x, y, width, height = box
    return x + width / 2, y + height / 2


class Attention:
    """Give a new person a moment to settle in, hold gaze through brief occlusion,
    and keep tracking the same body and identity even when their face is turned or covered."""
    NAMED_HOLD = 4.5

    def __init__(self):
        self.people = {}
        self.next_id = 1
        self.focus = None
        self.focus_since = 0

    def update(self, boxes, now, bodies=()):
        self.people = {
            key: person for key, person in self.people.items()
            if now - person['seen'] < (self.NAMED_HOLD if person.get('name') else 1.2 if person.get('body') else 0.75)
        }
        pairs = []
        for index, box in enumerate(boxes):
            box_center = center(box)
            for key, person in self.people.items():
                distance = math.dist(box_center, center(person['box']))
                if person.get('body'):
                    distance = min(distance, math.dist(box_center, center(person['body']['head_box'])))
                limit = max(0.085, person['box'][2] * 0.72)
                if distance < min(0.22, limit):
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
                person['face_seen'] = now
                remaining.remove(index)
                matched.add(key)

        unused_bodies = set(range(len(bodies)))
        # Attach body poses to face-matched people first so we know where their shoulders are.
        body_pairs = []
        for b_idx in unused_bodies:
            body = bodies[b_idx]
            head_c = center(body['head_box'])
            for key in matched:
                person = self.people[key]
                dist = math.dist(head_c, center(person['box']))
                if person.get('body'):
                    dist = min(dist, math.dist(body['anchor'], person['body']['anchor']))
                if dist < max(0.18, body['span'] * 0.95):
                    body_pairs.append((dist, key, b_idx))
        linked_people = set()
        for _, key, b_idx in sorted(body_pairs):
            if key not in linked_people and b_idx in unused_bodies:
                self.people[key]['body'] = bodies[b_idx]
                self.people[key]['body_seen'] = now
                unused_bodies.remove(b_idx)
                linked_people.add(key)

        # Keep tracking existing people via their body pose when their face is turned away or covered.
        occluded_pairs = []
        for b_idx in unused_bodies:
            body = bodies[b_idx]
            head_c = center(body['head_box'])
            for key, person in self.people.items():
                if key in matched:
                    continue
                if person.get('body'):
                    dist = math.dist(body['anchor'], person['body']['anchor'])
                    limit = max(0.22, max(body['span'], person['body']['span']) * 1.2)
                else:
                    dist = math.dist(head_c, center(person['box']))
                    limit = max(0.18, body['span'] * 0.9)
                if dist < limit:
                    occluded_pairs.append((dist, key, b_idx))
        for _, key, b_idx in sorted(occluded_pairs):
            if key not in matched and b_idx in unused_bodies:
                person = self.people[key]
                body = bodies[b_idx]
                person['box'] = tuple(0.35 * new + 0.65 * old
                                      for new, old in zip(body['head_box'], person['box']))
                person['body'] = body
                person['body_seen'] = now
                person['seen'] = now
                matched.add(key)
                unused_bodies.remove(b_idx)

        for index in sorted(remaining):
            box = boxes[index]
            attached_body = None
            if unused_bodies:
                best = min(unused_bodies, key=lambda b_idx: math.dist(center(bodies[b_idx]['head_box']), center(box)))
                if math.dist(center(bodies[best]['head_box']), center(box)) < max(0.18, bodies[best]['span'] * 0.95):
                    attached_body = bodies[best]
                    unused_bodies.remove(best)
            self.people[self.next_id] = {
                'box': box, 'seen': now, 'face_seen': now, 'first': now, 'attended': -math.inf,
                'body': attached_body, 'body_seen': now if attached_body else -math.inf,
            }
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
        self.finger = None

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
                bodies = self.pointing.recent_bodies(source, received) if self.pointing.ready else ()
                visible, focus = self.attention.update(boxes, received, bodies)
                for person in visible.values():
                    if person.get('face_seen') == received and received - person.get('thumb_at', -1e9) >= 1.:
                        person['thumb'], person['thumb_at'] = thumbnail(preview, person['box']), received
                face_visible = {k: p for k, p in visible.items() if p.get('face_seen') == received}
                if len(rows) and received >= self.face_at and people.needed('face') and face_visible:
                    key = min(face_visible, key=lambda identifier: face_visible[identifier].get('face_at', 0))
                    person = face_visible[key]
                    index = min(range(len(boxes)), key=lambda i: math.dist(center(boxes[i]), center(person['box'])))
                    person['face_at'] = received
                    self.face_at = received + 0.25
                    try:
                        name, strong = people.face_confident(detection, rows[index], source, key, alone=len(visible) == 1)
                        person['face_checks'] = person.get('face_checks', 0) + 1
                        if name:
                            if strong or person.get('last_name') == name or person.get('name') == name:
                                person['name'] = name
                                person['confirmed_at'] = received
                                person['face_confirmed_at'] = received
                            person['last_name'] = name
                        else:
                            # Looking away or covering part of the face returns None; never erase a known track.
                            person['last_name'] = None
                    except (cv2.error, RuntimeError, OSError, AttributeError, ValueError):
                        self.face_at = received + 2
                allowed_names = people.names('face') | people.names('voice')
                for person in visible.values():
                    if person.get('name') not in allowed_names:
                        person['name'] = None
                    elif person.get('name'):
                        # Refresh track confirmation while the same body/person stays continuously tracked.
                        person['confirmed_at'] = received
                target = self.attention.people.get(focus)
                focused_row = None
                if focus in face_visible and len(rows):
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

    def solo_identity(self, within=4.5):
        """Return (name, 'face' | 'body') for a lone recognized person in view, even if currently looking away."""
        with self.lock:
            now = time.monotonic()
            if not self.frame or now - self.frame[0] > .9 or not hub.accepts(self.target_source, 'eyes'):
                return None, None
            allowed = people.names('face') | people.names('voice')
            active = [p for p in self.attention.people.values() if now - p['seen'] < min(within, Attention.NAMED_HOLD)]
            if len(active) != 1:
                return None, None
            person = active[0]
            name = person.get('name')
            if name and now - person.get('confirmed_at', -math.inf) < within and name in allowed:
                by = 'face' if now - person.get('face_confirmed_at', -math.inf) < 2.2 else 'body'
                return name, by
        return None, None

    def solo_recognized(self, within=4.5):
        return self.solo_identity(within=within)[0]

    def note_voice(self, name):
        """Associate a confident voice match with a lone unidentified body in view."""
        if not name:
            return
        with self.lock:
            now = time.monotonic()
            if not self.frame or now - self.frame[0] > .9 or not hub.accepts(self.target_source, 'eyes'):
                return
            visible = [p for p in self.attention.people.values() if now - p['seen'] < .9]
            if len(visible) == 1 and not visible[0].get('name') and name in (people.names('face') | people.names('voice')):
                visible[0]['name'] = name
                visible[0]['confirmed_at'] = now

    def in_view(self):
        """Recognized names and how many others are in the camera right now, or None with no camera."""
        with self.lock:
            now = time.monotonic()
            if not self.frame or now - self.frame[0] > .9 or not hub.accepts(self.target_source, 'eyes'):
                return None
            known = people.names('face') | people.names('voice')
            names = sorted({entry['name'] for entry in self.frame[3]['people'] if entry['name'] in known})
            return {'names': names, 'others': len(self.frame[3]['people']) - len(names)}

    def people_snapshot(self):
        """Snapshot of all currently visible tracked people with age, box, center, and image for arrival intelligence."""
        with self.lock:
            now = time.monotonic()
            if not self.frame or now - self.frame[0] > .9 or not hub.accepts(self.target_source, 'eyes'):
                return None
            received, _, jpeg, info = self.frame
            known = people.names('face') | people.names('voice')
            entries = []
            for item in info['people']:
                person = self.attention.people.get(item['id'])
                if not person:
                    continue
                name = item['name'] if item['name'] in known else None
                entries.append({
                    'id': item['id'],
                    'name': name,
                    'box': person['box'],
                    'center': center(person['box']),
                    'age': max(0.0, now - person['first']),
                    'face_fresh': now - person.get('face_confirmed_at', -math.inf) < 2.5,
                    'face_visible': now - person.get('face_seen', -math.inf) < 1.0,
                    'face_checks': person.get('face_checks', 0),
                })
            return {'camera': self.target_source, 'received': received, 'image': jpeg, 'people': entries}

    def faces(self):
        """Small pictures of the faces in view right now, so you can see who BITCH means. Kept in memory only."""
        with self.lock:
            now = time.monotonic()
            if not self.frame or now - self.frame[0] > .9 or not hub.accepts(self.target_source, 'eyes'):
                return []
            people_in_view = sorted(((key, person) for key, person in self.attention.people.items()
                                     if now - person['seen'] < .6 and person.get('thumb')),
                                    key=lambda item: item[1]['box'][0])
            return [{'track': key, 'name': person.get('name'), 'thumb': person['thumb']} for key, person in people_in_view]

    def face_to_learn(self, stranger):
        """The face being introduced: the only one in view, or for "this is Alex" the closest unknown face."""
        with self.lock:
            now = time.monotonic()
            if not self.frame or now - self.frame[0] > .9 or not hub.accepts(self.target_source, 'eyes'):
                return None
            visible = [(key, person) for key, person in self.attention.people.items() if now - person['seen'] < .5]
            if stranger:
                visible = [(key, person) for key, person in visible if not person.get('name')]
            elif len(visible) != 1:
                return None
            if not visible:
                return None
            key, person = max(visible, key=lambda item: item[1]['box'][2])
            return self.target_source, key, person.get('name')

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

    def follow_finger(self, camera, received):
        robot = hub.get('robot')
        with self.lock:
            now = time.monotonic()
            if (not robot or not robot.online or not robot.motion_enabled or now-received > .65
                    or self.setup and now < self.setup['until']):
                return 'unavailable'
            if not self.pointing.start_hand(camera, now+25):
                return 'unavailable'
            self.direction = None
            self.finger = camera, now+25
            return 'accepted'

    def clear_direction(self):
        with self.lock:
            self.direction = self.finger = None
        self.pointing.start_hand(None, 0.)

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

    def check_ins(self):
        person = None
        present_since = 0.
        last_attempt = time.monotonic()
        phrases = {'reading': 'Taking a reading break, or should I leave you to it?',
                   'desk project': 'How’s the desk project going?'}
        while True:
            time.sleep(5)
            brain, hearing = hub.brain, hub.hearing
            now = time.monotonic()
            if not (config.get('check_ins') and not config.get('muted') and config.get('volume')
                    and hub.selected('voice') and brain and hearing and hearing.ready):
                person = None
                continue
            name = self.solo_recognized()
            if name != person:
                person, present_since = name, now
            if not name or now-present_since < 90 or now-last_attempt < 900 or now-brain.last_interaction < 900:
                continue
            if brain.busy.is_set() or brain.followup_seconds() or hearing.capture or now-hearing.activity_at < 5:
                continue
            source = hub.selected('ears')
            image, people_in_view = self.current_scene()
            if not source or image is None or people_in_view != 1:
                continue
            last_attempt = now
            try:
                phrase = phrases.get(gaze.visible_activity(image))
            except (OSError, ValueError, TypeError, KeyError):
                continue
            if (phrase and self.solo_recognized() == name and brain.last_interaction <= now
                    and not hearing.capture and time.monotonic()-hearing.activity_at >= 5):
                brain.check_in(phrase, source.id)

    def guide(self):
        from .life import life

        last_camera = None
        last_focus = None
        last_aim = None
        noticed_until = 0.
        thinking_since = None
        last_park = 0.
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
            mood = life.mood_vector()
            life_pose = life.active_pose(now)
            sfx_volume = (config.get('volume') / 100.0) if (config.get('sounds') and not config.get('muted')) else 0.0
            # Replies usually start within a second now, so keep eye contact while thinking
            # and only look away to ponder when an answer takes longer.
            thinking_since = (thinking_since or now) if hub.status == 'Thinking' else None
            pondering = 'ponder' if thinking_since and now - thinking_since >= THINK_BEFORE_PONDER else 'attend'
            reaction = {'Answering': 'notice', 'Thinking': pondering, 'Speaking': 'speaking'}.get(hub.status)
            social = reaction if brain and brain.busy.is_set() and reaction else 'attend' if listening else None
            if phase == 'Listening':
                social = 'listen'
                energy = min(1., hub.level) if now - hub.hearing.activity_at < .25 else 0.
            if phase == 'Understanding':
                social = 'ponder'
            if helper.waiting() and not speaking and not listening and phase not in ('Listening', 'Understanding') and social is None:
                social = 'ponder'   # brief ponder during initial foreground lookup only; stays attentive while listening
            if chirping:
                social = 'notice'
            elif social in ('ponder', 'speaking') and brain.expression and now < brain.expression[1]:
                social = brain.expression[0]
            if speaking and not chirping:
                delivery, expression = voice.delivery_now() or (brain.delivery, brain.performance)
                if brain.close_turn:
                    delivery = 'farewell'
                elif expression != 'none':
                    delivery = {
                        'sad': 'gentle',
                        'annoyed': 'annoyed' if robot and robot.emotive_performance else 'emphatic',
                        'amused': 'amused' if robot and robot.emotive_performance else 'playful',
                        'surprised': 'surprised' if robot and robot.emotive_performance else 'upbeat',
                    }[expression]
                social = 'speaking' if delivery == 'neutral' else 'speak-' + delivery
            if brain and brain.testing_output:
                social = None
            with self.lock:
                setup = self.setup
                direction = self.direction
                finger = self.finger
                following_finger = bool(finger and camera and camera.id == finger[0] and now < finger[1]
                                        and not (setup and now < setup['until']))
                if finger and not following_finger:
                    self.finger = None
                cue = (direction[1] if direction and camera and camera.id == direction[0]
                       and now < direction[2] else None)
                if direction and cue is None:
                    self.direction = None
                if following_finger:
                    cue = self.pointing.fingertip(camera.id, now)
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
            # How awake BITCH is decides how much the body moves when no one is talking to it.
            engaged = bool(brain and (brain.busy.is_set() or brain.followup_seconds())) or phase in ('Listening', 'Understanding')
            state, beat = rhythm.update(target is not None, engaged, brain.last_interaction if brain else 0., now)
            calm = state in ('attentive', 'glancing')
            if not social and life_pose:
                social = life_pose
            if not social and beat and idle and robot and getattr(robot, 'rhythm', False):
                social = beat   # nodding off, or the morning stretch
            resting = state in ('resting', 'asleep') and not social and not cue and not preview
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
                result = robot.look(mode, base, neck, mood=mood, sfx_volume=sfx_volume)
                drive = {'mode': ('Following your finger' if following_finger else 'Looking where you asked') if result else 'Pi did not accept gaze'}
                if result:
                    drive['point'] = [round(value, 3) for value in cue]
            elif social:
                base, neck = attention_aim or (0, REST_NECK)
                mode = social
                accent = speech_pitch if speaking and not chirping else 0.
                tilt, lean = voice.gesture() if speaking and not chirping else (0., 0.)
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
                result = robot.look(mode, base, neck, energy, accent, tilt, lean, mood=mood, sfx_volume=sfx_volume)
                labels = {
                    'notice': 'Noticed you', 'double-take': 'Double take', 'seek': 'Looking for you',
                    'ponder': 'Thinking', 'attend': 'Your turn', 'listen': 'Listening',
                    'recognize': 'Recognizing', 'mirror-left': 'Mirroring your tilt', 'mirror-right': 'Mirroring your tilt',
                    'happy': 'Happy', 'sad': 'Sympathetic', 'lonely': 'Feeling lonely', 'grumpy': 'Grumpy',
                    'victory': 'Celebrating', 'alarm': 'Wake-up alarm', 'yawn': 'Sleepy',
                    'proud': 'Proud', 'scared': 'Startled',
                }
                label = 'Speaking' if speaking and not chirping else labels.get(social, 'Speaking')
                drive = {'mode': label if result else 'Pi did not accept conversation pose'}
            elif not idle or resting:
                # Parked means unpowered. Once a second is enough to keep it parked.
                if now - last_park >= 1:
                    robot.look('park')
                    last_park = now
                drive = {'mode': 'Idle motion off' if not idle else 'Asleep' if state == 'asleep' else 'Resting'}
            elif target:
                base, neck = attention_aim
                result = robot.look('watch', base, neck, calm=calm, mood=mood, sfx_volume=sfx_volume)
                drive = {'mode': ('Glancing up' if state == 'glancing' else 'Watching quietly' if calm else 'Watching')
                         if result else 'Pi did not accept tracking'}
            else:
                result = robot.look('scan', mood=mood, sfx_volume=sfx_volume)
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
            time.sleep(.045 if hub.status == 'Speaking' else .08 if social else .5 if resting else .13)

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
