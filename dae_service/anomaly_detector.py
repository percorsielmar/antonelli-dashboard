# -*- coding: utf-8 -*-
"""
Pipeline di rilevamento anomalie per impianto Pascale 500kW.

Integra il DataFetcher con il PascaleAutoencoder (DAE-O).
Supporta sia il modello aggregato (backward-compatible) sia il
modello per-stringa con analisi normalizzata W/modulo.
"""

import logging
import os
import pickle
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np

from config import (
    ANOMALY_THRESHOLD_SIGMA,
    DATA_DIR,
    PLANT_FEATURES,
    STRING_THRESHOLD_FILE,
    STRING_TRAINING_SAMPLES_MIN,
    THRESHOLD_FILE,
    TRAINING_SAMPLES_MIN,
    WINDOW_SIZE,
)
from autoencoder import PascaleAutoencoder, StringAutoencoder
from data_fetcher import PascaleDataFetcher
from string_analyzer import StringAnalyzer
import db_persistence as db

logger = logging.getLogger(__name__)


class AnomalyDetector:
    """
    Rileva anomalie nell'impianto tramite picchi di MSE.

    1. Raccoglie dati dall'API pascale-dashboard
    2. Normalizza e passa al DAE-O
    3. Calcola MSE di ricostruzione
    4. Confronta con soglia (media + 3*sigma)
    5. Genera alert se MSE > soglia

    Modello per-stringa (opzionale):
    - Analizza ogni MPPT di ogni inverter separatamente
    - Normalizza per numero moduli (W/modulo)
    - Raggruppa per orientamento e confronta
    """

    def __init__(self, api_url: Optional[str] = None):
        kwargs = {"api_url": api_url} if api_url else {}
        self.fetcher = PascaleDataFetcher(**kwargs)
        self.model = PascaleAutoencoder()
        self.string_analyzer = StringAnalyzer()
        self.string_model: Optional[StringAutoencoder] = None
        self.mse_history: List[float] = []
        self.string_mse_history: List[float] = []
        self.anomaly_log: List[Dict] = []
        self.string_anomaly_log: List[Dict] = []
        self.threshold: float = 0.0
        self.mse_mean: float = 0.0
        self.mse_std: float = 0.0
        self.string_threshold: float = 0.0
        self.string_mse_mean: float = 0.0
        self.string_mse_std: float = 0.0
        self.is_ready = False
        self.string_model_ready = False
        self._needs_string_retrain = False
        self.db_available = False

        self._init_db()
        self._load_state()

    def _init_db(self):
        """Inizializza connessione e tabelle DB."""
        self.db_available = db.is_db_available()
        if self.db_available:
            db.init_dae_tables()
            logger.info("Persistenza DB attiva")
        else:
            logger.info("Persistenza DB non disponibile — uso solo filesystem")

    def _load_state(self):
        """Carica modello e soglia (DB prioritario, poi disco)."""
        loaded = False

        if self.db_available:
            loaded = self._load_from_db()

        if not loaded and self.model.load_model():
            self._load_threshold()
            if self.threshold > 0:
                loaded = True
                logger.info(f"Detector pronto (disco). Soglia: {self.threshold:.6f}")

        if loaded:
            self.is_ready = True

        if self.db_available:
            self._load_samples_from_db()
        else:
            self.fetcher.load_history_from_csv()

        # Carica modello per-stringa se disponibile
        self._load_string_model()

    def _load_from_db(self) -> bool:
        """Carica modello completo dal database."""
        state = db.load_model_state()
        if state is None or state["model_weights"] is None:
            return False
        try:
            if self.model.load_from_bytes(state["model_weights"], state["scaler_data"]):
                self.threshold = state["threshold"]
                self.mse_mean = state["mse_mean"]
                self.mse_std = state["mse_std"]
                self.mse_history = state.get("mse_history", [])
                logger.info(f"Detector pronto (DB). Soglia: {self.threshold:.6f}")
                return True
        except Exception as e:
            logger.error(f"Errore caricamento da DB: {e}")
        return False

    def _load_samples_from_db(self):
        """Carica campioni storici dal DB nel fetcher."""
        samples = db.load_samples(limit=5000)
        if samples:
            self.fetcher.history = samples
            logger.info(f"Caricati {len(samples)} campioni dal DB")
        else:
            self.fetcher.load_history_from_csv()

    def _load_threshold(self):
        if os.path.exists(THRESHOLD_FILE):
            try:
                with open(THRESHOLD_FILE, "rb") as f:
                    state = pickle.load(f)
                self.threshold = state["threshold"]
                self.mse_mean = state["mse_mean"]
                self.mse_std = state["mse_std"]
                self.mse_history = state.get("mse_history", [])
            except Exception as e:
                logger.error(f"Errore caricamento soglia: {e}")

    def _save_threshold(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        state = {
            "threshold": self.threshold,
            "mse_mean": self.mse_mean,
            "mse_std": self.mse_std,
            "mse_history": self.mse_history[-1000:],
        }
        with open(THRESHOLD_FILE, "wb") as f:
            pickle.dump(state, f)

    def _load_string_model(self):
        """Carica il modello per-stringa se disponibile."""
        if not self.string_analyzer.is_configured:
            return
        current_feature_names = self.string_analyzer.get_feature_names()
        if not current_feature_names:
            return
        # Crea con n_features placeholder — load_model() ripristinera' i valori salvati
        self.string_model = StringAutoencoder(
            n_features=len(current_feature_names),
            feature_names=current_feature_names,
        )
        if self.string_model.load_model():
            self._load_string_threshold()
            # Verifica coerenza feature: il modello potrebbe essere stato
            # addestrato con un set diverso di feature (es. meno MPPT attivi)
            saved_names = self.string_model.feature_names
            if len(saved_names) != len(current_feature_names) or saved_names != current_feature_names:
                logger.warning(
                    f"Feature mismatch: modello addestrato con {len(saved_names)} feature, "
                    f"analyzer attuale ne produce {len(current_feature_names)}. "
                    f"Il modello verra' riaddestrato al prossimo ciclo."
                )
                self._needs_string_retrain = True
            if self.string_threshold > 0:
                self.string_model_ready = True
                logger.info(
                    f"Modello per-stringa pronto. Soglia: {self.string_threshold:.6f}"
                )

    def _load_string_threshold(self):
        if os.path.exists(STRING_THRESHOLD_FILE):
            try:
                with open(STRING_THRESHOLD_FILE, "rb") as f:
                    state = pickle.load(f)
                self.string_threshold = state["threshold"]
                self.string_mse_mean = state["mse_mean"]
                self.string_mse_std = state["mse_std"]
                self.string_mse_history = state.get("mse_history", [])
            except Exception as e:
                logger.error(f"Errore caricamento soglia per-stringa: {e}")

    def _save_string_threshold(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        state = {
            "threshold": self.string_threshold,
            "mse_mean": self.string_mse_mean,
            "mse_std": self.string_mse_std,
            "mse_history": self.string_mse_history[-1000:],
        }
        with open(STRING_THRESHOLD_FILE, "wb") as f:
            pickle.dump(state, f)

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train_model(
        self,
        X_train: Optional[np.ndarray] = None,
        epochs: int = 150,
        batch_size: int = 64,
        patience: int = 30,
    ) -> Dict:
        """Addestra il modello aggregato sui dati storici."""
        if X_train is None:
            X_train = self.fetcher.get_feature_array()

        if len(X_train) < TRAINING_SAMPLES_MIN:
            logger.warning(f"Dati insufficienti: {len(X_train)}/{TRAINING_SAMPLES_MIN}")
            return {"error": "Dati insufficienti", "samples": len(X_train)}

        self.model.build_model()
        metrics = self.model.train(X_train, epochs=epochs, batch_size=batch_size, patience=patience)

        # Calcola soglia
        mse_values = self.model.compute_mse(X_train)
        self.mse_mean = float(np.mean(mse_values))
        self.mse_std = float(np.std(mse_values))
        self.threshold = self.mse_mean + ANOMALY_THRESHOLD_SIGMA * self.mse_std
        self._save_threshold()
        self._save_model_to_db()
        self.is_ready = True

        logger.info(f"Soglia: {self.threshold:.6f} (media={self.mse_mean:.6f}, std={self.mse_std:.6f})")
        return metrics

    def configure_strings(self):
        """
        Configura l'analizzatore stringhe dai dati storici.

        Stima n_moduli per stringa e rileva i gruppi di orientamento.
        Richiede campioni con dati raw inverter (_raw_inverters).
        """
        history_with_raw = [
            s for s in self.fetcher.history if s.get("_raw_inverters")
        ]
        if not history_with_raw:
            logger.warning("Nessun campione con dati raw inverter per configurazione stringhe")
            return

        # Configura n_moduli
        latest_raw = history_with_raw[-1].get("_raw_inverters", [])
        self.string_analyzer.configure_strings(
            inverter_data=latest_raw,
            history=history_with_raw,
        )

        # Rileva orientamenti
        if len(history_with_raw) >= 20:
            self.string_analyzer.detect_orientation_groups(history_with_raw)

        # Calcola feature per-stringa per ogni campione storico
        for sample in history_with_raw:
            raw = sample.get("_raw_inverters", [])
            if raw:
                feats = self.string_analyzer.extract_string_features(raw)
                sample["_string_features"] = feats

    def train_string_model(
        self,
        epochs: int = 200,
        batch_size: int = 64,
        patience: int = 40,
    ) -> Dict:
        """Addestra il modello per-stringa."""
        if not self.string_analyzer.is_configured:
            self.configure_strings()

        if not self.string_analyzer.is_configured:
            return {"error": "Stringhe non configurate"}

        feature_names = self.string_analyzer.get_feature_names()
        if not feature_names:
            return {"error": "Nessuna feature per-stringa disponibile"}

        X = self.fetcher.get_string_feature_array(feature_names)
        # Filtra righe vuote (campioni senza dati raw)
        row_sums = np.sum(np.abs(X), axis=1)
        X = X[row_sums > 0]

        if len(X) < STRING_TRAINING_SAMPLES_MIN:
            return {"error": "Dati insufficienti", "samples": len(X), "min": STRING_TRAINING_SAMPLES_MIN}

        self.string_model = StringAutoencoder(
            n_features=len(feature_names),
            feature_names=feature_names,
        )
        self.string_model.build_model()
        metrics = self.string_model.train(
            X, epochs=epochs, batch_size=batch_size, patience=patience
        )

        mse_values = self.string_model.compute_mse(X)
        self.string_mse_mean = float(np.mean(mse_values))
        self.string_mse_std = float(np.std(mse_values))
        self.string_threshold = (
            self.string_mse_mean + ANOMALY_THRESHOLD_SIGMA * self.string_mse_std
        )
        self._save_string_threshold()
        self.string_model_ready = True

        logger.info(
            f"Soglia per-stringa: {self.string_threshold:.6f} "
            f"(media={self.string_mse_mean:.6f}, std={self.string_mse_std:.6f})"
        )
        return metrics

    def _save_model_to_db(self):
        """Salva modello completo nel database."""
        if not self.db_available:
            return
        weights = self.model.get_weights_bytes()
        scaler = self.model.get_scaler_bytes()
        if weights and scaler:
            db.save_model_state(
                model_weights_bytes=weights,
                scaler_bytes=scaler,
                threshold=self.threshold,
                mse_mean=self.mse_mean,
                mse_std=self.mse_std,
                mse_history=self.mse_history,
            )

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    def process_sample(self, sample: Dict[str, float]) -> Dict:
        """
        Processa un campione e verifica anomalie (modello aggregato).

        Returns:
            {mse, threshold, is_anomaly, anomaly_score, timestamp, status}
        """
        if not self.is_ready:
            return {
                "mse": 0.0, "threshold": 0.0, "is_anomaly": False,
                "anomaly_score": 0.0, "timestamp": sample.get("timestamp", ""),
                "status": "not_ready",
            }

        # Estrai feature come array
        feature_vector = np.array(
            [sample.get(f, 0.0) for f in PLANT_FEATURES], dtype=np.float32
        )

        mse = self.model.compute_single_mse(feature_vector)
        self.mse_history.append(mse)

        is_anomaly = mse > self.threshold
        anomaly_score = mse / self.threshold if self.threshold > 0 else 0.0

        if is_anomaly:
            event = {
                "timestamp": sample.get("timestamp", datetime.now().isoformat()),
                "mse": mse, "threshold": self.threshold, "score": anomaly_score,
                "features": {f: sample.get(f, 0.0) for f in PLANT_FEATURES},
                "type": "plant",
            }
            self.anomaly_log.append(event)
            logger.warning(f"ANOMALIA! MSE={mse:.6f} > Soglia={self.threshold:.6f} (score={anomaly_score:.2f}x)")

        # Aggiorna soglia dinamica (media mobile esponenziale)
        if len(self.mse_history) > WINDOW_SIZE and not is_anomaly:
            recent = self.mse_history[-WINDOW_SIZE:]
            self.mse_mean = 0.95 * self.mse_mean + 0.05 * float(np.mean(recent))
            self.mse_std = 0.95 * self.mse_std + 0.05 * float(np.std(recent))
            self.threshold = self.mse_mean + ANOMALY_THRESHOLD_SIGMA * self.mse_std

        # Calcola e salva string features se il string_analyzer e' configurato
        raw_inverters = sample.get("_raw_inverters", [])
        if raw_inverters and self.string_analyzer.is_configured:
            string_feats = self.string_analyzer.extract_string_features(raw_inverters)
            sample["_string_features"] = string_feats

        if self.db_available:
            save_feats = {f: sample.get(f, 0.0) for f in PLANT_FEATURES}
            # Includi string features nel salvataggio DB
            if sample.get("_string_features"):
                save_feats["_string_features"] = sample["_string_features"]
            db.save_sample(
                timestamp=sample.get("timestamp", datetime.now().isoformat()),
                features=save_feats,
            )

        return {
            "mse": mse, "threshold": self.threshold, "is_anomaly": is_anomaly,
            "anomaly_score": anomaly_score,
            "timestamp": sample.get("timestamp", ""),
            "status": "active",
        }

    def process_sample_strings(self, sample: Dict) -> Dict:
        """
        Processa un campione con il modello per-stringa.

        Returns:
            {mse, threshold, is_anomaly, anomaly_score, status,
             anomalous_strings: [{string_label, deviation_pct, ...}],
             per_feature_mse: {feature: mse_value}}
        """
        if not self.string_model_ready or self.string_model is None:
            return {"status": "not_ready"}

        # Se il modello richiede retraining, eseguilo prima dell'inferenza
        if self._needs_string_retrain:
            logger.info("Riaddestramento modello per-stringa per feature mismatch...")
            retrain_result = self.train_string_model()
            if "error" not in retrain_result:
                self._needs_string_retrain = False
                logger.info("Riaddestramento completato con successo")
            else:
                logger.warning(f"Riaddestramento fallito: {retrain_result}")
                return {"status": "retrain_needed", "error": retrain_result.get("error")}

        raw_inverters = sample.get("_raw_inverters", [])
        if not raw_inverters:
            return {"status": "no_raw_data"}

        # Calcola feature per-stringa
        string_feats = self.string_analyzer.extract_string_features(raw_inverters)
        sample["_string_features"] = string_feats

        # Usa le feature_names del modello (quelle con cui e' stato addestrato)
        feature_names = self.string_model.feature_names
        feature_vector = np.array(
            [string_feats.get(f, 0.0) for f in feature_names], dtype=np.float32
        )

        mse = self.string_model.compute_single_mse(feature_vector)
        self.string_mse_history.append(mse)

        is_anomaly = mse > self.string_threshold
        anomaly_score = mse / self.string_threshold if self.string_threshold > 0 else 0.0

        # Diagnosi per-feature
        per_feat_mse = self.string_model.diagnose_features(feature_vector)

        # Identifica stringhe anomale dal confronto intra-gruppo
        anomalous_strings = self.string_analyzer.identify_anomalous_strings(
            string_feats, per_feat_mse
        )

        if is_anomaly or anomalous_strings:
            event = {
                "timestamp": sample.get("timestamp", datetime.now().isoformat()),
                "mse": mse,
                "threshold": self.string_threshold,
                "score": anomaly_score,
                "anomalous_strings": anomalous_strings,
                "type": "string",
            }
            self.string_anomaly_log.append(event)
            if anomalous_strings:
                for a in anomalous_strings:
                    logger.warning(
                        f"STRINGA ANOMALA: {a['string_label']} "
                        f"deviazione {a['deviation_pct']:+.1f}% dal gruppo"
                    )

        # Soglia dinamica
        if len(self.string_mse_history) > WINDOW_SIZE and not is_anomaly:
            recent = self.string_mse_history[-WINDOW_SIZE:]
            self.string_mse_mean = (
                0.95 * self.string_mse_mean + 0.05 * float(np.mean(recent))
            )
            self.string_mse_std = (
                0.95 * self.string_mse_std + 0.05 * float(np.std(recent))
            )
            self.string_threshold = (
                self.string_mse_mean + ANOMALY_THRESHOLD_SIGMA * self.string_mse_std
            )

        return {
            "mse": mse,
            "threshold": self.string_threshold,
            "is_anomaly": is_anomaly,
            "anomaly_score": anomaly_score,
            "anomalous_strings": anomalous_strings,
            "per_feature_mse": per_feat_mse,
            "timestamp": sample.get("timestamp", ""),
            "status": "active",
        }

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_mse_history(self, n_points: Optional[int] = None) -> List[float]:
        return self.mse_history[-n_points:] if n_points else self.mse_history

    def get_string_mse_history(self, n_points: Optional[int] = None) -> List[float]:
        return self.string_mse_history[-n_points:] if n_points else self.string_mse_history

    def get_anomaly_log(self, n_events: Optional[int] = None) -> List[Dict]:
        return self.anomaly_log[-n_events:] if n_events else self.anomaly_log

    def get_string_anomaly_log(self, n_events: Optional[int] = None) -> List[Dict]:
        return self.string_anomaly_log[-n_events:] if n_events else self.string_anomaly_log

    def get_status(self) -> Dict:
        return {
            "is_ready": self.is_ready,
            "threshold": self.threshold,
            "mse_mean": self.mse_mean,
            "mse_std": self.mse_std,
            "total_samples": len(self.mse_history),
            "total_anomalies": len(self.anomaly_log),
            "last_mse": self.mse_history[-1] if self.mse_history else None,
            "history_samples": len(self.fetcher.history),
            "string_model_ready": self.string_model_ready,
            "string_threshold": self.string_threshold,
            "string_total_anomalies": len(self.string_anomaly_log),
            "string_analyzer": self.string_analyzer.get_status(),
        }
