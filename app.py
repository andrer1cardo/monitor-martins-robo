from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import pandas as pd
import os

app = FastAPI()

# --- CONFIG ---------------------------------------------------------
CSV_LOCAL = "produtos_artrin.csv"  # suba esse arquivo no repositório
TIMEOUT = 120_000
ALVO_SELLER = "foto nascimento"  # normalizamos para lower()
DELTA_GAP = 0.01  # 1% abaixo do concorrente
# -------------------------------------------------------------------

class UrlsInput(BaseModel):
    urls: List[str]

class EansInput(BaseModel):
    eans: List[str]

def _open_page(url: str) -> Optional[str]:
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

def _parse_produto_detail(html: str, url: str):
    soup = BeautifulSoup(html, "html.parser")

    # --- Ajuste esses seletores conforme o HTML real do Martins ----
    title = soup.select_one("h1").get_text(strip=True) if soup.select_one("h1") else "Título não encontrado"
    sellers = []
    for card in soup.select(".seller-card"):
        seller_name = (card.select_one(".seller-name").get_text(strip=True)
                       if card.select_one(".seller-name") else "Desconhecido")
        price_text = (card.select_one(".seller-price").get_text(strip=True)
                      if card.select_one(".seller-price") else "0")
        try:
            price = float(price_text.replace("R$", "").replace(".", "").replace(",", "."))
        except:
            price = 0.0
        sellers.append({"seller": seller_name, "price": price})
    # ----------------------------------------------------------------

    return {
        "produto": title,
        "sku": url.split("/")[-1],
        "url": url,
        "sellers": sellers
    }

def _best_price_and_suggestion(rec, my_price: Optional[float] = None):
    sellers = sorted(rec["sellers"], key=lambda x: x["price"]) if rec["sellers"] else []
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

    if cheapest["seller"].lower().strip() == ALVO_SELLER:
        alvo = round(cheapest["price"] * (1 - DELTA_GAP), 2)
        if my_price > alvo:
            rec["sugestao_preco"] = alvo
            rec["comentario"] = "Foto Nascimento é o mais barato. Sugestão: cobrir com 1% abaixo."
        else:
            rec["sugestao_preco"] = my_price
            rec["comentario"] = "Você já está abaixo (ou igual) ao preço alvo de 1%."
    else:
        alvo = round(cheapest["price"] * (1 - DELTA_GAP), 2)
        if my_price > alvo:
            rec["sugestao_preco"] = alvo
            rec["comentario"] = "Concorrente genérico é o mais barato. Sugestão: cobrir com 1% abaixo."
        else:
            rec["sugestao_preco"] = my_price
            rec["comentario"] = "Você já está competitivo (<= 1% do concorrente)."

    return rec

def _scrape_by_url(url, my_price: Optional[float] = None):
    html = _open_page(url)
    if not html:
        return {"url": url, "erro": "Falha ao carregar a página."}
    rec = _parse_produto_detail(html, url)
    return _best_price_and_suggestion(rec, my_price)

def _find_first_product_url_from_search(ean: str) -> Optional[str]:
    # 1) tenta /busca/<ean>
    search_url = f"https://www.martinsatacado.com.br/busca/{ean}"
    html = _open_page(search_url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")

    # Tente ajustar este seletor para o link do primeiro produto na busca
    first_link = soup.select_one("a[href*='/produto/']")
    if first_link and first_link.get("href"):
        href = first_link.get("href")
        if href.startswith("http"):
            return href
        return "https://www.martinsatacado.com.br" + href
    return None

@app.get("/ping")
def ping():
    return {"status": "ok"}

@app.post("/comparar_urls")
def comparar_urls(data: UrlsInput):
    resultados = []
    for url in data.urls:
        resultados.append(_scrape_by_url(url))
    return resultados

@app.post("/comparar_por_eans")
def comparar_por_eans(data: EansInput):
    resultados = []
    for ean in data.eans:
        try:
            prod_url = _find_first_product_url_from_search(ean)
            if not prod_url:
                resultados.append({"ean": ean, "erro": "Produto não encontrado na busca."})
                continue
            resultados.append(_scrape_by_url(prod_url))
        except Exception as e:
            resultados.append({"ean": ean, "erro": str(e)})
    return resultados

@app.get("/comparar_lista_interna")
def comparar_lista_interna():
    if not os.path.exists(CSV_LOCAL):
        return {"erro": f"Arquivo {CSV_LOCAL} não encontrado no servidor. Suba-o no repositório."}

    df = pd.read_csv(CSV_LOCAL, sep=";", dtype=str).fillna("")
    # Tenta converter preço
    def to_float(v):
        try:
            return float(str(v).replace(",", "."))
        except:
            return None

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
            prod_url = _find_first_product_url_from_search(ean)
            if not prod_url:
                resultados.append({
                    "sku": sku,
                    "ean": ean,
                    "titulo": titulo,
                    "preco_atual": my_price,
                    "erro": "Produto não encontrado na busca"
                })
                continue

            rec = _scrape_by_url(prod_url, my_price=my_price)
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
