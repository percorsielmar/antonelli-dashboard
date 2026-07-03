# -*- coding: utf-8 -*-
"""
Dashboard Streamlit per anomaly detection impianto Pascale 500kW.

Mostra:
1. Parametri FV attuali (da API pascale-dashboard)
2. Grafico andamento MSE nel tempo
3. Indicatore Verde/Rosso per anomalie
4. Analisi per-stringa con W/modulo normalizzato
5. Mappa bottleneck 2D / correlazione orientamento
"""

import os
import sys
import time
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    INVERTER_LABELS,
    PASCALE_API_URL,
    PLANT_FEATURES,
    PLANT_NAME,
    POLLING_INTERVAL,
)
from anomaly_detector import AnomalyDetector
import db_persistence as db
import background_poller as poller

# ===================== PAGINA =====================

st.set_page_config(
    page_title=f"{PLANT_NAME} - Anomaly Detection DAE-O",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ===================== STATO SESSIONE =====================
# Usa il detector condiviso dal background poller (singleton)
if "detector" not in st.session_state:
    st.session_state.detector = poller.get_detector()
if "last_result" not in st.session_state:
    st.session_state.last_result = {}
if "last_string_result" not in st.session_state:
    st.session_state.last_string_result = {}
if "warmup_done" not in st.session_state:
    st.session_state.warmup_done = False


def auto_warmup():
    """Auto-inizializzazione: carica dati storici e addestra il modello."""
    detector = st.session_state.detector

    if detector.is_ready:
        # Modello caricato dal DB — carica campioni storici se pochi
        if len(detector.fetcher.history) < 50:
            with st.spinner("Caricamento campioni storici..."):
                n_loaded = detector.fetcher.fetch_recent_snapshots(days=7)
                if n_loaded > 0 and detector.db_available:
                    db.save_samples_batch(detector.fetcher.history[-n_loaded:])
        st.session_state.warmup_done = True
        return

    with st.spinner("Auto-inizializzazione: caricamento dati storici..."):
        n_loaded = detector.fetcher.fetch_recent_snapshots(days=7)

        if n_loaded > 0 and detector.db_available:
            db.save_samples_batch(detector.fetcher.history[-n_loaded:])

        if n_loaded < 50:
            sample = detector.fetcher.fetch_realtime()
            if sample:
                for _ in range(max(0, 50 - n_loaded)):
                    detector.fetcher.history.append(sample)
                n_loaded = len(detector.fetcher.history)

        if n_loaded >= 50:
            X = detector.fetcher.get_feature_array()
            n_samples = min(len(X), 500)
            X_train = X[-n_samples:]
            epochs = 100 if n_samples > 200 else 200
            metrics = detector.train_model(X_train=X_train, epochs=epochs)
            mse = metrics.get("train_mse_mean", 0)
            st.success(f"Auto-warmup: {n_samples} campioni, MSE medio: {mse:.4f}")
        else:
            st.warning(f"Solo {n_loaded} campioni. Clicca 'Fetch Dati Live' per raccogliere dati.")

    st.session_state.warmup_done = True


if not st.session_state.warmup_done:
    auto_warmup()

# Avvia background polling dopo warmup (se modello pronto)
if st.session_state.detector.is_ready:
    poller.start_polling()


def generate_demo_data():
    """Genera dati demo simulando l'impianto Pascale."""
    np.random.seed(42)
    n = 200
    data = {
        "total_ac_power": np.random.normal(350000, 80000, n).clip(0),  # ~350kW
        "total_dc1_power": np.random.normal(180000, 40000, n).clip(0),
        "total_dc2_power": np.random.normal(170000, 40000, n).clip(0),
        "total_yield_today": np.cumsum(np.random.uniform(5, 15, n)),
        "total_feedin": np.random.normal(100000, 50000, n),
        "active_inverters": np.random.choice([5, 6, 6, 6, 6, 6], n).astype(float),
        "hybrid_soc": np.random.normal(60, 15, n).clip(10, 100),
        "hybrid_bat_power": np.random.normal(2000, 3000, n),
    }

    # Inserisci anomalie (calo produzione)
    for idx in [50, 51, 120, 121, 122, 180]:
        data["total_ac_power"][idx] = 50000  # Calo a 50kW
        data["active_inverters"][idx] = 2.0  # Solo 2 inverter attivi

    fetcher = st.session_state.detector.fetcher
    for i in range(n):
        sample = {"timestamp": datetime.now().isoformat()}
        for feat in PLANT_FEATURES:
            sample[feat] = float(data[feat][i])
        fetcher.history.append(sample)

    st.success(f"Caricati {n} campioni demo con anomalie simulate")
    st.rerun()


# ===================== SIDEBAR =====================

with st.sidebar:
    st.title("DAE-O Config")
    if PASCALE_API_URL:
        st.caption(f"API: {PASCALE_API_URL}")
    else:
        st.caption("Modalità: solo Zeus API")

    # Zeus API status
    fetcher = st.session_state.detector.fetcher
    if fetcher._zeus_available:
        st.success("Zeus API: attiva (per-MPPT completi)")
    else:
        st.info("Zeus API: non disponibile (solo API pubblica)")
    st.markdown("---")

    status = st.session_state.detector.get_status()
    if status["is_ready"]:
        st.success("Modello aggregato attivo")
    else:
        st.warning("Modello aggregato non addestrato")

    if status.get("string_model_ready"):
        st.success("Modello per-stringa attivo")
    else:
        st.info("Modello per-stringa non addestrato")

    st.metric("Campioni storici", status["history_samples"])
    st.metric("Campioni processati", status["total_samples"])
    st.metric("Anomalie impianto", status["total_anomalies"])
    st.metric("Anomalie stringa", status.get("string_total_anomalies", 0))

    st.markdown("---")

    if st.button("Addestra Modello Aggregato", use_container_width=True):
        X = st.session_state.detector.fetcher.get_feature_array()
        if len(X) >= 50:
            n_samples = min(len(X), 500)
            X_train = X[-n_samples:]
            epochs = 100 if n_samples > 200 else 200
            with st.spinner(f"Training DAE-O ({n_samples} campioni, {epochs} epoche)..."):
                metrics = st.session_state.detector.train_model(
                    X_train=X_train, epochs=epochs, batch_size=64
                )
                st.success(f"MSE medio: {metrics.get('train_mse_mean', 0):.6f}")
        else:
            st.error(f"Servono almeno 50 campioni ({len(X)} disponibili)")

    if st.button("Configura Stringhe", use_container_width=True):
        with st.spinner("Configurazione stringhe in corso..."):
            st.session_state.detector.configure_strings()
            sa_status = st.session_state.detector.string_analyzer.get_status()
            st.success(
                f"Configurate {sa_status['configured_strings']} stringhe "
                f"in {sa_status['orientation_groups']} gruppi"
            )

    if st.button("Addestra Modello Per-Stringa", use_container_width=True):
        with st.spinner("Training DAE per-stringa..."):
            metrics = st.session_state.detector.train_string_model()
            if "error" in metrics:
                st.error(metrics["error"])
            else:
                st.success(f"MSE medio: {metrics.get('train_mse_mean', 0):.6f}")

    st.markdown("---")

    if st.button("Fetch Dati Live", use_container_width=True):
        sample = st.session_state.detector.fetcher.collect_sample()
        if sample:
            if st.session_state.detector.is_ready:
                result = st.session_state.detector.process_sample(sample)
                st.session_state.last_result = result
            if st.session_state.detector.string_model_ready:
                str_result = st.session_state.detector.process_sample_strings(sample)
                st.session_state.last_string_result = str_result
            st.success("Campione raccolto!")
            st.rerun()
        else:
            st.error("Errore connessione API")

    if st.button("Carica Demo", use_container_width=True):
        generate_demo_data()

    if st.button("Reset Dati", use_container_width=True):
        poller.stop_polling()
        st.session_state.detector = AnomalyDetector()
        st.session_state.last_result = {}
        st.session_state.last_string_result = {}
        st.session_state.warmup_done = False
        st.rerun()

    st.markdown("---")
    st.caption("Polling Server-Side (automatico)")
    poll_status = poller.get_poll_status()
    if poll_status["running"]:
        st.success(f"Attivo - {poll_status['poll_count']} campioni raccolti")
        if poll_status["last_poll"]:
            st.caption(f"Ultimo: {poll_status['last_poll']}")
    else:
        st.info("In attesa (modello non pronto)")

    st.markdown("---")
    st.caption("Polling manuale (browser)")
    auto_poll = st.checkbox("Attiva polling browser", value=False)

# ===================== MAIN =====================

st.title(f"{PLANT_NAME} — Anomaly Detection (DAE-O)")

# Tab layout
tab_plant, tab_strings, tab_latent = st.tabs([
    "Impianto Aggregato",
    "Analisi Per-Stringa",
    "Spazio Latente",
])

# ===================== TAB 1: IMPIANTO AGGREGATO =====================

with tab_plant:
    st.caption("Rilevamento anomalie con Deep Autoencoder su dati aggregati dell'impianto")

    # --- INDICATORI STATO ---
    col_status, col_power, col_yield, col_inv, col_batt = st.columns([1, 1, 1, 1, 1])

    last_sample = st.session_state.detector.fetcher.get_latest_sample()
    last_result = st.session_state.last_result

    with col_status:
        is_anomaly = last_result.get("is_anomaly", False)
        color_bg = "#ffcccc" if is_anomaly else "#ccffcc"
        color_border = "#ff0000" if is_anomaly else "#00aa00"
        icon = "🔴" if is_anomaly else "🟢"
        label = "ANOMALIA" if is_anomaly else "NORMALE"
        st.markdown(
            f'<div style="text-align:center;padding:10px;background:{color_bg};'
            f'border-radius:10px;border:2px solid {color_border}">'
            f'<h1 style="margin:0">{icon}</h1>'
            f'<h4 style="margin:5px 0">{label}</h4>'
            f'<p style="margin:0;font-size:0.8em">Stato Impianto</p></div>',
            unsafe_allow_html=True,
        )

    with col_power:
        power = last_sample.get("total_ac_power", 0) / 1000 if last_sample else 0
        st.metric("Potenza Totale", f"{power:.1f} kW")
        st.caption("500 kWp nominali")

    with col_yield:
        yld = last_sample.get("total_yield_today", 0) if last_sample else 0
        st.metric("Produzione Oggi", f"{yld:.1f} kWh")

    with col_inv:
        active = int(last_sample.get("active_inverters", 0)) if last_sample else 0
        st.metric("Inverter Attivi", f"{active} / 6")

    with col_batt:
        soc = last_sample.get("hybrid_soc", 0) if last_sample else 0
        bat = last_sample.get("hybrid_bat_power", 0) if last_sample else 0
        bat_status = "Carica" if bat < 0 else "Scarica" if bat > 0 else "Idle"
        st.metric("Batteria", f"{soc:.0f}%")
        st.caption(f"{bat_status}: {abs(bat)/1000:.1f} kW")

    st.markdown("---")

    # --- GRAFICO MSE ---
    col_chart, col_detail = st.columns([3, 1])

    with col_chart:
        st.subheader("MSE — Errore di Ricostruzione Autoencoder")

        mse_data = st.session_state.detector.get_mse_history(300)
        threshold = st.session_state.detector.threshold

        if mse_data:
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                y=mse_data, mode="lines", name="MSE",
                line=dict(color="#1f77b4", width=2),
            ))
            if threshold > 0:
                fig.add_hline(
                    y=threshold, line_dash="dash", line_color="red",
                    annotation_text=f"Soglia ({threshold:.4f})",
                )
                anomaly_pts = [i for i, m in enumerate(mse_data) if m > threshold]
                if anomaly_pts:
                    fig.add_trace(go.Scatter(
                        x=anomaly_pts,
                        y=[mse_data[i] for i in anomaly_pts],
                        mode="markers", name="Anomalie",
                        marker=dict(color="red", size=8, symbol="x"),
                    ))

            fig.update_layout(
                xaxis_title="Campione", yaxis_title="MSE", height=400,
                margin=dict(l=50, r=20, t=30, b=50),
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Raccogli dati e addestra il modello per visualizzare l'MSE.")

    with col_detail:
        st.subheader("Stato")
        if last_result and last_result.get("status") == "active":
            st.metric("MSE", f"{last_result['mse']:.6f}")
            st.metric("Soglia", f"{last_result['threshold']:.6f}")
            st.metric("Score", f"{last_result['anomaly_score']:.2f}x")
        else:
            st.info("In attesa...")

        st.markdown("---")
        st.subheader("Alert")
        alerts = st.session_state.detector.get_anomaly_log(5)
        if alerts:
            for a in reversed(alerts):
                st.error(f"**{a['timestamp'][:19]}** — {a['score']:.2f}x")
        else:
            st.success("Nessuna anomalia")

    # --- PARAMETRI NEL TEMPO ---
    st.markdown("---")
    st.subheader("Parametri Impianto")

    history = st.session_state.detector.fetcher.history
    if history and len(history) > 1:
        df = pd.DataFrame(history[-300:])
        features_sel = st.multiselect(
            "Parametri", PLANT_FEATURES,
            default=["total_ac_power", "active_inverters"],
        )
        if features_sel:
            fig2 = go.Figure()
            for f in features_sel:
                if f in df.columns:
                    fig2.add_trace(go.Scatter(y=df[f].values, mode="lines", name=f))
            fig2.update_layout(height=300, margin=dict(l=50, r=20, t=30, b=50))
            st.plotly_chart(fig2, use_container_width=True)

# ===================== TAB 2: ANALISI PER-STRINGA =====================

with tab_strings:
    st.caption("Analisi per-stringa con normalizzazione W/modulo e raggruppamento orientamento")

    detector = st.session_state.detector
    sa = detector.string_analyzer
    sa_status = sa.get_status()

    # Show data source
    last_sample = detector.fetcher.get_latest_sample()
    data_source = "N/A"
    if last_sample:
        data_source = last_sample.get("_data_source", "public")
    if data_source in ("zeus", "zeus+public"):
        st.success(f"Fonte dati: **Zeus API** (per-MPPT completi, fino a 12 MPPT/inverter)")
    else:
        st.info(f"Fonte dati: **API pubblica** (limitata a powerdc1..4)")

    if not sa_status["is_configured"]:
        st.warning(
            "Stringhe non ancora configurate. "
            "Clicca 'Configura Stringhe' nella sidebar dopo aver raccolto dati con _raw_inverters."
        )
    else:
        # Configurazione stringhe
        st.subheader("Configurazione Stringhe")
        col_config, col_groups = st.columns(2)

        with col_config:
            strings_df = pd.DataFrame(sa_status["strings"])
            if not strings_df.empty:
                strings_df = strings_df[strings_df["n_modules"].notna()]
                strings_df = strings_df[["label", "n_modules", "orientation_group", "is_parallel"]]
                strings_df.columns = ["Stringa", "N. Moduli", "Gruppo Orient.", "Parallela"]
                st.dataframe(strings_df, use_container_width=True, hide_index=True)

        with col_groups:
            groups = sa_status.get("groups", {})
            if groups:
                st.write("**Gruppi di orientamento** (auto-rilevati per correlazione)")
                for gid, labels in groups.items():
                    st.write(f"**Gruppo {gid}**: {', '.join(labels)}")
            else:
                st.info("Servono almeno 20 campioni per rilevare gli orientamenti.")

        st.markdown("---")

        # MSE per-stringa
        st.subheader("MSE Per-Stringa")

        str_mse_data = detector.get_string_mse_history(300)
        if str_mse_data:
            fig_str = go.Figure()
            fig_str.add_trace(go.Scatter(
                y=str_mse_data, mode="lines", name="MSE Stringa",
                line=dict(color="#ff7f0e", width=2),
            ))
            if detector.string_threshold > 0:
                fig_str.add_hline(
                    y=detector.string_threshold, line_dash="dash", line_color="red",
                    annotation_text=f"Soglia ({detector.string_threshold:.4f})",
                )
            fig_str.update_layout(
                xaxis_title="Campione", yaxis_title="MSE", height=350,
                margin=dict(l=50, r=20, t=30, b=50),
            )
            st.plotly_chart(fig_str, use_container_width=True)
        elif not detector.string_model_ready:
            st.info("Addestra il modello per-stringa per visualizzare l'MSE.")

        # Anomalie per-stringa
        st.subheader("Anomalie Per-Stringa")
        last_str = st.session_state.last_string_result
        if last_str and last_str.get("status") == "active":
            col_m1, col_m2, col_m3 = st.columns(3)
            with col_m1:
                st.metric("MSE Stringa", f"{last_str['mse']:.6f}")
            with col_m2:
                st.metric("Soglia", f"{last_str['threshold']:.6f}")
            with col_m3:
                st.metric("Score", f"{last_str['anomaly_score']:.2f}x")

            anom_strings = last_str.get("anomalous_strings", [])
            if anom_strings:
                st.error(f"**{len(anom_strings)} stringa/e anomala/e rilevata/e:**")
                for a in anom_strings:
                    dev = a["deviation_pct"]
                    direction = "sopra" if dev > 0 else "sotto"
                    st.warning(
                        f"**{a['string_label']}**: {abs(dev):.1f}% {direction} la media del gruppo "
                        f"({a['wpmod']:.1f} W/mod vs {a['group_mean']:.1f} W/mod media)"
                    )
            else:
                st.success("Tutte le stringhe nella norma")

            # Heatmap errore per feature
            per_feat = last_str.get("per_feature_mse", {})
            if per_feat:
                st.subheader("Errore di Ricostruzione Per Feature")
                wpmod_feats = {
                    k: v for k, v in per_feat.items() if k.endswith("_wpmod")
                }
                if wpmod_feats:
                    names = list(wpmod_feats.keys())
                    values = [wpmod_feats[n] for n in names]
                    short_names = []
                    for n in names:
                        parts = n.replace("_wpmod", "").split("_dc")
                        sn = parts[0] if len(parts) > 0 else n
                        dc = f"DC{parts[1]}" if len(parts) > 1 else ""
                        inv_label = INVERTER_LABELS.get(sn, sn[-8:])
                        short_names.append(f"{inv_label} {dc}")

                    fig_feat = go.Figure(go.Bar(
                        x=short_names, y=values,
                        marker_color=[
                            "#ef4444" if v > np.mean(values) + 2 * np.std(values) else "#3b82f6"
                            for v in values
                        ],
                    ))
                    fig_feat.update_layout(
                        xaxis_title="Stringa", yaxis_title="MSE Feature",
                        height=350, margin=dict(l=50, r=20, t=30, b=80),
                        xaxis_tickangle=-45,
                    )
                    st.plotly_chart(fig_feat, use_container_width=True)
        else:
            st.info("Raccogli un campione live per vedere l'analisi per-stringa.")

        # Log anomalie stringa
        str_alerts = detector.get_string_anomaly_log(10)
        if str_alerts:
            st.subheader("Storico Anomalie Stringa")
            for a in reversed(str_alerts):
                ts = a.get("timestamp", "")[:19]
                astrings = a.get("anomalous_strings", [])
                labels = ", ".join(s["string_label"] for s in astrings)
                st.error(f"**{ts}** — {labels} (score {a.get('score', 0):.2f}x)")

# ===================== TAB 3: SPAZIO LATENTE =====================

with tab_latent:
    st.caption("Proiezione dei campioni nello spazio ridotto del bottleneck dell'autoencoder")

    detector = st.session_state.detector
    if not detector.is_ready:
        st.info("Addestra il modello aggregato per visualizzare lo spazio latente.")
    else:
        history = detector.fetcher.history
        if len(history) < 10:
            st.warning("Servono almeno 10 campioni per la visualizzazione.")
        else:
            X = detector.fetcher.get_feature_array()
            n_show = min(len(X), 500)
            X_show = X[-n_show:]

            try:
                latent = detector.model.get_latent_representation(X_show)
                mse_values = detector.model.compute_mse(X_show)

                st.subheader("Mappa Bottleneck 2D")
                st.write(
                    "Ogni punto e' uno stato dell'impianto. "
                    "Colore = MSE (piu' rosso = piu' anomalo). "
                    "Punti isolati = anomalie."
                )

                fig_lat = go.Figure()

                # Colora per MSE
                fig_lat.add_trace(go.Scatter(
                    x=latent[:, 0],
                    y=latent[:, 1],
                    mode="markers",
                    marker=dict(
                        size=6,
                        color=mse_values,
                        colorscale="RdYlGn_r",
                        showscale=True,
                        colorbar=dict(title="MSE"),
                    ),
                    text=[
                        f"MSE: {m:.4f}<br>AC: {s.get('total_ac_power', 0)/1000:.1f}kW<br>"
                        f"Inv: {int(s.get('active_inverters', 0))}"
                        for m, s in zip(mse_values, history[-n_show:])
                    ],
                    hoverinfo="text",
                    name="Campioni",
                ))

                # Evidenzia anomalie
                anomaly_mask = mse_values > detector.threshold
                if np.any(anomaly_mask):
                    fig_lat.add_trace(go.Scatter(
                        x=latent[anomaly_mask, 0],
                        y=latent[anomaly_mask, 1],
                        mode="markers",
                        marker=dict(
                            size=12, color="red", symbol="x", line=dict(width=2, color="darkred")
                        ),
                        name="Anomalie",
                    ))

                fig_lat.update_layout(
                    xaxis_title="Dimensione Latente 1",
                    yaxis_title="Dimensione Latente 2",
                    height=500,
                    margin=dict(l=50, r=20, t=30, b=50),
                )
                st.plotly_chart(fig_lat, use_container_width=True)

                # Distribuzione MSE
                st.subheader("Distribuzione MSE")
                fig_hist = go.Figure()
                fig_hist.add_trace(go.Histogram(
                    x=mse_values, nbinsx=50,
                    marker_color="#3b82f6",
                    name="MSE",
                ))
                if detector.threshold > 0:
                    fig_hist.add_vline(
                        x=detector.threshold, line_dash="dash", line_color="red",
                        annotation_text=f"Soglia ({detector.threshold:.4f})",
                    )
                fig_hist.update_layout(
                    xaxis_title="MSE", yaxis_title="Conteggio", height=300,
                    margin=dict(l=50, r=20, t=30, b=50),
                )
                st.plotly_chart(fig_hist, use_container_width=True)

            except Exception as e:
                st.error(f"Errore proiezione latente: {e}")

    # Correlazione stringhe (indipendente dal modello)
    st.markdown("---")
    st.subheader("Matrice Correlazione Stringhe")

    sa = st.session_state.detector.string_analyzer
    history_raw = [
        s for s in st.session_state.detector.fetcher.history
        if s.get("_raw_inverters")
    ]

    if len(history_raw) >= 20 and sa.is_configured:
        from config import DC_POWER_FIELDS as _DC_FIELDS
        active_strings = [s for s in sa.strings if s.n_modules]
        power_series = {}
        for s in active_strings:
            power_series[s.label] = []

        for sample in history_raw[-200:]:
            inv_by_sn = {
                inv.get("inverterSN", ""): inv
                for inv in sample.get("_raw_inverters", [])
            }
            for s in active_strings:
                inv = inv_by_sn.get(s.inverter_sn, {})
                pdc = float(inv.get(_DC_FIELDS[s.mppt_index]) or 0)
                n = s.n_modules or 1
                power_series[s.label].append(pdc / n)

        labels = list(power_series.keys())
        min_len = min(len(v) for v in power_series.values()) if power_series else 0
        if min_len >= 10:
            matrix = np.array([power_series[k][:min_len] for k in labels])
            corr = np.corrcoef(matrix)

            fig_corr = go.Figure(go.Heatmap(
                z=corr, x=labels, y=labels,
                colorscale="RdYlGn",
                zmin=0, zmax=1,
                text=np.around(corr, 2),
                texttemplate="%{text}",
            ))
            fig_corr.update_layout(
                height=500, margin=dict(l=100, r=20, t=30, b=100),
                xaxis_tickangle=-45,
            )
            st.plotly_chart(fig_corr, use_container_width=True)
            st.caption(
                "Stringhe con correlazione alta (>0.90, verde) hanno lo stesso orientamento. "
                "Correlazione bassa = orientamenti diversi."
            )
    else:
        st.info(
            "Servono almeno 20 campioni con dati raw inverter per la matrice di correlazione."
        )

# --- AUTO POLLING (browser-side, opzionale) ---
if auto_poll:
    time.sleep(POLLING_INTERVAL)
    st.rerun()
