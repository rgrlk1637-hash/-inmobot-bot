import os, re, json, asyncio, logging
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
        soup = BeautifulSoup(r.text, 'lxml')
        def og(prop):
            tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
            return tag["content"] if tag and tag.get("content") else ""
        
        title = og("og:title") or (soup.title.string if soup.title else "Propiedad")
        desc = og("og:description") or ""
        image = og("og:image") or ""
        # buscar todas las og:image
        images = [m["content"] for m in soup.find_all("meta", property="og:image") if m.get("content")]
        if not images and image:
            images = [image]
        
        # precio simple con regex
        price_match = re.search(r"(USD|U\$S|\$)\s?[\d\.\,]+", r.text)
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
        "🏠 *InmoBot Genérico*\n\n"
        "1. Conectá tu Facebook/IG con /conectar\n"
        "2. Reenviame 1 o 20 links de propiedades (uno por línea)\n"
        "3. Te dejo todo en borradores y publicas aviso por aviso donde quieras: FB Feed, IG Feed, Reel o Historia.\n\n"
        "Es multi-usuario, cada uno publica en SU cuenta.",
        parse_mode="Markdown"
    )

async def conectar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    # Link firmado para vincular Telegram ID con FB OAuth
    link = f"{APP_URL}/auth/login?telegram_id={telegram_id}"
    kb = [[InlineKeyboardButton("🔗 Conectar Facebook + Instagram", url=link)]]
    await update.message.reply_text(
        "Para que pueda publicar en TU cuenta, conectala una sola vez:\n\n"
        f"{link}\n\n"
        "Te va a pedir permisos de Page e Instagram. Acepta y vuelve acá.",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def handle_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    urls = re.findall(r'https?://\S+', text)
    if not urls:
        await update.message.reply_text("No veo links. Mandame links como https://...")
        return

    await update.message.reply_text(f"🔍 Recibí {len(urls)} links. Procesando borradores...")

    for url in urls:
        prop = scrape_property(url)
        
        # Llamar a tu backend para crear borrador + copy IA
        try:
            resp = requests.post(f"{API_URL}/drafts", json={
                "telegram_id": update.effective_user.id,
                "source_url": url,
                "raw_data": prop
            }, timeout=30)
            data = resp.json()
            draft_id = data.get("draft_id")
            
            # Preview en Telegram
            caption = (
                f"📝 *Borrador #{draft_id}*\n"
                f"*{prop['title']}*\n"
                f"{prop['price']}\n\n"
                f"{data.get('ai_copy','')[:800]}\n\n"
                f"Fuente: {url}"
            )
            kb = [
                [InlineKeyboardButton("📘 Publicar FB Feed", callback_data=f"pub:fb_feed:{draft_id}"),
                 InlineKeyboardButton("📸 IG Feed", callback_data=f"pub:ig_feed:{draft_id}")],
                [InlineKeyboardButton("🎬 IG Reel", callback_data=f"pub:ig_reel:{draft_id}"),
                 InlineKeyboardButton("⭕ IG Historia", callback_data=f"pub:ig_story:{draft_id}")],
                [InlineKeyboardButton("✏️ Editar Copy", callback_data=f"edit:{draft_id}"),
                 InlineKeyboardButton("🗑️ Descartar", callback_data=f"del:{draft_id}")],
                [InlineKeyboardButton("👀 Ver Dashboard", url=f"{APP_URL}/dashboard?tid={update.effective_user.id}")]
            ]
            # Si hay imagen, mandar con foto
            if prop["images"]:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=prop["images"][0],
                    caption=caption,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(kb)
                )
            else:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=caption,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(kb)
                )
        except Exception as e:
            await update.message.reply_text(f"❌ Error con {url}: {e}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, target, draft_id = q.data.split(":")
    
    if action == "pub":
        await q.edit_message_caption(caption=q.message.caption + f"\n\n⏳ Publicando en {target}...")
        try:
            r = requests.post(f"{API_URL}/publish", json={
                "telegram_id": q.from_user.id,
                "draft_id": draft_id,
                "destination": target
            }, timeout=60)
            res = r.json()
            if res.get("success"):
                await q.edit_message_caption(caption=q.message.caption + f"\n\n✅ Publicado en {target}!\nID: {res.get('post_id')}")
            else:
                await context.bot.send_message(chat_id=q.message.chat_id, text=f"❌ Error: {res.get('error')}")
        except Exception as e:
            await context.bot.send_message(chat_id=q.message.chat_id, text=f"❌ Error publicando: {e}")
    elif action == "del":
        requests.delete(f"{API_URL}/drafts/{draft_id}", params={"telegram_id": q.from_user.id})
        await q.edit_message_caption(caption="🗑️ Borrador descartado")

if __name__ == "__main__":
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("conectar", conectar))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_links))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("Bot corriendo...")
    app.run_polling()
