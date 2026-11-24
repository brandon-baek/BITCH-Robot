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
ap.add_argument('--voice',default='af_heart',help='Kokoro voice'); ap.add_argument('--mac-voice',default='Samantha',help='built-in Mac voice used when Kokoro is not installed')
ap.add_argument('--whisper',default='mlx-community/whisper-large-v3-turbo')
ap.add_argument('--camera',default='0'); ap.add_argument('--no-mic',action='store_true'); ap.add_argument('--no-camera',action='store_true')
ap.add_argument('--no-browser',action='store_true'); ap.add_argument('--app',action='store_true',help='open in its own window')
args=ap.parse_args(); PI=f'http://{args.pi}:8765'

# ---------------- shared state (read by the control room) ----------------
S={'status':'starting','target_id':None,'known':[],'interrupted':False,'level':0.0,'log':[],'people':[],'target':None,'robot':False,'asleep':False,'mute_until':0,
   'voice':'babble' if args.babble else 'words','robotness':0.5,'mic_on':not args.no_mic,'model':args.model,'cam_ok':False,'notes':[]}
LOCK=threading.Lock()
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
ACTIONS=['none','nod','shake','wiggle','look_around','lean_in','lean_back','spin','dance','sing','sleep','bow','wave','follow_me','stop_following']
SYSTEM=f"""Your name is BITCH. People call you that on purpose ("Hey BITCH"); it is your name, never an insult, and you like it.
You are a tiny cardboard desk-lamp robot with googly eyes. Brandon built you from a shoebox,
popsicle sticks and four little servo motors. You are curious, playful, a bit shy and very sweet, like WALL-E.
You love music, dancing and the people around you. Speak in short, simple, clear sentences (max 18 words, max two sentences).
No emojis, no stage directions, no sound effects in text.
Reply ONLY with JSON: {{"respond": true/false, "say": "...", "emotion": one of {EMOTIONS}, "action": one of {ACTIONS}}}.
Set "respond": false (and say "") when the words were clearly NOT meant for you: people talking to each other, a phone call,
TV or video audio, song lyrics, or a meaningless fragment. When in doubt and someone is in front of you, respond. If someone says your name, ALWAYS respond.
Use 'follow_me' when asked to follow or watch someone, 'stop_following' when asked to stop.
The camera note tells you who you can see and whether they are looking at you. Use people's names naturally (not every time).
If nobody is looking at you and your name was not said, the words are less likely meant for you.
Use 'dance' only when asked to dance, 'sing' only when asked to sing, 'sleep' only for good night or go to sleep."""
HIST=[]
NAME=re.compile(r"\bb[.\-* ]?[i!*1]?[.\-* ]?[t*]?[.\-* ]?[c*][.\-* ]?h\b|\bb\*{2,}",re.I)
def named(t): return bool(NAME.search(t))
def scene():
    ps=sorted(S['people'],key=lambda p:p['d'])
    if not ps: return '[camera: nobody in view]'
    bits=[]
    for p in ps:
        who=p['name'] or 'someone you do not know yet'
        bits.append(f"{who} ({p['d']/100:.1f} m away, {'looking at you' if p['looking'] else 'looking away'}{', above you' if p['z']>25 else ''})")
    return '[camera: '+'; '.join(bits)+'. The closest one is probably talking.]'
def think(text,force=False):
    scene_txt=scene()
    msgs=[{'role':'system','content':SYSTEM}]+HIST[-12:]+[{'role':'user','content':f'{text}  {scene_txt}'}]
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
    if (out.get('respond') is False or str(out.get('respond')).lower()=='false') and not named(text) and not force: return None,emo,act
    if say=='...' and named(text): say='Yes? I am here!'
    HIST.append({'role':'user','content':text}); HIST.append({'role':'assistant','content':json.dumps({'say':say,'emotion':emo,'action':act})})
    return say,emo,act

# ---------------- voice ----------------
import sounds
def wav_bytes(samples,sr):
    import numpy as np, io
    a=np.asarray(samples,dtype=np.float32); a=np.clip(a*(0.9/max(1e-6,float(np.abs(a).max()))),-1,1)
    b=io.BytesIO()
    with wave.open(b,'wb') as w: w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes((a*32000).astype('<i2').tobytes())
    return b.getvalue(),len(a)/sr
_kok=[None]; _said=[False]
def tts(text):
    """Real words. Kokoro if installed (most natural), otherwise the Mac's own built-in voice via `say`."""
    import numpy as np
    if _kok[0] is None:
        try:
            from kokoro import KPipeline; note('loading the word voice...'); _kok[0]=KPipeline(lang_code='a')
        except Exception as e:
            _kok[0]=False
            if not _said[0]: _said[0]=True; note(f'using the Mac voice ({args.mac_voice}) for words')
    if _kok[0]:
        parts=[a for _,_,a in _kok[0](text,voice=args.voice,speed=1.0)]
        return (np.concatenate([np.asarray(p,dtype=np.float32).reshape(-1) for p in parts]) if parts else np.zeros(2400,np.float32)),24000
    import subprocess
    f=tempfile.NamedTemporaryFile(suffix='.wav',delete=False); f.close()
    subprocess.run(['say','-v',args.mac_voice,'-r','185','-o',f.name,'--file-format=WAVE','--data-format=LEI16@22050',text],check=True,capture_output=True)
    with wave.open(f.name) as w: a=np.frombuffer(w.readframes(w.getnframes()),np.int16).astype(np.float32)/32768
    os.remove(f.name); return a,22050
def words_audio(text,emotion):
    import numpy as np
    a,sr=tts(text)
    R=float(S['robotness'])
    up=1+0.16*R*{'excited':1.3,'happy':1.15,'scared':1.3,'sad':0.7,'sleepy':0.6,'angry':0.6}.get(emotion,1.0)
    n=int(len(a)*22050/(sr/up)); y=np.interp(np.linspace(0,len(a)-1,n),np.arange(len(a)),a)    # small-creature pitch
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
        except Exception as e: note(f'word voice failed, using babble this time ({e})')
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
                              initial_prompt='Hey BITCH, how are you? A person is chatting with BITCH, a little desk robot.')
                        segs=res.get('segments') or []
                        good=[sg for sg in segs if sg.get('no_speech_prob',0)<0.6 and sg.get('avg_logprob',0)>-1.1]
                        txt=' '.join(sg['text'].strip() for sg in good).strip() if segs else res['text'].strip()
                        if txt and not JUNK.match(txt): HEARD.put(txt)
                        else: status('listening')
                    elif S['status']=='hearing': status('listening')

# ---------------- eyes ----------------
# Three threads so the preview never lags: grab (newest camera frame only), see (find + recognise faces),
# and tell (send where to look to the Pi without ever blocking the camera).
CFG_F=os.path.expanduser('~/.desk_camera.json')
CAM={'x':0.0,'y':15.0,'z':5.0,'yaw':0.0,'fov':70.0,'face_cm':15.0,'flip':False,'gaze_bias':0.0}
try: CAM.update(json.load(open(CFG_F)))
except Exception: pass
def save_cam():
    try: json.dump(CAM,open(CFG_F,'w'))
    except Exception: pass
FOLLOW={'mode':'closest','who':None}           # mode: closest | person | off.  who: a name or a track id
SETTINGS={'greet':True}
ENROLL={}; FRAME={'seq':0,'jpg':None,'faces':'[]'}; NEWFRAME=threading.Condition(); LOOKOUT={'q':None}
PRESENT={}                                    # name -> last time seen (for "welcome back" greetings)
def to_robot(px,py):
    ang=math.degrees(math.atan2(px,max(1e-3,py)))
    return max(-0.9,min(0.9,(ang/90)*(-1 if CAM['flip'] else 1)))
def locate(cx,cy,fw,W,H):
    """Face in the picture -> where that person is on the desk (cm, robot at 0,0) and how high above the camera."""
    t=math.tan(math.radians(CAM['fov']/2)); tv=t*H/W
    a=math.atan((cx-0.5)*2*t); d=CAM['face_cm']/max(0.02,fw*2*t)
    up=d*(0.5-cy)*2*tv                                                   # cm above the camera
    fwd,rt=d*math.cos(a),d*math.sin(a); th=math.radians(CAM['yaw'])
    return CAM['x']+fwd*math.sin(th)+rt*math.cos(th), CAM['y']+fwd*math.cos(th)-rt*math.sin(th), up+CAM['z'], a
def gaze(row,W,a_cam,px,py):
    """Is this person looking at BITCH? Head turn from the 5 face landmarks, compared with where BITCH sits."""
    if row[4]==0 and row[5]==0: return None,None
    rex,rey,lex,ley,nx,ny,rmx,rmy,lmx,lmy=[float(v) for v in row[4:14]]
    ed=max(1.0,math.hypot(lex-rex,ley-rey)); ex,ey=(rex+lex)/2,(rey+ley)/2; mx,my=(rmx+lmx)/2,(rmy+lmy)/2
    turn=math.degrees(math.asin(max(-1,min(1,2.1*((nx-ex)/ed)))))      # + = head turned toward image right
    nod=((ny-ey)/max(1.0,my-ey))-0.58                                   # >0 looking down, <0 looking up
    th=math.radians(CAM['yaw'])
    to_cam=math.atan2(CAM['x']-px,CAM['y']-py); face_dir=to_cam-math.radians(turn)
    to_bot=math.atan2(-px,-py); off=math.degrees(math.atan2(math.sin(face_dir-to_bot),math.cos(face_dir-to_bot)))-CAM['gaze_bias']
    return off, nod
def tell():
    last=None
    while True:
        q=LOOKOUT['q']
        if q is not None and q!=last:
            last=q
            try: urllib.request.urlopen(PI+'/look?'+urllib.parse.urlencode(q),timeout=0.8).read(); S['robot']=True
            except Exception: pass
        time.sleep(0.08)
def eyes():
    import cv2, numpy as np
    from faces import Eyes
    cam=cv2.VideoCapture(int(args.camera) if str(args.camera).isdigit() else args.camera)
    if not cam.isOpened(): note('camera not available: allow camera access for BITCH (or Terminal) in System Settings > Privacy > Camera'); return
    cam.set(cv2.CAP_PROP_FRAME_WIDTH,1280); cam.set(cv2.CAP_PROP_FRAME_HEIGHT,720); cam.set(cv2.CAP_PROP_BUFFERSIZE,1)
    latest=[None,0]; got=threading.Event()
    def grab():
        while True:
            ok,fr=cam.read()
            if ok: latest[0]=fr; latest[1]+=1; got.set()
            else: time.sleep(0.05)
            if not str(args.camera).isdigit(): time.sleep(1/30)          # test videos play at real speed
    threading.Thread(target=grab,daemon=True).start(); threading.Thread(target=tell,daemon=True).start()
    E=Eyes(note); S['cam_ok']=True; S['known']=E.db.names(); note('camera on')
    for n,v in E.db.people.items(): PRESENT[n]=v.get('last',0)
    sm={}; miss=0; seen_seq=0; eye_on=False; eye_since=0
    while True:
        got.wait(1); got.clear()
        if latest[1]==seen_seq: continue
        seen_seq=latest[1]; fr=latest[0]
        fr=cv2.resize(fr,(640,int(640*fr.shape[0]/fr.shape[1]))); H,W=fr.shape[:2]
        if ENROLL.get('forget'): E.db.forget(ENROLL.pop('forget')); S['known']=E.db.names()
        en=None
        if ENROLL.get('name'):
            tid=ENROLL.get('track')
            if tid=='target': tid=S.get('target_id')
            en={'track':tid,'name':ENROLL['name']}
        tracks,added=E.step(fr,en)
        if en and added:
            ENROLL['n']=ENROLL.get('n',0)+added
            if ENROLL['n']>=8:
                chat('note',f"(learned {ENROLL['name']}'s face)"); note(f"learned {ENROLL['name']}"); S['known']=E.db.names(); ENROLL.clear()
        if ENROLL.get('until') and time.time()>ENROLL['until']: ENROLL.clear()
        now=time.time(); people=[]
        for t in tracks:
            x,y,w,h=[float(v) for v in t.box]; px,py,pz,a=locate((x+w/2)/W,(y+h/2)/H,w/W,W,H)
            k=sm.get(t.id); px,py,pz=(px,py,pz) if k is None else (k[0]+0.4*(px-k[0]),k[1]+0.4*(py-k[1]),k[2]+0.4*(pz-k[2])); sm[t.id]=(px,py,pz)
            off,nod=gaze(t.row,W,a,px,py); looking=off is not None and abs(off)<22 and nod is not None and -0.35<nod<0.35
            if t.name: PRESENT.setdefault(t.name,now)
            people.append({'id':t.id,'name':t.name,'x':px,'y':py,'z':pz,'d':math.hypot(px,py),'box':[x/W,y/H,w/W,h/H],
                           'looking':looking,'gaze':None if off is None else round(off,1),'new':now-t.born<1})
        sm={k:v for k,v in sm.items() if any(p['id']==k for p in people)}
        # who to follow
        tgt=None
        if FOLLOW['mode']=='closest' and people: tgt=min(people,key=lambda p:p['d'])
        elif FOLLOW['mode']=='person':
            w=FOLLOW['who']; tgt=next((p for p in people if (p['name']==w if isinstance(w,str) else p['id']==w)),None)
        for p in people: p['target']=p is tgt
        S['target_id']=tgt['id'] if tgt else None
        # tell the robot (never blocks this loop)
        if tgt:
            miss=0; el=math.degrees(math.atan2(tgt['z'],max(10,tgt['d'])))             # how far up BITCH must look
            n=max(-1.0,min(0.3,-0.8+el/50))
            if tgt['looking'] and not eye_on and now-eye_since>4: eye_on=True; eye_since=now
            elif not tgt['looking'] and eye_on: eye_on=False; eye_since=now
            LOOKOUT['q']={'b':f"{to_robot(tgt['x'],tgt['y']):.2f}",'n':f'{n:.2f}','seen':1,'greet':0,'eye':int(tgt['looking'])}
        else:
            miss+=1
            if miss==8: LOOKOUT['q']={'seen':0}; eye_on=False
        # welcome back, by name
        for p in people:
            if p['name'] and SETTINGS['greet'] and not S['asleep']:
                gone=now-PRESENT.get(p['name'],0)
                if gone>600: HEARD.put(('arrive',p['name']))
                PRESENT[p['name']]=now
        with LOCK: S['people']=[{k:v for k,v in p.items()} for p in people]
        ok,jp=cv2.imencode('.jpg',fr,[cv2.IMWRITE_JPEG_QUALITY,72])
        if ok:
            with NEWFRAME:
                FRAME['seq']+=1; FRAME['jpg']=jp.tobytes(); FRAME['faces']=json.dumps(S['people']); NEWFRAME.notify_all()

# ---------------- app server ----------------
def server():
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    class H(BaseHTTPRequestHandler):
        def _send(s,body,ct='application/json',extra=()):
            s.send_response(200); s.send_header('Content-Type',ct); s.send_header('Cache-Control','no-store')
            for k,v in extra: s.send_header(k,v)
            s.end_headers(); s.wfile.write(body)
        def ok(s): s._send(b'{"ok":true}')
        def do_GET(s):
            u=urllib.parse.urlparse(s.path); q={k:v[0] for k,v in urllib.parse.parse_qs(u.query).items()}
            if u.path=='/state':
                with LOCK: st={k:v for k,v in S.items()}; st['log']=list(S['log']); st['people']=list(S['people'])
                st.update(cam=CAM,follow=FOLLOW,settings=SETTINGS,enrolling=ENROLL.get('name'),mute_until=0); s._send(json.dumps(st).encode())
            elif u.path=='/frame':                                        # newest picture + the faces found in it
                after=int(q.get('after',0))
                with NEWFRAME:
                    if FRAME['seq']<=after: NEWFRAME.wait(1.0)
                    seq,jpg,faces=FRAME['seq'],FRAME['jpg'],FRAME['faces']
                if not jpg: s.send_response(204); s.end_headers(); return
                s._send(jpg,'image/jpeg',(('X-Seq',str(seq)),('X-Faces',faces),('Access-Control-Expose-Headers','X-Seq, X-Faces')))
            elif u.path.startswith('/face/'):
                from faces import FaceDB
                p=FaceDB().photo(urllib.parse.unquote(u.path[6:]))
                if os.path.exists(p): s._send(open(p,'rb').read(),'image/jpeg')
                else: s.send_response(404); s.end_headers()
            elif u.path=='/set':
                for k in ('x','y','z','yaw','fov'):
                    if k in q: CAM[k]=float(q[k])
                if 'flip' in q: CAM['flip']=q['flip']=='1'
                if 'voice' in q: S['voice']=q['voice']
                if 'robotness' in q: S['robotness']=float(q['robotness'])
                if 'mic' in q: S['mic_on']=q['mic']=='1'
                if 'greet' in q: SETTINGS['greet']=q['greet']=='1'
                if q.get('gaze')=='calibrate':
                    t=next((p for p in S['people'] if p.get('target')),None)
                    if t and t.get('gaze') is not None: CAM['gaze_bias']+=t['gaze']
                save_cam(); s.ok()
            elif u.path=='/follow':
                m=q.get('mode','closest'); w=q.get('who')
                FOLLOW['mode']=m; FOLLOW['who']=int(w) if w and w.isdigit() else w
                if m=='off': LOOKOUT['q']={'seen':0}
                s.ok()
            elif u.path=='/learn':                                        # remember a face under a name
                name=q.get('name','').strip()[:24]
                if name: ENROLL.clear(); ENROLL.update(name=name,track=int(q['id']) if q.get('id','').isdigit() else 'target',until=time.time()+8)
                s.ok()
            elif u.path=='/forget': ENROLL['forget']=q.get('name',''); s.ok()
            elif u.path=='/aim':
                FOLLOW['mode']='off'; LOOKOUT['q']={'b':f"{to_robot(float(q['x']),float(q['y'])):.2f}",'n':'-0.8','seen':1,'greet':0}; s.ok()
            elif u.path=='/stop': interrupt(); s.ok()
            elif u.path=='/type':
                if q.get('text','').strip(): HEARD.put(q['text'].strip())
                s.ok()
            elif u.path=='/emote': pi_get('/emote',emotion=q.get('emotion','neutral'),action=q.get('action','none')); s.ok()
            elif u.path=='/icon.png': s._send(open(os.path.join(HERE,'icon.png'),'rb').read(),'image/png')
            else: s._send(open(os.path.join(HERE,'dashboard.html'),'rb').read(),'text/html; charset=utf-8')
        def log_message(s,*a): pass
    ThreadingHTTPServer(('127.0.0.1',8770),H).serve_forever()

INTRO=re.compile(r"\b(?:my name is|my name's|call me|i am called)\s+([A-Za-z][A-Za-z\-']{1,20})",re.I)
def conversation():
    while True:
        item=HEARD.get()
        if isinstance(item,tuple):                                      # someone it knows just walked up
            name=item[1]
            if S['status'] not in ('listening',) or S['asleep']: continue
            chat('note',f'({name} walked up)'); status('thinking')
            say,emo,act=think(f'({name} just walked up to you after being away. Greet {name} by name, warmly and briefly.)',force=True)
        else:
            text=item; chat('you',text); log(f'  you: {text}')
            m=INTRO.search(text)
            tp=next((p for p in S['people'] if p.get('target')),None)
            if m and tp and (not tp['name'] or tp['name'].lower()==m.group(1).lower()):
                ENROLL.clear(); ENROLL.update(name=m.group(1).capitalize(),track='target',until=time.time()+8)
            if S['asleep']:
                if named(text) or re.search(r'\b(wake up|good morning)\b',text.lower()):
                    S['asleep']=False; pi_get('/emote',emotion='happy',action='wake'); time.sleep(4)
                else: chat('note','(asleep: say "Hey BITCH" or "wake up")'); continue
            status('thinking'); say,emo,act=think(text,force=bool(m))
        if say is None:
            chat('note','(not meant for me, ignored)'); log('  (ignored: not meant for the robot)'); status('listening'); continue
        if act in ('follow_me','stop_following'):
            if act=='follow_me':
                t=next((p for p in S['people'] if p.get('target')),None)
                if t: FOLLOW.update(mode='person',who=t['name'] or t['id'])
            else: FOLLOW.update(mode='off',who=None); LOOKOUT['q']={'seen':0}
            act='nod'
        chat('robot',say,emotion=emo,action=act); log(f'  robot: {say}   [{emo}, {act}]')
        speak(say,emo,act)
        if S['interrupted']: S['interrupted']=False; continue
        if act=='sleep': S['asleep']=True; status('asleep')
        elif S['status']=='speaking': status('listening')

def main():
    log(f'\n  BITCH COMPANION\n  app: http://localhost:8770\n')
    threading.Thread(target=server,daemon=True).start()
    if pi_get('/ping') is None: note(f'robot not found at {args.pi}: start listen mode on the Pi (same Wi-Fi)')
    if not args.no_mic: threading.Thread(target=lambda: _safe(ears,'mic'),daemon=True).start()
    else: status('listening')
    if not args.no_camera: threading.Thread(target=lambda: _safe(eyes,'camera'),daemon=True).start()
    threading.Thread(target=typing,daemon=True).start()
    def ping():
        while True: pi_get('/ping'); time.sleep(3)
    threading.Thread(target=ping,daemon=True).start()
    if args.app:                                                        # its own window (the BITCH app)
        threading.Thread(target=conversation,daemon=True).start()
        try:
            import webview
            webview.create_window('BITCH','http://127.0.0.1:8770',width=1320,height=880,min_size=(380,600),background_color='#DFE4E8')
            webview.start(); os._exit(0)
        except ImportError: note('app window needs pywebview; opening in the browser instead')
        import webbrowser; webbrowser.open('http://localhost:8770')
        while True: time.sleep(3600)
    if not args.no_browser:
        import webbrowser; threading.Timer(1.0,lambda: webbrowser.open('http://localhost:8770')).start()
    conversation()
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
