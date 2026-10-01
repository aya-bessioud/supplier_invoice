# Automatisation du traitement des factures avec n8n

Workflow **TO-BE** du projet : réception → IDP/OCR + IA (Gemini) → contrôle IA + règles métier → **Données valides ?** → **Approbation requise ?** → intégration ERP → archivage → reporting KPI.

```
invoice-n8n/
├── workflow/invoice_automation.json   ← le workflow à importer dans n8n
├── workflow/build_workflow.py         ← script qui génère le JSON (code lisible des nœuds)
├── sql/init.sql                       ← tables PostgreSQL + fournisseurs / bons de commande de démo
└── samples/                           ← 4 factures PDF de test (fictives)
```

---

## 1. Ce que fait le workflow

| Étape du schéma TO-BE | Nœud n8n | Rôle |
|---|---|---|
| 1. Réception de la facture | **Invoice upload form** | Formulaire web : on dépose le PDF / l'image et l'email du fournisseur |
| Paramètres | **Config** | Seuil d'approbation, confiance minimale, email du manager, dossier d'archive, modèle Gemini |
| 2. IDP / OCR + IA | **Prepare Gemini request** + **Gemini: OCR + AI extraction** | Gemini lit directement le PDF (OCR) et renvoie un JSON structuré : n° facture, dates, fournisseur, matricule fiscal, HT, TVA, TTC, bon de commande, IBAN, score de confiance |
| 3. Analyse & contrôle IA | **Parse extraction** + **Lookup supplier, PO & duplicates** + **Business rules check** | Contrôles : champs obligatoires, score de confiance ≥ 0,8, HT + TVA = TTC, fournisseur connu, bon de commande valide, pas de doublon |
| Données valides ? | **Valid data?** | Non → la facture est enregistrée comme rejetée et le fournisseur reçoit un email avec les motifs |
| Correction fournisseur | **Save: sent back to supplier** + **Email supplier: correction needed** | |
| Approbation requise ? | **Approval required?** | Montant TTC > 2 000 TND → approbation |
| Workflow d'approbation | **Approval workflow (manager)** + **Approved?** | Email au manager avec boutons **Approve / Reject** ; le workflow attend sa réponse |
| Intégration ERP | **Build ERP record** + **ERP integration (record invoice)** | Écriture dans la table `invoices` (l'ERP est simulé par PostgreSQL) + journal d'audit |
| Archivage | **Archiving** | Copie du fichier original dans le dossier d'archive |
| Paiement & reporting | **Payment & KPI reporting** | Lecture de la vue `kpi_summary` (taux touchless, taux d'exception, temps moyen, montant total) |

---

## 2. Installation (≈ 20 min)

### Prérequis
- **PostgreSQL** (déjà installé)
- **n8n** : `npx n8n` (Node.js 24 ou plus) ou Docker
- Une **clé API Gemini** (Google AI Studio → *Get API key*)
- Un **compte Gmail** pour envoyer les emails

### Étape 1 : créer la base de données
Dans un terminal (ou dans pgAdmin → *Query Tool*, en collant le contenu de `init.sql`) :
```bash
psql -U postgres -c "CREATE DATABASE invoices_db;"
psql -U postgres -d invoices_db -f sql/init.sql
```
Cela crée les tables `suppliers`, `purchase_orders`, `invoices`, `audit_log`, la vue `kpi_summary`, et 3 fournisseurs + 3 bons de commande de démo.

### Étape 2 : lancer n8n
```bash
npx n8n
```
Puis ouvrez http://localhost:5678 et créez votre compte local.

> Avec Docker, utilisez `host.docker.internal` comme hôte PostgreSQL à l'étape 4 :
> `docker run -it --rm -p 5678:5678 -v n8n_data:/home/node/.n8n docker.n8n.io/n8nio/n8n`

### Étape 3 : importer le workflow
**Workflows → ⋯ → Import from File** → choisissez `workflow/invoice_automation.json`.

### Étape 4 : créer les 3 credentials
Ouvrez chaque nœud concerné et sélectionnez / créez la credential :

| Credential | Nœuds | Réglages |
|---|---|---|
| **Header Auth** (nommez-la *Gemini API key*) | Gemini: OCR + AI extraction | **Name** : `x-goog-api-key` · **Value** : votre clé Gemini |
| **Postgres** | les 5 nœuds PostgreSQL | Host `localhost` (Docker : `host.docker.internal`) · Database `invoices_db` · User `postgres` · Password : le vôtre · Port `5432` · SSL : disable |
| **Gmail OAuth2** | Email supplier… et Approval workflow (manager) | Suivez l'assistant n8n (bouton *Sign in with Google*). Si n8n demande un Client ID : Google Cloud Console → API Gmail activée → identifiants OAuth, avec l'URL de redirection affichée par n8n |

La clé Gemini ne doit jamais être écrite dans le workflow : elle reste dans la credential.

### Étape 5 : régler le nœud **Config**
| Paramètre | Valeur par défaut | À faire |
|---|---|---|
| `manager_email` | manager@yourcompany.com | **Mettez votre email** (pour recevoir les demandes d'approbation) |
| `archive_dir` | /home/node/.n8n-files/archive | Windows : `C:/Users/<votre_nom>/.n8n-files/archive` · Docker : laisser tel quel |
| `approval_threshold` | 2000 | Seuil d'approbation en TND |
| `min_confidence` | 0.8 | Score de confiance IA minimal |
| `require_po` | true | Bon de commande obligatoire |
| `gemini_model` | gemini-2.5-flash | Modèle Gemini |

> n8n n'autorise l'écriture de fichiers que dans le dossier `.n8n-files` de votre dossier utilisateur. Créez le sous-dossier `archive` s'il n'existe pas.

### Étape 6 : activer
Cliquez sur **Publish** (ou le bouton *Active*) en haut à droite, puis ouvrez le formulaire :
**http://localhost:5678/form/invoice-upload**

---

## 3. Tester avec les factures fournies

Déposez les fichiers de `samples/` dans le formulaire (email fournisseur : le vôtre, pour recevoir les emails de correction).

| Fichier | Contenu | Résultat attendu |
|---|---|---|
| `facture_01_auto_valide.pdf` | Tech Supplies, 1 190 TND, PO-2026-0101 | ✅ Enregistrée **automatiquement** (route `auto`), archivée |
| `facture_02_approbation.pdf` | Office Pro, 4 998 TND > 2 000 | 📧 Email d'approbation au manager → **Approve** → enregistrée (route `approved`) |
| `facture_03_total_faux.pdf` | CloudNet, HT 1 800 + TVA 342 ≠ TTC 2 242 | ❌ Rejetée → email au fournisseur « Totals do not match » |
| `facture_04_fournisseur_inconnu.pdf` | Global Trade, pas de bon de commande | ❌ Rejetée → « Unknown supplier » + « Missing purchase-order reference » |
| `facture_01_auto_valide.pdf` **une 2ᵉ fois** | même facture | ❌ Rejetée → « Duplicate » |

### Vérifier les résultats dans PostgreSQL
```sql
SELECT id, invoice_number, supplier_name, gross_amount, status, route, rejection_reasons FROM invoices;
SELECT * FROM audit_log ORDER BY id;
SELECT * FROM kpi_summary;          -- tableau de bord KPI
```
Dans n8n, l'onglet **Executions** montre le chemin suivi par chaque facture, nœud par nœud.

### Résultat obtenu lors des tests (n8n 2.41.4 + PostgreSQL 16)
Pendant ces tests, l'appel Gemini était remplacé par un serveur local qui renvoie le même format de réponse, et les emails Gmail par des nœuds de simulation. Tout le reste (formulaire, règles, base, archivage, KPI) est le workflow réel.
| # | Facture | status | route | Motif |
|---|---|---|---|---|
| 1 | INV-TS-2026-041 | recorded | auto | |
| 2 | INV-CN-2026-007 | rejected_invalid | supplier_correction | Totals do not match: 1800 + 342 = 2142.000, but invoice says 2242 |
| 3 | GT-2026-0931 | rejected_invalid | supplier_correction | Unknown supplier: Global Trade SARL · Missing purchase-order reference |
| 4 | INV-TS-2026-041 (2ᵉ envoi) | rejected_invalid | supplier_correction | Duplicate: invoice INV-TS-2026-041 is already recorded |
| 5 | INV-OP-2026-118 | recorded | approved | (manager : Approve) |
| 6 | INV-OP-2026-118 | rejected_manager | manager_rejected | (manager : Reject) |

---

## 4. Remarques
- **Approbation par email** : les boutons *Approve / Reject* pointent vers votre n8n (`localhost`). Ils fonctionnent si vous cliquez depuis le même PC. Pour un manager sur un autre appareil, lancez n8n avec une URL publique (variable `WEBHOOK_URL`, par ex. via ngrok).
- **Gemini gratuit** : limité en nombre de requêtes par minute ; le nœud Gemini réessaie automatiquement 3 fois.
- **ERP** : simulé par la table `invoices`. Pour un vrai ERP (SAP, Odoo, Oracle), on remplacerait le nœud *ERP integration* par un appel API.
- **KPI** : la vue `kpi_summary` peut être branchée sur Metabase ou Grafana pour un vrai tableau de bord.
- **Sécurité** : les valeurs extraites par l'IA sont échappées avant d'être écrites en SQL.
