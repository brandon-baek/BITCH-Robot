# BITCH — project brief for Claude Code

BITCH is Brandon's desk robot (not a lamp): a cardboard and popsicle-stick body on a Raspberry Pi 4, with googly eyes.
"BITCH" is its real name, on purpose; "Hey BITCH" is its wake phrase.

## The goal
Turn BITCH into an extremely intelligent, genuinely useful AI assistant that feels alive. It should have its own
personality, memory and habits, and it should notice things on its own instead of only answering when asked.
- **The main interface is a web app** that works from any device, including Brandon's phone, not just from the Mac.
- **Design around today's hardware, and make new hardware easy to add.** Right now the robot has no built-in
  mic, camera, screen or speaker of its own. Its senses and voice come from other devices: the Mac or phone mic and
  camera, and a Bluetooth speaker. Each one should be a pluggable device. When a real mic, camera, OLED screen
  or speaker is added to the robot, it should be a new driver, not a rewrite.
- Keep it cheap, and run things on-device where possible (Ollama, Whisper, Kokoro). The code should stay easy for
  Brandon to read and change.

## Hardware (as built)
- Raspberry Pi 4 (hostname raspberrypi.local), controlled from the Mac through Raspberry Pi Connect. Pasting long
  text into the Pi shell drops the connection, so never ask Brandon to paste big blocks; push to GitHub instead.
- A PCA9685 servo board (adafruit_servokit), powered by battery pack B through its screw terminal.
  - S1 base on channel 0: 500–2500 µs
  - S2 arm on ch1, S3 neck on ch2, S4 head on ch3: 1000–2000 µs
  - Positions run from -1 to 1. REST = [0, -0.8, -0.8, 0]. A higher neck value means looking up; a higher arm value
    means lower (bedtime uses arm 1.0 or more, past the normal range).
- Sound plays on a JBL Flip 3 over Bluetooth using `pw-play` (PipeWire).
- An SSD1306 OLED is wired but not detected yet (its RES pin needs to go to Pi pin 7). face.py is ready for it. A 4" lazy Susan bearing for
  the base is waiting to be installed.
- Brandon's Mac is an M1 Pro (user `brandon.baek`). Ollama is installed. BITCH's Python environment is `~/dc-venv`,
  which must be Python 3.12 because Kokoro needs it.

## Code map
| File | Where it runs | What it does |
|---|---|---|
| `desk.py` | Pi | Servo drivers, `go()` easing, `Actor` spring-motion engine, remembered pose, bedtime and wake-up |
| `shows.py` | Pi | Performances (disco_pro and opera are synced to the music) |
| `sounds.py`, `music.py` | Pi (sounds.py also on the Mac) | Synthesized creature voice, original music tracks, cached files |
| `brain.py` | Pi | HTTP server on :8765: /look /speak /emote /say /hear /stop /move /ping /mic; lip-sync, emotion poses, plays move steps. Runs as the user service `pi/bitch-brain.service` |
| `character.py`, `actor.py` | Pi | Gestures as readable poses (turn, lean, look, tilt): one lead action each, head first then body (`lag`), held key poses; conversation states are held postures. `Actor` is the spring integrator |
| `desktop_chatgpt.py`, `macros.py` | Pi | BITCH's own Chromium (profile `~/.bitch-pi/chromium`, DevTools on 127.0.0.1:9223, `--password-store=basic` because the GNOME keyring hangs Chromium) drives chatgpt.com: effort slider (Instant/Medium), paste, Enter, read the reply, preload the next chat. `python3 desktop_chatgpt.py status|modes|controls|ask fast "…"` |
| `face.py`, `hardware.py` | Pi | OLED googly-eye face (SSD1306 on I2C, RES on GPIO4/pin 7; pure-Python drawing, `--png` to test on a Mac); detects screen/mic/camera and reports them as caps in /ping |
| `control.py` | Pi | Keyboard driving mode |
| `core/` | Mac | BITCH's mind: `python -m core` (the BITCH app runs `python -m core --app`). App on :8780 (http) and :8781 (https) |
| `core/hub.py` | Mac | Device registry, events, routing (which device speaks a reply), app updates |
| `core/devices/` | Mac | Drivers: `robot.py` (the Pi over HTTP; voice streams to the Pi's `/speak_stream` as it is made), `mac.py` (mic, camera, speaker), `web.py` (phones/app over the /link WebSocket). New hardware = a new driver |
| `core/hearing.py`, `sight.py`, `faces.py`, `voice.py` | Mac | Silero VAD + Parakeet (Whisper only when the name was missed) for any ears device (name prompt, early end of turn, never-deaf lead-in, `EchoGate` for talk-over); YuNet/SFace faces, desk map, arrivals, eyes stay on whoever BITCH is talking with; BITCH's own cloned voice (Qwen3-TTS, `core/voices/`, Kokoro as backup) streamed as it is written, with word-timed gestures and a glance at anyone it names |
| `core/prosody.py` | Mac | How something was said (pitch, range, loudness, pace) against that mic's usual voice, as a note for the brain |
| `core/power.py` | Mac | Deep sleep ("BITCH, take a break for an hour", the app's Rest buttons, or by itself when the Mac runs low on memory): unloads the brain, voice and Whisper (about 19 GB), parks, keeps the fast ears listening. Wakes on its name, a timer or an alarm, saying a pre-recorded "give me about N seconds" (learned from real wake-ups). The watchdog reads `kern.memorystatus_level` so the Mac never runs out |
| `core/rhythm.py` | Mac | How awake BITCH is (engaged, attentive, resting, asleep) so the motors only work when someone is there; hourly budget for unprompted motion |
| `core/helper.py` | Mac | Hands weather, calendar, email and web requests to the signed-in ChatGPT on the Pi (picks Instant or Medium per request) and speaks the answer when it returns (about 15 s) |
| `core/brain.py`, `persona.md`, `llm.py` | Mac | Brain (Ornith 1.5 35B-A3B in BITCH's own llama-server, cached prompt) answers in schema JSON; a `do` field lets it look, perform, run a routine, use a tool or ask ChatGPT, offered only when the request calls for it. In a group, a line without the name is checked for who it was meant for ("Mom, ..." never goes to BITCH) |
| `core/tools.py` | Mac | Real tools: timers, lists, weather (Open-Meteo, home city set by saying it), BITCH's voice volume, Mac music. Data tools fetch first, then the model answers from the facts; a failed action is never confirmed |
| `core/moves.py` | Mac | Move steps shared by the robot and the app; invents new moves (keyframes from the 9B, clamped to Actor limits) |
| `core/life.py` | Mac | Mood, timers/reminders/routines, greetings and the one-time welcome, initiative, memory reflection, diary |
| `core/memory.py`, `config.py` | Mac | SQLite memory (facts about people and conversation summaries, full-text search, unlocked by voice or face) and settings in `~/.bitch` (never in the repo). Every saved fact must quote the person's own words; `memory.clean()` runs nightly and from the app |
| `core/server.py`, `push.py`, `tls.py` | Mac | FastAPI app + /link, phone pairing, Web Push, home certificate for https |
| `app/` | Browser | The app (phone + Mac window): talk, see, life, memory, more; `rig.js` draws BITCH with the Actor's spring physics |
| `mac/setup.sh` | Mac | Sets up Python 3.12 with uv, installs everything, downloads the models, builds `~/Applications/BITCH.app` |
| `sim/` | Mac | Measured 3D model for simulation and RL: `params.py` (every dimension) → `python -m sim.build` → MuJoCo, URDF, GLB/OBJ/STL, web viewer; Gymnasium envs `Bitch-LookAt-v0`, `Bitch-Pose-v0` in desk.py units at 50 Hz. `python -m sim.speech_motion` plays spoken lines through the real gesture code and checks each nod, shake, tilt and name glance on the body |
| `testing/` | Mac | Fake servo board, pretend phone, `python -m unittest testing.test_bitch` (runs on port 8790, safe next to the real BITCH). With the real models: `conversation_lab` (simulated room, voices, echo; scenarios quick, chat, pauses, barge, echo, family, sleep), `eval_candidate.sh MODEL` (any model, live in a terminal: personality, tools, busy room), `try_deep_sleep` |

The Pi updates with `bash ~/BITCH-Robot/update.sh`, which pulls from GitHub, links the .py files into ~, and restarts
the `bitch-brain` user service (logs: `journalctl --user -u bitch-brain -f`).

## Working rules
- BITCH runs 24/7. Memory is the Mac's hard limit: the brain is loaded once, by BITCH's own llama-server only (never a
  second copy in Ollama), and `core/power.py` steps aside before memory runs out. Lab numbers are only valid with the
  live app closed.
- Motion that nobody asked for must go through `core/rhythm.py`; parked means unpowered. The Pi's
  `Actor` only writes servo changes above its deadband, and `~/.bitch-pi/wear.json` logs motor time per day.
- Measure voice changes with real models before shipping: far-field clips for wake and word errors, a scripted
  conversation with a fake Pi that echoes BITCH back into the mic, and the `Turn: first words … s` log line.
- The GitHub repo `brandon-baek/BITCH-Robot` is public, so never commit secrets.
- Commit as `Brandon <brandonbaekk@gmail.com>`.
- Test without the hardware first: `testing/fakes/adafruit_servokit.py`, `testing/fake_ollama.py` (OLLAMA_URL), a test video as
  the camera (`--camera file.mp4`), `BITCH_HOME=<scratch dir>` so tests never touch ~/.bitch. Then hand Brandon short commands to run.
- Brandon is a beginner with hardware but wants ambitious results. Give short, copy-pasteable steps and visuals.
  The design taste: big Helvetica type, the desk-grey, ink-navy, kraft palette, nothing that looks AI-generated.
