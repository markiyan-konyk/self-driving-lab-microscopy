// SCOPIO ROS gateway UI. Every action here is an HTTP call to the gateway,
// which translates it into a ROS service call / action goal. Telemetry is
// polled from /api/state, which the gateway fills from ROS subscriptions.
const $ = id => document.getElementById(id);
const msg = t => { $('msg').textContent = t || ''; };
const post = (path, body) => fetch(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {})
}).then(r => r.json());

let tracking = false, recording = false;

// ---- telemetry poll (ROS topics -> gateway cache -> here) ----
async function poll() {
  try {
    const s = await (await fetch('/api/state', { cache: 'no-store' })).json();
    const st = s.stage, la = s.laser, be = s.beads, re = s.recording;
    const line = (label, v) => `<div><span>${label}</span> <b>${v}</b></div>`;
    let html = '';
    html += line('stage', st ? `${st.x}, ${st.y}, ${st.z}` + (st.connected ? '' : ' (off)') : '—');
    html += line('laser V', la && la.connected ? `${la.vx}, ${la.vy}` : 'off');
    html += line('laser µm', la && la.connected ? `${la.global_x}, ${la.global_y}` : '—');
    html += line('beads', be ? `${be.count} (${be.clump_count} clump)` : '—');
    html += line('rec', re && re.recording ? `● ${re.remaining_s}s` : 'idle');
    $('hud').innerHTML = html;

    tracking = !!(be && document._trackerOn);
    recording = !!(re && re.recording);
    $('recBtn').classList.toggle('on', recording);
    $('recBtn').textContent = recording ? 'Stop recording' : 'Record';
    $('status').textContent = 'gateway online';
  } catch (e) { $('status').textContent = 'gateway offline'; }
}
setInterval(poll, 1000); poll();

// ---- tracker toggle (ROS service: tracker/set_active) ----
$('trackerBtn').addEventListener('click', async () => {
  const next = !document._trackerOn;
  try {
    const r = await post('/api/tracker', { active: next });
    if (!r.success) throw new Error(r.message);
    document._trackerOn = next;
    $('trackerBtn').classList.toggle('on', next);
    $('trackerBtn').textContent = next ? 'Stop tracking' : 'Start tracking';
    msg(r.message);
  } catch (e) { msg('tracker: ' + e.message); }
});

// ---- recording toggle (ROS service: recording/set) ----
$('recBtn').addEventListener('click', async () => {
  try {
    const r = await post('/api/recording', { start: !recording, duration_s: 0 });
    msg(r.message || (r.success ? 'ok' : 'failed'));
  } catch (e) { msg('record: ' + e.message); }
});

// ---- laser jog (ROS service: laser/set, relative) ----
const STEP = 0.05;
const dmap = { up: [0, STEP], down: [0, -STEP], left: [-STEP, 0], right: [STEP, 0] };
document.querySelectorAll('[data-d]').forEach(b => b.addEventListener('click', async () => {
  const [dvx, dvy] = dmap[b.dataset.d];
  try { await post('/api/laser/set', { vx: dvx, vy: dvy, relative: true }); msg(''); }
  catch (e) { msg('laser: ' + e.message); }
}));

// ---- zero tweezers (ROS service: tweezers/zero) ----
$('zeroBtn').addEventListener('click', async () => {
  try { const r = await post('/api/tweezers/zero', {}); msg(r.success ? 'zeroed' : 'no galvo'); }
  catch (e) { msg('zero: ' + e.message); }
});

// ---- run a circle waveform (ROS action: galvo/run_waveform) ----
$('circleBtn').addEventListener('click', async () => {
  try {
    await post('/api/galvo/waveform',
      { shape: 'circle', x_freq_hz: 2, y_freq_hz: 2, amplitude_vpp: 1, y_phase_deg: 90, duration_s: 5 });
    msg('circle running (5s)…');
  } catch (e) { msg('waveform: ' + e.message); }
});
