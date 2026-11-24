/* The little BITCH drawn in the app. It moves with the same spring physics as the robot (desk.py's Actor) and plays
   the same moves, so what you see on your phone is what the cardboard does on the desk. Googly eyes included. */
(function () {
  const LO = [-0.9, -0.95, -1.0, -0.85], HI = [0.9, -0.15, -0.1, 0.85], REST = [0, -0.8, -0.8, 0];
  const K = [55, 30, 65, 95], C = [11, 10, 11.5, 10], J = ['base', 'arm', 'neck', 'head'];
  const SPEEDS = { slow: 0.6, normal: 1.2, snappy: 2.6 };
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

  function Rig(canvas, opts = {}) {
    const g = canvas.getContext('2d');
    const s = {
      x: REST.slice(), v: [0, 0, 0, 0], tgt: REST.slice(), stiff: 1, fx: [], T: 0, idle: 1,
      feeling: 'neutral', brows: { l: 0, r: 0, ly: 0, ry: 0 }, talk: 0, talkUntil: 0, asleep: false, lid: 0, blinkAt: 2,
      pup: [{ x: 0, y: 0, vx: 0, vy: 0 }, { x: 0, y: 0, vx: 0, vy: 0 }], gaze: { x: 0, y: 0 }, busy: 0, seq: 0, look: null,
      thinking: false, flash: 0, level: () => 0, onSound: null, lib: opts.lib || null,
    };
    let W = 0, H = 0, dpr = 1, last = performance.now(), prevHead = { x: 0, y: 0, r: 0 };

    function fit() {
      const r = canvas.getBoundingClientRect(); dpr = devicePixelRatio || 1;
      if (!r.width) return;
      W = r.width; H = r.height; canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    }
    new ResizeObserver(fit).observe(canvas); fit();

    // ---------- motion ----------
    function step(dt) {
      const T = s.T, m = s.idle * (s.asleep ? 0.25 : 1);
      const idle = [m * (0.05 * Math.sin(T * 0.37) + 0.03 * Math.sin(T * 1.13)), m * 0.07 * Math.sin(T * 1.05),
        m * (0.06 * Math.sin(T * 1.05 + 0.7) + 0.03 * Math.sin(T * 0.29)), m * (0.06 * Math.sin(T * 0.43 + 1) + 0.03 * Math.sin(T * 1.7))];
      const add = [0, 0, 0, 0];
      for (const f of s.fx) if (f.t0 <= T && T < f.t1) {
        const env = Math.min(1, (T - f.t0) / 0.1, (f.t1 - T) / 0.1); add[f.j] += f.amp * env * Math.sin(6.2832 * f.hz * (T - f.t0));
      }
      s.fx = s.fx.filter(f => f.t1 > T);
      if (s.talkUntil > T) {                                  // talking: the head bobs like the robot's lip-sync
        const e = Math.max(s.level(), 0.35 + 0.35 * Math.sin(T * 17) * Math.sin(T * 5.3));
        s.talk += (e - s.talk) * 0.35; add[2] += 0.18 * s.talk; add[3] += 0.06 * s.talk * (Math.floor(T * 3) % 2 ? 1 : -1);
      } else s.talk *= 0.8;
      const tg = s.tgt.slice();
      if (s.look && !s.busy && T - s.look.t < 1.5) { tg[0] = clamp(s.look.b, -0.9, 0.9); if (s.look.n != null) tg[2] = clamp(s.look.n, -1, -0.1); }
      for (let i = 0; i < 4; i++) {
        const k = K[i] * s.stiff, c = C[i] * Math.sqrt(s.stiff);
        const acc = k * (tg[i] + idle[i] - s.x[i]) - c * s.v[i];
        s.v[i] = clamp(s.v[i] + acc * dt, -5, 5); s.x[i] = clamp(s.x[i] + s.v[i] * dt, LO[i], HI[i]);
      }
      s.out = s.x.map((v, i) => clamp(v + add[i], LO[i], HI[i]));
      s.T += dt;
    }

    const wait = (secs, seq) => new Promise(r => setTimeout(() => r(seq === s.seq), secs * 1000));
    async function play(steps) {
      const seq = ++s.seq; s.busy++;
      try {
        for (const st of steps || []) {
          if (seq !== s.seq) return;
          if (st.do === 'pose') {
            J.forEach((j, i) => { if (st[j] != null) s.tgt[i] = clamp(+st[j], LO[i], HI[i]) });
            s.stiff = SPEEDS[st.speed] || 1.2; await wait(st.secs ?? 0.5, seq);
          } else if (st.do === 'wiggle') {
            for (const j of st.joints || ['head']) s.fx.push({ j: J.indexOf(j), amp: st.size ?? 0.3, hz: st.hz ?? 3, t0: s.T, t1: s.T + (st.secs ?? 1) });
            await wait(st.secs ?? 1, seq);
          } else if (st.do === 'sound') { s.onSound && s.onSound(st.sound); kick(0.6); }
          else if (st.do === 'wait') await wait(st.secs ?? 0.5, seq);
          else if (st.do === 'rest') { s.tgt = REST.slice(); s.stiff = 0.9; await wait(st.secs ?? 0.8, seq); }
        }
      } finally { s.busy--; if (seq === s.seq) feel(s.feeling, true) }
    }
    function feel(name, soft) {
      s.feeling = name || 'neutral';
      const p = (s.lib && s.lib.feelings[s.feeling]) || { arm: -0.8, neck: -0.8, head: 0 };
      if (!s.busy) { s.tgt = [s.tgt[0] * 0.5, p.arm, p.neck, p.head]; s.stiff = soft ? 0.9 : 1.3 }
    }
    function gesture(name) { const g = s.lib && s.lib.gestures[name]; if (g) play(g) }
    function kick(a) { for (const p of s.pup) { p.vx += (Math.random() - 0.5) * 40 * a; p.vy -= 30 * a } }
    function talk(secs) { s.talkUntil = s.T + secs }

    // ---------- drawing ----------
    function rr(x, y, w, h, r) { g.beginPath(); g.roundRect(x, y, w, h, r) }
    function draw(now) {
      requestAnimationFrame(draw);
      if (!W) { fit(); if (!W) return; }
      const dt = Math.min(0.05, (now - last) / 1000); last = now;
      for (let k = 0; k < 2; k++) step(dt / 2);
      const [b, a, n, h] = s.out || s.x;
      const ink = css('--ink') || '#1f2a44', kraft = css('--kraft') || '#b98b54', kraft2 = css('--kraft2') || '#8a6235', kraft3 = css('--kraft3') || '#d9b98c';
      g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, W, H);
      const u = Math.min(W / 250, H / 252), x0 = W / 2, ground = H - 12 * u;
      const f = (a + 0.95) / 0.8;                     // 0 tall .. 1 leaning far forward
      const p = (n + 0.8) / 0.7;                      // -0.3 looking down .. 1 looking up
      // shadow
      g.fillStyle = 'rgba(31,42,68,.12)'; g.beginPath(); g.ellipse(x0, ground + 4 * u, 82 * u, 9 * u, 0, 0, 7); g.fill();
      // base (the shoebox on its turntable)
      const bw = 128 * u, bh = 44 * u, bx = x0 - bw / 2, by = ground - bh;
      g.lineWidth = 2.2 * u; g.strokeStyle = ink;
      g.fillStyle = kraft; rr(bx, by, bw, bh, 7 * u); g.fill(); g.stroke();
      g.fillStyle = kraft2; g.globalAlpha = 0.5; rr(bx + bw / 2 - 22 * u + b * 34 * u, by + 13 * u, 44 * u, 7 * u, 3 * u); g.fill(); g.globalAlpha = 1;
      g.beginPath(); g.moveTo(bx + 6 * u, by + 9 * u); g.lineTo(bx + bw - 6 * u, by + 9 * u); g.lineWidth = 1.2 * u; g.globalAlpha = .45; g.stroke(); g.globalAlpha = 1; g.lineWidth = 2.2 * u;
      // the arm (popsicle sticks)
      const elbowX = x0 + b * 30 * u, elbowY = by - (128 - 70 * f) * u, sc = 1 + 0.24 * f;
      g.save(); g.lineCap = 'round';
      for (const off of [-7, 7]) {
        g.strokeStyle = ink; g.lineWidth = 10 * u; g.beginPath(); g.moveTo(x0 + off * u, by + 2 * u); g.lineTo(elbowX + off * u * sc, elbowY + 30 * u); g.stroke();
        g.strokeStyle = kraft3; g.lineWidth = 6.4 * u; g.beginPath(); g.moveTo(x0 + off * u, by + 2 * u); g.lineTo(elbowX + off * u * sc, elbowY + 30 * u); g.stroke();
      }
      g.fillStyle = ink; g.beginPath(); g.arc(x0, by + 2 * u, 5 * u, 0, 7); g.fill();
      g.restore();
      // the head (lamp shade box) with googly eyes
      const turn = Math.cos(b * 0.9), hw = 152 * u * sc * (0.78 + 0.22 * turn), hh = 98 * u * sc * (1 - 0.1 * Math.abs(p));
      const hx = elbowX + b * 10 * u, hy = elbowY - 8 * u - p * 6 * u, rot = h * 0.42;
      g.save(); g.translate(hx, hy); g.rotate(rot);
      g.fillStyle = kraft2; rr(-hw / 2 + 6 * u, -hh / 2 - (p > 0 ? 10 : -4) * u * sc, hw - 12 * u, hh, 12 * u * sc); g.fill(); g.stroke();
      g.fillStyle = kraft; rr(-hw / 2, -hh / 2, hw, hh, 12 * u * sc); g.fill(); g.stroke();
      g.globalAlpha = .35; g.strokeStyle = kraft2; g.lineWidth = 1.5 * u;
      g.beginPath(); g.moveTo(-hw / 2 + 14 * u, hh / 2 - 12 * u); g.lineTo(hw / 2 - 30 * u, hh / 2 - 12 * u); g.stroke();
      g.globalAlpha = 1; g.strokeStyle = ink; g.lineWidth = 2.2 * u;
      // eyes
      const er = 25 * u * sc, pr = 11.5 * u * sc, ex = 30 * u * sc * (0.85 + 0.15 * turn), ey = -8 * u * sc - p * 8 * u;
      const ax = hx, ay = hy, accX = (ax - prevHead.x) / Math.max(dt, 1e-3), accY = (ay - prevHead.y) / Math.max(dt, 1e-3), accR = (rot - prevHead.r) / Math.max(dt, 1e-3);
      prevHead = { x: ax, y: ay, r: rot };
      if (now / 1000 > s.blinkAt) { s.blinkAt = now / 1000 + 2.5 + Math.random() * 4; s.blink = 1 }
      s.blink = Math.max(0, (s.blink || 0) - dt * 7);
      const lidT = s.asleep ? 1 : s.feeling === 'sleepy' ? 0.55 : s.feeling === 'angry' ? 0.22 : 0;
      s.lid += (Math.max(lidT, s.blink > 0.5 ? 1 : 0) - s.lid) * Math.min(1, dt * 18);
      const gx = clamp(s.gaze.x - b * 0.6, -1, 1), gy = clamp(s.gaze.y - p * 0.6, -1, 1);
      [-1, 1].forEach((side, i) => {
        const cx = side * ex + b * 6 * u, cy = ey, P = s.pup[i];
        // googly physics: a loose pupil that falls, lags and bounces
        const tx = gx * (er - pr) * 0.55, ty = gy * (er - pr) * 0.55 + (er - pr) * 0.35;
        P.vx += ((tx - P.x) * 60 - P.vx * 5 - accX * 0.35 - accR * side * 4) * dt;
        P.vy += ((ty - P.y) * 60 - P.vy * 5 - accY * 0.35 + 900 * u * dt * 0) * dt;
        P.x += P.vx * dt; P.y += P.vy * dt;
        const d = Math.hypot(P.x, P.y), lim = er - pr - 1.5 * u;
        if (d > lim) { P.x *= lim / d; P.y *= lim / d; const nx = P.x / d, ny = P.y / d, dot = P.vx * nx + P.vy * ny; if (dot > 0) { P.vx -= 1.6 * dot * nx; P.vy -= 1.6 * dot * ny } }
        g.fillStyle = '#fff'; g.beginPath(); g.arc(cx, cy, er, 0, 7); g.fill(); g.stroke();
        g.fillStyle = '#141a2a'; g.beginPath(); g.arc(cx + P.x, cy + P.y, pr, 0, 7); g.fill();
        g.fillStyle = 'rgba(255,255,255,.85)'; g.beginPath(); g.arc(cx + P.x - pr * 0.35, cy + P.y - pr * 0.35, pr * 0.28, 0, 7); g.fill();
        if (s.lid > 0.02) {                            // eyelid (a cardboard flap)
          g.save(); g.beginPath(); g.arc(cx, cy, er, 0, 7); g.clip();
          g.fillStyle = kraft2; g.fillRect(cx - er - 2, cy - er - 2, er * 2 + 4, (er * 2 + 4) * s.lid);
          g.restore(); g.beginPath(); g.arc(cx, cy, er, 0, 7); g.stroke();
          if (s.lid > 0.9) { g.beginPath(); g.moveTo(cx - er * 0.7, cy + 2 * u); g.quadraticCurveTo(cx, cy + 9 * u, cx + er * 0.7, cy + 2 * u); g.stroke() }
        }
      });
      // brows (cardboard strips) show the feeling
      // angle > 0 tips the inner ends down (cross), < 0 lifts them (worried); dy < 0 raises the brows
      const B = { happy: [-0.1, -8], excited: [-0.14, -12], love: [-0.22, -7], sad: [-0.45, -3], angry: [0.5, 4], scared: [-0.32, -13],
        curious: [[-0.12, -13], [0.14, -2]], confused: [[-0.35, -9], [0.3, 0]], proud: [-0.04, -9], sleepy: [0.06, 2], neutral: [0, -3] }[s.feeling] || [0, -3];
      const pair = Array.isArray(B[0]) ? B : [B, B];
      [-1, 1].forEach((side, i) => {
        const [ang, dy] = pair[i]; const k = side < 0 ? 'l' : 'r';
        s.brows[k] += (ang - s.brows[k]) * 0.15; s.brows[k + 'y'] += (dy - s.brows[k + 'y']) * 0.15;
        g.save(); g.translate(side * ex + b * 6 * u, ey - er - 10 * u * sc + s.brows[k + 'y'] * u * sc); g.rotate(-side * s.brows[k]);
        g.fillStyle = ink; rr(-15 * u * sc, -3 * u * sc, 30 * u * sc, 6 * u * sc, 3 * u * sc); g.fill(); g.restore();
      });
      // speaker slit "mouth": opens when it talks, smiles when it's happy
      const mo = Math.min(1, s.talk), my = ey + er + 16 * u * sc, mw = 30 * u * sc;
      g.fillStyle = ink;
      if (mo > 0.05) { rr(b * 6 * u - mw / 2, my - 3 * u - 8 * u * mo * sc, mw, 6 * u + 16 * u * mo * sc, 6 * u * sc); g.fill() }
      else {
        const smile = { happy: 5, excited: 7, love: 6, proud: 4, sad: -4, angry: -3, scared: -2 }[s.feeling] || 0;
        g.lineWidth = 3.2 * u * sc; g.lineCap = 'round'; g.beginPath(); g.moveTo(b * 6 * u - mw / 2, my); g.quadraticCurveTo(b * 6 * u, my + smile * u * sc, b * 6 * u + mw / 2, my); g.stroke(); g.lineWidth = 2.2 * u;
      }
      g.restore();
      // zzz / thinking / flash
      g.fillStyle = ink; g.font = `700 ${18 * u}px "Helvetica Neue",Helvetica,Arial`;
      if (s.asleep) for (let i = 0; i < 3; i++) { const t = ((s.T * 0.5 + i / 3) % 1); g.globalAlpha = 1 - t; g.fillText('z', hx + hw / 2 + 4 * u + t * 26 * u, hy - hh / 2 - t * 40 * u); }
      g.globalAlpha = 1;
      if (s.thinking) for (let i = 0; i < 3; i++) { g.globalAlpha = 0.3 + 0.7 * ((Math.sin(s.T * 6 - i) + 1) / 2); g.beginPath(); g.arc(hx + hw / 2 + (8 + i * 12) * u, hy - hh / 2 - (6 + i * 10) * u, (3 + i * 1.5) * u, 0, 7); g.fill() }
      g.globalAlpha = 1;
      if (s.flash > 0) { g.fillStyle = `rgba(255,255,255,${s.flash})`; g.fillRect(0, 0, W, H); s.flash = Math.max(0, s.flash - dt * 2.5) }
    }
    requestAnimationFrame(draw);
    canvas.addEventListener('pointerdown', e => {          // boop!
      const r = canvas.getBoundingClientRect();
      s.gaze = { x: clamp((e.clientX - r.left) / r.width * 2 - 1, -1, 1), y: clamp((e.clientY - r.top) / r.height * 2 - 1.2, -1, 1) };
      kick(1.2); s.fx.push({ j: 3, amp: 0.25, hz: 4, t0: s.T, t1: s.T + 0.5 }); setTimeout(() => s.gaze = { x: 0, y: 0 }, 1500);
      opts.onBoop && opts.onBoop();
    });
    return Object.assign(s, { play, feel, gesture, talk, kick, stop: () => { s.seq++; s.tgt = REST.slice() } });
  }
  window.Rig = Rig;
})();
