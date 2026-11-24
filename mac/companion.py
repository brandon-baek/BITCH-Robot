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
ap.add_argument('--model',default='llama3.2:3b',help='Ollama model')
ap.add_argument('--words',action='store_true',help='speak real words (needs kokoro)')
ap.add_argument('--voice',default='af_heart',help='Kokoro voice for --words')
ap.add_argument('--whisper',default='mlx-community/whisper-small.en-mlx')
ap.add_argument('--no-mic',action='store_true'); ap.add_argument('--no-camera',action='store_true')
ap.add_argument('--camera',type=int,default=0,help='camera number (try 1 for an iPhone)')
ap.add_argument('--preview',action='store_true',help='show the camera window')
ap.add_argument('--flip',action='store_true',help='robot looks the wrong way? use this')
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
    body=json.dumps({'model':args.model,'messages':msgs,'stream':False,'format':'json','options':{'temperature':0.8}}).encode()
    try:
        r=json.loads(urllib.request.urlopen(urllib.request.Request('http://localhost:11434/api/chat',data=body,headers={'Content-Type':'application/json'}),timeout=60).read())
        out=json.loads(r['message']['content'])
    except Exception as e:
        log('  ! LLM problem (is Ollama running? did you pull the model?):',e)
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
HEARD=queue.Queue(); STATE={'mute_until':0,'seen':None,'asleep':False}
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

# ---------------- eyes: camera face tracking ----------------
def eyes():
    import cv2
    cam=cv2.VideoCapture(args.camera)
    if not cam.isOpened(): log('  ! camera not available (allow Terminal to use the camera in System Settings)'); return
    det=cv2.CascadeClassifier(cv2.data.haarcascades+'haarcascade_frontalface_default.xml')
    log('  📷 camera on: the robot will look at you'); sx=sy=0.5; miss=0
    while True:
        ok,fr=cam.read()
        if not ok: time.sleep(0.2); continue
        g=cv2.cvtColor(cv2.resize(fr,(320,int(320*fr.shape[0]/fr.shape[1]))),cv2.COLOR_BGR2GRAY)
        faces=det.detectMultiScale(g,1.15,5,minSize=(30,30))
        if len(faces):
            x,y,w,h=max(faces,key=lambda f:f[2]*f[3]); cx=(x+w/2)/g.shape[1]; cy=(y+h/2)/g.shape[0]
            if args.flip: cx=1-cx
            sx+=0.5*(cx-sx); sy+=0.5*(cy-sy); miss=0; STATE['seen']=True
            pi_get('/look',x=f'{sx:.3f}',y=f'{sy:.3f}',seen=1)
        else:
            miss+=1
            if miss==8: STATE['seen']=False; pi_get('/look',seen=0)
        if args.preview:
            for (x,y,w,h) in faces: cv2.rectangle(g,(x,y),(x+w,y+h),255,2)
            cv2.imshow('Desk Creature eyes',g); cv2.waitKey(1)
        time.sleep(0.12)

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
    if not args.no_camera: threading.Thread(target=lambda: _safe(eyes,'camera'),daemon=True).start()
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
