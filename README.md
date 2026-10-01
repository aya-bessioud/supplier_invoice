# Automatisation des factures fournisseurs — n8n + Ollama (local) + Google Sheets

Prototype du workflow TO-BE : **réception** (formulaire ou Gmail) → **IDP / OCR + IA locale** → **contrôles métier** →
**Données valides ?** → **Approbation requise ?** → **ERP (mock Google Sheets)** → **archivage Google Drive** →
**paiement simulé & KPI**.

- L'IA tourne **en local** (Ollama `qwen3.5:9b`, secours `qwen3:8b`). Aucune donnée de facture n'est envoyée à une API d'IA externe.
- Google n'est utilisé que pour **Gmail** (réception, emails), **Sheets** (base de données / ERP mock / KPI) et **Drive** (archive).
- Aucun paiement réel : `payment_status = scheduled` est une simple marque.

```
facture/
├── start_all.cmd                  ← double-clic : lance le service OCR + n8n
├── .env / .env.example            ← réglages (IDs Google, URLs, seuils) ; .env n'est jamais commité
├── ocr/app.py                     ← service OCR local (FastAPI, port 8000) : POST /extract
├── workflows/build_workflows.py   ← génère les workflows n8n (JSON) à partir de .env
├── workflows/00_setup_google_sheet.json   création unique du Sheet, du dossier Drive, du libellé Gmail
├── workflows/01_invoice_automation.json   LE workflow (54 nœuds)
├── workflows/97_sheet_guide.json          crée / met à jour l'onglet « Guide » du Sheet
├── workflows/98_reset_test_data.json      vide Invoices + AuditLog pour recommencer une démo
├── workflows/99_read_sheet.json           lecture des onglets (utilisé par scripts/read_sheet.py)
├── scripts/                       ← démarrage, déploiement, tests (voir plus bas)
├── samples/                       ← 10 factures de test fictives
├── docs/                          ← cahier des charges + résultats de tests
├── secrets/  storage/             ← client OAuth, données n8n (ignorés par Git)
└── invoice-n8n/                   ← version d'origine (Gemini + PostgreSQL), pour référence
```

---

## 1. Démarrer (au quotidien)

1. Vérifiez qu'**Ollama** tourne (icône dans la barre des tâches).
2. **Double-cliquez `start_all.cmd`** : deux fenêtres s'ouvrent (*OCR service* et *n8n*) puis, après environ 30 s,
   le formulaire s'ouvre dans le navigateur.
3. Pour arrêter : fermez les deux fenêtres.

| Adresse | Rôle |
|---|---|
| http://localhost:5678 | éditeur n8n (workflows, onglet **Executions**) |
| http://localhost:5678/form/invoice-upload | formulaire de dépôt de facture |
| http://localhost:8000/docs | service OCR (page de test intégrée) |
| [Google Sheet « Invoice Automation DB »](https://docs.google.com/spreadsheets/d/16wAF8N3rI9wtdrsu0j8uFgStno7MFf3qc9mjPG5exR4/edit) | Suppliers, PurchaseOrders, Invoices (ERP), AuditLog, KPI |
| [Dossier Drive « Invoice Archive »](https://drive.google.com/drive/folders/1Lb0OiFmi6qxASLF9F2dGTTPS-t8RD95v) | originaux des factures enregistrées |

> n8n doit tourner sur **http://localhost:5678** exactement (c'est l'URL de redirection OAuth déclarée chez Google).

---

## 2. Le workflow `01_invoice_automation`

| Étape TO-BE | Nœuds n8n | Détail |
|---|---|---|
| 1. Réception | **Invoice upload form**, **Gmail Trigger** → *Normalize* → **Process each invoice** | Chaque facture (ou chaque pièce jointe PDF/PNG/JPG) est traitée dans sa propre exécution. Gmail (vérifié chaque minute) : emails **non lus** avec une pièce jointe PDF/image **et** soit le libellé `INVOICE_AUTOMATION`, soit « facture » / « invoice » dans le sujet ; ils sont marqués lus et le libellé est retiré dès la prise en charge → jamais traités deux fois. |
| 2. IDP / OCR + IA | **OCR: extract text** (service local) → **Ollama: AI extraction** → **Check AI answer** → (*fallback* `qwen3:8b`) → **Validate extraction** | PDF texte : lecture directe ; PDF scanné / image : Tesseract fra+eng. Ollama `/api/chat`, `format` = schéma JSON, `temperature 0`, `think:false`. Le JSON est validé et normalisé par du code ; garde-fous anti-hallucination (n° de facture, n° de BC et matricule fiscal doivent apparaître dans le texte ; le MF du client n'est jamais pris pour celui du fournisseur). |
| 3. Analyse & contrôle | **Get suppliers / purchase orders / recorded invoices** (Sheets *Get row(s)*) → **Business rules check** | Champs obligatoires, confiance IA ≥ 0,8, HT + TVA = TTC (± 0,01), fournisseur connu, BC existant / du même fournisseur / ouvert / **non dépassé (cumul des factures déjà enregistrées)**, pas de doublon (même fournisseur + même n°, statut `recorded`). |
| 4. Données valides ? | **Valid data?** → non : *Invoices* `rejected_invalid` + *AuditLog* + **Email supplier: correction needed** | Email (Gmail) au fournisseur avec les motifs. Adresse : celle du formulaire, sinon l'expéditeur Gmail, sinon le référentiel. |
| 5. Approbation requise ? | **Approval required?** (TTC **> 2000** TND) → **Approval workflow (manager)** (Gmail *Send and Wait*, boutons Approve / Reject) → **Approved?** | Reject → `rejected_manager`. Approve → **re-contrôle** doublon + BC (une autre copie a pu être enregistrée pendant l'attente) → ERP. |
| 6. Intégration ERP | **Build ERP record** → … → **ERP integration (mock): append to Invoices** | `status = recorded`, `route = auto` ou `approved`. Pour un vrai ERP, remplacer ce nœud par un *HTTP Request* vers l'API de l'ERP. |
| 7. Archivage | **Archiving: upload to Drive** | Nom unique `date_fournisseur_numero_INV-…​.ext` (jamais d'écrasement), lien stocké dans `archive_link`. |
| 8. Paiement & reporting | **Build ERP row** (`payment_status = scheduled`, **simulé**) → **AuditLog** → **Payment & reporting: read KPI** | L'onglet **KPI** est fait uniquement de formules (COUNTIF / SUMIF / AVERAGE sur *Invoices*) + un graphique des factures par statut : il se met à jour tout seul. |

Panne technique (service OCR ou Ollama injoignable) : l'exécution **échoue** et apparaît en rouge dans n8n ;
la facture n'est **pas** renvoyée au fournisseur (ce n'est pas sa faute). Un fichier corrompu, lui, est renvoyé
proprement au fournisseur (« Unreadable file »), sans plantage.

### Statuts dans l'onglet *Invoices*

| status | route | signification |
|---|---|---|
| `recorded` | `auto` | valide et ≤ 2000 TND → enregistrée sans intervention (touchless) |
| `recorded` | `approved` | > 2000 TND, approuvée par le manager |
| `rejected_invalid` | `supplier_correction` | contrôle KO → email de correction au fournisseur |
| `rejected_manager` | `manager_rejected` | refusée par le manager |

---

## 3. Suivre ce que fait le robot

| Où | Ce qu'on y voit |
|---|---|
| **n8n → Overview → `01_invoice_automation`** | le workflow : tous les nœuds et leurs liens (double-clic sur un nœud = son réglage / son code) |
| **n8n → `01_invoice_automation` → onglet Executions** | une ligne par traitement. Deux types : *trigger/webhook* = réception (formulaire ou Gmail), *integrated* = traitement de la facture. Cliquez-en une : le chemin suivi est en vert, cliquez un nœud pour voir ses données d'entrée/sortie (texte OCR, JSON de l'IA, motifs de rejet…) |
| **Google Sheet → onglet AuditLog** | le journal lisible de chaque action, par facture |
| **Google Sheet → onglet Invoices** | le résultat final de chaque facture |
| **Google Sheet → onglet Guide** | l'explication de chaque onglet et de chaque colonne |

Le formulaire demande : **Facture** (le fichier) et **Email du fournisseur (facultatif)** = l'adresse à qui renvoyer la
facture si elle doit être corrigée. Vide → l'email du fournisseur dans l'onglet *Suppliers* est utilisé
(adresses de démo fictives `.example` : mettez la vôtre pour recevoir les emails de correction).

## 4. Démo / tests manuels

Avant une démo : n8n → workflow **98_reset_test_data** → *Execute workflow* (vide *Invoices* et *AuditLog*,
garde fournisseurs, BC et formules). Puis déposez les fichiers de `samples/` dans le formulaire
(email fournisseur : le vôtre, pour recevoir les emails de correction).

| # | Fichier | Contenu | Résultat attendu |
|---|---|---|---|
| 1 | `facture_01_auto_valide.pdf` | Tech Supplies, 1 190 TND | `recorded` / `auto`, archivée |
| 2 | `facture_05_exactement_2000.pdf` | CloudNet, **2 000,000** TND | `recorded` / `auto` (pas d'approbation : la règle est **> 2000**) |
| 3 | `facture_07_scan.png` | image scannée (OCR) | `recorded` / `auto` |
| 4 | `facture_08_scan.pdf` | PDF scanné sans texte (OCR) | `recorded` / `auto` |
| 5 | `facture_03_total_faux.pdf` | HT 1 800 + TVA 342 ≠ TTC 2 242 | `rejected_invalid` + email « Totals do not match » |
| 6 | `facture_04_fournisseur_inconnu.pdf` | Global Trade, pas de BC | `rejected_invalid` + « Unknown supplier » + « Missing purchase-order reference » |
| 7 | `facture_06_sans_numero.pdf` | pas de n° de facture | `rejected_invalid` + « Missing invoice number » |
| 8 | `facture_09_corrompue.pdf` | fichier corrompu | `rejected_invalid` + « Unreadable file », pas de plantage |
| 9 | `facture_01_auto_valide.pdf` (2ᵉ fois) | doublon | `rejected_invalid` + « Duplicate » |
| 10 | `facture_02_approbation.pdf` | Office Pro, 4 998 TND | email d'approbation → **Reject** → `rejected_manager` |
| 11 | `facture_02_approbation.pdf` | idem | email → **Approve** → `recorded` / `approved` |
| 12 | `facture_02_approbation.pdf` ×2 **avant** de cliquer | deux copies en attente | Approve sur les deux → la 1ʳᵉ `recorded`, la 2ᵉ `rejected_invalid` « Duplicate … while this copy was waiting for approval » |
| 13 | email avec `facture_11_gmail.pdf` | test Gmail (voir ci-dessous) | `recorded` / `auto`, email marqué lu |

Les boutons **Approve / Reject** pointent vers `localhost:5678` : cliquez-les depuis ce PC, n8n allumé.

### Test du déclencheur Gmail
1. Depuis **un autre compte** (un email envoyé à soi-même peut arriver déjà « lu »), envoyez à `kharratok@gmail.com`
   un email dont le **sujet contient « facture »** (ou « invoice »), avec un PDF/PNG/JPG en pièce jointe
   (par ex. `samples/facture_11_gmail.pdf`, jamais déposée). Aucun filtre Gmail n'est nécessaire.
2. En 1 à 2 minutes : l'email passe en « lu », nouvelle exécution dans n8n, ligne dans *Invoices*
   (`file_name` = nom de la pièce jointe), *AuditLog* `RECEIVED source=gmail`.
3. Le déclencheur ne voit que les emails arrivés **après** son activation. Pour rattraper des emails non lus déjà reçus,
   mettre `GMAIL_BACKFILL_SINCE=AAAA-MM-JJ` dans `.env` puis redéployer. Les mots-clés sont réglables avec `GMAIL_QUERY`.
4. Le libellé `INVOICE_AUTOMATION` reste possible (filtre Gmail ou libellé posé **avant** l'arrivée de l'email).

### Voir les résultats en ligne de commande
```bash
python scripts/show_executions.py --last 5          # dernières exécutions (+ --nodes pour le détail nœud par nœud)
python scripts/read_sheet.py --last 3               # dernières lignes Invoices + AuditLog + KPI
python scripts/submit_invoice.py samples/facture_01_auto_valide.pdf   # dépôt via le formulaire + attente du résultat
```

---

## 5. Installation complète (nouveau PC)

Prérequis : Windows, **Node.js ≥ 24**, **Python 3.11**, **Ollama** (`ollama pull qwen3.5:9b`, `ollama pull qwen3:8b`),
**Tesseract 5** dans `C:\Program Files\Tesseract-OCR` (les langues fra/eng sont téléchargées dans `ocr/tessdata`).

```powershell
# 1. n8n (local au projet) et service OCR
cd C:\Users\<vous>\facture\n8n-runtime ; npm install n8n@2.41.4
cd ..\ocr ; python -m venv .venv ; .venv\Scripts\pip install -r requirements.txt
#    langues Tesseract : fra.traineddata, eng.traineddata, osd.traineddata (github.com/tesseract-ocr/tessdata_fast) -> ocr\tessdata\

# 2. réglages : copier .env.example en .env, remplir N8N_USER_FOLDER, N8N_ENCRYPTION_KEY (32+ caractères aléatoires),
#    GOOGLE_ACCOUNT, MANAGER_EMAIL
```

**Google Cloud** (projet `tidal-tower-510323-d3`) : APIs **Gmail**, **Google Sheets**, **Google Drive** activées ;
écran de consentement OAuth → *Utilisateurs test* → le compte de test ; client OAuth de type *Application Web* avec
l'URI de redirection `http://localhost:5678/rest/oauth2-credential/callback`. Le fichier client est dans `secrets/`.

**Credentials n8n** (une fois) : créer *Gmail OAuth2*, *Google Sheets OAuth2*, *Google Drive OAuth2* avec le
`client_id` / `client_secret` du fichier `secrets/google_oauth_client.json` (dans l'UI n8n, ou via
`n8n import:credentials` depuis un fichier temporaire supprimé ensuite), puis **Sign in with Google** sur chacune.
Le secret ne doit jamais apparaître dans un workflow, le README ou Git.

**Création de la base et des workflows** :
```powershell
.\scripts\n8n-cli.ps1 import:workflow --input=workflows\00_setup_google_sheet.json
.\scripts\n8n-cli.ps1 execute --id=invSetupSheet001        # crée Sheet + dossier Drive + libellé Gmail
#   -> recopier GOOGLE_SHEET_ID, GOOGLE_DRIVE_FOLDER_ID, GMAIL_LABEL_ID affichés (nœud "Setup summary") dans .env
.\scripts\deploy_workflows.ps1                            # n8n arrêté : génère, importe et publie les workflows
start_all.cmd
```
Après toute modification de `.env` ou de `build_workflows.py` : arrêter n8n, `scripts\deploy_workflows.ps1`, relancer.

---

## 6. Dépannage

| Symptôme | Cause / solution |
|---|---|
| `redirect_uri_mismatch` à la connexion Google | n8n n'est pas sur `http://localhost:5678`, ou la modification du client OAuth n'est pas encore propagée (attendre 5 min). |
| `access_denied` / « app non validée » bloquante | Le compte n'est pas dans *Utilisateurs test* de l'écran de consentement. |
| Exécution en échec « OCR service unavailable » | La fenêtre *OCR service* est fermée → relancer `start_all.cmd` (ou `scripts\start_ocr.ps1`). Test : http://localhost:8000/health |
| Exécution en échec « Ollama unavailable » | Lancer Ollama ; test : `ollama list`. |
| `ECONNREFUSED ::1:…` | Utiliser `127.0.0.1` et non `localhost` pour `OCR_URL` / `OLLAMA_URL` (Node résout `localhost` en IPv6). |
| Le formulaire répond 404 | Le workflow n'est pas publié ou n8n a été démarré avant la publication : `scripts\deploy_workflows.ps1` puis redémarrer n8n. |
| L'email Gmail n'est jamais traité | Sujet sans « facture » / « invoice » et sans libellé, pas de pièce jointe PDF/image, email déjà lu, ou reçu avant l'activation (voir `GMAIL_BACKFILL_SINCE`). |
| Les boutons Approve / Reject ne répondent pas | Cliquer depuis ce PC, n8n allumé. Pour un manager sur un autre appareil : URL publique (`WEBHOOK_URL`, tunnel). |
| Port 5679 « already in use » avec la CLI | Utiliser `scripts\n8n-cli.ps1` (il change le port du task-runner). |
| `.ps1` bloqué par la stratégie d'exécution | `powershell -ExecutionPolicy Bypass -File scripts\...` (c'est ce que fait `start_all.cmd`). |
| Première facture lente (~30 s) | Chargement du modèle dans la carte graphique ; ensuite ~7 s par facture. |

---

## 7. Sécurité

- `.env`, `secrets/`, `storage/` sont ignorés par Git ; les workflows JSON ne contiennent que des IDs (Sheet, dossier, libellé).
- Le secret OAuth n'est stocké que dans les credentials n8n (chiffrées avec `N8N_ENCRYPTION_KEY`).
- Les valeurs extraites par l'IA sont écrites dans Sheets en mode *USER_ENTERED* mais neutralisées
  (une valeur qui commence par `=`, `+`, `-`, `@` est forcée en texte : pas d'injection de formule).
- L'assistant IA intégré de n8n (« Connect a model ») **ne doit pas** être connecté à un fournisseur externe.
