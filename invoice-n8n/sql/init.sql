-- =====================================================================
--  Invoice Automation (n8n) - PostgreSQL schema + demo data
--  Run once:  psql -U postgres -d invoices_db -f init.sql
-- =====================================================================

DROP VIEW  IF EXISTS kpi_summary;
DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS invoices;
DROP TABLE IF EXISTS purchase_orders;
DROP TABLE IF EXISTS suppliers;

-- Supplier master data (used to check "supplier exists")
CREATE TABLE suppliers (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    tax_id      TEXT UNIQUE NOT NULL,          -- matricule fiscal
    email       TEXT,
    iban        TEXT
);

-- Purchase orders (used to check "PO reference is valid")
CREATE TABLE purchase_orders (
    id               SERIAL PRIMARY KEY,
    po_number        TEXT UNIQUE NOT NULL,
    supplier_tax_id  TEXT NOT NULL REFERENCES suppliers(tax_id),
    amount_gross     NUMERIC(12,3) NOT NULL,   -- max amount allowed (TTC)
    status           TEXT NOT NULL DEFAULT 'open'
);

-- Invoices = the "ERP" table (every processed invoice, accepted or not)
CREATE TABLE invoices (
    id                 SERIAL PRIMARY KEY,
    invoice_number     TEXT,
    invoice_date       DATE,
    due_date           DATE,
    supplier_name      TEXT,
    supplier_tax_id    TEXT,
    supplier_email     TEXT,
    po_reference       TEXT,
    iban               TEXT,
    currency           TEXT,
    net_amount         NUMERIC(12,3),
    vat_amount         NUMERIC(12,3),
    gross_amount       NUMERIC(12,3),
    ai_confidence      NUMERIC(4,3),
    status             TEXT NOT NULL,          -- recorded | rejected_invalid | rejected_manager
    route              TEXT NOT NULL,          -- auto | approved | supplier_correction | manager_rejected
    rejection_reasons  TEXT,
    file_name          TEXT,
    archive_path       TEXT,
    received_at        TIMESTAMPTZ,
    processed_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Audit trail (who / what / when)
CREATE TABLE audit_log (
    id          SERIAL PRIMARY KEY,
    invoice_id  INT REFERENCES invoices(id),
    event       TEXT NOT NULL,
    details     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- KPI view used by the "Payment & reporting" step (and Metabase / Grafana)
CREATE VIEW kpi_summary AS
SELECT
    count(*)                                                        AS invoices_processed,
    count(*) FILTER (WHERE status = 'recorded')                     AS invoices_recorded,
    count(*) FILTER (WHERE route  = 'auto')                         AS touchless,
    count(*) FILTER (WHERE route  = 'approved')                     AS approved_by_manager,
    count(*) FILTER (WHERE status = 'rejected_invalid')             AS sent_back_to_supplier,
    count(*) FILTER (WHERE status = 'rejected_manager')             AS rejected_by_manager,
    round(100.0 * count(*) FILTER (WHERE route = 'auto')
          / NULLIF(count(*), 0), 1)                                 AS touchless_rate_pct,
    round(100.0 * count(*) FILTER (WHERE status = 'rejected_invalid')
          / NULLIF(count(*), 0), 1)                                 AS exception_rate_pct,
    round(avg(EXTRACT(EPOCH FROM (processed_at - received_at)))
          FILTER (WHERE route = 'auto'), 1)                         AS avg_touchless_seconds,
    coalesce(sum(gross_amount) FILTER (WHERE status = 'recorded'), 0) AS total_recorded_tnd
FROM invoices;

-- ------------------------------------------------------------------
-- Demo data (fictitious companies)
-- ------------------------------------------------------------------
INSERT INTO suppliers (name, tax_id, email, iban) VALUES
 ('Tech Supplies SARL', '1234567/A/M/000', 'billing@techsupplies.example', 'TN59 1000 6035 1835 9847 8831'),
 ('Office Pro SA',      '7654321/B/A/000', 'accounts@officepro.example',   'TN59 0800 1000 0123 4567 8910'),
 ('CloudNet Services',  '1122334/C/M/000', 'finance@cloudnet.example',     'TN59 2500 7000 0000 1122 3344');

INSERT INTO purchase_orders (po_number, supplier_tax_id, amount_gross) VALUES
 ('PO-2026-0101', '1234567/A/M/000', 1500.000),
 ('PO-2026-0102', '7654321/B/A/000', 6000.000),
 ('PO-2026-0103', '1122334/C/M/000', 2500.000);
