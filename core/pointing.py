"""Local, camera-relative pointing. Landmarks and images stay in memory."""
from collections import deque
import math
from statistics import median
import threading
import time

MODEL = 'pose_landmarker_lite.task'
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


class Pointing:
    def __init__(self):
        self.pending = None
        self.frames = threading.Event()
        self.lock = threading.Lock()
        self.samples = deque(maxlen=95)
        self.ready = False

    def feed(self, source, received, frame):
        with self.lock:
            self.pending = source, received, frame
            self.frames.set()

    def observe(self, source, received, region):
        with self.lock:
            self.samples.append((source, received, region))

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
                num_poses=2, min_pose_detection_confidence=.60)
            with vision.PoseLandmarker.create_from_options(options) as detector:
                self.ready = True
                last = 0.
                while True:
                    self.frames.wait()
                    with self.lock:
                        item = self.pending
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
                    self.observe(source, received, region)
        except (OSError, ImportError, RuntimeError, ValueError) as error:
            self.ready = False
            hub.note('pointing', f'Pointing recognition unavailable: {error}')
