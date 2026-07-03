#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Avvia il servizio DAE-O per anomaly detection.

Modi di esecuzione:
  python run.py dashboard    → Avvia dashboard Streamlit
  python run.py collector    → Avvia raccolta dati continua con anomaly detection
  python run.py train        → Addestra il modello sui dati storici
  python run.py train-strings → Configura stringhe e addestra modello per-stringa
"""

import argparse
import logging
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run_dashboard():
    """Avvia la dashboard Streamlit."""
    dashboard_path = os.path.join(os.path.dirname(__file__), "dashboard.py")
    subprocess.run([
        sys.executable, "-m", "streamlit", "run", dashboard_path,
        "--server.port=8501", "--server.headless=true",
    ])


def run_collector(api_url: str, interval: int, auto_train: bool):
    """Avvia la raccolta dati continua."""
    from anomaly_detector import AnomalyDetector

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("collector")

    detector = AnomalyDetector(api_url=api_url)
    logger.info(f"Avvio collector - API: {api_url} - Intervallo: {interval}s")

    def on_sample(sample):
        if detector.is_ready:
            result = detector.process_sample(sample)
            if result["is_anomaly"]:
                logger.warning(f"ANOMALIA! Score={result['anomaly_score']:.2f}x")
            else:
                logger.info(f"OK - MSE={result['mse']:.6f}")

            # Analisi per-stringa
            if detector.string_model_ready:
                str_result = detector.process_sample_strings(sample)
                anom = str_result.get("anomalous_strings", [])
                if anom:
                    for a in anom:
                        logger.warning(
                            f"STRINGA ANOMALA: {a['string_label']} "
                            f"dev={a['deviation_pct']:+.1f}%"
                        )
        else:
            n = len(detector.fetcher.history)
            if n % 10 == 0:
                logger.info(f"Raccolti {n} campioni...")
            if auto_train and n >= 100 and not detector.is_ready:
                logger.info("Auto-training...")
                metrics = detector.train_model()
                logger.info(f"Training OK: MSE={metrics.get('train_mse_mean', 0):.6f}")

    detector.fetcher.run_continuous(callback=on_sample, interval=interval)


def run_train(api_url: str):
    """Addestra il modello sui dati storici disponibili."""
    from anomaly_detector import AnomalyDetector

    logging.basicConfig(level=logging.INFO)
    detector = AnomalyDetector(api_url=api_url)
    X = detector.fetcher.get_feature_array()

    if len(X) < 50:
        print(f"Dati insufficienti: {len(X)} campioni (minimo 50)")
        sys.exit(1)

    print(f"Training su {len(X)} campioni...")
    metrics = detector.train_model(X_train=X)
    print(f"Completato! MSE medio: {metrics.get('train_mse_mean', 0):.6f}")
    print(f"Soglia anomalia: {detector.threshold:.6f}")


def run_train_strings(api_url: str):
    """Configura stringhe e addestra modello per-stringa."""
    from anomaly_detector import AnomalyDetector

    logging.basicConfig(level=logging.INFO)
    detector = AnomalyDetector(api_url=api_url)

    print("Configurazione stringhe...")
    detector.configure_strings()
    sa_status = detector.string_analyzer.get_status()
    print(f"Stringhe configurate: {sa_status['configured_strings']}")
    print(f"Gruppi orientamento: {sa_status['orientation_groups']}")

    if sa_status['configured_strings'] == 0:
        print("Nessuna stringa configurata — servono dati con raw inverter")
        sys.exit(1)

    print("Training modello per-stringa...")
    metrics = detector.train_string_model()
    if "error" in metrics:
        print(f"Errore: {metrics['error']}")
        sys.exit(1)

    print(f"Completato! MSE medio: {metrics.get('train_mse_mean', 0):.6f}")
    print(f"Soglia per-stringa: {detector.string_threshold:.6f}")
    print(f"Gruppi: {sa_status.get('groups', {})}")


def main():
    parser = argparse.ArgumentParser(description="DAE-O Anomaly Detection - Pascale 500kW")
    parser.add_argument("command", choices=["dashboard", "collector", "train", "train-strings"],
                        help="Comando da eseguire")
    parser.add_argument("--api-url", default=None,
                        help="URL API pascale-dashboard")
    parser.add_argument("--interval", type=int, default=300,
                        help="Intervallo polling in secondi (default: 300)")
    parser.add_argument("--auto-train", action="store_true",
                        help="Auto-train dopo 100 campioni")
    args = parser.parse_args()

    if args.api_url:
        os.environ["PASCALE_API_URL"] = args.api_url

    if args.command == "dashboard":
        run_dashboard()
    elif args.command == "collector":
        api = args.api_url or os.environ.get("PASCALE_API_URL", "https://pascale-dashboard.onrender.com")
        run_collector(api, args.interval, args.auto_train)
    elif args.command == "train":
        api = args.api_url or os.environ.get("PASCALE_API_URL", "https://pascale-dashboard.onrender.com")
        run_train(api)
    elif args.command == "train-strings":
        api = args.api_url or os.environ.get("PASCALE_API_URL", "https://pascale-dashboard.onrender.com")
        run_train_strings(api)


if __name__ == "__main__":
    main()
