"""Deep sleep with the real models: how much memory it frees and how long waking takes.

    ~/dc-venv/bin/python -m testing.try_deep_sleep
"""
import os
import tempfile
import time

os.environ.setdefault('BITCH_HOME', tempfile.mkdtemp(prefix='bitch-sleep-'))

from core import config, llm, voice   # noqa: E402
from core.power import power, _level   # noqa: E402


def show(label):
    level, pressure = _level()
    rss = os.popen(f'ps -o rss= -p {llm.server.pid}').read().strip() if llm.server and llm.server.poll() is None else ''
    server = f'{int(rss) / 1048576:.1f} GB' if rss else 'stopped'
    print(f'{label:34s} memory free {level:3d}%  brain server {server}', flush=True)


config.update({'llm_server': True, 'voice_engine': 'qwen'})
started = time.time()
llm.start_server(wait=300)
llm.chat([{'role': 'user', 'content': 'Hi.'}], max_tokens=2)
voice.warm()
voice.warmed.wait(120)
show(f'awake (cold start {time.time() - started:.0f} s)')
power.sleep(0)
time.sleep(3)
show('deep sleep')
for attempt in (1, 2):
    started = time.time()
    woke = power.wake_with_words('test')
    print(f'wake {attempt}: {"awake" if woke else "FAILED"} in {time.time() - started:.1f} s '
          f'(estimate now {power.estimate():.0f} s)', flush=True)
    reply = llm.chat([{'role': 'user', 'content': 'Say hi in three words.'}], max_tokens=12)
    show(f'answering again: {reply!r}'[:34])
    if attempt == 1:
        power.sleep(0)
        time.sleep(3)
llm.stop_server()
