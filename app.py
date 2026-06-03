"""Streamlit dashboard for the local leads pipeline.

Provides a sidebar form to configure and launch ``main.py`` as a subprocess,
streams its output in real time, and displays the resulting leads table with
summary metrics once the run completes.
"""

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

from config import HEADLESS, SOCIAL_DOMAINS, VERSION

_LEADS_PATH = Path("data/leads.json")
_DISPLAY_COLS = ["lead", "website", "city", "cms", "seo_score", "seo_issues", "email", "phone"]


def _load_leads() -> pd.DataFrame | None:
    """Load leads from disk. Returns ``None`` when no results file exists yet."""
    if not _LEADS_PATH.exists():
        return None
    with open(_LEADS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return pd.DataFrame(data).fillna("")


def _show_results(df: pd.DataFrame) -> None:
    """Render summary metrics and the leads table for a given DataFrame."""
    total = len(df)
    with_msg = int(df["subject"].astype(bool).sum()) if "subject" in df.columns else 0
    with_email = int(df["email"].astype(bool).sum()) if "email" in df.columns else 0
    contactable = int(
        df[["email", "phone", *SOCIAL_DOMAINS]]
        .astype(bool).any(axis=1).sum()
    ) if "email" in df.columns else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Leads totales", total)
    c2.metric("Contactables", contactable)
    c3.metric("Con email", with_email)
    c4.metric("Con mensaje", with_msg)

    cols = [c for c in _DISPLAY_COLS if c in df.columns]
    st.dataframe(df[cols], width="stretch", hide_index=True)


# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(page_title="Local Leads Automation", layout="wide")
st.title(f"Local Leads Automation v{VERSION}")

# ── Sidebar — configuration ───────────────────────────────────────────────────

with st.sidebar:
    st.header("Configuración")
    profession = st.text_input("Profesión", placeholder="ej. abogados")
    city = st.text_input("Ciudad", placeholder="ej. Crevillent")
    no_limit = st.checkbox("Sin límite de resultados", value=False)
    max_results = None if no_limit else st.number_input(
        "Máx. resultados", min_value=1, max_value=500, value=20,
        help="Límite de negocios a extraer de Google Maps",
        disabled=no_limit,
    )
    headless = st.checkbox("Modo silencioso", value=True,
                           help="Desactiva para ver el navegador (depuración)")

    st.divider()
    run = st.button("▶ Ejecutar pipeline", type="primary", use_container_width=True,
                    disabled=not (profession.strip() and city.strip()))

# ── Main area ─────────────────────────────────────────────────────────────────

main = st.empty()

if run:
    main.empty()

    cmd = [sys.executable, "-u", "main.py", "--profession", profession, "--city", city]
    if max_results is not None:
        cmd += ["--max", str(max_results)]
    if not headless:
        cmd.append("--no-headless")

    log_placeholder = st.empty()
    log_lines: list[str] = []

    with st.spinner("Pipeline en ejecución…"):
        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        ) as proc:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    log_lines.append(line)
                    log_placeholder.code("\n".join(log_lines), language=None)

    log_placeholder.empty()
    with st.expander("Ver log de ejecución", expanded=False):
        st.code("\n".join(log_lines), language=None)

    if proc.returncode == 0:
        st.success("Pipeline completado correctamente.")
    else:
        st.error("El pipeline terminó con errores. Revisa el log.")

    df = _load_leads()
    if df is not None:
        st.divider()
        st.subheader("Resultados")
        _show_results(df)

else:
    with main.container():
        df = _load_leads()
        if df is not None:
            st.subheader("Último resultado guardado")
            _show_results(df)
        else:
            st.info("Configura los parámetros en el panel izquierdo y pulsa **Ejecutar pipeline**.")
