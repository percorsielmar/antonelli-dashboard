# -*- coding: utf-8 -*-
"""
Persistenza modello DAE-O e dati storici su Postgres/Neon.

Usa lo stesso database del backend Node.js (pascale-dashboard).
Quando DATABASE_URL è configurata, il modello e i campioni
vengono salvati nel DB e sopravvivono ai riavvii del container.
"""

import json
import logging
import os
import pickle
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None
    logger.info("psycopg2 non disponibile — persistenza DB disabilitata")


def _get_connection():
    """Crea una connessione al database."""
    try:
        return psycopg2.connect(DATABASE_URL, sslmode="require")
    except psycopg2.OperationalError:
        return psycopg2.connect(DATABASE_URL)


def is_db_available() -> bool:
    """Verifica se il database è configurato e raggiungibile."""
    if not DATABASE_URL or psycopg2 is None:
        return False
    try:
        conn = _get_connection()
        conn.close()
        return True
    except Exception as e:
        logger.warning(f"Database non disponibile: {e}")
        return False


def init_dae_tables():
    """Crea le tabelle per il servizio DAE-O."""
    if not DATABASE_URL or psycopg2 is None:
        return
    try:
        conn = _get_connection()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS dae_model_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                model_weights BYTEA,
                scaler_data BYTEA,
                threshold DOUBLE PRECISION DEFAULT 0,
                mse_mean DOUBLE PRECISION DEFAULT 0,
                mse_std DOUBLE PRECISION DEFAULT 0,
                mse_history BYTEA,
                updated_at TIMESTAMP DEFAULT NOW(),
                CONSTRAINT single_model CHECK (id = 1)
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS dae_samples (
                id BIGSERIAL PRIMARY KEY,
                timestamp TEXT NOT NULL,
                features JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_dae_samples_timestamp
            ON dae_samples (timestamp);
        """)

        conn.commit()
        cur.close()
        conn.close()
        logger.info("Tabelle DAE inizializzate")
    except Exception as e:
        logger.error(f"Errore creazione tabelle: {e}")


def save_model_state(
    model_weights_bytes: bytes,
    scaler_bytes: bytes,
    threshold: float,
    mse_mean: float,
    mse_std: float,
    mse_history: List[float],
):
    """Salva lo stato completo del modello nel database."""
    if not DATABASE_URL or psycopg2 is None:
        return
    try:
        conn = _get_connection()
        cur = conn.cursor()

        mse_hist_bytes = pickle.dumps(mse_history[-1000:])

        cur.execute("""
            INSERT INTO dae_model_state (id, model_weights, scaler_data,
                threshold, mse_mean, mse_std, mse_history, updated_at)
            VALUES (1, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (id) DO UPDATE SET
                model_weights = EXCLUDED.model_weights,
                scaler_data = EXCLUDED.scaler_data,
                threshold = EXCLUDED.threshold,
                mse_mean = EXCLUDED.mse_mean,
                mse_std = EXCLUDED.mse_std,
                mse_history = EXCLUDED.mse_history,
                updated_at = NOW();
        """, (
            psycopg2.Binary(model_weights_bytes),
            psycopg2.Binary(scaler_bytes),
            threshold, mse_mean, mse_std,
            psycopg2.Binary(mse_hist_bytes),
        ))

        conn.commit()
        cur.close()
        conn.close()
        logger.info("Modello salvato nel database")
    except Exception as e:
        logger.error(f"Errore salvataggio modello nel DB: {e}")


def load_model_state() -> Optional[Dict]:
    """
    Carica lo stato del modello dal database.

    Returns:
        Dict con chiavi: model_weights, scaler_data, threshold, mse_mean,
        mse_std, mse_history. Oppure None se non disponibile.
    """
    if not DATABASE_URL or psycopg2 is None:
        return None
    try:
        conn = _get_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT model_weights, scaler_data, threshold, mse_mean,
                   mse_std, mse_history, updated_at
            FROM dae_model_state WHERE id = 1;
        """)
        row = cur.fetchone()
        cur.close()
        conn.close()

        if row is None:
            return None

        mse_history = []
        if row[5]:
            mse_history = pickle.loads(bytes(row[5]))

        return {
            "model_weights": bytes(row[0]) if row[0] else None,
            "scaler_data": bytes(row[1]) if row[1] else None,
            "threshold": row[2],
            "mse_mean": row[3],
            "mse_std": row[4],
            "mse_history": mse_history,
            "updated_at": row[6],
        }
    except Exception as e:
        logger.error(f"Errore caricamento modello dal DB: {e}")
        return None


def save_sample(timestamp: str, features: Dict[str, float]):
    """Salva un singolo campione nel database."""
    if not DATABASE_URL or psycopg2 is None:
        return
    try:
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO dae_samples (timestamp, features) VALUES (%s, %s)",
            (timestamp, json.dumps(features)),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Errore salvataggio campione: {e}")


def save_samples_batch(samples: List[Dict[str, float]]):
    """Salva un batch di campioni nel database (in chunk da 100)."""
    if not DATABASE_URL or psycopg2 is None or not samples:
        return
    saved = 0
    chunk_size = 100
    for i in range(0, len(samples), chunk_size):
        chunk = samples[i : i + chunk_size]
        try:
            conn = _get_connection()
            cur = conn.cursor()
            values = [
                (s.get("timestamp", ""), json.dumps({k: v for k, v in s.items() if k != "timestamp"}))
                for s in chunk
            ]
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO dae_samples (timestamp, features) VALUES %s",
                values,
            )
            conn.commit()
            cur.close()
            conn.close()
            saved += len(chunk)
        except Exception as e:
            logger.error(f"Errore salvataggio chunk {i}-{i+len(chunk)}: {e}")
    if saved > 0:
        logger.info(f"Salvati {saved}/{len(samples)} campioni nel DB")


def load_samples(limit: int = 5000) -> List[Dict[str, float]]:
    """Carica i campioni storici dal database."""
    if not DATABASE_URL or psycopg2 is None:
        return []
    try:
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT timestamp, features FROM dae_samples ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()

        samples = []
        for ts, features in reversed(rows):
            sample = features if isinstance(features, dict) else json.loads(features)
            sample["timestamp"] = ts
            # Ripristina _string_features se salvate nel JSONB
            if "_string_features" in sample:
                sf = sample.pop("_string_features")
                sample["_string_features"] = sf
            samples.append(sample)

        logger.info(f"Caricati {len(samples)} campioni dal DB")
        return samples
    except Exception as e:
        logger.error(f"Errore caricamento campioni dal DB: {e}")
        return []


def get_sample_count() -> int:
    """Conta i campioni nel database."""
    if not DATABASE_URL or psycopg2 is None:
        return 0
    try:
        conn = _get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM dae_samples;")
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count
    except Exception:
        return 0
