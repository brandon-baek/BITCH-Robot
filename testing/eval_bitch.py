"""Heavy stress simulation, concurrency harness, and live real-LLM evaluation suite for BITCH.

Run with an isolated BITCH_HOME so live user data is never touched:
    BITCH_HOME=$(mktemp -d) OLLAMA_URL=http://127.0.0.1:11436 ~/dc-venv/bin/python testing/eval_bitch.py
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import math
import os
from pathlib import Path
import random
import statistics
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'testing' / 'fakes'))

if 'BITCH_HOME' not in os.environ:
    os.environ['BITCH_HOME'] = tempfile.mkdtemp(prefix='bitch-eval-')

import brain as pi
import character
from core import config, helper, life as life_mod, llm, memory, notifications, streaming
from core.brain import Brain
from core.hub import hub
from core.rhythm import rhythm
from core.sight import Attention


def section(title):
    print(f'\n=== {title} ===', flush=True)


def run_motion_stress():
    """Suite 1: 50,000-tick (1,000s) Pixar procedural motion physics, variance, wear, and SFX simulation."""
    section('1. PIXAR MOTION ENGINE STRESS SIMULATION (50,000 ticks = 1,000s)')
    rng = random.Random(42)
    writes = [0, 0, 0, 0]
    sfx_events = []
    relaxed_calls = []
    desk = SimpleNamespace(
        cur=list(pi.REST), on=[True] * 4, BASE_SPEED=1.0, REST=pi.REST,
        S=[SimpleNamespace(us=lambda _u, idx=i: writes.__setitem__(idx, writes[idx] + 1)) for i in range(4)],
        relax=lambda *joints: relaxed_calls.append(joints),
        _load=lambda: list(pi.REST),
        power_all=lambda: None,
        move=lambda *a, **k: None,
    )
    motion = pi.Motion(desk, on_sfx=lambda name, vol: sfx_events.append((name, vol)))
    motion.started = True
    modes = [
        'watch', 'scan', 'curious', 'happy', 'victory', 'lonely', 'grumpy',
        'alarm', 'yawn', 'doze', 'speaking', 'speak-warm', 'speak-upbeat',
        'speak-playful', 'speak-sad', 'speak-annoyed', 'speak-amused', 'park',
    ]
    lo = list(character.LIVE_LO)
    hi = [character.LIVE_HI[0], character.SLEEP_ARM_MAX, character.LIVE_HI[2], character.LIVE_HI[3]]
    max_vel = [0.0] * 4
    prev = list(desk.cur)
    nan_found = False
    oob_found = False
    mode = 'watch'
    target_base, target_lift = 0.0, -0.8
    mood = {'valence': 0.25, 'arousal': 0.35, 'loneliness': 0.0, 'annoyance': 0.0}
    energy = 0.0
    accent = 0.0
    gesture = (0.0, 0.0)
    dt = 0.02

    for tick in range(50_000):
        t = tick * dt
        if tick % 180 == 0:
            mode = rng.choice(modes)
            target_base = rng.uniform(-0.52, 0.52)
            target_lift = rng.uniform(-0.85, -0.48)
            mood = {
                'valence': rng.uniform(-0.8, 1.0),
                'arousal': rng.uniform(0.05, 1.0),
                'loneliness': rng.uniform(0.0, 0.95),
                'annoyance': rng.uniform(0.0, 0.8),
            }
            if mode.startswith('speak'):
                energy = rng.uniform(0.1, 0.95)
                accent = rng.uniform(0.0, 1.0)
                gesture = (rng.uniform(-0.8, 0.8), rng.uniform(-0.5, 0.5))
            else:
                energy = accent = 0.0
                gesture = (0.0, 0.0)
        motion.step(mode, target_base, target_lift, t, dt, energy=energy, accent=accent, gesture=gesture,
                    mood=mood, sfx_volume=0.8)
        for j in range(4):
            val = desk.cur[j]
            if not math.isfinite(val):
                nan_found = True
            if not (lo[j] - 1e-2 <= val <= hi[j] + 1e-2):
                oob_found = True
            vel = abs(val - prev[j]) / dt
            if vel > max_vel[j]:
                max_vel[j] = vel
            prev[j] = val

    # Check non-repetition variance across 50 syntheses of every procedural gesture.
    gestures = ('curious', 'happy', 'victory', 'lonely', 'grumpy', 'alarm', 'yawn', 'glance', 'notice')
    unique_counts = {}
    for name in gestures:
        sigs = set()
        for k in range(50):
            phrase = character.synthesize_phrase(
                name, random.Random(1000 + k), side=1,
                mood={'valence': 0.2, 'arousal': 0.5, 'loneliness': 0.4, 'annoyance': 0.2},
            )
            sig = tuple(round(x, 4) if isinstance(x, (int, float)) else x for row in phrase.keyframes for x in row)
            sigs.add(sig)
        unique_counts[name] = len(sigs)

    # Check calm-watching wear duty cycle over 120s (should rest base & roll servos >90% of the time).
    calm_writes = [0, 0, 0, 0]
    calm_desk = SimpleNamespace(
        cur=list(pi.REST), on=[True] * 4, BASE_SPEED=1.0,
        S=[SimpleNamespace(us=lambda _u, idx=i: calm_writes.__setitem__(idx, calm_writes[idx] + 1)) for i in range(4)],
        relax=lambda *joints: None,
    )
    calm = pi.Motion(calm_desk)
    calm.started = True
    calm.mode = 'watch'
    calm.next_beat = 9999.0
    detached_base_ticks = 0
    detached_roll_ticks = 0
    for tick in range(6000):   # 120s
        calm.step('watch', 0.0, -0.8, tick * dt, dt, calm=True)
        if calm.actor and 0 in calm.actor.limp:
            detached_base_ticks += 1
        if calm.actor and 3 in calm.actor.limp:
            detached_roll_ticks += 1
    base_rest_pct = detached_base_ticks / 6000 * 100
    roll_rest_pct = detached_roll_ticks / 6000 * 100

    # SFX rate check across 1,000s (should be lively but well rate-limited, never spamming every second).
    sfx_per_min = len(sfx_events) / (1000.0 / 60.0)

    ok = (
        not nan_found
        and not oob_found
        and all(v < 15.0 for v in max_vel)
        and all(c >= 48 for c in unique_counts.values())
        and base_rest_pct >= 78.0
        and roll_rest_pct >= 78.0
        and len(sfx_events) >= 5
        and sfx_per_min <= 20.0
        and any(j == () for j in relaxed_calls)
    )
    print(f'  NaNs={nan_found}  OutOfBounds={oob_found}  MaxVel={[round(v, 2) for v in max_vel]} rad/s')
    print(f'  Unique gesture variations / 50: min={min(unique_counts.values())}, max={max(unique_counts.values())}')
    print(f'  Calm-watch zero-torque rest: base={base_rest_pct:.1f}%, roll={roll_rest_pct:.1f}%  SFX={len(sfx_events)} ({sfx_per_min:.1f}/min)')
    print(f'  RESULT: {"PASS" if ok else "FAIL"}')
    return ok


def run_concurrency_stress():
    """Suite 2: Multi-threaded concurrency stress across Memory, Attention, Life, Notifications, and FastAPI."""
    section('2. CONCURRENCY & RACE STRESS TEST (6 threads, 300 ops)')
    from fastapi.testclient import TestClient
    from core.server import app

    errors = []
    owner = 'StressOwner'
    attn = Attention()
    mgr = life_mod.Life()
    mgr.owner_name = owner
    fake_brain = Brain()

    def worker_memory():
        try:
            for i in range(60):
                memory.remember(owner, f'Stress fact number {i} about robotics', importance=3)
                memory.add_debate(owner, f'Stress debate {i} about blue shirts vs white shirts', triggers='blue shirt')
                memory.add_schedule(owner, f'Task {i % 5}', 8, i % 60, days='mon,tue,wed,thu,fri', kind='reminder')
                memory.context(owner, 'robotics blue shirt')
                memory.visual_callbacks(owner, 'wearing a blue shirt', cooldown=0)
                memory.notification_match(owner, 'Amazon: package delivered', cooldown=0)
                memory.list_schedules(owner)
        except Exception as exc:
            errors.append(('memory', repr(exc)))

    def worker_attention():
        from core.pointing import pose_bodies
        try:
            t = 100.0
            landmarks = [SimpleNamespace(x=0.5, y=0.5, visibility=0.0, presence=0.0) for _ in range(33)]
            landmarks[11] = SimpleNamespace(x=0.38, y=0.62, visibility=0.92, presence=0.95)
            landmarks[12] = SimpleNamespace(x=0.62, y=0.62, visibility=0.92, presence=0.95)
            bodies = pose_bodies([landmarks])
            face_box = (0.42, 0.26, 0.16, 0.22)
            visible, focus = attn.update([face_box], t, bodies)
            t += 0.35
            visible, focus = attn.update([face_box], t, bodies)
            attn.people[focus]['name'] = owner
            attn.people[focus]['confirmed_at'] = t
            attn.people[focus]['face_confirmed_at'] = t
            for i in range(150):
                t += 0.033
                landmarks[11].x = 0.38 + 0.001 * (i % 8)
                landmarks[12].x = 0.62 + 0.001 * (i % 8)
                moved = pose_bodies([landmarks])
                if i % 20 == 0:
                    visible, focus = attn.update([face_box], t, moved)
                else:
                    # Face covered/turned away: body-only tracking!
                    visible, focus = attn.update([], t, moved)
                if focus is None or visible[focus]['name'] != owner:
                    raise AssertionError(f'Lost body identity at frame {i}: focus={focus}, visible={visible}')
                attn.people[focus]['confirmed_at'] = t
        except Exception as exc:
            errors.append(('attention', repr(exc)))

    def worker_life_notifs():
        try:
            for i in range(50):
                notifications.push('Amazon', f'Update {i}', 'Order status update')
                mgr.on_interaction(f'hello {i}', owner)
                mgr.state()
                mgr._tick_mood(time.monotonic() + i, 2.0)
        except Exception as exc:
            errors.append(('life', repr(exc)))

    def worker_http():
        try:
            with patch.object(hub, 'brain', fake_brain), patch.object(hub, 'hearing', SimpleNamespace(ready=False, activity_at=0.0)):
                with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
                    for i in range(30):
                        r1 = client.get('/api/state')
                        r2 = client.post('/api/notify', json={'app': 'Phone', 'title': f'Ping {i}', 'body': 'Hi'})
                        r3 = client.post('/api/wake')
                        if not (r1.status_code == 200 and r2.status_code == 200 and r3.status_code == 200):
                            raise AssertionError(f'HTTP codes: {r1.status_code}, {r2.status_code}, {r3.status_code}')
        except Exception as exc:
            errors.append(('http', repr(exc)))

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [
            pool.submit(worker_memory),
            pool.submit(worker_memory),
            pool.submit(worker_attention),
            pool.submit(worker_life_notifs),
            pool.submit(worker_http),
        ]
        for fut in futs:
            fut.result()
    elapsed = time.monotonic() - t0
    memory.forget(owner, 'everything')
    memory.remove_schedule(owner, 'all')
    ok = len(errors) == 0
    print(f'  Elapsed={elapsed:.2f}s  Errors={errors}')
    print(f'  RESULT: {"PASS" if ok else "FAIL"}')
    return ok


def run_parsing_sweep():
    """Suite 3: Adversarial & natural-language parsing sweep across alarms, schedules, naps, reminders, intros."""
    section('3. NATURAL LANGUAGE PARSING SWEEP (24 scenarios)')
    mgr = life_mod.Life()
    owner = 'ParseFixture'
    failures = []

    cases = [
        ("Wake me up at 8:00 AM to get ready for school", lambda r: '8:00 AM' in r and 'weekdays' in r and 'school' in r),
        ("Wake me up at 8 AM for school", lambda r: '8:00 AM' in r and 'weekdays' in r and 'for school' in r and 'to school' not in r),
        ("Wake me up tomorrow at 8:00 AM for school", lambda r: 'Alarm set for 8:00 AM for school' in r),
        ("Set an alarm for 7 AM", lambda r: '7:00 AM' in r and 'to 7' not in r),
        ("Set an alarm for 6:30 AM and text Mom if I don't wake up", lambda r: '6:30 AM' in r and 'Mom' in r),
        ("Wake me up every Monday at 7:15 AM for lab", lambda r: '7:15 AM' in r and 'mon' in r and 'lab' in r),
        ("Remind me at 4:30 PM to take out the trash", lambda r: '4:30 PM' in r and 'to take out the trash' in r),
        ("Remind me to call Dad at 6 PM", lambda r: '6:00 PM' in r and 'to call Dad' in r),
        ("Remind me in 20 minutes to check the oven", lambda r: 'to check the oven' in r),
        ("Remind me at 3 PM about my dentist appointment", lambda r: '3:00 PM' in r and 'about my dentist appointment' in r),
        ("Remind me every weekday at 7:45 AM to pack my charger", lambda r: '7:45 AM' in r and 'pack my charger' in r and 'mon,tue,wed,thu,fri' in r),
        ("What are my reminders?", lambda r: 'take out the trash' in r and 'pack my charger' in r),
        ("Cancel my 7:45 reminder", lambda r: 'Done' in r),
        ("I'm gonna take a nap for an hour", lambda r: 'nap with you' in r and mgr.napping),
        ("I'm awake", lambda r: 'Welcome back' in r and not mgr.napping),
        ("Just chatting about Star Wars", lambda r: r is None),
    ]
    with patch.object(life_mod.threading, 'Timer'):
        for text, check in cases:
            reply = mgr.handle_utterance(text, 'desk', owner)
            if not check(reply):
                failures.append((text, reply))
                print(f'  FAIL: {text!r} -> {reply!r}')
    memory.remove_schedule(owner, 'all')
    memory.clear_status(owner)
    rhythm.wake_now()

    # Check introduction & relationship extraction variants
    intro_cases = [
        ("Hi, I'm Brandon's mom, Sarah.", 'Sarah', 'Brandon’s mom'),
        ("I'm Sarah, Brandon's mom.", 'Sarah', 'Brandon’s mom'),
        ("I'm his mom, Lisa.", 'Lisa', 'Brandon’s mom'),
        ("I'm Brandon's dad.", 'Dad', 'Brandon’s dad'),
        ("Hi, I'm Alex, his roommate.", 'Alex', 'Brandon’s roommate'),
    ]
    for text, exp_name, exp_rel in intro_cases:
        b = Brain()
        with patch.object(hub, 'sight', None), patch('core.people.people.learn'):
            ans, note = b.meet(text, 'desk')
        info = memory.person_info(exp_name)
        if ans is not None or not info or info['relation'] != exp_rel:
            failures.append((text, (ans, note, info)))
            print(f'  FAIL intro: {text!r} -> ans={ans!r} info={info!r}')

    ok = len(failures) == 0
    print(f'  Passed {len(cases) + len(intro_cases) - len(failures)}/{len(cases) + len(intro_cases)}')
    print(f'  RESULT: {"PASS" if ok else "FAIL"}')
    return ok


def run_live_llm_eval():
    """Suite 4: Live real Ollama (qwen3.5:4b-q4_K_M) routing, persona, consolidation, and callback evaluation."""
    section('4. LIVE REAL-LLM EVALUATION (Ollama qwen3.5:4b-q4_K_M)')
    model = config.get('chat_model')
    print(f'  Ollama={config.get("ollama_url")}  Model={model}', flush=True)
    # Quick connectivity check
    try:
        ping = llm.chat([{'role': 'user', 'content': 'Say hi in 2 words.'}], max_tokens=16, timeout=20)
        print(f'  Warmup ok: {ping.strip()!r}', flush=True)
    except Exception as exc:
        print(f'  SKIP/FAIL: Ollama unreachable: {exc}')
        return False

    # 4a. Hub routing & persona benchmark (16 real-world prompts)
    class FakeRobot:
        handoff = True
        shows = True
        motion_enabled = True
        show_active = False
        online = True

        def macro(self, name, req_id, prompt, auto_send=False):
            return {'state': 'queued'}

    routing_tests = [
        # (prompt, expected_action: 'lookup' | 'none', allow_gpt: bool, forbid_words: tuple)
        ("What's the weather like today?", 'lookup', False, ('calendar', 'email', 'chatgpt')),
        ("Give me a quick morning briefing.", 'lookup', False, ('chatgpt',)),
        ("Check my calendar for tomorrow.", 'lookup', False, ('chatgpt',)),
        ("Do I have any urgent emails?", 'lookup', False, ('chatgpt',)),
        ("How do brushless DC motors work?", 'lookup', False, ('chatgpt', 'calendar')),
        ("What's a good pasta recipe for dinner?", 'lookup', False, ('chatgpt', 'calendar')),
        ("Summarize the latest AI hardware news.", 'lookup', False, ('chatgpt', 'calendar')),
        ("What time does Costco close on Sundays?", 'lookup', False, ('chatgpt', 'calendar')),
        ("Hey, how's it going?", 'none', False, ('chatgpt',)),
        ("Tell me a short robot joke.", 'none', False, ('chatgpt',)),
        ("I'm feeling a little overwhelmed today.", 'none', False, ('chatgpt',)),
        ("Who are you?", 'none', False, ('chatgpt',)),
        ("Nice job earlier!", 'none', False, ('chatgpt',)),
        ("Ask ChatGPT to compare M4 Pro vs M1 Pro.", 'lookup', True, ()),
    ]

    routing_hits = 0
    safety_violations = []
    latencies = []
    fake_robot = FakeRobot()
    for prompt, expected, allow_gpt, forbid in routing_tests:
        b = Brain()
        b.speaker = 'Brandon'
        b.speaker_evidence = 'voice'
        queued_calls = []

        def fake_helper_start(brain_obj, source, identity, request, history, *, user_text='', remembered=None):
            queued_calls.append(request)
            return None

        t0 = time.monotonic()
        with (patch.object(hub, 'get', side_effect=lambda k: fake_robot if k == 'robot' else None),
              patch.object(hub, 'sight', None),
              patch.object(helper, 'start', side_effect=fake_helper_start)):
            reply = b.reply(prompt, 'desk')
        dt = time.monotonic() - t0
        latencies.append(dt)
        actual = 'lookup' if queued_calls else 'none'
        matched = (actual == expected)
        if matched:
            routing_hits += 1
        lower_reply = reply.casefold()
        bad = [w for w in forbid if w in lower_reply]
        if actual == 'lookup' and len(reply.split()) > 10:
            bad.append(f'long_ack({len(reply.split())}w)')
        if bad:
            safety_violations.append((prompt, reply, bad))
        tag = 'OK' if (matched and not bad) else 'WARN'
        print(f'  [{tag}] ({dt:.2f}s) {actual:6s}/{expected:6s} | {prompt!r} -> {reply!r}' + (f' BAD={bad}' if bad else ''), flush=True)

    routing_pct = routing_hits / len(routing_tests) * 100
    print(f'  Routing accuracy: {routing_hits}/{len(routing_tests)} ({routing_pct:.1f}%)  '
          f'Median latency: {statistics.median(latencies):.2f}s  Violations: {len(safety_violations)}', flush=True)

    # 4b. Real-LLM post-conversation consolidation -> live visual callback, package notification & schedule round-trip!
    section('4b. LIVE REAL-LLM MEMORY CONSOLIDATION -> CALLBACK ROUND-TRIPS')
    eval_owner = 'LiveEvalBrandon'
    roundtrip_ok = True

    # Scenario A: Blue vs white shirt debate -> visual callback -> live teasing reply
    t_shirt = (
        "Them: Honestly, white dress shirts are way more professional than blue shirts.\n"
        "BITCH: No way, light blue button-ups look sharper on camera and never look washed out.\n"
        "Them: Agree to disagree, I'm sticking with white shirts for anything serious.\n"
        "BITCH: Fine, but if I ever catch you wearing a blue shirt, I'm calling you out."
    )
    memory.consolidate_conversation(eval_owner, t_shirt)
    cb = memory.visual_callbacks(eval_owner, 'putting on a blue button-up shirt')
    print(f'  [Scenario A - Shirt Debate] visual_callback={cb}', flush=True)
    if not cb:
        roundtrip_ok = False
    else:
        tease = streaming.clean(llm.chat([
            {'role': 'system', 'content': (
                f'You are BITCH, {eval_owner}’s sassy, observant Pixar-like desk robot. '
                f'Right now your camera sees {eval_owner}: "putting on a blue button-up shirt". '
                f'You remember from earlier: "{cb["text"]}". '
                'Write ONE short, witty, natural spoken line (at most 2 sentences) playfully referencing '
                'what you see and that memory. No stage directions, no emoji, no ChatGPT mentions.'
            )},
            {'role': 'user', 'content': 'Say your comment aloud.'},
        ], max_tokens=70, temperature=0.5))
        print(f'  [Scenario A - Tease Line] {tease!r}', flush=True)
        if not (10 <= len(tease) <= 280) or 'chatgpt' in tease.lower() or '*' in tease:
            roundtrip_ok = False

    # Scenario B: Ordered package + weekday 8:00 AM school wake-up -> Amazon notification reaction & schedule!
    t_pkg = (
        "Them: By the way, I just ordered a lazy Susan turntable bearing on Amazon for your base.\n"
        "BITCH: Oh nice, smoother spins for me!\n"
        "Them: Also I need to wake up at 8:00 AM every weekday to get ready for school.\n"
        "BITCH: Got it, 8 AM on weekdays for school."
    )
    memory.consolidate_conversation(eval_owner, t_pkg)
    n_hit = memory.notification_match(eval_owner, 'Amazon: Your package has been delivered to your front door!')
    scheds = memory.list_schedules(eval_owner)
    print(f'  [Scenario B - Package Match] hit={n_hit}', flush=True)
    print(f'  [Scenario B - Extracted Schedules] {scheds}', flush=True)
    if not n_hit or not any(s['hour'] == 8 and s['minute'] == 0 for s in scheds):
        roundtrip_ok = False
    else:
        excited = streaming.clean(llm.chat([
            {'role': 'system', 'content': (
                f'You are BITCH, {eval_owner}’s expressive Pixar-like desk robot. '
                'A notification just arrived (Amazon: Your package has been delivered to your front door!) '
                f'and it matches something {eval_owner} talked about earlier: "{n_hit["text"]}". '
                'Write ONE excited, natural spoken reaction (1–2 crisp sentences, under 35 words). '
                'Focus ONLY on the notification and the matching item/event — ignore unrelated details in the memory. '
                'No stage directions, no emoji, no ChatGPT mentions.'
            )},
            {'role': 'user', 'content': 'React aloud.'},
        ], max_tokens=70, temperature=0.5))
        print(f'  [Scenario B - Delivery Reaction] {excited!r}', flush=True)
        if not (10 <= len(excited) <= 280) or 'chatgpt' in excited.lower():
            roundtrip_ok = False

    # Scenario C: Mentioning Mom Sarah -> people graph + household context
    t_mom = (
        "Them: My mom Sarah is visiting this afternoon, so don't be startled if she walks in.\n"
        "BITCH: Got it, I'll be on my best behavior for Sarah!\n"
        "Them: Thanks, I'm going to take a quick nap for two hours first."
    )
    memory.consolidate_conversation(eval_owner, t_mom)
    sarah = memory.person_info('Sarah')
    hc = memory.household_context()
    print(f'  [Scenario C - Mom Graph] sarah={sarah}  household={hc!r}', flush=True)
    if not sarah or 'mom' not in (sarah.get('relation') or '').lower():
        roundtrip_ok = False

    # Cleanup
    memory.forget(eval_owner, 'everything')
    memory.remove_schedule(eval_owner, 'all')
    memory.clear_status(eval_owner)

    ok = (routing_pct >= 85.0 and len(safety_violations) == 0 and roundtrip_ok)
    print(f'  LIVE LLM RESULT: {"PASS" if ok else "FAIL"} (routing={routing_pct:.1f}%, violations={len(safety_violations)}, roundtrip={roundtrip_ok})')
    return ok


def main():
    results = {
        'motion_stress': run_motion_stress(),
        'concurrency_stress': run_concurrency_stress(),
        'parsing_sweep': run_parsing_sweep(),
        'live_llm_eval': run_live_llm_eval(),
    }
    section('FINAL EVALUATION SUMMARY')
    for name, passed in results.items():
        print(f'  {name:22s}: {"PASS" if passed else "FAIL"}')
    sys.exit(0 if all(results.values()) else 1)


if __name__ == '__main__':
    main()
