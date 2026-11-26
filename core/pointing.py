"""Local, camera-relative pointing. Landmarks and images stay in memory."""
from collections import deque
import math
from statistics import median
import threading
import time

MODEL = 'pose_landmarker_lite.task'
HAND_MODEL = 'hand_landmarker.task'
LOOK_BACK = 1.35
GESTURE_HOLD = .29
ARMS = ((11, 13, 15, 19), (12, 14, 16, 20))


def pointing_region(landmarks, aspect):
    """Estimate a region beyond a held out hand using shoulder, elbow, wrist and index."""
    candidates = []
    for indices in ARMS:
        joints = [landmarks[index] for index in indices]
        if min(min(joint.visibility or 0, joint.presence or 0) for joint in joints) < .58:
            continue
        if any(not 0 <= joint.x <= 1 or not 0 <= joint.y <= 1 for joint in joints):
            continue
        shoulder, elbow, wrist, index = [(joint.x * aspect, joint.y) for joint in joints]
        upper = (elbow[0] - shoulder[0], elbow[1] - shoulder[1])
        forearm = (wrist[0] - elbow[0], wrist[1] - elbow[1])
        size = math.hypot(*forearm)
        upper_size = math.hypot(*upper)
        if min(size, upper_size) < .073:
            continue
        extension = sum(a*b for a, b in zip(upper, forearm)) / (size * upper_size)
        if extension < .70 or abs(forearm[0]) < .42 * size and forearm[1] > .3 * size:
            continue
        finger = (index[0] - wrist[0], index[1] - wrist[1])
        if sum(a*b for a, b in zip(finger, forearm)) < .25 * size * max(.022, math.hypot(*finger)):
            continue
        dx, dy = (.82 * forearm[0] / size, .82 * forearm[1] / size)
        finger_size = math.hypot(*finger)
        if finger_size > .022:
            dx += .18 * finger[0] / finger_size
            dy += .18 * finger[1] / finger_size
        # Project a short way beyond the wrist into the frame. The endpoint is
        # a region of interest in this camera, with no metric depth estimate.
        distance = min(.38, max(.20, size * 1.65))
        limits = [(boundary-origin)/delta for origin, delta, boundary in
                  ((wrist[0], dx, aspect*.96 if dx > 0 else aspect*.04),
                   (wrist[1], dy, .96 if dy > 0 else .04)) if abs(delta) > .001]
        distance = min(distance, *limits)
        if distance < .06:
            continue
        candidates.append(((wrist[0]+dx*distance)/aspect, wrist[1]+dy*distance, dx, dy))
    return candidates[0] if len(candidates) == 1 else None


def index_tip(hands):
    """Accept one extended index; folded/ambiguous hands leave the target unset."""
    if len(hands) != 1:
        return None
    hand = hands[0]
    def distance(a, b):
        return math.dist((hand[a].x, hand[a].y, hand[a].z), (hand[b].x, hand[b].y, hand[b].z))
    if distance(8, 0) < 1.17 * distance(6, 0) or distance(8, 5) < .86 * (distance(5, 6)+distance(6, 7)+distance(7, 8)):
        return None
    if sum(distance(tip, 0) > 1.12 * distance(tip-2, 0) for tip in (12, 16, 20)) > 1:
        return None
    tip = hand[8]
    return (tip.x, tip.y) if .025 < tip.x < .975 and .025 < tip.y < .975 else None


def pose_bodies(poses):
    """Extract normalized upper-body anchors and estimated head boxes from MediaPipe poses.

    Even when someone turns away, looks down, or covers their face with their hands, their
    shoulders and head/ear landmarks keep tracking the same physical person."""
    bodies = []
    for landmarks in (poses or ()):
        if len(landmarks) < 13:
            continue
        shoulders = [landmarks[i] for i in (11, 12)
                     if max(landmarks[i].visibility or 0., landmarks[i].presence or 0.) >= .42
                     and -.12 <= landmarks[i].x <= 1.12 and -.05 <= landmarks[i].y <= 1.15]
        head_pts = [landmarks[i] for i in (0, 2, 5, 7, 8, 9, 10)
                    if max(landmarks[i].visibility or 0., landmarks[i].presence or 0.) >= .38
                    and -.05 <= landmarks[i].x <= 1.05 and -.05 <= landmarks[i].y <= 1.02]
        if len(shoulders) < 2 and not (shoulders and head_pts):
            continue
        if len(shoulders) == 2:
            sx = (shoulders[0].x + shoulders[1].x) / 2
            sy = (shoulders[0].y + shoulders[1].y) / 2
            span = max(.12, min(.58, math.hypot(shoulders[0].x - shoulders[1].x, shoulders[0].y - shoulders[1].y)))
        else:
            hx_hint = sum(pt.x for pt in head_pts) / len(head_pts)
            sx = .65 * shoulders[0].x + .35 * hx_hint
            sy = shoulders[0].y
            span = .22
        hw = max(.085, min(.34, span * .58))
        hh = max(.11, min(.42, hw * 1.28))
        if head_pts:
            cx = sum(pt.x for pt in head_pts) / len(head_pts)
            cy = sum(pt.y for pt in head_pts) / len(head_pts)
        else:
            cx, cy = sx, max(.08, sy - hh * .92)
        left = max(0., min(1. - hw, cx - hw / 2))
        top = max(0., min(1. - hh, cy - hh / 2))
        bodies.append({
            'anchor': (max(0., min(1., sx)), max(0., min(1., sy))),
            'span': span,
            'head_box': (left, top, hw, hh),
        })
    return bodies


class Pointing:
    def __init__(self):
        self.pending = None
        self.frames = threading.Event()
        self.lock = threading.Lock()
        self.samples = deque(maxlen=95)
        self.bodies = None
        self.ready = False
        self.hand_ready = False
        self.hand_request = None
        self.hand_target = None

    def feed(self, source, received, frame):
        with self.lock:
            self.pending = source, received, frame
            self.frames.set()

    def observe(self, source, received, region, bodies=()):
        with self.lock:
            self.samples.append((source, received, region))
            self.bodies = (source, received, tuple(bodies))

    def recent_bodies(self, source, now, max_age=.65):
        with self.lock:
            item = self.bodies
        if not item or item[0] != source or now - item[1] > max_age:
            return ()
        return item[2]

    def during(self, source, span, now):
        start, end = span
        with self.lock:
            samples = tuple(self.samples)
        # Ignore unrelated people or poses from another camera, or after speech.
        candidates = []
        streak = []
        for camera, stamp, region in samples:
            if camera != source or stamp < start-.30 or stamp > min(end+.45, now):
                continue
            if not region or (streak and (
                    stamp - streak[-1][0] > .32
                    or math.dist(region[:2], streak[0][1][:2]) > .10
                    or sum(a*b for a,b in zip(region[2:], streak[0][1][2:])) < .88)):
                streak = []
            if region:
                streak.append((stamp, region))
                if len(streak) >= 3 and stamp-streak[0][0] >= GESTURE_HOLD and stamp >= end-LOOK_BACK:
                    candidates.append(tuple(median(item[1][axis] for item in streak) for axis in (0, 1)))
        if candidates and all(math.dist(region, candidates[-1]) < .16 for region in candidates):
            return candidates[-1]
        return None

    def start_hand(self, source, deadline):
        with self.lock:
            self.hand_request = (source, deadline)
            self.hand_target = None
        return self.hand_ready

    def fingertip(self, source, now):
        with self.lock:
            result = self.hand_target
            return result[2] if result and result[0] == source and now-result[1] <= .50 else None

    def run(self, model):
        from .hub import hub
        if not model.is_file():
            return
        try:
            import cv2
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions, vision

            options = vision.PoseLandmarkerOptions(
                BaseOptions(model_asset_path=str(model)), running_mode=vision.RunningMode.VIDEO,
                num_poses=3, min_pose_detection_confidence=.50, min_tracking_confidence=.50)
            import contextlib
            with contextlib.ExitStack() as resources:
                detector = resources.enter_context(vision.PoseLandmarker.create_from_options(options))
                hand_path = model.with_name(HAND_MODEL)
                hand_detector = None
                if hand_path.is_file():
                    try:
                        hand_detector = resources.enter_context(vision.HandLandmarker.create_from_options(
                            vision.HandLandmarkerOptions(BaseOptions(model_asset_path=str(hand_path)),
                                                         running_mode=vision.RunningMode.VIDEO, num_hands=2)))
                        self.hand_ready = True
                    except (OSError, RuntimeError, ValueError) as error:
                        hub.note('hand', f'Finger tracking unavailable: {error}')
                self.ready = True
                last = 0.
                while True:
                    self.frames.wait()
                    with self.lock:
                        item = self.pending
                        requested = self.hand_request
                        self.frames.clear()
                    source, received, frame = item
                    if received - last < .115 or time.monotonic()-received > .50:
                        continue
                    last = received
                    height, width = frame.shape[:2]
                    resized = cv2.resize(frame, (min(480, width), round(height*min(480, width)/width)))
                    image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(resized, cv2.COLOR_BGR2RGB))
                    poses = detector.detect_for_video(image, int(received*1000)).pose_landmarks
                    region = pointing_region(poses[0], width/height) if len(poses) == 1 else None
                    self.observe(source, received, region, pose_bodies(poses))
                    if hand_detector and requested and requested[0] == source and received < requested[1]:
                        hands = hand_detector.detect_for_video(image, int(received*1000))
                        tip = index_tip(hands.hand_landmarks) if len(poses) <= 1 else None
                        if tip:
                            last_tip = self.fingertip(source, received)
                            if last_tip:
                                tip = tuple(a*.57+b*.43 for a, b in zip(tip, last_tip))
                            with self.lock:
                                if self.hand_request == requested:
                                    self.hand_target = (source, received, tip)
        except (OSError, ImportError, RuntimeError, ValueError) as error:
            self.ready = False
            hub.note('pointing', f'Pointing recognition unavailable: {error}')
