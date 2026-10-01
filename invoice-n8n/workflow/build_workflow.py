"""Builds invoice_automation.json (n8n workflow) from readable pieces."""
import json, uuid

def nid(): return str(uuid.uuid4())

# ---------------------------------------------------------------- Code nodes
PREPARE = r"""
// Take the uploaded file from the form and build the Gemini request.
const cfg  = $('Config').first().json;
const form = $('Invoice upload form').first();
const bin  = form.binary || {};
const key  = Object.keys(bin)[0];
if (!key) throw new Error('No file received from the form');

const buffer = await this.helpers.getBinaryDataBuffer(0, key);
const mime   = bin[key].mimeType || 'application/pdf';

const prompt = `You are an accounts-payable assistant. Read this supplier invoice (PDF or scan) and extract the fields.
Rules:
- supplier_* fields describe the SELLER who issued the invoice, never the customer ("Bill to").
- supplier_tax_id is the seller's tax ID (Tunisian "matricule fiscal", e.g. 1234567/A/M/000).
- Dates in ISO format YYYY-MM-DD.
- Amounts as plain numbers (no currency, no thousands separators). Copy the totals exactly as printed; do NOT recompute or correct them.
- If a field is not on the invoice, return null.
- confidence = your overall confidence (0 to 1) that all key fields were read correctly; list unclear fields in low_confidence_fields.`;

const S = (d) => ({ type: 'STRING', nullable: true, description: d });
const N = (d) => ({ type: 'NUMBER', nullable: true, description: d });

const gemini_body = {
  contents: [{ role: 'user', parts: [
    { text: prompt },
    { inline_data: { mime_type: mime, data: buffer.toString('base64') } },
  ]}],
  generationConfig: {
    temperature: 0,
    responseMimeType: 'application/json',
    responseSchema: {
      type: 'OBJECT',
      properties: {
        invoice_number:  S('Invoice number'),
        invoice_date:    S('YYYY-MM-DD'),
        due_date:        S('YYYY-MM-DD'),
        supplier_name:   S('Seller company name'),
        supplier_tax_id: S('Seller matricule fiscal / tax ID'),
        supplier_email:  S('Seller email'),
        po_reference:    S('Purchase-order reference'),
        iban:            S('Seller IBAN / RIB'),
        currency:        S('Currency code, e.g. TND'),
        net_amount:      N('Total excluding tax (HT)'),
        vat_amount:      N('Total VAT (TVA)'),
        gross_amount:    N('Total including tax (TTC)'),
        confidence:      N('Overall confidence 0-1'),
        low_confidence_fields: { type: 'ARRAY', items: { type: 'STRING' } },
      },
      required: ['invoice_number','invoice_date','supplier_name','supplier_tax_id',
                 'net_amount','vat_amount','gross_amount','confidence'],
    },
  },
};

return [{ json: {
  gemini_body,
  gemini_url: `${cfg.gemini_base_url}/v1beta/models/${cfg.gemini_model}:generateContent`,
  file_name: bin[key].fileName || 'invoice',
  file_extension: (bin[key].fileExtension || 'pdf'),
  supplier_email_form: form.json['Supplier email'] || null,
  received_at: new Date().toISOString(),
}}];
"""

PARSE = r"""
// Read Gemini's JSON answer and prepare the database lookups.
const prep = $('Prepare Gemini request').first().json;
const res  = $input.first().json;
const text = res?.candidates?.[0]?.content?.parts?.map(p => p.text || '').join('') || '';
let inv;
try { inv = JSON.parse(text); }
catch (e) { throw new Error('Gemini did not return valid JSON: ' + text.slice(0, 300)); }

const num = (v) => {
  if (v === null || v === undefined || v === '') return null;
  const n = typeof v === 'number' ? v : parseFloat(String(v).replace(/\s/g, '').replace(',', '.'));
  return Number.isFinite(n) ? n : null;
};
const str = (v) => (v === null || v === undefined || String(v).trim() === '' ? null : String(v).trim());

const invoice = {
  invoice_number:  str(inv.invoice_number),
  invoice_date:    str(inv.invoice_date),
  due_date:        str(inv.due_date),
  supplier_name:   str(inv.supplier_name),
  supplier_tax_id: str(inv.supplier_tax_id),
  supplier_email:  str(inv.supplier_email),
  po_reference:    str(inv.po_reference),
  iban:            str(inv.iban),
  currency:        str(inv.currency) || 'TND',
  net_amount:      num(inv.net_amount),
  vat_amount:      num(inv.vat_amount),
  gross_amount:    num(inv.gross_amount),
  confidence:      num(inv.confidence) ?? 0,
  low_confidence_fields: Array.isArray(inv.low_confidence_fields) ? inv.low_confidence_fields : [],
};

// SQL-safe literal (values come from the AI, so always escape them)
const q = (v) => (v === null || v === undefined) ? 'NULL' : "'" + String(v).replace(/'/g, "''") + "'";

const lookup_sql = `
SELECT
  (SELECT row_to_json(s) FROM suppliers s
    WHERE s.tax_id = ${q(invoice.supplier_tax_id)} OR lower(s.name) = lower(${q(invoice.supplier_name)})
    LIMIT 1) AS supplier,
  (SELECT count(*)::int FROM invoices i
    WHERE i.status = 'recorded' AND i.invoice_number = ${q(invoice.invoice_number)}
      AND (i.supplier_tax_id = ${q(invoice.supplier_tax_id)} OR lower(i.supplier_name) = lower(${q(invoice.supplier_name)}))
  ) AS duplicate_count,
  (SELECT row_to_json(p) FROM purchase_orders p WHERE p.po_number = ${q(invoice.po_reference)} LIMIT 1) AS purchase_order;`;

return [{ json: { invoice, lookup_sql, file_name: prep.file_name, received_at: prep.received_at } }];
"""

RULES = r"""
// Business rules = the single "Valid data?" decision of the TO-BE workflow.
const cfg    = $('Config').first().json;
const parsed = $('Parse extraction').first().json;
const prep   = $('Prepare Gemini request').first().json;
const inv    = parsed.invoice;
const db     = $input.first().json;           // supplier / duplicate_count / purchase_order
const reasons = [];

// 1. Mandatory fields
const required = { invoice_number: 'invoice number', invoice_date: 'invoice date', supplier_name: 'supplier name',
                   net_amount: 'net amount (HT)', vat_amount: 'VAT amount', gross_amount: 'gross amount (TTC)' };
for (const [k, label] of Object.entries(required)) if (inv[k] === null) reasons.push(`Missing ${label}`);

// 2. AI confidence score >= threshold
if (inv.confidence < cfg.min_confidence)
  reasons.push(`Low AI confidence (${inv.confidence} < ${cfg.min_confidence})` +
               (inv.low_confidence_fields.length ? ` on: ${inv.low_confidence_fields.join(', ')}` : ''));

// 3. Net + VAT = Gross
if (inv.net_amount !== null && inv.vat_amount !== null && inv.gross_amount !== null) {
  const diff = Math.abs(inv.net_amount + inv.vat_amount - inv.gross_amount);
  if (diff > cfg.amount_tolerance)
    reasons.push(`Totals do not match: ${inv.net_amount} + ${inv.vat_amount} = ${(inv.net_amount + inv.vat_amount).toFixed(3)}, but invoice says ${inv.gross_amount}`);
}

// 4. Supplier exists in master data
const supplier = db.supplier || null;
if (!supplier) reasons.push(`Unknown supplier: ${inv.supplier_name || '?'} (${inv.supplier_tax_id || 'no tax ID'})`);

// 5. Duplicate invoice
if ((db.duplicate_count || 0) > 0) reasons.push(`Duplicate: invoice ${inv.invoice_number} is already recorded`);

// 6. Purchase order
const po = db.purchase_order || null;
if (!inv.po_reference) {
  if (cfg.require_po) reasons.push('Missing purchase-order reference');
} else if (!po) {
  reasons.push(`Unknown purchase order ${inv.po_reference}`);
} else {
  if (supplier && po.supplier_tax_id !== supplier.tax_id) reasons.push(`PO ${po.po_number} belongs to another supplier`);
  if (po.status !== 'open') reasons.push(`PO ${po.po_number} is ${po.status}`);
  if (inv.gross_amount !== null && inv.gross_amount > Number(po.amount_gross) + cfg.amount_tolerance)
    reasons.push(`Amount ${inv.gross_amount} exceeds PO ${po.po_number} (${po.amount_gross})`);
}

const valid = reasons.length === 0;
const approval_required = valid && inv.gross_amount > cfg.approval_threshold;
const supplier_email = prep.supplier_email_form || inv.supplier_email || supplier?.email || null;

// SQL used if the invoice is sent back to the supplier
const q = (v) => (v === null || v === undefined) ? 'NULL' : "'" + String(v).replace(/'/g, "''") + "'";
const n = (v) => (typeof v === 'number' && Number.isFinite(v)) ? String(v) : 'NULL';
const d = (v) => (v && /^\d{4}-\d{2}-\d{2}$/.test(v)) ? q(v) + '::date' : 'NULL';
const invoice_values = (status, route, why) => `(${q(inv.invoice_number)}, ${d(inv.invoice_date)}, ${d(inv.due_date)},
  ${q(supplier?.name || inv.supplier_name)}, ${q(supplier?.tax_id || inv.supplier_tax_id)}, ${q(supplier_email)},
  ${q(inv.po_reference)}, ${q(inv.iban)}, ${q(inv.currency)},
  ${n(inv.net_amount)}, ${n(inv.vat_amount)}, ${n(inv.gross_amount)}, ${n(inv.confidence)},
  ${q(status)}, ${q(route)}, ${q(why)}, ${q(parsed.file_name)}, ${q(parsed.received_at)}::timestamptz)`;
const COLS = `(invoice_number, invoice_date, due_date, supplier_name, supplier_tax_id, supplier_email,
  po_reference, iban, currency, net_amount, vat_amount, gross_amount, ai_confidence,
  status, route, rejection_reasons, file_name, received_at)`;
const sqlFor = (status, route, why, event) => `
WITH ins AS (INSERT INTO invoices ${COLS} VALUES ${invoice_values(status, route, why)} RETURNING id, status, route)
INSERT INTO audit_log (invoice_id, event, details)
SELECT id, ${q(event)}, ${q(why)} FROM ins
RETURNING invoice_id, event;`;

return [{ json: {
  valid, approval_required, reasons,
  reasons_text: reasons.join(' | '),
  invoice: inv, supplier, purchase_order: po, supplier_email,
  reject_sql: sqlFor('rejected_invalid', 'supplier_correction', reasons.join(' | '), 'SENT_BACK_TO_SUPPLIER'),
  manager_reject_sql: sqlFor('rejected_manager', 'manager_rejected', 'Rejected by approver', 'REJECTED_BY_MANAGER'),
  _cols: COLS,
}}];
"""

BUILD_ERP = r"""
// Prepare the ERP record: route (auto / approved), archive path, SQL insert.
const cfg   = $('Config').first().json;
const r     = $('Business rules check').first().json;
const prep  = $('Prepare Gemini request').first().json;
const parsed = $('Parse extraction').first().json;
const inv   = r.invoice;
const route = $('Approved?').isExecuted ? 'approved' : 'auto';

const safe = (s) => String(s || 'unknown').replace(/[^A-Za-z0-9._-]+/g, '_');
const archive_path = `${cfg.archive_dir}/${new Date().toISOString().slice(0,10)}_${safe(r.supplier?.name)}_${safe(inv.invoice_number)}.${prep.file_extension}`;

const q = (v) => (v === null || v === undefined) ? 'NULL' : "'" + String(v).replace(/'/g, "''") + "'";
const n = (v) => (typeof v === 'number' && Number.isFinite(v)) ? String(v) : 'NULL';
const d = (v) => (v && /^\d{4}-\d{2}-\d{2}$/.test(v)) ? q(v) + '::date' : 'NULL';

const insert_sql = `
WITH ins AS (
  INSERT INTO invoices ${r._cols.replace('received_at)', 'received_at, archive_path)')}
  VALUES (${q(inv.invoice_number)}, ${d(inv.invoice_date)}, ${d(inv.due_date)},
    ${q(r.supplier.name)}, ${q(r.supplier.tax_id)}, ${q(r.supplier_email)},
    ${q(inv.po_reference)}, ${q(inv.iban || r.supplier.iban)}, ${q(inv.currency)},
    ${n(inv.net_amount)}, ${n(inv.vat_amount)}, ${n(inv.gross_amount)}, ${n(inv.confidence)},
    'recorded', ${q(route)}, NULL, ${q(parsed.file_name)}, ${q(parsed.received_at)}::timestamptz, ${q(archive_path)})
  RETURNING id, invoice_number, gross_amount, route
), log AS (
  INSERT INTO audit_log (invoice_id, event, details)
  SELECT id, 'RECORDED_IN_ERP', 'route=' || route || ', archived at ' || ${q(archive_path)} FROM ins
)
SELECT * FROM ins;`;

// keep the original file for the archive step
const form = $('Invoice upload form').first().binary || {};
const key  = Object.keys(form)[0];
return [{ json: { insert_sql, archive_path, route, invoice_number: inv.invoice_number },
          binary: key ? { invoice: form[key] } : {} }];
"""

# ---------------------------------------------------------------- nodes
def code(name, js, pos):
    return {"parameters": {"jsCode": js.strip()}, "id": nid(), "name": name,
            "type": "n8n-nodes-base.code", "typeVersion": 2, "position": pos}

def pg(name, query, pos, extra=None):
    n = {"parameters": {"operation": "executeQuery", "query": query, "options": {}},
         "id": nid(), "name": name, "type": "n8n-nodes-base.postgres", "typeVersion": 2.6,
         "position": pos, "credentials": {"postgres": {"id": "pgInvoicesDb01", "name": "Postgres invoices_db"}}}
    if extra: n.update(extra)
    return n

def if_node(name, left, pos):
    return {"parameters": {"conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                "conditions": [{"id": nid(), "leftValue": left, "rightValue": "",
                                "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                "combinator": "and"}, "options": {}},
            "id": nid(), "name": name, "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": pos}

GMAIL_CRED = {"gmailOAuth2": {"id": "gmailAccount0001", "name": "Gmail account"}}

nodes = [
 {"parameters": {"formTitle": "Invoice submission",
   "formDescription": "Upload a supplier invoice (PDF or image). It is processed automatically: OCR + AI extraction, business-rule checks, approval if needed, ERP recording and archiving.",
   "formFields": {"values": [
     {"fieldLabel": "Invoice", "fieldType": "file", "multipleFiles": False,
      "acceptFileTypes": ".pdf, .png, .jpg, .jpeg", "requiredField": True},
     {"fieldLabel": "Supplier email", "fieldType": "email", "placeholder": "billing@supplier.com", "requiredField": True}]},
   "options": {"path": "invoice-upload", "buttonLabel": "Submit invoice",
               "respondWithOptions": {"values": {"respondWith": "text", "formSubmittedText": "Invoice received. It is now being processed automatically."}}}},
  "id": nid(), "name": "Invoice upload form", "type": "n8n-nodes-base.formTrigger", "typeVersion": 2.2,
  "position": [0, 300], "webhookId": nid()},

 {"parameters": {"assignments": {"assignments": [
     {"id": nid(), "name": "gemini_model", "value": "gemini-2.5-flash", "type": "string"},
     {"id": nid(), "name": "gemini_base_url", "value": "https://generativelanguage.googleapis.com", "type": "string"},
     {"id": nid(), "name": "approval_threshold", "value": 2000, "type": "number"},
     {"id": nid(), "name": "min_confidence", "value": 0.8, "type": "number"},
     {"id": nid(), "name": "amount_tolerance", "value": 0.01, "type": "number"},
     {"id": nid(), "name": "require_po", "value": True, "type": "boolean"},
     {"id": nid(), "name": "manager_email", "value": "manager@yourcompany.com", "type": "string"},
     {"id": nid(), "name": "archive_dir", "value": "/home/node/.n8n-files/archive", "type": "string"}]},
   "includeOtherFields": True, "options": {}},
  "id": nid(), "name": "Config", "type": "n8n-nodes-base.set", "typeVersion": 3.4, "position": [220, 300]},

 code("Prepare Gemini request", PREPARE, [440, 300]),

 {"parameters": {"method": "POST", "url": "={{ $json.gemini_url }}",
   "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
   "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json.gemini_body) }}",
   "options": {"timeout": 120000}},
  "id": nid(), "name": "Gemini: OCR + AI extraction", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
  "position": [660, 300], "retryOnFail": True, "maxTries": 3, "waitBetweenTries": 5000,
  "credentials": {"httpHeaderAuth": {"id": "geminiApiKey0001", "name": "Gemini API key"}}},

 code("Parse extraction", PARSE, [880, 300]),
 pg("Lookup supplier, PO & duplicates", "={{ $json.lookup_sql }}", [1100, 300]),
 code("Business rules check", RULES, [1320, 300]),
 if_node("Valid data?", "={{ $json.valid }}", [1540, 300]),

 # ---- invalid -> supplier correction
 pg("Save: sent back to supplier", "={{ $('Business rules check').first().json.reject_sql }}", [1760, 520]),
 {"parameters": {"sendTo": "={{ $('Business rules check').first().json.supplier_email }}",
   "subject": "=Invoice {{ $('Business rules check').first().json.invoice.invoice_number || '' }}: correction needed",
   "emailType": "text",
   "message": "=Hello,\n\nWe could not process your invoice {{ $('Business rules check').first().json.invoice.invoice_number || '' }} for the following reason(s):\n\n- {{ $('Business rules check').first().json.reasons.join('\\n- ') }}\n\nPlease send a corrected invoice through the same form.\n\nAccounts Payable",
   "options": {"appendAttribution": False}},
  "id": nid(), "name": "Email supplier: correction needed", "type": "n8n-nodes-base.gmail", "typeVersion": 2.1,
  "position": [1980, 520], "webhookId": nid(), "credentials": GMAIL_CRED},

 # ---- valid -> approval?
 if_node("Approval required?", "={{ $json.approval_required }}", [1760, 200]),
 {"parameters": {"operation": "sendAndWait",
   "sendTo": "={{ $('Config').first().json.manager_email }}",
   "subject": "=Approval needed: invoice {{ $json.invoice.invoice_number }} ({{ $json.invoice.gross_amount }} {{ $json.invoice.currency }})",
   "message": "=Invoice {{ $json.invoice.invoice_number }} from {{ $json.supplier.name }} is above the approval threshold ({{ $('Config').first().json.approval_threshold }} TND).\n\nAmount: {{ $json.invoice.net_amount }} + VAT {{ $json.invoice.vat_amount }} = {{ $json.invoice.gross_amount }} {{ $json.invoice.currency }}\nPurchase order: {{ $json.invoice.po_reference }}\nInvoice date: {{ $json.invoice.invoice_date }} - due {{ $json.invoice.due_date }}\n\nAll automatic checks passed. Do you approve this invoice?",
   "approvalOptions": {"values": {"approvalType": "double", "approveLabel": "Approve", "disapproveLabel": "Reject"}},
   "options": {"appendAttribution": False}},
  "id": nid(), "name": "Approval workflow (manager)", "type": "n8n-nodes-base.gmail", "typeVersion": 2.1,
  "position": [1980, 80], "webhookId": nid(), "credentials": GMAIL_CRED},
 if_node("Approved?", "={{ $json.data.approved }}", [2200, 80]),
 pg("Save: rejected by manager", "={{ $('Business rules check').first().json.manager_reject_sql }}", [2420, -60]),

 # ---- ERP, archive, reporting
 code("Build ERP record", BUILD_ERP, [2420, 220]),
 {"parameters": {"operation": "write", "fileName": "={{ $json.archive_path }}", "dataPropertyName": "invoice", "options": {}},
  "id": nid(), "name": "Archiving", "type": "n8n-nodes-base.readWriteFile", "typeVersion": 1, "position": [2640, 220]},
 pg("ERP integration (record invoice)", "={{ $('Build ERP record').first().json.insert_sql }}", [2860, 220]),
 pg("Payment & KPI reporting", "SELECT * FROM kpi_summary;", [3080, 220]),

 # ---- sticky notes
 {"parameters": {"content": "## Invoice automation (TO-BE)\n1. **Reception**: n8n form (PDF / image)\n2. **IDP / OCR + AI**: Gemini reads the file and returns structured JSON\n3. **AI analysis & check**: confidence score + business rules (totals, supplier, PO, duplicate)\n4. **Valid data?** No -> supplier correction email\n5. **Approval required?** amount > threshold -> manager approves by email\n6. **ERP integration**, **archiving**, **KPI reporting** (PostgreSQL)\n\nSettings: edit the **Config** node.",
   "height": 300, "width": 460, "color": 5},
  "id": nid(), "name": "Sticky Note", "type": "n8n-nodes-base.stickyNote", "typeVersion": 1, "position": [-40, -80]},
]

C = lambda a, b, i=0: (a, b, i)
links = [
 C("Invoice upload form", "Config"), C("Config", "Prepare Gemini request"),
 C("Prepare Gemini request", "Gemini: OCR + AI extraction"), C("Gemini: OCR + AI extraction", "Parse extraction"),
 C("Parse extraction", "Lookup supplier, PO & duplicates"), C("Lookup supplier, PO & duplicates", "Business rules check"),
 C("Business rules check", "Valid data?"),
 C("Valid data?", "Approval required?", 0), C("Valid data?", "Save: sent back to supplier", 1),
 C("Save: sent back to supplier", "Email supplier: correction needed"),
 C("Approval required?", "Approval workflow (manager)", 0), C("Approval required?", "Build ERP record", 1),
 C("Approval workflow (manager)", "Approved?"),
 C("Approved?", "Build ERP record", 0), C("Approved?", "Save: rejected by manager", 1),
 C("Build ERP record", "Archiving"), C("Archiving", "ERP integration (record invoice)"),
 C("ERP integration (record invoice)", "Payment & KPI reporting"),
]
connections = {}
for a, b, i in links:
    outs = connections.setdefault(a, {"main": []})["main"]
    while len(outs) <= i: outs.append([])
    outs[i].append({"node": b, "type": "main", "index": 0})

wf = {"name": "Invoice Automation - IDP (Gemini) + Rules + Approval + ERP", "nodes": nodes,
      "connections": connections, "active": False, "settings": {"executionOrder": "v1"},
      "pinData": {}, "meta": {"templateCredsSetupCompleted": False}, "tags": []}

import sys
out = sys.argv[1] if len(sys.argv) > 1 else "invoice_automation.json"
json.dump(wf, open(out, "w"), indent=2, ensure_ascii=False)
print("written", out, len(nodes), "nodes")
