        const $ = id => document.getElementById(id);
        const messageDiv = $('message');
        const statusEl = $('status');
        const mobileStatus = $('mobileStatus');
        const msg = t => { messageDiv.textContent = t; };
        // The frame the camera is actually delivering. NOT a constant: switching
        // sensor mode changes it, and every overlay coordinate and the whole
        // measurement scale are expressed in these pixels.
        let NATIVE_W = 640, NATIVE_H = 480;

        async function request(path, options = {}) {
            const response = await fetch(path, { cache: 'no-store', ...options });
            if (response.ok) return response;
            // Unwrap {"error": ...} here, once, so every caller's catch shows the
            // reason instead of a raw JSON blob in the message bar.
            const body = await response.text();
            let detail = body;
            try { detail = JSON.parse(body).error || body; } catch (_) {}
            throw new Error(detail || response.statusText);
        }
        const postJSON = (path, body) => request(path, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        });

        // ===== Splash =====
        // Hide shortly after the DOM is ready. Do NOT wait for window 'load':
        // the MJPEG video <img> stream never finishes loading (and produces
        // nothing when no camera is attached), which would pin the splash open.
        (function hideSplash() {
            const go = () => setTimeout(() => $('splash').classList.add('hidden'), 1200);
            if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', go);
            else go();
        })();

        // ============================================================
        //  Movement (continuous press-and-hold)
        // ============================================================
        const HOLD_MS = 110;
        const pHeld = new Set(), kHeld = new Set();
        let holdTimer = null, moveInFlight = false;
        const unionHas = dir => pHeld.has(dir) || kHeld.has(dir);

        async function sendMove(dir) {
            if (moveInFlight) return;
            moveInFlight = true;
            try { await request('/move/' + dir); msg(''); }
            catch (e) { msg(e.message); }
            finally { moveInFlight = false; }
        }
        function tick() { const dir = [...pHeld, ...kHeld][0]; if (dir) sendMove(dir); }
        function refreshTimer() {
            const any = pHeld.size + kHeld.size > 0;
            if (any && !holdTimer) holdTimer = setInterval(tick, HOLD_MS);
            if (!any && holdTimer) { clearInterval(holdTimer); holdTimer = null; }
        }
        function setDirActive(dir, on) {
            document.querySelectorAll('[data-move="' + dir + '"]').forEach(b => b.classList.toggle('active', on));
        }
        function holdStart(set, dir) { if (set.has(dir)) return; set.add(dir); setDirActive(dir, true); sendMove(dir); refreshTimer(); }
        function holdStop(set, dir) { if (!set.has(dir)) return; set.delete(dir); if (!unionHas(dir)) setDirActive(dir, false); refreshTimer(); }

        document.querySelectorAll('[data-move]').forEach(btn => {
            const dir = btn.dataset.move;
            btn.addEventListener('pointerdown', e => {
                e.preventDefault();
                if (btn.setPointerCapture) { try { btn.setPointerCapture(e.pointerId); } catch (_) {} }
                holdStart(pHeld, dir);
            });
            const stop = () => holdStop(pHeld, dir);
            btn.addEventListener('pointerup', stop);
            btn.addEventListener('pointercancel', stop);
        });
        window.addEventListener('pointerup', () => { [...pHeld].forEach(d => holdStop(pHeld, d)); });

        const keyMap = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right', PageUp: 'page_up', PageDown: 'page_down' };
        document.addEventListener('keydown', e => {
            if (e.key === 'Escape') { $('calibModal').classList.remove('show'); cancelTool(); return; }
            if (e.target.tagName === 'INPUT') return;
            const dir = keyMap[e.key];
            if (!dir) return;
            e.preventDefault();
            if (e.repeat) return;
            holdStart(kHeld, dir);
        });
        document.addEventListener('keyup', e => { const dir = keyMap[e.key]; if (dir) holdStop(kHeld, dir); });

        // ============================================================
        //  Step size
        // ============================================================
        document.querySelectorAll('.step-arrow').forEach(btn => {
            btn.addEventListener('click', async () => {
                try {
                    const r = await request('/adjust/' + btn.dataset.step + '/' + btn.dataset.dir);
                    $(btn.dataset.step + '_value').value = await r.text();
                } catch (e) { msg(e.message); }
            });
        });
        document.querySelectorAll('.step-box').forEach(box => {
            box.addEventListener('change', async () => {
                let v = Math.max(1, parseInt(box.value || '1', 10));
                box.value = v;
                try { const r = await request('/set_step/' + box.dataset.axis + '/' + v); box.value = await r.text(); }
                catch (e) { msg(e.message); }
            });
        });

        // ============================================================
        //  Recording length (h / m / s + infinite)
        // ============================================================
        const durH = $('durH'), durM = $('durM'), durS = $('durS'), infiniteBtn = $('infiniteBtn');
        let infinite = false;
        const totalSeconds = () => Math.max(0,
            (parseInt(durH.value || 0, 10)) * 3600 + (parseInt(durM.value || 0, 10)) * 60 + (parseInt(durS.value || 0, 10)));
        async function pushDuration() {
            try { await postJSON('/set_recording_setting', { setting: 'duration', value: infinite ? 0 : totalSeconds() }); }
            catch (e) { msg(e.message); }
        }
        [durH, durM, durS].forEach(b => b.addEventListener('change', () => {
            if (infinite) { infinite = false; infiniteBtn.classList.remove('active'); }
            [durH, durM, durS].forEach(x => x.disabled = false);
            pushDuration();
        }));
        infiniteBtn.addEventListener('click', () => {
            infinite = !infinite;
            infiniteBtn.classList.toggle('active', infinite);
            [durH, durM, durS].forEach(x => x.disabled = infinite);
            pushDuration();
        });

        // ============================================================
        //  Recording control + timer
        // ============================================================
        const recordBtn = $('recordBtn'), mobileRecordBtn = $('mobileRecordBtn');
        let isRecording = false, recRemaining = null, recStartObserved = 0;

        function fmtTime(total) {
            total = Math.max(0, Math.floor(total));
            const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
            const pad = n => String(n).padStart(2, '0');
            return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
        }
        function renderTimer() {
            let text = '';
            if (isRecording) text = (recRemaining == null) ? '∞ ' + fmtTime((Date.now() - recStartObserved) / 1000) : fmtTime(recRemaining);
            $('recTimer').textContent = text;
            $('mobileRecTimer').textContent = text;
        }
        function renderRecordState() {
            [recordBtn, mobileRecordBtn].forEach(b => b && b.classList.toggle('recording', isRecording));
            recordBtn.querySelector('.rec-label').textContent = isRecording ? 'Stop Recording' : 'Record';
            if (mobileRecordBtn) mobileRecordBtn.querySelector('.rec-label').textContent = isRecording ? 'Stop' : 'Record';
            renderTimer();
        }
        function enterRecordingUI(remaining) { isRecording = true; recRemaining = (remaining === undefined ? null : remaining); recStartObserved = Date.now(); renderRecordState(); }
        function exitRecordingUI(text) { isRecording = false; recRemaining = null; renderRecordState(); if (text) msg(text); }
        async function toggleRecording() {
            if (isRecording) {
                let saved = '';
                // Resolves only once the file is closed, so the button never
                // flips back to "Stop" on the next status poll.
                try { saved = (await (await request('/stop_recording', { method: 'POST' })).json()).filename || ''; }
                catch (e) { msg('Stop failed: ' + e.message); }
                exitRecordingUI(saved ? 'Saved ' + saved : 'Recording stopped.');
                return;
            }
            try {
                const d = await (await postJSON('/start_recording', {})).json();
                enterRecordingUI(d.duration === undefined ? null : d.duration);
                msg('Recording → ' + d.path);
            } catch (e) { msg('Recording failed: ' + e.message); }
        }
        recordBtn.addEventListener('click', toggleRecording);
        if (mobileRecordBtn) mobileRecordBtn.addEventListener('click', toggleRecording);

        setInterval(() => { if (!isRecording) return; if (recRemaining != null) recRemaining = Math.max(0, recRemaining - 1); renderTimer(); }, 1000);
        async function pollRecording() {
            try {
                const d = await (await request('/recording_status')).json();
                if (d.recording && !isRecording) enterRecordingUI(d.remaining == null ? null : d.remaining);
                else if (d.recording && isRecording) { if (d.remaining != null) recRemaining = d.remaining; }
                else if (!d.recording && isRecording) exitRecordingUI('Recording saved.');
            } catch (e) {}
        }
        setInterval(pollRecording, 2000); pollRecording();

        // ============================================================
        //  Calibration (autofocus / white balance)
        // ============================================================
        const autofocusBtn = $('autofocusBtn'), whiteBalanceBtn = $('whiteBalanceBtn');
        async function waitForCalibration(timeoutSec = 120) {
            for (let i = 0; i < timeoutSec; i++) {
                await new Promise(r => setTimeout(r, 1000));
                try { if (!(await (await request('/calibration_status')).json()).running) return true; } catch (e) {}
            }
            return false;
        }
        async function runCalibration(endpoint, button, label) {
            autofocusBtn.disabled = true; whiteBalanceBtn.disabled = true; button.classList.add('busy');
            msg(label + ' started…');
            try {
                const resp = await fetch(endpoint, { method: 'POST' });
                const data = await resp.json();
                if (!resp.ok || data.error) throw new Error(data.error || resp.statusText);
                const finished = await waitForCalibration();
                msg(finished ? label + ' complete.' : label + ' is taking unusually long — check the server log.');
            } catch (e) { msg(label + ' failed: ' + e.message); }
            finally { autofocusBtn.disabled = false; whiteBalanceBtn.disabled = false; button.classList.remove('busy'); }
        }
        autofocusBtn.addEventListener('click', () => runCalibration('/autofocus', autofocusBtn, 'Autofocus'));
        whiteBalanceBtn.addEventListener('click', () => runCalibration('/white_balance', whiteBalanceBtn, 'White balance'));

        // ============================================================
        //  Telemetry HUD (position + real fps)
        // ============================================================
        async function pollTelemetry() {
            try {
                const d = await (await request('/telemetry')).json();
                $('posX').textContent = d.position.x;
                $('posY').textContent = d.position.y;
                $('posZ').textContent = d.position.z;
            } catch (e) {}
        }
        setInterval(pollTelemetry, 1000); pollTelemetry();

        async function refreshStatus() {
            try {
                const d = await (await request('/status')).json();
                const txt = d.controller_connected ? 'Controller online' : 'Controller unavailable';
                statusEl.textContent = txt; statusEl.className = 'status ' + (d.controller_connected ? 'ok' : 'bad');
                if (mobileStatus) mobileStatus.textContent = txt;
            } catch (e) {
                statusEl.textContent = 'Offline'; statusEl.className = 'status bad';
                if (mobileStatus) mobileStatus.textContent = 'Offline';
            }
        }
        refreshStatus(); setInterval(refreshStatus, 5000);

        // ============================================================
        //  Where clips are saved (on the machine running this UI)
        // ============================================================
        const destPath = $('destPath'), destHost = $('destHost'), destSetBtn = $('destSetBtn');
        const destSection = document.querySelector('.dest-section');

        function paintDest(d) {
            if (document.activeElement !== destPath) destPath.value = d.path || '';
            destHost.textContent = d.host ? 'on ' + d.host : '';
            destSection.classList.add('saved');
            setTimeout(() => destSection.classList.remove('saved'), 1200);
        }
        async function setDest() {
            destSetBtn.disabled = true;
            try {
                const d = await (await postJSON('/recordings/dir', { path: destPath.value })).json();
                if (d.error) throw new Error(d.error);
                paintDest(d);
                msg('Clips will be saved to ' + d.path);
            } catch (e) { msg('Folder not usable: ' + e.message); }
            finally { destSetBtn.disabled = false; }
        }
        destSetBtn.addEventListener('click', setDest);
        destPath.addEventListener('keydown', e => { if (e.key === 'Enter') setDest(); });
        request('/recordings/dir').then(r => r.json()).then(paintDest).catch(() => {});

        // ============================================================
        //  Calibration + Measure tools (overlay on the video)
        // ============================================================
        const videoFrame = $('videoFrame'), overlay = $('overlay'), octx = overlay.getContext('2d');
        const streamImg = $('stream'), toolBanner = $('toolBanner');
        const calibrateBtn = $('calibrateBtn'), measureBtn = $('measureBtn');
        const scaleReadout = $('scaleReadout'), measureResult = $('measureResult');
        let calibration = { um_per_px: null, um_per_px_width: 0 };
        let tool = null;            // 'calibrate' | 'measure' | 'review' | null
        let points = [];            // native-pixel coords {x,y}
        let frozenCanvas = null;

        function contentRect() {
            const w = overlay.width, h = overlay.height;
            const scale = Math.min(w / NATIVE_W, h / NATIVE_H);
            const cw = NATIVE_W * scale, ch = NATIVE_H * scale;
            return { scale, ox: (w - cw) / 2, oy: (h - ch) / 2, cw, ch };
        }
        function resizeOverlay() {
            overlay.width = overlay.clientWidth;
            overlay.height = overlay.clientHeight;
            drawOverlay();
        }
        if (window.ResizeObserver) new ResizeObserver(resizeOverlay).observe(videoFrame);
        window.addEventListener('resize', resizeOverlay);

        function toNative(rect, x, y) {
            return { x: Math.max(0, Math.min(NATIVE_W, (x - rect.ox) / rect.scale)),
                     y: Math.max(0, Math.min(NATIVE_H, (y - rect.oy) / rect.scale)) };
        }
        const toDisp = (rect, p) => ({ x: rect.ox + p.x * rect.scale, y: rect.oy + p.y * rect.scale });
        const distPx = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

        function niceNumber(x) {
            const exp = Math.floor(Math.log10(x)), f = x / Math.pow(10, exp);
            const nf = f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10;
            return nf * Math.pow(10, exp);
        }
        // Micrometres per pixel OF THE FRAME WE HAVE NOW. The stored scale
        // belongs to the resolution it was measured at, so switching sensor mode
        // changes it by exactly the width ratio -- same slide, fewer/more pixels.
        function umPerPx() {
            if (!calibration.um_per_px) return null;
            const at = calibration.um_per_px_width || NATIVE_W;
            return calibration.um_per_px * at / NATIVE_W;
        }
        function drawScaleBar(rect) {
            const upp = umPerPx();
            const targetDisp = rect.cw * 0.18;
            const targetUm = (targetDisp / rect.scale) * upp;
            const um = niceNumber(targetUm);
            const dispLen = (um / upp) * rect.scale;
            const x0 = rect.ox + 16, y0 = rect.oy + rect.ch - 18;
            octx.strokeStyle = '#fff'; octx.fillStyle = '#fff'; octx.lineWidth = 3;
            octx.beginPath();
            octx.moveTo(x0, y0); octx.lineTo(x0 + dispLen, y0);
            octx.moveTo(x0, y0 - 6); octx.lineTo(x0, y0 + 6);
            octx.moveTo(x0 + dispLen, y0 - 6); octx.lineTo(x0 + dispLen, y0 + 6);
            octx.stroke();
            const label = um >= 1000 ? (um / 1000) + ' mm' : um + ' µm';
            octx.font = '600 13px Inter, sans-serif'; octx.textAlign = 'center';
            octx.lineWidth = 3; octx.strokeStyle = 'rgba(0,0,0,.6)';
            octx.strokeText(label, x0 + dispLen / 2, y0 - 10);
            octx.fillText(label, x0 + dispLen / 2, y0 - 10);
        }
        function drawMeasure(rect) {
            const pts = points.map(p => toDisp(rect, p));
            octx.fillStyle = '#6ee7b7'; octx.strokeStyle = '#6ee7b7'; octx.lineWidth = 2;
            pts.forEach(p => { octx.beginPath(); octx.arc(p.x, p.y, 5, 0, 7); octx.fill(); });
            if (pts.length === 2) {
                octx.beginPath(); octx.moveTo(pts[0].x, pts[0].y); octx.lineTo(pts[1].x, pts[1].y); octx.stroke();
            }
        }
        function drawOverlay() {
            octx.clearRect(0, 0, overlay.width, overlay.height);
            const rect = contentRect();
            if (frozenCanvas) {
                octx.drawImage(frozenCanvas, rect.ox, rect.oy, rect.cw, rect.ch);
                octx.fillStyle = 'rgba(6,14,10,.28)';
                octx.fillRect(rect.ox, rect.oy, rect.cw, rect.ch);
            }
            if (umPerPx()) drawScaleBar(rect);
            if (points.length) drawMeasure(rect);
        }

        // The newest frame as a canvas: the frozen still if the view is held,
        // otherwise the live <img>. Everything that needs pixels -- the measuring
        // tools' dimmed backdrop, the screenshot -- goes through here, so they
        // all capture the SAME frame the operator is looking at.
        function currentFrame() {
            const src = held ? heldCanvas : streamImg;
            const c = document.createElement('canvas');
            c.width = NATIVE_W; c.height = NATIVE_H;
            try { c.getContext('2d').drawImage(src, 0, 0, NATIVE_W, NATIVE_H); }
            catch (e) { return null; }
            return c;
        }
        function freezeFrame() { frozenCanvas = currentFrame(); }
        function setBanner(text) { toolBanner.textContent = text; toolBanner.classList.toggle('show', !!text); }

        function startTool(which) {
            cancelTool();
            tool = which; points = [];
            freezeFrame();
            overlay.classList.add('active');
            (which === 'calibrate' ? calibrateBtn : measureBtn).classList.add('armed');
            setBanner(which === 'calibrate' ? 'Calibration: click two points a known distance apart'
                                            : 'Measure: click two points');
            drawOverlay();
        }
        function cancelTool() {
            if (!tool) return;
            tool = null; points = []; frozenCanvas = null;
            overlay.classList.remove('active');
            calibrateBtn.classList.remove('armed'); measureBtn.classList.remove('armed');
            setBanner('');
            drawOverlay();
        }

        overlay.addEventListener('pointerdown', e => {
            if (!tool) return;
            if (tool === 'review') { cancelTool(); return; }
            const r = overlay.getBoundingClientRect();
            const rect = contentRect();
            points.push(toNative(rect, e.clientX - r.left, e.clientY - r.top));
            drawOverlay();
            if (points.length === 2) finishTwoPoints();
        });

        function finishTwoPoints() {
            const px = distPx(points[0], points[1]);
            if (tool === 'calibrate') {
                pendingCalibPx = px;
                $('calibUm').value = '';
                $('calibModal').classList.add('show');
                $('calibUm').focus();
            } else {
                let txt = `${px.toFixed(1)} px`;
                const upp = umPerPx();
                if (upp) {
                    const um = px * upp;
                    txt += um >= 1000 ? ` · ${(um / 1000).toFixed(3)} mm` : ` · ${um.toFixed(2)} µm`;
                } else {
                    txt += ' · (not calibrated)';
                }
                measureResult.textContent = txt;
                setBanner(txt + '  — click video to dismiss');
                tool = 'review';
            }
        }

        // Calibration distance modal
        let pendingCalibPx = 0;
        const calibModal = $('calibModal');
        $('calibCancel').addEventListener('click', () => { calibModal.classList.remove('show'); cancelTool(); });
        $('calibSave').addEventListener('click', async () => {
            const um = parseFloat($('calibUm').value);
            if (!(um > 0)) { $('calibUm').focus(); return; }
            try {
                // The width travels WITH the scale -- without it the number is
                // unconvertible the moment the sensor mode changes.
                const rec = await (await postJSON('/set_calibration',
                    { pixels: pendingCalibPx, micrometres: um, width: NATIVE_W })).json();
                if (rec.error) throw new Error(rec.error);
                calibration = rec;
                updateScaleReadout();
                msg('Calibration saved.');
            } catch (e) { msg('Calibration failed: ' + e.message); }
            calibModal.classList.remove('show');
            cancelTool();
        });
        $('calibUm').addEventListener('keydown', e => { if (e.key === 'Enter') $('calibSave').click(); });

        function updateScaleReadout() {
            const upp = umPerPx();
            scaleReadout.textContent = upp
                ? `Scale: ${upp.toFixed(4)} µm/px  at ${NATIVE_W}×${NATIVE_H}`
                : 'Not calibrated';
            drawOverlay();
        }

        calibrateBtn.addEventListener('click', async () => {
            if (tool === 'calibrate') { cancelTool(); return; }
            if (isRecording) { try { await request('/stop_recording', { method: 'POST' }); exitRecordingUI('Recording stopped for calibration.'); } catch (e) {} }
            startTool('calibrate');
        });
        measureBtn.addEventListener('click', () => { tool === 'measure' || tool === 'review' ? cancelTool() : startTool('measure'); });

        async function loadCalibration() {
            try { calibration = await (await request('/get_calibration')).json(); } catch (e) {}
            updateScaleReadout();
        }
        streamImg.addEventListener('load', resizeOverlay);
        loadCalibration();
        resizeOverlay();

        // ============================================================
        //  Freeze / screenshot
        // ============================================================
        // Freeze is a VIEWING hold, not a capture control: the stream keeps
        // arriving and recording keeps writing every frame. It just stops the
        // picture moving so you can measure something on it.
        const freezeBtn = $('freezeBtn'), shotBtn = $('shotBtn');
        const heldCanvas = $('heldFrame'), heldCtx = heldCanvas.getContext('2d');
        let held = false;

        function setHeld(on) {
            if (on) {
                heldCanvas.width = NATIVE_W; heldCanvas.height = NATIVE_H;
                try { heldCtx.drawImage(streamImg, 0, 0, NATIVE_W, NATIVE_H); }
                catch (e) { msg('No frame to freeze yet.'); return; }
            }
            held = on;
            heldCanvas.classList.toggle('show', held);
            freezeBtn.classList.toggle('armed', held);
            freezeBtn.textContent = held ? 'Live' : 'Freeze';
            setBanner(held ? 'Frame held — press Live to resume' : '');
        }
        freezeBtn.addEventListener('click', () => setHeld(!held));

        shotBtn.addEventListener('click', async () => {
            const frame = currentFrame();
            if (!frame) { msg('No frame to save yet.'); return; }
            // Burn the overlay in: a screenshot with its own scale bar and
            // measurement on it is a lab record; one without is just a picture.
            try { frame.getContext('2d').drawImage(overlay, 0, 0, NATIVE_W, NATIVE_H); }
            catch (e) {}
            shotBtn.disabled = true;
            try {
                const blob = await new Promise(r => frame.toBlob(r, 'image/jpeg', 0.95));
                const d = await (await request('/screenshot', {
                    method: 'POST', headers: { 'Content-Type': 'image/jpeg' }, body: blob
                })).json();
                msg('Saved ' + d.filename);
            } catch (e) { msg('Screenshot failed: ' + e.message); }
            finally { shotBtn.disabled = false; }
        });

        // ============================================================
        //  Sensor mode (resolution vs frame rate)
        // ============================================================
        const modeReadout = $('modeReadout');
        const modeBtns = [...document.querySelectorAll('.mode-btn')];

        function paintMode(d) {
            const spec = (d.modes || {})[d.mode] || {};
            modeBtns.forEach(b => {
                b.classList.toggle('active', b.dataset.mode === d.mode);
                b.disabled = !(d.modes || {})[b.dataset.mode];
            });
            if (d.width) {
                // Every overlay coordinate is in these pixels, and so is the
                // scale, so the change has to reach both before the next draw.
                NATIVE_W = d.width; NATIVE_H = d.height;
                resizeOverlay(); updateScaleReadout();
            }
            modeReadout.textContent = d.width
                ? `${d.width}×${d.height} · ${spec.fps || '?'} fps`
                  + (spec.full_fov === false ? ' · cropped' : '')
                : '—';
        }
        modeBtns.forEach(btn => btn.addEventListener('click', async () => {
            if (btn.classList.contains('active')) return;
            modeBtns.forEach(b => b.disabled = true);
            msg('Switching sensor mode…');
            try {
                paintMode(await (await postJSON('/camera/mode', { mode: btn.dataset.mode })).json());
                msg('Sensor mode: ' + btn.dataset.mode);
            } catch (e) { msg('Mode switch failed: ' + e.message); }
            finally { modeBtns.forEach(b => b.disabled = false); }
        }));
        request('/camera/mode').then(r => r.json()).then(paintMode).catch(() => {});

        // ============================================================
        //  Galvo laser  (X = CH1, Y = CH2)
        // ============================================================
        // The wavegen buffer is tiny, so we DELIBERATELY do not stream on slider
        // drag: moving a slider only stages a value in its number box. Nothing
        // reaches the instrument until the operator clicks "Update Galvo", which
        // sends exactly one update() per channel.
        const galvoX = $('galvoX'), galvoY = $('galvoY');
        const galvoXVal = $('galvoXVal'), galvoYVal = $('galvoYVal');
        const galvoUpdateBtn = $('galvoUpdateBtn'), galvoReadout = $('galvoReadout');
        const galvoSection = document.querySelector('.galvo-section'), galvoStatusEl = $('galvoStatus');
        let galvoConnected = false;
        // The operator has staged a value that has not been sent yet. While
        // this is set, polling must NOT overwrite the controls -- that would
        // delete what they were in the middle of typing.
        let galvoStaged = false;

        const clampGalvo = v => { v = parseFloat(v); return isFinite(v) ? Math.max(-5, Math.min(5, v)) : 0; };
        function stageGalvo(slider, box, v) { v = clampGalvo(v); slider.value = v; box.value = v.toFixed(2); }

        // Slider drag -> update the paired number box only (no network traffic).
        galvoX.addEventListener('input', () => { galvoXVal.value = clampGalvo(galvoX.value).toFixed(2); galvoStaged = true; });
        galvoY.addEventListener('input', () => { galvoYVal.value = clampGalvo(galvoY.value).toFixed(2); galvoStaged = true; });
        galvoXVal.addEventListener('change', () => { stageGalvo(galvoX, galvoXVal, galvoXVal.value); galvoStaged = true; });
        galvoYVal.addEventListener('change', () => { stageGalvo(galvoY, galvoYVal, galvoYVal.value); galvoStaged = true; });

        function setGalvoStatus(connected) {
            galvoConnected = !!connected;
            galvoSection.classList.toggle('disabled', !galvoConnected);
            galvoUpdateBtn.disabled = !galvoConnected;
            galvoStatusEl.textContent = galvoConnected ? 'Galvo online' : 'Galvo offline';
            galvoStatusEl.className = 'galvo-status ' + (galvoConnected ? 'on' : 'off');
        }

        galvoUpdateBtn.addEventListener('click', async () => {
            if (!galvoConnected) { msg('Galvo offline'); return; }
            const x = clampGalvo(galvoX.value), y = clampGalvo(galvoY.value);
            galvoUpdateBtn.disabled = true;
            try {
                const d = await (await postJSON('/galvo/update', { x, y })).json();
                if (d.error) throw new Error(d.error);
                setGalvoStatus(d.connected);
                galvoStaged = false;          // sent: polling owns the controls again
                paintGalvoPosition(d.x, d.y);
                msg('Galvo updated.');
            } catch (e) { msg('Galvo update failed: ' + e.message); }
            finally { galvoUpdateBtn.disabled = !galvoConnected; }
        });

        function paintGalvoPosition(x, y) {
            stageGalvo(galvoX, galvoXVal, x);
            stageGalvo(galvoY, galvoYVal, y);
            galvoReadout.textContent = `CH1 ${clampGalvo(x).toFixed(2)} V · CH2 ${clampGalvo(y).toFixed(2)} V`;
        }

        async function pollGalvo() {
            try {
                const d = await (await request('/galvo/status')).json();
                setGalvoStatus(d.connected);
                // Show where the mirrors REALLY are. An agent over MCP, or a
                // second UI, moves the same instrument; before this the sliders
                // only ever showed what this browser had commanded, so the panel
                // and the hardware quietly drifted apart.
                if (!galvoStaged && d.x != null) paintGalvoPosition(d.x, d.y);
            } catch (e) { setGalvoStatus(false); }
        }
        setInterval(pollGalvo, 3000); pollGalvo();

        // ============================================================
        //  GPIO laser relay
        // ============================================================
        const laserToggleBtn = $('laserToggleBtn'), laserNote = $('laserNote');
        const laserState = laserToggleBtn.querySelector('.laser-state');
        let laserOn = false, laserAvailable = false, laserBusy = false;

        function paintLaser() {
            laserToggleBtn.disabled = !laserAvailable || laserBusy;
            laserToggleBtn.classList.toggle('on', laserOn);
            laserState.textContent = laserOn ? 'ON' : 'OFF';
            laserToggleBtn.setAttribute('aria-pressed', laserOn ? 'true' : 'false');
            laserToggleBtn.title = laserAvailable ? 'Switch the laser relay'
                : 'Laser relay offline — this is the last state it reported';
            // Offline is UNKNOWN, not off: say so rather than showing a calm OFF.
            laserNote.textContent = !laserAvailable
                ? (laserOn ? 'Relay offline — assume LIVE' : 'Relay offline')
                : (laserOn ? 'Relay energised' : 'Relay open');
            laserNote.classList.toggle('armed', laserOn);
        }

        // Never polls over a toggle in flight: it reads a cache the POST has not
        // written yet, and its await can land AFTER the click handler repaints,
        // putting the pre-toggle state back under a laser that just changed.
        async function pollLaser() {
            if (laserBusy) return;
            try {
                const d = await (await request('/relay/status')).json();
                laserAvailable = !!d.available; laserOn = !!d.on;
            }
            // laserOn is deliberately left alone: unreachable is unknown, not off.
            catch (e) { laserAvailable = false; }
            paintLaser();
        }

        laserToggleBtn.addEventListener('click', async () => {
            if (!laserAvailable || laserBusy) return;
            laserBusy = true; paintLaser();
            try {
                const d = await (await postJSON('/relay/set', { on: !laserOn })).json();
                laserAvailable = !!d.available; laserOn = !!d.on;
                msg(laserOn ? 'Laser switched ON.' : 'Laser switched OFF.');
            }
            // No re-read here: the state is now unknown, the poll below resyncs
            // within 2 s, and the button keeps showing the last state we know of.
            catch (e) { msg('Laser control failed: ' + e.message); }
            finally { laserBusy = false; paintLaser(); }
        });

        setInterval(pollLaser, 2000); pollLaser();

        // ============================================================
        //  Sample temperature  (Wavelength TC10 LAB)
        // ============================================================
        // Two separate commands, mirroring the instrument: writing a number sets
        // the SETPOINT, and the TEC only drives once "Enable" is on. Reading and
        // setpoint come from cached telemetry, so polling costs the instrument
        // nothing.
        const tempRead = $('tempRead'), tempSet = $('tempSet'), tempEnableBtn = $('tempEnableBtn');
        const tempSection = document.querySelector('.temp-section');
        let tempConnected = false, tempOn = false, tempEditing = false;

        // Don't fight the operator: while the box has focus, polling leaves it alone.
        tempSet.addEventListener('focus', () => { tempEditing = true; });
        tempSet.addEventListener('blur', () => { tempEditing = false; });

        function paintTemp(d) {
            tempConnected = !!d.connected;
            tempOn = !!d.output;
            const t = d.temperature, unit = d.units || 'C';
            tempRead.innerHTML = (t === null || t === undefined ? '—' : t.toFixed(2)) +
                                 '<i>°' + unit + '</i>';
            // grey = idle, amber = driving, green = in tolerance, red = offline/fault
            const faults = d.faults || [];
            let cls = 'fault';
            if (tempConnected && !faults.length) cls = !tempOn ? 'off' : (d.in_tolerance ? 'stable' : 'on');
            tempRead.className = 'temp-read ' + cls;
            tempRead.title = !tempConnected ? 'Temperature controller offline'
                           : faults.length ? 'Fault: ' + faults.join(', ')
                           : 'Measured sample temperature';

            tempEnableBtn.classList.toggle('on', tempOn && tempConnected);
            tempEnableBtn.textContent = tempOn ? 'Enabled' : 'Enable';
            tempEnableBtn.disabled = !tempConnected;
            tempSection.classList.toggle('disabled', !tempConnected);
            if (!tempEditing && d.setpoint !== null && d.setpoint !== undefined)
                tempSet.value = d.setpoint.toFixed(1);
        }

        tempSet.addEventListener('change', async () => {
            const v = parseFloat(tempSet.value);
            if (!isFinite(v)) return;
            try {
                const d = await (await postJSON('/temperature/set', { setpoint: v })).json();
                if (d.error) throw new Error(d.error);
                paintTemp(d);
                msg(`Setpoint ${v.toFixed(1)} °C.` + (tempOn ? '' : ' Press Enable to drive it.'));
            } catch (e) { msg('Setpoint failed: ' + e.message); }
        });

        tempEnableBtn.addEventListener('click', async () => {
            if (!tempConnected) { msg('Temperature controller offline'); return; }
            const want = !tempOn;
            tempEnableBtn.disabled = true;
            try {
                const d = await (await postJSON('/temperature/output', { on: want })).json();
                if (d.error) throw new Error(d.error);
                paintTemp(d);
                msg(want ? 'TEC output enabled.' : 'TEC output disabled.');
            } catch (e) { msg('TEC switch failed: ' + e.message); }
            finally { tempEnableBtn.disabled = !tempConnected; }
        });

        async function pollTemp() {
            try { paintTemp(await (await request('/temperature/status')).json()); }
            catch (e) { paintTemp({ connected: false }); }
        }
        setInterval(pollTemp, 2000); pollTemp();

        // ============================================================
        //  Mobile mode
        // ============================================================
        $('mobileToggle').addEventListener('click', () => {
            document.body.classList.add('mobile-mode');
            const el = document.documentElement;
            if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
            if (screen.orientation && screen.orientation.lock) screen.orientation.lock('landscape').catch(() => {});
        });
        $('mobileExit').addEventListener('click', () => {
            document.body.classList.remove('mobile-mode');
            if (document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(() => {});
            if (screen.orientation && screen.orientation.unlock) { try { screen.orientation.unlock(); } catch (_) {} }
        });
