import os
import re
import json
import time
import base64
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple
from collections import defaultdict

import pdfplumber
from selenium import webdriver
from selenium.common.exceptions import ElementClickInterceptedException, NoSuchWindowException, TimeoutException
from selenium.webdriver import ChromeOptions
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.select import Select
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

import gspread
from gspread.exceptions import SpreadsheetNotFound
from oauth2client.service_account import ServiceAccountCredentials

HEADLESS_MODE = os.environ.get("HEADLESS_MODE", "").strip().lower() in {"1", "true", "yes", "on"} or os.environ.get(
    "GITHUB_ACTIONS", ""
).strip().lower() == "true"
BASE_URL = "https://live.bap.com.br/"

PROJECT_ROOT = Path(__file__).resolve().parent
CREDENTIALS_JSON = PROJECT_ROOT / "credentials.json"
NOME_PLANILHA = "Monitoramento_BAP"
GOOGLE_CREDENTIALS_ENV = "GOOGLE_CREDENTIALS_JSON"
GOOGLE_CREDENTIALS_B64_ENV = "GOOGLE_CREDENTIALS_BASE64"

DOWNLOAD_DIR = PROJECT_ROOT / "downloads"
DEBUG_DIR = PROJECT_ROOT / "debug"
DEFAULT_TIMEOUT = 30


class Selectors:
    # Login
    USER_INPUT = (By.ID, "ucLoginSistema_tbNomeEntrar")
    PASSWORD_INPUT = (By.ID, "ucLoginSistema_tbSenhaEntrar")
    LOGIN_BUTTON = (By.ID, "ucLoginSistema_btEntrar")

    # Perfil
    PERFIL_SELECT = (By.ID, "ddPerfil")
    PERFIL_ACESSAR_BUTTON = (By.ID, "btAcessar")

    # Menus (Relatorios > Financeiro > Receitas e Despesas)
    MENU_CONTAINER = (By.CSS_SELECTOR, "nav.fixed-sidebar-left.menu-principal")
    TOGGLE_RELATORIOS = (
        By.XPATH,
        "//nav[contains(@class,'menu-principal')]//a[@href='#2_10011' or @data-target='#2_10011']",
    )
    TOGGLE_FINANCEIRO = (
        By.XPATH,
        "//nav[contains(@class,'menu-principal')]//a[@href='#2_10011_10054' or @data-target='#2_10011_10054']",
    )
    MENU_RELATORIOS = (
        By.XPATH,
        "//nav[contains(@class,'menu-principal')]//*[self::a or self::span]"
        "[contains(normalize-space(.),'Relatórios') or contains(normalize-space(.),'RELATÓRIOS') or contains(normalize-space(.),'RELATORIOS')]",
    )
    MENU_FINANCEIRO = (
        By.XPATH,
        "//ul[@id='2_10011']//a[contains(translate(normalize-space(.),"
        "'abcdefghijklmnopqrstuvwxyzáàâãéèêíìîóòôõúùûç',"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZAAAAEEEIIIOOOOUUUC'),'FINANCEIRO')]",
    )
    MENU_FINANCEIRO_FALLBACK = (
        By.XPATH,
        "//nav[contains(@class,'menu-principal')]//*[self::a or self::span]"
        "[contains(translate(normalize-space(.),"
        "'abcdefghijklmnopqrstuvwxyzáàâãéèêíìîóòôõúùûç',"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZAAAAEEEIIIOOOOUUUC'),'FINANCEIRO')]",
    )
    MENU_RECEITAS_DESPESAS = (
        By.XPATH,
        "//a[contains(@href,'Emp_RelatorioReceitaDespesa.aspx') and contains(translate(normalize-space(.),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'RECEITAS E DESPESAS')]",
    )
    MENU_RECEITAS_DESPESAS_FALLBACK = (
        By.XPATH,
        "//nav[contains(@class,'menu-principal')]//*[self::a or self::span][contains(translate(normalize-space(.),'abcdefghijklmnopqrstuvwxyz','ABCDEFGHIJKLMNOPQRSTUVWXYZ'),'RECEITAS E DESPESAS')]",
    )

    # Parametros do relatorio
    CAMPO_TIPO = (By.ID, "body_DropDownList_body_Formulario_Tipo")
    DATA_INICIO = (By.ID, "body_TextBox_body_Formulario_Periodo_Inicio")
    DATA_FIM = (By.ID, "body_TextBox_body_Formulario_Periodo_Fim")
    BOTAO_ABRIR = (By.ID, "body_Button_body_Formulario_Abrir")

    # Janela do demonstrativo
    BOTAO_BAIXAR_PDF = (By.ID, "btRelatorioPDF")


def criar_driver() -> webdriver.Chrome:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    options = ChromeOptions()
    prefs = {
        "download.default_directory": str(DOWNLOAD_DIR),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
        "safebrowsing.enabled": True,
    }
    options.add_experimental_option("prefs", prefs)

    if HEADLESS_MODE:
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

    options.add_argument("--window-size=1920,1080")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(DEFAULT_TIMEOUT)
    return driver


def primeiro_ultimo_dia_mes_anterior() -> tuple[str, str]:
    hoje = date.today()
    primeiro_dia_mes_atual = hoje.replace(day=1)
    ultimo = primeiro_dia_mes_atual - timedelta(days=1)
    primeiro = ultimo.replace(day=1)
    return primeiro.strftime("%d/%m/%Y"), ultimo.strftime("%d/%m/%Y")


def esperar_e_clicar(driver: webdriver.Chrome, locator: tuple[str, str], timeout: int = DEFAULT_TIMEOUT) -> None:
    WebDriverWait(driver, timeout).until(EC.element_to_be_clickable(locator)).click()


def esperar_e_preencher(
    driver: webdriver.Chrome, locator: tuple[str, str], valor: str, timeout: int = DEFAULT_TIMEOUT
) -> None:
    campo = WebDriverWait(driver, timeout).until(EC.visibility_of_element_located(locator))
    campo.clear()
    campo.send_keys(valor)


def esperar_e_clicar_com_fallback(
    driver: webdriver.Chrome, locators: List[tuple[str, str]], timeout: int = DEFAULT_TIMEOUT
) -> None:
    ultimo_erro: Exception | None = None
    for locator in locators:
        try:
            garantir_janela_ativa(driver)
            elemento = WebDriverWait(driver, timeout).until(EC.presence_of_element_located(locator))
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", elemento)
            time.sleep(0.3)
            try:
                WebDriverWait(driver, timeout).until(EC.element_to_be_clickable(locator)).click()
            except (TimeoutException, ElementClickInterceptedException):
                driver.execute_script("arguments[0].click();", elemento)
            return
        except NoSuchWindowException:
            try:
                garantir_janela_ativa(driver)
                elemento = WebDriverWait(driver, timeout).until(EC.presence_of_element_located(locator))
                driver.execute_script("arguments[0].click();", elemento)
                return
            except Exception as erro:  # noqa: BLE001
                ultimo_erro = erro
        except Exception as erro:  # noqa: BLE001
            ultimo_erro = erro
    raise TimeoutException(f"Nao foi possivel clicar em nenhum locator de menu. Erro final: {ultimo_erro}")


def garantir_janela_ativa(driver: webdriver.Chrome) -> None:
    handles = driver.window_handles
    if not handles:
        raise TimeoutException("Nenhuma janela do navegador disponivel.")

    try:
        atual = driver.current_window_handle
        if atual not in handles:
            driver.switch_to.window(handles[-1])
    except NoSuchWindowException:
        driver.switch_to.window(handles[-1])

    driver.switch_to.default_content()


def alternar_para_janela_com_elemento(
    driver: webdriver.Chrome, locator: tuple[str, str], timeout: int = DEFAULT_TIMEOUT
) -> bool:
    """
    Percorre as janelas abertas e alterna para a primeira que contem o elemento.
    Retorna True quando encontrou; False quando nao encontrou em nenhuma.
    """
    fim = time.time() + timeout
    while time.time() < fim:
        handles = driver.window_handles
        for handle in reversed(handles):
            try:
                driver.switch_to.window(handle)
                driver.switch_to.default_content()
                WebDriverWait(driver, 2).until(EC.presence_of_element_located(locator))
                return True
            except (TimeoutException, NoSuchWindowException):
                continue
        time.sleep(0.3)
    return False


def expandir_collapse(driver: webdriver.Chrome, panel_id: str, toggle_locators: List[tuple[str, str]]) -> None:
    garantir_janela_ativa(driver)
    panel_locator = (By.ID, panel_id)

    panel = WebDriverWait(driver, DEFAULT_TIMEOUT).until(EC.presence_of_element_located(panel_locator))
    classes = (panel.get_attribute("class") or "").lower()
    aria = (panel.get_attribute("aria-expanded") or "").lower()
    aberto = (" in" in f" {classes}") or ("show" in classes) or (aria == "true")

    if not aberto:
        esperar_e_clicar_com_fallback(driver, toggle_locators)
        WebDriverWait(driver, DEFAULT_TIMEOUT).until(
            lambda d: (
                " in" in f" {(d.find_element(*panel_locator).get_attribute('class') or '').lower()}"
                or "show" in (d.find_element(*panel_locator).get_attribute("class") or "").lower()
                or (d.find_element(*panel_locator).get_attribute("aria-expanded") or "").lower() == "true"
            )
        )


def salvar_debug(driver: webdriver.Chrome, nome_base: str) -> None:
    ts = int(time.time())
    screenshot_path = DEBUG_DIR / f"{nome_base}_{ts}.png"
    html_path = DEBUG_DIR / f"{nome_base}_{ts}.html"
    try:
        garantir_janela_ativa(driver)
        driver.save_screenshot(str(screenshot_path))
        html_path.write_text(driver.page_source, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def login(driver: webdriver.Chrome) -> None:
    usuario = os.environ.get("BAP_USERNAME")
    senha = os.environ.get("BAP_PASSWORD")
    if not usuario or not senha:
        raise ValueError("Defina as variaveis de ambiente BAP_USERNAME e BAP_PASSWORD.")

    driver.get(BASE_URL)
    esperar_e_preencher(driver, Selectors.USER_INPUT, usuario)
    esperar_e_preencher(driver, Selectors.PASSWORD_INPUT, senha)
    esperar_e_clicar(driver, Selectors.LOGIN_BUTTON)


def selecionar_perfil_administrador(driver: webdriver.Chrome) -> None:
    select_el = WebDriverWait(driver, DEFAULT_TIMEOUT).until(EC.visibility_of_element_located(Selectors.PERFIL_SELECT))
    Select(select_el).select_by_visible_text("ADM OPERACIONAL")
    esperar_e_clicar(driver, Selectors.PERFIL_ACESSAR_BUTTON)


def navegar_ate_relatorio(driver: webdriver.Chrome) -> None:
    garantir_janela_ativa(driver)
    WebDriverWait(driver, DEFAULT_TIMEOUT).until(EC.visibility_of_element_located(Selectors.MENU_CONTAINER))
    try:
        expandir_collapse(driver, "2_10011", [Selectors.TOGGLE_RELATORIOS, Selectors.MENU_RELATORIOS])
        expandir_collapse(driver, "2_10011_10054", [Selectors.TOGGLE_FINANCEIRO, Selectors.MENU_FINANCEIRO])
        esperar_e_clicar_com_fallback(
            driver, [Selectors.MENU_RECEITAS_DESPESAS, Selectors.MENU_RECEITAS_DESPESAS_FALLBACK]
        )
    except Exception:  # noqa: BLE001
        salvar_debug(driver, "falha_menu")
        # Fallback: navega direto para a pagina de Receitas e Despesas.
        driver.get("https://live.bap.com.br/Operacional/Empreendimento/Emp_RelatorioReceitaDespesa.aspx")


def preencher_parametros_relatorio(driver: webdriver.Chrome, tipo: str = "Receitas") -> None:
    tipo = tipo.strip().capitalize()
    if tipo not in ("Receitas", "Despesas"):
        raise ValueError("O parametro 'tipo' deve ser 'Receitas' ou 'Despesas'.")

    primeiro_dia, ultimo_dia = primeiro_ultimo_dia_mes_anterior()

    campo_tipo = WebDriverWait(driver, DEFAULT_TIMEOUT).until(EC.visibility_of_element_located(Selectors.CAMPO_TIPO))
    Select(campo_tipo).select_by_visible_text(tipo)

    esperar_e_preencher(driver, Selectors.DATA_INICIO, primeiro_dia)
    esperar_e_preencher(driver, Selectors.DATA_FIM, ultimo_dia)


def abrir_relatorio_em_nova_janela(driver: webdriver.Chrome) -> None:
    janelas_antes = driver.window_handles.copy()
    garantir_janela_ativa(driver)

    # Fecha o datepicker (quando aberto) para nao interceptar o clique no botao "Abrir".
    try:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        time.sleep(0.2)
    except Exception:  # noqa: BLE001
        pass

    try:
        esperar_e_clicar(driver, Selectors.BOTAO_ABRIR)
    except ElementClickInterceptedException:
        botao = WebDriverWait(driver, DEFAULT_TIMEOUT).until(EC.presence_of_element_located(Selectors.BOTAO_ABRIR))
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", botao)
        driver.execute_script("arguments[0].click();", botao)

    # Em alguns ambientes (especialmente headless/CI), o "Abrir" nao cria nova janela.
    # Primeiro tenta alternar para nova janela; se nao existir, continua na atual.
    try:
        WebDriverWait(driver, 10).until(lambda d: len(d.window_handles) > len(janelas_antes))
        janelas_depois = driver.window_handles
        nova_janela = next(handle for handle in janelas_depois if handle not in janelas_antes)
        driver.switch_to.window(nova_janela)
    except TimeoutException:
        pass

    # Garante que estamos em uma janela onde o botao de PDF existe.
    if not alternar_para_janela_com_elemento(driver, Selectors.BOTAO_BAIXAR_PDF, timeout=DEFAULT_TIMEOUT):
        raise TimeoutException(
            "Nao foi possivel localizar o botao de PDF apos clicar em 'Abrir' "
            "(nem em nova janela, nem na janela atual)."
        )


def baixar_pdf(driver: webdriver.Chrome) -> Path:
    garantir_janela_ativa(driver)
    if not alternar_para_janela_com_elemento(driver, Selectors.BOTAO_BAIXAR_PDF, timeout=DEFAULT_TIMEOUT):
        raise TimeoutException("Botao de PDF nao encontrado na janela atual.")

    janelas_antes = driver.window_handles.copy()
    esperar_e_clicar(driver, Selectors.BOTAO_BAIXAR_PDF)

    # Dependendo do comportamento do portal, o clique pode abrir uma nova aba/janela com o PDF.
    try:
        WebDriverWait(driver, 10).until(lambda d: len(d.window_handles) > len(janelas_antes))
        janelas_depois = driver.window_handles
        nova_janela = next(handle for handle in janelas_depois if handle not in janelas_antes)
        driver.switch_to.window(nova_janela)
    except TimeoutException:
        pass

    return aguardar_download_pdf()


def aguardar_download_pdf(timeout: int = 60) -> Path:
    inicio = time.time()
    while time.time() - inicio < timeout:
        incompletos = list(DOWNLOAD_DIR.glob("*.crdownload"))
        pdfs = sorted(DOWNLOAD_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)

        if not incompletos and pdfs:
            arquivo = pdfs[0]
            if arquivo.stat().st_size > 0:
                return arquivo
        time.sleep(1)

    raise TimeoutException("Tempo esgotado aguardando download do PDF.")


def obter_pdf_mais_recente() -> Path:
    pdfs = sorted(DOWNLOAD_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not pdfs:
        raise FileNotFoundError("Nenhum arquivo PDF encontrado na pasta de downloads.")
    return pdfs[0]


def converter_valor_monetario(valor_raw: str) -> float:
    valor_limpo = valor_raw.strip()
    negativo = "-" in valor_limpo or "(" in valor_limpo
    valor_limpo = valor_limpo.replace("R$", "").replace("(", "").replace(")", "")
    valor_limpo = valor_limpo.replace(".", "").replace(",", ".").replace("-", "").strip()
    try:
        valor = float(valor_limpo)
    except ValueError:
        return 0.0
    return -valor if negativo else valor


def normalizar_texto(texto: str) -> str:
    texto = texto.strip()
    texto = unicodedata.normalize("NFKD", texto).encode("ASCII", "ignore").decode("ASCII")
    texto = re.sub(r"\s+", " ", texto)
    return texto.upper()


def linha_tem_total(descricao: str) -> bool:
    desc = normalizar_texto(descricao)
    return any(chave in desc for chave in ("TOTAL", "SALDO FINAL", "VALOR TOTAL"))


# (id, rotulo PT-BR, palavras-chave no texto ja normalizado — sem acento, maiusculas)
CATEGORIAS_LANCAMENTO: List[Tuple[str, str, Tuple[str, ...]]] = [
    ("fundo_reserva", "Fundo de reserva", ("FUNDO DE RESERVA", "FUNDO RESERVA", "FDO RESERVA")),
    ("cota_extra_obra", "Cota extra / obra", ("COTA EXTRA", "EXTRA OBRA", "COTA OBRA", "PC:", "PC :")),
    ("multa_juros", "Multa / juros / mora", ("MULTA", "JUROS", "MORA", "ENCARGO")),
    ("reajuste", "Reajuste", ("REAJUSTE", "REAJ.", "REAJ ")),
    ("condominio", "Condomínio", ("CONDOMIN", "CONDOMIO", "TAXA CONDOM")),
    ("taxa_administracao", "Taxa de administração", ("ADMINISTR", "TX ADM", "TAXA ADM", "ADM COND")),
    ("seguro", "Seguro", ("SEGURO",)),
    ("agua_gas_energia", "Água / gás / energia", ("AGUA", "ENERGIA", " ELETR", " GAS ", "GASOL")),
    ("taxa_bancaria", "Taxa bancária / TED", ("TED", "DOC", "TARIFA", "BANCAR")),
    ("diversos", "Nota fiscal / documento", ("NOTA FISCAL", "NF-E", "NFE ", "BOLETO BANC")),
]


def classificar_lancamento(descricao: str, responsavel: str) -> Tuple[str, str]:
    texto = normalizar_texto(f"{descricao} {responsavel}")
    for cat_id, rotulo, palavras in CATEGORIAS_LANCAMENTO:
        if any(p in texto for p in palavras):
            return cat_id, rotulo
    return "outros", "Outros"


def montar_resumo_por_categoria(itens: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    resumo: Dict[str, Dict[str, Any]] = {}
    for item in itens:
        rotulo = str(item.get("categoria", "Outros"))
        cat_id = str(item.get("categoria_id", "outros"))
        valor = float(item.get("valor", 0.0))
        if rotulo not in resumo:
            resumo[rotulo] = {"categoria_id": cat_id, "total": 0.0, "qtd_itens": 0}
        resumo[rotulo]["total"] += valor
        resumo[rotulo]["qtd_itens"] += 1
    for chave in resumo:
        resumo[chave]["total"] = round(resumo[chave]["total"], 2)
    return dict(sorted(resumo.items(), key=lambda x: (-x[1]["total"], x[0])))


def extrair_dados_do_pdf(caminho_arquivo: Path | str) -> Dict[str, Any]:
    caminho_pdf = Path(caminho_arquivo)
    padrao_valor = re.compile(r"-?\s*(?:R\$\s*)?\d{1,3}(?:\.\d{3})*,\d{2}")
    padrao_total = re.compile(
        r"(TOTAL(?:\s+GERAL)?|VALOR\s+TOTAL|SALDO(?:\s+FINAL)?)\s*:?\s*(-?\s*(?:R\$\s*)?\d{1,3}(?:\.\d{3})*,\d{2})",
        re.IGNORECASE,
    )

    itens: List[Dict[str, Any]] = []
    totais_encontrados: List[Dict[str, Any]] = []
    tabelas_extraidas: List[Dict[str, Any]] = []
    textos_paginas: List[Dict[str, Any]] = []
    total_lancamentos_calculado = 0.0
    total_por_responsavel: defaultdict[str, float] = defaultdict(float)
    chaves_unicas = set()

    with pdfplumber.open(caminho_pdf) as pdf:
        for numero_pagina, pagina in enumerate(pdf.pages, start=1):
            texto = pagina.extract_text() or ""
            textos_paginas.append({"pagina": numero_pagina, "texto": texto})

            for linha in texto.splitlines():
                valores_linha = padrao_valor.findall(linha)
                if not valores_linha:
                    continue

                descricao = padrao_valor.split(linha, maxsplit=1)[0].strip(" -:\t")
                if not descricao:
                    descricao = "Sem descricao"

                for valor_raw in valores_linha:
                    valor = converter_valor_monetario(valor_raw)
                    item_total = linha_tem_total(descricao)
                    chave_item = (numero_pagina, round(valor, 2), normalizar_texto(descricao), "T")

                    if item_total:
                        totais_encontrados.append(
                            {
                                "pagina": numero_pagina,
                                "chave": descricao,
                                "valor_raw": valor_raw.strip(),
                                "valor": valor,
                                "origem": "texto",
                            }
                        )
                    elif chave_item not in chaves_unicas:
                        chaves_unicas.add(chave_item)
                        item = {
                            "pagina": numero_pagina,
                            "origem": "texto",
                            "responsavel": descricao,
                            "descricao": descricao,
                            "valor_raw": valor_raw.strip(),
                            "valor": valor,
                        }
                        itens.append(item)
                        total_lancamentos_calculado += valor
                        total_por_responsavel[descricao] += valor

                for match_total in padrao_total.finditer(linha):
                    totais_encontrados.append(
                        {
                            "pagina": numero_pagina,
                            "chave": match_total.group(1).strip(),
                            "valor_raw": match_total.group(2).strip(),
                            "valor": converter_valor_monetario(match_total.group(2)),
                            "origem": "texto_regex",
                        }
                    )

            tabelas = pagina.extract_tables() or []
            for tabela in tabelas:
                if not tabela:
                    continue

                tabelas_extraidas.append({"pagina": numero_pagina, "linhas": tabela})
                cabecalho = tabela[0] if tabela else []
                linhas_dados = tabela[1:] if len(tabela) > 1 else []

                for linha_tabela in linhas_dados:
                    if not linha_tabela:
                        continue

                    linha_limpa = [str(col).strip() if col else "" for col in linha_tabela]
                    responsavel = next(
                        (
                            col
                            for col in linha_limpa
                            if col and not padrao_valor.search(col) and not re.match(r"^\d{2}/\d{2}/\d{4}$", col)
                        ),
                        "Sem responsavel",
                    )

                    linha_mapeada = {}
                    for idx, coluna in enumerate(linha_limpa):
                        chave = str(cabecalho[idx]).strip() if idx < len(cabecalho) and cabecalho[idx] else f"coluna_{idx+1}"
                        linha_mapeada[chave] = coluna

                    descricao_linha = " | ".join([c for c in linha_limpa if c]) or "Sem descricao"
                    item_total_tabela = linha_tem_total(descricao_linha)
                    valores_encontrados_na_linha = []
                    for celula in linha_limpa:
                        valores_encontrados_na_linha.extend(padrao_valor.findall(celula))

                    for valor_raw in valores_encontrados_na_linha:
                        valor = converter_valor_monetario(valor_raw)
                        chave_item = (numero_pagina, round(valor, 2), normalizar_texto(descricao_linha), "B")

                        if item_total_tabela:
                            totais_encontrados.append(
                                {
                                    "pagina": numero_pagina,
                                    "chave": descricao_linha,
                                    "valor_raw": valor_raw.strip(),
                                    "valor": valor,
                                    "origem": "tabela",
                                }
                            )
                            continue

                        if chave_item in chaves_unicas:
                            continue

                        chaves_unicas.add(chave_item)
                        item = {
                            "pagina": numero_pagina,
                            "origem": "tabela",
                            "responsavel": responsavel,
                            "descricao": descricao_linha,
                            "linha_tabela": linha_mapeada,
                            "valor_raw": valor_raw.strip(),
                            "valor": valor,
                        }
                        itens.append(item)
                        total_lancamentos_calculado += valor
                        total_por_responsavel[responsavel] += valor

    totais_unicos = []
    chaves_total = set()
    for total in totais_encontrados:
        chave = (
            total.get("pagina"),
            normalizar_texto(str(total.get("chave", ""))),
            round(float(total.get("valor", 0.0)), 2),
        )
        if chave in chaves_total:
            continue
        chaves_total.add(chave)
        totais_unicos.append(total)

    for item in itens:
        cat_id, cat_rotulo = classificar_lancamento(str(item.get("descricao", "")), str(item.get("responsavel", "")))
        item["categoria_id"] = cat_id
        item["categoria"] = cat_rotulo

    resumo_por_categoria = montar_resumo_por_categoria(itens)

    return {
        "arquivo_pdf": str(caminho_pdf),
        "qtd_paginas": len(textos_paginas),
        "qtd_itens_extraidos": len(itens),
        "itens": itens,
        "resumo_por_categoria": resumo_por_categoria,
        "totais_identificados_no_pdf": totais_unicos,
        "total_lancamentos_calculado": round(total_lancamentos_calculado, 2),
        "total_por_responsavel": {k: round(v, 2) for k, v in sorted(total_por_responsavel.items())},
        "tabelas_extraidas": tabelas_extraidas,
        "textos_paginas": textos_paginas,
    }


def _truncar_para_celula(texto: str, limite: int = 48000) -> str:
    if len(texto) <= limite:
        return texto
    return texto[: limite - 40] + "\n...[TRUNCADO: celula muito longa]"


def _valor_total_geral_pdf(dados: Dict[str, Any]) -> str:
    totais = dados.get("totais_identificados_no_pdf") or []
    for entrada in totais:
        chave = normalizar_texto(str(entrada.get("chave", "")))
        if "TOTAL" in chave and "GERAL" in chave:
            return str(entrada.get("valor", ""))
    if totais:
        return str(totais[0].get("valor", ""))
    return ""


def _obter_credenciais_google_dict() -> Dict[str, Any]:
    """
    Prioriza credenciais via variavel de ambiente para CI/CD.
    Fallback local: arquivo credentials.json na raiz do projeto.
    """
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
        f"Nao foi possivel obter credenciais Google. "
        f"Defina a variavel {GOOGLE_CREDENTIALS_ENV} (JSON bruto) "
        f"ou {GOOGLE_CREDENTIALS_B64_ENV} (base64), "
        f"ou forneca o arquivo local {CREDENTIALS_JSON}."
    )


def salvar_na_planilha(dados_dicionario: Dict[str, Any]) -> None:
    """Grava uma linha na planilha 'Monitoramento_BAP' usando conta de servico Google."""
    escopos = (
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    )
    credenciais_dict = _obter_credenciais_google_dict()
    credenciais = ServiceAccountCredentials.from_json_keyfile_dict(credenciais_dict, escopos)
    cliente = gspread.authorize(credenciais)

    try:
        planilha = cliente.open(NOME_PLANILHA)
    except SpreadsheetNotFound as erro:
        raise FileNotFoundError(
            f"Planilha '{NOME_PLANILHA}' nao encontrada ou sem permissao. "
            "Crie a planilha com esse nome exato e compartilhe com o e-mail da conta de servico "
            "(client_email no JSON), com permissao de Editor."
        ) from erro

    aba = planilha.sheet1
    cabecalho = [
        "Data da Extração",
        "Arquivo PDF",
        "Total Lançamentos (R$)",
        "Qtd Itens",
        "Qtd Páginas",
        "Total Geral no PDF (R$)",
        "Resumo Categorias (JSON)",
        "Totais PDF (JSON)",
    ]

    primeira_linha = aba.row_values(1)
    linha_cabecalho_atual = [str(c).strip() for c in primeira_linha[: len(cabecalho)]]
    if not primeira_linha or not str(primeira_linha[0]).strip():
        aba.append_row(cabecalho, value_input_option="USER_ENTERED")
    elif linha_cabecalho_atual != cabecalho:
        raise ValueError(
            "A primeira linha da planilha nao corresponde ao cabecalho esperado. "
            f"Esperado (primeiras {len(cabecalho)} colunas): {cabecalho}. "
            f"Encontrado: {linha_cabecalho_atual}"
        )

    resumo_json = json.dumps(dados_dicionario.get("resumo_por_categoria") or {}, ensure_ascii=False)
    totais_json = json.dumps(dados_dicionario.get("totais_identificados_no_pdf") or [], ensure_ascii=False)

    nova_linha = [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        Path(str(dados_dicionario.get("arquivo_pdf", ""))).name,
        dados_dicionario.get("total_lancamentos_calculado"),
        dados_dicionario.get("qtd_itens_extraidos"),
        dados_dicionario.get("qtd_paginas"),
        _valor_total_geral_pdf(dados_dicionario),
        _truncar_para_celula(resumo_json),
        _truncar_para_celula(totais_json),
    ]

    aba.append_row(nova_linha, value_input_option="USER_ENTERED")
    print(f"Linha gravada na planilha '{NOME_PLANILHA}' (aba: {aba.title}).")


def salvar_no_google_sheets(dados: Dict[str, Any]) -> None:
    salvar_na_planilha(dados)


def main() -> None:
    driver = criar_driver()
    try:
        login(driver)
        selecionar_perfil_administrador(driver)
        navegar_ate_relatorio(driver)
        preencher_parametros_relatorio(driver, tipo="Receitas")
        abrir_relatorio_em_nova_janela(driver)

        baixar_pdf(driver)
        pdf_baixado = obter_pdf_mais_recente()
        dados = extrair_dados_do_pdf(pdf_baixado)
        salvar_no_google_sheets(dados)

        print(f"PDF baixado em: {pdf_baixado}")
        print(f"Paginas processadas: {dados['qtd_paginas']}")
        print("Resumo por categoria:")
        print(json.dumps(dados["resumo_por_categoria"], indent=2, ensure_ascii=False))
        print("Dados extraidos:")
        print(json.dumps(dados, indent=2, ensure_ascii=False))
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
