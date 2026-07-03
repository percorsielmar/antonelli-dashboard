---
name: testing-dae-anomaly
description: Test the DAE-O anomaly detection dashboard for the Pascale 500kW PV plant. Use when verifying auto-warmup, anomaly detection, Zeus API integration, or Streamlit dashboard changes.
---

# Testing DAE-O Anomaly Detection Dashboard

## Overview
The DAE-O service is a Python/Streamlit microservice in `dae_service/` that detects anomalies in the Pascale 500kW photovoltaic plant using a Deep Autoencoder. It supports two data sources: the public SolaX API (limited to 2 MPPT) and the internal Zeus API (up to 12 MPPT per inverter, 34+ active MPPT total).

## Prerequisites
- Python 3.11+ with dependencies from `dae_service/requirements.txt` (includes `pycryptodome` for Zeus AES encryption)
- Access to `https://pascale-dashboard.onrender.com` (the Node.js backend API)
- No authentication required for the public API
- For Zeus API testing: `SOLAX_CLOUD_EMAIL` and `SOLAX_CLOUD_PASSWORD` env vars

## Running Locally

```bash
cd dae_service
pip install -r requirements.txt
# Clean persisted data for fresh test
rm -rf data/
streamlit run dashboard.py --server.port=8501 --server.headless=true --server.address=0.0.0.0
```

## Key Test Flows

### 1. Auto-Warmup (fresh start)
- Delete `dae_service/data/` directory before starting
- Start Streamlit — auto-warmup should trigger automatically
- Expect: spinner "Auto-inizializzazione...", then success with MSE value
- Sidebar should show "Modello aggregato attivo" (green) with hundreds of samples
- Auto-warmup uses `/api/snapshots/recent` (full inverter data) or falls back to `/api/history/day` (estimated features)
- Note: auto-warmup data comes from the backend history, NOT from Zeus API — so the Per-Stringa tab will initially show "Fonte dati: API pubblica" until a live fetch is performed

### 2. Zeus API Status
- Sidebar should show "Zeus API: attiva (per-MPPT completi)" in green if `SOLAX_CLOUD_EMAIL` and `SOLAX_CLOUD_PASSWORD` are set
- If credentials are missing, shows "Zeus API: non disponibile" in blue — this is expected fallback behavior
- Zeus login may fail if the SolaX Cloud account is temporarily locked (lockout after 5 failed attempts, unlocks after 30 min)

### 3. Live Data Fetch (Zeus + Public merged)
- Click "Fetch Dati Live" in sidebar
- Expect: sample collected, MSE/Soglia/Score displayed in "Stato" panel
- After fetch, Per-Stringa tab should show "Fonte dati: **Zeus API** (per-MPPT completi, fino a 12 MPPT/inverter)" in green
- If model was trained on warmup data (different feature distribution), expect high MSE on live data — this is normal and resolves after retraining

### 4. Configure Strings (requires Zeus data)
- Click "Configura Stringhe" in sidebar AFTER at least one live fetch
- Expect: "Configurate NN stringhe in N gruppi" where NN >= 30 (typically 36)
- Per-Stringa tab shows a table with columns: Stringa, N. Moduli, Gruppo Orient., Parallela
- Module counts should be in range 10-18 (estimated from voltage: `round(Vdc / 34.2V)` for Trina Vertex S+ modules)
- Multiple inverter labels visible: Hybrid 15kW, X3F 100kW #1/#2, A3F 80kW, A3F 100kW #1/#2
- Orientation groups show "0 gruppi" until 20+ Zeus-fetched samples are collected for correlation analysis

### 5. Train Per-String Model (Manual)
- Click "Addestra Modello Per-Stringa" after configuring strings
- Expect: "Training DAE per-stringa..." spinner, then "MSE medio: X.XXXXXX"
- Sidebar changes to "Modello per-stringa attivo" (green)
- If this errors with dimension mismatch, the string configuration or feature extraction is broken
- Minimum samples needed: 5 (STRING_TRAINING_SAMPLES_MIN) — NOT the 50 needed for aggregate model
- If insufficient samples, error message shows "Dati insufficienti (n/5 campioni)"

### 5b. Auto-Training Per-String Model (Background)
- The background poller (`background_poller.py`) automatically trains the per-string model when >= 5 samples with `_string_features` are collected
- Auto-configures strings if not already configured, then auto-trains
- To test: set `BACKGROUND_POLLING_INTERVAL=30` env var to speed up polling (default is 300s)
- After ~3 polling cycles (with 30s interval), sidebar should change from "non addestrato" to "Modello per-stringa attivo"
- Look for log messages: "Background: stringhe auto-configurate" and "Background: modello per-stringa addestrato! MSE=..."
- String features (`_string_features`) are computed during `process_sample()` and saved to DB as JSONB — they persist across restarts
- Note: auto-training may happen during auto-warmup itself if enough samples with raw inverter data exist in the backend

### 6. Per-String Live Analysis
- After training per-string model, click "Fetch Dati Live"
- Expect: Per-Stringa tab shows MSE Stringa, Soglia, Score metrics
- "Errore di Ricostruzione Per Feature" bar chart should have ~36 bars (one per active MPPT)
- "Tutte le stringhe nella norma" or specific anomalous strings listed

### 7. Model Training (Aggregate)
- Click "Addestra Modello Aggregato" after collecting 50+ samples
- Expect: MSE medio between 0.5-3.0 for real plant data

### 8. Reset
- Click "Reset Dati" — clears session state
- If model weights exist on disk, model reloads immediately (is_ready=True)
- To test full reset: delete `dae_service/data/` AND click Reset

### 9. DB Persistence (requires DATABASE_URL)
- Set `DATABASE_URL` env var pointing to a Postgres instance
- For local testing: `docker run -d --name test-postgres -p 5433:5432 -e POSTGRES_PASSWORD=testpass -e POSTGRES_DB=pascale_test postgres:15`
- Then: `DATABASE_URL="postgresql://postgres:testpass@localhost:5433/pascale_test"`
- Test cycle: train model → verify "Modello salvato nel database" in logs → delete `data/` dir → restart → verify "Detector pronto (DB)" in logs and sidebar shows "Modello attivo" immediately
- `db_persistence.py` tries SSL first (for Neon), falls back to plain connection (for local Postgres)
- Without DATABASE_URL: falls back gracefully to filesystem-only mode

## Testing Order (recommended)

### Quick Auto-Training Test (fastest)
1. Start fresh (`rm -rf data/`), set `BACKGROUND_POLLING_INTERVAL=30`
2. Launch Streamlit, verify auto-warmup completes
3. Wait ~2-3 min for background poller to collect 5+ samples
4. Refresh page — sidebar should show "Modello per-stringa attivo"
5. Click Per-Stringa tab — verify string config table, MSE chart, anomaly history
6. Click Spazio Latente tab — verify 2D bottleneck scatter and correlation matrix

### Full Manual Test
1. Start fresh (`rm -rf data/`), launch Streamlit
2. Verify auto-warmup completes and Zeus API status shows in sidebar
3. Click "Fetch Dati Live" — verify Zeus data source in Per-Stringa tab
4. Click "Configura Stringhe" — verify 30+ strings with module counts
5. Click "Addestra Modello Per-Stringa" — verify training completes
6. Click "Fetch Dati Live" again — verify per-string analysis with 36-bar chart

## Architecture Notes
- `dashboard.py` — Streamlit UI with 3 tabs: Impianto Aggregato, Analisi Per-Stringa, Spazio Latente
- `data_fetcher.py` — Fetches from Render API + Zeus API with automatic fallback
- `zeus_client.py` — SolaX Zeus API client with AES-CBC encryption, JWT auth, per-MPPT data
- `autoencoder.py` — Keras autoencoders (aggregate 8-dim + per-string N-dim)
- `string_analyzer.py` — Per-string analysis: voltage-based module estimation, orientation grouping
- `anomaly_detector.py` — MSE-based detection with dynamic threshold
- `db_persistence.py` — Postgres persistence for model weights + samples
- `config.py` — All configuration constants, inverter SNs, MPPT counts

## Render Deployment
- Dashboard URL: `https://pascale-dae-anomaly.onrender.com`
- Free tier: service sleeps after 15 min inactivity, cold start ~30-60s
- With DATABASE_URL configured: model loads from DB instantly on restart
- Without DATABASE_URL: falls back to auto-warmup (re-fetches + retrains)
- Zeus API env vars needed on Render: `SOLAX_CLOUD_EMAIL`, `SOLAX_CLOUD_PASSWORD`, `SOLAX_SITE_ID`, `ZEUS_ENABLED`

## Testing Tips
- Use `BACKGROUND_POLLING_INTERVAL=30` to speed up background polling (30s vs default 300s)
- Use `nohup streamlit run dashboard.py ... > /tmp/streamlit.log 2>&1 &` and monitor with `tail -f /tmp/streamlit.log`
- After auto-warmup, Streamlit session state may not reflect background poller changes — refresh the browser page (F5) to see updated sidebar status
- The string model may train during auto-warmup (not just via background poller) if enough historical samples have raw inverter data
- Check for string training in logs: `grep -i "string\|per-stringa\|auto-train" /tmp/streamlit.log`
- The "Anomalie Per-Stringa" section may say "Raccogli un campione live" even after auto-training — this is because the current Streamlit session hasn't processed a live sample yet. The Storico Anomalie Stringa section below it will show historical anomalies correctly.

## Common Issues
- **High MSE on live data after warmup**: Model trained on backend historical data has different feature distribution than live Zeus+public merged data. Retraining on Zeus-enriched data normalizes this.
- **"Fonte dati: API pubblica" after warmup**: Expected — warmup data comes from backend snapshots (no Zeus). Click "Fetch Dati Live" to trigger Zeus fetch and update the indicator.
- **0 orientation groups**: Needs 20+ Zeus-fetched samples for correlation-based group detection. Collect more live samples or wait for polling to accumulate data.
- **Zeus login failure**: Account may be locked after 5 failed attempts (30 min lockout). Verify credentials are correct. The system tries both `username` and `email` as loginName.
- **Port conflict**: Streamlit defaults to 8501; Render uses PORT env var (default 10000). Kill existing processes before restarting: `pkill -f "streamlit run"` and `fuser -k 8501/tcp`
- **TensorFlow warnings**: CUDA/GPU warnings are expected on CPU-only machines — safe to ignore.
- **Optimizer warning on DB load**: "Skipping variable loading for optimizer 'adam'" is harmless — only weights are persisted, not optimizer state.
- **SSL connection errors**: `_get_connection()` tries `sslmode="require"` first (for Neon), then falls back to plain. If both fail, check DATABASE_URL format.
- **1 of 6 inverters offline**: Inverter A3F080J6733015 or A3F100J7057023 may show 0W — this can be a real plant condition (inverter off), not necessarily a bug.
- **Feature mismatch ValueError on model reload**: If Zeus API detects more MPPT strings than when the model was trained (e.g. 42 trained -> 48 current), the `StringAutoencoder` might crash with `ValueError: X has N features, but StandardScaler is expecting M features`. The fix (PR #14) saves `feature_names` + `n_features` in `string_features_meta.pkl` alongside model weights, and restores them on `load_model()`. For pre-fix models without this file, it falls back to reading `scaler.n_features_in_`. To test this adversarially, use `dae_service/test_feature_mismatch.py`.
- **Streamlit background process**: Use `nohup streamlit run dashboard.py ... > /tmp/streamlit.log 2>&1 &` for background execution. Check logs with `cat /tmp/streamlit.log` and `grep -i "error\|traceback" /tmp/streamlit.log`.

## Devin Secrets Needed
- `SOLAX_CLOUD_EMAIL` — SolaX Cloud login email. Required for Zeus API testing.
- `SOLAX_CLOUD_PASSWORD` — SolaX Cloud login password. Required for Zeus API testing.
- `DATABASE_URL` — Postgres connection string (e.g. Neon). Required for DB persistence testing. Without it, only filesystem-based tests are possible.
