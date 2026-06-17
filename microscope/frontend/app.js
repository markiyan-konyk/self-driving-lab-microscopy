        const $ = id => document.getElementById(id);
        const messageDiv = $('message');
        const statusEl = $('status');
        const mobileStatus = $('mobileStatus');
        const msg = t => { messageDiv.textContent = t; };

        async function request(path, options = {}) {
            const response = await fetch(path, { cache: 'no-store', ...options });
            if (!response.ok) throw new Error(await response.text() || response.statusText);
            return response;
        }
        const postJSON = (path, body) => request(path, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });

        // ============================================================
        //  Movement (continuous press-and-hold, pointer + keyboard)
        // ============================================================
        const HOLD_MS = 110;
        const pHeld = new Set();   // directions held by pointer
        const kHeld = new Set();   // directions held by keyboard
        let holdTimer = null;
        let moveInFlight = false;

        function unionHas(dir) { return pHeld.has(dir) || kHeld.has(dir); }

        async function sendMove(dir) {
            if (moveInFlight) return;       // don't let slow requests pile up
            moveInFlight = true;
            try { await request('/move/' + dir); msg(''); }
            catch (e) { msg(e.message); }
            finally { moveInFlight = false; }
        }

        function tick() {
            // Serialize: fire the first active direction each tick.
            const dir = [...pHeld, ...kHeld][0];
            if (dir) sendMove(dir);
        }
        function refreshTimer() {
            const any = pHeld.size + kHeld.size > 0;
            if (any && !holdTimer) holdTimer = setInterval(tick, HOLD_MS);
            if (!any && holdTimer) { clearInterval(holdTimer); holdTimer = null; }
        }
        function setDirActive(dir, on) {
            document.querySelectorAll('[data-move="' + dir + '"]')
                .forEach(b => b.classList.toggle('active', on));
        }
        function holdStart(set, dir) {
            if (set.has(dir)) return;
            set.add(dir);
            setDirActive(dir, true);
            sendMove(dir);          // immediate response to the first press
            refreshTimer();
        }
        function holdStop(set, dir) {
            if (!set.has(dir)) return;
            set.delete(dir);
            if (!unionHas(dir)) setDirActive(dir, false);
            refreshTimer();
        }

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
        // Safety net: any pointer release clears pointer-held directions.
        window.addEventListener('pointerup', () => { [...pHeld].forEach(d => holdStop(pHeld, d)); });

        const keyMap = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right', PageUp: 'page_up', PageDown: 'page_down' };
        document.addEventListener('keydown', e => {
            if (e.target.tagName === 'INPUT') return;     // don't hijack typing
            const dir = keyMap[e.key];
            if (!dir) return;
            e.preventDefault();
            if (e.repeat) return;                          // we drive our own repeat
            holdStart(kHeld, dir);
        });
        document.addEventListener('keyup', e => {
            const dir = keyMap[e.key];
            if (dir) holdStop(kHeld, dir);
        });

        // ============================================================
        //  Step size
        // ============================================================
        document.querySelectorAll('.step-arrow').forEach(btn => {
            btn.addEventListener('click', async () => {
                const axis = btn.dataset.step;
                const op = btn.dataset.dir; // inc | dec
                try {
                    const r = await request('/adjust/' + axis + '/' + op);
                    $(axis + '_value').value = await r.text();
                } catch (e) { msg(e.message); }
            });
        });
        document.querySelectorAll('.step-box').forEach(box => {
            box.addEventListener('change', async () => {
                const axis = box.dataset.axis;
                let v = Math.max(1, parseInt(box.value || '1', 10));
                box.value = v;
                try {
                    const r = await request('/set_step/' + axis + '/' + v);
                    box.value = await r.text();
                } catch (e) { msg(e.message); }
            });
        });

        // ============================================================
        //  Recording length (h / m / s + infinite)
        // ============================================================
        const durH = $('durH'), durM = $('durM'), durS = $('durS');
        const infiniteBtn = $('infiniteBtn');
        let infinite = false;

        function totalSeconds() {
            const h = parseInt(durH.value || 0, 10);
            const m = parseInt(durM.value || 0, 10);
            const s = parseInt(durS.value || 0, 10);
            return Math.max(0, h * 3600 + m * 60 + s);
        }
        async function pushDuration() {
            const value = infinite ? 0 : totalSeconds();
            try { await postJSON('/set_recording_setting', { setting: 'duration', value }); }
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
            [durH, durM, durS].forEach(x => { x.disabled = infinite; });
            pushDuration();
        });

        // ============================================================
        //  Recording control + timer
        // ============================================================
        const recordBtn = $('recordBtn');
        const mobileRecordBtn = $('mobileRecordBtn');
        let isRecording = false;
        let recRemaining = null;     // seconds left (null = open-ended)
        let recStartObserved = 0;

        function fmtTime(total) {
            total = Math.max(0, Math.floor(total));
            const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
            const pad = n => String(n).padStart(2, '0');
            return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
        }
        function renderTimer() {
            let text = '';
            if (isRecording) {
                if (recRemaining == null) {
                    text = '∞ ' + fmtTime((Date.now() - recStartObserved) / 1000);
                } else {
                    text = fmtTime(recRemaining);
                }
            }
            $('recTimer').textContent = text;
            $('mobileRecTimer').textContent = text;
        }
        function renderRecordState() {
            [recordBtn, mobileRecordBtn].forEach(b => b && b.classList.toggle('recording', isRecording));
            recordBtn.querySelector('.rec-label').textContent = isRecording ? 'Stop Recording' : 'Record';
            if (mobileRecordBtn) mobileRecordBtn.querySelector('.rec-label').textContent = isRecording ? 'Stop' : 'Record';
            renderTimer();
        }
        function enterRecordingUI(remaining) {
            isRecording = true;
            recRemaining = (remaining === undefined ? null : remaining);
            recStartObserved = Date.now();
            renderRecordState();
        }
        function exitRecordingUI(text) {
            isRecording = false;
            recRemaining = null;
            renderRecordState();
            if (text) msg(text);
        }
        async function toggleRecording() {
            if (isRecording) {
                try { await request('/stop_recording', { method: 'POST' }); }
                catch (e) { msg('Stop failed: ' + e.message); }
                exitRecordingUI('Recording stopped & saved.');
                return;
            }
            try {
                const r = await postJSON('/start_recording', {});
                const d = await r.json();
                enterRecordingUI(d.duration === undefined ? null : d.duration);
                msg('Recording ' + d.filename);
            } catch (e) { msg('Recording failed: ' + e.message); }
        }
        recordBtn.addEventListener('click', toggleRecording);
        if (mobileRecordBtn) mobileRecordBtn.addEventListener('click', toggleRecording);

        // Local 1s countdown for smoothness; server poll corrects/detects end.
        setInterval(() => {
            if (!isRecording) return;
            if (recRemaining != null) recRemaining = Math.max(0, recRemaining - 1);
            renderTimer();
        }, 1000);

        async function pollRecording() {
            try {
                const r = await request('/recording_status');
                const d = await r.json();
                if (d.recording && !isRecording) {
                    enterRecordingUI(d.remaining == null ? null : d.remaining);
                } else if (d.recording && isRecording) {
                    if (d.remaining != null) recRemaining = d.remaining;
                } else if (!d.recording && isRecording) {
                    exitRecordingUI('Recording saved.');
                }
            } catch (e) { /* transient */ }
        }
        setInterval(pollRecording, 2000);
        pollRecording();

        // ============================================================
        //  Camera controls (rows: slider <-> editable number + arrows)
        // ============================================================
        const camMap = {
            redGain: 'red_gain', greenGain: 'green_gain', blueGain: 'blue_gain',
            colourGain: 'colour_gain', analogueGain: 'analogue_gain',
            camContrast: 'contrast', camSaturation: 'saturation',
            camBrightness: 'brightness', camSharpness: 'sharpness',
        };

        let camDebounce = null;
        function sendCameraControls() {
            clearTimeout(camDebounce);
            camDebounce = setTimeout(() => {
                const body = {};
                for (const id in camMap) body[camMap[id]] = parseFloat($(id).value);
                postJSON('/set_camera_controls', body).catch(e => msg(e.message));
            }, 80);
        }

        async function sendFramerate(fps) {
            try {
                const r = await postJSON('/set_framerate', { fps: parseInt(fps, 10) });
                const d = await r.json();
                // The server derives exposure + brightness gain; reflect the gain.
                setControlValue('analogueGain', d.analogue_gain, false);
            } catch (e) { msg(e.message); }
        }

        function decimals(step) { const p = String(step).split('.')[1]; return p ? p.length : 0; }
        function setControlValue(id, value, send) {
            const sl = $(id), inp = $(id + 'Val');
            const min = parseFloat(sl.min), max = parseFloat(sl.max), step = parseFloat(sl.step) || 1;
            let v = Math.min(max, Math.max(min, parseFloat(value)));
            v = parseFloat(v.toFixed(decimals(step)));
            sl.value = v; inp.value = v;
            if (send) (id === 'framerate' ? sendFramerate(v) : sendCameraControls());
        }
        function wireControl(id) {
            const sl = $(id), inp = $(id + 'Val');
            sl.addEventListener('input', () => {
                inp.value = sl.value;
                id === 'framerate' ? sendFramerate(sl.value) : sendCameraControls();
            });
            inp.addEventListener('change', () => setControlValue(id, inp.value, true));
            document.querySelectorAll('.cam-step[data-slider="' + id + '"]').forEach(b => {
                b.addEventListener('click', () => {
                    const step = parseFloat(sl.step) || 1;
                    setControlValue(id, parseFloat(sl.value) + step * parseInt(b.dataset.dir, 10), true);
                });
            });
        }
        ['framerate', ...Object.keys(camMap)].forEach(wireControl);

        async function syncCameraControls() {
            try {
                const r = await request('/get_camera_controls');
                const cam = await r.json();
                setControlValue('framerate', cam.framerate, false);
                for (const id in camMap) setControlValue(id, cam[camMap[id]], false);
            } catch (e) { /* non-fatal */ }
        }

        // ============================================================
        //  Calibration (autofocus / white balance)
        // ============================================================
        const autofocusBtn = $('autofocusBtn');
        const whiteBalanceBtn = $('whiteBalanceBtn');

        async function waitForCalibration(timeoutSec = 120) {
            for (let i = 0; i < timeoutSec; i++) {
                await new Promise(r => setTimeout(r, 1000));
                try {
                    const r = await request('/calibration_status');
                    if (!(await r.json()).running) return true;
                } catch (e) { /* keep polling */ }
            }
            return false;
        }
        async function runCalibration(endpoint, button, label) {
            autofocusBtn.disabled = true; whiteBalanceBtn.disabled = true;
            button.classList.add('busy');
            msg(label + ' started…');
            try {
                const resp = await fetch(endpoint, { method: 'POST' });
                const data = await resp.json();
                if (!resp.ok || data.error) throw new Error(data.error || resp.statusText);
                const finished = await waitForCalibration();
                await syncCameraControls();
                msg(finished ? label + ' complete.' : label + ' is taking unusually long — check the server log.');
            } catch (e) {
                msg(label + ' failed: ' + e.message);
            } finally {
                autofocusBtn.disabled = false; whiteBalanceBtn.disabled = false;
                button.classList.remove('busy');
            }
        }
        autofocusBtn.addEventListener('click', () => runCalibration('/autofocus', autofocusBtn, 'Autofocus'));
        whiteBalanceBtn.addEventListener('click', () => runCalibration('/white_balance', whiteBalanceBtn, 'White balance'));

        // ============================================================
        //  Status
        // ============================================================
        async function refreshStatus() {
            try {
                const r = await request('/status');
                const d = await r.json();
                const txt = d.controller_connected ? 'Controller online' : 'Controller unavailable';
                statusEl.textContent = txt;
                statusEl.className = 'status ' + (d.controller_connected ? 'ok' : 'bad');
                if (mobileStatus) mobileStatus.textContent = txt;
            } catch (e) {
                statusEl.textContent = 'Offline';
                statusEl.className = 'status bad';
                if (mobileStatus) mobileStatus.textContent = 'Offline';
            }
        }
        refreshStatus();
        setInterval(refreshStatus, 5000);

        // ============================================================
        //  Mobile mode
        // ============================================================
        const mobileToggle = $('mobileToggle');
        const mobileExit = $('mobileExit');
        function enterMobile() {
            document.body.classList.add('mobile-mode');
            const el = document.documentElement;
            if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
            if (screen.orientation && screen.orientation.lock) screen.orientation.lock('landscape').catch(() => {});
        }
        function exitMobile() {
            document.body.classList.remove('mobile-mode');
            if (document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(() => {});
            if (screen.orientation && screen.orientation.unlock) { try { screen.orientation.unlock(); } catch (_) {} }
        }
        mobileToggle.addEventListener('click', enterMobile);
        mobileExit.addEventListener('click', exitMobile);
