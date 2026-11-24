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

MAP_HTML=r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Camera placement</title>
<style>
/* Desk Creature camera placement: a top-down desk diagram.
   Ink navy on cool desk grey, kraft-brown robot, signal-blue camera, sun-yellow you.
   Helvetica, set big for the live readout; everything else quiet. */
:root{--desk:#dfe4e8;--desk2:#d3d9de;--ink:#1f2a44;--soft:#5d6b84;--kraft:#b98b54;--kraft2:#8a6235;--cam:#2d6cdf;--you:#f4c430;--line:#c3cbd3}
*{box-sizing:border-box;min-width:0}
body{margin:0;background:var(--desk);color:var(--ink);font:16px/1.45 "Helvetica Neue",Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{display:grid;grid-template-columns:minmax(0,1fr) 340px;min-height:100vh}
.stage{position:relative;padding:20px}
canvas{display:block;width:100%;height:calc(100vh - 40px);min-height:420px;border-radius:22px;background:var(--desk2);touch-action:none;cursor:crosshair}
aside{padding:36px 32px;display:flex;flex-direction:column;gap:26px;border-left:1px solid var(--line)}
h1{font-weight:700;font-size:44px;line-height:.98;letter-spacing:-.03em;margin:0}
.lede{margin:0;color:var(--soft);max-width:30ch}
.read{font-weight:700;font-size:54px;line-height:.95;letter-spacing:-.04em}
.read small{display:block;font-size:15px;font-weight:400;letter-spacing:0;color:var(--soft);margin-top:8px}
.ctl{display:grid;gap:8px}.ctl label{display:flex;justify-content:space-between;font-weight:600}.ctl label span{color:var(--soft);font-weight:400}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:36px;background:transparent}
input[type=range]::-webkit-slider-runnable-track{height:4px;border-radius:2px;background:var(--ink)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:22px;height:22px;border-radius:50%;background:var(--cam);border:3px solid var(--desk);margin-top:-9px;box-shadow:0 0 0 1px var(--ink)}
input[type=range]::-moz-range-track{height:4px;background:var(--ink)}input[type=range]::-moz-range-thumb{width:18px;height:18px;border-radius:50%;background:var(--cam);border:3px solid var(--desk)}
.sw{display:flex;align-items:center;gap:12px;cursor:pointer;font-weight:600;min-height:44px}
.sw input{appearance:none;width:46px;height:26px;border-radius:13px;background:var(--line);position:relative;transition:background .2s;margin:0;flex:none}
.sw input::after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:left .2s}
.sw input:checked{background:var(--cam)}.sw input:checked::after{left:23px}
:focus-visible{outline:3px solid var(--cam);outline-offset:3px}
.hint{color:var(--soft);font-size:14px;margin:0;margin-top:auto}
@media(max-width:820px){.wrap{grid-template-columns:1fr}canvas{height:auto;aspect-ratio:1}aside{border-left:0;padding:8px 20px 32px}h1{font-size:36px}.read{font-size:40px}}
@media(prefers-reduced-motion:reduce){.sw input,.sw input::after{transition:none}}
</style></head><body><div class="wrap">
<div class="stage"><canvas id="c" aria-label="Top-down map of the robot and camera"></canvas></div>
<aside>
<div><h1>Camera placement</h1><p class="lede" style="margin-top:12px">Drag the camera to where it sits on your desk. Drag its blue handle to aim it.</p></div>
<div class="read" id="read">Looking…<small>Step in front of the camera</small></div>
<div class="ctl"><label for="yaw">Camera aim <span id="yv"></span></label><input type="range" id="yaw" min="-180" max="180" step="1"></div>
<div class="ctl"><label for="fov">Camera view width <span id="fv"></span></label><input type="range" id="fov" min="40" max="110" step="1"></div>
<label class="sw"><input type="checkbox" id="flip"> Robot turns the wrong way</label>
<p class="hint">Click anywhere on the desk to make the robot look there. Changes save as you go.</p>
</aside></div>
<script>{
const c=document.getElementById('c'),x=c.getContext('2d'),$=id=>document.getElementById(id);
const C={desk:'#d3d9de',ink:'#1f2a44',soft:'#5d6b84',kraft:'#b98b54',kraft2:'#8a6235',cam:'#2d6cdf',you:'#f4c430',line:'#c3cbd3'};
let cfg=null,person=null,aim=null,drag=null,W=0,H=0,S=2,OX=0,OY=0,t0=performance.now();
const reduce=matchMedia('(prefers-reduced-motion: reduce)').matches;
function fit(){const r=c.getBoundingClientRect(),d=devicePixelRatio||1;W=r.width;H=r.height;c.width=W*d;c.height=H*d;x.setTransform(d,0,0,d,0,0);
S=Math.min(W/340,H/290);OX=W/2;OY=H-54*S}
const w2s=(px,py)=>[OX+px*S,OY-py*S],s2w=(sx,sy)=>[(sx-OX)/S,(OY-sy)/S];
function rr(px,py,w,h,r){x.beginPath();x.roundRect(px-w/2,py-h/2,w,h,r)}
function draw(now){if(!cfg){requestAnimationFrame(draw);return}x.clearRect(0,0,W,H);
// dot grid, 10 cm
x.fillStyle=C.line;{const[x0,y1]=s2w(0,0),[x1,y0]=s2w(W,H);for(let gx=Math.ceil(x0/10)*10;gx<=x1;gx+=10)for(let gy=Math.ceil(y0/10)*10;gy<=y1;gy+=10){const[a,b]=w2s(gx,gy);if(a>8&&a<W-8&&b>8&&b<H-8){x.beginPath();x.arc(a,b,1.1,0,7);x.fill()}}}
// distance rings
const[rx,ry]=w2s(0,0);x.strokeStyle=C.soft;x.lineWidth=1;x.setLineDash([3,6]);x.font='13px "Helvetica Neue",Helvetica,Arial';x.fillStyle=C.soft;
for(const [r,l] of [[50,'50 cm'],[100,'1 m'],[150,'1.5 m'],[200,'2 m']]){x.beginPath();x.arc(rx,ry,r*S,Math.PI*1.08,Math.PI*1.92);x.stroke();x.fillText(l,rx+4,ry-r*S-6)}x.setLineDash([]);
// camera cone
const[cx,cy]=w2s(cfg.x,cfg.y),th=cfg.yaw*Math.PI/180,f=cfg.fov*Math.PI/360,L=Math.max(W,H)*1.4;
const g=x.createRadialGradient(cx,cy,0,cx,cy,L*0.55);g.addColorStop(0,'rgba(45,108,223,.28)');g.addColorStop(1,'rgba(45,108,223,0)');
x.fillStyle=g;x.beginPath();x.moveTo(cx,cy);x.lineTo(cx+L*Math.sin(th-f),cy-L*Math.cos(th-f));x.lineTo(cx+L*Math.sin(th+f),cy-L*Math.cos(th+f));x.closePath();x.fill();
// gaze beam
const tgt=aim||person;
if(tgt){const[px,py]=w2s(tgt[0],tgt[1]);const gb=x.createLinearGradient(rx,ry,px,py);gb.addColorStop(0,'rgba(185,139,84,.9)');gb.addColorStop(1,'rgba(244,196,48,.9)');
x.strokeStyle=gb;x.lineWidth=4;x.lineCap='round';x.beginPath();x.moveTo(rx,ry);x.lineTo(px,py);x.stroke()}
// robot, top-down: shoebox, turntable, arm, face with googly eyes
const ang=tgt?Math.atan2(tgt[0],Math.max(1,tgt[1])):0;
x.fillStyle='#fff';x.strokeStyle=C.ink;x.lineWidth=2;rr(rx,ry+6*S,34*S,22*S,4*S);x.fill();x.stroke();
x.save();x.translate(rx,ry);x.rotate(ang);x.fillStyle=C.kraft;x.beginPath();x.arc(0,0,7.5*S,0,7);x.fill();
x.fillStyle=C.kraft2;x.fillRect(-1.2*S,-15*S,2.4*S,15*S);x.fillStyle=C.kraft;rr(0,-17*S,13*S,3*S,1*S);x.fill();
for(const e of[-3,3]){x.fillStyle='#fff';x.beginPath();x.arc(e*S,-19.6*S,2*S,0,7);x.fill();x.strokeStyle=C.ink;x.lineWidth=1;x.stroke();x.fillStyle=C.ink;x.beginPath();x.arc(e*S,-20.4*S,0.9*S,0,7);x.fill()}
x.restore();
// camera body + aim handle
x.save();x.translate(cx,cy);x.rotate(th);x.fillStyle=C.cam;rr(0,0,12*S,8*S,2*S);x.fill();x.fillStyle='#fff';x.beginPath();x.arc(0,-4*S,2.2*S,0,7);x.fill();
x.fillStyle=C.cam;x.beginPath();x.arc(0,-4*S,1.1*S,0,7);x.fill();
x.strokeStyle=C.cam;x.lineWidth=2;x.beginPath();x.moveTo(0,-6.5*S);x.lineTo(0,-15*S);x.stroke();x.fillStyle='#fff';x.beginPath();x.arc(0,-16.5*S,3*S,0,7);x.fill();x.stroke();x.restore();
// you
if(person){const[px,py]=w2s(person[0],person[1]);const p=reduce?0:(((now-t0)/1400)%1);
x.strokeStyle='rgba(244,196,48,'+(0.7*(1-p))+')';x.lineWidth=2;x.beginPath();x.arc(px,py,(9+14*p)*S/2,0,7);x.stroke();
x.fillStyle=C.you;x.beginPath();x.arc(px,py,9*S/2,0,7);x.fill();x.strokeStyle=C.ink;x.lineWidth=2;x.stroke();
x.fillStyle=C.ink;x.font='600 14px "Helvetica Neue",Helvetica,Arial';x.textAlign='center';x.fillText('You',px,py+9*S/2+18);x.textAlign='start'}
if(aim){const[ax,ay]=w2s(aim[0],aim[1]);x.strokeStyle=C.ink;x.lineWidth=2;x.beginPath();x.arc(ax,ay,8,0,7);x.moveTo(ax-14,ay);x.lineTo(ax+14,ay);x.moveTo(ax,ay-14);x.lineTo(ax,ay+14);x.stroke()}
requestAnimationFrame(draw)}
function handle(){const th=cfg.yaw*Math.PI/180,[cx,cy]=w2s(cfg.x,cfg.y);return[cx+16.5*S*Math.sin(th),cy-16.5*S*Math.cos(th)]}
const pos=e=>{const r=c.getBoundingClientRect();return[e.clientX-r.left,e.clientY-r.top]};
c.onpointerdown=e=>{const[sx,sy]=pos(e),[hx,hy]=handle(),[cx,cy]=w2s(cfg.x,cfg.y);
if(Math.hypot(sx-hx,sy-hy)<14){drag='aim'}else if(Math.hypot(sx-cx,sy-cy)<9*S){drag='move'}
else{const[wx,wy]=s2w(sx,sy);aim=[wx,wy];fetch('/aim?x='+wx+'&y='+wy);clearTimeout(aim.t);setTimeout(()=>{aim=null},2500);return}
c.setPointerCapture(e.pointerId)};
c.onpointermove=e=>{const[sx,sy]=pos(e);
if(!drag){const[hx,hy]=cfg?handle():[0,0],[cx,cy]=cfg?w2s(cfg.x,cfg.y):[0,0];c.style.cursor=Math.hypot(sx-hx,sy-hy)<14?'grab':Math.hypot(sx-cx,sy-cy)<9*S?'move':'crosshair';return}
if(drag==='move'){const[wx,wy]=s2w(sx,sy);cfg.x=Math.round(wx);cfg.y=Math.round(wy)}
else{const[cx,cy]=w2s(cfg.x,cfg.y);cfg.yaw=Math.round(Math.atan2(sx-cx,cy-sy)*180/Math.PI);sync()}};
c.onpointerup=()=>{if(drag){drag=null;save()}};
const save=()=>fetch('/set?'+new URLSearchParams({x:cfg.x,y:cfg.y,yaw:cfg.yaw,fov:cfg.fov,flip:cfg.flip?1:0}));
function sync(){$('yaw').value=cfg.yaw;$('fov').value=cfg.fov;$('flip').checked=cfg.flip;$('yv').textContent=cfg.yaw+'°';$('fv').textContent=cfg.fov+'°'}
$('yaw').oninput=()=>{cfg.yaw=+$('yaw').value;sync();save()};$('fov').oninput=()=>{cfg.fov=+$('fov').value;sync();save()};$('flip').onchange=()=>{cfg.flip=$('flip').checked;save()};
async function pull(){try{const r=await(await fetch('/state')).json();if(!drag){const first=!cfg;cfg=r.cam;if(first)sync()}person=r.person;
if(person){const lr=Math.round(person[0]),fw=Math.round(person[1]);$('read').innerHTML=`${Math.abs(lr)} cm ${lr>=0?'right':'left'}<br>${fw} cm ahead<small>Where it thinks you are, from the robot</small>`}
else $('read').innerHTML='Looking…<small>Step in front of the camera</small>'}catch(e){}}
addEventListener('resize',fit);fit();setInterval(pull,200);pull();requestAnimationFrame(draw);
}</script></body></html>
"""
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
