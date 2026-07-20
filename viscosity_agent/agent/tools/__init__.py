"""LangChain @tool functions the agent may call.

Split by concern:
    instrument.py   -- stage / camera / calibration (wraps scopio_client)
    vision.py       -- client-side bead detection, clump + focus metrics
    acquisition.py  -- real-timestamp clip capture from the live stream
    pipeline.py     -- track / QC / estimate (wraps the viscosity/ code)
    sandbox.py      -- jailed write/read/run-python for the critique agent

Every tool is a thin, logged wrapper: it records its call to the run notebook
and returns compact JSON/text -- the LLM never sees raw pixels.
"""
