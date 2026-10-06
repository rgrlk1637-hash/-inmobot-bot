import os, re, json, asyncio, logging
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
APP_URL = os.getenv("APP_URL", "http://localhost:8000")
API_URL = f"{APP_URL}/api"

logging.basicConfig(level=logging.INFO)

# --- SCRAPER GENÉRICO ---
def scrape_property(url: str):
    """Extrae OG tags de cualquier portal inmobiliario"""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        def og(prop):
            tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
            return tag["content"] if tag and tag.get("content") else ""
        
        title = og("og:title") or (soup.title.string if soup.title else "Propiedad")
        desc = og("og:description") or ""
        image = og("og:image") or ""
        images = [m["content"] for m in soup.find_all("meta", property="og:image") if m.get("content")]
        if not images and image:
            images = [image]
        
        price_match = re.search(r"(USD|U\$S|\$|Gs\.)\s?[\d\.,]+", r.text)
        price = price_match.group(0) if price_match else ""

        return {
            "url": url,
            "title": title[:200],
            "description": desc[:1000],
            "images": images[:10],
            "price": price
        }
    except Exception as e:
        return {"url": url, "title": "Propiedad", "description": "", "images": [], "price": "", "error": str(e)}

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏠 InmoBot Generico\n\n"
        "1. Conecta tu Facebook/IG con /conectar\n"
        "2. Reenvíame 1 o varios links de propiedades (uno por línea)\n"
        "3. Te genero el copy y las fotos. Revisa todo en el Dashboard y publica.\n\n"
        f"Dashboard: {APP_URL}/dashboard?tid={update.effective_user.id}"
    )

async def conectar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    link = f"{APP_URL}/auth/login?telegram_id={telegram_id}"
    await update.message.reply_text(
        "Para publicar en tu cuenta de Facebook e Instagram, conectala una sola vez:\n\n"
        f"{link}\n\n"
        "Acepta los permisos y vuelve aca."
    )

async def handle_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    urls = re.findall(r'https?://\S+', text)
    if not urls:
        await update.message.reply_text("No veo links. Mandame links como https://...")
        return

    await update.message.reply_text(f"Recibi {len(urls)} link(s). Procesando...")

    resultados = []
    for url in urls:
        prop = scrape_property(url)
        try:
            resp = requests.post(f"{API_URL}/drafts", json={
                "telegram_id": update.effective_user.id,
                "source_url": url,
                "raw_data": prop
            }, timeout=30)
            data = resp.json()
            draft_id = data.get("draft_id", "?")
            n_fotos = len(prop.get("images", []))
            resultados.append(f"OK - {prop['title'][:50]} ({n_fotos} fotos)")
        except Exception as e:
            resultados.append(f"Error - {url[:40]}: {e}")

    resumen = "\n".join(f"- {r}" for r in resultados)
    dashboard_url = f"{APP_URL}/dashboard?tid={update.effective_user.id}"
    
    await update.message.reply_text(
        f"Listo! Procese {len(urls)} propiedad(es):\n\n"
        f"{resumen}\n\n"
        f"Revisa los copies y publica desde el Dashboard:\n{dashboard_url}"
    )

if __name__ == "__main__":
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("conectar", conectar))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_links))
    print("Bot corriendo...")
    app.run_polling()