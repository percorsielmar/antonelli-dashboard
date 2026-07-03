# -*- coding: utf-8 -*-
"""
Configurazione per il servizio di anomaly detection DAE-O.
Si integra con il backend pascale-dashboard gia' deployato su Render.

Supporta analisi per-stringa con normalizzazione per numero moduli
e raggruppamento automatico per orientamento.
"""

import os

# URL del backend Node.js su Render (oppure localhost per sviluppo)
PASCALE_API_URL = os.environ.get(
    "PASCALE_API_URL",
    "https://pascale-dashboard.onrender.com"
)

# Intervallo di polling in secondi (il backend aggiorna ogni 5 min)
POLLING_INTERVAL = int(os.environ.get("POLLING_INTERVAL", "300"))

# Intervallo background polling server-side (indipendente dal browser)
BACKGROUND_POLLING_INTERVAL = int(os.environ.get("BACKGROUND_POLLING_INTERVAL", "300"))

# ---------------------------------------------------------------------------
# Specifiche moduli fotovoltaici — Trina Vertex S+ NEG18RC.27
# ---------------------------------------------------------------------------
MODULE_VMPP = 34.2       # Tensione al punto di massima potenza (V)
MODULE_VOC = 41.5         # Tensione a circuito aperto (V)
MODULE_PNOM = 500         # Potenza nominale (Wp)
MODULE_TEMP_COEFF = -0.0035  # Coefficiente temperatura potenza (%/°C)

# ---------------------------------------------------------------------------
# Inverter dell'impianto Pascale 500kW
# ---------------------------------------------------------------------------
INVERTER_LABELS = {
    "H34A15IA529024": "Hybrid 15kW",
    "X3F100J3116121": "X3F 100kW #1",
    "X3F100J3116094": "X3F 100kW #2",
    "A3F080J6733015": "A3F 80kW",
    "A3F100J7057023": "A3F 100kW #1",
    "A3F100L7869005": "A3F 100kW #2",
}

INVERTER_SNS = list(INVERTER_LABELS.keys())

# MPPT per ogni inverter (reali dal Zeus API, fino a 12 per X3-FTH)
INVERTER_MPPT_COUNT = {
    "H34A15IA529024": 2,    # Hybrid 15kW: 2 MPPT
    "X3F100J3116121": 12,   # X3-FTH 100kW: 12 MPPT
    "X3F100J3116094": 12,   # X3-FTH 100kW: 12 MPPT
    "A3F080J6733015": 12,   # X3-FTH 80kW: 12 MPPT
    "A3F100J7057023": 12,   # X3-FTH 100kW: 12 MPPT
    "A3F100L7869005": 12,   # X3-FTH 100kW: 12 MPPT
}

# MPPT fallback per API pubblica (solo 4 campi powerdc1..4)
INVERTER_MPPT_COUNT_PUBLIC_API = {
    "H34A15IA529024": 2,
    "X3F100J3116121": 4,
    "X3F100J3116094": 4,
    "A3F080J6733015": 4,
    "A3F100J7057023": 4,
    "A3F100L7869005": 4,
}

# Campi DC potenza e tensione (estesi fino a 12 MPPT per Zeus API)
DC_POWER_FIELDS = [f"powerdc{i}" for i in range(1, 13)]
DC_VOLTAGE_FIELDS = [f"vdc{i}" for i in range(1, 13)]
DC_CURRENT_FIELDS = [f"idc{i}" for i in range(1, 13)]
# Feature estratte da ogni inverter per il modello
INVERTER_FEATURES = [
    "acpower",       # Potenza AC (W)
    "powerdc1",      # Potenza DC stringa 1 (W)
    "powerdc2",      # Potenza DC stringa 2 (W)
    "powerdc3",      # Potenza DC stringa 3 (W)
    "powerdc4",      # Potenza DC stringa 4 (W)
    "yieldtoday",    # Produzione giornaliera (kWh)
    "feedinpower",   # Potenza immessa in rete (W)
]

# ---------------------------------------------------------------------------
# Feature per il DAE — due livelli: impianto + per-stringa
# ---------------------------------------------------------------------------

# Feature aggregate impianto (backward-compatible)
PLANT_FEATURES = [
    "total_ac_power",       # Somma potenze AC tutti gli inverter (W)
    "total_dc1_power",      # Somma DC1 (W)
    "total_dc2_power",      # Somma DC2 (W)
    "total_yield_today",    # Produzione totale giornaliera (kWh)
    "total_feedin",         # Potenza totale immessa in rete (W)
    "active_inverters",     # Numero inverter attivi
    "hybrid_soc",           # Stato di carica batteria hybrid (%)
    "hybrid_bat_power",     # Potenza batteria hybrid (W)
]

# Feature per-stringa: generate dinamicamente in base agli inverter attivi.
# Per ogni MPPT di ogni inverter:
#   - potenza DC normalizzata (W/modulo)
#   - rapporto DC/AC dell'inverter
# Per ogni gruppo di orientamento (auto-rilevato):
#   - coefficiente di variazione (deviazione/media)
#
# Il numero totale di feature dipende dalla configurazione rilevata.
# Con 6 inverter e fino a 12 MPPT = max 62 MPPT totali (~34 attivi).
# Feature per-stringa: ~34 potenze normalizzate + 6 rapporti DC/AC
#                     + N CV per orientamento + 8 feature impianto

# Architettura DAE per modello per-stringa
STRING_AUTOENCODER_NODES = [24, 12]
STRING_AUTOENCODER_CENTRAL = 6
STRING_AUTOENCODER_ACTIVATION = "sigmoid"
STRING_AUTOENCODER_LEARNING_RATE = 0.001

N_FEATURES = len(PLANT_FEATURES)

# Parametri modello (backward-compatible, per modello aggregato)
AUTOENCODER_NODES = [6, 4]          # Layer encoder
AUTOENCODER_CENTRAL = 2             # Bottleneck
AUTOENCODER_ACTIVATION = "sigmoid"
AUTOENCODER_LEARNING_RATE = 0.001

# Soglia anomalia
ANOMALY_THRESHOLD_SIGMA = 3.0   # media + N*sigma
TRAINING_SAMPLES_MIN = 50       # Minimo campioni per training aggregato
STRING_TRAINING_SAMPLES_MIN = 5  # Minimo campioni per training per-stringa
WINDOW_SIZE = 50                # Finestra per soglia dinamica

# Soglia correlazione per raggruppamento orientamento
ORIENTATION_CORR_THRESHOLD = 0.90

# Percorsi file (persistenza)
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
MODEL_WEIGHTS_FILE = os.path.join(DATA_DIR, "dae_model.weights.h5")
STRING_MODEL_WEIGHTS_FILE = os.path.join(DATA_DIR, "dae_string_model.weights.h5")
SCALER_FILE = os.path.join(DATA_DIR, "scaler.pkl")
STRING_SCALER_FILE = os.path.join(DATA_DIR, "string_scaler.pkl")
THRESHOLD_FILE = os.path.join(DATA_DIR, "threshold.pkl")
STRING_THRESHOLD_FILE = os.path.join(DATA_DIR, "string_threshold.pkl")
HISTORY_FILE = os.path.join(DATA_DIR, "history.csv")
STRING_HISTORY_FILE = os.path.join(DATA_DIR, "string_history.csv")
STRING_MAP_FILE = os.path.join(DATA_DIR, "string_map.pkl")
STRING_FEATURES_FILE = os.path.join(DATA_DIR, "string_features_meta.pkl")

# ---------------------------------------------------------------------------
# Zeus API (SolaX Cloud internal API per dati per-MPPT completi)
# ---------------------------------------------------------------------------
ZEUS_ENABLED = os.environ.get("ZEUS_ENABLED", "true").lower() in ("1", "true", "yes")
SOLAX_SITE_ID = os.environ.get("SOLAX_SITE_ID", "1905618003408482305")

# Dashboard
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8501"))
