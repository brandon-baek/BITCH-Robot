"""Checks for wake gating, alignment, pairing and the Pi protocol."""
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode
import urllib.error
import urllib.request
import wave

import numpy as np

SCRATCH = tempfile.TemporaryDirectory(prefix='bitch-test-')
os.environ['BITCH_HOME'] = SCRATCH.name
os.environ['BITCH_PI_HOME'] = SCRATCH.name

import brain as pi
from core import config, gaze, memory, voice
from core.brain import Brain
from core.devices.base import Device
from core.hub import hub
from core.sight import Attention, HeadGesture, Sight, aim_at
from core.hearing import Hearing
from core.people import People
import macros


class WakeTests(unittest.TestCase):
    def test_only_addressed_speech_enters_the_queue(self):
        brain = Brain()
        for sentence in ['Playing in the kitchen', 'Ask ChatGPT to read my calendar', 'Call her a bitch', 'The bit chart']:
            self.assertFalse(brain.submit(sentence, 'desk'), sentence)
        self.assertTrue(brain.submit('Hey, BITCH! What time is it?', 'desk'))
        self.assertEqual(brain.requests.get_nowait(), ('What time is it?', 'desk'))
        self.assertFalse(brain.submit('Hey BITCH, hi', 'desk'))

    def test_wake_address_preserves_words_before_and_after_the_name(self):
        for spoken, expected in (
            ('Tell me the time BITCH.', 'Tell me the time.'),
            ('Can you look over here, BITCH?', 'Can you look over here?'),
            ('Can you look over here BITCH please?', 'Can you look over here?'),
            ('Can you, BITCH, tell me the time?', 'Can you tell me the time?'),
        ):
            brain = Brain()
            self.assertTrue(brain.addressed(spoken, 'desk', time.monotonic()))
            self.assertTrue(brain.submit(spoken, 'desk'))
            self.assertEqual(brain.requests.get_nowait(), (expected, 'desk'))
            self.assertTrue(brain.accepted_wake)
        self.assertFalse(Brain().submit('She called me a bitch.', 'desk'))

    def test_one_followup_from_the_same_microphone(self):
        brain = Brain()
        brain.followup = ('desk', time.monotonic() + 7)
        self.assertFalse(brain.submit('Can you hear me?', 'phone'))
        self.assertTrue(brain.submit('Can you hear me?', 'desk'))
        self.assertIsNone(brain.followup)
        other = Brain()
        other.followup = ('desk', time.monotonic() - 1)
        self.assertFalse(other.submit('Can you hear me?', 'desk'))

    def test_each_turn_renews_the_conversation_on_the_same_mic(self):
        brain = Brain()
        with patch.object(hub, 'accepts', return_value=True), patch.object(brain, 'reply', return_value='Okay.'), patch.object(voice, 'say'):
            brain.start()
            for index, text in enumerate(['Hey BITCH, first', 'second turn', 'third turn', 'stop listening']):
                self.assertTrue(brain.submit(text, 'desk'))
                deadline = time.monotonic() + 2
                while brain.busy.is_set() and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertFalse(brain.busy.is_set())
                self.assertEqual(brain.followup_seconds() > 0, index < 3)
            self.assertFalse(brain.submit('outside the window', 'desk'))

    def test_brain_can_only_queue_an_explicit_agent_request(self):
        brain = Brain()
        saved = []
        robot = SimpleNamespace(macro=lambda *args: saved.append(args) or True, handoff=True, auto_handoff=False)
        with patch.object(hub, 'get', return_value=robot), patch('core.brain.llm.chat', return_value='*waves* Got it.') as model:
            self.assertEqual(brain.reply('Could an agent help?', 'desk'), 'Got it.')
            self.assertEqual(saved, [])
            self.assertIn('I queued', brain.reply('Could you ask Chat GPT to draft a summary.', 'desk'))
            self.assertEqual(saved[0][1], 'agent')
            self.assertTrue(saved[0][2].startswith('draft a summary.'))
            self.assertIn('read aloud', saved[0][2])
            model.assert_called_once()

    def test_chatgpt_answers_come_back_and_are_spoken_without_markdown(self):
        from core import helper

        brain = Brain()
        statuses = iter([{'state': 'sending'}, {'state': 'returned', 'answer': 'As ChatGPT, **Sunny**, 64°F. [Forecast](https://x.y)'}])
        robot = SimpleNamespace(macro=lambda *args: True, handoff=True, auto_handoff=True,
                                macro_status=lambda request_id: next(statuses))
        announced = []
        brain.announce = lambda source, text, heard=None, chime=False, **kw: announced.append((source, text, heard, kw))
        with (patch.object(hub, 'get', return_value=robot), patch.object(helper.time, 'sleep'),
              patch.object(helper.threading, 'Thread')):
            self.assertIsNone(helper.start(brain, 'desk', None, "What's the weather?"))
            self.assertEqual(helper.tasks[-1]['state'], 'working')
            helper.watch(brain, 'desk', helper.tasks[-1])
        self.assertEqual(announced[0][:2], ('desk', 'Sunny, 64°F. Forecast'))
        self.assertNotIn('ChatGPT', announced[0][1])
        self.assertNotIn('ChatGPT', announced[0][2])
        self.assertEqual(helper.tasks[-1]['state'], 'done')
        self.assertTrue(helper.OUTSIDE.search("What's on my calendar tomorrow?"))
        self.assertTrue(helper.should_lookup("Give me a morning briefing."))
        # Hallucinated calendar/email or unprompted ChatGPT mentions get sanitized from the quick spoken ack.
        self.assertEqual(
            helper.sanitize_ack("Let me check your calendar and email through ChatGPT for tomorrow's weather.",
                                "What's the weather tomorrow?"),
            'Let me check.',
        )
        # Immediate 'ready' failure from Pi browser reports right away instead of hanging until timeout.
        robot.macro_status = lambda request_id: {'state': 'ready', 'reason': 'Sign in to ChatGPT in Chromium on the Pi.'}
        announced.clear()
        with patch.object(hub, 'get', return_value=robot), patch.object(helper.time, 'sleep'):
            helper.watch(brain, 'desk', {'id': 'r', 'request': 'Weather?', 'state': 'working', 'explicit': False})
        self.assertIn('signed in', announced[0][1])
        self.assertNotIn('ChatGPT', announced[0][1])
        # Explicit ChatGPT request may still name ChatGPT when reporting tab status.
        robot.macro_status = lambda request_id: {'state': 'send_unverified'}
        announced.clear()
        with patch.object(hub, 'get', return_value=robot), patch.object(helper.time, 'sleep'):
            helper.watch(brain, 'desk', {'id': 'x', 'request': 'Ask ChatGPT', 'state': 'working', 'explicit': True})
        self.assertIn('ChatGPT tab', announced[0][1])

    def test_slow_hub_request_turns_into_background_notification_and_returns_later(self):
        from core import helper

        brain = Brain()
        clock = [100.]
        statuses = iter([
            {'state': 'sending'},
            {'state': 'sending'},
            {'state': 'returned', 'answer': 'You have a 10 AM design review and clear skies at 68°F.'},
        ])
        robot = SimpleNamespace(macro=lambda *args: True, handoff=True, auto_handoff=True,
                                macro_status=lambda request_id: next(statuses))
        announced = []
        brain.announce = lambda source, text, heard=None, chime=False, **kw: announced.append((text, chime, kw))

        def fake_monotonic():
            val = clock[0]
            clock[0] += 8.
            return val

        with (patch.object(hub, 'get', return_value=robot), patch.object(helper.time, 'sleep'),
              patch.object(helper.time, 'monotonic', side_effect=fake_monotonic),
              patch.object(helper, 'spoken', side_effect=lambda ans, **kw: 'Briefing ready: ' + ans),
              patch.object(helper.threading, 'Thread')):
            helper.start(brain, 'desk', 'Brandon', 'Give me a briefing.', user_text='Give me a briefing.')
            task = helper.tasks[-1]
            self.assertTrue(helper.waiting())
            helper.watch(brain, 'desk', task)
        self.assertEqual(task['state'], 'done')
        self.assertFalse(helper.waiting())
        # First announcement was the quiet background heads-up (closing turn), second was the chime notification.
        self.assertEqual(len(announced), 2)
        self.assertTrue(announced[0][2].get('close_turn'))
        self.assertNotIn('ChatGPT', announced[0][0])
        self.assertTrue(announced[1][1])
        self.assertIn('Briefing ready:', announced[1][0])

    def test_voicechat_gets_addressed_audio_and_qwen_handles_the_local_agent_request(self):
        brain = Brain()
        audio = np.zeros(16000, dtype=np.float32)
        self.assertTrue(brain.submit('Bitch, how are you feeling?', 'desk', audio=audio))
        text, source = brain.requests.get_nowait()
        stream = SimpleNamespace(parts=[])
        with (patch.dict(config.stored, {'voicechat_enabled': True}), patch.object(hub, 'sight', None),
              patch('core.brain.voicechat.respond', return_value=('A little smug.', (b'wav', .8))) as native,
              patch('core.brain.llm.chat', return_value='{"v":"n","say":"It lowers the freezing point.","end":false}') as agent):
            self.assertEqual(brain.reply(text, source, stream=stream), 'A little smug.')
            self.assertIs(native.call_args.args[0], audio)
            self.assertEqual(brain.native_audio, (b'wav', .8))
            self.assertEqual(brain.reply('Ask Qwen to explain how salt melts ice.', source, stream=stream),
                             'It lowers the freezing point.')
            self.assertEqual(agent.call_args.args[0][-1]['content'], 'explain how salt melts ice.')
            native.assert_called_once()

    def test_volume_attenuates_speech_and_cues(self):
        samples = np.ones(240, dtype=np.float32) * 0.5
        with patch.object(config, 'get', return_value=100):
            loud, _ = voice.encode(samples)
        with patch.object(config, 'get', return_value=40):
            quiet, _ = voice.encode(samples)
        with wave.open(io.BytesIO(loud), 'rb') as a, wave.open(io.BytesIO(quiet), 'rb') as b:
            peak = lambda stream: np.frombuffer(stream.readframes(stream.getnframes()), '<i2').max()
            full, reduced = peak(a), peak(b)
            self.assertGreater(reduced, 0)
            self.assertLess(reduced, full / 2)

    def test_model_closure_does_not_rearm_the_mic_or_play_a_listening_cue(self):
        brain = Brain()
        with (patch.object(hub, 'accepts', return_value=True), patch.object(voice, 'say') as say,
              patch('core.brain.llm.chat', return_value='{"say":"Take care.","turn":"end","delivery":"warm"}')):
            brain.start()
            self.assertTrue(brain.submit("Hey BITCH, that answers it. Appreciate it.", 'desk'))
            deadline = time.monotonic() + 2
            while brain.busy.is_set() and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertFalse(brain.busy.is_set())
            say.assert_called_once_with('Take care.', brain.hush, ready_cue=False, delivery='warm')
            self.assertEqual(brain.followup_seconds(), 0)
            self.assertFalse(brain.addressed("Let's go to the store", 'desk', time.monotonic()))
            # The thread is kept, so "Hey BITCH, one more thing" a minute later still has context.
            self.assertEqual(len(brain.history[('desk', None)]), 2)
            brain.history_at[('desk', None)] -= 901
            brain.clear_conversation('desk')
            self.assertFalse(brain.history)

    def test_new_question_overrides_model_closure(self):
        brain = Brain()
        with patch('core.brain.llm.chat', return_value='{"say":"Yes.","turn":"end","delivery":"neutral"}'):
            self.assertEqual(brain.reply('Thanks. One more thing, does that work on the Mac?', 'desk'), 'Yes.')
        self.assertFalse(brain.close_turn)

    def test_conversation_grounds_object_handling_and_drops_model_parroting(self):
        brain = Brain()
        with patch('core.brain.llm.chat') as generate:
            self.assertIn('don’t have hands', brain.reply('Did you pick up the mug?', 'desk'))
            generate.assert_not_called()
            generate.return_value = '{"say":"What did you just say?","turn":"continue"}'
            self.assertEqual(brain.reply('What did you just say?', 'desk'), 'I lost my train of thought. Say that again?')

    def test_streaming_phrases_read_escaped_spoken_words_and_skip_the_rest(self):
        from core.streaming import Phrases

        phrases = Phrases('Tell me a fact about water.')
        partial = '{"delivery":"warm","performance":"none","say":"Water moves through rivers and clouds. It'
        self.assertEqual(phrases.feed(partial), (['Water moves through rivers and clouds.'],
                                               {'delivery': 'warm', 'performance': 'none'}))
        self.assertEqual(phrases.feed(partial + r' also freezes at zero\u002e","turn":"continue"}')[0],
                         ['It also freezes at zero.'])

    def test_compact_turn_streams_expression_before_the_closure_field(self):
        brain = Brain()
        stream = SimpleNamespace(parts=[])
        stream.offer = lambda phrase, delivery, **kwargs: stream.parts.append((phrase, delivery))
        partial = '{"v":"a","say":"Oh, come on! A fern beat me.'

        def generate(messages, *, schema, model, on_delta, stop, temperature):
            self.assertEqual(schema['required'], ['v', 'say', 'end'])
            on_delta(partial)
            self.assertEqual(stream.parts, [('Oh, come on!', 'emphatic')])
            return partial + '","end":false}'

        with patch('core.brain.llm.chat', side_effect=generate):
            self.assertEqual(brain.reply('A plant won the staring contest.', 'desk', stream=stream),
                             'Oh, come on! A fern beat me.')
        self.assertEqual(brain.performance, 'annoyed')
        self.assertFalse(brain.close_turn)

    def test_model_can_look_through_the_camera_once_and_then_answers_from_the_frame(self):
        brain = Brain()
        calls = []

        def generate(messages, **kwargs):
            calls.append((kwargs['schema'], messages[-1].get('images')))
            if len(calls) == 1:
                return '{"do":"look","v":"n","say":"Let me see.","end":false}'
            return '{"delivery":"neutral","performance":"none","say":"A red mug.","turn":"continue","remember":"","show":"none"}'

        sight = SimpleNamespace(current_scene=lambda: (b'jpeg', 1))
        with patch.object(hub, 'sight', sight), patch('core.brain.llm.chat', side_effect=generate):
            self.assertEqual(brain.reply('Guess what this thing is.', 'desk'), 'A red mug.')
        self.assertEqual(len(calls), 2)
        self.assertIn('look', calls[0][0]['properties']['do']['enum'])
        self.assertNotIn('do', calls[1][0]['properties'])
        self.assertTrue(calls[1][1])

    def test_people_are_introduced_and_forgotten_in_conversation(self):
        from core.people import people

        brain = Brain()
        brain.turn_audio = np.zeros(16000, np.float32)
        sight = SimpleNamespace(face_to_learn=lambda stranger: ('cam', 7 if stranger else 3, None))
        learned = []
        with (patch.object(hub, 'sight', sight), patch.object(people, 'profiles', {}),
              patch.object(people, 'learn', side_effect=lambda name, **kwargs: learned.append((name, kwargs)))):
            answer, note = brain.meet("Hi, I'm Sam.", 'desk')
            self.assertIsNone(answer)
            self.assertIn('Sam', note)
            self.assertEqual((brain.speaker, brain.speaker_evidence), ('Sam', 'introduced'))
            self.assertEqual(learned[-1][1]['face'], ('cam', 3))
            self.assertIsNotNone(learned[-1][1]['first'])
            answer, note = brain.meet('This is my friend Alex.', 'desk')
            self.assertIn('Alex', note)
            self.assertEqual(learned[-1][1]['face'], ('cam', 7))
            self.assertIsNone(learned[-1][1]['first'])
            self.assertEqual(brain.speaker, 'Sam')
            self.assertEqual(brain.meet("I'm tired.", 'desk'), (None, None))
            with patch.object(people, 'forget') as forget:
                self.assertIn('forgotten you, Sam', brain.meet('Forget me.', 'desk')[0])
                forget.assert_called_once_with('Sam', None)
            self.assertIsNone(brain.speaker)

    def test_app_state_reports_live_captions_company_and_people(self):
        from core import server

        brain = Brain()
        brain.busy.set()
        brain.speaker, brain.speaker_evidence, brain.spoken_reply = 'Sam', 'voice', 'Hello there.'
        hearing = SimpleNamespace(phase=lambda: None, activity_at=0, ready=True)
        with (patch.object(hub, 'brain', brain), patch.object(hub, 'hearing', hearing), patch.object(hub, 'sight', None),
              patch.object(hub, 'status', 'Speaking'), patch.object(server, 'authorize', return_value='local')):
            data = server.state(None)
        self.assertEqual(data['speaking'], 'Hello there.')
        self.assertEqual(data['company'], {'name': 'Sam', 'by': 'voice'})
        self.assertIn('learning', data['people'])

    def test_actions_are_offered_only_when_the_request_calls_for_them(self):
        brain = Brain()
        robot = SimpleNamespace(shows=True, motion_enabled=True, show_active=False, handoff=True)
        # Substantive / informational questions and briefings route through lookup; robot banter stays local.
        self.assertIn('lookup', brain.available_actions("What's the square root of 144?", robot, False)[0])
        self.assertIn('lookup', brain.available_actions('Give me a morning briefing.', robot, False)[0])
        self.assertEqual(brain.available_actions('How are you feeling today?', robot, False)[0], ['none'])
        self.assertIn('dance', brain.available_actions('Can you bust a move?', robot, False)[0])
        self.assertNotIn('lookup', brain.available_actions('Do you like your voice?', robot, False)[0])
        self.assertIn('lookup', brain.available_actions('Get ChatGPT to plan my week.', robot, False)[0])

    def test_streamed_phrases_carry_mood_changes_without_reading_codes_aloud(self):
        from core.streaming import Phrases

        phrases = Phrases('How are you feeling?')
        parts, _ = phrases.feed('{"v":"a","say":"Oh, come on! [p] Fine, you win.","end":false}')
        self.assertEqual(parts, ['Oh, come on!', 'Fine, you win.'])
        self.assertEqual(phrases.styles, [None, 'p'])

    def test_streaming_reply_can_stop_before_playing_a_queued_phrase(self):
        import threading

        stop = threading.Event()
        spoken = []
        stream = voice.ReplyStream(stop, lambda: None, spoken.append)
        with patch.object(voice, 'render', return_value=(b'wav', 1.0)), patch.object(
                voice, 'play', side_effect=lambda *args, **kwargs: stop.set()):
            stream.offer('First sentence.', 'neutral')
            stream.offer('Second sentence.', 'warm')
            stream.close()
        self.assertEqual(spoken, ['First sentence.'])

    def test_ready_sentences_play_as_one_clip_without_synthesizer_padding(self):
        import threading

        padded = np.r_[np.zeros(9600), np.sin(np.arange(24000) * .1), np.zeros(12000)].astype(np.float32)
        trimmed = voice.trim(padded, 24000)
        self.assertLess(len(trimmed) / 24000, 1.2)
        self.assertGreater(len(trimmed) / 24000, 1.)
        sentence = voice.encode(trimmed)
        gate, plays, spoken = threading.Event(), [], []

        def speaker(wav, duration, stop, **kwargs):
            plays.append((duration, kwargs['delivery']))
            gate.wait(2)

        stream = voice.ReplyStream(threading.Event(), lambda: None, spoken.append)
        with patch.object(voice, 'render', return_value=sentence), patch.object(voice, 'play', side_effect=speaker):
            stream.offer('One.', 'neutral')
            while not plays:
                time.sleep(.01)
            for phrase in ('Two.', 'Three.'):
                stream.offer(phrase, 'neutral')
            stream.offer('Four.', 'warm')
            time.sleep(.1)
            gate.set()
            stream.close()
        self.assertEqual(spoken, ['One.', 'Two.', 'Three.', 'Four.'])
        self.assertEqual([delivery for _, delivery in plays], ['neutral', 'neutral', 'warm'])
        self.assertAlmostEqual(plays[1][0], 2 * sentence[1] + .16, places=2)

    def test_a_short_opening_sentence_streams_by_itself(self):
        from core.streaming import Phrases

        self.assertEqual(Phrases('What is your favorite color?').feed('{"v":"w","say":"Yellow. It')[0], ['Yellow.'])
        self.assertEqual(Phrases('Yes I think so too').feed('{"v":"n","say":"Yes. Me')[0], ['Yes.'])

    def test_a_finished_sounding_preview_ends_the_turn_but_a_trailing_one_waits(self):
        from core.hearing import sounds_finished

        for text in ('Hey BITCH, what time is it?', 'Tell me a joke.', 'Yes.', 'I like that.'):
            self.assertTrue(sounds_finished(text), text)
        for text in ('Hey BITCH.', 'Hey BITCH, I was thinking and.', 'So the thing is,', 'Well...', 'Can you', ''):
            self.assertFalse(sounds_finished(text), text)

    def test_interruption_opens_the_same_mic_and_rejects_playback_keyword(self):
        brain = Brain()
        brain.busy.set()
        brain.active_source = 'desk'
        brain.spoken_reply = 'Hold on to the rail.'
        with patch.object(hub, 'status', 'Speaking'), patch.object(hub, 'sight', None):
            self.assertFalse(brain.interrupt('desk', 'Hold on.'))
            self.assertFalse(brain.interrupt('other', 'Wait.'))
            self.assertTrue(brain.interrupt('desk', 'Wait. The blue one is mine.'))
            self.assertTrue(brain.hush.is_set())
            self.assertTrue(brain.addressed('The blue one is mine.', 'desk', time.monotonic()))
            self.assertFalse(brain.addressed('The blue one is mine.', 'other', time.monotonic()))

    def test_natural_barge_in_rejects_echo_and_accepts_a_correction(self):
        brain = Brain()
        brain.busy.set()
        brain.active_source = 'desk'
        brain.spoken_reply = 'The white one is on the shelf.'
        with patch.object(hub, 'status', 'Speaking'), patch.object(hub, 'sight', None):
            self.assertFalse(brain.interrupt('desk', 'The white one is on the shelf.'))
            self.assertTrue(brain.interrupt('desk', 'Actually the blue one is mine.'))

    def test_barge_in_rejects_inexact_playback_transcript(self):
        brain = Brain()
        brain.busy.set()
        brain.active_source = 'desk'
        brain.spoken_reply = "Actually, the blue mug I mentioned earlier is still on your bookshelf."
        with patch.object(hub, 'status', 'Speaking'), patch.object(hub, 'sight', None):
            self.assertFalse(brain.interrupt('desk', 'Actually, that blue mug is still on your book shelf.'))
            self.assertTrue(brain.interrupt('desk', 'Actually, could we change the subject?'))

    def test_barge_in_handles_delayed_playback_with_quiet_tail(self):
        from core import voice

        rng = np.random.default_rng(27)
        reference = rng.normal(0, 4000, 24000)
        echo = np.repeat(reference[4000:6200], 4) / 32000
        silence = np.zeros(2880)
        with patch.object(voice, 'echo_reference', (40., reference)):
            self.assertTrue(voice.is_echo(np.r_[echo, silence], 43.1))
            self.assertFalse(voice.is_echo(rng.normal(0, .15, 12000), 43.1))

    def test_interrupted_utterance_waits_for_reply_cleanup(self):
        import threading
        from core.hearing import Clip

        brain = Brain()
        hearing = Hearing()
        brain.interruption = True
        brain.busy.set()
        now = time.monotonic()
        clip = Clip('final', 'desk', brain.revision, now, now-2, now-1, np.zeros(100), interrupted=True)
        worker = threading.Timer(.08, brain.busy.clear)
        worker.start()
        with patch.object(hub, 'brain', brain), patch.object(hub, 'accepts', return_value=True):
            self.assertTrue(hearing.wait_for_interrupted_reply(clip))
            brain.revision += 1
            self.assertFalse(hearing.wait_for_interrupted_reply(clip))
        worker.join()

    def test_generated_routine_resolves_vision_and_rejects_unknown_actions(self):
        from core import skills

        bow = {'op': 'gesture', 'style': 'bow', 'seconds': 1.4, 'when': 'always'}
        chirp = {'op': 'chirp', 'style': 'tada', 'seconds': .6, 'when': 'person_visible'}
        self.assertEqual(len(skills.actions([bow, chirp], True)), 2)
        self.assertEqual(skills.actions([bow, chirp], None), [{k: v for k, v in bow.items() if k != 'when'}])
        with self.assertRaises(ValueError):
            skills.validate_plan([{**bow, 'style': 'raw pulse'}])

    def test_taught_phrase_mixes_speech_camera_and_authorized_motion_in_order(self):
        from core import skills

        brain = Brain()
        plan = [
            {'op': 'say', 'style': 'Good work.', 'seconds': 1., 'when': 'always'},
            {'op': 'look', 'style': 'finger', 'seconds': 2., 'when': 'always'},
            {'op': 'gesture', 'style': 'bow', 'seconds': 1., 'when': 'always'},
        ]
        output = []
        robot = SimpleNamespace(shows=True, motion_enabled=True, show_active=False,
                                show=lambda *args: output.append(('gesture', args[3][0]['style'])) or {'state': 'queued'},
                                show_status=lambda _: 'finished')
        with (patch.object(hub, 'get', return_value=robot), patch.object(hub, 'sight', None),
              patch.object(skills, 'draft', return_value=(plan, '')) as draft,
              patch.object(skills, 'save') as save, patch.object(voice, 'say', side_effect=lambda *a, **k: output.append(('say', a[0]))),
              patch.object(brain, 'skill_look', side_effect=lambda *a: output.append(('look', a[0])))):
            self.assertEqual(brain.reply('When I say victory lap, say good work, follow my finger, then bow.', 'desk'),
                             'Here goes victory lap.')
            draft.assert_called_once_with('say good work, follow my finger, then bow.')
            save.assert_called_once_with('victory lap', plan)
            brain.start_performance()
        self.assertEqual(output, [('say', 'Good work.'), ('look', 'finger'), ('gesture', 'bow')])

    def test_context_memory_keeps_preferences_and_drops_inference_and_sensitive_data(self):
        owner = 'Natural memory fixture'
        try:
            self.assertTrue(memory.remember_in_context(owner, 'I prefer black coffee.', 'I prefer black coffee'))
            self.assertFalse(memory.remember_in_context(owner, 'I am eating dinner.', 'I like steak'))
            self.assertFalse(memory.remember_in_context(owner, 'I prefer this password.', 'I prefer this password'))
            self.assertFalse(memory.remember_in_context(owner, 'I prefer black coffee.', 'I prefer lemon green tea'))
            self.assertEqual(memory.relevant(owner, 'How do I take my coffee?'), ['I prefer black coffee'])
        finally:
            memory.forget(owner, 'everything')

    def test_missing_personal_context_does_not_reach_the_model(self):
        brain = Brain()
        with patch('core.brain.llm.chat') as generate, patch.object(hub, 'sight', None):
            self.assertIn('don’t have that in my memory', brain.reply('What did I tell you yesterday about my dog?', 'desk'))
            self.assertIn('can’t see the camera', brain.reply('Who is standing behind me?', 'desk'))
            self.assertIn('didn’t move', brain.reply('Did you put that package in the closet?', 'desk'))
            generate.assert_not_called()

    def test_saved_notes_are_voice_scoped(self):
        brain = Brain()
        try:
            brain.speaker = 'Local memory fixture'
            brain.speaker_evidence = 'introduced'   # just saying a name doesn't unlock someone's memories
            self.assertIn('recognize', brain.reply('Remember that my bike is lime green.', 'desk'))
            brain.speaker_evidence = 'voice'
            self.assertEqual(brain.reply('Remember that my bike is lime green.', 'desk'), 'I’ll remember that.')
            self.assertIn('lime green', brain.reply('What do you remember?', 'desk'))
            self.assertFalse(memory.recall('Someone else'))
            self.assertEqual(brain.reply('Forget my bike.', 'desk'), 'I’ve forgotten it.')
            self.assertFalse(memory.recall(brain.speaker))
        finally:
            memory.forget('Local memory fixture', 'everything')

    def test_conversations_become_memories_that_come_back_later(self):
        brain = Brain()
        owner = 'Memory fixture'
        try:
            history = brain.history.setdefault(('desk', None), __import__('collections').deque(maxlen=24))
            history.extend([{'role': 'user', 'content': 'My exam is on Thursday.'}, {'role': 'assistant', 'content': 'Good luck.'},
                            {'role': 'user', 'content': 'It is about solid state transformers.'},
                            {'role': 'assistant', 'content': 'Fancy.'}])
            brain.history_owner[('desk', None)] = owner
            brain.history_at[('desk', None)] = 0.
            with patch('core.brain.llm.chat', return_value='Memory fixture has an exam on Thursday about solid state transformers.') as model:
                brain.reflect(now=1000.)
                brain.reflect(now=2000.)   # nothing new, nothing rewritten
            model.assert_called_once()
            memory.remember(owner, 'I prefer black coffee')
            context = memory.context(owner, 'When is my exam again?')
            self.assertIn('black coffee', context)
            self.assertIn('exam on Thursday', context)
            self.assertIn('Earlier conversations', context)
            self.assertIsNone(memory.context(None, 'anything'))
        finally:
            memory.forget(owner, 'everything')
            for row in memory.everything():
                if row['about'] == owner:
                    memory.delete(row['id'])

    def test_first_turn_distinguishes_face_and_voice_evidence(self):
        brain = Brain()
        self.assertTrue(brain.submit('Hey BITCH, do you recognize my voice?', 'desk', face='Sample'))
        self.assertEqual(brain.reply('Do you recognize my voice?', 'desk'),
                         'I see you, Sample. Give me another sentence to check your voice.')
        brain.busy.clear()
        brain.requests.get_nowait()
        brain.followup = ('desk', time.monotonic() + 3)
        self.assertTrue(brain.submit('Can you recognize my voice now?', 'desk', name='Sample'))
        self.assertEqual(brain.reply('Can you recognize my voice now?', 'desk'),
                         'Yeah, I recognize your voice, Sample.')

    def test_bluetooth_preroll_preserves_the_entire_audible_cue(self):
        pcm = np.asarray([20, 10, -30, -40], dtype='<i2').tobytes()
        with patch.object(config, 'get', return_value=50):
            audio, duration = voice.preroll(pcm)
        self.assertGreaterEqual(duration, 2.)
        self.assertTrue(any(audio[:-len(pcm)]))
        self.assertEqual(audio[-len(pcm):], pcm)

    def test_syllable_track_leaves_spaces_and_gives_signed_nod(self):
        signal = np.zeros(voice.SR, dtype='<i2')
        signal[4800:7680] = (np.sin(np.arange(2880)*.08) * 6000).astype('<i2')
        signal[14400:17280] = signal[4800:7680]
        energy, nods = voice.motion_track(signal.tobytes())
        self.assertEqual(energy[0], 0)
        self.assertEqual(energy[-1], 0)
        self.assertGreater(energy.max(), .7)
        self.assertGreater(nods.max(), .5)
        self.assertLess(nods.min(), -.7)

    def test_voice_keeps_a_valid_mono_wav_and_intelligible_speed(self):
        samples = np.sin(2 * np.pi * 440 * np.arange(24000) / 24000)
        with patch.object(voice, 'synthesize', return_value=(samples, 24000)):
            data, duration = voice.render('Hello.')
        self.assertAlmostEqual(duration, 1 / 1.035, places=3)
        with wave.open(io.BytesIO(data), 'rb') as audio:
            self.assertEqual((audio.getnchannels(), audio.getframerate(), audio.getsampwidth()), (1, 24000, 2))


class AlignmentTests(unittest.TestCase):
    def test_alignment_supports_camera_offset_yaw_pitch_and_legacy_profiles(self):
        self.assertAlmostEqual(aim_at(0.5, 0.5, [[0.2, -0.35], [0.6, 0.0], [0.8, 0.43]])[0], -0.0875)
        self.assertAlmostEqual(aim_at(0.6, 0.5, [[0.8, -0.35], [0.5, 0.0], [0.2, 0.43]])[0], -0.116667, places=5)
        self.assertEqual(aim_at(0.5, 0.3, [[0.2, -0.3, 0.4, -0.7], [0.5, -0.09, 0.3, -0.65],
                                           [0.75, 0.2, 0.35, -0.69]]), (-0.09, -0.65))

    def test_alignment_requires_settled_aim_and_saves_tracking_with_points(self):
        sight = Sight()
        camera = Device('testcam', 'Camera', ('eyes',))
        with patch.object(hub, 'selected', return_value=camera), patch.object(hub, 'get', return_value=SimpleNamespace(online=True)), patch.object(config, 'update') as save:
            sight.calibration('start', 'desk')
            with self.assertRaisesRegex(ValueError, 'still moving'):
                sight.calibration('sample', 'desk', 0)
            sight.target_source = camera.id
            for x, base, pitch in [(0.25, -0.3, -0.62), (0.50, 0, -0.7), (0.75, 0.32, -0.82)]:
                sight.calibration('aim', 'desk', base, pitch)
                sight.setup['settled'] = True
                now = time.monotonic()
                sight.sample_window.clear()
                sight.sample_window.extend((x, 0.4, now) for _ in range(4))
                sight.calibration('sample', 'desk', base, pitch)
                if base == -0.3:
                    with self.assertRaisesRegex(ValueError, 'Move sideways'):
                        sight.calibration('sample', 'desk', base, pitch)
            result = sight.calibration('save', 'desk', track=True)
            self.assertEqual(result[0], [0.25, -0.3, 0.4, -0.62])
            self.assertTrue(save.call_args.args[0]['idle'])

    def test_attention_holds_through_brief_occlusion_and_does_not_chase_largest_box(self):
        tracker = Attention()
        left = (0.2, 0.3, 0.15, 0.24)
        right = (0.65, 0.25, 0.21, 0.31)
        self.assertIsNone(tracker.update([left], 0)[1])
        self.assertEqual(tracker.update([left], 0.3)[1], 1)
        self.assertEqual(tracker.update([right], 0.5)[1], 1)
        self.assertEqual(tracker.update([left, right], 0.8)[1], 1)
        for time_stamp in np.arange(1.0, 8.31, 0.2):
            tracker.update([left, right], time_stamp)
        self.assertEqual(tracker.update([left, right], 8.5)[1], 2)

    def test_body_tracking_preserves_identity_when_looking_away_or_covering_face(self):
        from core.people import people
        from core.pointing import pose_bodies

        landmarks = [SimpleNamespace(x=0.5, y=0.5, visibility=0.0, presence=0.0) for _ in range(33)]
        landmarks[11] = SimpleNamespace(x=0.38, y=0.62, visibility=0.92, presence=0.95)
        landmarks[12] = SimpleNamespace(x=0.62, y=0.62, visibility=0.92, presence=0.95)
        bodies = pose_bodies([landmarks])
        self.assertEqual(len(bodies), 1)

        sight = Sight()
        sight.target_source = 'mac-camera'
        face_box = (0.42, 0.26, 0.16, 0.22)
        sight.attention.update([face_box], 10.0, bodies)
        visible, focus = sight.attention.update([face_box], 10.35, bodies)
        sight.attention.people[focus]['name'] = 'Brandon'
        sight.attention.people[focus]['confirmed_at'] = 10.35
        sight.attention.people[focus]['face_confirmed_at'] = 10.35

        # Face disappears for 20 seconds (looking away / face covered), while shoulders shift slightly.
        for step in range(1, 40):
            now = 10.0 + step * 0.5
            landmarks[11].x = 0.40
            landmarks[12].x = 0.64
            moved_bodies = pose_bodies([landmarks])
            visible, focus = sight.attention.update([], now, moved_bodies)
            sight.attention.people[focus]['confirmed_at'] = now
        self.assertEqual(focus, 1)
        self.assertEqual(visible[1]['name'], 'Brandon')

        sight.frame = (29.5, 1, b'jpg', {
            'size': [640, 480],
            'people': [{'id': 1, 'box': list(visible[1]['box']), 'name': 'Brandon'}],
            'focus': 1,
        })
        with (patch.object(hub, 'accepts', return_value=True),
              patch.object(people, 'names', side_effect=lambda kind: {'Brandon'} if kind == 'face' else set()),
              patch('core.sight.time.monotonic', return_value=29.6)):
            self.assertEqual(sight.solo_identity(), ('Brandon', 'body'))
            self.assertEqual(sight.solo_recognized(), 'Brandon')
            self.assertEqual(sight.in_view(), {'names': ['Brandon'], 'others': 0})

        brain = Brain()
        self.assertTrue(brain.submit('Hey BITCH, do you recognize me?', 'desk', face='Brandon', face_by='body'))
        self.assertIn('still tracking you, Brandon', brain.reply('Do you recognize me?', 'desk'))


class MotionTests(unittest.TestCase):
    def test_attention_leads_with_the_neck_and_eases_into_a_turn(self):
        desk = SimpleNamespace(cur=list(pi.REST), on=[True] * 4, BASE_SPEED=1.0,
                               S=[SimpleNamespace(us=lambda _: None) for _ in range(4)])
        motion = pi.Motion(desk)
        motion.started = True
        bases, necks = [], []
        for tick in range(100):
            motion.step('watch', 0.23, -0.62, tick * 0.02, 0.02)
            bases.append(desk.cur[0])
            necks.append(desk.cur[2])
        self.assertEqual(bases[4], 0)
        self.assertGreater(necks[4], pi.REST[2])
        velocity = np.diff([0, *bases]) / 0.02
        self.assertLessEqual(max(velocity), 0.581)
        self.assertLessEqual(max(abs(np.diff(velocity[:35]))), 0.032)
        self.assertAlmostEqual(bases[45], 0.23, delta=0.03)
        self.assertAlmostEqual(bases[-1], 0.23, delta=0.009)
        motion.step('watch', 0.42, -0.62, 2, 0.02)
        self.assertGreater(desk.cur[0], bases[-1])


    def test_reply_accent_keeps_gaze_separate_and_moves_the_body(self):
        def performance(voice_accent):
            desk = SimpleNamespace(cur=list(pi.REST), on=[True] * 4, BASE_SPEED=1.,
                                   S=[SimpleNamespace(us=lambda _: None) for _ in range(4)])
            motion = pi.Motion(desk)
            motion.started = True
            for tick in range(90):
                motion.step('speak-gentle', 0., -.67, tick * .02, .02, .65, voice_accent)
            return desk.cur

        raised = performance(.12)
        dipped = performance(-.12)
        self.assertGreater(raised[2] - dipped[2], .12)
        self.assertGreater(abs(raised[3] - dipped[3]), .08)
        self.assertGreater(abs(raised[1] - dipped[1]), .02)
        self.assertLess(abs(raised[0] - dipped[0]), .002)


    def test_word_gestures_tilt_and_lean_the_head_while_talking(self):
        def talk(gesture):
            desk = SimpleNamespace(cur=list(pi.REST), on=[True] * 4, BASE_SPEED=1.,
                                   S=[SimpleNamespace(us=lambda _: None) for _ in range(4)])
            motion = pi.Motion(desk)
            motion.started = True
            for tick in range(60):
                motion.step('speaking', 0., -.67, tick * .02, .02, .5, 0., None, gesture)
            return desk.cur

        still, tilted, leaning = talk((0., 0.)), talk((.3, 0.)), talk((0., .15))
        self.assertGreater(abs(tilted[3] - still[3]), .15)
        self.assertGreater(leaning[1] - still[1], .05)
        self.assertLess(abs(tilted[0] - still[0]), .01)

    def test_spoken_words_become_timed_gestures(self):
        events = voice.gestures([('No', .1), (',', .3), ('do', .5), ('you', .7), ('?', 1.), ('Yeah', 1.4), ('.', 1.7)])
        kinds = [(kind, round(at, 1)) for kind, at, _, _ in events]
        self.assertIn(('shake', .1), kinds)
        self.assertIn(('lean', .7), kinds)
        self.assertIn(('nod', 1.4), kinds)
        self.assertTrue(any(kind == 'tilt' for kind, _ in kinds))
        nod, tilt, lean = voice.gesture_at(events, 1.6)
        self.assertLess(nod, 0)


    def test_calm_watching_rests_the_base_and_writes_only_real_changes(self):
        writes = [0, 0, 0, 0]
        desk = SimpleNamespace(cur=list(pi.REST), on=[True] * 4, BASE_SPEED=1.,
                               S=[SimpleNamespace(us=lambda _, i=i: writes.__setitem__(i, writes[i] + 1)) for i in range(4)],
                               relax=lambda *joints: None)
        motion = pi.Motion(desk)
        motion.started = True
        for tick in range(500):   # ten seconds of watching someone who sits still
            motion.step('watch', .1, -.7, tick * .02, .02, calm=True)
        self.assertIn(0, motion.actor.limp)            # the base is resting unpowered
        self.assertLess(max(writes), 250)              # far fewer than 50 writes a second
        self.assertGreater(motion.wear['powered'], 9)
        busy = [0, 0, 0, 0]
        desk.S = [SimpleNamespace(us=lambda _, i=i: busy.__setitem__(i, busy[i] + 1)) for i in range(4)]
        motion = pi.Motion(desk)
        motion.started = True
        for tick in range(500):
            motion.step('watch', .1, -.7, tick * .02, .02)
        self.assertGreater(sum(busy), sum(writes))      # without calm the breathing keeps it moving

    def test_rhythm_rests_when_alone_and_sleeps_at_night(self):
        from core.rhythm import Rhythm

        rhythm = Rhythm()
        with patch.object(rhythm, 'night', return_value=False):
            self.assertEqual(rhythm.update(True, False, 0., now=1000.)[0], 'engaged')   # just arrived
            self.assertEqual(rhythm.update(True, False, 0., now=1030.)[0], 'attentive')
            self.assertEqual(rhythm.update(False, False, 0., now=1040.)[0], 'looking')
            self.assertEqual(rhythm.update(False, False, 0., now=1100.)[0], 'resting')
            self.assertEqual(rhythm.update(False, True, 1100., now=1101.)[0], 'engaged')
        with patch.object(rhythm, 'night', return_value=True):
            state, beat = rhythm.update(False, False, 1100., now=1500.)
            self.assertEqual((state, beat), ('asleep', 'doze'))
            self.assertEqual(rhythm.update(False, False, 1100., now=1510.), ('asleep', None))

    def test_sound_effects_render_for_every_action(self):
        for name, make in voice.EARCONS.items():
            sound = make()
            self.assertTrue(.1 < len(sound) / voice.SR < 1.5, name)
            self.assertLess(float(np.abs(sound).max()), .5, name)

    def test_procedural_pixar_motion_is_non_repeating_bounded_and_wear_safe(self):
        import character
        import random

        # Two procedural syntheses of the same gesture must vary organically rather than replaying identical frames.
        p1 = character.synthesize_phrase('curious', random.Random(1), side=1, mood={'valence': 0.5, 'arousal': 0.6})
        p2 = character.synthesize_phrase('curious', random.Random(2), side=1, mood={'valence': 0.5, 'arousal': 0.6})
        self.assertNotEqual(p1.keyframes, p2.keyframes)

        sfx_log = []
        relaxed = []
        desk = SimpleNamespace(
            cur=list(pi.REST), on=[True] * 4, BASE_SPEED=1.0,
            S=[SimpleNamespace(us=lambda _: None) for _ in range(4)],
            relax=lambda *joints: relaxed.append(joints),
        )
        motion = pi.Motion(desk, on_sfx=lambda name, vol: sfx_log.append((name, vol)))
        motion.started = True
        modes = [
            ('watch', 0.25, -0.65, {'valence': 0.6, 'arousal': 0.7, 'loneliness': 0.0, 'annoyance': 0.0}, False),
            ('victory', 0.0, -0.65, {'valence': 0.9, 'arousal': 0.9, 'loneliness': 0.0, 'annoyance': 0.0}, False),
            ('lonely', -0.1, -0.75, {'valence': -0.4, 'arousal': 0.2, 'loneliness': 0.85, 'annoyance': 0.0}, False),
            ('grumpy', 0.1, -0.72, {'valence': -0.2, 'arousal': 0.6, 'loneliness': 0.0, 'annoyance': 0.8}, False),
            ('watch', 0.1, -0.72, {'valence': 0.1, 'arousal': 0.3, 'loneliness': 0.0, 'annoyance': 0.0}, True),
            ('park', 0.0, -0.80, None, False),
        ]
        now = 0.0
        for mode, base, neck, mood, calm in modes:
            ticks = 450 if calm else 180
            for _ in range(ticks):
                motion.step(mode, base, neck, now, 0.02, calm=calm, mood=mood, sfx_volume=0.8)
                now += 0.02
                self.assertTrue(all(np.isfinite(desk.cur)))
                self.assertLessEqual(abs(desk.cur[0]), pi.BASE + 0.01)
                self.assertGreaterEqual(desk.cur[1], -1.07)
                self.assertLessEqual(desk.cur[1], -0.41)
                self.assertGreaterEqual(desk.cur[2], pi.NECK[0] - 0.01)
                self.assertLessEqual(desk.cur[2], pi.NECK[1] + 0.01)
                self.assertLessEqual(abs(desk.cur[3]), 0.65)
            if calm:
                # Calm watching detaches both the bearing base (0) and balanced head tilt (3).
                self.assertIn(0, motion.actor.limp)
                self.assertIn(3, motion.actor.limp)
        self.assertTrue(any(name == 'tada' for name, _ in sfx_log))
        self.assertTrue(any(name in ('sigh', 'whimper', 'sad') for name, _ in sfx_log))
        self.assertTrue(any(joints == () for joints in relaxed))   # full park detach

    def test_memory_consolidation_relationships_and_visual_callbacks(self):
        owner = 'ShirtDebateFixture'
        payload = json.dumps({
            'summary': 'ShirtDebateFixture debated blue vs white shirts and mentioned Sarah.',
            'facts': ['Prefers clean minimalist dress shirts'],
            'debates': [{'text': 'Argued white shirts are more professional than blue shirts',
                         'triggers': 'white shirt, blue shirt, shirt'}],
            'people': [{'name': 'Sarah', 'relation': "ShirtDebateFixture's mom"}],
            'status': 'ShirtDebateFixture is taking a nap right now',
            'status_hours': 2,
        })
        try:
            memory.consolidate_conversation(owner, 'Them: White shirts beat blue shirts.', chat_fn=lambda *a, **k: payload)
            hit = memory.visual_callbacks(owner, 'putting on a blue button-up shirt')
            self.assertIsNotNone(hit)
            self.assertIn('white shirts', hit['text'].lower())
            # Household context surfaces active statuses & known people even before an unknown visitor is enrolled.
            household = memory.household_context()
            self.assertIn('taking a nap', household)
            self.assertIn('Sarah', household)
            self.assertIsNone(memory.context(None, 'anything'))
            # Relationship introduction extracts both name and relation.
            brain = Brain()
            with patch.object(hub, 'sight', None), patch('core.people.people.learn'):
                answer, note = brain.meet("Hi, I'm Brandon's mom, Sarah.", 'desk')
            self.assertIsNone(answer)
            self.assertIn('Sarah', note)
            self.assertEqual(memory.person_info('Sarah')['relation'], 'Brandon’s mom')
        finally:
            memory.forget(owner, 'everything')
            for row in memory.everything():
                if row['about'] == owner:
                    memory.delete(row['id'])

    def test_nap_companion_alarm_and_notifications(self):
        from core import life as life_mod, notifications
        from core.rhythm import rhythm

        self.assertTrue(notifications.push('Messages', 'Mom', 'Dinner at 7!'))
        self.assertIn('Dinner at 7!', notifications.summary(limit=5))
        target = life_mod.parse_wake_time('wake me up in 45 minutes')
        self.assertIsNotNone(target)
        self.assertAlmostEqual(target - time.time(), 45 * 60, delta=5)

        mgr = life_mod.Life()
        with patch.object(life_mod.threading, 'Timer'):
            reply = mgr.handle_utterance("I'm going to take a nap for 2 hours", 'desk', 'NapFixture')
        self.assertIn('nap with you', reply)
        self.assertTrue(mgr.napping)
        self.assertTrue(any('nap' in text.lower() for _, text, *_ in memory.active_statuses('NapFixture')))

        mgr.nap_started_at = time.time() - 10
        notifications.push('Calendar', 'Design Review', 'Starts in 15 min')
        wake_msg = mgr.wake_from_nap('desk', 'NapFixture', trigger_briefing=False)
        self.assertFalse(mgr.napping)
        self.assertEqual(mgr.active_pose(), 'victory')
        self.assertIn('Design Review', wake_msg)
        self.assertFalse(memory.active_statuses('NapFixture'))
        rhythm.wake_now()

    def test_auto_sleep_detection_and_safe_wake_guard(self):
        from core import life as life_mod
        from core.rhythm import rhythm

        rhythm.wake_now()
        mgr = life_mod.Life()
        mgr.last_checkin_at = time.monotonic()   # even right after a check-in, auto-sleep must still work!
        fake_sight = SimpleNamespace(
            solo_recognized=lambda: 'AutoSleepFixture',
            current_scene=lambda: (b'frame', 1),
            people_snapshot=lambda: {
                'camera': 'cam', 'received': time.monotonic(), 'image': b'frame',
                'people': [{'id': 1, 'name': 'AutoSleepFixture', 'face_fresh': True, 'age': 10.0,
                            'center': (0.5, 0.5), 'box': (0.4, 0.3, 0.2, 0.2)}],
            },
        )
        fake_hearing = SimpleNamespace(ready=True, capture=None, activity_at=time.monotonic() - 120)
        fake_brain = SimpleNamespace(busy=threading.Event(), followup_seconds=lambda: 0.0, announce=Mock())
        with (patch.object(hub, 'sight', fake_sight), patch.object(hub, 'hearing', fake_hearing),
              patch.object(hub, 'brain', fake_brain), patch.object(config, 'get', side_effect=lambda k, d=None: True if k == 'muted' else d),
              patch.object(life_mod.gaze, 'observe_scene', return_value={'emotion': 'sleeping', 'activity': 'head down', 'notable': ''})):
            t0 = time.monotonic() + 100.0
            mgr._tick_observations(t0)
            self.assertFalse(mgr.napping)   # 1st sighting alone does not sleep
            mgr._tick_observations(t0 + 50.0)
            self.assertTrue(mgr.napping)
            self.assertTrue(mgr.auto_napping)
            self.assertEqual(rhythm.state, 'asleep')
            # Still sleeping 5 minutes later with face in view -> MUST NOT falsely wake up!
            mgr.nap_started_at = time.time() - 300
            mgr._tick_arrivals(t0 + 350.0)
            self.assertTrue(mgr.napping)
            fake_brain.announce.assert_not_called()
        # Once visibly awake, _tick_arrivals wakes Brandon and celebrates!
        with (patch.object(hub, 'sight', fake_sight), patch.object(hub, 'hearing', fake_hearing),
              patch.object(hub, 'brain', fake_brain),
              patch.object(life_mod.gaze, 'observe_scene', return_value={'emotion': 'neutral', 'activity': 'sitting up', 'notable': ''})):
            mgr._tick_arrivals(t0 + 420.0)
            self.assertFalse(mgr.napping)
            self.assertEqual(mgr.active_pose(), 'victory')
            fake_brain.announce.assert_called_once()
        memory.clear_status('AutoSleepFixture')
        rhythm.wake_now()

    def test_daily_school_schedule_reminders_and_alarm_dismissal(self):
        from datetime import datetime
        from core import life as life_mod
        from core.rhythm import rhythm

        owner = 'ScheduleFixture'
        mgr = life_mod.Life()
        try:
            r1 = mgr.handle_utterance('Wake me up at 8:00 AM to get ready for school', 'desk', owner)
            self.assertIn('8:00 AM', r1)
            self.assertIn('weekdays', r1)
            self.assertIn('to get ready for school', r1)

            r2 = mgr.handle_utterance('Wake me up tomorrow at 8:00 AM for school', 'desk', owner)
            self.assertIn('Alarm set for 8:00 AM for school', r2)
            self.assertEqual(len(memory.list_schedules(owner)), 1)   # tomorrow is one-off, not added to recurring table

            r3 = mgr.handle_utterance('Set an alarm for 7 AM', 'desk', owner)
            self.assertIn('7:00 AM', r3)
            self.assertNotIn('to 7 AM', r3)

            r4 = mgr.handle_utterance('Remind me at 4:30 PM about my dentist appointment', 'desk', owner)
            self.assertIn('about my dentist appointment', r4)
            self.assertIn('4:30 PM', r4)

            r5 = mgr.handle_utterance('Remind me every weekday at 7:45 AM to grab my charger', 'desk', owner)
            self.assertIn('to grab my charger', r5)
            self.assertIn('mon,tue,wed,thu,fri', r5)

            listed_alarms = mgr.handle_utterance('What are my alarms?', 'desk', owner)
            self.assertIn('get ready for school', listed_alarms)
            self.assertNotIn('grab my charger', listed_alarms)
            listed_rems = mgr.handle_utterance('What are my reminders?', 'desk', owner)
            self.assertIn('grab my charger', listed_rems)
            self.assertIn('dentist appointment', listed_rems)
            self.assertIn('Known schedule', memory.context(owner, 'alarm'))

            # Saturday 8:00 AM -> weekday school alarm must NOT fire!
            fake_brain = SimpleNamespace(busy=threading.Event(), announce=Mock())
            mgr.alarm_ringing = False
            with patch.object(hub, 'brain', fake_brain):
                sat_8am = datetime(2026, 11, 28, 8, 0, 0).astimezone()   # Saturday
                mgr._tick_schedule(time.monotonic(), now_dt=sat_8am)
                self.assertFalse(mgr.alarm_ringing)
                # Monday 8:00 AM -> weekday school alarm FIRES!
                mon_8am = datetime(2026, 11, 30, 8, 0, 0).astimezone()   # Monday
                mgr._tick_schedule(time.monotonic(), now_dt=mon_8am)
                self.assertTrue(mgr.alarm_ringing)
                self.assertEqual(mgr.active_pose(), 'alarm')
                # Alarm ringing allows hands-free dismissal & snooze without wake word!
                with patch.object(life_mod, 'life', mgr), patch('core.brain.life', mgr):
                    b = Brain()
                    self.assertTrue(b.addressed("I'm up", 'desk', time.monotonic()))
                    self.assertTrue(b.addressed("Snooze 10 minutes", 'desk', time.monotonic()))
                    self.assertFalse(b.addressed("Random background chatter", 'desk', time.monotonic()))
                # Dismissing turns off alarm and celebrates.
                done = mgr.handle_utterance("I'm up", 'desk', owner)
                self.assertFalse(mgr.alarm_ringing)
                self.assertIn('Alarm off', done)
                # Monday 8:01 AM -> does NOT double-fire within 12h.
                mgr._tick_schedule(time.monotonic(), now_dt=datetime(2026, 11, 30, 8, 1, 0).astimezone())
                self.assertFalse(mgr.alarm_ringing)
                # Reminder while napping queues into pending_highlights for wake-up!
                mgr.napping = True
                mgr.nap_owner = owner
                mgr._tick_schedule(time.monotonic(), now_dt=datetime(2026, 11, 30, 7, 45, 0).astimezone())
                self.assertTrue(any('grab my charger' in h for h in mgr.pending_highlights))
                wake_msg = mgr.wake_from_nap('desk', owner, trigger_briefing=False)
                self.assertIn('grab my charger', wake_msg)

            canceled = mgr.handle_utterance('Cancel my 8:00 alarm', 'desk', owner)
            self.assertIn('Done', canceled)
        finally:
            memory.remove_schedule(owner, 'all')
            memory.clear_status(owner)
            rhythm.wake_now()

    def test_memory_aware_excited_notification_reactions(self):
        from core import life as life_mod, notifications
        from core.rhythm import rhythm

        owner = 'PackageFixture'
        rhythm.wake_now()
        with notifications.lock:
            notifications.pushed.clear()
        mgr = life_mod.Life()
        mgr.owner_name = owner
        mgr.started_at = mgr.notif_cursor = time.time() - 0.1
        fake_brain = SimpleNamespace(busy=threading.Event(), followup_seconds=lambda: 0.0, announce=Mock())
        try:
            memory.remember(owner, 'Ordered a lazy Susan bearing for the robot base', importance=4)
            # Promo spam must NOT match or interrupt!
            self.assertIsNone(memory.notification_match(owner, 'Amazon: 20% off Prime Day deals today!'))
            notifications.push('Amazon', 'Prime Day Deals', 'Save 20% on kitchen gadgets today!')
            with (patch.object(hub, 'brain', fake_brain), patch.object(mgr, 'primary_owner', return_value=owner)):
                mgr._tick_notifications(time.monotonic() + 20.0, force=True)
            fake_brain.announce.assert_not_called()

            # Real Amazon delivery notification matches the remembered order and triggers excited victory reaction!
            hit = memory.notification_match(owner, 'Amazon: Your package has been delivered!')
            self.assertIsNotNone(hit)
            self.assertIn('lazy susan', hit['text'].lower())
            notifications.push('Amazon', 'Delivered', 'Your package was delivered to your front door!')
            with (patch.object(hub, 'brain', fake_brain), patch.object(mgr, 'primary_owner', return_value=owner),
                  patch.object(life_mod.llm, 'chat', return_value='Ooh PackageFixture, your lazy Susan bearing just arrived!')):
                mgr._tick_notifications(time.monotonic() + 40.0, force=True)
            fake_brain.announce.assert_called_once()
            self.assertIn('lazy Susan', fake_brain.announce.call_args.args[1])
            self.assertEqual(mgr.active_pose(), 'victory')
            # Second poll deduplicates the same notification.
            with (patch.object(hub, 'brain', fake_brain), patch.object(mgr, 'primary_owner', return_value=owner)):
                mgr._tick_notifications(time.monotonic() + 60.0, force=True)
            self.assertEqual(fake_brain.announce.call_count, 1)

            # Delivery while sleeping queues for wake-up briefing!
            mgr.napping = True
            mgr.nap_owner = owner
            notifications.push('UPS', 'Package Arrived', 'Delivered at front porch')
            with (patch.object(hub, 'brain', fake_brain), patch.object(mgr, 'primary_owner', return_value=owner)):
                mgr._tick_notifications(time.monotonic() + 80.0, force=True)
            self.assertEqual(len(mgr.pending_highlights), 1)
            wake_msg = mgr.wake_from_nap('desk', owner, trigger_briefing=False)
            self.assertIn('UPS', wake_msg)
        finally:
            memory.forget(owner, 'everything')
            rhythm.wake_now()

    def test_multimodal_arrivals_mom_during_nap_and_group_greetings(self):
        from core import life as life_mod
        from core.rhythm import rhythm

        rhythm.wake_now()
        mgr = life_mod.Life()
        mgr.owner_name = 'Brandon'
        mgr.napping = True
        mgr.nap_owner = 'Brandon'
        mgr.nap_started_at = time.time() - 30
        memory.set_status('Brandon', 'Brandon is taking a nap right now and should be up around 4:00 PM', ttl_seconds=3600, tag='nap')
        fake_brain = SimpleNamespace(busy=threading.Event(), followup_seconds=lambda: 0.0, announce=Mock())
        fake_hearing = SimpleNamespace(ready=True, capture=None, activity_at=0.0)
        fake_sight = SimpleNamespace(
            direct=Mock(return_value='accepted'),
            people_snapshot=lambda: {
                'camera': 'cam', 'received': time.monotonic(), 'image': b'jpg',
                'people': [{'id': 77, 'name': None, 'face_fresh': True, 'age': 3.5,
                            'center': (0.6, 0.4), 'box': (0.5, 0.3, 0.2, 0.2)}],
            },
        )
        try:
            with (patch.object(hub, 'sight', fake_sight), patch.object(hub, 'brain', fake_brain),
                  patch.object(hub, 'hearing', fake_hearing), patch.object(hub, 'selected', return_value=SimpleNamespace(id='desk')),
                  patch.object(config, 'get', side_effect=lambda k, d=None: {'check_ins': True, 'muted': False, 'volume': 80}.get(k, d))):
                mgr._tick_arrivals(time.monotonic() + 50.0)
            fake_brain.announce.assert_called_once()
            announced = fake_brain.announce.call_args.args[1]
            self.assertIn('taking a nap', announced)
            self.assertIn('who are you', announced.lower())
            self.assertIsNotNone(mgr.awaiting_intro)

            # Mom answers "I'm his mom, Sarah" -> enrolls Sarah with relation "Brandon’s mom"!
            b = Brain()
            with patch.object(hub, 'sight', None), patch('core.people.people.learn'), patch('core.brain.life', mgr):
                answer, note = b.meet("I'm his mom, Sarah", 'desk')
            self.assertIsNone(answer)
            self.assertIn('Sarah', note)
            self.assertEqual(memory.person_info('Sarah')['relation'], 'Brandon’s mom')

            # Awake group arrival (1 known + 2 unknown) -> greets known, describes unknown, asks relation.
            mgr.napping = False
            mgr.last_checkin_at = 0.0
            fake_brain.announce.reset_mock()
            group_sight = SimpleNamespace(
                direct=Mock(return_value='accepted'),
                people_snapshot=lambda: {
                    'camera': 'cam', 'received': time.monotonic(), 'image': b'jpg',
                    'people': [
                        {'id': 1, 'name': 'Brandon', 'face_fresh': True, 'age': 4.0, 'center': (0.2, 0.4), 'box': (0.1, 0.3, 0.15, 0.2)},
                        {'id': 2, 'name': None, 'face_fresh': True, 'age': 4.0, 'center': (0.5, 0.4), 'box': (0.4, 0.3, 0.22, 0.2)},
                        {'id': 3, 'name': None, 'face_fresh': True, 'age': 4.0, 'center': (0.8, 0.4), 'box': (0.7, 0.3, 0.16, 0.2)},
                    ],
                },
            )
            with (patch.object(hub, 'sight', group_sight), patch.object(hub, 'brain', fake_brain),
                  patch.object(hub, 'hearing', fake_hearing), patch.object(hub, 'selected', return_value=SimpleNamespace(id='desk')),
                  patch.object(life_mod.gaze, 'describe_person', return_value='in the red hoodie'),
                  patch.object(config, 'get', side_effect=lambda k, d=None: {'check_ins': True, 'muted': False, 'volume': 80}.get(k, d))):
                mgr._tick_arrivals(time.monotonic() + 200.0)
            fake_brain.announce.assert_called_once()
            line = fake_brain.announce.call_args.args[1]
            self.assertIn('Hey Brandon!', line)
            self.assertIn('in the red hoodie', line)
            self.assertIn('how do you know Brandon', line)
        finally:
            memory.clear_status('Brandon')
            rhythm.wake_now()


class CharacterTests(unittest.TestCase):
    def test_idle_gesture_moves_distinct_joints_and_returns_to_the_gaze(self):
        desk = SimpleNamespace(cur=list(pi.REST), on=[True] * 4, BASE_SPEED=1.0,
                               S=[SimpleNamespace(us=lambda _: None) for _ in range(4)])
        motion = pi.Motion(desk)
        motion.started = True
        motion.mode = 'watch'
        motion.next_beat = 99.0
        motion.idle_beat = (pi.BEATS['curious'], 0.0, 1)
        poses = []
        for index in range(165):
            motion.step('watch', 0.0, -0.8, index * 0.02, 0.02)
            poses.append(desk.cur[:])
        self.assertLess(min(pose[1] for pose in poses), -0.807)
        self.assertGreater(max(pose[1] for pose in poses), -0.783)
        self.assertGreater(max(pose[3] for pose in poses), 0.06)
        self.assertAlmostEqual(poses[-1][3], 0, delta=.008)

    def test_an_idle_glance_does_not_cancel_itself(self):
        desk = SimpleNamespace(cur=list(pi.REST), on=[True] * 4, BASE_SPEED=1.0,
                               S=[SimpleNamespace(us=lambda _: None) for _ in range(4)])
        motion = pi.Motion(desk)
        motion.started = True
        motion.mode = 'watch'
        motion.next_beat = 99.0
        motion.idle_beat = (pi.BEATS['glance'], 0.0, 1)
        motion.idle_anchor = (0.0, -0.8)
        for index in range(61):
            motion.step('watch', 0.0, -0.8, index * 0.02, 0.02)
        self.assertGreater(desk.cur[0], 0.1)
        self.assertIsNotNone(motion.idle_beat)
        motion.step('watch', 0.30, -0.8, 1.23, 0.02)
        self.assertIsNone(motion.idle_beat)

    def test_profiles_match_confidently_and_allow_forgetting(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config, 'HOME', Path(directory)):
            profiles = People()
            profiles.save({'First': {'voice': [[1.0, 0.0]]}, 'Second': {'voice': [[0.0, 1.0]]}})
            self.assertEqual(profiles.match('voice', [.99, .01]), 'First')
            self.assertIsNone(profiles.match('voice', [.71, .70]))
            self.assertEqual(profiles.path.stat().st_mode & 0o777, 0o600)
            profiles.remove('First')
            self.assertIsNone(profiles.match('voice', [.99, .01]))


class JointAttentionTests(unittest.TestCase):
    def test_finger_follow_is_distinct_from_a_point_request(self):
        from core.pointing import index_tip
        self.assertEqual(gaze.request('Could you follow my finger around?'), ('follow', None))
        self.assertEqual(gaze.request('Stop following my finger.'), ('stop-follow', None))
        hand = [SimpleNamespace(x=.5, y=.6, z=0.) for _ in range(21)]
        for index, y in ((5, .54), (6, .48), (7, .41), (8, .33)):
            hand[index].y = y
        self.assertEqual(index_tip([hand]), (.5, .33))
        self.assertIsNone(index_tip([hand, hand]))

    def test_geometric_point_projects_beyond_the_hand(self):
        from core.pointing import pointing_region
        landmarks = [SimpleNamespace(x=0, y=0, visibility=0, presence=0) for _ in range(33)]
        for index, x, y in ((11, .27, .47), (13, .36, .44), (15, .46, .42), (19, .50, .41)):
            landmarks[index] = SimpleNamespace(x=x, y=y, visibility=.95, presence=.96)
        target = pointing_region(landmarks, 4/3)
        self.assertGreater(target[0], .61)
        self.assertLess(target[1], .42)
        landmarks[15].visibility = .2
        self.assertIsNone(pointing_region(landmarks, 4/3))

    def test_gestures_must_be_stable_near_the_addressed_speech(self):
        from core.pointing import Pointing
        pointer = Pointing()
        for timestamp in (10.0, 10.13, 10.26, 10.39):
            pointer.observe('wide', timestamp, (.71, .40, 1., 0.))
        self.assertEqual(pointer.during('wide', (9.8, 10.5), 12.3), (.71, .40))
        self.assertIsNone(pointer.during('other', (9.8, 10.5), 12.3))
        self.assertIsNone(pointer.during('wide', (13., 13.8), 14.))
        for timestamp in (10.60, 10.73, 10.86, 10.99):
            pointer.observe('wide', timestamp, (.25, .40, -1., 0.))
        self.assertIsNone(pointer.during('wide', (9.8, 11.0), 12.4))

    def test_addressed_gaze_reports_uncertainty_and_keeps_camera_provenance(self):
        brain = Brain()
        fake = SimpleNamespace(
            direction_scene=lambda: ('phone', 50., b'jpg', None, 1),
            pointing=SimpleNamespace(ready=True, during=lambda *args: (.7, .4)),
        )
        fake.direct = Mock(return_value='accepted')
        with patch.object(hub, 'sight', fake):
            self.assertTrue(brain.submit('Hey BITCH, look there.', 'mic', 48., speech_end=49.))
            self.assertEqual(brain.reply('Look there.', 'mic'), 'Right there?')
            fake.direct.assert_called_once_with('phone', 50., (.7, .4), 'point')
            with patch.object(gaze, 'locate', return_value=('several', None)):
                self.assertIn('Which one?', brain.reply('Look at the mug.', 'mic'))
            fake.direct.assert_called_once()
        self.assertEqual(gaze.request('Could you look towards the blue mug?'), ('object', 'the blue mug'))
        self.assertIsNone(gaze.request('What’s that?'))

    def test_object_coordinates_must_be_plausible(self):
        with patch.object(gaze.llm, 'chat', return_value='{"status":"one","box":[650,300,810,560]}'):
            self.assertEqual(gaze.locate(b'jpg', 'blue cup'), ('one', (.73, .43)))
        with patch.object(gaze.llm, 'chat', return_value='{"status":"one","box":[0,0,1000,1000]}'):
            self.assertEqual(gaze.locate(b'jpg', 'blue cup'), ('missing', None))


class InteractionTests(unittest.TestCase):
    def test_head_mirror_needs_sustained_tilt_and_rearms_on_neutral(self):
        gesture = HeadGesture()
        tilted = [0, 0, 0, 0, 8, 10, 30, 18]
        neutral = [0, 0, 0, 0, 8, 10, 30, 10]
        for now in (1, 1.1, 1.3):
            gesture.observe(3, tilted, now, True)
        self.assertIsNone(gesture.mode)
        gesture.observe(3, tilted, 1.5, True)
        self.assertEqual(gesture.mode, 'mirror-left')
        until = gesture.until
        gesture.observe(3, tilted, 15, True)
        self.assertEqual(until, gesture.until)
        gesture.observe(3, neutral, 15.2, True)
        gesture.observe(3, tilted, 16, False)
        gesture.observe(3, tilted, 16.5, False)
        self.assertEqual(gesture.mode, 'mirror-right')
        gesture.observe(4, None, 17)
        self.assertIsNone(gesture.mode)

    def test_mic_gate_and_turn_phase_through_mid_sentence_expiry(self):
        brain, hearing = Brain(), Hearing()
        hearing.whisper_ready = hearing.detector_ready = True
        now = time.monotonic()
        with patch.object(hub, 'brain', brain), patch.object(hub, 'accepts', return_value=True):
            brain.busy.set()
            hearing.feed('desk', np.zeros(512))
            # Kept as lead-in for the next turn rather than dropped.
            self.assertFalse(hearing.audio.empty())
            brain.busy.clear()
            brain.followup = ('desk', now-.1)
            hearing.capture = ('desk', brain.revision, now-1., now)
            self.assertEqual(hearing.phase(), 'Listening')
            hearing.capture = ('desk', brain.revision, now, now)
            self.assertEqual(hearing.phase(), 'Checking for name')
            brain.revision += 1
            self.assertIsNone(hearing.phase())

    def test_visual_questions_refresh_image_without_storing_frames(self):
        brain = Brain()
        with patch.object(hub, 'sight') as sight, patch('core.brain.llm.chat', return_value='A mug.') as chat:
            sight.current_scene.side_effect = [(b'one', 1), (b'two', 1), (None, None)]
            brain.reply('What am I holding?', 'desk')
            first = chat.call_args.args[0][-1]['images']
            brain.reply('And now?', 'desk')
            self.assertNotEqual(first, chat.call_args.args[0][-1]['images'])
            self.assertIn('camera', brain.reply('What do you see?', 'desk'))
            self.assertEqual(chat.call_count, 2)
            self.assertFalse(any('images' in item for turns in brain.history.values() for item in turns))


class ConnectionTests(unittest.TestCase):
    def test_pairs_over_https_and_rejects_unnamed_clients(self):
        from fastapi.testclient import TestClient
        from core.server import app

        with patch.object(hub, 'brain', Brain()), patch.object(hub, 'hearing', SimpleNamespace(ready=False)):
            with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as local:
                code = local.get('/api/pair-code').json()['code']
                self.assertEqual(local.post('/api/settings', json={'idle': False},
                                            headers={'Origin': 'https://elsewhere.invalid'}).status_code, 403)
            with TestClient(app, base_url='https://host.local:8781', client=('192.168.1.22', 50000)) as phone:
                self.assertEqual(phone.get('/api/state').status_code, 401)
                self.assertEqual(phone.post('/api/pair', json={'code': code}).status_code, 200)
                state = phone.get('/api/state')
                self.assertEqual(state.status_code, 200)
                self.assertNotIn('robot_key', state.json()['settings'])
                with phone.websocket_connect('wss://host.local:8781/link/' + 'a' * 32) as link:
                    link.send_text('ears,eyes')
                    self.assertTrue(link.receive_json()['device'].startswith('web-'))
                self.assertEqual(phone.post('/api/settings', json={'robot_key': 'someone'}).status_code, 400)

    def test_server_wake_notify_and_state_endpoints(self):
        from fastapi.testclient import TestClient
        from core.server import app
        from core import life as life_mod, notifications
        from core.rhythm import rhythm

        brain = Brain()
        memory.upsert_person('MomApiFixture', relation='Brandon’s mom')
        with patch.object(hub, 'brain', brain), patch.object(hub, 'hearing', SimpleNamespace(ready=False, activity_at=0.0)):
            with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as local:
                self.assertEqual(local.post('/api/notify', json={'app': 'Amazon', 'title': 'Delivered', 'body': 'At door'}).status_code, 200)
                self.assertEqual(local.post('/api/notify', json={'app': 'Empty'}).status_code, 400)
                self.assertIn('At door', notifications.summary(limit=5))
                rhythm.sleep_now()
                self.assertEqual(rhythm.state, 'asleep')
                self.assertEqual(local.post('/api/wake').status_code, 200)
                self.assertEqual(rhythm.state, 'engaged')
                st = local.get('/api/state').json()
                self.assertIn('life', st)
                self.assertIn('rhythm', st)
                self.assertFalse(st['life']['sleeping'])


class PiProtocolTests(unittest.TestCase):
    def test_desktop_chatgpt_url_and_prompt_helpers(self):
        import desktop_chatgpt as dc

        self.assertTrue(dc.is_home_address('chatgpt.com'))
        self.assertTrue(dc.is_home_address('https://chatgpt.com/?model=auto'))
        self.assertFalse(dc.is_home_address('https://chatgpt.com/c/12345678-1234-1234-1234-123456789abc'))
        self.assertEqual(
            dc.chat_url('chatgpt.com/c/12345678-1234-1234-1234-123456789abc/'),
            'https://chatgpt.com/c/12345678-1234-1234-1234-123456789abc',
        )
        self.assertIsNone(dc.chat_url('https://evil.example/c/12345678-1234-1234-1234-123456789abc'))
        prompt = macros.format_prompt('Give me a quick morning briefing for Tuesday.')
        self.assertTrue(dc.prompt_matches('Give me a quick morning briefing for Tuesday.', prompt))
        self.assertFalse(dc.prompt_matches('Unrelated chat message.', prompt))
        env = dc.desktop_env()
        self.assertEqual(env.get('ACCESSIBILITY_ENABLED'), '1')
        self.assertNotIn('NO_AT_BRIDGE', env)

    def test_chatgpt_macro_targets_only_the_new_chat_url(self):
        from desktop_chatgpt import matches_address

        self.assertIn('passing along their request on their behalf', macros.format_prompt('Find me a lamp.'))
        self.assertIn('\nFind me a lamp.\n', macros.format_prompt('Find me a lamp.'))
        self.assertTrue(matches_address('chatgpt.com/#bitch-draft-fixture', 'bitch-draft-fixture'))
        self.assertFalse(matches_address('https://chatgpt.com/c/existing#bitch-draft-fixture', 'bitch-draft-fixture'))
        self.assertFalse(matches_address('https://chatgpt.com.example/#bitch-draft-fixture', 'bitch-draft-fixture'))

    def test_automatic_handoff_sends_once_and_does_not_retry_an_uncertain_click(self):
        request = {'name': 'agent', 'id': '1' * 32, 'prompt': 'Test light.', 'auto_send': True}
        uncertain = {**request, 'id': '2' * 32}
        manual = {'name': 'agent', 'id': '3' * 32, 'prompt': 'Previous queued item.'}
        with (tempfile.TemporaryDirectory() as folder, patch.object(macros, 'QUEUE', Path(folder)),
              patch.object(macros.desktop_chatgpt, 'submit', return_value={'state': 'returned', 'answer': 'Good.'}) as send):
            for item in (request, uncertain, manual):
                macros.enqueue(item)
            path = macros.job_path(uncertain['id'])
            macros.write_job(path, {**macros.read_job(path.stem), 'state': 'sending'})
            macros.send_pending()
            macros.enqueue(request)
            macros.send_pending()
            send.assert_called_once_with(macros.format_prompt(request['prompt']))
            self.assertEqual(macros.handoff_status(request['id']), {'state': 'returned', 'answer': 'Good.'})
            self.assertEqual(macros.handoff_status(uncertain['id'])['state'], 'send_unverified')
            self.assertEqual(macros.handoff_status(manual['id'])['state'], 'queued')

    def test_paired_protocol_motion_idle_and_local_inbox(self):
        with tempfile.TemporaryDirectory() as folder, socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
            reservation.close()
            root = Path(__file__).resolve().parent.parent
            states = Path(folder) / 'servo.log'
            home = Path(folder) / 'pi'
            env = dict(os.environ, HOME=folder, BITCH_PI_HOME=str(home), FAKE_SERVO_LOG=str(states),
                       PYTHONPATH=str(root / 'testing' / 'fakes'))
            process = subprocess.Popen([sys.executable, '-c', f'import brain; brain.PORT={port}; brain.listen()'],
                                       cwd=root, env=env, stdout=subprocess.DEVNULL)
            def cleanup():
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

            self.addCleanup(cleanup)
            for _ in range(40):
                try:
                    with socket.create_connection(('127.0.0.1', port), 0.1):
                        break
                except OSError:
                    time.sleep(0.1)
            key = (home / 'robot.key').read_text()
            self.assertRegex(key, re.compile(r'^[a-zA-Z0-9_-]+$'))

            def request(path, data=None, paired=True):
                headers = {'Authorization': f'Bearer {key}'} if paired else {}
                req = urllib.request.Request(f'http://127.0.0.1:{port}' + path,
                                             data=json.dumps(data).encode() if data is not None else None, headers=headers)
                try:
                    with urllib.request.urlopen(req, timeout=2) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)

            self.assertEqual(request('/ping', paired=False)[0], 401)
            self.assertEqual(request('/ping')[1]['protocol'], 1)
            self.assertEqual(request('/ping')[1]['performance'], 3)
            self.assertTrue(request('/ping')[1]['joint_attention'])
            self.assertEqual(request('/look', {'mode': 'speak-upbeat', 'accent': 2})[0], 400)
            self.assertEqual(request('/ping')[1]['gestures'], 1)
            self.assertEqual(request('/look', {'mode': 'speaking', 'accent': .4, 'tilt': .3, 'lean': -.1})[0], 200)
            self.assertEqual(request('/look', {'mode': 'speaking', 'tilt': 2})[0], 400)
            self.assertEqual(request('/hear?text=dance')[0], 404)
            self.assertFalse(states.exists())
            self.assertEqual(request('/look', {'mode': 'watch', 'base': float('nan')})[0], 400)
            self.assertEqual(request('/look', {'mode': 'look-target', 'base': 9})[0], 200)
            time.sleep(2.7)
            self.assertAlmostEqual(request('/look', {'mode': 'park'})[1]['base'], 0.55, delta=0.02)
            self.assertTrue(states.exists())
            record = {'name': 'agent', 'id': 'b' * 32, 'prompt': 'Read the calendar.'}
            self.assertEqual(request('/macro', record)[0], 200)
            self.assertEqual(request('/macro', record)[0], 200)
            self.assertEqual(request('/macro', {**record, 'prompt': 'Different'})[0], 400)
            jobs = list((home / 'macros').glob('*.json'))
            self.assertEqual(len(jobs), 1)
            self.assertEqual(json.loads(jobs[0].read_text())['prompt'], record['prompt'])
            base = f'http://127.0.0.1:{port}'
            path = '/macro/status?id=' + record['id']
            self.assertEqual(request(path)[1]['state'], 'queued')
            self.assertEqual(request(path, paired=False)[0], 401)
            with urllib.request.urlopen(base + '/inbox', timeout=2) as response:
                self.assertIn(b'Read the calendar.', response.read())
            def inbox(action, origin, **extra):
                form = urlencode({'id': record['id'], 'action': action, **extra})
                req = urllib.request.Request(base + '/inbox', data=form.encode(), headers={'Origin': origin})
                try:
                    with urllib.request.urlopen(req, timeout=2) as response:
                        return response.status
                except urllib.error.HTTPError as error:
                    return error.code
            self.assertEqual(inbox('prepare', 'http://external.invalid'), 403)
            self.assertEqual(inbox('return', base, answer='Too early.'), 400)
            self.assertEqual(inbox('prepare', base), 200)
            self.assertEqual(request(path)[1]['state'], 'ready')
            self.assertEqual(inbox('return', base, answer='A test answer <with brackets>.'), 200)
            self.assertEqual(request(path)[1], {'state': 'returned', 'answer': 'A test answer <with brackets>.'})
            with urllib.request.urlopen(base + '/inbox', timeout=2) as response:
                self.assertIn(b'&lt;with brackets&gt;', response.read())
            self.assertEqual(inbox('return', base, answer='Duplicate.'), 400)
            show = {'name': 'routine', 'id': 'e' * 32, 'volume': 0,
                    'steps': [{'op': 'gesture', 'style': 'bow', 'seconds': 1.3}]}
            self.assertEqual(request('/show', show, paired=False)[0], 401)
            self.assertEqual(request('/show', {**show, 'steps': [{'op': 'shell'}]})[0], 400)
            self.assertEqual(request('/show', show)[1]['state'], 'queued')
            self.assertEqual(request('/show', show)[1]['state'], 'queued')
            after = time.monotonic() + 1.
            while time.monotonic() < after and request('/show')[1]['state'] == 'preparing':
                time.sleep(.02)
            self.assertEqual(request('/show')[1]['state'], 'playing')
            self.assertTrue(request('/ping')[1]['show_active'])
            self.assertEqual(request('/stop', {})[0], 200)
            self.assertEqual(request('/show')[1]['state'], 'stopped')
            self.assertFalse(request('/ping')[1]['show_active'])
            cleanup()

    def test_rejects_arbitrary_macro_names_and_paths(self):
        with self.assertRaises(ValueError):
            macros.enqueue({'id': 'a' * 32, 'name': 'shell', 'prompt': 'anything'})
        with self.assertRaises(ValueError):
            macros.job_path('../../settings')
