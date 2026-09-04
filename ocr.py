"""Optional OCR helpers and deliberately conservative invoice parsing."""
import os
import re
from datetime import datetime

try:
    import pytesseract
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    if os.name == "nt" and not os.environ.get("TESSERACT_CMD"):
        for candidate in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                          r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
            if os.path.isfile(candidate):
                pytesseract.pytesseract.tesseract_cmd = candidate
                break
except ImportError:
    pytesseract = Image = ImageEnhance = ImageFilter = ImageOps = None

try:
    from pdf2image import convert_from_path
except ImportError:
    convert_from_path = None


def _variants(image):
    """Produce inexpensive variants which handle phone photos and faint bills."""
    gray = ImageOps.grayscale(image)
    # Tesseract is substantially more reliable when character height is larger.
    scale = 2 if max(gray.size) < 2600 else 1
    if scale != 1:
        gray = gray.resize((gray.width * scale, gray.height * scale), Image.Resampling.LANCZOS)
    contrast = ImageEnhance.Contrast(gray).enhance(1.8)
    sharp = contrast.filter(ImageFilter.SHARPEN)
    return (gray, contrast, sharp,
            contrast.point(lambda p: 255 if p > 165 else 0),
            contrast.point(lambda p: 255 if p > 205 else 0))


def _ocr_image(image):
    results = []
    ocr_error = None
    for variant in _variants(image):
        for config in ("--psm 6", "--psm 11", "--psm 4"):
            try:
                text = pytesseract.image_to_string(variant, config=config) or ""
                if text.strip():
                    confidence = 0
                    try:
                        data = pytesseract.image_to_data(
                            variant, config=config, output_type=pytesseract.Output.DICT)
                        values = [float(x) for x in data.get("conf", []) if float(x) >= 0]
                        confidence = sum(values) / len(values) if values else 0
                    except Exception:
                        pass
                    results.append((confidence + min(len(text), 300) / 30, text))
            except Exception as exc:
                ocr_error = ocr_error or exc
                continue
    if not results:
        if ocr_error:
            raise ocr_error
        return ""
    results.sort(key=lambda pair: pair[0], reverse=True)
    # Keep the strongest reading, then recover lines missed by another variant.
    lines, seen = [], set()
    for _, text in results[:6]:
        for line in text.splitlines():
            key = re.sub(r"\W", "", line).lower()
            if key and key not in seen:
                seen.add(key)
                lines.append(line.strip())
    return "\n".join(lines)


def extract_text(path):
    """Return (text, warning); OCR remains optional and never blocks manual entry."""
    ext = os.path.splitext(path)[1].lower()
    if pytesseract is None:
        return "", "OCR is unavailable: install pytesseract and Pillow, then install Tesseract OCR."
    try:
        if ext == ".pdf":
            if convert_from_path is None:
                return "", "PDF OCR is unavailable: install pdf2image and Poppler (Windows)."
            pages = convert_from_path(path, dpi=300, thread_count=1)
            return "\n\n".join(_ocr_image(page) for page in pages), None
        if Image is None:
            return "", "Image OCR is unavailable: install Pillow."
        with Image.open(path) as image:
            return _ocr_image(image.convert("RGB")), None
    except Exception as exc:
        return "", "OCR could not read this file (%s). Please verify the fields manually." % str(exc)


def _first(patterns, text):
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.M)
        if match:
            return match.group(1).strip()
    return ""


_MONEY = r"(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
_LABEL_END = r"(?:\s*[:#\-]\s*|\s+)"


def _number(value):
    try:
        return float(value.replace(",", "")) if value else 0
    except (ValueError, AttributeError):
        return 0


def _date(value):
    value = value.replace(".", "/")
    parts = value.split("/")
    if len(parts) == 3 and len(parts[2]) == 2:
        parts[2] = "20" + parts[2]
        value = "/".join(parts)
    for fmt in ("%d/%m/%Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def _category(name):
    if re.search(r"\b(lipstick|makeup|cosmetic|shampoo|conditioner|cream|lotion|"
                 r"perfume|deodorant|face\s*wash|soap|sunscreen)\b", name, re.I):
        return "cosmetic"
    return "medicine"


def parse_invoice(text):
    """Extract only strongly labelled fields and table-shaped items."""
    text = text or ""
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    supplier = _first((r"^(?:supplier|vendor|seller|sold\s*by|from)" + _LABEL_END + r"(.+)$",), text)
    if not supplier and lines and not re.search(
            r"invoice|bill|tax|gst|date|total|receipt|pharmacy", lines[0], re.I):
        supplier = lines[0][:120]
    invoice_number = _first((
        r"(?:invoice|inv|bill)\s*(?:no\.?|number|#)\s*" + _LABEL_END + r"([A-Z0-9][A-Z0-9./_-]{1,})",
    ), text)
    raw_date = _first((
        r"(?:invoice\s*)?date\s*" + _LABEL_END + r"([0-9]{1,4}[./-][0-9]{1,2}[./-][0-9]{1,4})",
    ), text)
    invoice_date = _date(raw_date.replace("-", "/")) if raw_date else ""

    # CGST and SGST are the two parts of GST on most Indian retail bills.
    tax_values = []
    for label in ("cgst", "sgst", "igst", "gst", "tax"):
        for match in re.finditer(r"\b" + label + r"\b[^0-9]{0,12}" + _MONEY, text, re.I):
            value = _number(match.group(1))
            if value and (label != "gst" or not tax_values):
                tax_values.append(value)
    gst = sum(tax_values)
    total_raw = _first((
        r"(?:grand\s*total|net\s*(?:amount|payable)|amount\s*payable|total\s*(?:amount|due)?)"
        + _LABEL_END + _MONEY,
    ), text)

    items = []
    skip = r"total|tax|gst|cgst|sgst|igst|amount|invoice|date|subtotal|discount|round"
    for line in lines:
        if re.search(skip, line, re.I) or re.match(r"^(?:qty|quantity|description|item|product)\b", line, re.I):
            continue
        # Name qty unit-price amount, with optional unit (TAB, STRIP, PCS, etc.).
        match = re.match(
            r"^(.+?)\s+(\d+(?:\.\d+)?)\s+(?:[A-Za-z]{1,10}\s+)?"
            r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s+([0-9][0-9,]*(?:\.[0-9]{1,2})?)$", line)
        if not match:
            match = re.match(r"^(.+?)\s+(\d+(?:\.\d+)?)\s+"
                             r"(?:₹|rs\.?)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)$", line, re.I)
            amount = match.group(3) if match else ""
            unit = amount
        else:
            amount, unit = match.group(4), match.group(3)
        if match:
            name = match.group(1).strip(" .:-")
            if len(name) >= 2 and re.search(r"[A-Za-z]", name):
                qty = _number(match.group(2))
                total = _number(amount)
                price = total / qty if total and qty else _number(unit)
                items.append({"product_name": name, "category": _category(name),
                              "quantity": str(qty).rstrip("0").rstrip("."),
                              "unit_price": ("%g" % price), "amount": ("%g" % total)})
    return {"supplier": supplier, "invoice_number": invoice_number, "invoice_date": invoice_date,
            "gst": gst, "total": _number(total_raw), "items": items[:50], "raw_text": text}
