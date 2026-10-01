"""Builds the n8n workflows of the invoice-automation prototype (Ollama + Google Sheets/Drive/Gmail).

    python workflows/build_workflows.py          -> writes workflows/*.json from the values in .env

Generated files (import with scripts/n8n-cli.ps1 import:workflow --input=...):
    00_setup_google_sheet.json   one-time: creates the "Invoice Automation DB" spreadsheet, the Drive
                                 folder "Invoice Archive" and the Gmail label INVOICE_AUTOMATION
    01_invoice_automation.json   the pipeline: form + Gmail intake -> OCR -> Ollama -> rules ->
                                 supplier correction / manager approval -> ERP (Sheets) -> Drive archive -> KPI
    99_read_sheet.json           test helper: returns the rows of Invoices, AuditLog and KPI

Only IDs and settings go into the JSON. Google secrets stay in n8n credentials.
"""
import json
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "workflows"


def load_env():
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


ENV = load_env()
E = lambda k, d="": ENV.get(k) or d

WF_SETUP, WF_MAIN, WF_READ = "invSetupSheet001", "invAutomation001", "invReadSheet0001"
CRED_SHEETS = {"googleSheetsOAuth2Api": {"id": "gsheetInvoice001", "name": "Google Sheets (kharratok)"}}
CRED_DRIVE = {"googleDriveOAuth2Api": {"id": "gdriveInvoice001", "name": "Google Drive (kharratok)"}}
CRED_GMAIL = {"gmailOAuth2": {"id": "gmailInvoice0001", "name": "Gmail (kharratok)"}}

INVOICE_COLUMNS = ["invoice_id", "invoice_number", "invoice_date", "due_date", "supplier_name", "supplier_tax_id",
                   "supplier_email", "po_reference", "iban", "currency", "net_amount", "vat_amount", "gross_amount",
                   "ai_confidence", "status", "route", "rejection_reasons", "approved_by", "payment_status",
                   "file_name", "archive_link", "received_at", "processed_at"]


def nid(name):  # stable node ids -> re-imports keep the same nodes
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "invoice-n8n/" + name))


# ============================================================================ node helpers
def node(name, type_, ver, params, pos, **extra):
    n = {"parameters": params, "id": nid(name), "name": name, "type": type_, "typeVersion": ver, "position": pos}
    n.update(extra)
    return n


def code(name, js, pos, **extra):
    return node(name, "n8n-nodes-base.code", 2, {"jsCode": js.strip()}, pos, **extra)


def if_true(name, expr, pos):
    return node(name, "n8n-nodes-base.if", 2.2, {
        "conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                       "conditions": [{"id": nid(name + "/c"), "leftValue": expr, "rightValue": "",
                                       "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                       "combinator": "and"}, "options": {}}, pos)


def doc(sheet_id_expr):
    return {"__rl": True, "value": sheet_id_expr, "mode": "id"}


def tab(name):
    return {"__rl": True, "value": name, "mode": "name"}


def sheet_read(name, sheet_expr, tab_name, pos, filters=None):
    p = {"operation": "read", "documentId": doc(sheet_expr), "sheetName": tab(tab_name), "options": {}}
    if filters:
        p["filtersUI"] = {"values": [{"lookupColumn": c, "lookupValue": v} for c, v in filters]}
    return node(name, "n8n-nodes-base.googleSheets", 4.5, p, pos, credentials=CRED_SHEETS,
                alwaysOutputData=True, executeOnce=True)


def sheet_append(name, sheet_expr, tab_name, pos):
    return node(name, "n8n-nodes-base.googleSheets", 4.5, {
        "operation": "append", "documentId": doc(sheet_expr), "sheetName": tab(tab_name),
        "columns": {"mappingMode": "autoMapInputData", "value": {}, "matchingColumns": [], "schema": [],
                    "attemptToConvertTypes": False, "convertFieldsToString": False},
        "options": {"cellFormat": "USER_ENTERED"}}, pos, credentials=CRED_SHEETS)


def http_json(name, url, body_expr, pos, cred=None, **extra):
    p = {"method": "POST", "url": url, "sendBody": True, "specifyBody": "json", "jsonBody": body_expr,
         "options": {"timeout": 600000}}
    if cred:
        p.update({"authentication": "predefinedCredentialType", "nodeCredentialType": list(cred)[0]})
        extra["credentials"] = cred
    return node(name, "n8n-nodes-base.httpRequest", 4.2, p, pos, **extra)


def sticky(name, text, pos, w, h, color):
    return node(name, "n8n-nodes-base.stickyNote", 1, {"content": text, "width": w, "height": h, "color": color}, pos)


def connect(links):
    conns = {}
    for a, b, *i in links:
        outs = conns.setdefault(a, {"main": []})["main"]
        idx = i[0] if i else 0
        while len(outs) <= idx:
            outs.append([])
        outs[idx].append({"node": b, "type": "main", "index": 0})
    return conns


def workflow(wid, name, nodes, links):
    return {"id": wid, "name": name, "nodes": nodes, "connections": connect(links), "active": False,
            "settings": {"executionOrder": "v1", "saveManualExecutions": True, "saveDataSuccessExecution": "all",
                         "saveDataErrorExecution": "all", "saveExecutionProgress": True,
                         "callerPolicy": "workflowsFromSameOwner", "timezone": E("GENERIC_TIMEZONE", "Africa/Tunis")},
            "pinData": {}, "meta": {"templateCredsSetupCompleted": True}, "tags": []}


# ============================================================================ 00 setup
SETUP_BUILD = r"""
// Header rows, KPI formulas (COUNTIF / SUMIF / AVERAGE on Invoices) and the "invoices by status" chart.
const ss = $input.first().json;
const id = ss.spreadsheetId;
const sheetId = Object.fromEntries((ss.sheets || []).map(s => [s.properties.title, s.properties.sheetId]));
const COLS = __INVOICE_COLUMNS__;
const col = (name) => String.fromCharCode(65 + COLS.indexOf(name));          // A..W
const R = (name) => `Invoices!${col(name)}2:${col(name)}`;

const kpi = [
  ['KPI', 'Value'],
  ['Invoices processed',                  `=COUNTA(${R('invoice_id')})`],
  ['Invoices recorded',                   `=COUNTIF(${R('status')},"recorded")`],
  ['Touchless (route = auto)',            `=COUNTIF(${R('route')},"auto")`],
  ['Approved by manager',                 `=COUNTIF(${R('route')},"approved")`],
  ['Sent back to supplier',               `=COUNTIF(${R('status')},"rejected_invalid")`],
  ['Rejected by manager',                 `=COUNTIF(${R('status')},"rejected_manager")`],
  ['Touchless rate %',                    '=IF(B2=0,0,ROUND(100*B4/B2,1))'],
  ['Exception rate %',                    '=IF(B2=0,0,ROUND(100*B6/B2,1))'],
  ['Average processing time (s, touchless)', `=IFERROR(ROUND(AVERAGE(FILTER((${R('processed_at')}-${R('received_at')})*86400,${R('route')}="auto")),1),0)`],
  ['Average processing time (s, all)',    `=IFERROR(ROUND(AVERAGE(FILTER((${R('processed_at')}-${R('received_at')})*86400,${R('invoice_id')}<>"")),1),0)`],
  ['Total amount recorded (TND)',         `=SUMIF(${R('status')},"recorded",${R('gross_amount')})`],
  ['Payments scheduled (simulated)',      `=COUNTIF(${R('payment_status')},"scheduled")`],
];
const byStatus = [
  ['Status', 'Invoices'],
  ['recorded',         `=COUNTIF(${R('status')},D2)`],
  ['rejected_invalid', `=COUNTIF(${R('status')},D3)`],
  ['rejected_manager', `=COUNTIF(${R('status')},D4)`],
];

const values_body = { valueInputOption: 'USER_ENTERED', data: [
  { range: 'Suppliers!A1:E1',      values: [['supplier_id', 'name', 'tax_id', 'email', 'iban']] },
  { range: 'PurchaseOrders!A1:D1', values: [['po_number', 'supplier_tax_id', 'amount_gross', 'status']] },
  { range: 'Invoices!A1:W1',       values: [COLS] },
  { range: 'AuditLog!A1:D1',       values: [['timestamp', 'invoice_id', 'event', 'details']] },
  { range: 'KPI!A1:B13',           values: kpi },
  { range: 'KPI!D1:E4',            values: byStatus },
]};

const bold = (sid, cols) => ({ repeatCell: { range: { sheetId: sid, startRowIndex: 0, endRowIndex: 1, startColumnIndex: 0, endColumnIndex: cols },
  cell: { userEnteredFormat: { textFormat: { bold: true }, backgroundColor: { red: 0.91, green: 0.93, blue: 0.95 } } },
  fields: 'userEnteredFormat(textFormat,backgroundColor)' } });
const freeze = (sid) => ({ updateSheetProperties: { properties: { sheetId: sid, gridProperties: { frozenRowCount: 1 } }, fields: 'gridProperties.frozenRowCount' } });
const dt = (c) => ({ repeatCell: { range: { sheetId: sheetId.Invoices, startRowIndex: 1, startColumnIndex: COLS.indexOf(c), endColumnIndex: COLS.indexOf(c) + 1 },
  cell: { userEnteredFormat: { numberFormat: { type: 'DATE_TIME', pattern: 'yyyy-mm-dd hh:mm:ss' } } }, fields: 'userEnteredFormat.numberFormat' } });

const format_body = { requests: [
  { updateSpreadsheetProperties: { properties: { timeZone: '__TZ__', locale: 'en_US' }, fields: 'timeZone,locale' } },
  ...['Suppliers', 'PurchaseOrders', 'Invoices', 'AuditLog', 'KPI'].map(t => freeze(sheetId[t])),
  bold(sheetId.Suppliers, 5), bold(sheetId.PurchaseOrders, 4), bold(sheetId.Invoices, 23), bold(sheetId.AuditLog, 4), bold(sheetId.KPI, 5),
  dt('received_at'), dt('processed_at'),
  { updateDimensionProperties: { range: { sheetId: sheetId.KPI, dimension: 'COLUMNS', startIndex: 0, endIndex: 1 }, properties: { pixelSize: 300 }, fields: 'pixelSize' } },
  { addChart: { chart: {
      spec: { title: 'Invoices by status', basicChart: { chartType: 'COLUMN', legendPosition: 'NO_LEGEND',
        axis: [{ position: 'BOTTOM_AXIS', title: 'Status' }, { position: 'LEFT_AXIS', title: 'Invoices' }],
        domains: [{ domain: { sourceRange: { sources: [{ sheetId: sheetId.KPI, startRowIndex: 0, endRowIndex: 4, startColumnIndex: 3, endColumnIndex: 4 }] } } }],
        series:  [{ series: { sourceRange: { sources: [{ sheetId: sheetId.KPI, startRowIndex: 0, endRowIndex: 4, startColumnIndex: 4, endColumnIndex: 5 }] } }, targetAxis: 'LEFT_AXIS' }],
        headerCount: 1 } },
      position: { overlayPosition: { anchorCell: { sheetId: sheetId.KPI, rowIndex: 6, columnIndex: 3 }, widthPixels: 520, heightPixels: 320 } } } } },
]};

return [{ json: { spreadsheetId: id, spreadsheetUrl: ss.spreadsheetUrl, values_body, format_body } }];
"""

SEED_SUPPLIERS = r"""
// Demo master data, same as sql/init.sql (fictitious companies). The apostrophe keeps tax IDs as text.
return [
  { supplier_id: 1, name: 'Tech Supplies SARL', tax_id: "'1234567/A/M/000", email: 'billing@techsupplies.example', iban: 'TN59 1000 6035 1835 9847 8831' },
  { supplier_id: 2, name: 'Office Pro SA',      tax_id: "'7654321/B/A/000", email: 'accounts@officepro.example',   iban: 'TN59 0800 1000 0123 4567 8910' },
  { supplier_id: 3, name: 'CloudNet Services',  tax_id: "'1122334/C/M/000", email: 'finance@cloudnet.example',     iban: 'TN59 2500 7000 0000 1122 3344' },
].map(json => ({ json }));
"""

SEED_POS = r"""
return [
  { po_number: 'PO-2026-0101', supplier_tax_id: "'1234567/A/M/000", amount_gross: 1500, status: 'open' },
  { po_number: 'PO-2026-0102', supplier_tax_id: "'7654321/B/A/000", amount_gross: 6000, status: 'open' },
  { po_number: 'PO-2026-0103', supplier_tax_id: "'1122334/C/M/000", amount_gross: 2500, status: 'open' },
].map(json => ({ json }));
"""

SETUP_SUMMARY = r"""
// IDs to copy into .env (GOOGLE_SHEET_ID, GOOGLE_DRIVE_FOLDER_ID, GMAIL_LABEL_ID).
const ss = $('Build headers, KPI & chart').first().json;
const folder = $('Drive: create folder "Invoice Archive"').first().json;
const label = $('Gmail: list labels').all().map(i => i.json).find(l => l.name === '__LABEL__');
return [{ json: {
  GOOGLE_SHEET_ID: ss.spreadsheetId, sheet_url: ss.spreadsheetUrl,
  GOOGLE_DRIVE_FOLDER_ID: folder.id, folder_url: `https://drive.google.com/drive/folders/${folder.id}`,
  GMAIL_LABEL_ID: label ? label.id : null,
}}];
"""


def build_setup():
    sid = "={{ $('Build headers, KPI & chart').first().json.spreadsheetId }}"
    nodes = [
        node("Run setup once", "n8n-nodes-base.manualTrigger", 1, {}, [0, 300]),
        node("Sheets: create spreadsheet", "n8n-nodes-base.googleSheets", 4.5, {
            "authentication": "oAuth2", "resource": "spreadsheet", "operation": "create", "title": "Invoice Automation DB",
            "sheetsUi": {"sheetValues": [{"title": t} for t in ["Suppliers", "PurchaseOrders", "Invoices", "AuditLog", "KPI"]]},
            "options": {"locale": "en_US"}}, [220, 300], credentials=CRED_SHEETS),
        code("Build headers, KPI & chart",
             SETUP_BUILD.replace("__INVOICE_COLUMNS__", json.dumps(INVOICE_COLUMNS))
                        .replace("__TZ__", E("GENERIC_TIMEZONE", "Africa/Tunis")), [440, 300]),
        http_json("Sheets API: headers & KPI formulas",
                  "=https://sheets.googleapis.com/v4/spreadsheets/{{ $json.spreadsheetId }}/values:batchUpdate",
                  "={{ JSON.stringify($json.values_body) }}", [660, 300], CRED_SHEETS),
        http_json("Sheets API: format & chart",
                  "=https://sheets.googleapis.com/v4/spreadsheets/{{ $('Build headers, KPI & chart').first().json.spreadsheetId }}:batchUpdate",
                  "={{ JSON.stringify($('Build headers, KPI & chart').first().json.format_body) }}", [880, 300], CRED_SHEETS),
        code("Seed suppliers", SEED_SUPPLIERS, [1100, 300], executeOnce=True),
        sheet_append("Suppliers: append rows", sid, "Suppliers", [1320, 300]),
        code("Seed purchase orders", SEED_POS, [1540, 300], executeOnce=True),
        sheet_append("PurchaseOrders: append rows", sid, "PurchaseOrders", [1760, 300]),
        node('Drive: create folder "Invoice Archive"', "n8n-nodes-base.googleDrive", 3, {
            "resource": "folder", "name": "Invoice Archive",
            "driveId": {"__rl": True, "mode": "list", "value": "My Drive"},
            "folderId": {"__rl": True, "mode": "list", "value": "root", "cachedResultName": "/ (Root folder)"},
            "options": {}}, [1980, 300], credentials=CRED_DRIVE, executeOnce=True),
        node("Gmail: create label", "n8n-nodes-base.gmail", 2.1, {
            "resource": "label", "operation": "create", "name": E("GMAIL_LABEL_NAME", "INVOICE_AUTOMATION"), "options": {}},
             [2200, 300], credentials=CRED_GMAIL, executeOnce=True, onError="continueRegularOutput"),
        node("Gmail: list labels", "n8n-nodes-base.gmail", 2.1, {"resource": "label", "operation": "getAll", "returnAll": True},
             [2420, 300], credentials=CRED_GMAIL, executeOnce=True),
        code("Setup summary", SETUP_SUMMARY.replace("__LABEL__", E("GMAIL_LABEL_NAME", "INVOICE_AUTOMATION")), [2640, 300]),
        sticky("Note setup", "## 00 - One-time setup\nCreates the Google Sheet **Invoice Automation DB** (tabs, headers, demo suppliers & POs, KPI formulas + chart), the Drive folder **Invoice Archive** and the Gmail label **INVOICE_AUTOMATION**.\n\nRun it **once**, then copy the IDs from *Setup summary* into `.env`.",
               [0, 0], 520, 220, 5),
    ]
    order = ["Run setup once", "Sheets: create spreadsheet", "Build headers, KPI & chart", "Sheets API: headers & KPI formulas",
             "Sheets API: format & chart", "Seed suppliers", "Suppliers: append rows", "Seed purchase orders",
             "PurchaseOrders: append rows", 'Drive: create folder "Invoice Archive"', "Gmail: create label",
             "Gmail: list labels", "Setup summary"]
    return workflow(WF_SETUP, "00_setup_google_sheet", nodes, list(zip(order, order[1:])))


# ============================================================================ 01 main workflow
NORMALIZE_FORM = r"""
// Intake 1/2 - n8n form upload -> one invoice item {json: metadata, binary.invoice: file}
const item = $input.first();
const bin = item.binary || {};
const key = Object.keys(bin)[0];
if (!key) throw new Error('No file received from the form');
return [{ json: {
  source: 'form',
  // optional: where to send the correction email (otherwise: supplier email from the master data)
  supplier_email_intake: item.json['Email du fournisseur (facultatif)'] || null,
  gmail_message_id: null, email_subject: null,
  received_at: $now.toFormat('yyyy-MM-dd HH:mm:ss'),
  file_name: bin[key].fileName || 'invoice',
}, binary: { invoice: bin[key] } }];
"""

NORMALIZE_GMAIL = r"""
// Intake 2/2 - Gmail: one invoice item per PDF / PNG / JPG attachment (other attachments are ignored)
const OK_MIME = /^(application\/pdf|image\/png|image\/jpe?g)$/i;
const OK_EXT = ['pdf', 'png', 'jpg', 'jpeg'];
const out = [];
for (const item of $input.all()) {
  const m = item.json;
  const from = m.from?.value?.[0]?.address || null;
  for (const b of Object.values(item.binary || {})) {
    const ext = String(b.fileExtension || (b.fileName || '').split('.').pop() || '').toLowerCase();
    if (!OK_MIME.test(b.mimeType || '') && !OK_EXT.includes(ext)) continue;
    out.push({ json: {
      source: 'gmail',
      supplier_email_intake: from,
      gmail_message_id: m.id, email_subject: m.subject || null,
      received_at: $now.toFormat('yyyy-MM-dd HH:mm:ss'),
      file_name: b.fileName || 'attachment',
    }, binary: { invoice: b } });
  }
}
return out;
"""

PREPARE_INVOICE = r"""
// One invoice = one execution of this pipeline. Give it a unique ID and keep the original file.
const cfg  = $('Config').first().json;
const trig = $('When called by intake').first();
const j    = trig.json;
const bin  = trig.binary?.invoice;
if (!bin) throw new Error('No invoice file received from the intake');

const rnd = Math.random().toString(36).slice(2, 6).toUpperCase();
const invoice_id = `INV-${$now.setZone(cfg.timezone).toFormat('yyyyMMdd-HHmmss')}-${rnd}`;
const file_name = bin.fileName || j.file_name || 'invoice';
return [{ json: {
  invoice_id,
  source: j.source || 'unknown',
  supplier_email_intake: j.supplier_email_intake || null,
  gmail_message_id: j.gmail_message_id || null,
  email_subject: j.email_subject || null,
  received_at: j.received_at || $now.setZone(cfg.timezone).toFormat('yyyy-MM-dd HH:mm:ss'),
  file_name,
  file_extension: String(bin.fileExtension || file_name.split('.').pop() || 'bin').toLowerCase(),
  mime_type: bin.mimeType || null,
}, binary: { invoice: bin } }];
"""

PREPARE_AI = r"""
// Build the Ollama /api/chat request (local LLM only - no invoice data leaves the PC).
const cfg = $('Config').first().json;
const ocr = $input.first().json;
const msg = (e) => typeof e === 'string' ? e : (e?.message || (e ? JSON.stringify(e) : null));
// OCR service unreachable = OUR technical problem, not the supplier's: fail the execution (visible in n8n).
// A corrupt / unsupported file comes back as {ok:false, error} (HTTP 422) and is handled as a supplier correction.
if (ocr.ok === undefined && ocr.error) throw new Error(`OCR service unavailable (${cfg.ocr_url}): ${msg(ocr.error)}`);
const ocr_ok = ocr.ok === true && typeof ocr.text === 'string' && ocr.text.trim().length > 0;
const ocr_error = ocr_ok ? null : (msg(ocr.error) || 'OCR service returned no text');

const system = `You are an accounts-payable data-extraction engine. You receive the raw text of ONE supplier invoice
(from a PDF text layer or from OCR; OCR text may contain small errors). Return ONLY the JSON object defined by the schema.
Rules:
- supplier_* fields describe the SELLER who issued the invoice (letterhead at the top), never the customer of the
  "Bill to" / "Client" / "Facturé à" block.
- supplier_tax_id = the SELLER's Tunisian matricule fiscal (MF), e.g. 1234567/A/M/000. Never the customer's MF.
- invoice_number = the invoice number exactly as printed (Invoice No, N° facture, Facture n°). If no invoice number is
  printed, return null. Never invent one and never use the PO reference or a date instead.
- po_reference = purchase-order reference (PO, BC, bon de commande); null if absent or "-".
- Dates as YYYY-MM-DD.
- Amounts as plain numbers. A space inside a number is a thousands separator ("1 190.000" = 1190). Tunisian dinar
  amounts have 3 decimals. net_amount = Total HT, vat_amount = total TVA, gross_amount = Total TTC.
  Copy the totals exactly as printed: do NOT recompute or correct them.
- currency = ISO code (TND for DT / dinar).
- If a field is not on the invoice, return null.
- confidence = your overall confidence (0 to 1) that all key fields were read correctly;
  list unclear or missing fields in low_confidence_fields.`;

const S = (d) => ({ type: ['string', 'null'], description: d });
const N = (d) => ({ type: ['number', 'null'], description: d });
const schema = { type: 'object', properties: {
  invoice_number:  S('Invoice number as printed'),
  invoice_date:    S('YYYY-MM-DD'),
  due_date:        S('YYYY-MM-DD'),
  supplier_name:   S('Seller company name'),
  supplier_tax_id: S('Seller matricule fiscal'),
  supplier_email:  S('Seller email'),
  po_reference:    S('Purchase-order reference'),
  iban:            S('Seller IBAN / RIB'),
  currency:        S('ISO currency code'),
  net_amount:      N('Total HT'),
  vat_amount:      N('Total TVA'),
  gross_amount:    N('Total TTC'),
  confidence:      { type: 'number', description: '0 to 1' },
  low_confidence_fields: { type: 'array', items: { type: 'string' } },
}};
schema.required = Object.keys(schema.properties);

const body = (model) => ({
  model, stream: false, think: false, format: schema, keep_alive: '30m',
  options: { temperature: 0, num_ctx: 8192 },
  messages: [
    { role: 'system', content: system },
    { role: 'user', content: `Invoice text (extraction method: ${ocr.method}):\n<<<\n${String(ocr.text || '').slice(0, 15000)}\n>>>` },
  ],
});

return [{ json: {
  ocr_ok, ocr_error,
  ocr_method: ocr.method || 'failed',
  ocr_confidence: ocr.ocr_confidence ?? null,
  text_chars: ocr.chars || 0,
  ollama_url: String(cfg.ollama_url).replace(/\/$/, '') + '/api/chat',
  body_main: body(cfg.ollama_model),
  body_fallback: body(cfg.ollama_fallback_model),
}}];
"""

CHECK_AI = r"""
// Is the main model's answer a usable JSON object? If not, the fallback model is tried.
const res = $input.first().json;
let data = null, error = null;
if (res.error) {
  error = typeof res.error === 'string' ? res.error : (res.error.message || JSON.stringify(res.error));
} else {
  try {
    data = JSON.parse(res?.message?.content ?? '');
    if (!data || typeof data !== 'object' || Array.isArray(data) || !('gross_amount' in data)) { data = null; error = 'not the expected JSON object'; }
  } catch (e) { error = 'invalid JSON: ' + String(res?.message?.content).slice(0, 200); }
}
return [{ json: { ai_ok: data !== null, data, error, llm_seconds: Math.round((res.total_duration || 0) / 1e8) / 10 } }];
"""

VALIDATE = r"""
// Validate + normalise the LLM JSON before anything uses it (deterministic code, not the LLM).
const cfg  = $('Config').first().json;
const prep = $('Prepare AI request').first().json;
const ocrText = String($('OCR: extract text').first().json.text || '');
const fatal = [], warnings = [];
let raw = null, model_used = null;

const parse = (c) => {
  if (typeof c !== 'string') return null;
  try { const j = JSON.parse(c.replace(/^\s*```(json)?/i, '').replace(/```\s*$/, '')); return j && typeof j === 'object' && !Array.isArray(j) ? j : null; }
  catch (e) { return null; }
};

if (!prep.ocr_ok) {
  fatal.push(`Unreadable file: ${prep.ocr_error}`);
} else if ($('Ollama: fallback model').isExecuted) {
  model_used = cfg.ollama_fallback_model;
  warnings.push(`${cfg.ollama_model} gave no usable JSON (${$('Check AI answer').first().json.error}); fallback ${model_used} used`);
  const fb = $input.first().json;
  if (fb.error && !fb.message)   // Ollama unreachable for both models = technical error, not the supplier's fault
    throw new Error(`Ollama unavailable (${cfg.ollama_url}): ${typeof fb.error === 'string' ? fb.error : fb.error.message}`);
  raw = parse(fb?.message?.content);
  if (!raw) fatal.push(`AI extraction failed: no valid JSON from ${cfg.ollama_model} nor ${cfg.ollama_fallback_model}`);
} else {
  model_used = cfg.ollama_model;
  raw = $('Check AI answer').first().json.data;
}
raw = raw || {};

const norm = (s) => String(s ?? '').toUpperCase().replace(/[^A-Z0-9]/g, '');
const str = (v) => {
  if (v === null || v === undefined) return null;
  const s = String(v).trim();
  return s === '' || /^(null|none|n\/a|-)$/i.test(s) ? null : s;
};
const num = (v) => {
  if (v === null || v === undefined || v === '') return null;
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  let s = String(v).replace(/[^\d,.\-]/g, '');
  if (s.includes(',') && s.includes('.')) s = s.lastIndexOf(',') > s.lastIndexOf('.') ? s.replace(/\./g, '').replace(',', '.') : s.replace(/,/g, '');
  else if (s.includes(',')) s = s.replace(',', '.');
  const n = parseFloat(s);
  return Number.isFinite(n) ? n : null;
};
const date = (v, field) => {
  const s = str(v);
  if (!s) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
  const m = s.match(/^(\d{1,2})[\/.\-](\d{1,2})[\/.\-](\d{4})$/);
  if (m) return `${m[3]}-${m[2].padStart(2, '0')}-${m[1].padStart(2, '0')}`;
  warnings.push(`${field} has an unexpected format (${s})`);
  return null;
};
const currency = (v) => {
  const s = (str(v) || '').toUpperCase();
  if (/^[A-Z]{3}$/.test(s)) return s;
  if (/DT|DINAR/.test(s)) return 'TND';
  return cfg.default_currency;
};

const lowConf = Array.isArray(raw.low_confidence_fields) ? raw.low_confidence_fields.map(String) : [];
let confidence = num(raw.confidence) ?? 0;
if (confidence > 1 && confidence <= 100) confidence = confidence / 100;
confidence = Math.max(0, Math.min(1, confidence));

const invoice = {
  invoice_number:  str(raw.invoice_number),
  invoice_date:    date(raw.invoice_date, 'invoice_date'),
  due_date:        date(raw.due_date, 'due_date'),
  supplier_name:   str(raw.supplier_name),
  supplier_tax_id: str(raw.supplier_tax_id) ? str(raw.supplier_tax_id).toUpperCase().replace(/\s+/g, '') : null,
  supplier_email:  str(raw.supplier_email),
  po_reference:    str(raw.po_reference),
  iban:            str(raw.iban),
  currency:        currency(raw.currency),
  net_amount:      num(raw.net_amount),
  vat_amount:      num(raw.vat_amount),
  gross_amount:    num(raw.gross_amount),
  confidence,
  low_confidence_fields: lowConf,
};

if (prep.ocr_ok && model_used) {
  // Anti-hallucination: identifiers must literally appear in the document text.
  for (const f of ['invoice_number', 'po_reference', 'supplier_tax_id']) {
    if (invoice[f] && !norm(ocrText).includes(norm(invoice[f]))) {
      warnings.push(`${f} "${invoice[f]}" not found in the document text - ignored`);
      invoice[f] = null; lowConf.push(f);
    }
  }
  // Our own (customer) tax ID must never be taken as the supplier's.
  if (invoice.supplier_tax_id && norm(invoice.supplier_tax_id) === norm(cfg.company_tax_id)) {
    warnings.push(`AI returned the customer's tax ID ${invoice.supplier_tax_id} as supplier tax ID - ignored`);
    invoice.supplier_tax_id = null; lowConf.push('supplier_tax_id');
  }
}
invoice.low_confidence_fields = [...new Set(lowConf)];

return [{ json: {
  invoice, fatal_reasons: fatal, warnings, model_used,
  ocr_ok: prep.ocr_ok, ocr_error: prep.ocr_error, ocr_method: prep.ocr_method,
  ocr_confidence: prep.ocr_confidence, text_chars: prep.text_chars,
  llm_seconds: $('Check AI answer').isExecuted ? $('Check AI answer').first().json.llm_seconds : null,
}}];
"""

RULES = r"""
// Business rules = the single "Valid data?" decision of the TO-BE workflow (ported from the PostgreSQL version).
const cfg = $('Config').first().json;
const v   = $('Validate extraction').first().json;
const p   = $('Prepare invoice').first().json;
const inv = v.invoice;
const rows = (name) => $(name).all().map(i => i.json).filter(j => j && Object.keys(j).length > 0);
const suppliers = rows('Get suppliers');
const pos       = rows('Get purchase orders');
const recorded  = rows('Get recorded invoices');
const norm = (s) => String(s ?? '').toUpperCase().replace(/[^A-Z0-9]/g, '');
const lc   = (s) => String(s ?? '').trim().toLowerCase();
const reasons  = [...v.fatal_reasons];
const warnings = [...v.warnings];
let supplier = null, po = null;

if (reasons.length === 0) {
  // 1. Mandatory fields
  const required = { invoice_number: 'invoice number', invoice_date: 'invoice date', supplier_name: 'supplier name',
                     net_amount: 'net amount (HT)', vat_amount: 'VAT amount', gross_amount: 'gross amount (TTC)' };
  for (const [k, label] of Object.entries(required)) if (inv[k] === null) reasons.push(`Missing ${label}`);

  // 2. AI confidence score >= threshold
  if (inv.confidence < cfg.min_confidence)
    reasons.push(`Low AI confidence (${inv.confidence} < ${cfg.min_confidence})` +
                 (inv.low_confidence_fields.length ? ` on: ${inv.low_confidence_fields.join(', ')}` : ''));

  // 3. Net + VAT = Gross (tolerance)
  if (inv.net_amount !== null && inv.vat_amount !== null && inv.gross_amount !== null) {
    const diff = Math.abs(inv.net_amount + inv.vat_amount - inv.gross_amount);
    if (diff > cfg.amount_tolerance)
      reasons.push(`Totals do not match: ${inv.net_amount} + ${inv.vat_amount} = ${(inv.net_amount + inv.vat_amount).toFixed(3)}, but invoice says ${inv.gross_amount}`);
  }

  // 4. Supplier exists in master data (tax ID first, then exact name)
  supplier = suppliers.find(s => inv.supplier_tax_id && norm(s.tax_id) === norm(inv.supplier_tax_id))
          || suppliers.find(s => inv.supplier_name && lc(s.name) === lc(inv.supplier_name)) || null;
  if (!supplier) reasons.push(`Unknown supplier: ${inv.supplier_name || '?'} (${inv.supplier_tax_id || 'no tax ID'})`);
  else if (inv.supplier_tax_id && norm(supplier.tax_id) !== norm(inv.supplier_tax_id))
    reasons.push(`Supplier tax ID ${inv.supplier_tax_id} does not match master data (${supplier.tax_id})`);

  // 5. Duplicate = same supplier + same invoice number already recorded
  const sameSupplier = (r) => supplier ? norm(r.supplier_tax_id) === norm(supplier.tax_id) : lc(r.supplier_name) === lc(inv.supplier_name);
  if (inv.invoice_number && recorded.some(r => norm(r.invoice_number) === norm(inv.invoice_number) && sameSupplier(r)))
    reasons.push(`Duplicate: invoice ${inv.invoice_number} is already recorded`);

  // 6. Purchase order: exists, belongs to the supplier, open, not exceeded (cumulative over recorded invoices)
  if (!inv.po_reference) {
    if (cfg.require_po) reasons.push('Missing purchase-order reference');
  } else {
    po = pos.find(x => norm(x.po_number) === norm(inv.po_reference)) || null;
    if (!po) reasons.push(`Unknown purchase order ${inv.po_reference}`);
    else {
      if (supplier && norm(po.supplier_tax_id) !== norm(supplier.tax_id)) reasons.push(`PO ${po.po_number} belongs to another supplier`);
      if (lc(po.status) !== 'open') reasons.push(`PO ${po.po_number} is ${po.status}`);
      const limit = Number(po.amount_gross);
      const used = recorded.filter(r => norm(r.po_reference) === norm(po.po_number)).reduce((a, r) => a + (Number(r.gross_amount) || 0), 0);
      if (inv.gross_amount !== null && used + inv.gross_amount > limit + cfg.amount_tolerance)
        reasons.push(used > 0 ? `PO ${po.po_number} exceeded: ${used.toFixed(3)} already invoiced + ${inv.gross_amount} > ${limit}`
                              : `Amount ${inv.gross_amount} exceeds PO ${po.po_number} (${limit})`);
    }
  }

  // Not blocking, but logged in the audit trail
  if (supplier && inv.iban && supplier.iban && norm(inv.iban) !== norm(supplier.iban))
    warnings.push(`IBAN on invoice (${inv.iban}) differs from master data (${supplier.iban})`);
}

const valid = reasons.length === 0;
return [{ json: {
  valid,
  approval_required: valid && inv.gross_amount > cfg.approval_threshold,
  reasons, reasons_text: reasons.join(' | '), warnings,
  invoice: inv, supplier, purchase_order: po,
  supplier_email: p.supplier_email_intake || supplier?.email || inv.supplier_email || null,
}}];
"""

# Shared helpers for the nodes that build Sheets rows.
ROW_HELPERS = r"""
const cfg = $('Config').first().json;
const ts  = () => $now.setZone(cfg.timezone).toFormat('yyyy-MM-dd HH:mm:ss');
// USER_ENTERED is used so dates/timestamps become real dates for the KPI formulas.
// txt(): never let extracted text become a formula; id(): force text (keeps leading zeros, no date parsing).
const txt = (v) => { if (v === null || v === undefined) return ''; const s = String(v); return /^[=+\-@]/.test(s) ? "'" + s : s; };
const id  = (v) => (v === null || v === undefined || v === '') ? '' : "'" + String(v);
const num = (v) => (typeof v === 'number' && Number.isFinite(v)) ? v : '';

function invoiceRow({ status, route, reasons = '', approved_by = '', payment_status = '', archive_link = '' }) {
  const p = $('Prepare invoice').first().json;
  const r = $('Business rules check').first().json;
  const inv = r.invoice, sup = r.supplier;
  return {
    invoice_id: id(p.invoice_id), invoice_number: id(inv.invoice_number),
    invoice_date: txt(inv.invoice_date), due_date: txt(inv.due_date),
    supplier_name: txt(sup?.name || inv.supplier_name), supplier_tax_id: id(sup?.tax_id || inv.supplier_tax_id),
    supplier_email: txt(r.supplier_email), po_reference: id(inv.po_reference), iban: id(inv.iban || sup?.iban),
    currency: txt(inv.currency), net_amount: num(inv.net_amount), vat_amount: num(inv.vat_amount),
    gross_amount: num(inv.gross_amount), ai_confidence: num(inv.confidence),
    status, route, rejection_reasons: txt(reasons), approved_by: txt(approved_by), payment_status,
    file_name: txt(p.file_name), archive_link: txt(archive_link),
    received_at: p.received_at, processed_at: ts(),
  };
}

function auditRows(events) {
  const p = $('Prepare invoice').first().json;
  return events.map(([event, details]) => ({ json: { timestamp: ts(), invoice_id: id(p.invoice_id), event, details: txt(details) } }));
}

function intakeEvents() {
  const p = $('Prepare invoice').first().json;
  const v = $('Validate extraction').first().json;
  const r = $('Business rules check').first().json;
  return [
    ['RECEIVED', `source=${p.source}; file=${p.file_name}; from=${p.supplier_email_intake || '-'}` + (p.gmail_message_id ? `; gmail_id=${p.gmail_message_id}` : '')],
    ['TEXT_EXTRACTED', v.ocr_ok ? `method=${v.ocr_method}; chars=${v.text_chars}` + (v.ocr_confidence !== null ? `; ocr_confidence=${v.ocr_confidence}` : '') : `FAILED: ${v.ocr_error}`],
    ['AI_EXTRACTED', v.model_used ? `model=${v.model_used}; confidence=${r.invoice.confidence}` + (v.llm_seconds ? `; ${v.llm_seconds}s` : '') : 'skipped (no text)'],
    ['RULES_CHECKED', (r.valid ? `valid; approval_required=${r.approval_required}` : `invalid: ${r.reasons_text}`) + (r.warnings.length ? ` | warnings: ${r.warnings.join(' / ')}` : '')],
  ];
}
"""

BUILD_REJECTED = ROW_HELPERS + r"""
const r = ($('Re-check after approval').isExecuted ? $('Re-check after approval').first().json : $('Business rules check').first().json);
return [{ json: invoiceRow({ status: 'rejected_invalid', route: 'supplier_correction', reasons: r.reasons_text, payment_status: 'not_payable' }) }];
"""

AUDIT_REJECTED = ROW_HELPERS + r"""
const r = $('Business rules check').first().json;
const recheck = $('Re-check after approval').isExecuted;
const events = recheck   // intake events were already logged with APPROVAL_REQUESTED
  ? [['APPROVED_BY_MANAGER', `by ${cfg.manager_email}`], ['RECHECK_FAILED', $('Re-check after approval').first().json.reasons_text]]
  : intakeEvents();
return auditRows([...events, ['SENT_BACK_TO_SUPPLIER', `correction email to ${r.supplier_email || '(no email)'}`]]);
"""

AUDIT_APPROVAL = ROW_HELPERS + r"""
const r = $('Business rules check').first().json;
return auditRows([...intakeEvents(),
  ['APPROVAL_REQUESTED', `gross ${r.invoice.gross_amount} ${r.invoice.currency} > ${cfg.approval_threshold}; email to ${cfg.manager_email}`]]);
"""

BUILD_MGR_REJECTED = ROW_HELPERS + r"""
return [{ json: invoiceRow({ status: 'rejected_manager', route: 'manager_rejected',
  reasons: `Rejected by approver (${cfg.manager_email})`, payment_status: 'not_payable' }) }];
"""

AUDIT_MGR_REJECTED = ROW_HELPERS + r"""
return auditRows([['REJECTED_BY_MANAGER', `by ${cfg.manager_email}`]]);
"""

RECHECK = r"""
// Re-check after approval: while this invoice was waiting for the manager, another copy of it (or another invoice
// on the same PO) may have been recorded. Duplicate and PO-consumption checks are run again before the ERP write.
const cfg = $('Config').first().json;
const r = $('Business rules check').first().json;
const inv = r.invoice, sup = r.supplier, po = r.purchase_order;
const recorded = $input.all().map(i => i.json).filter(j => j && Object.keys(j).length > 0);
const norm = (s) => String(s ?? '').toUpperCase().replace(/[^A-Z0-9]/g, '');
const reasons = [];
if (inv.invoice_number && recorded.some(x => norm(x.invoice_number) === norm(inv.invoice_number) && norm(x.supplier_tax_id) === norm(sup.tax_id)))
  reasons.push(`Duplicate: invoice ${inv.invoice_number} was recorded while this copy was waiting for approval`);
if (po) {
  const used = recorded.filter(x => norm(x.po_reference) === norm(po.po_number)).reduce((a, x) => a + (Number(x.gross_amount) || 0), 0);
  if (used + inv.gross_amount > Number(po.amount_gross) + cfg.amount_tolerance)
    reasons.push(`PO ${po.po_number} exceeded while waiting for approval: ${used.toFixed(3)} already invoiced + ${inv.gross_amount} > ${po.amount_gross}`);
}
return [{ json: { valid: reasons.length === 0, reasons, reasons_text: reasons.join(' | ') } }];
"""

BUILD_ERP = r"""
// ERP integration step 1/2: decide the route and prepare a unique archive name (never overwrite).
const cfg = $('Config').first().json;
const p = $('Prepare invoice').first().json;
const r = $('Business rules check').first().json;
const approved = $('Approved?').isExecuted;          // only reached through "Approved? = true"
const safe = (s) => String(s || 'unknown').normalize('NFKD').replace(/[^A-Za-z0-9._-]+/g, '_').replace(/_+/g, '_').slice(0, 40);
return [{ json: {
  route: approved ? 'approved' : 'auto',
  approved_by: approved ? cfg.manager_email : '',
  archive_name: `${p.received_at.slice(0, 10)}_${safe(r.supplier?.name)}_${safe(r.invoice.invoice_number)}_${p.invoice_id}.${p.file_extension}`,
}, binary: { invoice: $('Prepare invoice').first().binary.invoice } }];
"""

BUILD_ERP_ROW = ROW_HELPERS + r"""
// ERP integration step 2/2: the record for the mock ERP (= the Invoices tab).
// To plug a real ERP, replace the next node ("ERP integration (mock)") by an HTTP Request to the ERP API.
const erp  = $('Build ERP record').first().json;
const file = $input.first().json;                    // Google Drive upload response
const link = file.webViewLink || (file.id ? `https://drive.google.com/file/d/${file.id}/view` : '');
// Payment is SIMULATED: the invoice is only flagged "scheduled" - nothing is ever paid.
return [{ json: invoiceRow({ status: 'recorded', route: erp.route, approved_by: erp.approved_by,
                             payment_status: 'scheduled', archive_link: link }) }];
"""

AUDIT_RECORDED = ROW_HELPERS + r"""
const erp = $('Build ERP record').first().json;
const row = $('Build ERP row').first().json;
const events = $('AuditLog: approval requested').isExecuted ? [] : intakeEvents();
if (erp.route === 'approved') events.push(['APPROVED_BY_MANAGER', `by ${erp.approved_by}`]);
events.push(['ARCHIVED', row.archive_link]);
events.push(['RECORDED_IN_ERP', `route=${erp.route}; gross=${row.gross_amount} ${row.currency}`]);
events.push(['PAYMENT_SCHEDULED', `SIMULATED - no real payment; due ${row.due_date || '?'}`]);
return auditRows(events);
"""

RESULT_RECORDED = r"""
const row = $('Build ERP row').first().json;
const kpi = Object.fromEntries($input.all().map(i => [i.json.KPI, i.json.Value]).filter(([k]) => k));
return [{ json: { invoice_id: row.invoice_id.replace(/^'/, ''), status: row.status, route: row.route,
  gross_amount: row.gross_amount, archive_link: row.archive_link, payment_status: row.payment_status, kpi } }];
"""

RESULT_REJECTED = r"""
const r = $('Business rules check').first().json;
return [{ json: { invoice_id: $('Prepare invoice').first().json.invoice_id, status: 'rejected_invalid',
  route: 'supplier_correction', reasons: (($('Re-check after approval').isExecuted ? $('Re-check after approval').first().json : $('Business rules check').first().json)).reasons, email_sent_to: r.supplier_email } }];
"""

RESULT_MGR = r"""
return [{ json: { invoice_id: $('Prepare invoice').first().json.invoice_id, status: 'rejected_manager', route: 'manager_rejected' } }];
"""

R = "$('Business rules check').first().json"
REASONS = "($('Re-check after approval').isExecuted ? $('Re-check after approval').first().json : $('Business rules check').first().json)"
EMAIL_SUPPLIER = (
    "=Bonjour,\n\n"
    "Nous n'avons pas pu traiter votre facture {{ " + R + ".invoice.invoice_number || '(sans numéro)' }} "
    "(fichier : {{ $('Prepare invoice').first().json.file_name }}) pour la ou les raisons suivantes :\n\n"
    "- {{ " + REASONS + ".reasons.join('\\n- ') }}\n\n"
    "Merci de nous renvoyer une facture corrigée.\n\n"
    "Référence de traitement : {{ $('Prepare invoice').first().json.invoice_id }}\n\n"
    "Service Comptabilité Fournisseurs (message automatique)")

EMAIL_APPROVAL = (
    "=Bonjour,\n\n"
    "La facture {{ " + R + ".invoice.invoice_number }} de {{ " + R + ".supplier.name }} dépasse le seuil "
    "d'approbation ({{ $('Config').first().json.approval_threshold }} TND).\n\n"
    "Montant : HT {{ " + R + ".invoice.net_amount }} + TVA {{ " + R + ".invoice.vat_amount }} = "
    "TTC {{ " + R + ".invoice.gross_amount }} {{ " + R + ".invoice.currency }}\n"
    "Bon de commande : {{ " + R + ".invoice.po_reference }}\n"
    "Date : {{ " + R + ".invoice.invoice_date }} - échéance {{ " + R + ".invoice.due_date }}\n"
    "Référence : {{ $('Prepare invoice').first().json.invoice_id }}\n\n"
    "Tous les contrôles automatiques sont OK. Approuvez-vous cette facture ?")


GMAIL_QUERY = E("GMAIL_QUERY", "has:attachment {label:INVOICE_AUTOMATION subject:facture subject:factures subject:invoice subject:invoices}")


def gmail_static_data():
    """GMAIL_BACKFILL_SINCE=YYYY-MM-DD in .env: on (re)activation the trigger also picks up matching UNREAD emails
    received since that day (otherwise it only sees emails arriving after activation)."""
    since = E("GMAIL_BACKFILL_SINCE")
    if not since:
        return None
    from datetime import datetime
    t = datetime.fromisoformat(since + "T00:00:00+01:00")   # Africa/Tunis = UTC+1, no daylight saving
    return {"node:Gmail Trigger": {"Gmail Trigger": {"lastTimeChecked": int(t.timestamp())}}}


def build_main():
    sid = "={{ $('Config').first().json.sheet_id }}"
    label_id = E("GMAIL_LABEL_ID", "LABEL_ID_NOT_SET")
    cfg = [
        ("ocr_url", E("OCR_URL", "http://localhost:8000/extract"), "string"),
        ("ollama_url", E("OLLAMA_URL", "http://localhost:11434"), "string"),
        ("ollama_model", E("OLLAMA_MODEL", "qwen3.5:9b"), "string"),
        ("ollama_fallback_model", E("OLLAMA_FALLBACK_MODEL", "qwen3:8b"), "string"),
        ("sheet_id", E("GOOGLE_SHEET_ID", "SHEET_ID_NOT_SET"), "string"),
        ("drive_folder_id", E("GOOGLE_DRIVE_FOLDER_ID", "FOLDER_ID_NOT_SET"), "string"),
        ("manager_email", E("MANAGER_EMAIL", "manager@example.com"), "string"),
        ("approval_threshold", float(E("APPROVAL_THRESHOLD", "2000")), "number"),
        ("min_confidence", float(E("MIN_CONFIDENCE", "0.8")), "number"),
        ("amount_tolerance", float(E("AMOUNT_TOLERANCE", "0.01")), "number"),
        ("require_po", True, "boolean"),
        ("company_tax_id", E("COMPANY_TAX_ID", "5555555/D/A/000"), "string"),
        ("default_currency", "TND", "string"),
        ("timezone", E("GENERIC_TIMEZONE", "Africa/Tunis"), "string"),
    ]
    nodes = [
        # ---------------------------------------------------------------- 1. intake (two triggers)
        node("Invoice upload form", "n8n-nodes-base.formTrigger", 2.2, {
            "formTitle": "Dépôt d'une facture fournisseur",
            "formDescription": "Déposez la facture reçue d'un fournisseur (PDF, PNG ou JPG). Elle est traitée automatiquement en ~30 s : lecture (OCR + IA locale), contrôles, approbation du manager si > 2000 TND, enregistrement et archivage.\n\nRésultat : onglet « Invoices » du Google Sheet « Invoice Automation DB ». Historique : onglet « AuditLog ». Détail nœud par nœud : n8n > Executions.",
            "formFields": {"values": [
                {"fieldLabel": "Facture (PDF, PNG ou JPG)", "fieldType": "file", "multipleFiles": False,
                 "acceptFileTypes": ".pdf, .png, .jpg, .jpeg", "requiredField": True},
                {"fieldLabel": "Email du fournisseur (facultatif)", "fieldType": "email",
                 "placeholder": "adresse à qui renvoyer la facture si elle doit être corrigée", "requiredField": False}]},
            "options": {"path": "invoice-upload", "buttonLabel": "Envoyer la facture",
                        "respondWithOptions": {"values": {"respondWith": "text",
                                                          "formSubmittedText": "Facture reçue. Elle est traitée automatiquement (~30 s). Voir le résultat dans l'onglet Invoices du Google Sheet."}}}},
             [0, -520], webhookId="6d0f4f2e-3b1a-4f7e-9a51-1c2b3d4e5f60"),
        code("Normalize: form upload", NORMALIZE_FORM, [260, -520]),
        node("Gmail Trigger", "n8n-nodes-base.gmailTrigger", 1.2, {
            "pollTimes": {"item": [{"mode": "everyMinute"}]}, "simple": False,
            "filters": {"q": GMAIL_QUERY, "readStatus": "unread"},
            "options": {"downloadAttachments": True, "dataPropertyAttachmentsPrefixName": "attachment_"}},
             [0, -300], credentials=CRED_GMAIL),
        code("Normalize: Gmail attachments", NORMALIZE_GMAIL, [260, -340]),
        node("Process each invoice", "n8n-nodes-base.executeWorkflow", 1.2, {
            "workflowId": {"__rl": True, "value": WF_MAIN, "mode": "id"}, "mode": "each",
            "options": {"waitForSubWorkflow": False},
            "workflowInputs": {"mappingMode": "defineBelow", "value": {}, "matchingColumns": [], "schema": [],
                               "attemptToConvertTypes": False, "convertFieldsToString": True}}, [540, -440]),
        node("Gmail: mark as read", "n8n-nodes-base.gmail", 2.1, {"operation": "markAsRead", "messageId": "={{ $json.id }}"},
             [260, -160], credentials=CRED_GMAIL),
        node("Gmail: remove label", "n8n-nodes-base.gmail", 2.1, {
            "operation": "removeLabels", "messageId": "={{ $('Gmail Trigger').item.json.id }}", "labelIds": [label_id]},
             [540, -160], credentials=CRED_GMAIL),

        # ---------------------------------------------------------------- 2. IDP / OCR + AI
        node("When called by intake", "n8n-nodes-base.executeWorkflowTrigger", 1.1, {"inputSource": "passthrough"}, [0, 300]),
        node("Config", "n8n-nodes-base.set", 3.4, {"assignments": {"assignments": [
            {"id": nid("cfg/" + k), "name": k, "value": v, "type": t} for k, v, t in cfg]}, "options": {}}, [220, 300]),
        code("Prepare invoice", PREPARE_INVOICE, [440, 300]),
        node("OCR: extract text", "n8n-nodes-base.httpRequest", 4.2, {
            "method": "POST", "url": "={{ $('Config').first().json.ocr_url }}", "sendBody": True,
            "contentType": "multipart-form-data",
            "bodyParameters": {"parameters": [{"parameterType": "formBinaryData", "name": "file", "inputDataFieldName": "invoice"}]},
            "options": {"timeout": 180000, "response": {"response": {"neverError": True}}}},
             [660, 300], retryOnFail=True, maxTries=2, waitBetweenTries=3000, onError="continueRegularOutput"),
        code("Prepare AI request", PREPARE_AI, [880, 300]),
        if_true("Text extracted?", "={{ $json.ocr_ok }}", [1100, 300]),
        http_json("Ollama: AI extraction", "={{ $json.ollama_url }}", "={{ JSON.stringify($json.body_main) }}", [1320, 180],
                  onError="continueRegularOutput"),
        code("Check AI answer", CHECK_AI, [1540, 180]),
        if_true("AI answer usable?", "={{ $json.ai_ok }}", [1760, 180]),
        http_json("Ollama: fallback model", "={{ $('Prepare AI request').first().json.ollama_url }}",
                  "={{ JSON.stringify($('Prepare AI request').first().json.body_fallback) }}", [1980, 60],
                  onError="continueRegularOutput"),
        code("Validate extraction", VALIDATE, [2200, 300]),

        # ---------------------------------------------------------------- 3. AI analysis & check (deterministic)
        sheet_read("Get suppliers", sid, "Suppliers", [2420, 300]),
        sheet_read("Get purchase orders", sid, "PurchaseOrders", [2640, 300]),
        sheet_read("Get recorded invoices", sid, "Invoices", [2860, 300], filters=[("status", "recorded")]),
        code("Business rules check", RULES, [3080, 300]),

        # ---------------------------------------------------------------- 4. Valid data? -> supplier correction
        if_true("Valid data?", "={{ $json.valid }}", [3300, 300]),
        code("Build rejected record", BUILD_REJECTED, [3520, 640]),
        sheet_append("Invoices: append (rejected)", sid, "Invoices", [3740, 640]),
        code("Audit rows (rejected)", AUDIT_REJECTED, [3960, 640], executeOnce=True),
        sheet_append("AuditLog: append (rejected)", sid, "AuditLog", [4180, 640]),
        node("Email supplier: correction needed", "n8n-nodes-base.gmail", 2.1, {
            "sendTo": "={{ " + R + ".supplier_email }}",
            "subject": "=Facture {{ " + R + ".invoice.invoice_number || '(sans numéro)' }} : correction nécessaire",
            "emailType": "text", "message": EMAIL_SUPPLIER, "options": {"appendAttribution": False}},
             [4400, 640], credentials=CRED_GMAIL, webhookId="0b7c2a8e-5d4f-4e6a-8b1c-2d3e4f5a6b70", executeOnce=True),
        code("Result: sent back to supplier", RESULT_REJECTED, [4620, 640]),

        # ---------------------------------------------------------------- 5. Approval required? -> manager
        if_true("Approval required?", "={{ $json.approval_required }}", [3520, 200]),
        code("Audit rows (approval requested)", AUDIT_APPROVAL, [3740, 20]),
        sheet_append("AuditLog: approval requested", sid, "AuditLog", [3960, 20]),
        node("Approval workflow (manager)", "n8n-nodes-base.gmail", 2.1, {
            "operation": "sendAndWait", "sendTo": "={{ $('Config').first().json.manager_email }}",
            "subject": "=Approbation requise : facture {{ " + R + ".invoice.invoice_number }} ({{ " + R + ".invoice.gross_amount }} {{ " + R + ".invoice.currency }})",
            "message": EMAIL_APPROVAL,
            "approvalOptions": {"values": {"approvalType": "double", "approveLabel": "Approve", "disapproveLabel": "Reject"}},
            "options": {"appendAttribution": False}},
             [4180, 20], credentials=CRED_GMAIL, webhookId="3f2e1d0c-9b8a-4c7d-8e6f-5a4b3c2d1e0f", executeOnce=True),
        if_true("Approved?", "={{ $json.data.approved }}", [4400, 20]),
        sheet_read("Re-check: get recorded invoices", sid, "Invoices", [4620, 120], filters=[("status", "recorded")]),
        code("Re-check after approval", RECHECK, [4840, 120]),
        if_true("Still valid?", "={{ $json.valid }}", [5060, 120]),
        code("Build manager-rejected record", BUILD_MGR_REJECTED, [4620, -160]),
        sheet_append("Invoices: append (rejected by manager)", sid, "Invoices", [4840, -160]),
        code("Audit rows (rejected by manager)", AUDIT_MGR_REJECTED, [5060, -160], executeOnce=True),
        sheet_append("AuditLog: append (rejected by manager)", sid, "AuditLog", [5280, -160]),
        code("Result: rejected by manager", RESULT_MGR, [5500, -160], executeOnce=True),

        # ---------------------------------------------------------------- 6-8. ERP, archive, payment & reporting
        code("Build ERP record", BUILD_ERP, [5280, 300]),
        node("Archiving: upload to Drive", "n8n-nodes-base.googleDrive", 3, {
            "name": "={{ $json.archive_name }}", "driveId": {"__rl": True, "mode": "list", "value": "My Drive"},
            "folderId": {"__rl": True, "mode": "id", "value": "={{ $('Config').first().json.drive_folder_id }}"},
            "inputDataFieldName": "invoice", "options": {}}, [5500, 300], credentials=CRED_DRIVE),
        code("Build ERP row", BUILD_ERP_ROW, [5720, 300]),
        sheet_append("ERP integration (mock): append to Invoices", sid, "Invoices", [5940, 300]),
        code("Audit rows (recorded)", AUDIT_RECORDED, [6160, 300], executeOnce=True),
        sheet_append("AuditLog: append (recorded)", sid, "AuditLog", [6380, 300]),
        sheet_read("Payment & reporting: read KPI", sid, "KPI", [6600, 300]),
        code("Result: recorded", RESULT_RECORDED, [6820, 300]),

        # ---------------------------------------------------------------- notes
        sticky("Note intake", "## 1. Invoice receipt\nTwo triggers feed the same pipeline:\n- **n8n form** (PDF / PNG / JPG)\n- **Gmail**: unread emails with a PDF/image attachment and the label `INVOICE_AUTOMATION` **or** « facture » / « invoice » in the subject. The email is marked as read and the label removed right after the hand-off, so it is never processed twice.\n\nEach invoice is processed in its own execution (*Process each invoice* calls this same workflow).",
               [-60, -800], 860, 240, 7),
        sticky("Note IDP", "## 2. IDP / OCR + AI (100 % local)\n- Local OCR service (FastAPI, port 8000): text PDF -> direct text, scan / image -> Tesseract fra+eng\n- Ollama `qwen3.5:9b` (JSON schema, temperature 0, think off); `qwen3:8b` if the answer is unusable\n- **Validate extraction**: JSON checks, normalisation, anti-hallucination (IDs must appear in the text)",
               [-60, -40], 2380, 180, 6),
        sticky("Note rules", "## 3. AI analysis & check (deterministic code)\nMandatory fields, confidence >= 0.8, HT + TVA = TTC (+/-0.01), supplier exists, PO exists / same supplier / open / not exceeded, no duplicate. Lookups = Google Sheets *Get row(s)*.",
               [2380, -40], 900, 180, 4),
        sticky("Note outcomes", "## 4-8. Outcomes\n- **Invalid** -> `rejected_invalid` + AuditLog + correction email to the supplier\n- **> 2000 TND** -> manager approval by email (Approve / Reject)\n- **ERP (mock)** = append to *Invoices* (`recorded`, route `auto` / `approved`), original archived in Drive *Invoice Archive*, payment **simulated** (`scheduled`), KPI tab = formulas",
               [3480, -420], 1000, 200, 3),
    ]
    links = [
        ("Invoice upload form", "Normalize: form upload"), ("Normalize: form upload", "Process each invoice"),
        ("Gmail Trigger", "Normalize: Gmail attachments"), ("Normalize: Gmail attachments", "Process each invoice"),
        ("Gmail Trigger", "Gmail: mark as read"), ("Gmail: mark as read", "Gmail: remove label"),

        ("When called by intake", "Config"), ("Config", "Prepare invoice"), ("Prepare invoice", "OCR: extract text"),
        ("OCR: extract text", "Prepare AI request"), ("Prepare AI request", "Text extracted?"),
        ("Text extracted?", "Ollama: AI extraction", 0), ("Text extracted?", "Validate extraction", 1),
        ("Ollama: AI extraction", "Check AI answer"), ("Check AI answer", "AI answer usable?"),
        ("AI answer usable?", "Validate extraction", 0), ("AI answer usable?", "Ollama: fallback model", 1),
        ("Ollama: fallback model", "Validate extraction"),
        ("Validate extraction", "Get suppliers"), ("Get suppliers", "Get purchase orders"),
        ("Get purchase orders", "Get recorded invoices"), ("Get recorded invoices", "Business rules check"),
        ("Business rules check", "Valid data?"),

        ("Valid data?", "Approval required?", 0), ("Valid data?", "Build rejected record", 1),
        ("Build rejected record", "Invoices: append (rejected)"), ("Invoices: append (rejected)", "Audit rows (rejected)"),
        ("Audit rows (rejected)", "AuditLog: append (rejected)"), ("AuditLog: append (rejected)", "Email supplier: correction needed"),
        ("Email supplier: correction needed", "Result: sent back to supplier"),

        ("Approval required?", "Audit rows (approval requested)", 0), ("Approval required?", "Build ERP record", 1),
        ("Audit rows (approval requested)", "AuditLog: approval requested"),
        ("AuditLog: approval requested", "Approval workflow (manager)"), ("Approval workflow (manager)", "Approved?"),
        ("Approved?", "Re-check: get recorded invoices", 0),
        ("Re-check: get recorded invoices", "Re-check after approval"), ("Re-check after approval", "Still valid?"),
        ("Still valid?", "Build ERP record", 0), ("Still valid?", "Build rejected record", 1), ("Approved?", "Build manager-rejected record", 1),
        ("Build manager-rejected record", "Invoices: append (rejected by manager)"),
        ("Invoices: append (rejected by manager)", "Audit rows (rejected by manager)"),
        ("Audit rows (rejected by manager)", "AuditLog: append (rejected by manager)"),
        ("AuditLog: append (rejected by manager)", "Result: rejected by manager"),

        ("Build ERP record", "Archiving: upload to Drive"), ("Archiving: upload to Drive", "Build ERP row"),
        ("Build ERP row", "ERP integration (mock): append to Invoices"),
        ("ERP integration (mock): append to Invoices", "Audit rows (recorded)"),
        ("Audit rows (recorded)", "AuditLog: append (recorded)"),
        ("AuditLog: append (recorded)", "Payment & reporting: read KPI"),
        ("Payment & reporting: read KPI", "Result: recorded"),
    ]
    wf = workflow(WF_MAIN, "01_invoice_automation", nodes, links)
    if gmail_static_data():
        wf["staticData"] = gmail_static_data()
    return wf


# ============================================================================ 99 read helper
READ_SUMMARY = r"""
const rows = (n) => $(n).all().map(i => i.json).filter(j => Object.keys(j).length);
return [{ json: { invoices: rows('Read Invoices'), audit: rows('Read AuditLog'), kpi: rows('Read KPI') } }];
"""


def build_read():
    sid = E("GOOGLE_SHEET_ID", "SHEET_ID_NOT_SET")
    nodes = [node("Run", "n8n-nodes-base.manualTrigger", 1, {}, [0, 0]),
             sheet_read("Read Invoices", sid, "Invoices", [220, 0]),
             sheet_read("Read AuditLog", sid, "AuditLog", [440, 0]),
             sheet_read("Read KPI", sid, "KPI", [660, 0]),
             code("Summary", READ_SUMMARY, [880, 0], executeOnce=True)]
    order = ["Run", "Read Invoices", "Read AuditLog", "Read KPI", "Summary"]
    return workflow(WF_READ, "99_read_sheet", nodes, list(zip(order, order[1:])))


GUIDE = [
    ("#", "INVOICE AUTOMATION DB — GUIDE", ""),
    ("", "Ce classeur est la base de données du prototype : n8n y lit les référentiels (fournisseurs, bons de commande) et y écrit chaque facture traitée. Ne renommez pas les onglets ni les colonnes.", ""),
    ("", "", ""),
    ("#", "ONGLET", "À QUOI IL SERT"),
    ("Suppliers", "Référentiel des FOURNISSEURS : les entreprises qui vous vendent des biens ou services et vous envoient des factures. Une facture d'un fournisseur absent de cette liste est rejetée.", "rempli par vous (3 fournisseurs de démo)"),
    ("PurchaseOrders", "BONS DE COMMANDE (BC) que votre entreprise a passés à un fournisseur. Une facture doit citer un BC existant, du même fournisseur, ouvert, et ne pas dépasser son montant.", "rempli par vous (3 BC de démo)"),
    ("Invoices", "REGISTRE DES FACTURES = l'ERP simulé : une ligne par facture reçue, quel que soit le résultat (enregistrée, rejetée…).", "écrit par n8n"),
    ("AuditLog", "JOURNAL : chaque action du robot (réception, lecture, contrôle, email, archivage…) avec l'heure. Permet de suivre ce qui s'est passé pour une facture.", "écrit par n8n"),
    ("KPI", "TABLEAU DE BORD calculé par formules à partir de Invoices (se met à jour tout seul) + graphique des factures par statut.", "formules"),
    ("", "", ""),
    ("#", "COLONNES — Suppliers", "EXEMPLE"),
    ("supplier_id", "numéro interne du fournisseur", "1"),
    ("name", "raison sociale du fournisseur", "Tech Supplies SARL"),
    ("tax_id", "matricule fiscal (identifiant fiscal tunisien de l'entreprise)", "1234567/A/M/000"),
    ("email", "email de facturation du fournisseur", "billing@techsupplies.example"),
    ("iban", "compte bancaire du fournisseur (pour le payer)", "TN59 1000 6035 1835 9847 8831"),
    ("", "", ""),
    ("#", "COLONNES — PurchaseOrders", "EXEMPLE"),
    ("po_number", "numéro du bon de commande", "PO-2026-0101"),
    ("supplier_tax_id", "matricule fiscal du fournisseur à qui le BC a été passé", "1234567/A/M/000"),
    ("amount_gross", "montant maximum TTC autorisé (cumul de toutes ses factures)", "1500"),
    ("status", "open = utilisable ; closed = plus aucune facture acceptée", "open"),
    ("", "", ""),
    ("#", "COLONNES — Invoices", "EXEMPLE"),
    ("invoice_id", "référence unique donnée par le robot à chaque traitement (la même dans AuditLog)", "INV-20261001-112055-XV2H"),
    ("invoice_number", "numéro de facture écrit par le fournisseur", "INV-OP-2026-140"),
    ("invoice_date", "date de la facture", "2026-09-30"),
    ("due_date", "date d'échéance (date limite de paiement)", "2026-10-30"),
    ("supplier_name", "fournisseur (nom du référentiel s'il est reconnu)", "Office Pro SA"),
    ("supplier_tax_id", "matricule fiscal du fournisseur (vendeur, jamais celui du client)", "7654321/B/A/000"),
    ("supplier_email", "adresse à qui on écrit en cas de correction : email du formulaire, sinon expéditeur Gmail, sinon référentiel", "accounts@officepro.example"),
    ("po_reference", "bon de commande cité sur la facture", "PO-2026-0102"),
    ("iban", "IBAN lu sur la facture", "TN59 0800 …"),
    ("currency", "devise", "TND"),
    ("net_amount", "total HT (hors taxes)", "100"),
    ("vat_amount", "TVA", "19"),
    ("gross_amount", "total TTC (toutes taxes comprises) = HT + TVA", "119"),
    ("ai_confidence", "confiance de l'IA dans sa lecture (0 à 1) ; moins de 0,8 = rejet", "0.95"),
    ("status", "RÉSULTAT : recorded = enregistrée ; rejected_invalid = renvoyée au fournisseur ; rejected_manager = refusée par le manager", "recorded"),
    ("route", "CHEMIN : auto = sans intervention humaine ; approved = approuvée par le manager (> 2000 TND) ; supplier_correction ; manager_rejected", "auto"),
    ("rejection_reasons", "motifs du rejet (envoyés au fournisseur)", "Totals do not match …"),
    ("approved_by", "manager qui a approuvé", "kharratok@gmail.com"),
    ("payment_status", "scheduled = paiement programmé (SIMULÉ : aucun paiement réel) ; not_payable = facture rejetée", "scheduled"),
    ("file_name", "nom du fichier reçu (formulaire ou pièce jointe Gmail)", "facture_10_gmail.pdf"),
    ("archive_link", "lien vers l'original archivé dans Google Drive « Invoice Archive » (factures enregistrées)", "https://drive.google.com/…"),
    ("received_at", "heure de réception", "2026-10-01 11:20:55"),
    ("processed_at", "heure de fin de traitement", "2026-10-01 11:21:24"),
    ("", "", ""),
    ("#", "COLONNES — AuditLog", "EXEMPLE"),
    ("timestamp", "heure de l'action", ""),
    ("invoice_id", "facture concernée (même référence que dans Invoices)", ""),
    ("event", "RECEIVED = reçue · TEXT_EXTRACTED = texte lu (PDF ou OCR) · AI_EXTRACTED = champs lus par l'IA · RULES_CHECKED = contrôles · SENT_BACK_TO_SUPPLIER = email de correction · APPROVAL_REQUESTED · APPROVED_BY_MANAGER · REJECTED_BY_MANAGER · RECHECK_FAILED · ARCHIVED · RECORDED_IN_ERP · PAYMENT_SCHEDULED (simulé)", ""),
    ("details", "détails de l'action (méthode, modèle IA, motifs, lien…)", ""),
    ("", "", ""),
    ("#", "RÈGLES DE CONTRÔLE (dans l'ordre)", ""),
    ("1", "tous les champs obligatoires sont lus (n°, date, fournisseur, HT, TVA, TTC)", ""),
    ("2", "confiance de l'IA ≥ 0,8", ""),
    ("3", "HT + TVA = TTC (à 0,01 près)", ""),
    ("4", "le fournisseur existe dans Suppliers", ""),
    ("5", "le BC existe, appartient au même fournisseur, est ouvert et n'est pas dépassé", ""),
    ("6", "pas de doublon (même fournisseur + même n° déjà enregistré)", ""),
    ("→", "Un contrôle KO : rejected_invalid + email au fournisseur. Tout OK : ≤ 2000 TND enregistrée automatiquement ; > 2000 TND email d'approbation au manager (Approve / Reject).", ""),
]


def build_guide():
    """Adds (or refreshes) a "Guide" tab in the spreadsheet explaining every tab and column, in French."""
    sid, gid = E("GOOGLE_SHEET_ID", "SHEET_ID_NOT_SET"), 777
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{sid}"
    values = {"values": [[a if a != "#" else "", b, c] for a, b, c in GUIDE]}
    titles = [i for i, (a, _, _) in enumerate(GUIDE) if a == "#"]
    fmt = {"requests": [
        {"updateDimensionProperties": {"range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                                       "properties": {"pixelSize": w}, "fields": "pixelSize"}} for i, w in [(0, 170), (1, 760), (2, 280)]
    ] + [
        {"repeatCell": {"range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": len(GUIDE), "startColumnIndex": 0, "endColumnIndex": 3},
                        "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                        "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
        {"repeatCell": {"range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": len(GUIDE), "startColumnIndex": 0, "endColumnIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}}, "fields": "userEnteredFormat.textFormat.bold"}},
    ] + [
        {"repeatCell": {"range": {"sheetId": gid, "startRowIndex": r, "endRowIndex": r + 1, "startColumnIndex": 0, "endColumnIndex": 3},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.85, "green": 0.9, "blue": 0.97}}},
                        "fields": "userEnteredFormat(textFormat,backgroundColor)"}} for r in titles
    ]}
    nodes = [
        node("Run", "n8n-nodes-base.manualTrigger", 1, {}, [0, 0]),
        http_json("Add Guide tab", f"{base}:batchUpdate",
                  json.dumps({"requests": [{"addSheet": {"properties": {"sheetId": gid, "title": "Guide", "index": 0}}}]}),
                  [220, 0], CRED_SHEETS, onError="continueRegularOutput"),   # fails harmlessly if the tab exists
        http_json("Clear Guide", f"{base}/values:batchClear", '{"ranges": ["Guide!A1:C200"]}', [440, 0], CRED_SHEETS),
        node("Write Guide text", "n8n-nodes-base.httpRequest", 4.2, {
            "method": "PUT", "url": f"{base}/values/Guide!A1:C{len(GUIDE)}?valueInputOption=RAW",
            "authentication": "predefinedCredentialType", "nodeCredentialType": "googleSheetsOAuth2Api",
            "sendBody": True, "specifyBody": "json", "jsonBody": json.dumps(values, ensure_ascii=False), "options": {}},
             [660, 0], credentials=CRED_SHEETS),
        http_json("Format Guide", f"{base}:batchUpdate", json.dumps(fmt), [880, 0], CRED_SHEETS),
    ]
    order = ["Run", "Add Guide tab", "Clear Guide", "Write Guide text", "Format Guide"]
    return workflow("invSheetGuide001", "97_sheet_guide", nodes, list(zip(order, order[1:])))


def build_reset():
    """Clears the data rows of Invoices and AuditLog (headers, master data and KPI formulas are kept)."""
    sid = E("GOOGLE_SHEET_ID", "SHEET_ID_NOT_SET")
    nodes = [node("Run", "n8n-nodes-base.manualTrigger", 1, {}, [0, 0]),
             http_json("Clear Invoices & AuditLog rows", f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values:batchClear",
                       '{"ranges": ["Invoices!A2:W", "AuditLog!A2:D"]}', [220, 0], CRED_SHEETS),
             sticky("Note reset", "## 98 - Reset test data\nEmpties the **Invoices** and **AuditLog** tabs (row 2 and below) to restart a demo. Suppliers, purchase orders and KPI formulas are kept. Drive archive files are NOT deleted.",
                    [0, -220], 460, 180, 3)]
    return workflow("invResetTests001", "98_reset_test_data", nodes, [("Run", "Clear Invoices & AuditLog rows")])


if __name__ == "__main__":
    for fname, wf in [("00_setup_google_sheet.json", build_setup()), ("01_invoice_automation.json", build_main()),
                      ("97_sheet_guide.json", build_guide()), ("98_reset_test_data.json", build_reset()), ("99_read_sheet.json", build_read())]:
        (OUT / fname).write_text(json.dumps(wf, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"written workflows/{fname}: {len(wf['nodes'])} nodes")
