"""BITCH's eyes: find faces (YuNet), track them between frames, and recognise people it has met (SFace).
Everything runs on the Mac. Known faces live in ~/.bitch/faces/ (faces.json + one photo per person)."""
import json, os, time, urllib.request
from collections import deque, Counter
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser('~/.bitch'); FACES = os.path.join(HOME, 'faces')
ZOO = 'https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/'
MODELS = {'yunet': 'face_detection_yunet/face_detection_yunet_2023mar.onnx',
          'sface': 'face_recognition_sface/face_recognition_sface_2021dec.onnx'}
MATCH = 0.42          # cosine similarity needed to say "that's you" (OpenCV suggests 0.363; a bit stricter = fewer mix-ups)

def model(key, note=print):
    p = os.path.join(HERE, os.path.basename(MODELS[key]))
    if not os.path.exists(p) or os.path.getsize(p) < 100_000:
        note(f'downloading the {"face finder" if key == "yunet" else "face recogniser"}...')
        try: urllib.request.urlretrieve(ZOO + MODELS[key], p)
        except Exception as e: note(f'download failed ({e})'); return None
    return p

class FaceDB:
    def __init__(s):
        os.makedirs(FACES, exist_ok=True); s.f = os.path.join(FACES, 'faces.json'); s.people = {}
        try: s.people = {k: {'emb': [np.array(e, np.float32) for e in v['emb']], 'last': v.get('last', 0)}
                         for k, v in json.load(open(s.f)).items()}
        except Exception: pass
    def save(s):
        try: json.dump({k: {'emb': [e.round(5).tolist() for e in v['emb']], 'last': v['last']} for k, v in s.people.items()}, open(s.f, 'w'))
        except Exception: pass
    def match(s, feat):
        best, score = None, 0.0
        for name, v in s.people.items():
            sc = max(float(np.dot(feat, e)) for e in v['emb'])
            if sc > score: best, score = name, sc
        return (best, score) if score >= MATCH else (None, score)
    def add(s, name, feat, photo=None):
        p = s.people.setdefault(name, {'emb': [], 'last': time.time()})
        if all(float(np.dot(feat, e)) < 0.93 for e in p['emb']):    # keep only views that add something new
            p['emb'] = (p['emb'] + [feat])[-24:]
        if photo is not None and not os.path.exists(s.photo(name)):
            open(s.photo(name), 'wb').write(photo)
        s.save()
    def forget(s, name):
        s.people.pop(name, None); s.save()
        try: os.remove(s.photo(name))
        except Exception: pass
    def photo(s, name): return os.path.join(FACES, ''.join(c for c in name if c.isalnum() or c in ' -_') + '.jpg')
    def names(s): return sorted(s.people)

class Track:
    _n = 0
    def __init__(s, row, now):
        Track._n += 1; s.id = Track._n; s.row = row; s.seen = now; s.born = now
        s.votes = deque(maxlen=6); s.name = None; s.score = 0.0; s.next_rec = now
    @property
    def box(s): return s.row[:4]
    def vote(s, name, score):
        s.votes.append(name); s.score = score
        top, n = Counter(v for v in s.votes if v).most_common(1)[0] if any(s.votes) else (None, 0)
        if top and n >= 2: s.name = top
        elif s.votes.count(None) >= 4: s.name = None

def iou(a, b):
    ax, ay, aw, ah = a; bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx)); iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    i = ix * iy; return i / (aw * ah + bw * bh - i + 1e-6)

class Eyes:
    """Call step(frame) for each camera frame; it returns the tracks currently visible."""
    def __init__(s, note=print):
        import cv2; s.cv2 = cv2; s.note = note; s.tracks = []; s.db = FaceDB(); s.rec = None; s.yn = None; s.haar = None
        p = model('yunet', note)
        if p:
            try: s.yn = cv2.FaceDetectorYN.create(p, '', (640, 360), 0.6, 0.3, 20)
            except Exception as e: note(f'face finder failed ({e})')
        if s.yn is None: s.haar = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        p = model('sface', note)
        if p and s.yn is not None:
            try: s.rec = cv2.FaceRecognizerSF.create(p, '')
            except Exception as e: note(f'face recogniser failed ({e})')
        if s.rec is None: note('face recognition off (will still find faces)')
    def detect(s, fr):
        H, W = fr.shape[:2]
        if s.yn is not None:
            s.yn.setInputSize((W, H)); _, r = s.yn.detect(fr)
            return [] if r is None else [row.astype(np.float32) for row in r]
        g = s.cv2.equalizeHist(s.cv2.cvtColor(fr, s.cv2.COLOR_BGR2GRAY))
        return [np.array([x, y, w, h] + [0] * 10 + [0.7], np.float32) for (x, y, w, h) in s.haar.detectMultiScale(g, 1.1, 4, minSize=(36, 36))]
    def feature(s, fr, row):
        f = s.rec.feature(s.rec.alignCrop(fr, row)).reshape(-1).astype(np.float32)
        return f / (np.linalg.norm(f) + 1e-6)
    def step(s, fr, enroll=None):
        """enroll = {'track': id, 'name': str}: collect samples of that face under that name. Returns (tracks, enrolled_count)."""
        now = time.time(); rows = s.detect(fr); used = set(); got = 0
        for row in sorted(rows, key=lambda r: -r[2] * r[3]):            # match detections to existing tracks
            best, bi = None, 0.25
            for t in s.tracks:
                if t.id in used: continue
                o = iou(row[:4], t.box)
                if o > bi: best, bi = t, o
            if best is None:
                cx, cy = row[0] + row[2] / 2, row[1] + row[3] / 2
                for t in s.tracks:
                    if t.id not in used and now - t.seen < 0.6 and abs(cx - t.box[0] - t.box[2] / 2) < row[2] * 0.7 and abs(cy - t.box[1] - t.box[3] / 2) < row[3] * 0.7:
                        best = t; break
            if best is None: best = Track(row, now); s.tracks.append(best)
            best.row = row; best.seen = now; used.add(best.id)
        s.tracks = [t for t in s.tracks if now - t.seen < 1.2]
        if s.rec is not None:
            for t in s.tracks:
                if t.id not in used: continue
                is_enroll = enroll and enroll.get('track') == t.id
                if not is_enroll and now < t.next_rec: continue
                try: feat = s.feature(fr, t.row)
                except Exception: continue
                if is_enroll:
                    x, y, w, h = [int(v) for v in t.box]; m = int(0.35 * w)
                    crop = fr[max(0, y - m):y + h + m, max(0, x - m):x + w + m]
                    ok, jp = s.cv2.imencode('.jpg', s.cv2.resize(crop, (160, int(160 * crop.shape[0] / max(1, crop.shape[1])))), [s.cv2.IMWRITE_JPEG_QUALITY, 85])
                    s.db.add(enroll['name'], feat, jp.tobytes() if ok else None); got += 1
                    t.votes.extend([enroll['name']] * 2); t.name = enroll['name']
                else:
                    name, sc = s.db.match(feat); t.vote(name, sc)
                t.next_rec = now + (0.35 if t.name is None else 1.5)
                if t.name and t.name in s.db.people: s.db.people[t.name]['last'] = now
        return [t for t in s.tracks if t.id in used], got
