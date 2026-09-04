"""Optional Gemini vision extraction for invoice images and PDFs."""
import json
import os
import re


def _load_local_env():
    """Load simple KEY=value settings without exposing secrets or requiring dotenv."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip().strip("\"'")
                if key and value and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        return


_load_local_env()
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

SCHEMA = {
    "type": "object",
    "properties": {
        "supplier": {"type": "string"},
        "invoice_number": {"type": "string"},
        "invoice_date": {"type": "string", "description": "ISO date YYYY-MM-DD, or empty"},
        "gst": {"type": "number"},
        "total": {"type": "number"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "product_name": {"type": "string"},
                    "category": {"type": "string", "enum": ["medicine", "cosmetic", "other"]},
                    "quantity": {"type": "number"},
                    "unit_price": {"type": "number"},
                },
                "required": ["product_name", "category", "quantity", "unit_price"],
            },
        },
    },
    "required": ["supplier", "invoice_number", "invoice_date", "gst", "total", "items"],
}

PROMPT = """Extract the invoice from this image or PDF. Return ONLY a JSON object matching
the supplied schema. Do not guess: use an empty string for unreadable supplier, invoice number,
or date, and 0 for unreadable numeric values. invoice_date must be YYYY-MM-DD. gst is the total
tax amount (including CGST/SGST). total is the final payable amount. For every line item provide
product_name, category (medicine, cosmetic, or other), quantity, and unit_price. Do not include
markdown, commentary, or extra keys."""


def _validate(value):
    if not isinstance(value, dict):
        raise ValueError("Gemini returned a non-object response.")
    result = {}
    for key in ("supplier", "invoice_number", "invoice_date"):
        item = value.get(key, "")
        if not isinstance(item, str):
            raise ValueError("Gemini returned an invalid %s." % key)
        result[key] = item.strip()
    for key in ("gst", "total"):
        item = value.get(key, 0)
        if isinstance(item, bool) or not isinstance(item, (int, float)) or item < 0:
            raise ValueError("Gemini returned an invalid %s." % key)
        result[key] = float(item)
    items = value.get("items", [])
    if not isinstance(items, list):
        raise ValueError("Gemini returned invalid items.")
    result["items"] = []
    for item in items[:50]:
        if not isinstance(item, dict):
            raise ValueError("Gemini returned an invalid line item.")
        name = item.get("product_name", "")
        category = item.get("category", "other")
        quantity = item.get("quantity", 0)
        price = item.get("unit_price", 0)
        if (not isinstance(name, str) or not name.strip() or
                category not in ("medicine", "cosmetic", "other") or
                isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or quantity <= 0 or
                isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0):
            raise ValueError("Gemini returned an invalid line item.")
        result["items"].append({"product_name": name.strip(), "category": category,
                                "quantity": quantity, "unit_price": price})
    if result["invoice_date"]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", result["invoice_date"]):
            raise ValueError("Gemini returned an invalid invoice date.")
        try:
            from datetime import date
            date.fromisoformat(result["invoice_date"])
        except ValueError:
            raise ValueError("Gemini returned an invalid invoice date.")
    result["raw_text"] = ""
    return result


def extract_invoice(path):
    """Return (extracted, warning). A warning means the caller should use local OCR."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None, None
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return None, "Gemini is enabled but google-genai is not installed; using local OCR."
    try:
        with open(path, "rb") as source:
            data = source.read()
        ext = os.path.splitext(path)[1].lower()
        mime_type = "application/pdf" if ext == ".pdf" else {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".jpe": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp",
        }.get(ext)
        if not mime_type:
            raise ValueError("Unsupported Gemini file type.")
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", MODEL),
            contents=[types.Part.from_bytes(data=data, mime_type=mime_type), PROMPT],
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=SCHEMA, temperature=0,
            ),
        )
        text = getattr(response, "text", None)
        if not text:
            raise ValueError("Gemini returned an empty response.")
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
        return _validate(json.loads(text)), None
    except Exception as exc:
        return None, "Gemini extraction failed (%s); using local OCR." % str(exc)
