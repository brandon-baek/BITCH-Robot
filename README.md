# Desk Creature

A cardboard, lamp-style desk robot: Raspberry Pi 4, four MG90S servos on a PCA9685 board,
a 2.42" OLED face (in progress), and a synthesized creature voice.

| File | What it is |
|---|---|
| `desk.py` | Servo control: smooth moves, spring-driven `Actor`, wake-up, rest, bedtime |
| `shows.py` | Performances: `showtime()`, `life()`, `hello()`, `disco()`, `disco_pro()`, `opera()`, `peekaboo()`, `spooked()`, `sleepy()`, `catch_fly()`, `emotions()` and more |
| `tests.py` | Joint tests, hold test, nudge tool |
| `sounds.py` | The voice: vowel-shaped creature sounds plus R2-style machine chirps, randomized every time |
| `music.py` | Original background music made on the Pi: disco (70 s, sectioned), dance, spooky, lullaby, playful, fanfare, ambient |
| `control.py` | Keyboard control mode: drive every joint live, trigger sounds and emotes |
| `brain.py` | Listen mode: reacts to what you say (heard by your Mac) with moves and chirps |
| `mac/companion.py` | Runs on the Mac: ears (Whisper), brain (LLM), voice, eyes (camera) |
| `update.sh` | Pulls the latest code and links it into `~` |

## First time on the Pi
```
git clone https://github.com/brandon-baek/BITCH-Robot ~/BITCH-Robot && bash ~/BITCH-Robot/update.sh
pip install adafruit-circuitpython-servokit --break-system-packages
python3 -c "from sounds import *; build()"
```

## Every update after that
```
bash ~/BITCH-Robot/update.sh
```

## Run a show
```
python3 -c "from shows import *; showtime()"
```

Servo channels on the PCA9685: 0 base, 1 arm, 2 neck, 3 head.

## Talk to it from your Mac (on-device AI)
The Mac does the hearing (Whisper), thinking (a small LLM in Ollama) and seeing (camera);
the robot moves, looks at you and talks back through its speaker.

**On the Pi:** `python3 -c "from brain import *; listen()"`

**On the Mac, once:**
1. Install Ollama from https://ollama.com, open it, then in Terminal: `ollama pull llama3.2:3b`
2. `git clone https://github.com/brandon-baek/BITCH-Robot ~/BITCH-Robot`
3. `python3 -m venv ~/dc-venv && ~/dc-venv/bin/pip install -r ~/BITCH-Robot/mac/requirements.txt`

**On the Mac, each time:** `cd ~/BITCH-Robot && git pull -q && ~/dc-venv/bin/python mac/companion.py`

Options: `--words` (real words, needs `brew install espeak-ng` and `~/dc-venv/bin/pip install kokoro soundfile`),
`--camera 1` (iPhone via Continuity Camera), `--preview` (see what it sees), `--flip` (looks the wrong way),
`--no-camera`, `--no-mic`, `--pi <address>`, `--model <ollama model>`.

## Drive it with the keyboard
`python3 -c "from control import *; drive()"`
