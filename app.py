import streamlit as st
import pandas as pd
import requests
import time
import unicodedata
import re
import io
from datetime import datetime

st.set_page_config(
    page_title="Hunter — Recherche emails maires",
    page_icon="📧",
    layout="wide",
)

# ── CSS ──────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="stAppViewContainer"] { background: #f8f7f4; }
[data-testid="stSidebar"] { background: #ffffff; border-right: 1px solid #e5e3dc; }
.metric-card {
    background: white; border: 1px solid #e5e3dc; border-radius: 10px;
    padding: 1rem 1.25rem; text-align: center;
}
.metric-label { font-size: 12px; color: #888; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 4px; }
.metric-value { font-size: 28px; font-weight: 600; }
.found    { color: #1D9E75; }
.notfound { color: #D85A30; }
.neutral  { color: #222; }
.log-box {
    background: #1e1e1e; color: #d4d4d4; border-radius: 8px;
    padding: 12px; font-family: monospace; font-size: 12px;
    height: 220px; overflow-y: auto;
}
</style>
""", unsafe_allow_html=True)


# ── Helpers ──────────────────────────────────────────────────────────────────
def normalize(s: str) -> str:
    """Supprime accents, met en minuscules, garde uniquement a-z0-9."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", s.lower())


def clean_domain(raw: str) -> str:
    """Extrait le domaine depuis une URL ou un email."""
    raw = (raw or "").strip()
    if "@" in raw:
        raw = raw.split("@")[1]
    raw = re.sub(r"^https?://", "", raw)
    return raw.split("/")[0].strip()


PATTERNS = {
    "prénom.nom":  lambda p, n: f"{p}.{n}",
    "nom.prénom":  lambda p, n: f"{n}.{p}",
    "p.nom":       lambda p, n: f"{p[0]}.{n}",
    "nom.p":       lambda p, n: f"{n}.{p[0]}",
    "prénomnom":   lambda p, n: f"{p}{n}",
    "nomprenom":   lambda p, n: f"{n}{p}",
    "prénom":      lambda p, n: p,
    "pnom":        lambda p, n: f"{p[0]}{n}",
}


def verify_email(api_key: str, email: str) -> dict:
    """Appel Hunter email-verifier. Renvoie le dict data ou lève une exception."""
    url = "https://api.hunter.io/v2/email-verifier"
    r = requests.get(url, params={"email": email, "api_key": api_key}, timeout=10)
    if r.status_code == 429:
        raise RuntimeError("Quota Hunter atteint (429). Augmentez le délai ou attendez.")
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json().get("data", {})


def score_color(score: int) -> str:
    if score >= 70:
        return "🟢"
    if score >= 40:
        return "🟡"
    return "🔴"


# ── Sidebar — Configuration ───────────────────────────────────────────────────
with st.sidebar:
    st.title("⚙️ Configuration")

    api_key = st.text_input(
        "Clé API Hunter.io",
        type="password",
        help="Disponible sur hunter.io → Settings → API",
    )
    if api_key:
        st.success("Clé renseignée ✓")
    else:
        st.info("Créez un compte gratuit sur [hunter.io](https://hunter.io)")

    st.divider()

    threshold = st.slider(
        "Seuil d'acceptabilité (%)",
        min_value=10, max_value=95, value=50, step=5,
        help="Score Hunter minimum pour considérer l'email comme trouvé",
    )
    st.caption({
        range(0, 40): "⚠️ Large — risque de faux positifs",
        range(40, 70): "✅ Modéré — bon équilibre",
        range(70, 100): "🔒 Strict — très fiable",
    }.get(next(r for r in [range(0,40), range(40,70), range(70,100)] if threshold in r), ""))

    delay_ms = st.slider(
        "Délai entre appels (ms)",
        min_value=300, max_value=5000, value=700, step=100,
        help="Évite le rate-limit Hunter (429). Plan gratuit: 25 vérif/mois.",
    )

    st.divider()
    st.markdown("**Patterns à tester**")
    selected_patterns = {}
    for name in PATTERNS:
        selected_patterns[name] = st.checkbox(name, value=name in ["prénom.nom", "p.nom", "nom.prénom", "pnom"])

    active_patterns = {k: v for k, v in PATTERNS.items() if selected_patterns[k]}


# ── Main ──────────────────────────────────────────────────────────────────────
st.title("📧 Recherche d'emails — Maires de France")
st.caption("Teste automatiquement les combinaisons via l'API Hunter.io et identifie l'email personnel du maire.")

# ── Upload CSV ────────────────────────────────────────────────────────────────
st.header("1 · Import du fichier CSV")
uploaded = st.file_uploader("Glissez votre CSV de maires", type=["csv"])

df_raw = None
col_prenom = col_nom = col_domain = None

if uploaded:
    try:
        # Détection séparateur
        sample = uploaded.read(2048).decode("utf-8", errors="replace")
        uploaded.seek(0)
        sep = ";" if sample.count(";") > sample.count(",") else ","
        df_raw = pd.read_csv(uploaded, sep=sep, dtype=str).fillna("")
        st.success(f"{len(df_raw)} lignes chargées · {len(df_raw.columns)} colonnes")

        cols = ["— sélectionner —"] + list(df_raw.columns)

        c1, c2, c3 = st.columns(3)

        def guess(keywords):
            for k in keywords:
                for c in df_raw.columns:
                    if k in c.lower():
                        return c
            return cols[0]

        with c1:
            col_prenom = st.selectbox("Colonne Prénom", cols,
                index=cols.index(guess(["prenom","prénom","firstname","first", "prenom_maire"])))
        with c2:
            col_nom = st.selectbox("Colonne Nom", cols,
                index=cols.index(guess(["nom","name","last", "nom_maire"])))
        with c3:
            col_domain = st.selectbox("Colonne Domaine/Email", cols,
                index=cols.index(guess(["domain","domaine","mail","email","site","web", "email_contact"])))

        if "— sélectionner —" not in [col_prenom, col_nom, col_domain]:
            st.dataframe(
                df_raw[[col_prenom, col_nom, col_domain]].head(5),
                use_container_width=True, hide_index=True,
            )
    except Exception as e:
        st.error(f"Erreur lecture CSV : {e}")

# ── Lancement ─────────────────────────────────────────────────────────────────
st.header("2 · Lancement")

ready = (
    api_key
    and df_raw is not None
    and col_prenom and col_nom and col_domain
    and "— sélectionner —" not in [col_prenom, col_nom, col_domain]
    and len(active_patterns) > 0
)

if not ready:
    missing = []
    if not api_key: missing.append("clé API")
    if df_raw is None: missing.append("fichier CSV")
    if df_raw is not None and "— sélectionner —" in [col_prenom, col_nom, col_domain]:
        missing.append("mapping des colonnes")
    if not active_patterns: missing.append("au moins un pattern")
    st.warning(f"En attente : {', '.join(missing)}")

col_btn1, col_btn2, _ = st.columns([1, 1, 4])
start = col_btn1.button("▶ Lancer", type="primary", disabled=not ready)
stop_placeholder = col_btn2.empty()

# ── Session state ─────────────────────────────────────────────────────────────
if "results" not in st.session_state:
    st.session_state.results = []
if "running" not in st.session_state:
    st.session_state.running = False
if "stop" not in st.session_state:
    st.session_state.stop = False

if start:
    st.session_state.results = []
    st.session_state.running = True
    st.session_state.stop = False

# ── Boucle principale ─────────────────────────────────────────────────────────
if st.session_state.running and df_raw is not None:

    if stop_placeholder.button("⏹ Arrêter"):
        st.session_state.stop = True

    progress_bar = st.progress(0, text="Initialisation...")
    log_area = st.empty()
    log_lines = []

    def add_log(msg, icon=""):
        log_lines.append(f"{icon} {msg}" if icon else msg)
        log_area.code("\n".join(log_lines[-30:]), language=None)

    total = len(df_raw)
    found_count = 0

    for idx, row in df_raw.iterrows():
        if st.session_state.stop:
            add_log("Arrêt demandé par l'utilisateur.", "🛑")
            break

        prenom_raw = str(row.get(col_prenom, "")).strip()
        nom_raw = str(row.get(col_nom, "")).strip()
        domain_raw = str(row.get(col_domain, "")).strip()

        prenom = normalize(prenom_raw)
        nom = normalize(nom_raw)
        domain = clean_domain(domain_raw)

        if not prenom or not nom or not domain:
            add_log(f"[{idx+1}/{total}] Données manquantes — ignoré", "⚠️")
            continue

        progress_bar.progress(
            (idx + 1) / total,
            text=f"{idx+1}/{total} — {prenom_raw} {nom_raw}",
        )

        best_score = 0
        best_email = None
        best_pattern = None
        email_found = False

        for pat_name, pat_fn in active_patterns.items():
            if st.session_state.stop:
                break
            local = pat_fn(prenom, nom)
            email = f"{local}@{domain}"
            try:
                data = verify_email(api_key, email)
                score = data.get("score", 0) or 0
                add_log(f"[{idx+1}] {email} → score {score}%")
                if score > best_score:
                    best_score = score
                    best_email = email
                    best_pattern = pat_name
                if score >= threshold:
                    email_found = True
                    break
            except RuntimeError as e:
                add_log(str(e), "❌")
                if "429" in str(e):
                    st.error(str(e))
                    st.session_state.stop = True
                    break
            except Exception as e:
                add_log(f"Erreur réseau: {e}", "⚠️")
            time.sleep(delay_ms / 1000)

        st.session_state.results.append({
            "Prénom": prenom_raw,
            "Nom": nom_raw,
            "Domaine": domain,
            "Email trouvé": best_email or "—",
            "Score (%)": best_score,
            "Pattern": best_pattern or "—",
            "Statut": "✅ Trouvé" if email_found else "❌ Non trouvé",
        })

        if email_found:
            found_count += 1
            add_log(f"→ TROUVÉ : {best_email} ({best_score}%)", "✅")
        else:
            add_log(f"→ Non trouvé (meilleur: {best_score}%)", "❌")

    st.session_state.running = False
    progress_bar.progress(1.0, text="Terminé ✓")

# ── Résultats ─────────────────────────────────────────────────────────────────
if st.session_state.results:
    st.header("3 · Résultats")
    df_res = pd.DataFrame(st.session_state.results)

    found_mask = df_res["Statut"] == "✅ Trouvé"
    n_total = len(df_res)
    n_found = found_mask.sum()
    n_not = n_total - n_found
    rate = round(n_found / n_total * 100) if n_total else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Traités", n_total)
    c2.metric("Trouvés ✅", n_found)
    c3.metric("Non trouvés ❌", n_not)
    c4.metric("Taux de succès", f"{rate}%")

    tab1, tab2, tab3 = st.tabs(["Tous", "Trouvés ✅", "Non trouvés ❌"])
    with tab1:
        st.dataframe(df_res, use_container_width=True, hide_index=True)
    with tab2:
        st.dataframe(df_res[found_mask], use_container_width=True, hide_index=True)
    with tab3:
        st.dataframe(df_res[~found_mask], use_container_width=True, hide_index=True)

    # Export
    st.subheader("Export")
    ec1, ec2 = st.columns(2)

    csv_bytes = df_res.to_csv(index=False, sep=";", encoding="utf-8-sig").encode("utf-8-sig")
    ec1.download_button(
        "⬇ Télécharger CSV complet",
        data=csv_bytes,
        file_name=f"maires_emails_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
    )

    df_found = df_res[found_mask]
    if not df_found.empty:
        csv_found = df_found.to_csv(index=False, sep=";", encoding="utf-8-sig").encode("utf-8-sig")
        ec2.download_button(
            "⬇ Télécharger uniquement les trouvés",
            data=csv_found,
            file_name=f"maires_emails_trouves_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
        )
