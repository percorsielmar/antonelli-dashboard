# -*- coding: utf-8 -*-
"""
Modulo di acquisizione dati dal backend pascale-dashboard.

Interroga l'API REST del server Node.js (su Render) per ottenere
i dati real-time e storici dell'impianto FV Pascale 500kW.

Salva sia le feature aggregate (backward-compatible) sia i dati
grezzi per-inverter necessari per l'analisi per-stringa.
"""

import csv
import logging
import os
import time
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import requests

from config import (
    DATA_DIR,
    DC_POWER_FIELDS,
    HISTORY_FILE,
    INVERTER_MPPT_COUNT,
    INVERTER_MPPT_COUNT_PUBLIC_API,
    INVERTER_SNS,
    PASCALE_API_URL,
    PLANT_FEATURES,
    POLLING_INTERVAL,
    ZEUS_ENABLED,
)

logger = logging.getLogger(__name__)


class PascaleDataFetcher:
    """Recupera dati dall'API del pascale-dashboard su Render + Zeus API."""

    def __init__(self, api_url: str = PASCALE_API_URL):
        self.api_url = api_url.rstrip("/")
        self.session = requests.Session()
        self.history: List[Dict[str, float]] = []
        self._zeus_client = None
        self._zeus_available = False
        self._init_zeus()
        self._ensure_data_dir()

    def _init_zeus(self):
        """Inizializza il client Zeus API se le credenziali sono disponibili."""
        if not ZEUS_ENABLED:
            logger.info("Zeus API disabilitata (ZEUS_ENABLED=false)")
            return
        try:
            from zeus_client import ZeusClient
            self._zeus_client = ZeusClient()
            if self._zeus_client.is_available:
                self._zeus_available = True
                logger.info("Zeus API client inizializzato")
                # Auto-discover inverter se la lista e' vuota
                if not INVERTER_SNS:
                    self._discovered_sns = self._zeus_client.discover_inverters()
                    logger.info(f"Auto-discovery: {len(self._discovered_sns)} inverter trovati")
                else:
                    self._discovered_sns = []
            else:
                logger.info("Zeus API: credenziali non disponibili, uso API pubblica")
                self._discovered_sns = []
        except ImportError as e:
            logger.info(f"Zeus API: modulo non disponibile ({e}), uso API pubblica")
            self._discovered_sns = []
        except Exception as e:
            logger.warning(f"Zeus API: errore inizializzazione — {e}")
            self._discovered_sns = []

    @property
    def active_inverter_sns(self) -> list:
        """Ritorna la lista di SN da usare: configurati o auto-scoperti."""
        return INVERTER_SNS if INVERTER_SNS else self._discovered_sns

    def _ensure_data_dir(self):
        """Crea directory dati se non esiste."""
        os.makedirs(DATA_DIR, exist_ok=True)
        if not os.path.exists(HISTORY_FILE):
            header = ["timestamp"] + PLANT_FEATURES
            with open(HISTORY_FILE, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(header)

    def fetch_realtime(self) -> Optional[Dict[str, float]]:
        """
        Recupera i dati real-time.

        Se Zeus API e' disponibile, arricchisce i dati con per-MPPT completi.
        Altrimenti usa solo l'API pubblica (se configurata).

        Returns:
            Dizionario con feature dell'impianto + dati raw inverter, oppure None.
        """
        # Prova Zeus API per dati per-MPPT completi
        zeus_inverters = None
        zeus_mppt_data = None
        if self._zeus_available and self._zeus_client:
            try:
                zeus_inverters, zeus_mppt_data = self._zeus_client.extract_mppt_flat(
                    self.active_inverter_sns
                )
                if zeus_inverters:
                    logger.debug(f"Zeus: dati da {len(zeus_inverters)} inverter")
            except Exception as e:
                logger.warning(f"Zeus API fallita, fallback a API pubblica: {e}")
                zeus_inverters = None

        # Modalita' solo-Zeus: se non c'e' API pubblica configurata
        if not self.api_url:
            if zeus_inverters:
                features = self._extract_plant_features(zeus_inverters)
                features["_raw_inverters"] = zeus_inverters
                features["_zeus_mppt_data"] = zeus_mppt_data
                features["_data_source"] = "zeus"
                return features
            return None

        # API pubblica come fonte primaria o fallback
        try:
            response = self.session.get(
                f"{self.api_url}/api/realtime",
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()

            if not data.get("success") or not isinstance(data.get("result"), list):
                logger.warning(f"Risposta API non valida: {data.get('exception', 'N/A')}")
                # Se abbiamo dati Zeus, usiamo quelli
                if zeus_inverters:
                    features = self._extract_plant_features(zeus_inverters)
                    features["_raw_inverters"] = zeus_inverters
                    features["_zeus_mppt_data"] = zeus_mppt_data
                    features["_data_source"] = "zeus"
                    return features
                return None

            inverters = data["result"]

            # Arricchisci con dati Zeus (per-MPPT completi)
            if zeus_inverters:
                inverters = self._merge_zeus_data(inverters, zeus_inverters)

            features = self._extract_plant_features(inverters)
            features["_raw_inverters"] = inverters
            if zeus_mppt_data:
                features["_zeus_mppt_data"] = zeus_mppt_data
                features["_data_source"] = "zeus+public"
            else:
                features["_data_source"] = "public"
            return features

        except requests.exceptions.RequestException as e:
            logger.error(f"Errore fetch realtime: {e}")
            # Fallback puro Zeus se API pubblica non disponibile
            if zeus_inverters:
                features = self._extract_plant_features(zeus_inverters)
                features["_raw_inverters"] = zeus_inverters
                features["_zeus_mppt_data"] = zeus_mppt_data
                features["_data_source"] = "zeus"
                return features
            return None

    def _merge_zeus_data(
        self, public_inverters: list, zeus_inverters: list
    ) -> list:
        """
        Unisce dati API pubblica con dati Zeus (per-MPPT completi).

        I dati Zeus hanno powerdc1..12, vdc1..12 per ogni inverter.
        L'API pubblica ha feedinpower e altri campi non presenti in Zeus.
        """
        zeus_by_sn = {inv["inverterSN"]: inv for inv in zeus_inverters}
        merged = []
        for pub_inv in public_inverters:
            sn = pub_inv.get("inverterSN", "")
            zeus_inv = zeus_by_sn.get(sn)
            if zeus_inv:
                combined = dict(pub_inv)
                # Zeus ha dati per-MPPT superiori (powerdc1..12, vdc1..12)
                for key, val in zeus_inv.items():
                    if key.startswith(("powerdc", "vdc", "idc")):
                        combined[key] = val
                    elif key == "acpower" and val > 0:
                        combined[key] = val
                merged.append(combined)
            else:
                merged.append(pub_inv)
        # Aggiungi inverter solo in Zeus (non presenti in API pubblica)
        pub_sns = {inv.get("inverterSN", "") for inv in public_inverters}
        for zeus_inv in zeus_inverters:
            if zeus_inv["inverterSN"] not in pub_sns:
                merged.append(zeus_inv)
        return merged

    def fetch_day_history(self, date: str) -> List[Dict[str, float]]:
        """
        Recupera lo storico giornaliero dall'API.

        Args:
            date: data in formato YYYY-MM-DD.

        Returns:
            Lista di campioni per la giornata.
        """
        try:
            response = self.session.get(
                f"{self.api_url}/api/history/day",
                params={"date": date},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()

            samples = []
            for point in data.get("data", []):
                sample = {
                    "timestamp": point.get("ts", ""),
                    "total_ac_power": point.get("power", 0) * 1000,  # kW -> W
                    "total_yield_today": point.get("yield", 0),
                    "total_feedin": point.get("feedIn", 0) * 1000,
                }
                # Dati per-inverter non disponibili nell'endpoint aggregato
                sample["total_dc1_power"] = 0
                sample["total_dc2_power"] = 0
                sample["active_inverters"] = len(point.get("inverters", []))
                sample["hybrid_soc"] = 0
                sample["hybrid_bat_power"] = 0
                samples.append(sample)

            return samples

        except requests.exceptions.RequestException as e:
            logger.error(f"Errore fetch storico {date}: {e}")
            return []

    def _extract_plant_features(self, inverters: list) -> Dict[str, float]:
        """Estrae le feature aggregate dell'impianto dalla lista inverter."""
        total_ac = 0
        total_dc1 = 0
        total_dc2 = 0
        total_yield = 0
        total_feedin = 0
        active = 0
        hybrid_soc = 0
        hybrid_bat = 0

        for inv in inverters:
            ac = float(inv.get("acpower") or 0)
            total_ac += ac
            total_dc1 += float(inv.get("powerdc1") or 0)
            total_dc2 += float(inv.get("powerdc2") or 0)
            total_yield += float(inv.get("yieldtoday") or 0)
            total_feedin += float(inv.get("feedinpower") or 0)

            if ac > 0:
                active += 1

            # Inverter Hybrid (type 14) ha la batteria
            if str(inv.get("inverterType")) == "14":
                hybrid_soc = float(inv.get("soc") or 0)
                hybrid_bat = float(inv.get("batPower") or 0)

        features = {
            "timestamp": datetime.now().isoformat(),
            "total_ac_power": total_ac,
            "total_dc1_power": total_dc1,
            "total_dc2_power": total_dc2,
            "total_yield_today": total_yield,
            "total_feedin": total_feedin,
            "active_inverters": float(active),
            "hybrid_soc": hybrid_soc,
            "hybrid_bat_power": hybrid_bat,
        }

        return features

    def extract_per_inverter_data(
        self, inverters: list
    ) -> Dict[str, Dict[str, float]]:
        """
        Estrae dati dettagliati per ogni inverter (DC per MPPT, tensione, AC).

        Supporta sia dati dall'API pubblica (4 MPPT) sia Zeus (fino a 12 MPPT).

        Returns:
            Dict {inverter_sn: {acpower, powerdc1..N, vdc1..N, ...}}
        """
        result = {}
        for inv in inverters:
            sn = inv.get("inverterSN", "")
            if not sn:
                continue
            data = {
                "acpower": float(inv.get("acpower") or 0),
                "yieldtoday": float(inv.get("yieldtoday") or 0),
                "feedinpower": float(inv.get("feedinpower") or 0),
            }
            n_mppt = INVERTER_MPPT_COUNT.get(sn, 2)
            for i in range(n_mppt):
                pfield = DC_POWER_FIELDS[i]
                data[pfield] = float(inv.get(pfield) or 0)
                from config import DC_VOLTAGE_FIELDS
                vfield = DC_VOLTAGE_FIELDS[i]
                data[vfield] = float(inv.get(vfield) or 0)
            if str(inv.get("inverterType")) == "14":
                data["soc"] = float(inv.get("soc") or 0)
                data["batPower"] = float(inv.get("batPower") or 0)
            result[sn] = data
        return result

    def collect_sample(self) -> Optional[Dict[str, float]]:
        """Raccoglie un campione e lo salva nello storico."""
        sample = self.fetch_realtime()
        if sample is None:
            return None

        self.history.append(sample)
        self._append_to_csv(sample)
        return sample

    def _append_to_csv(self, sample: Dict[str, float]):
        """Salva un campione nel CSV storico."""
        try:
            row = [sample["timestamp"]] + [sample.get(f, 0.0) for f in PLANT_FEATURES]
            with open(HISTORY_FILE, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(row)
        except IOError as e:
            logger.error(f"Errore scrittura CSV: {e}")

    def get_feature_array(self, n_samples: Optional[int] = None) -> np.ndarray:
        """Restituisce i dati storici come array numpy."""
        if not self.history:
            return np.array([]).reshape(0, len(PLANT_FEATURES))

        data = self.history[-n_samples:] if n_samples else self.history
        matrix = [[sample.get(f, 0.0) for f in PLANT_FEATURES] for sample in data]
        return np.array(matrix, dtype=np.float32)

    def get_string_feature_array(
        self, feature_names: List[str], n_samples: Optional[int] = None
    ) -> np.ndarray:
        """
        Restituisce i dati storici per-stringa come array numpy.

        Args:
            feature_names: lista ordinata di nomi feature (da StringAnalyzer).
            n_samples: numero max di campioni.

        Returns:
            Array (n_campioni, n_feature_stringa).
        """
        if not self.history:
            return np.array([]).reshape(0, len(feature_names))

        data = self.history[-n_samples:] if n_samples else self.history
        matrix = []
        for sample in data:
            string_feats = sample.get("_string_features", {})
            row = [string_feats.get(f, 0.0) for f in feature_names]
            matrix.append(row)
        return np.array(matrix, dtype=np.float32)

    def load_history_from_csv(self) -> int:
        """Carica storico dal CSV."""
        if not os.path.exists(HISTORY_FILE):
            return 0
        try:
            import pandas as pd
            df = pd.read_csv(HISTORY_FILE)
            self.history = df.to_dict("records")
            logger.info(f"Caricati {len(self.history)} campioni dallo storico")
            return len(self.history)
        except Exception as e:
            logger.error(f"Errore caricamento storico: {e}")
            return 0

    def fetch_recent_snapshots(self, days: int = 7) -> int:
        """
        Carica snapshot storici dal backend per auto-warmup.

        Prova prima l'endpoint /api/snapshots/recent (dati grezzi completi).
        Se non disponibile, usa /api/history/day come fallback.
        Se nessun backend e' configurato (solo Zeus), ritorna 0.

        Args:
            days: numero di giorni di storico da caricare.

        Returns:
            Numero di campioni caricati.
        """
        if not self.api_url:
            logger.info("Nessun backend configurato (solo Zeus), skip storico")
            return 0
        count = self._fetch_raw_snapshots(days)
        if count > 0:
            return count
        return self._fetch_history_fallback(days)

    def _fetch_raw_snapshots(self, days: int) -> int:
        """Carica snapshot grezzi con dati inverter completi."""
        try:
            response = self.session.get(
                f"{self.api_url}/api/snapshots/recent",
                params={"days": days},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            count = 0
            for snap in data.get("snapshots", []):
                inverters = snap.get("inverters", [])
                if not inverters:
                    continue
                features = self._extract_plant_features(inverters)
                features["timestamp"] = snap.get("ts", features["timestamp"])
                features["_raw_inverters"] = inverters
                self.history.append(features)
                count += 1

            logger.info(f"Auto-warmup (raw): {count} campioni da {data.get('days', 0)} giorni")
            return count

        except Exception as e:
            logger.info(f"Endpoint raw non disponibile, uso fallback: {e}")
            return 0

    def _fetch_history_fallback(self, days: int) -> int:
        """Fallback: usa /api/history/day e /api/history/dates."""
        try:
            resp = self.session.get(f"{self.api_url}/api/history/dates", timeout=15)
            resp.raise_for_status()
            dates = resp.json().get("dates", [])
            recent_dates = dates[-days:]

            count = 0
            for date_str in recent_dates:
                resp = self.session.get(
                    f"{self.api_url}/api/history/day",
                    params={"date": date_str},
                    timeout=15,
                )
                resp.raise_for_status()
                day_data = resp.json()

                for point in day_data.get("data", []):
                    power_w = point.get("power", 0) * 1000
                    feed_w = point.get("feedIn", 0) * 1000
                    inverters = point.get("inverters", [])
                    active = sum(1 for inv in inverters if inv.get("power", 0) > 0)

                    features = {
                        "timestamp": point.get("ts", ""),
                        "total_ac_power": power_w,
                        "total_dc1_power": power_w * 0.52,
                        "total_dc2_power": power_w * 0.48,
                        "total_yield_today": point.get("yield", 0),
                        "total_feedin": feed_w,
                        "active_inverters": float(active),
                        "hybrid_soc": 50.0,
                        "hybrid_bat_power": 0.0,
                    }
                    self.history.append(features)
                    count += 1

            logger.info(f"Auto-warmup (fallback): {count} campioni da {len(recent_dates)} giorni")
            return count

        except Exception as e:
            logger.error(f"Errore fetch history fallback: {e}")
            return 0

    def get_latest_sample(self) -> Optional[Dict[str, float]]:
        """Restituisce l'ultimo campione."""
        return self.history[-1] if self.history else None

    def get_raw_inverters_history(self) -> List[List[Dict]]:
        """Restituisce lo storico dei dati raw inverter (per analisi stringhe)."""
        return [
            s.get("_raw_inverters", [])
            for s in self.history
            if s.get("_raw_inverters")
        ]

    def run_continuous(self, callback=None, interval: int = POLLING_INTERVAL):
        """Avvia raccolta continua."""
        logger.info(f"Avvio polling continuo ({interval}s) da {self.api_url}")
        try:
            while True:
                sample = self.collect_sample()
                if sample and callback:
                    callback(sample)
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Polling interrotto")
