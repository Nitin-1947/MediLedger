# MediLedger
Responsive Flask + SQLite purchase-bill manager for medical/cosmetic shops.

## Run
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```
Open http://127.0.0.1:5000 and register an account. Data is isolated by user. Use **Scan bill** to OCR an image/PDF and verify the prefilled form. OCR dependencies are optional: if unavailable, the original is retained and manual entry remains available. Set `SECRET_KEY` in production.
Use **Forgot your password?** on the login page to reset a password using the registered email.

## Optional Gemini Vision extraction
Installations include the current `google-genai` SDK. Copy `.env.example` to `.env` and
set `GEMINI_API_KEY` in the environment to have the scanner send the original image or PDF directly to Gemini
(the default model is `gemini-3.6-flash`; override it with `GEMINI_MODEL`). If the
key is absent, Gemini is not installed, or the request/response fails validation,
the app shows a warning and falls back to local OCR. The key is read only from the
environment and is never displayed or stored. Always verify extracted values.

## OCR setup (optional)
`pip install -r requirements.txt` installs the Python wrappers. On Windows, install the
[Tesseract OCR installer](https://github.com/UB-Mannheim/tesseract/wiki) separately and
ensure `tesseract.exe` is on `PATH` (or set `pytesseract.pytesseract.tesseract_cmd` in
`ocr.py`). PDF OCR additionally needs Poppler; install a Windows Poppler build and add
its `bin` directory to `PATH`. A missing engine produces a clear warning and never blocks saving.
The scanner converts photos to grayscale, enlarges small text, and tries
contrast, sharpening and threshold variants with several Tesseract page
layouts. PDFs are rendered page by page at 300 DPI. It also recognizes common
Indian invoice labels (including CGST/SGST) and table-shaped medicine or
cosmetic lines. These are suggestions only: always review every field before
saving.

## Windows one-click start

Double-click `run_mediledger.bat` in this folder. The first run creates a local virtual environment and installs dependencies; this can take a minute. You may set `GEMINI_API_KEY` and optionally `GEMINI_MODEL` before launching (or place `GEMINI_API_KEY=...` and `GEMINI_MODEL=...` in a local `.env` file). Keep the black terminal window open while using the app.

## Vercel deployment

The repository includes `vercel.json` and `api/index.py` for Vercel deployment. Add
`SECRET_KEY`, `GEMINI_API_KEY`, and optionally `GEMINI_MODEL` under Vercel project
Environment Variables, then redeploy. Vercel's filesystem is temporary, so SQLite data
and uploaded files are not durable across serverless instances; use a hosted database
and object storage for production.

`api/index.py` is the Vercel entrypoint; it loads `app.py`, which imports the Gemini
scanner from `gemini.py`. Never paste the Gemini API key into `index.py` or commit it.
