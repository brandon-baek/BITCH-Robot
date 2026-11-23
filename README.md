# Desk Creature

A cardboard, lamp-style desk robot: Raspberry Pi 4, four MG90S servos on a PCA9685 board,
a 2.42" OLED face (in progress), and a synthesized creature voice.

| File | What it is |
|---|---|
| `desk.py` | Servo control: smooth moves, spring-driven `Actor`, wake-up, rest, bedtime |
| `shows.py` | Performances: `showtime()`, `life()`, `hello()`, `disco()`, `peekaboo()`, `spooked()`, `sleepy()`, `catch_fly()`, `emotions()` and more |
| `tests.py` | Joint tests, hold test, nudge tool |
| `sounds.py` | The voice: vowel-shaped creature sounds plus R2-style machine chirps, randomized every time |
| `music.py` | Original background music made on the Pi: dance, spooky, lullaby, playful, fanfare, ambient |
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
