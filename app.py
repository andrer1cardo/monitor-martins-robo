import os
import re
import json
import time
from functools import lru_cache
from typing import List, Optional, Dict, Any

import pandas as pd
from bs4 import BeautifulSoup
from fastapi import FastAPI, Body, HTTPException
from pydantic import BaseModel, Field
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

# ===================== CONFIG =====================
CSV_LOCAL = os.getenv("CSV_LOCAL", "produtos_artrin.csv")
ALVO_SELLER = os.getenv("ALVO_SELLER", "foto nascimento").strip().lower()
DELTA_GAP = float(os.getenv("DELTA_GAP", "0.01"))      # 1% abaixo do concorrente
REQUEST_TIMEOUT_MS = int(os.getenv("REQUEST_TIMEOUT_MS", "120000"))
SEARCH_PAGES_TO_TRY = int(os.getenv("SEARCH_PAGES_TO_TRY", "1"))
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
RATE_LIMIT_SECONDS = float(os.getenv("RATE_LIMIT_SECONDS", "0.4"))  # anti-bloqueio simples
# ==================================================

# ================== FASTAPI SETUP =================
app = FastAPI(
    title="Monitor Martins Robo",
    version="2.0.0",
    description="Robô para comparar preços no marketplace Martins."
)

# ============== PLAYWRIGHT (REUSO GLOBAL) =========
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None

def get_page() -> Page:
    """Obtem uma nova Page usando o contexto global."""
    if _context is None:
        raise RuntimeError("Playwright não inicializado.")
    return _context.new_page()

@app.on_event("startup")
def startup_playwright():
    global _browser, _context, _p
    _p = sync_playwright().start()
    _browser = _p.chromium.launch(headless=HEADLESS)
    _context = _browser.new_context(user_agent=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ))

@app.on_event("shutdown")
def shutdown_playwright():
    global _browser, _context, _p
    try:
        if _context:
            _context.close()
        if _browser:
            _browser.close()
        if _p:
            _p.stop()
    except Exception:
        pass
# ==================================================


# ===================== MODELOS =====================
class UrlsInput(BaseModel):
    urls: List[str] = Field(..., example=[
        "https://www.martinsatacado.com.br/produto/xxxxx",
        "https://www.martinsatacado.com.br/produto/yyyyy"
    ])

class EansInput(BaseModel):
    eans: List[str] = Field(..., example=["7890123456789", "7908156904704"])

class ComparacaoOut(BaseModel):
    sku: Optional[str]
    ean: Optional[str]
    titulo: Optional[str]
    url: Optional[str]
    preco_atual: Optional[float]
    mais_barato: Optional[Dict[str, Any]]
    sellers: Optional[List[Dict[str, Any]]]
    sugestao_preco: Optional[float]
    comentario: Optional[str]
    erro: Optional[str]
# ==================================================


# ==================== HELPERS =====================
def normalize(txt: str) -> str:
    return re.sub(r"\s+", " ", (txt or "").strip().lower())

def clean_price(txt: str) -> Optional[float]:
    if not txt:
        return None
    # remove símbolos
    txt = txt.replace("\xa0", " ")
    txt = re.sub(r"[^\d,\.]", "", txt)
    if not txt:
        return None
    # trata 1.234,56
    try:
        if txt.count(",") == 1 and txt.count(".") >= 1:
            txt = txt.replace(".", "").replace(",", ".")
        else:
            txt = txt.replace(",", ".")
        return float(txt)
    except Exception:
        return None

def to_float(v) -> Optional[float]:
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return None

def sleep_rl():
    """Rate limit simples para não tomar bloqueio do site."""
    time.sleep(RATE_LIMIT_SECONDS)

def extract_from_json_ld(soup: BeautifulSoup) -> Dict[str, Any]:
    """Tenta extrair dados estruturados (JSON-LD)."""
    data = {}
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            parsed = json.loads(tag.string or "{}")
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict) and item.get("@type") in ("Product", "Offer"):
                        data.update(item)
            elif isinstance(parsed, dict) and parsed.get("@type") in ("Product", "Offer"):
                data.update(parsed)
        except Exception:
            continue
    return data

def find_first_product_url_from_search(ean: str) -> Optional[str]:
    """
    Busca por EAN na página de busca e tenta achar o primeiro produto.
    Ajuste os seletores se necessário (depende do HTML real do Martins).
    """
    base_urls = [
        f"https://www.martinsatacado.com.br/busca/{ean}",
        f"https://www.martinsatacado.com.br/busca?q={ean}"
    ]

    for url in base_urls:
        sleep_rl()
        page = get_page()
        try:
            page.goto(url, timeout=REQUEST_TIMEOUT_MS)
            html = page.content()
            soup = BeautifulSoup(html, "html.parser")
            # Tente pegar o primeiro link de produto
            first = soup.select_one("a[href*='/produto/']")
            if first and first.get("href"):
                href = first["href"]
                if href.startswith("http"):
                    return href
                return "https://www.martinsatacado.com.br" + href
        except Exception:
            continue
        finally:
            page.close()
    return None

def parse_product_detail(html: str, url: str) -> Dict[str, Any]:
    """
    Parseia a página do produto: título, EAN, sellers e preços.
    Ajuste de seletores caso o HTML seja diferente.
    """
    soup = BeautifulSoup(html, "html.parser")
    data = {
        "produto": None,
        "ean": None,
        "sellers": []
    }

    # 1) Título
    title_el = (soup.select_one("h1")
                or soup.select_one("[data-testid='product-title']")
                or soup.select_one("h1[class*='title']"))
    if title_el:
        data["produto"] = title_el.get_text(strip=True)

    # 2) JSON-LD (se existir)
    json_ld = extract_from_json_ld(soup)
    if json_ld:
        # título via JSON-LD
        if not data["produto"]:
            data["produto"] = json_ld.get("name")
        # EAN via JSON-LD
        data["ean"] = json_ld.get("gtin13") or json_ld.get("gtin14") or json_ld.get("sku")

    # 3) EAN por texto: "EAN 123..." em algum lugar
    if not data["ean"]:
        possible = soup.find_all(string=re.compile(r"\bEAN\b", re.IGNORECASE))
        for t in possible:
            digits = re.findall(r"(\d{8,14})", t)
            if digits:
                data["ean"] = digits[0]
                break

    # 4) Sellers & preços – tente múltiplos seletores
    seller_blocks = soup.select(".seller-card")
    if not seller_blocks:
        seller_blocks = soup.select("[data-seller], [class*='seller']")

    for block in seller_blocks:
        seller_name_el = (block.select_one(".seller-name")
                          or block.select_one("[data-seller-name]")
                          or block.select_one("[class*='seller']"))
        price_el = (block.select_one(".seller-price")
                    or block.select_one("[data-seller-price]")
                    or block.select_one("[class*='price']"))

        seller_name = (seller_name_el.get_text(strip=True)
                       if seller_name_el else "Desconhecido")
        price_value = clean_price(price_el.get_text(strip=True)) if price_el else None

        if price_value is not None:
            data["sellers"].append({"seller": seller_name, "price": price_value})

    # fallback: se não achou sellers, tente um preço “único” na página
    if not data["sellers"]:
        price_one = soup.select_one(".price, .sales-price, [class*='price']")
        price_value = clean_price(price_one.get_text(strip=True)) if price_one else None
        if price_value is not None:
            data["sellers"].append({"seller": "Martins", "price": price_value})

    # sku (último segmento da URL)
    data["sku"] = url.split("/")[-1]
    data["url"] = url
    return data

def calc_suggestion(cheapest_price: float, my_price: Optional[float]) -> (Optional[float], str):
    """Calcula preço sugerido com base no gap %."""
    if my_price is None:
        return None, "Sem preço atual informado. Defina seu preço para sugerirmos ajuste."
    alvo = round(cheapest_price * (1 - DELTA_GAP), 2)
    if my_price > alvo:
        return alvo, "Sugestão: cobrir o mais barato com 1% abaixo."
    return my_price, "Você já está competitivo (<= 1% do concorrente)."

def enrich_with_strategy(rec: Dict[str, Any], my_price: Optional[float] = None) -> Dict[str, Any]:
    sellers = sorted(rec.get("sellers", []), key=lambda x: x["price"]) if rec.get("sellers") else []
    if not sellers:
        rec["mais_barato"] = None
        rec["sugestao_preco"] = None
        rec["comentario"] = "Nenhum seller encontrado."
        return rec

    cheapest = sellers[0]
    rec["mais_barato"] = cheapest
    cheapest_seller_norm = normalize(cheapest["seller"])

    sugestao, comentario = calc_suggestion(cheapest["price"], my_price)
    rec["sugestao_preco"] = sugestao

    if cheapest_seller_norm == ALVO_SELLER:
        comentario = "Foto Nascimento é o mais barato. " + comentario
    rec["comentario"] = comentario
    return rec

def scrape_product_by_url(url: str, my_price: Optional[float] = None) -> Dict[str, Any]:
    sleep_rl()
    page = get_page()
    try:
        page.goto(url, timeout=REQUEST_TIMEOUT_MS)
        html = page.content()
        parsed = parse_product_detail(html, url)
        parsed = enrich_with_strategy(parsed, my_price)
        return parsed
    except Exception as e:
        return {"url": url, "erro": str(e)}
    finally:
        page.close()

@lru_cache(maxsize=1)
def load_csv() -> pd.DataFrame:
    if not os.path.exists(CSV_LOCAL):
        raise FileNotFoundError(f"Arquivo {CSV_LOCAL} não encontrado.")
    df = pd.read_csv(CSV_LOCAL, sep=";", dtype=str).fillna("")
    return df
# ==================================================


# ===================== ENDPOINTS ==================
@app.get("/ping")
def ping():
    return {"status": "ok"}

@app.post("/comparar_urls", response_model=List[ComparacaoOut])
def comparar_urls(data: UrlsInput):
    out = []
    for url in data.urls:
        rec = scrape_product_by_url(url)
        out.append(rec)
    return out

@app.post("/comparar_por_eans", response_model=List[ComparacaoOut])
def comparar_por_eans(data: EansInput):
    out = []
    for ean in data.eans:
        try:
            prod_url = find_first_product_url_from_search(ean)
            if not prod_url:
                out.append({"ean": ean, "erro": "Produto não encontrado na busca."})
                continue
            rec = scrape_product_by_url(prod_url)
            rec["ean"] = rec.get("ean") or ean
            out.append(rec)
        except Exception as e:
            out.append({"ean": ean, "erro": str(e)})
    return out

@app.get("/comparar_lista_interna", response_model=List[ComparacaoOut])
def comparar_lista_interna():
    try:
        df = load_csv()
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))

    resultados: List[Dict[str, Any]] = []

    def _to_float_safe(x):
        try:
            return float(str(x).replace(",", "."))
        except Exception:
            return None

    for _, row in df.iterrows():
        ean = str(row.get("EAN", "")).strip()
        my_price = _to_float_safe(row.get("PREÇO_ATUAL", ""))
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
            prod_url = find_first_product_url_from_search(ean)
            if not prod_url:
                resultados.append({
                    "sku": sku,
                    "ean": ean,
                    "titulo": titulo,
                    "preco_atual": my_price,
                    "erro": "Produto não encontrado na busca"
                })
                continue

            rec = scrape_product_by_url(prod_url, my_price=my_price)
            rec.update({
                "sku": sku,
                "ean": rec.get("ean") or ean,
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
# ==================================================
