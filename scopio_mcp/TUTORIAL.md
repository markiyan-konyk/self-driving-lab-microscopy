# Running an experiment the microscope drives itself

Zero to an unattended overnight campaign, assuming you have never given Claude a
loop before. Commands are PowerShell (Windows); on macOS/Linux the only
differences are `/` instead of `\` and `.venv/bin/` instead of `.venv\Scripts\`.

The worked example is **viscosity vs. temperature from Brownian motion**, but
nothing below is specific to it — swap the mission and the rest stands.

---

## 0. What is actually happening

```
you            Claude Code            scopio_mcp/server.py        the Pi
 |   prompt   ->   |                          |                     |
 |                 |  tool call (stdio) ->    |   HTTP + key ->     |
 |                 |                          |                <-  JSON / JPEG
 |             <- it reads the result and decides what to do next
```

Claude has **two ways of perceiving the microscope**, and they do different jobs.
This distinction is the whole trick, so it is worth getting straight before you
automate anything:

- **`grab_frame` puts a real image into Claude's context.** It sees pixels, not a
  filename. Use it for judgment: is the field empty, are the beads clumped, is
  there a bubble, is it in focus, is everything drifting one way.
- **`record_clip` writes JPEG frames to disk**, and Claude then analyses them
  with code *it writes itself* (numpy/scipy, via Bash). That is where numbers
  come from. It cannot eyeball a bead to 20 nm, but it can write a centroid
  tracker and debug it when the answer looks wrong.

An automated experiment that only does the second is a script with extra steps.
The first is what lets it notice the run has gone bad.

---

## 1. Make the lab folder

One folder per campaign. Keep it separate from this repo — it will fill up with
recordings.

```powershell
mkdir C:\MatterLab\runs\viscosity-2026-08
cd C:\MatterLab\runs\viscosity-2026-08

# copy the two packages in, side by side
copy -r C:\MatterLab\Self_Driving\self-driving-lab-microscopy\scopio_mcp .
copy -r C:\MatterLab\Self_Driving\self-driving-lab-microscopy\scopio_client .

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .\scopio_client -r scopio_mcp\requirements.txt

copy scopio_mcp\.env.example scopio_mcp\.env
notepad scopio_mcp\.env        # SCOPIO_URL + SCOPIO_API_KEY

claude mcp add -s project scopio -- .venv\Scripts\python.exe scopio_mcp\server.py
```

That last line writes `.mcp.json`, which is how Claude Code knows to start the
server. Full detail in `scopio_mcp/README.md`.

---

## 2. Drive it by hand first

**Do not skip this.** You need to see what the agent can and cannot do before you
trust it overnight, and it takes ten minutes.

```powershell
claude
```

Approve the `scopio` server when asked, then type `/mcp` to confirm it loaded.
Now paste these, one at a time:

> Call `status`. Is the microscope healthy and is the TEC on?

> Grab a frame and describe what you see. Is it in focus? Are there beads? Is
> anything obviously wrong with the sample?

> Record a 10 second clip. Then write a Python script that tracks the beads
> frame to frame and estimates the diffusion coefficient. Tell me the number and
> how much you trust it.

Watch what it does on that third one. It will write a script, run it, and often
find its own script is wrong and fix it. **That loop — write analysis, run it,
distrust the result, revise — is the thing you are about to automate.** If it
struggles here, it will struggle at 3am, and the fix is a better brief (step 3),
not a longer run.

---

## 3. Give it the mission: `CLAUDE.md`

`CLAUDE.md` is a file Claude Code reads **automatically at the start of every
session in that folder**. You never type it, never paste it, and it survives
every restart. It is where the standing instructions live.

Create `CLAUDE.md` in the lab folder:

```markdown
# Campaign: viscosity vs. temperature

## Goal
Measure the viscosity of the loaded sample from 15 to 40 C, from the Brownian
motion of the beads, and produce eta(T) with honest uncertainties.

Beads: 1.0 um diameter polystyrene. Sample: 40% glycerol in water, sealed.

## Hard limits -- never exceed, no exceptions
- Temperature setpoint: 10 C minimum, 45 C maximum.
- Never jog z by more than 400 steps from the last known focus.
- Never touch the galvo/AWG. This campaign does not use the tweezers.
- If `status` reports the gateway or camera unhealthy, write it in the notebook
  and stop. Do not try to restart anything on the Pi.

## The gate -- do this before anything else
Measure pure water at 20.0 C. You must recover 1.00 +/- 0.05 mPa*s before any
other temperature counts. If you cannot, the problem is your method, not the
sample: debug it and write down what was wrong. Do not proceed past this.

## Protocol for one temperature point
1. Set the setpoint, enable the TEC output.
2. Poll the temperature until it is stable within 0.1 C, then dwell 5 more
   minutes -- the sample lags the sensor.
3. Run `camera/autofocus`. Focus drifts as the stage thermally expands.
4. `grab_frame` and LOOK at it. Beads present, sharp, not clumped, no bubble,
   no bulk flow? If it looks wrong, fix it or record why and skip the point.
5. `record_clip` for 60 s. Use the MEASURED fps it returns, never the requested.
6. Analyse: drift-subtract, MSD over several lag times, correct for motion blur
   and localization noise. Get D, then eta = kT / (6 pi r D).
7. Append the result to NOTEBOOK.md.

## Judgment
Choose the next temperature yourself. Sample where the curve is interesting, not
on a fixed grid. If a point disagrees with its neighbours, repeat it before
believing it.

## Notebook discipline
NOTEBOOK.md is your only memory. Your context WILL be summarised and you WILL
forget things not written down. After every measurement, every failure and every
hypothesis, append an entry. Read it fully at the start of every session.
```

Note what the limits section is doing: it is a **brief, not a safety system**.
See step 9.

---

## 4. Give it a memory: `NOTEBOOK.md`

Claude's context has a size limit. On a long run it gets summarised — older
detail is compressed away, and some is lost. That is fine for chat and fatal for
a three-day experiment.

So: anything that must survive goes in a file. Create `NOTEBOOK.md`:

```markdown
# Lab notebook

Append only. Newest at the bottom. One entry per measurement, failure, or
hypothesis.

## Entry format
### <timestamp> -- <what this was>
- what I did:
- what I measured:
- what I saw in the frame:
- confidence and why:
- what I do next:

---

### 2026-08-01 09:00 -- campaign start
- Sample loaded, sealed chamber. Nothing measured yet.
- Next: the water control gate at 20 C.
```

This file is what makes restarting cheap. A fresh session reads it and knows
exactly where the campaign stands.

---

## 5. Stop it asking permission

By default Claude asks before each tool call. Unattended, that means it stops at
the first one and waits forever.

Create `.claude\settings.json` in the lab folder:

```json
{
  "permissions": {
    "allow": [
      "mcp__scopio__status",
      "mcp__scopio__describe_instrument",
      "mcp__scopio__grab_frame",
      "mcp__scopio__record_clip",
      "mcp__scopio__focus_metric",
      "mcp__scopio__white_balance",
      "mcp__scopio__camera_controls",
      "mcp__scopio__stage_move",
      "mcp__scopio__send_goal",
      "mcp__scopio__instrument_call",
      "mcp__scopio__call_service",
      "Read", "Write", "Edit", "Bash"
    ]
  }
}
```

Deliberately listed one by one, because **whatever you leave out will hang the
run**. `galvo_scpi` is absent on purpose: this campaign has no business firing
the laser, so if it ever tries, the run stalls and you find out.

`Bash` is broad, and it has to be — the agent writes and runs its own analysis
code. That is the point of it.

---

## 6. The loop

A "loop" here is nothing clever: **start Claude again, over and over, with the
same instruction.** Each run reads `CLAUDE.md` and `NOTEBOOK.md`, does one more
increment, writes down what happened, and exits.

### The easy version, while you are watching

Inside an interactive `claude` session:

```
/loop Read CLAUDE.md and NOTEBOOK.md. Do the next single step of the campaign, append what happened to NOTEBOOK.md, then stop.
```

`/loop` with no interval lets Claude pace itself. Good for the first hour when
you still want to see what it does. Stop it with Escape.

### The unattended version

Save this as `run.ps1` in the lab folder:

```powershell
# One campaign increment per iteration, fresh context each time.
# Stop it by creating a file called STOP in this folder.
$prompt = @'
Read CLAUDE.md and NOTEBOOK.md in full. Do the next single step of the
campaign, append what happened to NOTEBOOK.md, then stop.
'@

while (-not (Test-Path STOP)) {
    claude -p $prompt
    Start-Sleep -Seconds 30
}
Write-Host "STOP file found -- campaign halted."
```

Run it with `.\run.ps1`.

**Why a fresh session each time instead of one long one?** Because a fresh
context cannot get confused by three days of accumulated summary. Everything it
needs is in the two files, so it rebuilds from disk each iteration and stays as
sharp on day three as on hour one. You trade continuity of reasoning for a run
that does not degrade. The notebook is what makes that trade work — which is why
step 4 is not optional.

`-p` means "print and exit" (non-interactive). If you would rather not maintain
the allowlist from step 5, `claude -p --permission-mode bypassPermissions $prompt`
skips all checks — blunter, and only sensible once step 9 is done.

---

## 7. Subagents, and why

A subagent is a **fresh Claude with its own separate context** that does one job
and reports back a summary. The parent never sees the intermediate work.

That matters here for one reason: analysing 60 seconds of frames burns an
enormous amount of context, and the campaign director does not need any of it —
it needs `D = 0.41 ± 0.03 µm²/s, tracking looked clean`. Without subagents, the
director drowns in its own data by the fourth temperature point.

You do not call subagents yourself. You tell it to, in `CLAUDE.md`:

```markdown
## Analysis
Do not analyse clips in this session. For each clip, dispatch a subagent with
the clip path and the bead radius, and have it return only: D, its uncertainty,
the fitted MSD parameters, and a one-line verdict on tracking quality. Keep this
session for deciding what to measure next.
```

---

## 8. Watching it, and stopping it

- **What it did**: read `NOTEBOOK.md`. That is the point of the notebook — it is
  for you as much as for the agent.
- **What it saw**: the frames are in `recordings\<name>\`. Look at them yourself
  when a number seems off.
- **Stop it**: `New-Item STOP -ItemType File` in the lab folder. The loop finishes its current
  iteration and exits cleanly, mid-experiment-state written down. Ctrl+C also
  works but can cut an iteration in half.
- **Resume**: delete `STOP`, run `.\run.ps1` again. It picks up from the
  notebook.

---

## 9. Before you leave it alone overnight

Two things are true right now and both bite.

**The ROS nodes have no range limits.** The `CLAUDE.md` limits in step 3 are a
brief, and a brief is a suggestion. Nothing in the stack will refuse a 200 °C
setpoint or a z move that drives the objective into the coverslip. Before any
unattended run, put clamps in the nodes themselves — `temperature_node.py` and
`stage`. A limit enforced only by a prompt is not a limit.

**It has no hands.** If the coverslip dries, a bubble walks in, or the beads
sediment out, the campaign is over no matter how good the reasoning is. A sealed
chamber is what buys you the overnight run.

Also worth knowing: on a Claude subscription you will hit rate limits, not a
hard stop. The loop pauses and picks up when the window resets — that is normal
and the notebook makes it harmless.

---

## The short version

```
lab folder/
├── .mcp.json              <- claude mcp add wrote this          (step 1)
├── .claude/settings.json  <- so it never waits for you          (step 5)
├── CLAUDE.md              <- the standing mission brief         (step 3)
├── NOTEBOOK.md            <- its memory, and your record        (step 4)
├── run.ps1                <- start Claude, again and again      (step 6)
├── scopio_mcp/  scopio_client/
└── recordings/            <- written by record_clip
```

Build it in that order, drive it by hand once (step 2), and only then start the
loop.
