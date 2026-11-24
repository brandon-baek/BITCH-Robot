#!/usr/bin/env python3
"""
Desk Creature companion: runs on your Mac. Open http://localhost:8770 for the control room.
  ears:  Mac mic -> Whisper large-v3-turbo (on-device, MLX)
  brain: Qwen in Ollama (on-device) -> what to say + emotion + action
  voice: real words (Kokoro TTS, gently robotized) or creature babble
  eyes:  Mac/iPhone camera -> YuNet face finder -> everyone's position on the desk map -> robot looks at you
"""
import argparse, json, math, os, queue, random, re, sys, tempfile, threading, time, urllib.request, urllib.parse, wave, struct
HERE=os.path.dirname(os.path.abspath(__file__)); ROOT=os.path.dirname(HERE); sys.path.insert(0,ROOT)
ap=argparse.ArgumentParser()
ap.add_argument('--pi',default='raspberrypi.local'); ap.add_argument('--model',default='qwen3.5:4b')
ap.add_argument('--babble',action='store_true',help='start in creature-babble voice instead of words')
ap.add_argument('--voice',default='af_heart',help='Kokoro voice')
ap.add_argument('--whisper',default='mlx-community/whisper-large-v3-turbo')
ap.add_argument('--camera',type=int,default=0); ap.add_argument('--no-mic',action='store_true'); ap.add_argument('--no-camera',action='store_true')
ap.add_argument('--no-browser',action='store_true')
args=ap.parse_args(); PI=f'http://{args.pi}:8765'

# ---------------- shared state (read by the control room) ----------------
S={'status':'starting','interrupted':False,'level':0.0,'log':[],'people':[],'target':None,'robot':False,'asleep':False,'mute_until':0,
   'voice':'babble' if args.babble else 'words','robotness':0.5,'mic_on':not args.no_mic,'model':args.model,'cam_ok':False,'notes':[]}
LOCK=threading.Lock(); JPEG=[None]
def log(*a): print(*a,flush=True)
def note(msg):
    log('  '+msg)
    with LOCK: S['notes']=(S['notes']+[msg])[-6:]
def chat(role,text,**k):
    with LOCK: S['log']=(S['log']+[dict(role=role,text=text,t=time.time(),**k)])[-60:]
def status(s): S['status']=s

# ---------------- robot link ----------------
def pi_get(path,**q):
    try: r=json.loads(urllib.request.urlopen(PI+path+'?'+urllib.parse.urlencode(q),timeout=3).read()); S['robot']=True; return r
    except Exception: S['robot']=False; return None
def pi_post(path,data,**q):
    try:
        r=urllib.request.Request(PI+path+'?'+urllib.parse.urlencode(q),data=data,method='POST',headers={'Content-Type':'audio/wav'})
        out=json.loads(urllib.request.urlopen(r,timeout=15).read()); S['robot']=True; return out
    except Exception as e: S['robot']=False; note(f'could not reach the robot ({e})'); return None

# ---------------- brain ----------------
EMOTIONS=['happy','sad','curious','excited','love','angry','scared','sleepy','proud','confused','neutral']
ACTIONS=['none','nod','shake','wiggle','look_around','lean_in','lean_back','spin','dance','sing','sleep','bow','wave']
SYSTEM=f"""You are Desk Creature, a tiny cardboard desk-lamp robot with googly eyes. Brandon built you from a shoebox,
popsicle sticks and four little servo motors. You are curious, playful, a bit shy and very sweet, like WALL-E.
You love music, dancing and the people around you. Speak in short, simple, clear sentences (max 18 words, max two sentences).
No emojis, no stage directions, no sound effects in text.
Reply ONLY with JSON: {{"respond": true/false, "say": "...", "emotion": one of {EMOTIONS}, "action": one of {ACTIONS}}}.
Set "respond": false (and say "") when the words were clearly NOT meant for you: people talking to each other, a phone call,
TV or video audio, song lyrics, or a meaningless fragment. When in doubt and someone is in front of you, respond.
Use 'dance' only when asked to dance, 'sing' only when asked to sing, 'sleep' only for good night or go to sleep."""
HIST=[]
def think(text):
    n=len(S['people']); scene=f"[camera: {n} {'person' if n==1 else 'people'} in front of you]"
    msgs=[{'role':'system','content':SYSTEM}]+HIST[-12:]+[{'role':'user','content':f'{text}  {scene}'}]
    out=None; err=None
    for model,extra in ((args.model,{'think':False}),(args.model,{}),('qwen3:4b-instruct',{})):
        body=json.dumps({'model':model,'messages':msgs,'stream':False,'format':'json','options':{'temperature':0.7},**extra}).encode()
        try:
            r=json.loads(urllib.request.urlopen(urllib.request.Request('http://localhost:11434/api/chat',data=body,headers={'Content-Type':'application/json'}),timeout=90).read())
            out=json.loads(r['message']['content']); break
        except Exception as e: err=e
    if out is None:
        note(f'brain problem: open Ollama, then run: ollama pull {args.model}  ({err})')
        out={'say':'Beep? My brain is sleepy.','emotion':'confused','action':'none'}
    say=re.sub(r'[*_#`~]|\(.*?\)','',str(out.get('say',''))).strip()[:200] or '...'
    emo=out.get('emotion','neutral'); emo=emo if emo in EMOTIONS else 'neutral'
    act=out.get('action','none'); act=act if act in ACTIONS else 'none'
    if out.get('respond') is False or str(out.get('respond')).lower()=='false': return None,emo,act
    HIST.append({'role':'user','content':text}); HIST.append({'role':'assistant','content':json.dumps({'say':say,'emotion':emo,'action':act})})
    return say,emo,act

# ---------------- voice ----------------
import sounds
def wav_bytes(samples,sr):
    pk=max(1e-6,max(abs(float(x)) for x in samples)); g=0.9/pk
    f=tempfile.NamedTemporaryFile(suffix='.wav',delete=False); f.close()
    with wave.open(f.name,'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(b''.join(struct.pack('<h',int(max(-1,min(1,float(x)*g))*32000)) for x in samples))
    d=open(f.name,'rb').read(); os.remove(f.name); return d,len(samples)/sr
_kok=[None]
def words_audio(text,emotion):
    import numpy as np
    if _kok[0] is None:
        note('loading the word voice (first time downloads it)...')
        from kokoro import KPipeline; _kok[0]=KPipeline(lang_code='a')
    parts=[a for _,_,a in _kok[0](text,voice=args.voice,speed=1.0)]
    a=np.concatenate([np.asarray(p,dtype=np.float32).reshape(-1) for p in parts]) if parts else np.zeros(2400,np.float32)
    R=float(S['robotness'])
    up=1+0.16*R*{'excited':1.3,'happy':1.15,'scared':1.3,'sad':0.7,'sleepy':0.6,'angry':0.6}.get(emotion,1.0)
    n=int(len(a)*22050/(24000/up)); y=np.interp(np.linspace(0,len(a)-1,n),np.arange(len(a)),a)    # small-creature pitch
    y=y/(np.abs(y).max()+1e-6)
    if R>0:
        y=np.tanh((1+1.2*R)*y)/np.tanh(1+1.2*R)                                                   # warm grit
        t=np.arange(len(y))/22050; y=y*(1+0.05*R*np.sin(2*np.pi*38*t))                             # faint electronic shimmer
        q=int(64-40*R); y=np.round(y*q)/q                                                          # digital edge
    tail=[]
    chirp={'happy':'happy','excited':'excited','sad':'sad','love':'love','curious':'curious','confused':'question','scared':'whimper','proud':'tada','angry':'grumble'}.get(emotion)
    if chirp and R>0.15 and random.random()<0.7: tail=[0.55*v for v in sounds.V[chirp]()]
    return list(y)+[0.0]*int(0.07*22050)+tail
def speak(text,emotion,action):
    status('speaking')
    s=None
    if S['voice']=='words':
        try: s=words_audio(text,emotion)
        except Exception as e: note(f'word voice unavailable, using babble ({e})'); S['voice']='babble'
    if s is None: s=sounds._fx(sounds.speak_audio(text,emotion)[0])
    data,dur=wav_bytes(s,22050)
    S['interrupted']=False
    pi_post('/speak',data,emotion=emotion,action=action,text=text)
    end=time.time()+dur+0.4
    while time.time()<end and not S['interrupted']: time.sleep(0.05)

# ---------------- ears ----------------
# Silero VAD tells real speech apart from claps, music, typing and other noise.
# A 0.8 s rolling memory means the first words are never cut off.
# You can talk over the robot: strong speech while it talks stops it (barge-in).
HEARD=queue.Queue()
JUNK=re.compile(r"^\W*(you|thank you\.?|thanks for watching\.?|bye\.?|\.+|uh+|um+|hmm+|mm+)\W*$",re.I)
def interrupt():
    if S['status']=='speaking':
        S['interrupted']=True; pi_get('/stop'); status('hearing'); chat('note','(interrupted)')
def ears():
    import numpy as np, sounddevice as sd, mlx_whisper
    status('loading ears'); note('loading Whisper (first time downloads ~1.6 GB)...')
    mlx_whisper.transcribe(np.zeros(16000,np.float32),path_or_hf_repo=args.whisper)
    vad=None
    try:
        import torch; from silero_vad import load_silero_vad
        vad=load_silero_vad(); torch.set_num_threads(1); note('ears ready (speech detector on)')
    except Exception as e: note(f'speech detector unavailable, using loudness only ({e})')
    SR=16000; CH=512; q=queue.Queue()
    pre=[]; buf=[]; talking=False; vc=0; on_run=0; off_run=0; barge=0; echo=0.004; floor=0.003
    def prob(x):
        if vad is None:
            r=float(np.sqrt(np.mean(x*x))); return 1.0 if r>max(0.008,floor*2.8) else 0.0
        return float(vad(torch.from_numpy(x),SR).item())
    with sd.InputStream(samplerate=SR,channels=1,blocksize=CH,dtype='float32',callback=lambda d,f,t,s: q.put(d[:,0].copy())):
        while True:
            x=q.get(); r=float(np.sqrt(np.mean(x*x))); S['level']=min(1.0,r*12)
            if not S['mic_on'] or S['status'] in ('thinking','understanding'): pre=[]; continue
            if S['status'] in ('starting','loading ears'): status('listening')
            p=prob(x)
            if not talking:
                floor=0.985*floor+0.015*r if p<0.3 else floor
                pre=(pre+[x])[-25:]                                   # ~0.8 s of memory before speech starts
                if S['status']=='speaking':
                    if p<0.5: echo=0.97*echo+0.03*r                   # how loud the robot's own voice is at the mic
                    strong=p>0.85 and r>echo*2.2
                    barge=barge+1 if strong else 0
                    if barge>=10: interrupt(); talking=True; buf=list(pre); off_run=0; barge=0
                    continue
                on_run=on_run+1 if p>0.6 else 0
                if on_run>=2: talking=True; buf=list(pre); off_run=0; status('hearing')
            else:
                buf.append(x); off_run=off_run+1 if p<0.35 else 0; vc+=p>0.5
                if off_run>=22 or len(buf)>int(15*SR/CH):             # ~0.7 s of quiet ends the sentence
                    talking=False; on_run=0; audio=np.concatenate(buf); buf=[]; pre=[]
                    voiced=vc*CH/SR if vad else 1; vc=0
                    if vad is not None: vad.reset_states()
                    if len(audio)>SR*0.4 and voiced>0.25:
                        status('understanding')
                        res=mlx_whisper.transcribe(audio,path_or_hf_repo=args.whisper,language='en',condition_on_previous_text=False,
                              initial_prompt='A person is chatting with Desk Creature, a little desk robot.')
                        segs=res.get('segments') or []
                        good=[sg for sg in segs if sg.get('no_speech_prob',0)<0.6 and sg.get('avg_logprob',0)>-1.1]
                        txt=' '.join(sg['text'].strip() for sg in good).strip() if segs else res['text'].strip()
                        if txt and not JUNK.match(txt): HEARD.put(txt)
                        else: status('listening')
                    elif S['status']=='hearing': status('listening')

# ---------------- eyes ----------------
CFG_F=os.path.expanduser('~/.desk_camera.json')
CAM={'x':0.0,'y':15.0,'yaw':0.0,'fov':70.0,'face_cm':15.0,'flip':False}
try: CAM.update(json.load(open(CFG_F)))
except Exception: pass
def save_cam():
    try: json.dump(CAM,open(CFG_F,'w'))
    except Exception: pass
def to_robot(px,py):
    ang=math.degrees(math.atan2(px,max(1e-3,py)))
    return max(-0.9,min(0.9,(ang/90)*(-1 if CAM['flip'] else 1)))
def person_from_face(cx,cy,fw):
    t=math.tan(math.radians(CAM['fov']/2)); a=math.atan((cx-0.5)*2*t); d=CAM['face_cm']/max(0.02,fw*2*t)
    fwd,rt=d*math.cos(a),d*math.sin(a); th=math.radians(CAM['yaw'])
    return CAM['x']+fwd*math.sin(th)+rt*math.cos(th), CAM['y']+fwd*math.cos(th)-rt*math.sin(th)
YUNET=os.path.join(HERE,'face_detection_yunet_2023mar.onnx')
def get_yunet():
    if not os.path.exists(YUNET):
        try:
            note('downloading the face finder (230 KB)...')
            urllib.request.urlretrieve('https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx',YUNET)
        except Exception as e: note(f'face finder download failed, using the basic one ({e})'); return None
    return YUNET
def eyes():
    import cv2
    cam=cv2.VideoCapture(args.camera)
    if not cam.isOpened(): note('camera not available: System Settings > Privacy > Camera, allow Terminal'); return
    model=get_yunet(); yn=None
    if model:
        try: yn=cv2.FaceDetectorYN.create(model,'',(640,360),0.6,0.3,20)
        except Exception as e: note(f'face finder failed to load ({e})')
    haar=cv2.CascadeClassifier(cv2.data.haarcascades+'haarcascade_frontalface_default.xml') if yn is None else None
    S['cam_ok']=True; note('camera on'); tx=ty=None; miss=0; last_send=0
    while True:
        ok,fr=cam.read()
        if not ok: time.sleep(0.1); continue
        fr=cv2.resize(fr,(640,int(640*fr.shape[0]/fr.shape[1]))); H,W=fr.shape[:2]
        faces=[]
        if yn is not None:
            yn.setInputSize((W,H)); _,res=yn.detect(fr)
            if res is not None: faces=[(float(r[0]),float(r[1]),float(r[2]),float(r[3]),float(r[-1])) for r in res]
        else:
            g=cv2.equalizeHist(cv2.cvtColor(fr,cv2.COLOR_BGR2GRAY))
            faces=[(x,y,w,h,0.7) for (x,y,w,h) in haar.detectMultiScale(g,1.1,4,minSize=(36,36))]
        people=[]
        for (x,y,w,h,sc) in faces:
            px,py=person_from_face((x+w/2)/W,(y+h/2)/H,w/W); people.append({'x':px,'y':py,'box':[x,y,w,h],'score':sc,'d':math.hypot(px,py)})
        tgt=min(people,key=lambda p:p['d']) if people else None          # closest person gets the attention
        for p in people: p['target']=p is tgt
        if tgt:
            tx=tgt['x'] if tx is None else tx+0.45*(tgt['x']-tx); ty=tgt['y'] if ty is None else ty+0.45*(tgt['y']-ty); miss=0
            if time.time()-last_send>0.12:
                last_send=time.time(); vy=(tgt['box'][1]+tgt['box'][3]/2)/H
                pi_get('/look',b=f'{to_robot(tx,ty):.3f}',n=f'{max(-1,min(-0.3,-0.8+(0.5-vy)*0.6)):.3f}',seen=1)
        else:
            miss+=1
            if miss==10: pi_get('/look',seen=0)
        with LOCK: S['people']=[{'x':p['x'],'y':p['y'],'target':p['target'],'score':p['score']} for p in people]
        for p in people:                                                  # draw what it sees for the preview
            x,y,w,h=map(int,p['box']); col=(48,196,244) if p['target'] else (223,108,45)
            cv2.rectangle(fr,(x,y),(x+w,y+h),col,3 if p['target'] else 2)
            lab=f"{p['d']/100:.1f} m" + ('  looking' if p['target'] else '')
            cv2.putText(fr,lab,(x,max(18,y-8)),cv2.FONT_HERSHEY_SIMPLEX,0.55,col,2,cv2.LINE_AA)
        ok,jp=cv2.imencode('.jpg',fr,[cv2.IMWRITE_JPEG_QUALITY,70])
        if ok: JPEG[0]=jp.tobytes()
        time.sleep(0.05)

# ---------------- control room server ----------------
def server():
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    class H(BaseHTTPRequestHandler):
        def _send(s,body,ct='application/json'):
            s.send_response(200); s.send_header('Content-Type',ct); s.send_header('Cache-Control','no-store'); s.end_headers(); s.wfile.write(body)
        def do_GET(s):
            u=urllib.parse.urlparse(s.path); q={k:v[0] for k,v in urllib.parse.parse_qs(u.query).items()}
            if u.path=='/state':
                with LOCK: st=dict(S); st['cam']=CAM; st['log']=list(S['log']); st['people']=list(S['people'])
                st['mute_until']=0; s._send(json.dumps(st).encode())
            elif u.path=='/video':
                s.send_response(200); s.send_header('Content-Type','multipart/x-mixed-replace; boundary=frame'); s.end_headers()
                try:
                    while True:
                        if JPEG[0]: s.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'+JPEG[0]+b'\r\n')
                        time.sleep(0.1)
                except Exception: return
            elif u.path=='/set':
                for k in ('x','y','yaw','fov'):
                    if k in q: CAM[k]=float(q[k])
                if 'flip' in q: CAM['flip']=q['flip']=='1'
                if 'voice' in q: S['voice']=q['voice']
                if 'robotness' in q: S['robotness']=float(q['robotness'])
                if 'mic' in q: S['mic_on']=q['mic']=='1'
                save_cam(); s._send(b'{"ok":true}')
            elif u.path=='/aim':
                pi_get('/look',b=f"{to_robot(float(q['x']),float(q['y'])):.3f}",n='-0.8',seen=1); s._send(b'{"ok":true}')
            elif u.path=='/stop': interrupt(); s._send(b'{"ok":true}')
            elif u.path=='/type':
                if q.get('text','').strip(): HEARD.put(q['text'].strip())
                s._send(b'{"ok":true}')
            elif u.path=='/emote':
                pi_get('/emote',emotion=q.get('emotion','neutral'),action=q.get('action','none')); s._send(b'{"ok":true}')
            else: s._send(open(os.path.join(HERE,'dashboard.html'),'rb').read(),'text/html')
        def log_message(s,*a): pass
    ThreadingHTTPServer(('127.0.0.1',8770),H).serve_forever()

def main():
    log(f'\n  DESK CREATURE COMPANION\n  control room: http://localhost:8770\n')
    threading.Thread(target=server,daemon=True).start()
    if not args.no_browser:
        import webbrowser; threading.Timer(1.0,lambda: webbrowser.open('http://localhost:8770')).start()
    if pi_get('/ping') is None: note(f'robot not found at {args.pi}: start listen mode on the Pi (same Wi-Fi)')
    if not args.no_mic: threading.Thread(target=lambda: _safe(ears,'mic'),daemon=True).start()
    else: status('listening')
    if not args.no_camera: threading.Thread(target=lambda: _safe(eyes,'camera'),daemon=True).start()
    threading.Thread(target=typing,daemon=True).start()
    def ping():
        while True: pi_get('/ping'); time.sleep(3)
    threading.Thread(target=ping,daemon=True).start()
    while True:
        text=HEARD.get(); chat('you',text); log(f'  you: {text}')
        if S['asleep']:
            if re.search(r'\b(wake|morning|hey|hello|hi)\b',text.lower()):
                S['asleep']=False; pi_get('/emote',emotion='happy',action='wake'); time.sleep(4)
            else: chat('robot','(asleep: say "wake up")'); continue
        status('thinking'); say,emo,act=think(text)
        if say is None:
            chat('note','(not meant for me, ignored)'); log('  (ignored: not meant for the robot)'); status('listening'); continue
        chat('robot',say,emotion=emo,action=act); log(f'  robot: {say}   [{emo}, {act}]')
        speak(say,emo,act)
        if S['interrupted']: S['interrupted']=False; continue
        if act=='sleep': S['asleep']=True; status('asleep')
        elif S['status']=='speaking': status('listening')
def typing():
    while True:
        try: t=input()
        except EOFError: return
        if t.strip(): HEARD.put(t.strip())
def _safe(fn,name):
    try: fn()
    except ImportError as e: note(f'{name} off: missing {e.name}. Run: bash ~/BITCH-Robot/mac/setup.sh')
    except Exception as e: note(f'{name} stopped: {e}')
if __name__=='__main__':
    try: main()
    except KeyboardInterrupt: log('\n  bye!')
