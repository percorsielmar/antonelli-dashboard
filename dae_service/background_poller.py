# -*- coding: utf-8 -*-
"""
Background polling — raccoglie e analizza dati ogni 5 minuti,
indipendentemente dal browser dell'utente.

Usa un singleton (modulo-level) condiviso da tutte le sessioni Streamlit.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

from config import BACKGROUND_POLLING_INTERVAL, STRING_TRAINING_SAMPLES_MIN

logger = logging.getLogger(__name__)

# Stato globale condiviso
_detector = None
_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_running = False
_last_poll: Optional[str] = None
_poll_count = 0


def get_detector():
    """Restituisce il detector condiviso (singleton)."""
    global _detector
    if _detector is None:
        from anomaly_detector import AnomalyDetector
        _detector = AnomalyDetector()
    return _detector


def get_poll_status() -> Dict:
    """Stato del polling background."""
    return {
        "running": _running,
        "last_poll": _last_poll,
        "poll_count": _poll_count,
    }


def _poll_once():
    """Esegue un singolo ciclo di polling."""
    global _last_poll, _poll_count
    import db_persistence as db

    detector = get_detector()
    if not detector.is_ready:
        logger.info("Background poll: modello non pronto, skip")
        return

    try:
        sample = detector.fetcher.collect_sample()
        if sample:
            result = detector.process_sample(sample)
            _last_poll = datetime.now().strftime("%H:%M:%S")
            _poll_count += 1

            mse = result.get("mse", 0)
            is_anomaly = result.get("is_anomaly", False)
            status = "ANOMALIA" if is_anomaly else "OK"
            logger.info(
                f"Background poll #{_poll_count}: MSE={mse:.4f} [{status}]"
            )

            # Analisi per-stringa se il modello e' pronto
            if detector.string_model_ready:
                try:
                    detector.process_sample_strings(sample)
                except Exception as e:
                    logger.debug(f"Background poll: errore analisi stringhe: {e}")

            # Auto-configura stringhe e auto-addestra quando ci sono abbastanza campioni
            _try_auto_train_strings(detector)
        else:
            logger.warning("Background poll: nessun campione dall'API")
    except Exception as e:
        logger.error(f"Background poll errore: {e}")


def _try_auto_train_strings(detector):
    """Auto-addestra il modello per-stringa quando ci sono abbastanza dati."""
    if detector.string_model_ready:
        return

    try:
        # Auto-configura stringhe se necessario
        if not detector.string_analyzer.is_configured:
            raw_samples = [s for s in detector.fetcher.history if s.get("_raw_inverters")]
            if raw_samples:
                detector.configure_strings()
                logger.info("Background: stringhe auto-configurate")

        if not detector.string_analyzer.is_configured:
            return

        # Conta campioni con string features
        n_string = sum(1 for s in detector.fetcher.history if s.get("_string_features"))
        if n_string >= STRING_TRAINING_SAMPLES_MIN:
            logger.info(f"Background: auto-training modello per-stringa ({n_string} campioni)")
            metrics = detector.train_string_model()
            if "error" not in metrics:
                logger.info(f"Background: modello per-stringa addestrato! MSE={metrics.get('train_mse_mean', 0):.6f}")
            else:
                logger.warning(f"Background: training per-stringa fallito: {metrics}")
    except Exception as e:
        logger.error(f"Background: errore auto-training stringhe: {e}")


def _polling_loop():
    """Loop infinito di polling."""
    global _running
    _running = True
    logger.info(f"Background polling avviato (intervallo: {BACKGROUND_POLLING_INTERVAL}s)")

    while _running:
        _poll_once()
        time.sleep(BACKGROUND_POLLING_INTERVAL)


def start_polling():
    """Avvia il thread di background polling (se non già attivo)."""
    global _thread, _running
    with _lock:
        if _thread is not None and _thread.is_alive():
            return  # Già in esecuzione

        _running = True
        _thread = threading.Thread(target=_polling_loop, daemon=True, name="dae-poller")
        _thread.start()
        logger.info("Background polling thread avviato")


def stop_polling():
    """Ferma il polling."""
    global _running
    _running = False
    logger.info("Background polling fermato")
