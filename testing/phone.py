"""A pretend phone for testing BITCH from the command line: it connects to /link like the app does.

  python testing/phone.py say "set a 2 minute timer"            type messages (one per argument)
  python testing/phone.py talk question.wav                     hold the mic button and say a 16 kHz WAV file
  python testing/phone.py listen 30                             just watch what BITCH does for 30 seconds
Options: --url ws://host:8780/link   --cookie <token> (for a paired phone over Wi-Fi)   --wait seconds
"""
import argparse, asyncio, json, time, wave
import websockets


async def main(a):
    headers = {'Cookie': 'bitch=' + a.cookie} if a.cookie else {}
    async with websockets.connect(a.url, max_size=None, additional_headers=headers) as ws:
        await ws.send(json.dumps({'type': 'hello', 'id': a.id, 'name': 'Test phone'}))
        if a.cmd == 'talk': await ws.send(json.dumps({'type': 'caps', 'caps': ['ears', 'voice']}))
        t0 = time.time()

        async def rx():
            async for m in ws:
                s = f'{time.time() - t0:5.1f}s'
                if isinstance(m, bytes): print(f'{s} << voice audio, {len(m)} bytes', flush=True); continue
                d = json.loads(m); t = d.get('type')
                if t == 'chat': print(f"{s} {d['msg']['role']}: {d['msg']['text']}", flush=True)
                elif t in ('status', 'perform', 'show', 'inventing', 'action', 'known'):
                    print(s, t, json.dumps({k: v for k, v in d.items() if k not in ('type', 'steps')})[:160], flush=True)
        task = asyncio.create_task(rx())
        await asyncio.sleep(1)
        if a.cmd == 'listen':
            await asyncio.sleep(float(a.items[0]) if a.items else 30)
        for item in a.items if a.cmd != 'listen' else []:
            if a.cmd == 'say':
                print(f'{time.time() - t0:5.1f}s >> {item}', flush=True)
                await ws.send(json.dumps({'type': 'say', 'text': item, 'speak': a.speak}))
            else:
                w = wave.open(item); pcm = w.readframes(w.getnframes()); w.close()
                await ws.send(json.dumps({'type': 'ptt', 'on': True}))
                for i in range(0, len(pcm), 1024):
                    await ws.send(bytes([2]) + pcm[i:i + 1024]); await asyncio.sleep(0.032)
                await ws.send(json.dumps({'type': 'ptt', 'on': False}))
            await asyncio.sleep(a.wait)
        task.cancel()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['say', 'talk', 'listen']); ap.add_argument('items', nargs='*')
    ap.add_argument('--url', default='ws://127.0.0.1:8780/link'); ap.add_argument('--cookie'); ap.add_argument('--id')
    ap.add_argument('--wait', type=float, default=10); ap.add_argument('--speak', action='store_true')
    asyncio.run(main(ap.parse_args()))
