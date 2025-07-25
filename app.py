import os
import re
from typing import List, Optional

import pandas as pd
from bs4 import BeautifulSoup
from fastapi import FastAPI
from pydantic import BaseModel
from playwright.sync_api import sync_playwright

# ---------------------- CONFIG ------------------------------------
CSV_LOCAL = os.getenv("CSV_LOCAL", "produtos_artrin.csv")
TIMEOUT = 120_000
ALVO_SELLER = "foto nascimento"          # normalizado para lower()
DELTA_GAP = 0.01                          # 1% abaixo do concorrente
MAX_SEARCH_PAGES = 1                      # se quiser, aumente para paginar buscas
# ------------------------------------------------------------------

app = FastAPI(title="Monitor Martins Robo", version="1.1.0")


# ========================= MODELOS ================================

class UrlsInput(BaseModel):
    urls: List[str]

class EansInput(BaseModel):
    eans: List[str]


# ========================= HELPERS ================================

def clean_price(txt: str) -> Optional[float]:
    if not txt:
        return None
    # remove tudo que não é número, vírgula ou ponto
    txt = re.sub(r"[^0-9,\.]", "", txt)
    if not txt:
        return None
    # trata casos tipo 1.234,56
    try:
        if txt.count(",") == 1 and txt.count(".") >= 1:
            txt = txt.replace(".", "").replace(",", ".")
        else:
            txt = txt.replace(",", ".")
        return float(txt)
    except Exception:
        return None

def to_float(v):
    try:
        return float(str(v).replace(",", "."))
    except:
        return None

def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def open_page(url: str) -> Optional[str]:
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=TIMEOUT)
            html = page.content()
            browser.close()
        return html
    except Exception as e:
        return None


def parse_product_detail(html: str, url: str):
    """
    Ajuste os seletores abaixo conforme o HTML real da Martins.
    Use o log/print do HTML em um produto real para refinar.
    """
    soup = BeautifulSoup(html, "html.parser")

    # título
    title_el = soup.select_one("h1")  # ajuste se o título tiver outra classe
    title = title_el.get_text(strip=True) if title_el else "Título não encontrado"

    # sellers
    sellers = []
    # tente identificar a área onde os vendedores aparecem
    seller_cards = soup.select(".seller-card")
    if not seller_cards:
        # fallback: alguns marketplaces usam lista/tabela
        seller_cards = soup.select("[data-seller]")

    for card in seller_cards:
        # tente várias possibilidades de seletores
        s_name_el = card.select_one(".seller-name") or card.select_one("[data-seller-name]")
        s_price_el = card.select_one(".seller-price") or card.select_one("[data-seller-price]")

        seller_name = s_name_el.get_text(strip=True) if s_name_el else "Desconhecido"
        price_text = s_price_el.get_text(strip=True) if s_price_el else ""
        price = clean_price(price_text)

        if price is not None:
            sellers.append({"seller": seller_name, "price": price})

    return {
        "produto": title,
        "sku": url.split("/")[-1],
        "url": url,
        "sellers": sellers
    }


def best_price_and_suggestion(rec: dict, my_price: Optional[float] = None) -> dict:
    sellers = sorted(rec.get("sellers", []), key=lambda x: x["price"]) if rec.get("sellers") else []
    if not sellers:
        rec["mais_barato"] = None
        rec["sugestao_preco"] = None
        rec["comentario"] = "Nenhum seller encontrado."
        return rec

    cheapest = sellers[0]
    rec["mais_barato"] = cheapest

    if my_price is None:
        rec["sugestao_preco"] = None
        rec["comentario"] = "Sem preço atual informado. Defina seu preço para sugerirmos ajuste."
        return rec

    cheapest_seller_norm = normalize(cheapest["seller"])
    alvo = round(cheapest["price"] * (1 - DELTA_GAP), 2)

    if cheapest_seller_norm == normalize(ALVO_SELLER):
        if my_price > alvo:
            rec["sugestao_preco"] = alvo
            rec["comentario"] = "Foto Nascimento é o mais barato. Sugestão: cobrir com 1% abaixo."
        else:
            rec["sugestao_preco"] = my_price
            rec["comentario"] = "Você já está abaixo (ou igual) ao preço alvo de 1%."
    else:
        if my_price > alvo:
            rec["sugestao_preco"] = alvo
            rec["comentario"] = "Concorrente genérico é o mais barato. Sugestão: cobrir com 1% abaixo."
        else:
            rec["sugestao_preco"] = my_price
            rec["comentario"] = "Você já está competitivo (<= 1% do concorrente)."

    return rec


def scrape_by_url(url: str, my_price: Optional[float] = None) -> dict:
    html = open_page(url)
    if not html:
        return {"url": url, "erro": "Falha ao carregar a página."}
    rec = parse_product_detail(html, url)
    return best_price_and_suggestion(rec, my_price)


def first_product_url_from_search(ean: str) -> Optional[str]:
    """
    Tenta achar o primeiro resultado de produto a partir de uma busca pelo EAN.
    Ajuste seletores e os formatos de URL de busca se necessário.
    """
    candidates = [
        f"https://www.martinsatacado.com.br/busca/{ean}",
        f"https://www.martinsatacado.com.br/busca?q={ean}"
    ]
    for url in candidates:
        html = open_page(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")

        # Tente capturar o link para o produto
        first_link = soup.select_one("a[href*='/produto/']")
        if first_link and first_link.get("href"):
            href = first_link.get("href")
            if href.startswith("http"):
                return href
            return "https://www.martinsatacado.com.br" + href
    return None


# ========================= ENDPOINTS ==============================

@app.get("/ping")
def ping():
    return {"status": "ok"}


@app.post("/comparar_urls")
def comparar_urls(data: UrlsInput):
    resultados = []
    for url in data.urls:
        resultados.append(scrape_by_url(url))
    return resultados


@app.post("/comparar_por_eans")
def comparar_por_eans(data: EansInput):
    resultados = []
    for ean in data.eans:
        try:
            prod_url = first_product_url_from_search(ean)
            if not prod_url:
                resultados.append({"ean": ean, "erro": "Produto não encontrado na busca."})
                continue
            resultados.append(scrape_by_url(prod_url))
        except Exception as e:
            resultados.append({"ean": ean, "erro": str(e)})
    return resultados


@app.get("/comparar_lista_interna")
def comparar_lista_interna():
    if not os.path.exists(CSV_LOCAL):
        return {"erro": f"Arquivo {CSV_LOCAL} não encontrado no servidor. Suba-o no repositório."}

    df = pd.read_csv(CSV_LOCAL, sep=";", dtype=str).fillna("")

    resultados = []
    for _, row in df.iterrows():
        ean = str(row.get("EAN", "")).strip()
        my_price = to_float(row.get("PREÇO_ATUAL", ""))
        titulo = str(row.get("TÍTULO", "")).strip()
        sku = str(row.get("SKU PRINCIPAL", "")).strip()

        if not ean:
            resultados.append({
                "sku": sku,
                "titulo": titulo,
                "erro": "EAN vazio"
            })
            continue

        try:
            prod_url = first_product_url_from_search(ean)
            if not prod_url:
                resultados.append({
                    "sku": sku,
                    "ean": ean,
                    "titulo": titulo,
                    "preco_atual": my_price,
                    "erro": "Produto não encontrado na busca"
                })
                continue

            rec = scrape_by_url(prod_url, my_price=my_price)
            rec.update({
                "sku": sku,
                "ean": ean,
                "titulo": titulo,
                "preco_atual": my_price
            })
            resultados.append(rec)
        except Exception as e:
            resultados.append({
                "sku": sku,
                "ean": ean,
                "titulo": titulo,
                "preco_atual": my_price,
                "erro": str(e)
            })

    return resultados
