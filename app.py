import base64
import json
import os
from pathlib import Path
from typing import Any, Dict

import gspread
import pandas as pd
import plotly.express as px
import streamlit as st
from oauth2client.service_account import ServiceAccountCredentials

PROJECT_ROOT = Path(__file__).resolve().parent
NOME_PLANILHA = "Monitoramento_BAP"
CREDENTIALS_JSON = PROJECT_ROOT / "credentials.json"
GOOGLE_CREDENTIALS_ENV = "GOOGLE_CREDENTIALS_JSON"
GOOGLE_CREDENTIALS_B64_ENV = "GOOGLE_CREDENTIALS_BASE64"


def _obter_credenciais_google_dict() -> Dict[str, Any]:
    """Le credenciais com prioridade para Streamlit secrets e variaveis de ambiente."""
    if "gcp_service_account" in st.secrets:
        return dict(st.secrets["gcp_service_account"])

    conteudo_json = os.environ.get(GOOGLE_CREDENTIALS_ENV, "").strip()
    if conteudo_json:
        return json.loads(conteudo_json)

    conteudo_b64 = os.environ.get(GOOGLE_CREDENTIALS_B64_ENV, "").strip()
    if conteudo_b64:
        decodificado = base64.b64decode(conteudo_b64).decode("utf-8")
        return json.loads(decodificado)

    if CREDENTIALS_JSON.is_file():
        return json.loads(CREDENTIALS_JSON.read_text(encoding="utf-8"))

    raise FileNotFoundError(
        "Credenciais Google nao encontradas. Configure st.secrets['gcp_service_account'], "
        "ou GOOGLE_CREDENTIALS_JSON/GOOGLE_CREDENTIALS_BASE64, ou credentials.json local."
    )


@st.cache_data(ttl=300)
def carregar_dados_planilha() -> pd.DataFrame:
    escopos = (
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    )
    credenciais = ServiceAccountCredentials.from_json_keyfile_dict(_obter_credenciais_google_dict(), escopos)
    cliente = gspread.authorize(credenciais)
    planilha = cliente.open(NOME_PLANILHA)
    aba = planilha.sheet1
    registros = aba.get_all_records()
    if not registros:
        return pd.DataFrame()
    return pd.DataFrame(registros)


def _parse_numero(valor: Any) -> float:
    if valor is None:
        return 0.0
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).strip()
    if not texto:
        return 0.0
    texto = texto.replace("R$", "").replace(".", "").replace(",", ".").strip()
    try:
        return float(texto)
    except ValueError:
        return 0.0


def _formatar_real(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def preparar_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()
    df["Data da Extração"] = pd.to_datetime(df["Data da Extração"], errors="coerce")
    df["Total Lançamentos (R$)"] = df["Total Lançamentos (R$)"].apply(_parse_numero)
    df["Qtd Itens"] = pd.to_numeric(df["Qtd Itens"], errors="coerce").fillna(0).astype(int)
    df["Qtd Páginas"] = pd.to_numeric(df["Qtd Páginas"], errors="coerce").fillna(0).astype(int)
    df = df.dropna(subset=["Data da Extração"]).sort_values("Data da Extração")
    df["Mes"] = df["Data da Extração"].dt.to_period("M").dt.to_timestamp()
    return df


def render_dashboard(df: pd.DataFrame) -> None:
    st.title("Dashboard - Monitoramento BAP")
    st.caption("Resumo das extrações da planilha Monitoramento_BAP")

    if df.empty:
        st.warning("Nenhum dado encontrado na planilha.")
        return

    ultimo_mes = df["Mes"].max()
    df_ultimo_mes = df[df["Mes"] == ultimo_mes]

    total_ultimo_mes = df_ultimo_mes["Total Lançamentos (R$)"].sum()
    total_extracoes_mes = len(df_ultimo_mes)
    media_mes = total_ultimo_mes / total_extracoes_mes if total_extracoes_mes else 0.0
    total_itens_mes = int(df_ultimo_mes["Qtd Itens"].sum())

    st.markdown(
        """
        <style>
        div[data-testid="stMetric"] {
            background-color: #111827;
            border: 1px solid #1f2937;
            padding: 18px;
            border-radius: 14px;
        }
        div[data-testid="stMetricValue"] { font-size: 2rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns(3)
    col1.metric("Total do último mês", _formatar_real(total_ultimo_mes))
    col2.metric("Extrações no último mês", f"{total_extracoes_mes}")
    col3.metric("Itens processados no mês", f"{total_itens_mes}")

    st.markdown("---")
    st.subheader("Evolução mensal dos valores")

    evolucao = (
        df.groupby("Mes", as_index=False)["Total Lançamentos (R$)"]
        .sum()
        .sort_values("Mes")
        .rename(columns={"Total Lançamentos (R$)": "Total"})
    )
    evolucao["MesLabel"] = evolucao["Mes"].dt.strftime("%Y-%m")

    grafico = px.bar(
        evolucao,
        x="MesLabel",
        y="Total",
        text_auto=".2s",
        labels={"MesLabel": "Mês", "Total": "Total (R$)"},
        title="Total de lançamentos por mês",
    )
    grafico.update_layout(height=420)
    st.plotly_chart(grafico, use_container_width=True)

    st.subheader("Histórico completo de extrações")
    tabela = df.sort_values("Data da Extração", ascending=False).copy()
    tabela["Data da Extração"] = tabela["Data da Extração"].dt.strftime("%Y-%m-%d %H:%M:%S")
    tabela["Total Lançamentos (R$)"] = tabela["Total Lançamentos (R$)"].apply(_formatar_real)
    st.dataframe(tabela, use_container_width=True, hide_index=True)


def main() -> None:
    st.set_page_config(page_title="Monitoramento BAP", page_icon="📊", layout="wide")
    try:
        df = carregar_dados_planilha()
    except Exception as erro:  # noqa: BLE001
        st.error("Falha ao conectar na planilha Monitoramento_BAP.")
        st.exception(erro)
        return

    df = preparar_dataframe(df)
    render_dashboard(df)


if __name__ == "__main__":
    main()
