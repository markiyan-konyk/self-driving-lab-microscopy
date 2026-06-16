        const messageDiv = document.getElementById('message');
        const statusEl = document.getElementById('status');
        const trackInfo = document.getElementById('trackInfo');
        const trackCanvas = document.getElementById('trackCanvas');
        const ctx = trackCanvas.getContext('2d');

        const canvasWidth = 600;
        const canvasHeight = 200;
        trackCanvas.width = canvasWidth;
        trackCanvas.height = canvasHeight;

        let trackingEnabled = true;
        let currentCircle = null;

        function drawAxes() {
            ctx.fillStyle = 'white';
            ctx.fillRect(0, 0, canvasWidth, canvasHeight);
            ctx.strokeStyle = 'black';
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.moveTo(0, canvasHeight/2);
            ctx.lineTo(canvasWidth, canvasHeight/2);
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(canvasWidth-10, canvasHeight/2);
            ctx.lineTo(canvasWidth-3, canvasHeight/2-3);
            ctx.lineTo(canvasWidth-3, canvasHeight/2+3);
            ctx.fillStyle = 'black';
            ctx.fill();
            ctx.beginPath();
            ctx.moveTo(canvasWidth/2, 0);
            ctx.lineTo(canvasWidth/2, canvasHeight);
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(canvasWidth/2, 10);
            ctx.lineTo(canvasWidth/2-3, 3);
            ctx.lineTo(canvasWidth/2+3, 3);
            ctx.fill();
            ctx.fillStyle = 'black';
            ctx.font = '12px sans-serif';
            ctx.fillText('X', canvasWidth-15, canvasHeight/2-5);
            ctx.fillText('Y', canvasWidth/2+8, 15);
            ctx.fillStyle = 'gray';
            ctx.beginPath();
            ctx.arc(canvasWidth/2, canvasHeight/2, 2, 0, 2*Math.PI);
            ctx.fill();
        }

        function plotCircle(x, y, r) {
            const px = (x / 640) * canvasWidth;
            const py = (y / 480) * canvasHeight;
            ctx.fillStyle = 'red';
            ctx.beginPath();
            ctx.arc(px, py, 6, 0, 2*Math.PI);
            ctx.fill();
            ctx.strokeStyle = 'red';
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.arc(px, py, 10, 0, 2*Math.PI);
            ctx.stroke();
            console.log(`Plotted point at (${px.toFixed(1)}, ${py.toFixed(1)})`);
        }

        const evtSource = new EventSource('/tracking_stream');
        evtSource.onmessage = function(event) {
            const data = JSON.parse(event.data);
            console.log("SSE received:", data);
            if (data.circle) {
                const [cx, cy, r] = data.circle;
                console.log(`Circle detected: (${cx}, ${cy}) radius ${r}`);
                trackInfo.textContent = `Center: (${cx.toFixed(1)}, ${cy.toFixed(1)})  Radius: ${r.toFixed(1)} px`;
                if (trackingEnabled) {
                    currentCircle = {x: cx, y: cy, r: r};
                    drawAxes();
                    plotCircle(cx, cy, r);
                }
            } else {
                trackInfo.textContent = 'No circle detected';
                if (trackingEnabled) {
                    currentCircle = null;
                    drawAxes();
                }
            }
        };

        const toggleBtn = document.getElementById('trackToggleBtn');
        toggleBtn.onclick = async () => {
            trackingEnabled = !trackingEnabled;
            toggleBtn.textContent = trackingEnabled ? 'Tracking ON' : 'Tracking OFF';
            // Always tell the server, so detection actually stops when toggled
            // off (it used to only be notified when re-enabling).
            fetch('/set_tracking', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled: trackingEnabled})
            });
            if (!trackingEnabled) {
                drawAxes();
                trackInfo.textContent = 'Tracking disabled';
                currentCircle = null;
            }
        };

        const slider = document.getElementById('trackIntervalSlider');
        const intervalSpan = document.getElementById('intervalValue');
        slider.oninput = () => {
            const val = slider.value;
            intervalSpan.textContent = val;
            fetch('/set_tracking_interval', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({interval_ms: parseInt(val)})
            });
        };

        drawAxes();

        // ===== Camera controls =====
        let camDebounce = null;
        function sendCameraControls() {
            clearTimeout(camDebounce);
            camDebounce = setTimeout(() => {
                fetch('/set_camera_controls', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        red_gain: parseFloat(document.getElementById('redGain').value),
                        blue_gain: parseFloat(document.getElementById('blueGain').value),

                        green_gain: parseFloat(document.getElementById('greenGain').value),

                        exposure: parseInt(document.getElementById('exposure').value),
                        colour_gain: parseFloat(document.getElementById('colourGain').value),
                        analogue_gain: parseFloat(document.getElementById('analogueGain').value),
                        contrast: parseFloat(document.getElementById('camContrast').value),
                        saturation: parseFloat(document.getElementById('camSaturation').value),
                        brightness: parseFloat(document.getElementById('camBrightness').value),
                        sharpness: parseFloat(document.getElementById('camSharpness').value),
                    })
                });
            }, 100);
        }

        const camSliders = [
            ['redGain', 'redGainVal'],
            ['blueGain', 'blueGainVal'],
            ['greenGain',   'greenGainVal'],
            ['exposure', 'exposureVal'],
            ['colourGain', 'colourGainVal'],
            ['analogueGain', 'analogueGainVal'],
        ];
        camSliders.forEach(([sliderId, valId]) => {
            const sl = document.getElementById(sliderId);
            const vl = document.getElementById(valId);
            sl.oninput = () => {
                vl.textContent = sl.value;
                sendCameraControls();
            };
        });

        document.querySelectorAll('.cam-step').forEach(btn => {
            btn.onclick = () => {
                const sl = document.getElementById(btn.dataset.slider);
                const dir = parseInt(btn.dataset.dir);
                const step = parseFloat(sl.step);
                let val = parseFloat(sl.value) + step * dir;
                val = Math.max(parseFloat(sl.min), Math.min(parseFloat(sl.max), val));
                val = Math.round(val * 1000) / 1000;
                sl.value = val;
                document.getElementById(btn.dataset.slider + 'Val').textContent = val;
                sendCameraControls();
            };
        });

        ['camContrast', 'camSaturation', 'camBrightness', 'camSharpness'].forEach(id => {
            document.getElementById(id).oninput = () => sendCameraControls();
        });

        async function request(path, options={}) {
            const response = await fetch(path, { cache: 'no-store', ...options });
            if (!response.ok) throw new Error(await response.text() || response.statusText);
            return response;
        }

        async function move(dir) {
            try { await request('/move/' + dir); messageDiv.textContent = ''; }
            catch(e) { messageDiv.textContent = e.message; }
        }
        async function adjust(axis, op) {
            try {
                const resp = await request('/adjust/' + axis + '/' + op);
                const val = await resp.text();
                document.getElementById(axis + '_value').innerText = val;
                messageDiv.textContent = '';
            } catch(e) { messageDiv.textContent = e.message; }
        }

        let curDur = {{ record_duration }};
        let curFps = {{ record_framerate }};
        const durSpan = document.getElementById('durDisplay');
        const fpsSpan = document.getElementById('fpsDisplay');

        async function updateSetting(setting, value) {
            await request('/set_recording_setting', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ setting, value })
            });
        }

        document.getElementById('recDurDown').onclick = async () => {
            let newVal = Math.max(1, curDur - 1);
            curDur = newVal;
            durSpan.innerText = curDur;
            await updateSetting('duration', curDur);
        };
        document.getElementById('recDurUp').onclick = async () => {
            let newVal = curDur + 1;
            curDur = newVal;
            durSpan.innerText = curDur;
            await updateSetting('duration', curDur);
        };
        document.getElementById('recFpsDown').onclick = async () => {
            let newVal = Math.max(1, curFps - 10);
            curFps = newVal;
            fpsSpan.innerText = curFps;
            await updateSetting('framerate', curFps);
        };
        document.getElementById('recFpsUp').onclick = async () => {
            let newVal = curFps + 10;
            curFps = newVal;
            fpsSpan.innerText = curFps;
            await updateSetting('framerate', curFps);
        };

        // The /start_recording route returns immediately while the recording
        // runs in a background thread, so the button doubles as a Stop button
        // (via /stop_recording) until the duration elapses.
        const recordBtn = document.getElementById('recordBtn');
        let isRecording = false;
        let recordTimer = null;

        function recordingDone(msg) {
            isRecording = false;
            clearTimeout(recordTimer);
            recordTimer = null;
            recordBtn.textContent = '🎥 Record';
            messageDiv.textContent = msg;
        }

        recordBtn.onclick = async () => {
            if (isRecording) {
                try {
                    await request('/stop_recording', { method: 'POST' });
                    recordingDone('Recording stopped early.');
                } catch(e) {
                    messageDiv.textContent = `Stop failed: ${e.message}`;
                }
                return;
            }
            try {
                const resp = await request('/start_recording', { method: 'POST' });
                const result = await resp.json();
                isRecording = true;
                recordBtn.textContent = '⏹ Stop';
                messageDiv.textContent = `Recording ${result.filename} (${curDur}s)...`;
                recordTimer = setTimeout(
                    () => recordingDone(`Recording saved: ${result.filename}`),
                    curDur * 1000 + 1500
                );
            } catch(e) {
                messageDiv.textContent = `Recording failed: ${e.message}`;
            }
        };

        document.querySelectorAll('[data-move]').forEach(btn => btn.addEventListener('click', () => move(btn.dataset.move)));
        document.querySelectorAll('[data-adjust]').forEach(btn => {
            const [axis, op] = btn.dataset.adjust.split(':');
            btn.addEventListener('click', () => adjust(axis, op));
        });

        document.addEventListener('keydown', (event) => {
            const keyMap = { ArrowUp:'up', ArrowDown:'down', ArrowLeft:'left', ArrowRight:'right', PageUp:'page_up', PageDown:'page_down' };
            if (event.repeat || !(event.key in keyMap)) return;
            event.preventDefault();
            move(keyMap[event.key]);
        });

        async function refreshStatus() {
            try {
                const resp = await request('/status');
                const data = await resp.json();
                statusEl.textContent = data.controller_connected ? 'Controller online' : 'Controller unavailable';
            } catch(e) { statusEl.textContent = 'Offline'; }
        }
        refreshStatus();
        setInterval(refreshStatus, 5000);

        const autofocusBtn = document.getElementById('autofocusBtn');
        const whiteBalanceBtn = document.getElementById('whiteBalanceBtn');

        // The /autofocus and /white_balance routes return immediately while a
        // worker thread runs, so poll /calibration_status to know when the
        // calibration actually finished.
        async function waitForCalibration(timeoutSec = 120) {
            for (let i = 0; i < timeoutSec; i++) {
                await new Promise(r => setTimeout(r, 1000));
                try {
                    const resp = await request('/calibration_status');
                    const data = await resp.json();
                    if (!data.running) return true;
                } catch (e) { /* transient error: keep polling */ }
            }
            return false;
        }

        // Pull cam_controls from the server and update every slider/input so
        // calibrated gains aren't overwritten by stale UI values on the next
        // slider touch.
        async function syncCameraControls() {
            const resp = await request('/get_camera_controls');
            const cam = await resp.json();
            const sliderMap = {
                redGain: 'red_gain',
                greenGain: 'green_gain',
                blueGain: 'blue_gain',
                exposure: 'exposure',
                colourGain: 'colour_gain',
                analogueGain: 'analogue_gain',
            };
            for (const [id, key] of Object.entries(sliderMap)) {
                const val = (id === 'exposure') ? Math.round(cam[key]) : Math.round(cam[key] * 100) / 100;
                document.getElementById(id).value = val;
                document.getElementById(id + 'Val').textContent = val;
            }
            const numMap = {
                camContrast: 'contrast',
                camSaturation: 'saturation',
                camBrightness: 'brightness',
                camSharpness: 'sharpness',
            };
            for (const [id, key] of Object.entries(numMap)) {
                document.getElementById(id).value = cam[key];
            }
        }

        async function runCalibration(endpoint, button, message) {
            autofocusBtn.disabled = true;
            whiteBalanceBtn.disabled = true;
            const originalText = button.textContent;
            button.textContent = message + '...';
            messageDiv.textContent = message + ' started...';
            try {
                const resp = await fetch(endpoint, { method: 'POST' });
                const data = await resp.json();
                if (!resp.ok || data.error) {
                    throw new Error(data.error || resp.statusText);
                }
                const finished = await waitForCalibration();
                try { await syncCameraControls(); } catch (e) { /* non-fatal */ }
                messageDiv.textContent = finished
                    ? message + ' completed.'
                    : message + ' is taking unusually long - check the server log.';
            } catch (e) {
                messageDiv.textContent = message + ' failed: ' + e.message;
            } finally {
                autofocusBtn.disabled = false;
                whiteBalanceBtn.disabled = false;
                button.textContent = originalText;
            }
        }

        autofocusBtn.onclick = () => runCalibration('/autofocus', autofocusBtn, 'Autofocus');
        whiteBalanceBtn.onclick = () => runCalibration('/white_balance', whiteBalanceBtn, 'White balance');
