import os, json, uuid, time
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
APP_URL = os.getenv("APP_URL")
FB_APP_ID = os.getenv("FACEBOOK_APP_ID")
FB_SECRET = os.getenv("FACEBOOK_APP_SECRET")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_KEY) if OPENAI_KEY else None

# --- In-Memory Database ---
users_db = {}
drafts_db = {}

app = FastAPI()

# --- MODELOS ---
class CreateDraft(BaseModel):
    telegram_id: int
    source_url: str
    raw_data: dict

class PublishReq(BaseModel):
    telegram_id: int
    draft_id: str
    destination: str

# --- IA COPY ---
def generate_copy(raw):
    if not client:
        return f"🏠 {raw.get('title')}\n{raw.get('description','')[:300]}\n\n📲 Consultas por WhatsApp\n#{raw.get('title','').replace(' ','')[:20]} #Asuncion #Inmuebles"
    
    prompt = f"""
    Sos copywriter inmobiliario experto en Paraguay. Datos:
    Título: {raw.get('title')}
    Precio: {raw.get('price')}
    Desc: {raw.get('description')}
    Link: {raw.get('url')}
    Crea un copy corto, humano, no genérico, vendedor, con 1-2 emojis, CTA a WhatsApp, y 4 hashtags locales.
    No inventes ambientes que no están.
    Formato: 3 párrafos máximo.
    """
    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}],
            max_tokens=300
        )
        return r.choices[0].message.content
    except:
        return f"{raw.get('title')} - {raw.get('price')}\n{raw.get('description','')[:300]}"

# --- AUTH FACEBOOK ---
@app.get("/auth/login")
def fb_login(telegram_id: str):
    scopes = "pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish,business_management"
    redirect_uri = f"{APP_URL}/auth/callback"
    state = f"{telegram_id}|{uuid.uuid4()}"
    url = f"https://www.facebook.com/v19.0/dialog/oauth?client_id={FB_APP_ID}&redirect_uri={redirect_uri}&scope={scopes}&state={state}&response_type=code"
    return RedirectResponse(url)

@app.get("/auth/callback")
def fb_callback(code: str, state: str):
    telegram_id = state.split("|")[0]
    redirect_uri = f"{APP_URL}/auth/callback"
    token_url = f"https://graph.facebook.com/v19.0/oauth/access_token?client_id={FB_APP_ID}&redirect_uri={redirect_uri}&client_secret={FB_SECRET}&code={code}"
    tok = requests.get(token_url).json()
    access_token = tok.get("access_token")
    if not access_token:
        raise HTTPException(400, f"Error token: {tok}")

    pages_res = requests.get(f"https://graph.facebook.com/v19.0/me/accounts?access_token={access_token}").json()
    pages = pages_res.get("data", [])

    users_db[telegram_id] = {
        "telegram_id": telegram_id,
        "fb_access_token": access_token,
        "pages": json.dumps(pages)
    }

    return HTMLResponse(f"<h1>✅ Conectado!</h1><p>Ya podés volver a Telegram. Conectaste {len(pages)} página(s).</p><script>window.close()</script>")

# --- DRAFTS ---
@app.post("/api/drafts")
def create_draft(payload: CreateDraft):
    raw = payload.raw_data
    ai_copy = generate_copy(raw)
    draft_id = str(uuid.uuid4())[:8]
    
    drafts_db[draft_id] = {
        "id": draft_id,
        "telegram_id": str(payload.telegram_id),
        "source_url": payload.source_url,
        "title": raw.get("title",""),
        "price": raw.get("price",""),
        "images": json.dumps(raw.get("images",[])),
        "raw_desc": raw.get("description",""),
        "ai_copy": ai_copy,
        "status": "draft"
    }
    return {"draft_id": draft_id, "ai_copy": ai_copy}

@app.delete("/api/drafts/{draft_id}")
def delete_draft(draft_id: str, telegram_id: int):
    if draft_id in drafts_db and drafts_db[draft_id]["telegram_id"] == str(telegram_id):
        del drafts_db[draft_id]
    return {"ok": True}

# --- PUBLISH ---
@app.post("/api/publish")
def publish(req: PublishReq):
    user = users_db.get(str(req.telegram_id))
    draft = drafts_db.get(req.draft_id)
    
    if not user or not draft or draft["telegram_id"] != str(req.telegram_id):
        raise HTTPException(404, "Usuario o borrador no encontrado")
    
    pages = json.loads(user["pages"] or "[]")
    if not pages:
        raise HTTPException(400, "No tenés páginas conectadas. Hacé /conectar")
    
    page = pages[0]
    page_id = page["id"]
    page_token = page["access_token"]
    images = json.loads(draft["images"] or "[]")

    try:
        if req.destination == "fb_feed":
            if images:
                media = requests.post(f"https://graph.facebook.com/v19.0/{page_id}/photos", data={
                    "url": images[0],
                    "caption": draft["ai_copy"],
                    "access_token": page_token
                }).json()
            else:
                media = requests.post(f"https://graph.facebook.com/v19.0/{page_id}/feed", data={
                    "message": draft["ai_copy"] + f"\n\n{ draft['source_url'] }",
                    "access_token": page_token
                }).json()
            drafts_db[req.draft_id]["status"] = "published_fb"
            return {"success": True, "post_id": media.get("id") or media.get("post_id"), "raw": media}

        elif req.destination.startswith("ig_"):
            ig_res = requests.get(f"https://graph.facebook.com/v19.0/{page_id}?fields=instagram_business_account&access_token={page_token}").json()
            ig_id = ig_res.get("instagram_business_account", {}).get("id")
            if not ig_id:
                raise HTTPException(400, "Tu página no tiene Instagram Business vinculado")

            if req.destination == "ig_feed":
                cont = requests.post(f"https://graph.facebook.com/v19.0/{ig_id}/media", data={
                    "image_url": images[0] if images else "",
                    "caption": draft["ai_copy"],
                    "access_token": page_token
                }).json()
                pub = requests.post(f"https://graph.facebook.com/v19.0/{ig_id}/media_publish", data={
                    "creation_id": cont.get("id"),
                    "access_token": page_token
                }).json()
                return {"success": True, "post_id": pub.get("id"), "raw": pub}

            elif req.destination == "ig_story":
                cont = requests.post(f"https://graph.facebook.com/v19.0/{ig_id}/media", data={
                    "image_url": images[0] if images else "",
                    "media_type": "STORIES",
                    "access_token": page_token
                }).json()
                pub = requests.post(f"https://graph.facebook.com/v19.0/{ig_id}/media_publish", data={
                    "creation_id": cont.get("id"),
                    "access_token": page_token
                }).json()
                return {"success": True, "post_id": pub.get("id")}

            elif req.destination == "ig_reel":
                video_url = images[0] if images else ""
                cont = requests.post(f"https://graph.facebook.com/v19.0/{ig_id}/media", data={
                    "media_type": "REELS",
                    "video_url": video_url,
                    "caption": draft["ai_copy"],
                    "access_token": page_token
                }).json()
                return {"success": True, "post_id": cont.get("id"), "raw": cont, "note": "Reel en proceso, tarda unos minutos"}

        return {"success": False, "error": "Destino no soportado"}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/dashboard")
def dashboard(tid: str):
    drafts = [d for d in drafts_db.values() if d["telegram_id"] == tid]
    html = "<h1>Tus borradores</h1>"
    for d in reversed(drafts):
        html += f"<div style='border:1px solid #ccc;padding:10px;margin:10px'><b>{d['title']}</b> - {d['status']}<br>{d['ai_copy'][:300]}<br><small>{d['source_url']}</small></div>"
    return HTMLResponse(html)

@app.get("/")
def home():
    return {"status": "InmoBot OK"}
