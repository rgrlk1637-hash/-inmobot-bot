import os, re, asyncio, logging
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup
from seguridad import bot_key
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
APP_URL = os.getenv("APP_URL", "http://localhost:8000")
API_URL = f"{APP_URL}/api"

logging.basicConfig(level=logging.INFO)


# --- SCRAPER ---
# Solo se leen datos del aviso original (titulo, descripcion, precio, fotos).
# El servidor extrae unicamente hechos (tipo, zona, dormitorios...) y descarta el resto.
def scrape_property(url: str):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        def og(prop):
            tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
            return tag["content"] if tag and tag.get("content") else ""

        title = og("og:title") or (soup.title.string if soup.title and soup.title.string else "Propiedad")
        desc = og("og:description") or ""
        images = [m["content"] for m in soup.find_all("meta", property="og:image") if m.get("content")]
        price_match = re.search(r"(USD|U\$S|US\$|\$|Gs\.)\s?[\d\.,]+", r.text)
        price = price_match.group(0) if price_match else ""
        return {"url": url, "title": title[:200], "description": desc[:1000], "images": images[:10], "price": price}
    except Exception as e:
        return {"url": url, "title": "Propiedad", "description": "", "images": [], "price": "", "error": str(e)}


def _post(path, payload, timeout=30):
    return requests.post(f"{API_URL}{path}", json=payload, timeout=timeout,
                         headers={"X-Bot-Key": bot_key()})


# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    try:
        r = await asyncio.to_thread(_post, "/registro", {
            "telegram_id": u.id, "first_name": u.first_name or "", "username": u.username or ""}, 10)
    except Exception as e:
        await update.message.reply_text(f"No pude registrarte ahora ({e}). Probá de nuevo en un minuto.")
        return
    if r.status_code == 403:
        await update.message.reply_text("Este bot es privado. Pedile acceso al administrador.")
        return
    data = r.json()
    if data.get("tiene_wa"):
        await update.message.reply_text(
            f"Hola {data.get('nombre') or ''}! Ya estás registrado.\n\n"
            "Mandame uno o varios links de propiedades y te armo los avisos.\n\n"
            f"Tu tablero (enlace personal, no lo compartas): {data.get('dash')}\n\n"
            "Comandos: /perfil para cambiar tu nombre o WhatsApp · /conectar para vincular Facebook e Instagram."
        )
        return
    context.user_data["esperando_wa"] = True
    await update.message.reply_text(
        "Bienvenido a InmoBot · LLAVE.IA\n\n"
        "Para que cada aviso salga con TU contacto, escribime tu número de WhatsApp.\n"
        "Ejemplo: 0981 123 456"
    )


async def perfil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = update.effective_user.id
    args = " ".join(context.args) if context.args else ""
    if "|" not in args:
        await update.message.reply_text(
            "Formato: /perfil Nombre | 0981 123 456\n\n"
            "Ejemplo: /perfil Juan Perez | 0981 123 456\n\n"
            "Esto aparecerá en todos los avisos que generes."
        )
        return
    nombre, whatsapp = [x.strip() for x in args.split("|", 1)]
    await _guardar_wa(update, nombre, whatsapp)


async def _guardar_wa(update: Update, nombre: str, whatsapp: str):
    u = update.effective_user
    try:
        r = await asyncio.to_thread(_post, "/perfil", {
            "telegram_id": u.id, "nombre": nombre or "", "whatsapp": whatsapp}, 10)
    except Exception as e:
        await update.message.reply_text(f"Error guardando tu perfil: {e}")
        return False
    if r.status_code != 200:
        await update.message.reply_text("Ese número no parece válido. Probá así: 0981 123 456")
        return False
    d = r.json()
    await update.message.reply_text(
        f"Listo, {d.get('nombre') or 'perfil guardado'}.\nWhatsApp: {d['whatsapp']}\n\n"
        "Ahora mandame uno o varios links de propiedades.\n"
        f"Tu tablero (enlace personal, no lo compartas): {d.get('dash')}"
    )
    return True


async def conectar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    try:
        r = await asyncio.to_thread(_post, "/registro", {"telegram_id": u.id, "first_name": u.first_name or "", "username": u.username or ""}, 10)
        r.raise_for_status()
    except Exception:
        await update.message.reply_text("No pude generar tu enlace ahora. Probá de nuevo en un minuto.")
        return
    await update.message.reply_text(
        "Para publicar en Facebook e Instagram, conectá tu cuenta:\n\n"
        f"{r.json()['login_url']}\n\n"
        "Aceptá los permisos y volvé acá."
    )


def _procesar_link(tid, url):
    prop = scrape_property(url)
    resp = _post("/drafts", {"telegram_id": tid, "source_url": url, "raw_data": prop})
    resp.raise_for_status()
    return resp.json()


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    urls = re.findall(r"https?://\S+", text)

    # Esperando el WhatsApp tras /start
    if context.user_data.get("esperando_wa") and not urls:
        if await _guardar_wa(update, "", text):
            context.user_data["esperando_wa"] = False
        return

    if not urls:
        await update.message.reply_text("No veo links. Mandame un link de propiedad (https://…).")
        return

    await update.message.reply_text(f"Procesando {len(urls)} propiedad(es)…")
    tid = update.effective_user.id
    lineas, dash = [], ""
    for url in urls:
        try:
            d = await asyncio.to_thread(_procesar_link, tid, url)
            dash = d.get("dash") or dash
            nota = f"{d['score']}/100 · {d['n_fotos']} foto(s)"
            consejo = f"\n   💡 {d['tips'][0]}" if d.get("tips") else ""
            lineas.append(f"✅ {d['titulo'][:60]} ({nota}){consejo}")
        except Exception as e:
            lineas.append(f"❌ {url[:40]}: {e}")

    await update.message.reply_text(
        "Listo:\n\n" + "\n".join(lineas) +
        f"\n\nRevisá, elegí el estilo del texto y publicá desde tu tablero:\n{dash}"
    )


if __name__ == "__main__":
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("perfil", perfil))
    app.add_handler(CommandHandler("conectar", conectar))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("Bot corriendo...")
    app.run_polling()
