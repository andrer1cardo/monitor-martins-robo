
from fastapi import FastAPI
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

app = FastAPI()

class UrlsInput(BaseModel):
    urls: list[str]

def scrape_produto(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, timeout=120000)
        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    produto = soup.select_one("h1").get_text(strip=True) if soup.select_one("h1") else "Título não encontrado"
    sellers = []
    for card in soup.select(".seller-card"):
        seller_name = card.select_one(".seller-name").get_text(strip=True) if card.select_one(".seller-name") else "Desconhecido"
        price_text = card.select_one(".seller-price").get_text(strip=True) if card.select_one(".seller-price") else "0"
        try:
            price = float(price_text.replace("R$", "").replace(".", "").replace(",", "."))
        except:
            price = 0.0
        sellers.append({"seller": seller_name, "price": price})

    return {"produto": produto, "sku": url.split("/")[-1], "sellers": sellers}

@app.post("/buscar_varios")
def buscar_varios(data: UrlsInput):
    resultados = []
    for url in data.urls:
        try:
            resultados.append(scrape_produto(url))
        except Exception as e:
            resultados.append({"url": url, "erro": str(e)})
    return resultados
