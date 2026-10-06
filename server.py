import os, json, uuid
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
import requests
from dotenv import load_dotenv

load_dotenv()
APP_URL = os.getenv("APP_URL", "http://localhost:8000")
FB_APP_ID = os.getenv("FACEBOOK_APP_ID")
FB_SECRET = os.getenv("FACEBOOK_APP_SECRET")

# --- Bases de datos en memoria ---
users_db = {}      # facebook tokens
profiles_db = {}   # CTAs personalizados por telegram_id
drafts_db = {}     # borradores

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

class PerfilReq(BaseModel):
    telegram_id: int
    nombre: str
    whatsapp: str

# --- COPY CON CTA PERSONALIZADO ---
def generate_copy(raw, tid: str):
    title = raw.get('title', 'Propiedad')
    price = raw.get('price', '')
    desc = raw.get('description', '')
    tag = re.sub(r'[^a-zA-Z0-9]', '', title)[:20]

    perfil = profiles_db.get(tid, {})
    nombre = perfil.get("nombre", "")
    whatsapp = perfil.get("whatsapp", "")

    if whatsapp:
        cta = f"Consultas con {nombre} al {whatsapp}" if nombre else f"Consultas al {whatsapp}"
    else:
        cta = "Consultas por WhatsApp"

    copy_ig = (
        f"{title}\n"
        f"Precio: {price}\n\n"
        f"{desc[:250]}\n\n"
        f"{cta}\n"
        f"#{tag} #Asuncion #Inmuebles #Paraguay"
    )
    copy_fb = (
        f"{title}\n\n"
        f"{desc[:600]}\n\n"
        f"Precio: {price}\n\n"
        f"{cta}\n\n"
        f"#{tag} #Asuncion #Inmuebles #Luque #Paraguay #Inversion"
    )
    return {"ig": copy_ig, "fb": copy_fb}

import re

# --- PERFIL ---
@app.post("/api/perfil")
def set_perfil(req: PerfilReq):
    profiles_db[str(req.telegram_id)] = {
        "nombre": req.nombre,
        "whatsapp": req.whatsapp
    }
    return {"ok": True}

# --- AUTH FACEBOOK ---
@app.get("/auth/login")
def fb_login(telegram_id: str):
    if not FB_APP_ID or FB_APP_ID == "placeholder":
        return HTMLResponse("""
        <html><body style="font-family:sans-serif;padding:40px;max-width:500px;margin:auto">
        <h2>App de Meta no configurada</h2>
        <p>El administrador debe configurar FACEBOOK_APP_ID en Railway.</p>
        </body></html>
        """)
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
    users_db[telegram_id] = {"fb_access_token": access_token, "pages": json.dumps(pages)}
    return HTMLResponse(f"""
    <html><body style="font-family:sans-serif;text-align:center;padding:60px">
    <h2>Conectado correctamente</h2>
    <p>Vinculaste {len(pages)} pagina(s). Podes volver a Telegram.</p>
    <script>setTimeout(()=>window.close(),2000)</script>
    </body></html>
    """)

# --- DRAFTS ---
@app.post("/api/drafts")
def create_draft(payload: CreateDraft):
    raw = payload.raw_data
    tid = str(payload.telegram_id)
    copies = generate_copy(raw, tid)
    draft_id = str(uuid.uuid4())[:8]
    drafts_db[draft_id] = {
        "id": draft_id,
        "telegram_id": tid,
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

# --- PUBLISH ---
@app.post("/api/publish")
def publish(req: PublishReq):
    user = users_db.get(str(req.telegram_id))
    draft = drafts_db.get(req.draft_id)
    if not draft or draft["telegram_id"] != str(req.telegram_id):
        raise HTTPException(404, "Borrador no encontrado")
    if not user:
        raise HTTPException(400, "No conectaste Facebook. Usa el boton del Dashboard.")
    pages = json.loads(user["pages"] or "[]")
    if not pages:
        raise HTTPException(400, "No hay paginas conectadas.")
    page = pages[0]
    page_id = page["id"]
    page_token = page["access_token"]
    images = json.loads(draft["images"] or "[]")
    copy_text = draft["copy_ig"] if "ig" in req.destination else draft["copy_fb"]
    try:
        if req.destination == "fb_feed":
            if images:
                media = requests.post(f"https://graph.facebook.com/v19.0/{page_id}/photos",
                    data={"url": images[0], "caption": copy_text, "access_token": page_token}).json()
            else:
                media = requests.post(f"https://graph.facebook.com/v19.0/{page_id}/feed",
                    data={"message": copy_text + f"\n\n{draft['source_url']}", "access_token": page_token}).json()
            drafts_db[req.draft_id]["status"] = "publicado FB"
            return {"success": True, "post_id": media.get("id"), "raw": media}
        elif req.destination == "ig_feed":
            ig_res = requests.get(f"https://graph.facebook.com/v19.0/{page_id}?fields=instagram_business_account&access_token={page_token}").json()
            ig_id = ig_res.get("instagram_business_account", {}).get("id")
            if not ig_id:
                raise HTTPException(400, "La pagina no tiene Instagram Business vinculado")
            cont = requests.post(f"https://graph.facebook.com/v19.0/{ig_id}/media",
                data={"image_url": images[0] if images else "", "caption": copy_text, "access_token": page_token}).json()
            pub = requests.post(f"https://graph.facebook.com/v19.0/{ig_id}/media_publish",
                data={"creation_id": cont.get("id"), "access_token": page_token}).json()
            drafts_db[req.draft_id]["status"] = "publicado IG"
            return {"success": True, "post_id": pub.get("id"), "raw": pub}
        return {"success": False, "error": "Destino no soportado"}
    except Exception as e:
        return {"success": False, "error": str(e)}

# --- DASHBOARD EJECUTIVO ---
@app.get("/dashboard")
def dashboard(tid: str):
    drafts = [d for d in drafts_db.values() if d["telegram_id"] == tid]
    perfil = profiles_db.get(tid, {})
    nombre_usuario = perfil.get("nombre", f"Usuario {tid[:6]}")
    conectado = tid in users_db

    cards = ""
    for d in reversed(drafts):
        imgs = json.loads(d["images"] or "[]")
        img_tag = f'<img src="{imgs[0]}" style="width:100%;height:180px;object-fit:cover">' if imgs else '<div style="width:100%;height:60px;background:#f5f5f5;display:flex;align-items:center;justify-content:center;color:#999;font-size:13px">Sin imagen</div>'
        publicado = "publicado" in d["status"]
        status_style = "color:#27ae60;font-weight:600" if publicado else "color:#999"

        cards += f"""
        <div style="border:1px solid #e5e5e5;border-radius:4px;overflow:hidden;margin-bottom:16px;background:#fff">
            {img_tag}
            <div style="padding:16px">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px">
                    <div style="font-weight:600;font-size:15px;line-height:1.3;max-width:75%">{d['title'][:80]}</div>
                    <span style="{status_style};font-size:12px;white-space:nowrap;margin-left:8px">{d['status'].upper()}</span>
                </div>
                <div style="color:#444;font-size:13px;margin-bottom:4px">{d['price']}</div>
                <a href="{d['source_url']}" target="_blank" style="color:#666;font-size:12px;text-decoration:none">Ver fuente →</a>

                <div style="margin-top:14px;border-top:1px solid #f0f0f0;padding-top:14px">
                    <div style="font-size:12px;font-weight:600;color:#333;margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px">Copy Instagram</div>
                    <textarea id="ig_{d['id']}" style="width:100%;height:100px;padding:8px;font-size:12px;border:1px solid #e0e0e0;border-radius:3px;resize:vertical;font-family:inherit;box-sizing:border-box">{d['copy_ig']}</textarea>
                </div>

                <div style="margin-top:12px">
                    <div style="font-size:12px;font-weight:600;color:#333;margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px">Copy Facebook</div>
                    <textarea id="fb_{d['id']}" style="width:100%;height:120px;padding:8px;font-size:12px;border:1px solid #e0e0e0;border-radius:3px;resize:vertical;font-family:inherit;box-sizing:border-box">{d['copy_fb']}</textarea>
                </div>

                <div style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap">
                    <button onclick="publicar('{d['id']}','fb_feed','{tid}')"
                        style="background:#1877f2;color:#fff;border:none;padding:8px 14px;border-radius:3px;cursor:pointer;font-size:13px;font-weight:500">
                        Facebook
                    </button>
                    <button onclick="publicar('{d['id']}','ig_feed','{tid}')"
                        style="background:#222;color:#fff;border:none;padding:8px 14px;border-radius:3px;cursor:pointer;font-size:13px;font-weight:500">
                        Instagram
                    </button>
                    <button onclick="descartar('{d['id']}','{tid}')"
                        style="background:#fff;color:#666;border:1px solid #ddd;padding:8px 14px;border-radius:3px;cursor:pointer;font-size:13px">
                        Descartar
                    </button>
                </div>
                <div id="msg_{d['id']}" style="margin-top:8px;font-size:13px"></div>
            </div>
        </div>
        """

    if not cards:
        cards = '<div style="text-align:center;padding:60px 20px;color:#999;font-size:14px">No hay propiedades pendientes.<br>Manda un link al bot de Telegram para empezar.</div>'

    fb_status = '<span style="color:#27ae60">● Facebook conectado</span>' if conectado else f'<a href="{APP_URL}/auth/login?telegram_id={tid}" style="color:#1877f2;font-weight:600;text-decoration:none">→ Conectar Facebook + Instagram</a>'
    perfil_info = f"<span style='color:#666;font-size:13px'>CTA: {perfil.get('nombre','')} {perfil.get('whatsapp','')}</span>" if perfil else f"<span style='color:#999;font-size:12px'>Sin CTA — usa /perfil en Telegram</span>"

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>InmoBot</title>
    <style>
        * {{ box-sizing:border-box; margin:0; padding:0 }}
        body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:#f7f7f7; color:#111; }}
        .top {{ background:#fff; border-bottom:1px solid #e5e5e5; padding:14px 20px; display:flex; justify-content:space-between; align-items:center; position:sticky; top:0; z-index:10 }}
        .top-left {{ font-size:15px; font-weight:700; letter-spacing:-.3px }}
        .top-right {{ font-size:13px; text-align:right; line-height:1.6 }}
        .main {{ max-width:560px; margin:0 auto; padding:20px 16px }}
        .section-title {{ font-size:11px; font-weight:700; color:#999; text-transform:uppercase; letter-spacing:.8px; margin-bottom:14px }}
    </style>
</head>
<body>
    <div class="top">
        <div class="top-left">InmoBot</div>
        <div class="top-right">
            {fb_status}<br>
            {perfil_info}
        </div>
    </div>
    <div class="main">
        <div class="section-title" style="margin-top:20px">Propiedades — {len(drafts)} total</div>
        {cards}
    </div>
    <script>
    async function publicar(id, dest, tid) {{
        const msg = document.getElementById('msg_'+id);
        msg.style.color = '#999'; msg.textContent = 'Publicando...';
        try {{
            const r = await fetch('/api/publish', {{
                method:'POST', headers:{{'Content-Type':'application/json'}},
                body: JSON.stringify({{telegram_id:parseInt(tid), draft_id:id, destination:dest}})
            }});
            const d = await r.json();
            if (d.success) {{ msg.style.color='#27ae60'; msg.textContent='Publicado. Post ID: '+d.post_id; }}
            else {{ msg.style.color='#c0392b'; msg.textContent='Error: '+d.error; }}
        }} catch(e) {{ msg.style.color='#c0392b'; msg.textContent='Error: '+e; }}
    }}
    async function descartar(id, tid) {{
        if (!confirm('Descartar este borrador?')) return;
        await fetch('/api/drafts/'+id+'?telegram_id='+tid, {{method:'DELETE'}});
        location.reload();
    }}
    </script>
</body>
</html>""")

@app.get("/")
def home():
    return {"status": "InmoBot OK"}
