# BITCH — project brief for Claude Code

BITCH is Brandon's desk-lamp robot: a cardboard and popsicle-stick body on a Raspberry Pi 4, with googly eyes.
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
- An SSD1306 OLED is wired but not detected yet (its RES pin needs to go to Pi pin 7). A 4" lazy Susan bearing for
  the base is waiting to be installed.
- Brandon's Mac is an M1 Pro (user `brandon.baek`). Ollama is installed. BITCH's Python environment is `~/dc-venv`,
  which must be Python 3.12 because Kokoro needs it.

## Code map
| File | Where it runs | What it does |
|---|---|---|
| `desk.py` | Pi | Servo drivers, `go()` easing, `Actor` spring-motion engine, remembered pose, bedtime and wake-up |
| `shows.py` | Pi | Performances (disco_pro and opera are synced to the music) |
| `sounds.py`, `music.py` | Pi | Synthesized creature voice, original music tracks, cached files |
| `brain.py` | Pi | HTTP server on :8765: /look /speak /emote /say /hear /stop; lip-sync, emotion poses, eye-contact perk |
| `control.py` | Pi | Keyboard driving mode |
| `mac/companion.py` | Mac | Ears (Silero VAD + Whisper, barge-in), brain (Qwen in Ollama, JSON replies), voice (Kokoro or the Mac's `say`), app server on :8770 |
| `mac/faces.py` | Mac | YuNet face detection, tracking, SFace recognition; known faces kept in `~/.bitch/faces` |
| `mac/dashboard.html` | Browser | The BITCH app: live view, desk map, chat, people, follow mode, settings |
| `mac/setup.sh` | Mac | Sets up Python 3.12 with uv, installs everything, downloads the models, builds `~/Applications/BITCH.app` |

The Pi updates with `bash ~/BITCH-Robot/update.sh`, which pulls from GitHub and links the .py files into ~.

## Working rules
- The GitHub repo `brandon-baek/BITCH-Robot` is public, so never commit secrets.
- Commit as `Brandon <brandonbaekk@gmail.com>`.
- Test without the hardware first: mock `adafruit_servokit`, run a fake Ollama, and use a test video as the camera
  (`--camera file.mp4`). Then hand Brandon short commands to run.
- Brandon is a beginner with hardware but wants ambitious results. Give short, copy-pasteable steps and visuals.
  The design taste: big Helvetica type, the desk-grey, ink-navy, kraft palette, nothing that looks AI-generated.
