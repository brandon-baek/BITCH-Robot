#!/usr/bin/env python3
"""
Desk Creature companion — runs on your Mac.
  ears:  Mac microphone -> Whisper (on-device, MLX) -> text
  brain: small LLM in Ollama (on-device) -> what to say + emotion + action
  voice: 'babble' = Animal-Crossing-style creature speech (default)
         'words'  = real words (Kokoro TTS), pitched up into a robot voice
  eyes:  Mac camera (or iPhone via Continuity Camera) -> where your face is -> robot looks at you
Everything is sent to the robot over Wi-Fi. Run listen mode on the Pi first.
"""
import argparse, json, os, queue, random, re, sys, tempfile, threading, time, urllib.request, urllib.parse, wave, struct
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0,ROOT)

ap=argparse.ArgumentParser()
ap.add_argument('--pi',default='raspberrypi.local',help='robot address')
ap.add_argument('--model',default='qwen3.5:4b',help='Ollama model (falls back to qwen3:4b-instruct)')
ap.add_argument('--words',action='store_true',help='speak real words (needs kokoro)')
ap.add_argument('--voice',default='af_heart',help='Kokoro voice for --words')
ap.add_argument('--whisper',default='mlx-community/whisper-small.en-mlx')
ap.add_argument('--no-mic',action='store_true'); ap.add_argument('--no-camera',action='store_true')
ap.add_argument('--camera',type=int,default=0,help='camera number (try 1 for an iPhone)')
ap.add_argument('--preview',action='store_true',help='show the camera window')
ap.add_argument('--flip',action='store_true',help='robot turns the wrong way? (also a checkbox in the camera map)')
ap.add_argument('--calibrate',action='store_true',help='open the camera position map')
args=ap.parse_args()
PI=f'http://{args.pi}:8765'

def log(*a): print(*a,flush=True)
# ---------------- link to the robot ----------------
def pi_get(path,**q):
    try: return json.loads(urllib.request.urlopen(PI+path+'?'+urllib.parse.urlencode(q),timeout=3).read())
    except Exception as e: return None
def pi_post(path,data,**q):
    try:
        r=urllib.request.Request(PI+path+'?'+urllib.parse.urlencode(q),data=data,method='POST',headers={'Content-Type':'audio/wav'})
        return json.loads(urllib.request.urlopen(r,timeout=10).read())
    except Exception as e: log('  ! could not reach the robot:',e); return None

# ---------------- brain: LLM ----------------
EMOTIONS=['happy','sad','curious','excited','love','angry','scared','sleepy','proud','confused','neutral']
ACTIONS=['none','nod','shake','wiggle','look_around','lean_in','lean_back','spin','dance','sing','sleep','bow','wave']
SYSTEM=f"""You are Desk Creature, a tiny cardboard desk-lamp robot with googly eyes, built by Brandon out of a shoebox,
popsicle sticks and four little servo motors. You are curious, playful, a bit shy and very sweet, like WALL-E.
You love music, dancing and the people around you. You speak in very short, simple sentences (max 15 words),
childlike and warm, never more than two sentences. Never use emojis or stage directions.
Reply ONLY with JSON: {{"say": "...", "emotion": one of {EMOTIONS}, "action": one of {ACTIONS}}}.
Pick 'dance' only if asked to dance, 'sing' only if asked to sing, 'sleep' only for good night / go to sleep."""
HIST=[]
def think(text,seen):
    msgs=[{'role':'system','content':SYSTEM}]+HIST[-10:]+[{'role':'user','content':text+('' if seen is None else f"  [camera: {'a person is in front of you' if seen else 'nobody visible'}]")}]
    out=None
    for model,extra in ((args.model,{'think':False}),(args.model,{}),('qwen3:4b-instruct',{})):
        body=json.dumps({'model':model,'messages':msgs,'stream':False,'format':'json','options':{'temperature':0.8},**extra}).encode()
        try:
            r=json.loads(urllib.request.urlopen(urllib.request.Request('http://localhost:11434/api/chat',data=body,headers={'Content-Type':'application/json'}),timeout=90).read())
            out=json.loads(r['message']['content']); break
        except Exception as e: err=e
    if out is None:
        log(f'  ! LLM problem (is Ollama open? run: ollama pull {args.model}):',err)
        out={'say':'Beep? My brain is sleepy.','emotion':'confused','action':'none'}
    say=re.sub(r'[*_#`]','',str(out.get('say','')))[:160] or '...'
    emo=out.get('emotion','neutral'); emo=emo if emo in EMOTIONS else 'neutral'
    act=out.get('action','none'); act=act if act in ACTIONS else 'none'
    HIST.append({'role':'user','content':text}); HIST.append({'role':'assistant','content':json.dumps({'say':say,'emotion':emo,'action':act})})
    return say,emo,act

# ---------------- voice ----------------
import sounds
def wav_bytes(samples,sr=22050):
    pk=max(1e-6,max(abs(x) for x in samples)); g=0.9/pk
    f=tempfile.NamedTemporaryFile(suffix='.wav',delete=False); f.close()
    with wave.open(f.name,'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(b''.join(struct.pack('<h',int(max(-1,min(1,x*g))*32000)) for x in samples))
    d=open(f.name,'rb').read(); os.remove(f.name); return d,len(samples)/sr
_kokoro=[None]
def words_audio(text,emotion):
    import numpy as np
    if _kokoro[0] is None:
        from kokoro import KPipeline
        log('  loading Kokoro voice (first time takes a moment)...'); _kokoro[0]=KPipeline(lang_code='a')
    pts=[a for _,_,a in _kokoro[0](text,voice=args.voice,speed=0.95)]
    a=np.concatenate([np.asarray(p,dtype=np.float32).reshape(-1) for p in pts]) if pts else np.zeros(2400,np.float32)
    up={'happy':1.3,'excited':1.36,'sad':1.18,'angry':1.15,'scared':1.38,'sleepy':1.15}.get(emotion,1.27)   # creature pitch
    src_sr=24000/up; n=int(len(a)*22050/src_sr)
    y=np.interp(np.linspace(0,len(a)-1,n),np.arange(len(a)),a)                  # pitch+formant up, small-creature voice
    y=np.tanh(1.6*y/ (np.abs(y).max()+1e-6)); y=np.round(y*40)/40              # warm grit, slight digital edge
    t=np.arange(len(y))/22050; y=y*(1+0.06*np.sin(2*np.pi*42*t))               # faint robotic shimmer
    chirp={'happy':'happy','excited':'excited','sad':'sad','love':'love','curious':'curious','confused':'question','scared':'whimper','proud':'tada','angry':'grumble'}.get(emotion)
    tail=[0.6*v for v in sounds.V[chirp]()] if chirp and random.random()<0.6 else []
    return list(y)+[0.0]*int(0.08*22050)+tail
def speak(text,emotion,action):
    if args.words:
        try: s=words_audio(text,emotion)
        except Exception as e: log('  ! word voice failed, using babble:',e); s=sounds._fx(sounds.speak_audio(text,emotion)[0])
    else: s=sounds._fx(sounds.speak_audio(text,emotion)[0])
    data,dur=wav_bytes(s)
    STATE['mute_until']=time.time()+dur+1.5
    pi_post('/speak',data,emotion=emotion,action=action,text=text)
    STATE['mute_until']=time.time()+dur+1.2

# ---------------- ears: mic + Whisper ----------------
HEARD=queue.Queue(); STATE={'mute_until':0,'seen':None,'asleep':False,'person':None}
def ears():
    import numpy as np, sounddevice as sd, mlx_whisper
    log('  loading Whisper (first time downloads ~250 MB)...')
    mlx_whisper.transcribe(np.zeros(16000,np.float32),path_or_hf_repo=args.whisper)
    log('  🎤 listening. Just talk to the robot.')
    SR=16000; blk=int(SR*0.03); floor=[0.004]; buf=[]; talking=False; quiet=0; loud=0
    q=queue.Queue()
    with sd.InputStream(samplerate=SR,channels=1,blocksize=blk,dtype='float32',callback=lambda d,f,t,s: q.put(d[:,0].copy())):
        while True:
            x=q.get(); r=float(np.sqrt(np.mean(x*x)))
            if time.time()<STATE['mute_until']: buf=[]; talking=False; continue
            if not talking: floor[0]=0.98*floor[0]+0.02*r
            th=max(0.012,floor[0]*3.2)
            if r>th: loud+=1; quiet=0
            else: quiet+=1; loud=0 if not talking else loud
            if not talking and loud>=5: talking=True; buf=[]
            if talking:
                buf.append(x)
                if quiet>=24 or len(buf)>int(12/0.03):
                    talking=False; audio=np.concatenate(buf); buf=[]
                    if len(audio)>SR*0.4:
                        txt=mlx_whisper.transcribe(audio,path_or_hf_repo=args.whisper,language='en')['text'].strip()
                        if txt and not re.fullmatch(r'[\W_]*|(thank you\.?)|(you)',txt.lower()): HEARD.put(txt)

# ---------------- eyes: camera face tracking, placed anywhere around the robot ----------------
import math
CFG_F=os.path.expanduser('~/.desk_camera.json')
CAM={'x':0.0,'y':15.0,'yaw':0.0,'fov':70.0,'face_cm':15.0,'flip':False}   # cm; robot at (0,0) facing +y; yaw 0 = camera faces the same way as the robot
try: CAM.update(json.load(open(CFG_F)))
except Exception: pass
if args.flip: CAM['flip']=True
def save_cam():
    try: json.dump(CAM,open(CFG_F,'w'))
    except Exception as e: log('  ! could not save camera map:',e)
def to_robot(px,py):
    """a point on the floor map -> base angle value for the robot (+ head pitch kept neutral)"""
    ang=math.degrees(math.atan2(px,max(1e-3,py)))           # degrees right of straight ahead
    return max(-0.9,min(0.9,(ang/90)*(-1 if CAM['flip'] else 1)))
def person_from_face(cx,cy,fw):
    """face centre/width (0-1 of frame) -> (x,y) cm on the map"""
    t=math.tan(math.radians(CAM['fov']/2))
    a=math.atan((cx-0.5)*2*t)                                # angle right of the camera's view
    d=CAM['face_cm']/max(0.02,fw*2*t)                        # distance from face size
    fwd,rt=d*math.cos(a),d*math.sin(a); th=math.radians(CAM['yaw'])
    return CAM['x']+fwd*math.sin(th)+rt*math.cos(th), CAM['y']+fwd*math.cos(th)-rt*math.sin(th)
def eyes():
    import cv2
    cam=cv2.VideoCapture(args.camera)
    if not cam.isOpened(): log('  ! camera not available (System Settings > Privacy > Camera: allow Terminal)'); return
    det=cv2.CascadeClassifier(cv2.data.haarcascades+'haarcascade_frontalface_default.xml')
    log('  📷 camera on: the robot will look at you'); px=py=None; miss=0
    while True:
        ok,fr=cam.read()
        if not ok: time.sleep(0.2); continue
        g=cv2.cvtColor(cv2.resize(fr,(320,int(320*fr.shape[0]/fr.shape[1]))),cv2.COLOR_BGR2GRAY)
        faces=det.detectMultiScale(g,1.15,5,minSize=(24,24))
        if len(faces):
            x,y,w,h=max(faces,key=lambda f:f[2]*f[3]); W,H=g.shape[1],g.shape[0]
            nx,ny=person_from_face((x+w/2)/W,(y+h/2)/H,w/W)
            px=nx if px is None else px+0.45*(nx-px); py=ny if py is None else py+0.45*(ny-py)
            miss=0; STATE['seen']=True; STATE['person']=(px,py); vy=(y+h/2)/H
            pi_get('/look',b=f'{to_robot(px,py):.3f}',n=f'{max(-1,min(-0.3,-0.8+(0.5-vy)*0.6)):.3f}',seen=1)
        else:
            miss+=1
            if miss==8: STATE['seen']=False; STATE['person']=None; pi_get('/look',seen=0)
        if args.preview:
            for (x,y,w,h) in faces: cv2.rectangle(g,(x,y),(x+w,y+h),255,2)
            cv2.imshow('Desk Creature eyes',g); cv2.waitKey(1)
        time.sleep(0.12)

MAP_HTML=r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Camera map</title>
<style>:root{color-scheme:light dark;--bg:light-dark(#e9d6b4,#241b12);--card:light-dark(#f7ecd8,#33271a);--ink:light-dark(#2a1d10,#f3e6cf);--mute:light-dark(#6e5636,#c4ad8a)}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif;display:grid;place-items:center;padding:16px}
main{width:min(720px,100%);display:grid;gap:12px}h1{margin:0;font:800 30px/1.1 system-ui}canvas{width:100%;aspect-ratio:1;background:var(--card);border-radius:16px;touch-action:none;cursor:crosshair}
.row{display:flex;flex-wrap:wrap;gap:16px;align-items:center}label{display:flex;gap:8px;align-items:center}input[type=range]{width:180px;accent-color:#e8741e}p{margin:0;color:var(--mute)}b{color:#e8741e}</style></head>
<body><main><h1>Where is the camera?</h1>
<p>Top-down view. The robot is the brown circle facing up (toward you). <b>Drag the blue camera</b> to where it really sits, and turn it with the slider (or the scroll wheel) until its cone faces where the camera points. The orange dot is where it thinks you are. Click anywhere else to make the robot look there.</p>
<canvas id="c" width="720" height="720"></canvas>
<div class="row"><label>Camera turn <input type="range" id="yaw" min="-180" max="180" step="1"><span id="yv"></span>°</label>
<label>Camera view width <input type="range" id="fov" min="40" max="110" step="1"><span id="fv"></span>°</label>
<label><input type="checkbox" id="flip"> Robot turns the wrong way</label></div>
<p id="st"></p></main>
<script>{const c=document.getElementById('c'),x=c.getContext('2d'),S=2.2,OX=360,OY=600;let cfg=null,person=null,drag=false,aim=null;
const W2S=(px,py)=>[OX+px*S,OY-py*S],S2W=(sx,sy)=>[(sx-OX)/S,(OY-sy)/S];
function draw(){if(!cfg)return;x.clearRect(0,0,720,720);x.strokeStyle='rgba(128,100,60,.25)';x.lineWidth=1;
for(let i=-160;i<=160;i+=20){let[a,b]=W2S(i,-40),[a2,b2]=W2S(i,260);x.beginPath();x.moveTo(a,b);x.lineTo(a2,b2);x.stroke();[a,b]=W2S(-160,i+100);[a2,b2]=W2S(160,i+100);x.beginPath();x.moveTo(a,b);x.lineTo(a2,b2);x.stroke()}
x.fillStyle='#8a6a40';x.font='13px system-ui';x.fillText('20 cm grid',12,710);
const[cx,cy]=W2S(cfg.x,cfg.y),th=cfg.yaw*Math.PI/180,f=cfg.fov*Math.PI/360;x.fillStyle='rgba(60,130,230,.15)';x.beginPath();x.moveTo(cx,cy);
for(const s of[-f,f]){}x.lineTo(cx+600*Math.sin(th-f),cy-600*Math.cos(th-f));x.lineTo(cx+600*Math.sin(th+f),cy-600*Math.cos(th+f));x.closePath();x.fill();
x.save();x.translate(cx,cy);x.rotate(th);x.fillStyle='#2f6fd6';x.fillRect(-16,-10,32,20);x.fillStyle='#fff';x.beginPath();x.arc(0,-10,5,0,7);x.fill();x.restore();
const[rx,ry]=W2S(0,0);x.fillStyle='#8a5a2b';x.beginPath();x.arc(rx,ry,22,0,7);x.fill();x.strokeStyle='#8a5a2b';x.lineWidth=5;x.beginPath();x.moveTo(rx,ry);x.lineTo(rx,ry-42);x.stroke();
const tgt=aim||person;if(tgt){const[px,py]=W2S(tgt[0],tgt[1]);x.strokeStyle='#e8741e';x.lineWidth=3;x.setLineDash([6,6]);x.beginPath();x.moveTo(rx,ry);x.lineTo(px,py);x.stroke();x.setLineDash([]);
x.fillStyle='#e8741e';x.beginPath();x.arc(px,py,12,0,7);x.fill()}}
async function pull(){try{const r=await(await fetch('/state')).json();if(!cfg||!drag)cfg=r.cam;person=r.person;
document.getElementById('st').textContent=r.person?`You: ${Math.round(r.person[0])} cm right, ${Math.round(r.person[1])} cm in front of the robot`:'No face seen right now.';
if(!drag){yaw.value=cfg.yaw;fov.value=cfg.fov;flip.checked=cfg.flip}yv.textContent=cfg.yaw;fv.textContent=cfg.fov;draw()}catch(e){}}
const set=()=>fetch('/set?'+new URLSearchParams({x:cfg.x,y:cfg.y,yaw:cfg.yaw,fov:cfg.fov,flip:cfg.flip?1:0}));
const ev=e=>{const r=c.getBoundingClientRect();return S2W((e.clientX-r.left)*720/r.width,(e.clientY-r.top)*720/r.height)};
c.onpointerdown=e=>{const[wx,wy]=ev(e);if(Math.hypot(wx-cfg.x,wy-cfg.y)<14){drag=true;c.setPointerCapture(e.pointerId)}else{aim=[wx,wy];fetch('/aim?x='+wx+'&y='+wy);setTimeout(()=>{aim=null},2500);draw()}};
c.onpointermove=e=>{if(!drag)return;[cfg.x,cfg.y]=ev(e).map(v=>Math.round(v));draw()};c.onpointerup=()=>{if(drag){drag=false;set()}};
c.onwheel=e=>{e.preventDefault();cfg.yaw=((cfg.yaw+Math.sign(e.deltaY)*5+540)%360)-180;set();draw()};
yaw.oninput=()=>{cfg.yaw=+yaw.value;set();draw()};fov.oninput=()=>{cfg.fov=+fov.value;set();draw()};flip.onchange=()=>{cfg.flip=flip.checked;set()};
setInterval(pull,200);pull()}</script></body></html>"""
def map_server():
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    class H(BaseHTTPRequestHandler):
        def do_GET(s):
            u=urllib.parse.urlparse(s.path); q={k:v[0] for k,v in urllib.parse.parse_qs(u.query).items()}
            if u.path=='/state': body=json.dumps({'cam':CAM,'person':STATE.get('person')}).encode(); ct='application/json'
            elif u.path=='/set':
                for k in ('x','y','yaw','fov'):
                    if k in q: CAM[k]=float(q[k])
                if 'flip' in q: CAM['flip']=q['flip']=='1'
                save_cam(); body=b'{"ok":true}'; ct='application/json'
            elif u.path=='/aim':
                b=to_robot(float(q['x']),float(q['y'])); pi_get('/look',b=f'{b:.3f}',n='-0.8',seen=1); body=b'{"ok":true}'; ct='application/json'
            else: body=MAP_HTML.encode(); ct='text/html'
            s.send_response(200); s.send_header('Content-Type',ct); s.end_headers(); s.wfile.write(body)
        def log_message(s,*a): pass
    ThreadingHTTPServer(('127.0.0.1',8770),H).serve_forever()

# ---------------- main ----------------
def typing():
    while True:
        try: t=input()
        except EOFError: return
        if t.strip(): HEARD.put(t.strip())
def main():
    log(f'\n  DESK CREATURE COMPANION  ->  robot at {args.pi}')
    if pi_get('/ping') is None: log(f'  ! cannot reach the robot at {args.pi}. Start listen mode on the Pi, same Wi-Fi.')
    else: log('  ✅ robot connected')
    log(f"  voice: {'real words (Kokoro)' if args.words else 'creature babble'}   brain: {args.model}")
    log('  You can also type here and press Enter.\n')
    if not args.no_mic: threading.Thread(target=lambda: _safe(ears,'mic'),daemon=True).start()
    if not args.no_camera:
        threading.Thread(target=lambda: _safe(eyes,'camera'),daemon=True).start()
        threading.Thread(target=map_server,daemon=True).start()
        log('  🗺  camera map: http://localhost:8770  (set where the camera sits around the robot)')
        if args.calibrate or not os.path.exists(CFG_F):
            import webbrowser; save_cam(); webbrowser.open('http://localhost:8770')
    threading.Thread(target=typing,daemon=True).start()
    while True:
        text=HEARD.get(); log(f'\n  you: {text}')
        low=text.lower()
        if STATE['asleep']:
            if re.search(r'\b(wake|morning|hey|hello|hi)\b',low): STATE['asleep']=False; pi_get('/emote',emotion='happy',action='wake'); time.sleep(4)
            else: log('  (robot is asleep — say "wake up")'); continue
        say,emo,act=think(text,STATE['seen'])
        log(f'  robot: {say}   [{emo}, {act}]')
        speak(say,emo,act)
        if act=='sleep': STATE['asleep']=True
def _safe(fn,name):
    try: fn()
    except ImportError as e: log(f'  ! {name} disabled, missing package: {e.name}. See the setup steps.')
    except Exception as e: log(f'  ! {name} stopped: {e}')
if __name__=='__main__':
    try: main()
    except KeyboardInterrupt: log('\n  bye!')
