# BITCH

A cardboard desk robot with four servos, a Raspberry Pi and a JBL speaker. The Mac runs speech recognition, conversation and anonymous face tracking. A paired phone or computer can supply the mic, camera or both.

## Run it

1. On the Mac, run `bash ~/BITCH-Robot/mac/setup.sh` once, then open **BITCH** from Applications. Allow the microphone and camera if prompted. The same controls are available at `http://127.0.0.1:8780` while it runs.
2. On the Pi, stop the old listener and run `python3 brain.py` from the updated checkout. In another Pi terminal, run `python3 brain.py --key`.
   Use `python3 brain.py --no-motion` to test pairing, audio and desktop handoff with the servo driver disabled.
3. On the Mac, open **Connections → Connect the Raspberry Pi**, enter the Pi's name or IPv4 address and paste the key. Use the Pi and Mac on a trusted local network. The Pi checks for the connected JBL Flip 3 PipeWire output. To use a different output, set BITCH_PI_SPEAKER to its PipeWire description or to `default` in the Pi service environment. **Auto** chooses it when available and otherwise uses the Mac speaker.

Say **“Hey BITCH, what’s the time?”** or **“Why do cats knead blankets, BITCH?”**. BITCH passes the full request through, including the words before its name. A short tone acknowledges the name on its own. The rising two-note cue opens your next turn for 24 seconds on the same microphone. A natural goodbye or closing acknowledgment can end the session immediately. The screen shows when BITCH is waiting for speech, hearing you, thinking, or speaking. Say **“stop listening”** or let the conversation time out to return to the wake word.

Idle motion holds attention through brief gaps and leaves space between gestures. BITCH has different poses for doing a double take, listening, thinking, speaking, getting curious, looking aside, and scanning an empty room. Each gesture has a lead-in, a hold and a return. Quick tracking still follows you when you move. Switch **Idle motion** off to park the body. **Voice volume** adjusts the speaker output. **Mute voice** keeps replies on the screen. Existing settings and the old local database stay on disk. The active companion only reads the pairing records in that database.

## Use an external microphone or camera

Plug a USB device into the Mac and select it as the macOS input, or use **Connections → Use this browser’s devices** to choose the exact input. A browser keeps sharing while its page is open and selected. Microphones and cameras each have their own source selector, so they can come from different devices.

For another device on the same Wi-Fi, use **Connections → Pair a phone or another computer** on the Mac. Open the address shown there, install and trust the BITCH Home certificate, and use the secure link. Enter the pairing code shown on the Mac and choose **Use microphone**, **Use camera**, or both. Keep the page in the foreground on phones. The same pairing works in the Pi’s desktop browser for a camera or mic plugged into the Pi.

For the native Mac capture, use `~/dc-venv/bin/python -m sounddevice` to list microphone indices and start the companion with `~/dc-venv/bin/python -m core --mic N --camera N` to pick specific devices.

## Align the camera and body

**What BITCH sees** shows the live image, anonymous person boxes and which one the robot is following. The Body readout distinguishes idle off, scanning, following and Pi connection problems. It shows the requested turn next to the last commanded position from the Pi.

1. Fix the camera in place and choose **Align camera to robot** under the feed.
2. Stand at your usual distance on the robot’s left. Aim **Turn** and **Head pitch** at your face, hold still and capture. Repeat from the center and right.
3. Select **Save & start tracking** for an immediate check, or **Save only** to keep the current idle setting. Old horizontal alignments still work. Repeat after moving the camera or changing your usual distance substantially.

The alignment maps what the camera sees at three working positions to this robot’s yaw and pitch. At substantially different distances, a camera far to the side can still introduce parallax. For individual servo trim, stop the Pi listener and run `python3 center.py calibrate`.

### Movement references

- Apple's [ELEGNT paper](https://arxiv.org/html/2501.12493v1) motivated a short head cue before the base turns and restrained motion during interaction.
- Disney's [Realistic and Interactive Robot Gaze](https://la.disneyresearch.com/publication/realistic-and-interactive-robot-gaze/) informed holding a target through short gaps, reducing abrupt switches and periodically shifting attention.
- Disney's [Olaf](https://arxiv.org/html/2512.16705v1), [BDX](https://la.disneyresearch.com/publication/design-and-control-of-a-bipedal-robotic-character/) and [Vibration-Minimizing Motion Retargeting](https://la.disneyresearch.com/publication/publication-process-vibration-minimizing-motion-retargeting-for-robotic-characters/) motivated authored poses, gentle transitions, and rate and acceleration limits for this lightweight body.

## Recognize people

On the Mac, open **Connections → Recognize people**. Type a name and choose **Learn face** or **Learn voice**. Enroll only someone who agrees.

- For a face, stand alone in the camera view and turn your head a little as six samples are captured.
- For a voice, say three different sentences, around three seconds each, with a short pause after each one. You can enroll both under the same name.

A known face can appear by name in the Mac preview and get a quiet double take. A confident voice match can carry the speaker’s name and turn context into the conversation. Unknown or ambiguous matches are left unnamed. The saved profiles are local face and voice embeddings. Use **Forget** to remove both. Recognition does not authenticate commands.

## Desktop agent handoff

Say **“BITCH, ask ChatGPT to suggest a name for my lamp.”** The companion sends that explicit request to the Pi. The signed-in Chromium session opens a new ChatGPT conversation, pastes the attributed request, sends it, and returns the completed reply to the local Pi inbox. Then ask **“What did ChatGPT say?”** to hear it. Everyday conversation uses the local models.

The [ChatGPT inbox](#chatgpt-inbox) shows the status of each request and offers manual recovery if the Pi browser is unavailable. The companion does not automatically retry a send it could not verify.

## Nemotron VoiceChat trial

On Apple Silicon, allow about 12 GB of free disk space and run `bash mac/setup-voicechat.sh` to install the pinned speech-swift CLI and INT5 VoiceChat bundle. Enable **Try Nemotron VoiceChat** under **Connections** on the Mac. The trial runs speech-to-speech on each completed addressed utterance using the project's microphone, wake gate, and speaker. This uses the CLI’s file mode, so the full audio reply is generated before playback.

Qwen handles complex requests, vision, recall, skills, and fallbacks locally. You can say **“BITCH, ask Qwen to explain how this works”** to route a question to it directly. **“Ask ChatGPT to…”** explicitly uses the Pi’s signed-in browser. An alternate installed bundle can be selected with `BITCH_VOICECHAT_MODEL` or the Mac setting `voicechat_model`.

## Verify locally

Run `~/dc-venv/bin/python -m unittest testing.test_bitch -v` for wake, conversation, alignment, pairing, the macro inbox and the Pi protocol with a fake servo board.

### Live character and conversation

The shows and camera tracker use the same four-joint Actor. A new face can get a double take, a missing face gets a brief search near its last position, and quiet periods make space for peeks and stretches. A listening lean holds through a pause, and a sustained head tilt from the focused person can get a delayed echo. While speaking, the head follows phrase accents and the arm moves with the slower changes in vocal energy. The reply’s delivery shapes the larger gesture, including a closing dip on a farewell. Parking and alignment have no idle offset.

A brief chirp acknowledges a wake with a question. Longer waits can add a quiet thinking sound. The two rising notes invite a follow-up. After the Pi speaker has been idle, a short, low-volume signal opens the Bluetooth audio path before the cue or first word. The app shows **I’m listening** during an open utterance and **Got it. One moment…** during transcription. Recording is gated through playback completion and a short echo tail. **Test speaker** plays a brief check through the displayed output. In **Auto**, disconnecting the Pi speaker allows the Mac to take over.

Ask “Hey BITCH, what am I holding?” to send one fresh frame from the selected camera to the local Qwen 3.5 4B vision model. Short visual follow-ups get a new frame. Camera images are kept out of conversation history. Face tracking continues independently of visual question answering.

## Look where I’m pointing

Say **“Hey BITCH, look there”** while holding one arm out toward the target. Keep your shoulder, elbow, wrist and hand in the camera frame for a moment. The preview circles the region it picked. **“Look at the blue mug”** looks for a named object. **“What am I pointing at?”** can ask whether a visible object in that region is the one you meant. Camera alignment works best when the target is near the calibration distance. Run `bash mac/setup.sh` on the Mac to install pointing support.

The [character motion and shared attention notes](docs/character-motion.md) describe the research, the choices carried into the robot, and how to evaluate the result.

If you run Ollama on a different local port, set `ollama_url` in the Mac companion’s `~/.bitch/settings.json` or pass `OLLAMA_URL` when launching it. On macOS, a model directory outside iCloud Drive keeps model files available for local inference.

## Interruptions, names and check-ins

While BITCH is speaking, start your correction naturally, such as **“Actually, the blue one is mine”** or **“Wait, I meant tomorrow.”** An enrolled speaker can cut in with other phrasing. BITCH checks microphone audio and its transcription against what it was just saying. Once it yields, keep speaking so it gets the rest of your sentence in the next turn. Use **Stop speech** to end the conversation or a performance from the screen.

Say **“Remember that my bike is lime green”**, **“What do you remember?”**, or **“Forget my bike.”** Saved notes persist on the Mac and are scoped to a recognized voice. **Remember natural preferences** also lets BITCH retain clearly stated lasting details without a “remember” command.

Say **“Follow my finger around”** and show one extended index finger. The robot follows it for up to 25 seconds while it stays in view. Say **“Stop following”** to end early. The companion uses a local hand model whose weights are installed by `mac/setup.sh`.

**Keep Pi audio awake** keeps a very quiet PipeWire stream open while the companion is using the Pi speaker. The stream expires when the companion disconnects. It can play an occasional low chime between replies. **Occasional visual check-ins** lets the companion start a short conversation after a long quiet period if it sees one recognized person clearly reading or doing a desk project. It classifies a frame locally and keeps no activity log.

## Performances and new routines

Say **“BITCH, dance,” “BITCH, do the disco,”** or **“BITCH, sing something.”** The Pi plays the original score and choreography through the same spring controller and joint limits it uses for conversation. **“Stop the show”** stops the track and returns control to the normal motion system.

Say **“BITCH, invent a routine called Victory Lap that bows, chirps twice, then sways.”** Or say **“BITCH, when I say inspection, say ‘let’s see,’ follow my finger for three seconds, then nod.”** Use **“BITCH, inspection”** or **“BITCH, do Victory Lap”** to run one again. Named routines persist on the Mac. A routine can combine speech, looking at a person or pointing target, following a fingertip, gestures, holds, and chirps. It can choose steps based on whether the camera sees a person at startup. The robot reports when an installed capability or a target is unavailable.

## ChatGPT inbox

On the Pi desktop, open `http://127.0.0.1:8765/inbox`. New spoken requests send automatically. **Send with ChatGPT** runs a selected older request. **Draft in ChatGPT** and **Show formatted prompt** are available for manual recovery. The inbox preserves a returned answer, and BITCH can read it when asked.

The draft macro uses the Pi’s Wayland desktop, Chromium, `at-spi2-core`, `gir1.2-atspi-2.0`, `wl-clipboard`, and `wtype`. If Chromium was already running without AT-SPI support, reopen it from the Pi desktop with `env -u NO_AT_BRIDGE chromium --force-renderer-accessibility --restore-last-session`.

Local conversation streams complete phrases from Ollama into speech. BITCH can start speaking during generation and prepare the next phrase while the current one plays. A quiet sound acknowledges an accepted turn while the model works. Transcription can start during a pause, and BITCH waits for the same end-of-turn silence before answering. A short reply may still arrive as one phrase.

While BITCH is replying, cut in with a short sentence such as **“Actually, I meant the blue one.”** The microphone compares the interruption against playback before forwarding it to the next turn. In **Connections**, **Learn my preferences** lets BITCH keep durable preferences or project details that a recognized speaker says in ordinary conversation. Use **“You can forget about…”** to remove a saved detail.
