# M5-B-T4a OCR engine probe

This probe is intentionally isolated from the production dependency set.

```powershell
cd backend
python -m venv audit/m5-b-t4a/.venv
audit/m5-b-t4a/.venv/Scripts/python.exe -m pip install -r audit/m5-b-t4a/requirements.txt
audit/m5-b-t4a/.venv/Scripts/python.exe audit/m5-b-t4a/probe.py init-truth
audit/m5-b-t4a/.venv/Scripts/python.exe -u audit/m5-b-t4a/probe.py benchmark
audit/m5-b-t4a/.venv/Scripts/python.exe audit/m5-b-t4a/probe.py render-report
```

`init-truth` must only be used to create the initial WinRT draft. Once a human has
corrected `data/generic/ocr-truth/truth.md`, later benchmarks must not regenerate it.

Each engine is benchmarked at 896-pixel width first. If that configuration's
median exceeds 500 ms, the probe records an early stop and does not run the
remaining configurations for that engine. During this development-stage run,
player IDs are not filtered from the OCR draft or candidate outputs.
