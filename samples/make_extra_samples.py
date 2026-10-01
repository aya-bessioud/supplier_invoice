"""Generates the extra test cases (fictitious data):
  05 exactly 2000.000 TND (must NOT need approval: rule is > 2000)
  06 missing invoice number
  (06-08 use Office Pro / PO-2026-0102 so the cumulative PO check stays meaningful)
  07 scanned invoice as PNG image (OCR path)
  08 scanned invoice as image-only PDF (PDF OCR path)
  09 corrupt PDF
Run with the OCR venv:  ..\\ocr\\.venv\\Scripts\\python make_extra_samples.py
"""
import io
import random
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent
CLIENT = ("Societe Exemple SA", "Rue du Lac Leman, Les Berges du Lac, 1053 Tunis", "MF 5555555/D/A/000")
OP = dict(name="Office Pro SA", addr="45 Avenue Habib Bourguiba, 3000 Sfax", tax_id="7654321/B/A/000",
          email="accounts@officepro.example", iban="TN59 0800 1000 0123 4567 8910")
TS = dict(name="Tech Supplies SARL", addr="12 Rue de l'Industrie, 2035 Charguia II, Tunis", tax_id="1234567/A/M/000",
          email="billing@techsupplies.example", iban="TN59 1000 6035 1835 9847 8831")
CN = dict(name="CloudNet Services", addr="8 Rue du Lac Biwa, 1053 Tunis", tax_id="1122334/C/M/000",
          email="finance@cloudnet.example", iban="TN59 2500 7000 0000 1122 3344")


def fmt(x): return f"{x:,.3f}".replace(",", " ")


def invoice(sup, num, date, due, po, lines, vat_rate=0.19):
    """Same layout as make_samples.py; num=None leaves the invoice-number line out."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4); W, H = A4
    c.setFillColor(colors.HexColor("#1E3A5F")); c.rect(0, H-90, W, 90, fill=1, stroke=0)
    c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 20); c.drawString(40, H-50, sup["name"])
    c.setFont("Helvetica", 10); c.drawString(40, H-68, sup["addr"]); c.drawString(40, H-82, f"MF: {sup['tax_id']}   Email: {sup['email']}")
    c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 24); c.drawRightString(W-40, H-130, "FACTURE / INVOICE")
    c.setFont("Helvetica", 11); y = H-160
    rows = [("Invoice No", num), ("Invoice date", date), ("Due date", due), ("PO reference", po or "-")]
    for k, v in rows:
        if v is None:
            continue
        c.drawRightString(W-150, y, k + ":"); c.drawString(W-145, y, v); y -= 16
    c.setFont("Helvetica-Bold", 11); c.drawString(40, H-160, "Bill to:")
    c.setFont("Helvetica", 10)
    for i, l in enumerate(CLIENT): c.drawString(40, H-176-14*i, l)
    y = H-260
    c.setFillColor(colors.HexColor("#E7ECF3")); c.rect(40, y-6, W-80, 20, fill=1, stroke=0); c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 10)
    for x, t in [(46, "Description"), (330, "Qty"), (390, "Unit price"), (480, "Amount")]: c.drawString(x, y, t)
    c.setFont("Helvetica", 10); net = 0
    for d, q, u in lines:
        y -= 20; a = q*u; net += a
        c.drawString(46, y, d); c.drawString(330, y, str(q)); c.drawString(390, y, fmt(u)); c.drawString(480, y, fmt(a))
    net = round(net, 3); vat = round(net*vat_rate, 3); gross = round(net+vat, 3)
    y -= 40
    for k, v, b in [("Total HT (net)", net, False), (f"TVA {int(vat_rate*100)}%", vat, False), ("Total TTC (gross)", gross, True)]:
        c.setFont("Helvetica-Bold" if b else "Helvetica", 11)
        c.drawRightString(460, y, k); c.drawRightString(W-45, y, fmt(v) + " TND"); y -= 18
    c.setFont("Helvetica", 9); c.drawString(40, 80, f"Payment by bank transfer - IBAN: {sup['iban']}")
    c.drawString(40, 66, "Fictitious invoice generated for testing the n8n invoice-automation workflow.")
    c.save()
    return buf.getvalue(), (net, vat, gross)


def as_scan(pdf_bytes, dpi=150):
    """Render a PDF page and make it look like a slightly skewed, noisy grey scan."""
    img = pdfium.PdfDocument(pdf_bytes)[0].render(scale=dpi / 72).to_pil().convert("L")
    img = img.rotate(0.8, expand=True, fillcolor=255, resample=Image.BICUBIC)
    rnd = random.Random(7)
    px = img.load()
    for _ in range(img.width * img.height // 60):           # salt-and-pepper dust
        x, y = rnd.randrange(img.width), rnd.randrange(img.height)
        px[x, y] = rnd.choice((90, 200))
    return img.filter(ImageFilter.GaussianBlur(0.5))


# 05 - exactly 2000.000 TND: 1680.672 + 19% (319.328) = 2000.000, CloudNet PO-2026-0103 (max 2500)
pdf, t = invoice(CN, "INV-CN-2026-015", "2026-09-25", "2026-10-25", "PO-2026-0103",
                 [("Cloud hosting - October", 1, 1680.672)])
assert abs(t[2] - 2000.0) < 1e-9, t
(HERE / "facture_05_exactement_2000.pdf").write_bytes(pdf)

# 06 - missing invoice number (otherwise valid)
pdf, _ = invoice(OP, None, "2026-09-26", "2026-10-26", "PO-2026-0102", [("Desk lamp", 5, 50.0)])
(HERE / "facture_06_sans_numero.pdf").write_bytes(pdf)

# 07 - scanned PNG (valid, 238 TND -> recorded automatically)
pdf, _ = invoice(OP, "INV-OP-2026-131", "2026-09-27", "2026-10-27", "PO-2026-0102",
                 [("A4 paper box", 4, 50.0)])
scan = as_scan(pdf)
scan.save(HERE / "facture_07_scan.png")

# 08 - same kind of scan, but wrapped in an image-only PDF (no text layer)
pdf, _ = invoice(OP, "INV-OP-2026-132", "2026-09-28", "2026-10-28", "PO-2026-0102", [("Whiteboard", 2, 150.0)])
as_scan(pdf).save(HERE / "facture_08_scan.pdf", "PDF", resolution=150)

# 09 - corrupt file: PDF header followed by garbage, truncated
rnd = random.Random(42)
(HERE / "facture_09_corrompue.pdf").write_bytes(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog" +
                                               bytes(rnd.randrange(256) for _ in range(2000)))
print("ok")

# 10 - valid invoice reserved for the Gmail-trigger test (119 TND, Office Pro / PO-2026-0102)
pdf, _ = invoice(OP, "INV-OP-2026-140", "2026-09-30", "2026-10-30", "PO-2026-0102", [("Stapler", 2, 50.0)])
(HERE / "facture_10_gmail.pdf").write_bytes(pdf)
print("ok 10")

# 11 - second valid invoice for the Gmail test (178.5 TND, Office Pro / PO-2026-0102), never submitted
pdf, _ = invoice(OP, "INV-OP-2026-141", "2026-10-01", "2026-10-31", "PO-2026-0102", [("Toner cartridge", 3, 50.0)])
(HERE / "facture_11_gmail.pdf").write_bytes(pdf)
print("ok 11")
