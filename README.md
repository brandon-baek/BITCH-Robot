# BITCH

A cardboard, lamp-style desk robot with googly eyes, and the AI that lives in it. Raspberry Pi 4, four MG90S servos
on a PCA9685 board, a JBL speaker, and a brain that runs on your Mac (free and on-device: Whisper, Qwen in Ollama, Kokoro).

You talk to BITCH at the desk, or from your phone anywhere in the house. It remembers what you tell it, sets timers
and reminders, keeps lists, checks the weather, looks things up, sees through the camera, greets you by name,
speaks up on its own now and then, and when you ask for a move it has never done, it invents one.

## Start it

**Mac, once** (Ollama app installed and opened):
```
git -C ~/BITCH-Robot pull 2>/dev/null || git clone https://github.com/brandon-baek/BITCH-Robot ~/BITCH-Robot; bash ~/BITCH-Robot/mac/setup.sh
```
**Mac, every time:** open **BITCH** from Applications (or Spotlight). Allow the camera and microphone the first time.
Closing its window only hides it; BITCH keeps living. Use the BITCH item in the menu bar to open the window again or quit.

**Pi, every time:** `python3 -c "from brain import *; listen()"`
**Pi, after an update:** `bash ~/BITCH-Robot/update.sh`

## Your phone
1. Same Wi-Fi as the Mac. Open `http://<your Mac's name>.local:8780` (the BITCH window shows the exact address under **More**).
2. Type the 6-digit pairing code from **More** on the Mac. Phones only pair once.
3. For voice, the camera and notifications, follow **More › This phone** once: install BITCH's home certificate, then use
   the `https://…:8781` address and add it to your Home Screen.

## Things to say
"Hey BITCH, set a 10 minute timer" · "remind me at 7 to call mum" · "every weekday at 8 tell me the weather" ·
"add eggs to the shopping list" · "remember that my sister is Chloe" · "what am I holding?" · "do the chicken dance" ·
"good night"

## How it fits together
```
 phone app      Mac (mic, camera, speaker)      robot (body, JBL)      new hardware later
     \                    |                          |                        /
      '----------- device link: each device says what it can do ----------'
                                  |
            core (on the Mac):  senses -> mind -> outputs
                                memory, mood, routines, initiative
```
Every sense and output is a **device** with capabilities: `ears`, `eyes`, `keys` (senses) and `voice`, `body`, `face`,
`notify` (outputs). Adding a mic, camera or OLED to the robot means writing one small driver in `core/devices/`.

| Where | What |
|---|---|
| `core/` | BITCH's mind, runs on the Mac: `python -m core` |
| `core/devices/` | One driver per kind of device: `robot.py` (the Pi), `mac.py` (Mac mic, camera, speaker), `web.py` (phones and the app) |
| `core/brain.py`, `persona.md` | How it thinks and who it is |
| `core/tools.py` | What it can do (timers, reminders, lists, weather, look-ups, camera, routines) |
| `core/moves.py` | Its body language, and how it invents new moves |
| `core/life.py` | Mood, schedule, greetings, speaking up on its own, memories and diary |
| `core/memory.py` | Everything it remembers, in `~/.bitch/bitch.db` |
| `app/` | The app for your phone and the Mac window |
| `brain.py` | On the Pi: listens to the Mac and moves the servos (also plays invented moves) |
| `face.py`, `hardware.py` | On the Pi: the OLED googly-eye face, and finding hardware plugged into the robot (screen, USB mic, camera) |
| `desk.py`, `shows.py`, `sounds.py`, `music.py` | On the Pi: servos, performances, voice and music |
| `control.py`, `tests.py` | On the Pi: keyboard driving, joint tests |
| `testing/` | Tests that need no hardware: `~/dc-venv/bin/python -m unittest testing.test_bitch` |

Servo channels on the PCA9685: 0 base, 1 arm, 2 neck, 3 head.

## Hardware on the robot itself
Plug it in and restart listen mode; the Mac picks it up by itself (see **More › Devices**).
- **OLED face** (I2C: SDA pin 3, SCL pin 5, RES pin 7): `pip install adafruit-circuitpython-ssd1306 --break-system-packages`,
  then `python3 face.py` to see every feeling. BITCH's eyes then blink, look around, talk and sleep on the screen.
- **USB microphone**: BITCH hears through the robot instead of the Mac (no setup).
- Check what the robot finds: `python3 hardware.py`

## On the Pi without the Mac
Run a show: `python3 -c "from shows import *; showtime()"` · Drive with the keyboard: `python3 -c "from control import *; drive()"`
