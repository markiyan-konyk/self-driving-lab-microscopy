import os
import secrets
import threading
import time
import csv
import json
import gc
from collections import deque
from functools import wraps

import cv2
import numpy as np
from flask import Flask, Response, jsonify, redirect, render_template_string, request, session, url_for
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput
from sangaboard import Sangaboard
from simplejpeg import encode_jpeg

try:
    from pynput import keyboard
    KEYBOARD_IMPORT_ERROR = None
except Exception as exc:
    keyboard = None
    KEYBOARD_IMPORT_ERROR = exc

app = Flask(__name__)
app.secret_key = os.environ.get("MICROSCOPE_SESSION_SECRET") or secrets.token_hex(32)
#MICROSCOPE_PASSWORD = os.environ.get("MICROSCOPE_PASSWORD")
MICROSCOPE_PASSWORD = "password"


# ========== Global variables ==========
sb = None
controller_error = None
move_lock = threading.Lock()

steps = {"x": 40, "y": 40, "z": 40}

# Recording settings
record_duration = 10
record_framerate = 100
server = 8000

# Detection settings
STREAM_FPS = 30
DETECTION_FPS = 10
FRAME_SKIP = STREAM_FPS // DETECTION_FPS

tracking_enabled = True
tracking_interval_ms = 100
tracking_interval_sec = 0.1

# Camera
picam2 = None
camera_running = False
camera_lock = threading.Lock()

# Camera controls
cam_controls = {
    "red_gain": 1.5,
    "blue_gain": 3.0,
    "exposure": 10000,
    "analogue_gain": 1.0,
    "colour_gain": 1.0,
    "contrast": 1.0,
    "saturation": 1.0,
    "brightness": 0.0,
    "sharpness": 1.0,
}

# Tracking data
current_circle = None
last_tracking_time = 0
current_jpeg = None

class ReliableSSEClient:
    def __init__(self):
        self.latest = None
        self.available = False
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)

    def put(self, data):
        with self.cond:
            self.latest = data
            self.available = True
            self.cond.notify()

    def get(self):
        with self.cond:
            while not self.available:
                self.cond.wait()
            data = self.latest
            self.available = False
            return data

tracking_clients = []
tracking_lock = threading.Lock()

# Recording state
is_recording = False
recording_coordinates = []
current_recording_filename = None

# ========== HTML templates (FULL) ==========
LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Microscope Login</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root { color-scheme: light dark; font-family: Inter, sans-serif; background: #111827; color: #f8fafc; }
        * { box-sizing: border-box; }
        body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 18px; background: #111827; }
        .login { width: min(100%, 380px); display: flex; flex-direction: column; gap: 16px; padding: 22px; border: 1px solid #374151; border-radius: 8px; background: #161f2f; }
        h1 { margin: 0; font-size: 22px; font-weight: 700; }
        label { display: flex; flex-direction: column; gap: 8px; color: #cbd5e1; font-size: 13px; text-transform: uppercase; }
        input { width: 100%; min-height: 46px; border: 1px solid #475569; border-radius: 8px; background: #0f172a; color: #f8fafc; font: inherit; font-size: 18px; padding: 9px 11px; outline: none; }
        button { min-height: 46px; border: 1px solid #2563eb; border-radius: 8px; background: #1d4ed8; color: #f8fafc; font-weight: 700; cursor: pointer; }
        button:hover { background: #2563eb; }
        .error { min-height: 20px; margin: 0; color: #fca5a5; font-size: 14px; }
    </style>
</head>
<body>
    <form class="login" method="post" action="{{ url_for('login') }}">
        <h1>Microscope</h1>
        <label>Password <input name="password" type="password" autocomplete="current-password" autofocus required></label>
        <button type="submit">Unlock</button>
        <p class="error">{{ error }}</p>
    </form>
</body>
</html>
"""

HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Microscope Control with Tracking</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root { color-scheme: light dark; font-family: Inter, sans-serif; background: #111827; color: #f8fafc; }
        * { box-sizing: border-box; }
        body { margin: 0; min-height: 100vh; background: #111827; }
        main { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 18px; min-height: 100vh; padding: 18px; }
        .camera-pane { min-width: 0; display: flex; flex-direction: column; gap: 10px; }
        .topbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
        h1 { margin: 0; font-size: 18px; font-weight: 650; }
        .status { display: inline-flex; align-items: center; min-height: 28px; padding: 4px 9px; border-radius: 7px; background: #1f2937; color: #d1d5db; font-size: 13px; white-space: nowrap; }
        .video-frame { position: relative; flex: 1; min-height: 360px; overflow: hidden; border: 1px solid #374151; border-radius: 8px; background: #030712; }
        .video-frame img { width: 100%; height: 100%; object-fit: contain; display: block; }
        .plot-frame { margin-top: 10px; border: 1px solid #374151; border-radius: 8px; background: #0f172a; padding: 10px; }
        .plot-canvas { width: 100%; height: 200px; background: white; }
        .panel { display: flex; flex-direction: column; gap: 18px; padding: 14px; border: 1px solid #374151; border-radius: 8px; background: #161f2f; overflow-y: auto; max-height: 100vh; }
        .section-title { margin: 0 0 10px; color: #cbd5e1; font-size: 13px; font-weight: 650; text-transform: uppercase; }
        .jog { display: grid; grid-template-columns: repeat(3, 74px); grid-template-rows: repeat(3, 58px); gap: 8px; justify-content: center; }
        button { border: 1px solid #475569; border-radius: 8px; background: #263244; color: #f8fafc; font: inherit; font-weight: 650; cursor: pointer; touch-action: manipulation; }
        button:hover { background: #334155; }
        button:active { transform: translateY(1px); background: #1d4ed8; }
        .jog button { font-size: 25px; }
        .up { grid-column: 2; grid-row: 1; }
        .left { grid-column: 1; grid-row: 2; }
        .down { grid-column: 2; grid-row: 2; }
        .right { grid-column: 3; grid-row: 2; }
        .z-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
        .z-grid button { min-height: 48px; }
        .steps { display: grid; grid-template-columns: 28px 1fr auto 1fr; gap: 8px; align-items: center; }
        .steps span { color: #e5e7eb; font-variant-numeric: tabular-nums; text-align: center; min-width: 40px; }
        .steps button { min-height: 38px; }
        .recording-controls { margin-top: 8px; border-top: 1px solid #374151; padding-top: 12px; }
        .recording-controls .flex-row { display: flex; align-items: center; gap: 8px; margin-top: 8px; justify-content: space-between; }
        .tracking-controls { margin-top: 8px; border-top: 1px solid #374151; padding-top: 12px; }
        .tracking-controls .flex-row { display: flex; align-items: center; gap: 8px; margin-top: 8px; justify-content: space-between; }
        .camera-controls { margin-top: 8px; border-top: 1px solid #374151; padding-top: 12px; }
        .cam-row { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
        .cam-row span:first-child { min-width: 55px; color: #cbd5e1; font-size: 13px; }
        .cam-row input[type=range] { flex: 1; }
        .cam-row .cam-val { min-width: 55px; text-align: right; font-variant-numeric: tabular-nums; font-size: 13px; color: #e5e7eb; }
        .cam-step { min-height: 24px; width: 26px; padding: 0; font-size: 11px; line-height: 1; border-radius: 5px; }
        .num-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 8px; }
        .num-box { display: flex; flex-direction: column; gap: 4px; }
        .num-box span { color: #cbd5e1; font-size: 11px; text-transform: uppercase; text-align: center; }
        .num-box input { width: 100%; min-height: 36px; border: 1px solid #475569; border-radius: 6px; background: #0f172a; color: #f8fafc; font: inherit; font-size: 14px; padding: 4px 6px; text-align: center; outline: none; }
        .message { min-height: 20px; color: #93c5fd; font-size: 13px; }
        .logout { display: inline-flex; align-items: center; justify-content: center; min-height: 28px; padding: 4px 9px; border: 1px solid #475569; border-radius: 7px; color: #d1d5db; font-size: 13px; text-decoration: none; }
        .logout:hover { background: #263244; color: #f8fafc; }
        @media (max-width: 820px) {
            main { grid-template-columns: 1fr; padding: 12px; }
            .video-frame { min-height: 46vh; }
        }
    </style>
</head>
<body>
    <main>
        <section class="camera-pane">
            <div class="topbar">
                <h1>Microscope</h1>
                <span class="status" id="status">Connecting</span>
                <a class="logout" href="/logout">Lock</a>
            </div>
            <div class="video-frame">
                <img src="/video_feed" alt="Microscope camera stream" id="stream">
            </div>
            <div class="plot-frame">
                <canvas id="trackCanvas" width="600" height="200" style="width:100%; height:200px; background:white; border:1px solid #ccc;"></canvas>
            </div>
        </section>

        <aside class="panel">
            <section>
                <p class="section-title">XY Jog</p>
                <div class="jog">
                    <button class="up" data-move="up">↑</button>
                    <button class="left" data-move="left">←</button>
                    <button class="down" data-move="down">↓</button>
                    <button class="right" data-move="right">→</button>
                </div>
            </section>
            <section>
                <p class="section-title">Focus</p>
                <div class="z-grid">
                    <button data-move="page_up">Z+</button>
                    <button data-move="page_down">Z-</button>
                </div>
            </section>
            <section>
                <p class="section-title">Step Size</p>
                <div class="steps">
                    <strong>X</strong>
                    <button data-adjust="x:dec">-5</button>
                    <span id="x_value">{{ steps.x }}</span>
                    <button data-adjust="x:inc">+5</button>
                    <strong>Y</strong>
                    <button data-adjust="y:dec">-5</button>
                    <span id="y_value">{{ steps.y }}</span>
                    <button data-adjust="y:inc">+5</button>
                    <strong>Z</strong>
                    <button data-adjust="z:dec">-5</button>
                    <span id="z_value">{{ steps.z }}</span>
                    <button data-adjust="z:inc">+5</button>
                </div>
            </section>

            <section class="recording-controls">
                <p class="section-title">Recordings</p>
                <div class="flex-row">
                    <button id="recDurDown">-1s</button>
                    <span id="durDisplay">10</span><span> s</span>
                    <button id="recDurUp">+1s</button>
                </div>
                <div class="flex-row">
                    <button id="recFpsDown">-10fps</button>
                    <span id="fpsDisplay">100</span><span> fps</span>
                    <button id="recFpsUp">+10fps</button>
                </div>
                <div class="flex-row">
                    <button id="recordBtn" style="background:#2563eb; width:100%;">🎥 Record</button>
                </div>
            </section>

            <section class="tracking-controls">
                <p class="section-title">Particle Tracking</p>
                <div class="flex-row">
                    <button id="trackToggleBtn" style="background:#2563eb;">Tracking ON</button>
                </div>
                <div class="flex-row">
                    <span>Log interval (ms)</span>
                    <input type="range" id="trackIntervalSlider" min="50" max="500" step="50" value="100">
                    <span id="intervalValue">100</span><span> ms</span>
                </div>
                <div class="message" id="trackInfo">No circle detected</div>
            </section>

            <section class="camera-controls">
                <p class="section-title">White Balance</p>
                <div class="cam-row">
                    <span>Red</span>
                    <input type="range" id="redGain" min="0" max="8" step="0.1" value="{{ cam.red_gain }}">
                    <span class="cam-val" id="redGainVal">{{ cam.red_gain }}</span>
                    <button class="cam-step" data-slider="redGain" data-dir="-1">◀</button>
                    <button class="cam-step" data-slider="redGain" data-dir="1">▶</button>
                </div>
                <div class="cam-row">
                    <span>Blue</span>
                    <input type="range" id="blueGain" min="0" max="8" step="0.1" value="{{ cam.blue_gain }}">
                    <span class="cam-val" id="blueGainVal">{{ cam.blue_gain }}</span>
                    <button class="cam-step" data-slider="blueGain" data-dir="-1">◀</button>
                    <button class="cam-step" data-slider="blueGain" data-dir="1">▶</button>
                </div>
            </section>

            <section class="camera-controls">
                <p class="section-title">Exposure</p>
                <div class="cam-row">
                    <span>Time µs</span>
                    <input type="range" id="exposure" min="100" max="100000" step="100" value="{{ cam.exposure }}">
                    <span class="cam-val" id="exposureVal">{{ cam.exposure|int }}</span>
                    <button class="cam-step" data-slider="exposure" data-dir="-1">◀</button>
                    <button class="cam-step" data-slider="exposure" data-dir="1">▶</button>
                </div>
            </section>

            <section class="camera-controls">
                <p class="section-title">Gains</p>
                <div class="cam-row">
                    <span>Colour</span>
                    <input type="range" id="colourGain" min="0.1" max="4" step="0.1" value="{{ cam.colour_gain }}">
                    <span class="cam-val" id="colourGainVal">{{ cam.colour_gain }}</span>
                    <button class="cam-step" data-slider="colourGain" data-dir="-1">◀</button>
                    <button class="cam-step" data-slider="colourGain" data-dir="1">▶</button>
                </div>
                <div class="cam-row">
                    <span>Analogue</span>
                    <input type="range" id="analogueGain" min="1" max="16" step="0.1" value="{{ cam.analogue_gain }}">
                    <span class="cam-val" id="analogueGainVal">{{ cam.analogue_gain }}</span>
                    <button class="cam-step" data-slider="analogueGain" data-dir="-1">◀</button>
                    <button class="cam-step" data-slider="analogueGain" data-dir="1">▶</button>
                </div>
            </section>

            <section class="camera-controls">
                <p class="section-title">Image Processing</p>
                <div class="num-row">
                    <div class="num-box">
                        <span>Contrast</span>
                        <input type="number" id="camContrast" value="{{ cam.contrast }}" step="0.1" min="0" max="4">
                    </div>
                    <div class="num-box">
                        <span>Saturation</span>
                        <input type="number" id="camSaturation" value="{{ cam.saturation }}" step="0.1" min="0" max="4">
                    </div>
                    <div class="num-box">
                        <span>Brightness</span>
                        <input type="number" id="camBrightness" value="{{ cam.brightness }}" step="0.05" min="-1" max="1">
                    </div>
                    <div class="num-box">
                        <span>Sharpness</span>
                        <input type="number" id="camSharpness" value="{{ cam.sharpness }}" step="0.1" min="0" max="8">
                    </div>
                </div>
            </section>

            <div class="message" id="message"></div>
        </aside>
    </main>

    <script>
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
            const py = canvasHeight - (y / 480) * canvasHeight;
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
            if (!trackingEnabled) {
                drawAxes();
        
        trackInfo.textContent = 'Tracking disabled';
        currentCircle = null;
    } else {
        fetch('/set_tracking', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: true})
        });
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

        document.getElementById('recordBtn').onclick = async () => {
            const btn = document.getElementById('recordBtn');
            btn.disabled = true;
            btn.textContent = 'Recording...';
            messageDiv.textContent = 'Recording started...';
            try {
                const resp = await request('/start_recording', { method: 'POST' });
                const result = await resp.json();
                messageDiv.textContent = `Recording saved: ${result.filename}`;
            } catch(e) {
                messageDiv.textContent = `Recording failed: ${e.message}`;
            } finally {
                btn.disabled = false;
                btn.textContent = '🎥 Record';
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
    </script>
</body>
</html>
"""

# ========== Authentication ==========
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("authenticated"):
            return view(*args, **kwargs)
        return redirect(url_for("login"))
    return wrapped

@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if MICROSCOPE_PASSWORD and secrets.compare_digest(pwd, MICROSCOPE_PASSWORD):
            session.clear()
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "Incorrect password" if MICROSCOPE_PASSWORD else "Password not configured"
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    return render_template_string(HTML, steps=steps, record_duration=record_duration, record_framerate=record_framerate, cam=cam_controls)

@app.route("/status")
@login_required
def status():
    return jsonify({
        "controller_connected": sb is not None,
        "controller_error": controller_error,
        "steps": steps,
    })

# ========== Motor control ==========
@app.route("/move/<direction>")
@login_required
def move(direction):
    dir_map = {
        "left": [steps["x"], 0, 0],
        "right": [-steps["x"], 0, 0],
        "up": [0, steps["y"], 0],
        "down": [0, -steps["y"], 0],
        "page_up": [0, 0, steps["z"]],
        "page_down": [0, 0, -steps["z"]],
    }
    if direction not in dir_map:
        return "Unknown direction", 404
    if sb is None:
        return "Sangaboard unavailable", 503
    with move_lock:
        sb.move_rel(dir_map[direction])
    return "OK"

@app.route("/adjust/<axis>/<op>")
@login_required
def adjust(axis, op):
    if axis not in steps:
        return "Unknown axis", 404
    if op == "inc":
        steps[axis] += 5
    elif op == "dec":
        steps[axis] = max(1, steps[axis] - 5)
    else:
        return "Unknown op", 400
    return str(steps[axis])

# ========== Tracking settings ==========
@app.route("/set_tracking", methods=["POST"])
@login_required
def set_tracking():
    data = request.get_json()
    global tracking_enabled
    tracking_enabled = data.get("enabled", True)
    return "OK"

@app.route("/set_tracking_interval", methods=["POST"])
@login_required
def set_tracking_interval():
    data = request.get_json()
    global tracking_interval_ms, tracking_interval_sec
    tracking_interval_ms = data.get("interval_ms", 100)
    tracking_interval_sec = tracking_interval_ms / 1000.0
    return "OK"

# ========== Recording settings ==========
@app.route("/set_recording_setting", methods=["POST"])
@login_required
def set_recording_setting():
    data = request.get_json()
    setting = data.get("setting")
    value = data.get("value")
    global record_duration, record_framerate
    if setting == "duration":
        record_duration = max(1, int(value))
    elif setting == "framerate":
        record_framerate = max(1, int(value))
    else:
        return "Invalid setting", 400
    return "OK"

# ========== Camera controls ==========
def apply_camera_controls():
    if picam2 is None:
        return
    cg = cam_controls["colour_gain"]
    picam2.set_controls({
        "AwbEnable": False,
        "AeEnable": False,
        "ColourGains": (cam_controls["red_gain"] * cg, cam_controls["blue_gain"] * cg),
        "ExposureTime": int(cam_controls["exposure"]),
        "AnalogueGain": cam_controls["analogue_gain"],
        "Contrast": cam_controls["contrast"],
        "Saturation": cam_controls["saturation"],
        "Brightness": cam_controls["brightness"],
        "Sharpness": cam_controls["sharpness"],
    })

@app.route("/set_camera_controls", methods=["POST"])
@login_required
def set_camera_controls():
    data = request.get_json()
    for key in cam_controls:
        if key in data:
            cam_controls[key] = float(data[key])
    with camera_lock:
        apply_camera_controls()
    return "OK"

# ========== Camera and tracking worker ==========
def start_camera():
    global picam2, camera_running
    with camera_lock:
        if picam2 is not None:
            return
        picam2 = Picamera2()
        config = picam2.create_video_configuration(
            main={"size": (640, 480), "format": "RGB888"},
            lores={"size": (320, 240), "format": "YUV420"},
            controls={"FrameRate": STREAM_FPS}
        )
        picam2.configure(config)
        picam2.start()
        camera_running = True
        time.sleep(0.5)
        apply_camera_controls()
        print(f"Camera started: {STREAM_FPS} fps, detection {DETECTION_FPS} fps (skip {FRAME_SKIP})")

def detect_circle(image_bgr_small):
    gray = cv2.cvtColor(image_bgr_small, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5,5), 1.5)
    circles = cv2.HoughCircles(blurred, cv2.HOUGH_GRADIENT, dp=1.2, minDist=40,
                              param1=50, param2=35, minRadius=10, maxRadius=80)
    if circles is not None:
        circles = np.round(circles[0, :]).astype(int)
        largest = max(circles, key=lambda c: c[2])
        return tuple(largest)
    return None

def tracking_worker():
    global current_circle, last_tracking_time, tracking_enabled
    global is_recording, recording_coordinates, current_jpeg

    frame_counter = 0
    frame_interval = 1.0 / STREAM_FPS
    next_frame_time = time.perf_counter()

    last_sse_time = 0.0
    last_jpeg_time = 0.0
    last_detected_circle = None

    while True:
        now = time.perf_counter()
        if now < next_frame_time:
            time.sleep(next_frame_time - now)
        next_frame_time = time.perf_counter() + frame_interval

        if not camera_running or picam2 is None:
            continue

        try:
            with camera_lock:
                frame_yuv = picam2.capture_array("lores")
            if frame_yuv is None:
                continue

            start_proc = time.perf_counter()
            frame_small = cv2.cvtColor(frame_yuv, cv2.COLOR_YUV2BGR_I420)

            circle_small = None
            frame_counter += 1
            if tracking_enabled and (frame_counter % FRAME_SKIP == 0):
                circle_small = detect_circle(frame_small)

            if circle_small is not None:
                last_detected_circle = circle_small

            if last_detected_circle is not None:
                x, y, r = last_detected_circle
                circle_display = (x*2, y*2, r*2)
            else:
                circle_display = None

            current_circle = circle_display

            if is_recording and circle_small is not None:
                now_sec = time.time()
                if now_sec - last_tracking_time >= tracking_interval_sec:
                    last_tracking_time = now_sec
                    timestamp_ms = int(now_sec * 1000)
                    x, y, r = circle_small
                    recording_coordinates.append([timestamp_ms, int(x), int(y), int(r)])

            frame_display = cv2.resize(frame_small, (640, 480))
            if circle_display is not None:
                xd, yd, rd = circle_display
                cv2.circle(frame_display, (xd, yd), rd, (0,0,255), 2)
                cv2.circle(frame_display, (xd, yd), 3, (0,0,255), -1)
                cv2.putText(frame_display, f"({xd},{yd}) r={rd}", (xd+10, yd-10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,0,255), 1)
            else:
                cv2.putText(frame_display, "No circle", (10,30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,0,255), 2)

            now_time = time.time()
            if now_time - last_jpeg_time >= (1/15):
                last_jpeg_time = now_time
                current_jpeg = encode_jpeg(frame_display, quality=60, colorspace='BGR')

            if now_time - last_sse_time >= 0.1:
                last_sse_time = now_time
                if last_detected_circle is not None:
                    x, y, r = last_detected_circle
                    circle_json = [int(x*2), int(y*2), int(r*2)]
                else:
                    circle_json = None
                data = json.dumps({"circle": circle_json})
                with tracking_lock:
                    for client in tracking_clients:
                        try:
                            client.put(data)
                        except Exception:
                            pass

            if frame_counter % 100 == 0:
                print(f"\n==== PERF ==== clients: {len(tracking_clients)} ============\n")

            if frame_counter % 300 == 0:
                gc.collect()

        except Exception as e:
            print(f"Tracking worker error: {e}")
            time.sleep(0.1)

# ========== SSE stream ==========
def event_stream():
    client = ReliableSSEClient()
    with tracking_lock:
        tracking_clients.append(client)
    try:
        while True:
            data = client.get()
            yield f"data: {data}\n\n"
    finally:
        with tracking_lock:
            if client in tracking_clients:
                tracking_clients.remove(client)

@app.route("/tracking_stream")
@login_required
def tracking_stream():
    headers = {
        'X-Accel-Buffering': 'no',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive'
    }
    return Response(event_stream(), mimetype='text/event-stream', headers=headers)

@app.route("/video_feed")
@login_required
def video_feed():
    def generate():
        global current_jpeg
        while True:
            if current_jpeg is None:
                time.sleep(0.02)
                continue
            jpeg = current_jpeg
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n'
                   b'Content-Length: ' + str(len(jpeg)).encode() + b'\r\n\r\n' +
                   jpeg + b'\r\n')
            time.sleep(1/15)
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

# ========== Recording ==========
def get_next_recording_index():
    rec_dir = "recordings"
    os.makedirs(rec_dir, exist_ok=True)
    existing = [f for f in os.listdir(rec_dir) if f.startswith("recording_") and f.endswith(".mp4")]
    numbers = []
    for f in existing:
        try:
            num = int(f.split('_')[1])
            numbers.append(num)
        except:
            pass
    if numbers:
        return max(numbers) + 1
    else:
        return 1

def start_recording_async(duration_sec, framerate_fps, output_path):
    global is_recording, recording_coordinates, current_recording_filename
    recording_coordinates = []
    current_recording_filename = os.path.splitext(os.path.basename(output_path))[0]
    is_recording = True
    with camera_lock:
        if picam2 is None:
            start_camera()
        picam2.set_controls({"FrameRate": framerate_fps})
        encoder = H264Encoder(bitrate=10000000)
        output = FfmpegOutput(output_path)
        picam2.start_encoder(encoder, output)
    time.sleep(duration_sec)
    with camera_lock:
        picam2.stop_encoder()
        picam2.set_controls({"FrameRate": STREAM_FPS})
    is_recording = False
    if recording_coordinates:
        csv_dir = "coordinates"
        os.makedirs(csv_dir, exist_ok=True)
        csv_filename = f"{current_recording_filename}.csv"
        csv_path = os.path.join(csv_dir, csv_filename)
        with open(csv_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp_ms", "x", "y", "radius"])
            writer.writerows(recording_coordinates)
        print(f"CSV saved: {csv_path}")

@app.route("/start_recording", methods=["POST"])
@login_required
def start_recording():
    global record_duration, record_framerate
    if record_framerate < 1 or record_duration < 1:
        return jsonify({"error": "Invalid parameters"}), 400
    rec_dir = "recordings"
    os.makedirs(rec_dir, exist_ok=True)
    idx = get_next_recording_index()
    filename = f"recording_{idx}_{record_framerate}fps_{record_duration}s.mp4"
    filepath = os.path.join(rec_dir, filename)
    thread = threading.Thread(target=start_recording_async,
                              args=(record_duration, record_framerate, filepath),
                              daemon=True)
    thread.start()
    return jsonify({"filename": filename})

# ========== Keyboard listener ==========
def start_keyboard_listener():
    if keyboard is None:
        print(f"Keyboard disabled: {KEYBOARD_IMPORT_ERROR}")
        return None
    pressed_keys = set()
    def rebuild_key_map():
        return {
            keyboard.Key.right: [-steps["x"], 0, 0],
            keyboard.Key.left: [steps["x"], 0, 0],
            keyboard.Key.up: [0, steps["y"], 0],
            keyboard.Key.down: [0, -steps["y"], 0],
            keyboard.Key.page_up: [0, 0, steps["z"]],
            keyboard.Key.page_down: [0, 0, -steps["z"]],
        }
    key_map_move = rebuild_key_map()
    def on_press(key):
        nonlocal key_map_move
        with move_lock:
            if key in key_map_move and sb is not None:
                sb.move_rel(key_map_move[key])
            if hasattr(key, "char") and key.char in ("x","y","z","=","-"):
                pressed_keys.add(key.char)
            adjusted = False
            if "x" in pressed_keys and "=" in pressed_keys:
                steps["x"] += 5; adjusted = True
            if "x" in pressed_keys and "-" in pressed_keys:
                steps["x"] = max(1, steps["x"]-5); adjusted = True
            if "y" in pressed_keys and "=" in pressed_keys:
                steps["y"] += 5; adjusted = True
            if "y" in pressed_keys and "-" in pressed_keys:
                steps["y"] = max(1, steps["y"]-5); adjusted = True
            if "z" in pressed_keys and "=" in pressed_keys:
                steps["z"] += 5; adjusted = True
            if "z" in pressed_keys and "-" in pressed_keys:
                steps["z"] = max(1, steps["z"]-5); adjusted = True
            if adjusted:
                key_map_move = rebuild_key_map()
                if hasattr(key, "char") and key.char in ("=","-"):
                    pressed_keys.discard(key.char)
    def on_release(key):
        if hasattr(key, "char") and key.char in ("x","y","z","=","-"):
            pressed_keys.discard(key.char)
    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()
    return listener

# ========== Main ==========
def run_server():
    print("======= Microscope Controller =======")
    print(f"Flask server on http://0.0.0.0:{server}")
    app.run(host="0.0.0.0", port=server, debug=False, use_reloader=False, threaded=True)

def main():
    global sb, controller_error
    start_camera()
    tracking_thread = threading.Thread(target=tracking_worker, daemon=True)
    tracking_thread.start()
    try:
        with Sangaboard() as board:
            sb = board
            start_keyboard_listener()
            run_server()
    except Exception as e:
        controller_error = str(e)
        print(f"Sangaboard unavailable: {controller_error}")
        run_server()

if __name__ == "__main__":
    main()
