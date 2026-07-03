# -*- coding: utf-8 -*-
"""
Deep Autoencoder (DAE-O) adattato per impianto Pascale 500kW.

Architettura simmetrica per riduzione dimensionalita' e anomaly detection
basata su errore di ricostruzione (MSE).

Supporta due modalita':
1. Modello aggregato (8 feature impianto) — backward-compatible
2. Modello per-stringa (~40 feature normalizzate per n_moduli e orientamento)
"""

import io
import logging
import os
import pickle
import tempfile
from typing import Dict, List, Optional

import numpy as np
from sklearn.preprocessing import StandardScaler

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"

from tensorflow.keras.models import Model
from tensorflow.keras.layers import Input, Dense
from tensorflow.keras.callbacks import EarlyStopping
from tensorflow.keras.optimizers import Adam

from config import (
    AUTOENCODER_ACTIVATION,
    AUTOENCODER_CENTRAL,
    AUTOENCODER_LEARNING_RATE,
    AUTOENCODER_NODES,
    DATA_DIR,
    MODEL_WEIGHTS_FILE,
    N_FEATURES,
    SCALER_FILE,
    STRING_AUTOENCODER_ACTIVATION,
    STRING_AUTOENCODER_CENTRAL,
    STRING_AUTOENCODER_LEARNING_RATE,
    STRING_AUTOENCODER_NODES,
    STRING_MODEL_WEIGHTS_FILE,
    STRING_SCALER_FILE,
    STRING_FEATURES_FILE,
)

logger = logging.getLogger(__name__)


class PascaleAutoencoder:
    """
    Deep Autoencoder per rilevamento anomalie impianto Pascale.

    Input: 8 feature aggregate dell'impianto
    Architettura: 8 -> 6 -> 4 -> [2] -> 4 -> 6 -> 8
    Output: ricostruzione delle 8 feature
    Anomalia: MSE(input, output) > soglia
    """

    def __init__(
        self,
        n_features: int = N_FEATURES,
        nodes: Optional[List[int]] = None,
        nodes_central: int = AUTOENCODER_CENTRAL,
        activation: str = AUTOENCODER_ACTIVATION,
        learning_rate: float = AUTOENCODER_LEARNING_RATE,
    ):
        self.n_features = n_features
        self.nodes = nodes if nodes is not None else AUTOENCODER_NODES
        self.nodes_central = nodes_central
        self.activation = activation
        self.learning_rate = learning_rate

        self.autoencoder: Optional[Model] = None
        self.encoder: Optional[Model] = None
        self.scaler = StandardScaler()
        self.is_trained = False

    def build_model(self):
        """Costruisce l'autoencoder simmetrico."""
        input_layer = Input(shape=(self.n_features,), name="input")

        # Encoder
        encoded = input_layer
        for i, n in enumerate(self.nodes):
            encoded = Dense(n, activation=self.activation, name=f"enc_{i}")(encoded)

        # Bottleneck
        bottleneck = Dense(
            self.nodes_central, activation=self.activation, name="bottleneck"
        )(encoded)

        # Decoder (specchiato)
        decoded = bottleneck
        for i, n in enumerate(reversed(self.nodes)):
            decoded = Dense(n, activation=self.activation, name=f"dec_{i}")(decoded)

        # Output
        output_layer = Dense(self.n_features, activation="linear", name="output")(decoded)

        self.autoencoder = Model(input_layer, output_layer, name="pascale_dae")
        self.encoder = Model(input_layer, bottleneck, name="pascale_encoder")
        self.autoencoder.compile(optimizer=Adam(learning_rate=self.learning_rate), loss="mse")

        logger.info(
            f"Modello: {self.n_features} -> {self.nodes} -> "
            f"[{self.nodes_central}] -> {list(reversed(self.nodes))} -> {self.n_features}"
        )

    def train(
        self,
        X_train: np.ndarray,
        epochs: int = 500,
        batch_size: int = 32,
        patience: int = 50,
    ) -> Dict:
        """Addestra il modello."""
        if self.autoencoder is None:
            self.build_model()

        # Normalizzazione
        X_scaled = self.scaler.fit_transform(X_train)

        # Split 80/20
        split = int(len(X_scaled) * 0.8)
        X_tr = X_scaled[:split]
        X_val = X_scaled[split:]

        history = self.autoencoder.fit(
            X_tr, X_tr,
            epochs=epochs,
            batch_size=batch_size,
            shuffle=True,
            validation_data=(X_val, X_val),
            callbacks=[
                EarlyStopping(
                    monitor="val_loss", patience=patience,
                    mode="min", restore_best_weights=True, verbose=1
                )
            ],
            verbose=1,
        )

        self.is_trained = True
        self.save_model()

        # MSE sul training
        train_mse = self.compute_mse(X_train)
        return {
            "final_loss": history.history["loss"][-1],
            "final_val_loss": history.history["val_loss"][-1],
            "epochs_trained": len(history.history["loss"]),
            "train_mse_mean": float(np.mean(train_mse)),
            "train_mse_std": float(np.std(train_mse)),
        }

    def compute_mse(self, X: np.ndarray) -> np.ndarray:
        """Calcola MSE per ogni campione."""
        if not self.is_trained:
            raise RuntimeError("Modello non addestrato")
        X_scaled = self.scaler.transform(X)
        X_recon = self.autoencoder.predict(X_scaled, verbose=0)
        return np.mean((X_scaled - X_recon) ** 2, axis=1)

    def compute_per_feature_mse(self, X: np.ndarray) -> np.ndarray:
        """MSE per ogni feature di ogni campione (per diagnostica per-stringa)."""
        if not self.is_trained:
            raise RuntimeError("Modello non addestrato")
        X_scaled = self.scaler.transform(X)
        X_recon = self.autoencoder.predict(X_scaled, verbose=0)
        return (X_scaled - X_recon) ** 2

    def compute_single_mse(self, sample: np.ndarray) -> float:
        """MSE per un singolo campione."""
        return float(self.compute_mse(sample.reshape(1, -1))[0])

    def compute_single_per_feature_mse(self, sample: np.ndarray) -> np.ndarray:
        """MSE per-feature per un singolo campione."""
        return self.compute_per_feature_mse(sample.reshape(1, -1))[0]

    def get_latent_representation(self, X: np.ndarray) -> np.ndarray:
        """Proietta i campioni nello spazio latente (bottleneck)."""
        if not self.is_trained or self.encoder is None:
            raise RuntimeError("Modello non addestrato")
        X_scaled = self.scaler.transform(X)
        return self.encoder.predict(X_scaled, verbose=0)

    def save_model(self):
        """Salva modello e scaler su disco."""
        os.makedirs(DATA_DIR, exist_ok=True)
        if self.autoencoder:
            self.autoencoder.save_weights(MODEL_WEIGHTS_FILE)
        with open(SCALER_FILE, "wb") as f:
            pickle.dump(self.scaler, f)
        logger.info("Modello salvato su disco")

    def get_weights_bytes(self) -> Optional[bytes]:
        """Serializza i pesi del modello in bytes per il DB."""
        if not self.autoencoder:
            return None
        try:
            with tempfile.NamedTemporaryFile(suffix=".weights.h5", delete=False) as tmp:
                tmp_path = tmp.name
            self.autoencoder.save_weights(tmp_path)
            with open(tmp_path, "rb") as f:
                data = f.read()
            os.unlink(tmp_path)
            return data
        except Exception as e:
            logger.error(f"Errore serializzazione pesi: {e}")
            return None

    def get_scaler_bytes(self) -> bytes:
        """Serializza lo scaler in bytes per il DB."""
        return pickle.dumps(self.scaler)

    def load_from_bytes(self, weights_bytes: bytes, scaler_bytes: bytes) -> bool:
        """Carica modello da bytes (dal DB)."""
        try:
            self.build_model()
            with tempfile.NamedTemporaryFile(suffix=".weights.h5", delete=False) as tmp:
                tmp.write(weights_bytes)
                tmp_path = tmp.name
            self.autoencoder.load_weights(tmp_path)
            os.unlink(tmp_path)
            self.scaler = pickle.loads(scaler_bytes)
            self.is_trained = True
            logger.info("Modello caricato dal database")
            return True
        except Exception as e:
            logger.error(f"Errore caricamento da bytes: {e}")
            return False

    def load_model(self) -> bool:
        """Carica modello da disco."""
        if not os.path.exists(MODEL_WEIGHTS_FILE) or not os.path.exists(SCALER_FILE):
            return False
        try:
            self.build_model()
            self.autoencoder.load_weights(MODEL_WEIGHTS_FILE)
            with open(SCALER_FILE, "rb") as f:
                self.scaler = pickle.load(f)
            self.is_trained = True
            logger.info("Modello caricato da disco")
            return True
        except Exception as e:
            logger.error(f"Errore caricamento: {e}")
            return False


class StringAutoencoder(PascaleAutoencoder):
    """
    DAE per analisi per-stringa.

    Usa feature normalizzate (W/modulo) e raggruppate per orientamento.
    Architettura piu' ampia per gestire ~40 feature.
    """

    def __init__(self, n_features: int = 0, feature_names: Optional[List[str]] = None):
        self.feature_names = feature_names or []
        n = n_features or len(self.feature_names)
        if n == 0:
            n = 40

        nodes = list(STRING_AUTOENCODER_NODES)
        central = STRING_AUTOENCODER_CENTRAL
        # Adatta i layer alla dimensione dell'input
        if n < nodes[0]:
            nodes = [max(n - 2, 4), max(n - 4, 2)]
            central = max(n // 4, 2)

        super().__init__(
            n_features=n,
            nodes=nodes,
            nodes_central=central,
            activation=STRING_AUTOENCODER_ACTIVATION,
            learning_rate=STRING_AUTOENCODER_LEARNING_RATE,
        )

    def save_model(self):
        """Salva modello per-stringa su disco (path separati)."""
        os.makedirs(DATA_DIR, exist_ok=True)
        if self.autoencoder:
            self.autoencoder.save_weights(STRING_MODEL_WEIGHTS_FILE)
        with open(STRING_SCALER_FILE, "wb") as f:
            pickle.dump(self.scaler, f)
        # Salva feature_names e n_features per garantire coerenza al reload
        with open(STRING_FEATURES_FILE, "wb") as f:
            pickle.dump({
                "feature_names": self.feature_names,
                "n_features": self.n_features,
            }, f)
        logger.info("Modello per-stringa salvato su disco")

    def load_model(self) -> bool:
        """Carica modello per-stringa da disco."""
        if not os.path.exists(STRING_MODEL_WEIGHTS_FILE) or not os.path.exists(STRING_SCALER_FILE):
            return False
        try:
            # Ripristina feature_names e n_features salvati durante il training
            if os.path.exists(STRING_FEATURES_FILE):
                with open(STRING_FEATURES_FILE, "rb") as f:
                    feat_meta = pickle.load(f)
                self.feature_names = feat_meta["feature_names"]
                self.n_features = feat_meta["n_features"]
            else:
                # Backward compat: nessun file feature salvato.
                # Leggi n_features dal scaler per ricostruire correttamente.
                with open(STRING_SCALER_FILE, "rb") as f:
                    saved_scaler = pickle.load(f)
                scaler_n = getattr(saved_scaler, "n_features_in_", None)
                if scaler_n and scaler_n != self.n_features:
                    logger.warning(
                        f"Scaler attende {scaler_n} feature, "
                        f"costruttore ne ha {self.n_features}. "
                        f"Adatto il modello."
                    )
                    self.n_features = int(scaler_n)
                    # Tronca feature_names se piu' lunghe
                    self.feature_names = self.feature_names[:self.n_features]

            # Ricalcola architettura in base a n_features effettivo
            nodes = list(STRING_AUTOENCODER_NODES)
            central = STRING_AUTOENCODER_CENTRAL
            if self.n_features < nodes[0]:
                nodes = [max(self.n_features - 2, 4), max(self.n_features - 4, 2)]
                central = max(self.n_features // 4, 2)
            self.nodes = nodes
            self.nodes_central = central

            self.build_model()
            self.autoencoder.load_weights(STRING_MODEL_WEIGHTS_FILE)
            with open(STRING_SCALER_FILE, "rb") as f:
                self.scaler = pickle.load(f)
            self.is_trained = True
            logger.info(
                f"Modello per-stringa caricato da disco "
                f"(n_features={self.n_features})"
            )
            return True
        except Exception as e:
            logger.error(f"Errore caricamento modello per-stringa: {e}")
            return False

    def diagnose_features(
        self, sample: np.ndarray
    ) -> Dict[str, float]:
        """
        Calcola l'errore di ricostruzione per ogni feature.

        Returns:
            Dict {feature_name: mse_value} — le feature con MSE alta
            sono quelle anomale.
        """
        if not self.feature_names:
            return {}
        per_feat = self.compute_single_per_feature_mse(sample)
        return {
            name: float(val)
            for name, val in zip(self.feature_names, per_feat)
        }
