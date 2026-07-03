# -*- coding: utf-8 -*-
"""
Analisi per-stringa dell'impianto Pascale 500kW.

Funzionalita':
1. Estrae dati DC per MPPT di ogni inverter
2. Stima il numero di moduli per stringa dalla tensione DC
3. Normalizza la potenza a W/modulo
4. Raggruppa le stringhe per orientamento tramite correlazione
5. Genera feature per il DAE per-stringa
"""

import logging
import os
import pickle
from typing import Dict, List, Optional, Tuple

import numpy as np

from config import (
    DC_POWER_FIELDS,
    DC_VOLTAGE_FIELDS,
    INVERTER_LABELS,
    INVERTER_MPPT_COUNT,
    INVERTER_SNS,
    MODULE_VMPP,
    ORIENTATION_CORR_THRESHOLD,
    STRING_MAP_FILE,
)

logger = logging.getLogger(__name__)


class StringInfo:
    """Rappresenta una stringa (MPPT input) dell'impianto."""

    def __init__(self, inverter_sn: str, mppt_index: int):
        self.inverter_sn = inverter_sn
        self.mppt_index = mppt_index
        self.n_modules: Optional[int] = None
        self.orientation_group: Optional[int] = None
        self.is_parallel: bool = False

    @property
    def label(self) -> str:
        inv_name = INVERTER_LABELS.get(self.inverter_sn, self.inverter_sn[-6:])
        return f"{inv_name} DC{self.mppt_index + 1}"

    @property
    def key(self) -> str:
        return f"{self.inverter_sn}_dc{self.mppt_index + 1}"

    def __repr__(self) -> str:
        mods = f" ({self.n_modules} mod)" if self.n_modules else ""
        grp = f" grp={self.orientation_group}" if self.orientation_group is not None else ""
        return f"<String {self.label}{mods}{grp}>"


class StringAnalyzer:
    """Analizza e raggruppa le stringhe dell'impianto."""

    def __init__(self):
        self.strings: List[StringInfo] = []
        self.orientation_groups: Dict[int, List[StringInfo]] = {}
        self.is_configured = False
        self._build_string_list()
        self._load_string_map()

    def _build_string_list(self):
        """Crea la lista di tutte le stringhe dall'inventario inverter."""
        self.strings = []
        for sn in INVERTER_SNS:
            n_mppt = INVERTER_MPPT_COUNT.get(sn, 2)
            for i in range(n_mppt):
                self.strings.append(StringInfo(sn, i))
        logger.info(f"Inventario stringhe: {len(self.strings)} MPPT totali")

    def _load_string_map(self):
        """Carica configurazione stringhe (n_moduli, gruppi) da disco."""
        if not os.path.exists(STRING_MAP_FILE):
            return
        try:
            with open(STRING_MAP_FILE, "rb") as f:
                data = pickle.load(f)
            for s in self.strings:
                info = data.get(s.key, {})
                s.n_modules = info.get("n_modules")
                s.orientation_group = info.get("orientation_group")
                s.is_parallel = info.get("is_parallel", False)
            self.orientation_groups = data.get("_groups", {})
            self.is_configured = any(s.n_modules is not None for s in self.strings)
            if self.is_configured:
                logger.info("Configurazione stringhe caricata da disco")
        except Exception as e:
            logger.error(f"Errore caricamento string_map: {e}")

    def _save_string_map(self):
        """Salva configurazione stringhe su disco."""
        data = {}
        for s in self.strings:
            data[s.key] = {
                "n_modules": s.n_modules,
                "orientation_group": s.orientation_group,
                "is_parallel": s.is_parallel,
            }
        data["_groups"] = self.orientation_groups
        os.makedirs(os.path.dirname(STRING_MAP_FILE), exist_ok=True)
        with open(STRING_MAP_FILE, "wb") as f:
            pickle.dump(data, f)
        logger.info("Configurazione stringhe salvata")

    def estimate_modules_from_voltage(
        self, inverter_data: List[Dict]
    ) -> Dict[str, int]:
        """
        Stima il numero di moduli per stringa dalla tensione DC.

        Args:
            inverter_data: lista di oggetti inverter dall'API SolaX
                           (deve contenere campi vdc1..vdc4)

        Returns:
            Dict con chiave string_key e valore n_moduli stimato.
        """
        results: Dict[str, int] = {}
        for inv in inverter_data:
            sn = inv.get("inverterSN", "")
            if sn not in INVERTER_MPPT_COUNT:
                continue
            n_mppt = INVERTER_MPPT_COUNT[sn]
            for i in range(n_mppt):
                vdc_field = DC_VOLTAGE_FIELDS[i]
                vdc = float(inv.get(vdc_field) or 0)
                if vdc < MODULE_VMPP * 0.5:
                    continue
                n_mod = round(vdc / MODULE_VMPP)
                if n_mod < 1:
                    continue
                key = f"{sn}_dc{i + 1}"
                results[key] = n_mod
        return results

    def estimate_modules_from_power_ratios(
        self, history: List[Dict]
    ) -> Dict[str, int]:
        """
        Stima il numero di moduli confrontando le potenze DC tra stringhe.

        Usa i campioni a massima produzione per stimare la potenza nominale
        di ogni stringa e da li' derivare il numero di moduli.

        Args:
            history: lista di campioni con dati per-inverter raw.

        Returns:
            Dict con chiave string_key e valore n_moduli stimato.
        """
        if len(history) < 10:
            return {}

        peak_powers: Dict[str, List[float]] = {}
        for sample in history:
            inverters = sample.get("_raw_inverters", [])
            for inv in inverters:
                sn = inv.get("inverterSN", "")
                if sn not in INVERTER_MPPT_COUNT:
                    continue
                n_mppt = INVERTER_MPPT_COUNT[sn]
                for i in range(n_mppt):
                    pdc = float(inv.get(DC_POWER_FIELDS[i]) or 0)
                    if pdc > 100:
                        key = f"{sn}_dc{i + 1}"
                        peak_powers.setdefault(key, []).append(pdc)

        results: Dict[str, int] = {}
        for key, powers in peak_powers.items():
            if len(powers) < 5:
                continue
            top_5pct = sorted(powers, reverse=True)[:max(1, len(powers) // 20)]
            peak_w = float(np.mean(top_5pct))
            from config import MODULE_PNOM
            n_mod = round(peak_w / (MODULE_PNOM * 0.85))
            if n_mod >= 1:
                results[key] = n_mod
        return results

    def configure_strings(
        self, inverter_data: Optional[List[Dict]] = None,
        history: Optional[List[Dict]] = None,
    ):
        """
        Configura le stringhe: stima n_moduli e rileva stringhe parallele.

        Prova prima con la tensione (preciso), poi con i rapporti di
        potenza (approssimato).
        """
        module_counts: Dict[str, int] = {}

        if inverter_data:
            module_counts = self.estimate_modules_from_voltage(inverter_data)

        if not module_counts and history:
            module_counts = self.estimate_modules_from_power_ratios(history)

        if not module_counts:
            logger.warning("Impossibile stimare n_moduli — dati insufficienti")
            return

        for s in self.strings:
            n_mod = module_counts.get(s.key)
            if n_mod is not None:
                s.n_modules = n_mod

        self._detect_parallel_strings()
        self.is_configured = True
        self._save_string_map()

        summary = [(s.label, s.n_modules) for s in self.strings if s.n_modules]
        logger.info(f"Stringhe configurate: {summary}")

    def _detect_parallel_strings(self):
        """Rileva stringhe parallele (n_moduli anomalmente alto per un singolo MPPT)."""
        known_counts = [s.n_modules for s in self.strings if s.n_modules]
        if not known_counts:
            return
        median_count = int(np.median(known_counts))
        for s in self.strings:
            if s.n_modules and s.n_modules > median_count * 1.7:
                s.is_parallel = True
                logger.info(f"{s.label}: probabile parallelo ({s.n_modules} mod vs mediana {median_count})")

    def detect_orientation_groups(self, history: List[Dict]) -> Dict[int, List[str]]:
        """
        Raggruppa le stringhe per orientamento tramite correlazione temporale.

        Stringhe con lo stesso orientamento producono profili giornalieri
        simili (picco alla stessa ora, stessa curva). Usa la matrice di
        correlazione per raggrupparle.

        Args:
            history: lista di campioni con dati per-inverter raw.

        Returns:
            Dict group_id -> lista di string_keys.
        """
        active_strings = [s for s in self.strings if s.n_modules]
        if len(active_strings) < 2 or len(history) < 20:
            return {}

        power_series: Dict[str, List[float]] = {s.key: [] for s in active_strings}
        for sample in history:
            inverters = sample.get("_raw_inverters", [])
            inv_by_sn = {inv.get("inverterSN", ""): inv for inv in inverters}
            for s in active_strings:
                inv = inv_by_sn.get(s.inverter_sn, {})
                pdc = float(inv.get(DC_POWER_FIELDS[s.mppt_index]) or 0)
                n_mod = s.n_modules or 1
                power_series[s.key].append(pdc / n_mod)

        keys = [s.key for s in active_strings]
        min_len = min(len(power_series[k]) for k in keys)
        if min_len < 20:
            return {}

        matrix = np.array([power_series[k][:min_len] for k in keys])

        # Filtra campioni notturni (bassa produzione)
        col_means = np.mean(matrix, axis=0)
        daylight_mask = col_means > np.max(col_means) * 0.05
        if np.sum(daylight_mask) < 10:
            return {}
        matrix = matrix[:, daylight_mask]

        corr = np.corrcoef(matrix)

        # Clustering gerarchico semplificato
        assigned = [False] * len(keys)
        groups: Dict[int, List[str]] = {}
        group_id = 0

        for i in range(len(keys)):
            if assigned[i]:
                continue
            group = [keys[i]]
            assigned[i] = True
            for j in range(i + 1, len(keys)):
                if assigned[j]:
                    continue
                if corr[i, j] >= ORIENTATION_CORR_THRESHOLD:
                    group.append(keys[j])
                    assigned[j] = True
            groups[group_id] = group
            group_id += 1

        # Assegna ai StringInfo
        self.orientation_groups = groups
        for gid, group_keys in groups.items():
            for s in self.strings:
                if s.key in group_keys:
                    s.orientation_group = gid

        self._save_string_map()
        for gid, gkeys in groups.items():
            labels = [next(s.label for s in self.strings if s.key == k) for k in gkeys]
            logger.info(f"Orientamento gruppo {gid}: {labels}")

        return groups

    def extract_string_features(
        self, inverter_data: List[Dict]
    ) -> Dict[str, float]:
        """
        Estrae feature per-stringa da un singolo campione.

        Returns:
            Dict con tutte le feature per-stringa:
            - {string_key}_wpmod: W/modulo per ogni stringa
            - {inv_sn}_dc_ac_ratio: rapporto DC/AC per inverter
            - group_{gid}_cv: coefficiente variazione per gruppo
            - group_{gid}_mean: potenza media W/modulo per gruppo
        """
        features: Dict[str, float] = {}
        inv_by_sn = {inv.get("inverterSN", ""): inv for inv in inverter_data}

        for s in self.strings:
            inv = inv_by_sn.get(s.inverter_sn, {})
            pdc = float(inv.get(DC_POWER_FIELDS[s.mppt_index]) or 0)
            n_mod = s.n_modules or 1
            wpmod = pdc / n_mod if n_mod > 0 else 0.0
            features[f"{s.key}_wpmod"] = wpmod

        # Rapporto DC/AC per inverter
        for sn in INVERTER_SNS:
            inv = inv_by_sn.get(sn, {})
            ac = float(inv.get("acpower") or 0)
            n_mppt = INVERTER_MPPT_COUNT.get(sn, 2)
            total_dc = sum(
                float(inv.get(DC_POWER_FIELDS[i]) or 0) for i in range(n_mppt)
            )
            ratio = total_dc / ac if ac > 10 else 0.0
            features[f"{sn}_dc_ac_ratio"] = ratio

        # Coefficiente di variazione per gruppo orientamento
        for gid, group_keys in self.orientation_groups.items():
            values = [features.get(f"{k}_wpmod", 0.0) for k in group_keys]
            values = [v for v in values if v > 0]
            if len(values) >= 2:
                mean_v = float(np.mean(values))
                std_v = float(np.std(values))
                cv = std_v / mean_v if mean_v > 0 else 0.0
                features[f"group_{gid}_cv"] = cv
                features[f"group_{gid}_mean"] = mean_v
            elif len(values) == 1:
                features[f"group_{gid}_cv"] = 0.0
                features[f"group_{gid}_mean"] = values[0]
            else:
                features[f"group_{gid}_cv"] = 0.0
                features[f"group_{gid}_mean"] = 0.0

        return features

    def get_feature_names(self) -> List[str]:
        """Restituisce la lista ordinata dei nomi delle feature per-stringa."""
        names = []

        for s in self.strings:
            if s.n_modules is not None:
                names.append(f"{s.key}_wpmod")

        for sn in INVERTER_SNS:
            names.append(f"{sn}_dc_ac_ratio")

        for gid in sorted(self.orientation_groups.keys()):
            names.append(f"group_{gid}_cv")
            names.append(f"group_{gid}_mean")

        return names

    def identify_anomalous_strings(
        self,
        features: Dict[str, float],
        mse_per_feature: Optional[Dict[str, float]] = None,
    ) -> List[Dict]:
        """
        Identifica quali stringhe si discostano dal loro gruppo.

        Returns:
            Lista di anomalie: [{string_label, group, wpmod, group_mean, deviation_pct}]
        """
        anomalies = []
        for gid, group_keys in self.orientation_groups.items():
            values = {k: features.get(f"{k}_wpmod", 0.0) for k in group_keys}
            active = {k: v for k, v in values.items() if v > 0}
            if len(active) < 2:
                continue
            mean_v = float(np.mean(list(active.values())))
            if mean_v < 1:
                continue
            for k, v in active.items():
                dev_pct = (v - mean_v) / mean_v * 100
                if abs(dev_pct) > 15:
                    s = next(s for s in self.strings if s.key == k)
                    anomalies.append({
                        "string_label": s.label,
                        "string_key": k,
                        "group_id": gid,
                        "wpmod": v,
                        "group_mean": mean_v,
                        "deviation_pct": dev_pct,
                    })
        return anomalies

    def get_status(self) -> Dict:
        """Stato dell'analizzatore stringhe."""
        configured = [s for s in self.strings if s.n_modules is not None]
        return {
            "total_strings": len(self.strings),
            "configured_strings": len(configured),
            "orientation_groups": len(self.orientation_groups),
            "is_configured": self.is_configured,
            "strings": [
                {
                    "label": s.label,
                    "key": s.key,
                    "n_modules": s.n_modules,
                    "orientation_group": s.orientation_group,
                    "is_parallel": s.is_parallel,
                }
                for s in self.strings
            ],
            "groups": {
                gid: [next(s.label for s in self.strings if s.key == k) for k in keys]
                for gid, keys in self.orientation_groups.items()
            },
        }
