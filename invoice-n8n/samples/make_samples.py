"""Generates 4 fictitious test invoices (PDF) for the n8n workflow."""
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib import colors

CLIENT = ("Societe Exemple SA", "Rue du Lac Leman, Les Berges du Lac, 1053 Tunis", "MF 5555555/D/A/000")

def fmt(x): return f"{x:,.3f}".replace(",", " ")

def invoice(path, sup, num, date, due, po, lines, vat_rate=0.19, gross_override=None):
    c = canvas.Canvas(path, pagesize=A4); W, H = A4
    c.setFillColor(colors.HexColor("#1E3A5F")); c.rect(0, H-90, W, 90, fill=1, stroke=0)
    c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 20); c.drawString(40, H-50, sup["name"])
    c.setFont("Helvetica", 10); c.drawString(40, H-68, sup["addr"]); c.drawString(40, H-82, f"MF: {sup['tax_id']}   Email: {sup['email']}")
    c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 24); c.drawRightString(W-40, H-130, "FACTURE / INVOICE")
    c.setFont("Helvetica", 11); y = H-160
    for k, v in [("Invoice No", num), ("Invoice date", date), ("Due date", due), ("PO reference", po or "-")]:
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
    vat = round(net*vat_rate, 3); gross = gross_override if gross_override else net+vat
    y -= 40
    for k, v, b in [("Total HT (net)", net, False), (f"TVA {int(vat_rate*100)}%", vat, False), ("Total TTC (gross)", gross, True)]:
        c.setFont("Helvetica-Bold" if b else "Helvetica", 11)
        c.drawRightString(460, y, k); c.drawRightString(W-45, y, fmt(v) + " TND"); y -= 18
    c.setFont("Helvetica", 9); c.drawString(40, 80, f"Payment by bank transfer - IBAN: {sup['iban']}")
    c.drawString(40, 66, "Fictitious invoice generated for testing the n8n invoice-automation workflow.")
    c.save()

TS = dict(name="Tech Supplies SARL", addr="12 Rue de l'Industrie, 2035 Charguia II, Tunis", tax_id="1234567/A/M/000", email="billing@techsupplies.example", iban="TN59 1000 6035 1835 9847 8831")
OP = dict(name="Office Pro SA", addr="45 Avenue Habib Bourguiba, 3000 Sfax", tax_id="7654321/B/A/000", email="accounts@officepro.example", iban="TN59 0800 1000 0123 4567 8910")
CN = dict(name="CloudNet Services", addr="8 Rue du Lac Biwa, 1053 Tunis", tax_id="1122334/C/M/000", email="finance@cloudnet.example", iban="TN59 2500 7000 0000 1122 3344")
GT = dict(name="Global Trade SARL", addr="3 Rue de Marseille, 1000 Tunis", tax_id="9988776/Z/M/000", email="contact@globaltrade.example", iban="TN59 1400 0000 9988 7766 5544")

invoice("facture_01_auto_valide.pdf", TS, "INV-TS-2026-041", "2026-09-15", "2026-10-15", "PO-2026-0101",
        [("USB-C docking station", 4, 150.0), ("Wireless keyboard + mouse set", 5, 80.0)])
invoice("facture_02_approbation.pdf", OP, "INV-OP-2026-118", "2026-09-18", "2026-10-18", "PO-2026-0102",
        [("Ergonomic office chair", 12, 250.0), ("Height-adjustable desk", 2, 600.0)])
invoice("facture_03_total_faux.pdf", CN, "INV-CN-2026-007", "2026-09-20", "2026-10-20", "PO-2026-0103",
        [("Cloud hosting - September", 1, 1500.0), ("Managed backup", 1, 300.0)], gross_override=2242.0)
invoice("facture_04_fournisseur_inconnu.pdf", GT, "GT-2026-0931", "2026-09-22", "2026-10-22", None,
        [("Printer toner cartridges", 10, 45.0)])
print("ok")
