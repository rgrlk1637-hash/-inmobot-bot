import os, re, json, logging
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
APP_URL = os.getenv("APP_URL", "http://localhost:8000")
API_URL = f"{APP_URL}/api"

logging.basicConfig(level=logging.INFO)

# --- SCRAPER ---
def scrape_property(url: str):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        def og(prop):
            tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
            return tag["content"] if tag and tag.get("content") else ""
        title = og("og:title") or (soup.title.string if soup.title else "Propiedad")
        desc = og("og:description") or ""
        images = [m["content"] for m in soup.find_all("meta", property="og:image") if m.get("content")]
        price_match = re.search(r"(USD|U\$S|\$|Gs\.)\s?[\d\.,]+", r.text)
        price = price_match.group(0) if price_match else ""
        return {"url": url, "title": title[:200], "description": desc[:1000], "images": images[:10], "price": price}
    except Exception as e:
        return {"url": url, "title": "Propiedad", "description": "", "images": [], "price": "", "error": str(e)}

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    await update.message.reply_text(
        "InmoBot\n\n"
        "Comandos disponibles:\n"
        "/perfil Nombre | +595981234567  - Configura tu CTA personal\n"
        "/conectar - Conecta tu Facebook e Instagram\n\n"
        "Para procesar una propiedad, mandame el link directamente.\n\n"
        f"Dashboard: {APP_URL}/dashboard?tid={tid}"
    )

async def perfil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    args = " ".join(context.args) if context.args else ""
    if "|" not in args:
        await update.message.reply_text(
            "Formato: /perfil Nombre | +595981234567\n\n"
            "Ejemplo: /perfil Juan Perez | +595981234567\n\n"
            "Esto aparecera en todos los copies que generes."
        )
        return
    partes = args.split("|", 1)
    nombre = partes[0].strip()
    whatsapp = partes[1].strip()
    try:
        resp = requests.post(f"{API_URL}/perfil", json={
            "telegram_id": tid,
            "nombre": nombre,
            "whatsapp": whatsapp
        }, timeout=10)
        await update.message.reply_text(
            f"Perfil guardado:\nNombre: {nombre}\nWhatsApp: {whatsapp}\n\n"
            "Tus proximas publicaciones van a incluir tu contacto."
        )
    except Exception as e:
        await update.message.reply_text(f"Error guardando perfil: {e}")

async def conectar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    link = f"{APP_URL}/auth/login?telegram_id={tid}"
    await update.message.reply_text(
        "Para publicar en Facebook e Instagram, conecta tu cuenta:\n\n"
        f"{link}\n\n"
        "Acepta los permisos y vuelve aca."
    )

async def handle_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    urls = re.findall(r'https?://\S+', text)
    if not urls:
        await update.message.reply_text("No veo links. Mandame un link de propiedad.")
        return

    await update.message.reply_text(f"Procesando {len(urls)} propiedad(es)...")

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
            resultados.append(f"{prop['title'][:50]} ({n_fotos} foto(s))")
        except Exception as e:
            resultados.append(f"Error - {url[:40]}: {e}")

    resumen = "\n".join(f"- {r}" for r in resultados)
    await update.message.reply_text(
        f"Listo. {len(urls)} propiedad(es) procesada(s):\n\n{resumen}\n\n"
        f"Revisa y publica desde el Dashboard:\n{APP_URL}/dashboard?tid={update.effective_user.id}"
    )

if __name__ == "__main__":
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("perfil", perfil))
    app.add_handler(CommandHandler("conectar", conectar))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_links))
    print("Bot corriendo...")
    app.run_polling()