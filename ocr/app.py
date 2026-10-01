"""Local OCR / text-extraction service for the n8n invoice workflow.

POST /extract  (multipart, field "file")  ->  {"ok", "text", "method", "pages", "chars", ...}
  - text PDF            -> text read directly with pdfplumber      (method "pdf_text")
  - scanned PDF         -> pages rendered with pypdfium2 + Tesseract (method "pdf_ocr")
  - PNG / JPG image     -> Tesseract fra+eng                        (method "image_ocr")
  - corrupt / unknown   -> HTTP 422 {"ok": false, "error": "..."}   (never crashes)

Everything runs locally: no file or text leaves this machine.
"""
import io
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image, ImageOps

BASE = Path(__file__).resolve().parent
TESSERACT = os.environ.get("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
TESSDATA = os.environ.get("TESSDATA_PREFIX", str(BASE / "tessdata"))
LANGS = os.environ.get("OCR_LANGS", "fra+eng")
MAX_BYTES = int(os.environ.get("OCR_MAX_BYTES", 20 * 1024 * 1024))
MAX_PAGES = int(os.environ.get("OCR_MAX_PAGES", 10))
MIN_TEXT_CHARS = 40          # fewer real characters than this => treat the PDF as scanned
RENDER_DPI = 300

app = FastAPI(title="Invoice OCR service", version="1.0")


class ExtractionError(Exception):
    pass


def sniff(data: bytes) -> str:
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    raise ExtractionError("Unsupported or corrupt file: not a PDF, PNG or JPG")


def tesseract(img: Image.Image) -> tuple[str, float | None]:
    """OCR one image; returns (text, mean word confidence 0-100)."""
    img = ImageOps.exif_transpose(img).convert("L")
    if img.width < 1500:                                   # small scans: upscale for better OCR
        f = 1500 / img.width
        img = img.resize((int(img.width * f), int(img.height * f)), Image.LANCZOS)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "page.png"
        img.save(src)
        env = {**os.environ, "TESSDATA_PREFIX": TESSDATA}
        run = subprocess.run([TESSERACT, str(src), "stdout", "-l", LANGS, "--psm", "6", "-c", "tessedit_create_tsv=1"],
                             capture_output=True, env=env, timeout=120)
    if run.returncode != 0:
        raise ExtractionError("Tesseract failed: " + run.stderr.decode(errors="replace")[:300])
    lines, confs, current, key = [], [], [], None
    for row in run.stdout.decode("utf-8", errors="replace").splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or c[0] != "5":
            continue
        k = (c[2], c[3], c[4])                             # block, paragraph, line
        if k != key and current:
            lines.append(" ".join(current)); current = []
        key = k
        if c[11].strip():
            current.append(c[11].strip())
            if float(c[10]) >= 0:
                confs.append(float(c[10]))
    if current:
        lines.append(" ".join(current))
    return "\n".join(lines), (round(sum(confs) / len(confs), 1) if confs else None)


def extract_pdf(data: bytes) -> dict:
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            n = len(pdf.pages)
            if n == 0:
                raise ExtractionError("PDF has no pages")
            text = "\n\n".join((p.extract_text() or "") for p in pdf.pages[:MAX_PAGES])
    except ExtractionError:
        raise
    except Exception as e:                                 # pdfminer raises many types on bad files
        raise ExtractionError(f"Corrupt or unreadable PDF: {type(e).__name__}") from e

    if sum(ch.isalnum() for ch in text) >= MIN_TEXT_CHARS:
        return {"text": text, "method": "pdf_text", "pages": n, "ocr_confidence": None}

    try:                                                   # scanned PDF -> render + OCR
        doc = pdfium.PdfDocument(data)
        texts, confs = [], []
        for i in range(min(len(doc), MAX_PAGES)):
            img = doc[i].render(scale=RENDER_DPI / 72).to_pil()
            t, c = tesseract(img)
            texts.append(t)
            if c is not None:
                confs.append(c)
    except ExtractionError:
        raise
    except Exception as e:
        raise ExtractionError(f"Could not render PDF for OCR: {type(e).__name__}") from e
    return {"text": "\n\n".join(texts), "method": "pdf_ocr", "pages": n,
            "ocr_confidence": round(sum(confs) / len(confs), 1) if confs else None}


def extract_image(data: bytes) -> dict:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise ExtractionError(f"Corrupt or unreadable image: {type(e).__name__}") from e
    text, conf = tesseract(img)
    return {"text": text, "method": "image_ocr", "pages": 1, "ocr_confidence": conf}


@app.get("/health")
def health():
    ok = Path(TESSERACT).exists() and (Path(TESSDATA) / "fra.traineddata").exists()
    return {"ok": ok, "tesseract": TESSERACT, "tessdata": TESSDATA, "langs": LANGS}


@app.post("/extract")
async def extract(file: UploadFile = File(...)):
    t0 = time.time()
    data = await file.read()
    base = {"file_name": file.filename, "bytes": len(data)}
    try:
        if not data:
            raise ExtractionError("Empty file")
        if len(data) > MAX_BYTES:
            raise ExtractionError(f"File too large ({len(data)} bytes)")
        kind = sniff(data)
        result = extract_pdf(data) if kind == "pdf" else extract_image(data)
        if not result["text"].strip():
            raise ExtractionError(f"No text found in the document ({result['method']})")
    except ExtractionError as e:
        return JSONResponse(status_code=422, content={"ok": False, "error": str(e), "method": "failed", **base})
    except Exception as e:                                 # last-resort guard: never crash the service
        return JSONResponse(status_code=500, content={"ok": False, "error": f"Internal error: {type(e).__name__}",
                                                      "method": "failed", **base})
    return {"ok": True, **base, "type": kind, **result, "chars": len(result["text"]),
            "elapsed_s": round(time.time() - t0, 2)}
