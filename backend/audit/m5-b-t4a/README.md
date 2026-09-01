# M5-B-T4a OCR engine probe

This probe is intentionally isolated from the production dependency set.

```powershell
cd backend
python -m venv audit/m5-b-t4a/.venv
audit/m5-b-t4a/.venv/Scripts/python.exe -m pip install -r audit/m5-b-t4a/requirements.txt
audit/m5-b-t4a/.venv/Scripts/python.exe audit/m5-b-t4a/probe.py init-truth
audit/m5-b-t4a/.venv/Scripts/python.exe -u audit/m5-b-t4a/probe.py benchmark
audit/m5-b-t4a/.venv/Scripts/python.exe audit/m5-b-t4a/probe.py render-report
audit/m5-b-t4a/.venv/Scripts/python.exe -u audit/m5-b-t4a/probe.py benchmark-a2
audit/m5-b-t4a/.venv/Scripts/python.exe audit/m5-b-t4a/probe.py render-report-a2
```

`init-truth` must only be used to create the initial WinRT draft. Once a human has
corrected `data/generic/ocr-truth/truth.md`, later benchmarks must not regenerate it.

Each engine is benchmarked at 896-pixel width first. If that configuration's
median exceeds 500 ms, the probe records an early stop and does not run the
remaining configurations for that engine. During this development-stage run,
player IDs are not filtered from the OCR draft or candidate outputs.

`benchmark-a2` runs the two B-T4a2 OpenVINO candidates through the full
thread/input/detector factorial. Each configuration runs in a fresh worker
process, discards three warm-up frames, and is repeated in three interleaved
rounds. It reads the frozen independent reference anchors from
`data/generic/ocr-truth/reference-anchors.json`; those anchors are not human
gold and must not be used to calculate formal precision.
