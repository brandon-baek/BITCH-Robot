# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
Brandon, alone. He opens the companion app on his phone and in the BITCH window on his Mac to talk to his desk robot, see what it is doing, and change a setting now and then. He built the robot himself and is a beginner with hardware.

## Product Purpose
BITCH is a four-joint cardboard desk robot (not a lamp) on a Raspberry Pi, with a personality, memory and habits. The app is its companion: the place to talk to it by text, see what it heard and said, and control it. Success means the app stays out of the way. Most of the time BITCH is used by voice, and the app is a glance and a tap.

## Positioning
A robot that feels alive on Brandon's desk, run on his own Mac (Ollama, Whisper, Kokoro), handing anything that needs his accounts (calendar, email, weather, web) to his signed-in ChatGPT on the Pi.

## Operating Context
- The phone is used at arm's length or on the desk, often while BITCH is talking. The Mac window sits beside other work.
- Voice is the main way in: "Hey BITCH". The app shows the turn as it happens: listening, thinking, speaking.
- Devices are pluggable: the Mac mic and camera, a phone's mic and camera, the Pi's Bluetooth speaker. Future hardware is a new driver.

## Capabilities and Constraints
- Main screen, confirmed: the conversation (what Brandon and BITCH said, plus typing to BITCH), the current status, and quick controls: mute BITCH's voice, mute Brandon's own mic, and stop.
- Everything else lives in Settings: camera view and alignment, devices (mic, camera, speaker), volume, behavior switches, people recognition, pairing a phone, connecting the Pi, ChatGPT requests.
- Plain HTML, CSS and JavaScript in `app/`, served by FastAPI on :8780 and :8781. No build step. Must work in the Mac window (WKWebView) and on iPhone Safari over the home certificate.
- Pairing and secure-link setup screens must stay for a phone's first visit.

## Brand Commitments
- The name is BITCH, on purpose. The wake phrase is "Hey BITCH".
- Brandon's stated taste: big Helvetica type, a desk-grey, ink-navy and kraft palette, nothing that looks AI-generated. For this app he asked for Apple-quality design and bare essentials.

## Evidence on Hand
No photos of the robot in the repo. The live camera frame is the only imagery. Do not invent photos, testimonials or features.

## Product Principles
- Voice first, app second: the app confirms and controls; it never demands attention.
- Show the turn honestly: what BITCH heard, what it is doing, what it said.
- One screen of essentials; settings are one tap away and never in the way.
- Readable at arm's length on a phone.

## Accessibility & Inclusion
Status changes are announced to screen readers. Controls are reachable by keyboard in the Mac window and are at least 44 pt on the phone.
