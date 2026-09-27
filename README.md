# Azota practice extractor

This utility follows the normal Azota practice flow, acknowledges the
intentional proctoring dialog, and exports only data exposed by the page/API.
It never fills answer fields and never clicks **Nộp bài**.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
playwright install chromium
```

Set credentials in the environment rather than in source code:

```bash
export AZOTA_URL='https://azota.vn/vi/de-thi/lqjgtx'
export AZOTA_EXAM_PASSWORD='your-practice-password'
export AZOTA_DISPLAY_NAME='Azota Developer'
```

Run in a visible browser (the default):

```bash
python azota_extract.py
```

For CI/staging runs where proctoring does not need a visible browser:

```bash
python azota_extract.py --headless --output-dir ./artifacts
```

The command writes `results.json`, `results.csv`, and a redacted
`diagnostics.json` under the output directory. If the normal flow does not
expose an answer key before grading, `correct_answer` remains `null` and the
visible question set is still exported. Question text is in the `prompt`
field of JSON and the `prompt` column of CSV.

## Tests

```bash
python -m unittest -v
```
