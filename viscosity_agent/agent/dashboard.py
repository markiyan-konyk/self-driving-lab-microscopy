"""Live web dashboard for a run.

A tiny Flask app (same pattern as ui/run_ui.py) that tails the run's notebook and
state snapshot and serves them to a polling single-page UI: the running feed of
decisions / tool calls / results, the latest annotated scene image, the live
viscosity estimate vs the literature value, and the budget status. This is the
primary "what is the agent doing and is it working" view.

Run standalone against an existing run:
    python -m agent.dashboard --run-dir runs/<timestamp> --port 8070
or let run_agent.py launch it in a background thread.
"""

import argparse
import json
import os
import threading

from flask import Flask, Response, jsonify, request, send_from_directory


def create_app(run_dir: str) -> Flask:
    run_dir = os.path.abspath(run_dir)
    app = Flask(__name__)

    jsonl = os.path.join(run_dir, "notebook.jsonl")
    state_json = os.path.join(run_dir, "state.json")

    @app.route("/")
    def index():
        return Response(_PAGE, mimetype="text/html")

    @app.route("/state")
    def state():
        if not os.path.isfile(state_json):
            return jsonify({"status": "waiting"})
        try:
            with open(state_json, encoding="utf-8") as f:
                return jsonify(json.load(f))
        except (OSError, ValueError):
            return jsonify({"status": "waiting"})

    @app.route("/events")
    def events():
        since = request.args.get("since", default=0, type=int)
        out = []
        if os.path.isfile(jsonl):
            try:
                with open(jsonl, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        ev = json.loads(line)
                        if ev.get("seq", 0) > since:
                            out.append(ev)
            except (OSError, ValueError):
                pass
        return jsonify(out[-400:])       # cap payload

    @app.route("/snap/<path:relpath>")
    def snap(relpath):
        full = os.path.abspath(os.path.join(run_dir, relpath))
        if not full.startswith(run_dir) or not os.path.isfile(full):
            return "not found", 404
        rel = os.path.relpath(full, run_dir)
        return send_from_directory(run_dir, rel, conditional=True)

    return app


def serve(run_dir: str, port: int = 8070, host: str = "0.0.0.0"):
    create_app(run_dir).run(host=host, port=port, debug=False,
                            use_reloader=False, threaded=True)


def launch_in_thread(run_dir: str, port: int = 8070):
    """Start the dashboard in a daemon thread; returns immediately."""
    t = threading.Thread(target=serve, args=(run_dir, port), daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------- #
_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SCOPIO · Autonomous Viscometry</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--edge:#26303d;--fg:#e6edf3;--dim:#8b98a5;
--accent:#58a6ff;--good:#3fb950;--warn:#d29922;--bad:#f85149;--mag:#bc8cff;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{display:flex;align-items:center;gap:14px;padding:12px 18px;
border-bottom:1px solid var(--edge);background:var(--panel);position:sticky;top:0;z-index:5}
header h1{font-size:15px;margin:0;letter-spacing:.5px}
header h1 b{color:var(--accent)}
.pill{padding:2px 10px;border-radius:999px;font-size:12px;border:1px solid var(--edge)}
.pill.live{color:var(--good);border-color:var(--good)}
.spacer{flex:1}
.muted{color:var(--dim)}
main{display:grid;grid-template-columns:300px 1fr 340px;gap:14px;padding:14px;
align-items:start}
@media(max-width:1100px){main{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--edge);border-radius:10px;
padding:14px;margin-bottom:14px}
.card h2{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);
margin:0 0 10px}
.eta{font-size:30px;font-weight:700;color:var(--good)}
.eta small{font-size:14px;color:var(--dim);font-weight:400}
.kv{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px dotted #222c37}
.kv b{color:var(--fg);font-weight:600}
.dev-ok{color:var(--good)} .dev-warn{color:var(--warn)} .dev-bad{color:var(--bad)}
.snapwrap{text-align:center}
.snapwrap img{max-width:100%;border-radius:8px;border:1px solid var(--edge)}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:10px}
.metrics div{background:#0d141c;border:1px solid var(--edge);border-radius:8px;padding:8px;text-align:center}
.metrics span{display:block;font-size:11px;color:var(--dim)}
.metrics b{font-size:18px}
.reason{background:#0d141c;border-left:3px solid var(--mag);padding:8px 10px;
border-radius:4px;margin-top:6px;white-space:pre-wrap}
.verdict{font-weight:700}
#feed{max-height:60vh;overflow:auto}
.ev{display:flex;gap:10px;padding:3px 0;border-bottom:1px solid #1b232c}
.ev .t{color:var(--dim);flex:0 0 62px;text-align:right}
.ev .m{white-space:pre-wrap;word-break:break-word}
.k-phase .m{color:var(--accent);font-weight:700}
.k-decision .m{color:var(--mag)}
.k-result .m{color:var(--good)}
.k-error .m{color:var(--bad)}
.k-tool .m{color:#9fb1c1}
.k-metric .m{color:var(--warn)}
.k-snapshot .m{color:var(--accent)}
.bar{height:6px;background:#0d141c;border-radius:4px;overflow:hidden;margin-top:4px}
.bar>i{display:block;height:100%;background:var(--accent)}
</style></head>
<body>
<header>
  <h1><b>SCOPIO</b> · Autonomous Viscometry</h1>
  <span id="status" class="pill">connecting…</span>
  <span id="model" class="pill muted"></span>
  <span class="spacer"></span>
  <span id="clock" class="muted"></span>
</header>
<main>
  <section>
    <div class="card">
      <h2>Viscosity estimate</h2>
      <div class="eta" id="eta">— <small>waiting</small></div>
      <div id="etarow" class="muted" style="margin-top:6px"></div>
      <div class="kv"><span>literature (water)</span><b>0.89 mPa·s</b></div>
      <div class="kv"><span>deviation</span><b id="dev">—</b></div>
      <div class="kv"><span>beads · clips</span><b id="nb">—</b></div>
    </div>
    <div class="card">
      <h2>Setup</h2>
      <div class="kv"><span>calibration</span><b id="cal">—</b></div>
      <div class="kv"><span>source</span><b id="calsrc">—</b></div>
      <div class="kv"><span>iteration</span><b id="iter">—</b></div>
      <div class="kv"><span>survey moves</span><b id="moves">—</b></div>
      <div class="bar"><i id="iterbar" style="width:0%"></i></div>
    </div>
  </section>
  <section>
    <div class="card snapwrap">
      <h2>Live field of view</h2>
      <img id="snap" alt="waiting for first frame" />
      <div class="metrics">
        <div><span>beads</span><b id="m-beads">—</b></div>
        <div><span>clump</span><b id="m-clump">—</b></div>
        <div><span>focus</span><b id="m-focus">—</b></div>
        <div><span>fps</span><b id="m-fps">—</b></div>
      </div>
    </div>
    <div class="card">
      <h2>Activity</h2>
      <div id="feed"></div>
    </div>
  </section>
  <section>
    <div class="card">
      <h2>Latest decision</h2>
      <div id="decision" class="reason muted">—</div>
    </div>
    <div class="card">
      <h2>Self-critique</h2>
      <div id="critique" class="reason muted">—</div>
    </div>
  </section>
</main>
<script>
let since=0;
function mPa(x){return (x*1e3);}
function fmtEta(a){
  if(!a||a.weighted_mean_Pa_s==null) return null;
  return {v:mPa(a.weighted_mean_Pa_s), u:mPa(a.weighted_unc_Pa_s||0), n:a.n_particles};
}
async function poll(){
  try{
    const st=await (await fetch('/state')).json();
    document.getElementById('status').textContent=st.status||'…';
    document.getElementById('status').className='pill '+((st.status==='done')?'':'live');
    document.getElementById('model').textContent=(st.dry_run?'dry-run · ':'')+(st.provider_model||'');
    if(st.elapsed_s!=null) document.getElementById('clock').textContent=st.elapsed_s.toFixed(0)+'s';
    const e=fmtEta(st.aggregate);
    if(e){
      document.getElementById('eta').innerHTML=e.v.toFixed(3)+' <small>± '+e.u.toFixed(3)+' mPa·s</small>';
      document.getElementById('etarow').textContent=(st.aggregate.weighted_mean_Pa_s).toExponential(3)+' Pa·s (weighted)';
      const dev=((e.v-0.89)/0.89*100);
      const d=document.getElementById('dev');
      d.textContent=(dev>=0?'+':'')+dev.toFixed(1)+'%';
      d.className=Math.abs(dev)<10?'dev-ok':(Math.abs(dev)<25?'dev-warn':'dev-bad');
      document.getElementById('nb').textContent=e.n+' · '+(st.n_clips||0);
    }
    document.getElementById('cal').textContent=(st.um_per_px!=null?st.um_per_px+' µm/px':'—');
    document.getElementById('calsrc').textContent=st.calibration_source||'—';
    document.getElementById('iter').textContent=(st.iteration!=null?st.iteration:'—');
    document.getElementById('moves').textContent=(st.survey_attempts!=null?st.survey_attempts:'—');
    if(st.iteration!=null) document.getElementById('iterbar').style.width=Math.min(100,st.iteration/5*100)+'%';
    const sc=st.scene||{};
    if(sc.bead_count!=null){
      document.getElementById('m-beads').textContent=sc.bead_count;
      document.getElementById('m-clump').textContent=((sc.clump_fraction||0)*100).toFixed(0)+'%';
      document.getElementById('m-focus').textContent=(sc.focus_score||0).toFixed(0);
      document.getElementById('m-fps').textContent=(sc.measured_fps||0).toFixed(1);
      if(sc.snapshot) document.getElementById('snap').src='/snap/'+sc.snapshot+'?t='+Date.now();
    }
    const sd=st.scene_decision;
    if(sd) document.getElementById('decision').innerHTML=
      '<span class="verdict">'+(sd.action||'')+'</span>\n'+(sd.reasoning||'');
    const cr=st.critique;
    if(cr){document.getElementById('critique').innerHTML=
      '<span class="verdict">'+(cr.verdict||'')+'</span> (conf '+(cr.confidence)+')\n'+(cr.reasoning||'')+
      ((cr.concerns&&cr.concerns.length)?'\n• '+cr.concerns.join('\n• '):'');}
  }catch(err){}
  try{
    const evs=await (await fetch('/events?since='+since)).json();
    const feed=document.getElementById('feed');
    const atBottom=feed.scrollTop+feed.clientHeight>=feed.scrollHeight-40;
    for(const ev of evs){
      since=Math.max(since,ev.seq||0);
      const row=document.createElement('div');
      row.className='ev k-'+ev.kind;
      row.innerHTML='<span class="t">'+(ev.elapsed_s||0).toFixed(1)+'s</span>'+
                    '<span class="m">'+escapeHtml(ev.msg||'')+'</span>';
      feed.appendChild(row);
    }
    while(feed.children.length>600) feed.removeChild(feed.firstChild);
    if(atBottom) feed.scrollTop=feed.scrollHeight;
  }catch(err){}
}
function escapeHtml(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
poll(); setInterval(poll,1000);
</script>
</body></html>"""


def main():
    ap = argparse.ArgumentParser(description="SCOPIO viscosity-agent dashboard")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--port", type=int, default=8070)
    args = ap.parse_args()
    print(f"dashboard on http://0.0.0.0:{args.port}  (run: {args.run_dir})")
    serve(args.run_dir, port=args.port)


if __name__ == "__main__":
    main()
