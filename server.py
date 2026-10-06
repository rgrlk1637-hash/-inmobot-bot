import os, json, uuid
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
import requests
from dotenv import load_dotenv

load_dotenv()
APP_URL = os.getenv("APP_URL", "http://localhost:8000")
FB_APP_ID = os.getenv("FACEBOOK_APP_ID")
FB_SECRET = os.getenv("FACEBOOK_APP_SECRET")

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

# --- COPY GENERATOR (sin OpenAI por ahora) ---
def generate_copy(raw):
    title = raw.get('title', 'Propiedad')
    price = raw.get('price', '')
    desc = raw.get('description', '')[:300]
    tag = title.replace(' ', '')[:20]
    
    copy_ig = f"🏠 {title}\n💰 {price}\n\n{desc[:200]}\n\n📲 Consultas por WhatsApp\n#{tag} #Asuncion #Inmuebles #Paraguay"
    copy_fb = f"🏠 {title}\n\n{desc}\n\n💰 Precio: {price}\n\n📲 Escribinos por WhatsApp para más información.\n\n#{tag} #Asuncion #Inmuebles #Luque #Paraguay #Inversión"
    
    return {"ig": copy_ig, "fb": copy_fb}

# --- AUTH FACEBOOK ---
@app.get("/auth/login")
def fb_login(telegram_id: str):
    if not FB_APP_ID:
        return HTMLResponse("<h2>❌ FACEBOOK_APP_ID no configurado en Railway</h2>")
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
    return HTMLResponse(f"""
    <html><body style="font-family:sans-serif;text-align:center;padding:50px">
    <h1>✅ Conectado con éxito!</h1>
    <p>Conectaste {len(pages)} página(s) de Facebook.</p>
    <p>Ya podés volver a Telegram y usar el dashboard para publicar.</p>
    <script>setTimeout(()=>window.close(),3000)</script>
    </body></html>
    """)

# --- DRAFTS API ---
@app.post("/api/drafts")
def create_draft(payload: CreateDraft):
    raw = payload.raw_data
    copies = generate_copy(raw)
    draft_id = str(uuid.uuid4())[:8]
    
    drafts_db[draft_id] = {
        "id": draft_id,
        "telegram_id": str(payload.telegram_id),
        "source_url": payload.source_url,
        "title": raw.get("title", ""),
        "price": raw.get("price", ""),
        "images": json.dumps(raw.get("images", [])),
        "raw_desc": raw.get("description", ""),
        "copy_ig": copies["ig"],
        "copy_fb": copies["fb"],
        "status": "pendiente"
    }
    return {"draft_id": draft_id, "ai_copy": copies["fb"]}

@app.delete("/api/drafts/{draft_id}")
def delete_draft(draft_id: str, telegram_id: int):
    if draft_id in drafts_db and drafts_db[draft_id]["telegram_id"] == str(telegram_id):
        del drafts_db[draft_id]
    return {"ok": True}

# --- PUBLISH API ---
@app.post("/api/publish")
def publish(req: PublishReq):
    user = users_db.get(str(req.telegram_id))
    draft = drafts_db.get(req.draft_id)
    
    if not draft or draft["telegram_id"] != str(req.telegram_id):
        raise HTTPException(404, "Borrador no encontrado")
    if not user:
        raise HTTPException(400, "No conectaste tu Facebook. Usa /conectar en Telegram primero.")
    
    pages = json.loads(user["pages"] or "[]")
    if not pages:
        raise HTTPException(400, "No tenes paginas conectadas.")
    
    page = pages[0]
    page_id = page["id"]
    page_token = page["access_token"]
    images = json.loads(draft["images"] or "[]")

    copy_text = draft["copy_ig"] if "ig" in req.destination else draft["copy_fb"]

    try:
        if req.destination == "fb_feed":
            if images:
                media = requests.post(f"https://graph.facebook.com/v19.0/{page_id}/photos", data={
                    "url": images[0], "caption": copy_text, "access_token": page_token
                }).json()
            else:
                media = requests.post(f"https://graph.facebook.com/v19.0/{page_id}/feed", data={
                    "message": copy_text + f"\n\n{draft['source_url']}", "access_token": page_token
                }).json()
            drafts_db[req.draft_id]["status"] = "publicado_fb"
            return {"success": True, "post_id": media.get("id"), "raw": media}

        elif req.destination == "ig_feed":
            ig_res = requests.get(f"https://graph.facebook.com/v19.0/{page_id}?fields=instagram_business_account&access_token={page_token}").json()
            ig_id = ig_res.get("instagram_business_account", {}).get("id")
            if not ig_id:
                raise HTTPException(400, "Tu pagina no tiene Instagram Business vinculado")
            cont = requests.post(f"https://graph.facebook.com/v19.0/{ig_id}/media", data={
                "image_url": images[0] if images else "",
                "caption": copy_text,
                "access_token": page_token
            }).json()
            pub = requests.post(f"https://graph.facebook.com/v19.0/{ig_id}/media_publish", data={
                "creation_id": cont.get("id"), "access_token": page_token
            }).json()
            drafts_db[req.draft_id]["status"] = "publicado_ig"
            return {"success": True, "post_id": pub.get("id"), "raw": pub}

        return {"success": False, "error": "Destino no soportado"}
    except Exception as e:
        return {"success": False, "error": str(e)}

# --- DASHBOARD HTML ---
@app.get("/dashboard")
def dashboard(tid: str):
    drafts = [d for d in drafts_db.values() if d["telegram_id"] == tid]
    
    cards = ""
    for d in reversed(drafts):
        imgs = json.loads(d["images"] or "[]")
        img_html = f'<img src="{imgs[0]}" style="width:100%;max-height:200px;object-fit:cover;border-radius:8px;margin-bottom:10px">' if imgs else ""
        status_color = "#27ae60" if "publicado" in d["status"] else "#e67e22"
        
        cards += f"""
        <div style="background:#fff;border-radius:12px;padding:20px;margin:16px 0;box-shadow:0 2px 8px rgba(0,0,0,0.1)">
            {img_html}
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <h3 style="margin:0;font-size:16px">{d['title'][:80]}</h3>
                <span style="background:{status_color};color:#fff;padding:4px 10px;border-radius:20px;font-size:12px">{d['status'].upper()}</span>
            </div>
            <p style="color:#666;margin:4px 0">💰 {d['price']}</p>
            <p style="color:#888;font-size:12px">🔗 <a href="{d['source_url']}" target="_blank">Ver fuente</a></p>
            
            <details style="margin-top:12px">
                <summary style="cursor:pointer;color:#3498db;font-weight:bold">📸 Copy para Instagram</summary>
                <textarea id="ig_{d['id']}" style="width:100%;height:120px;margin-top:8px;padding:8px;border-radius:6px;border:1px solid #ddd;font-size:13px;box-sizing:border-box">{d['copy_ig']}</textarea>
            </details>
            
            <details style="margin-top:8px">
                <summary style="cursor:pointer;color:#1877f2;font-weight:bold">📘 Copy para Facebook</summary>
                <textarea id="fb_{d['id']}" style="width:100%;height:140px;margin-top:8px;padding:8px;border-radius:6px;border:1px solid #ddd;font-size:13px;box-sizing:border-box">{d['copy_fb']}</textarea>
            </details>

            <div style="margin-top:14px;display:flex;gap:10px;flex-wrap:wrap">
                <button onclick="publicar('{d['id']}', 'fb_feed', '{tid}')" 
                    style="background:#1877f2;color:#fff;border:none;padding:10px 16px;border-radius:8px;cursor:pointer;font-size:14px">
                    📘 Publicar en Facebook
                </button>
                <button onclick="publicar('{d['id']}', 'ig_feed', '{tid}')" 
                    style="background:linear-gradient(45deg,#f09433,#e6683c,#dc2743,#cc2366,#bc1888);color:#fff;border:none;padding:10px 16px;border-radius:8px;cursor:pointer;font-size:14px">
                    📸 Publicar en Instagram
                </button>
                <button onclick="descartar('{d['id']}', '{tid}')"
                    style="background:#e74c3c;color:#fff;border:none;padding:10px 16px;border-radius:8px;cursor:pointer;font-size:14px">
                    🗑️ Descartar
                </button>
            </div>
            <p id="msg_{d['id']}" style="margin-top:8px;font-weight:bold;color:#27ae60"></p>
        </div>
        """

    if not cards:
        cards = "<div style='text-align:center;padding:60px;color:#888'><h3>No hay propiedades pendientes</h3><p>Manda un link al bot de Telegram para empezar.</p></div>"

    conectar_url = f"{APP_URL}/auth/login?telegram_id={tid}"
    
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>InmoBot Dashboard</title>
        <style>
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f0f2f5; min-height: 100vh; }}
            .header {{ background: linear-gradient(135deg, #1a1a2e, #16213e); color: #fff; padding: 20px; text-align: center; }}
            .header h1 {{ font-size: 24px; }}
            .header p {{ font-size: 13px; color: #aaa; margin-top: 4px; }}
            .connect-btn {{ display:inline-block;margin-top:12px;background:#1877f2;color:#fff;padding:10px 20px;border-radius:8px;text-decoration:none;font-size:14px; }}
            .container {{ max-width: 600px; margin: 0 auto; padding: 16px; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🏠 InmoBot Dashboard</h1>
            <p>Revisa, edita y publica tus propiedades</p>
            <a href="{conectar_url}" class="connect-btn">🔗 Conectar Facebook + Instagram</a>
        </div>
        <div class="container">
            {cards}
        </div>
        <script>
        async function publicar(draftId, destino, tid) {{
            const msg = document.getElementById('msg_' + draftId);
            msg.style.color = '#e67e22';
            msg.textContent = 'Publicando...';
            try {{
                const r = await fetch('/api/publish', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{telegram_id: parseInt(tid), draft_id: draftId, destination: destino}})
                }});
                const data = await r.json();
                if (data.success) {{
                    msg.style.color = '#27ae60';
                    msg.textContent = '✅ Publicado correctamente! ID: ' + data.post_id;
                }} else {{
                    msg.style.color = '#e74c3c';
                    msg.textContent = '❌ Error: ' + data.error;
                }}
            }} catch(e) {{
                msg.style.color = '#e74c3c';
                msg.textContent = '❌ Error de conexion: ' + e;
            }}
        }}
        async function descartar(draftId, tid) {{
            if (!confirm('¿Seguro que querés descartar este borrador?')) return;
            await fetch('/api/drafts/' + draftId + '?telegram_id=' + tid, {{method: 'DELETE'}});
            location.reload();
        }}
        </script>
    </body>
    </html>
    """)

@app.get("/")
def home():
    return {"status": "InmoBot OK"}
