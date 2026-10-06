import os, json, uuid, re
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
import requests
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, String, Text, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()
APP_URL = os.getenv("APP_URL", "http://localhost:8000")
FB_APP_ID = os.getenv("FACEBOOK_APP_ID")
FB_SECRET = os.getenv("FACEBOOK_APP_SECRET")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# --- BASE DE DATOS ---
# Railway provee DATABASE_URL automáticamente cuando agregás PostgreSQL.
# Si no hay DB, usamos SQLite local como fallback.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL if DATABASE_URL else "sqlite:///./inmobot.db", echo=False)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class Profile(Base):
    __tablename__ = "profiles"
    telegram_id  = Column(String, primary_key=True)
    nombre       = Column(String, default="")
    whatsapp     = Column(String, default="")

class FbUser(Base):
    __tablename__ = "fb_users"
    telegram_id  = Column(String, primary_key=True)
    access_token = Column(Text)
    pages_json   = Column(Text, default="[]")

class Draft(Base):
    __tablename__ = "drafts"
    id          = Column(String, primary_key=True)
    telegram_id = Column(String, index=True)
    source_url  = Column(Text)
    title       = Column(Text)
    price       = Column(String)
    images_json = Column(Text, default="[]")
    raw_desc    = Column(Text)
    copy_ig     = Column(Text)
    copy_fb     = Column(Text)
    status      = Column(String, default="pendiente")   # pendiente | publicado FB | publicado IG | descartado
    destination = Column(String, default="")
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    published_at= Column(DateTime, nullable=True)

Base.metadata.create_all(engine)

app = FastAPI()

# --- MODELOS PYDANTIC ---
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

# --- HELPERS ---
def get_db():
    db = SessionLocal()
    try:
        return db
    except:
        db.close()
        raise

def generate_copy(raw: dict, tid: str) -> dict:
    db = SessionLocal()
    try:
        perfil = db.query(Profile).filter_by(telegram_id=tid).first()
        nombre   = perfil.nombre   if perfil else ""
        whatsapp = perfil.whatsapp if perfil else ""
    finally:
        db.close()

    title = raw.get("title", "Propiedad")
    price = raw.get("price", "")
    desc  = raw.get("description", "")
    tag   = re.sub(r"[^a-zA-Z0-9]", "", title)[:20]

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

def stats_mes(db, telegram_id: str) -> dict:
    """Devuelve conteos del mes en curso para ese usuario."""
    now = datetime.now(timezone.utc)
    inicio_mes = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    base = db.query(Draft).filter(
        Draft.telegram_id == telegram_id,
        Draft.published_at >= inicio_mes
    )
    total_fb = base.filter(Draft.destination == "fb_feed").count()
    total_ig = base.filter(Draft.destination == "ig_feed").count()
    return {"fb": total_fb, "ig": total_ig, "total": total_fb + total_ig, "mes": now.strftime("%B %Y")}

# --- PERFIL ---
@app.post("/api/perfil")
def set_perfil(req: PerfilReq):
    db = SessionLocal()
    try:
        p = db.query(Profile).filter_by(telegram_id=str(req.telegram_id)).first()
        if not p:
            p = Profile(telegram_id=str(req.telegram_id))
        p.nombre = req.nombre
        p.whatsapp = req.whatsapp
        db.add(p); db.commit()
        return {"ok": True}
    finally:
        db.close()

# --- AUTH FACEBOOK ---
@app.get("/auth/login")
def fb_login(telegram_id: str):
    if not FB_APP_ID or FB_APP_ID == "placeholder":
        return HTMLResponse("<html><body style='font-family:sans-serif;padding:40px'><h2>App de Meta no configurada aun.</h2></body></html>")
    scopes = "pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish,business_management"
    redirect_uri = f"{APP_URL}/auth/callback"
    state = f"{telegram_id}|{uuid.uuid4()}"
    url = f"https://www.facebook.com/v19.0/dialog/oauth?client_id={FB_APP_ID}&redirect_uri={redirect_uri}&scope={scopes}&state={state}&response_type=code"
    return RedirectResponse(url)

@app.get("/auth/callback")
def fb_callback(code: str, state: str):
    telegram_id = state.split("|")[0]
    redirect_uri = f"{APP_URL}/auth/callback"
    tok = requests.get(
        f"https://graph.facebook.com/v19.0/oauth/access_token"
        f"?client_id={FB_APP_ID}&redirect_uri={redirect_uri}&client_secret={FB_SECRET}&code={code}"
    ).json()
    access_token = tok.get("access_token")
    if not access_token:
        raise HTTPException(400, f"Error: {tok}")
    pages = requests.get(f"https://graph.facebook.com/v19.0/me/accounts?access_token={access_token}").json().get("data", [])
    db = SessionLocal()
    try:
        u = db.query(FbUser).filter_by(telegram_id=telegram_id).first()
        if not u:
            u = FbUser(telegram_id=telegram_id)
        u.access_token = access_token
        u.pages_json = json.dumps(pages)
        db.add(u); db.commit()
    finally:
        db.close()
    return HTMLResponse(f"<html><body style='font-family:sans-serif;text-align:center;padding:60px'><h2>Conectado</h2><p>{len(pages)} pagina(s) vinculadas. Podes cerrar esta ventana.</p><script>setTimeout(()=>window.close(),2000)</script></body></html>")

# --- DRAFTS ---
@app.post("/api/drafts")
def create_draft(payload: CreateDraft):
    raw = payload.raw_data
    tid = str(payload.telegram_id)
    copies = generate_copy(raw, tid)
    draft_id = str(uuid.uuid4())[:8]
    db = SessionLocal()
    try:
        d = Draft(
            id=draft_id, telegram_id=tid, source_url=payload.source_url,
            title=raw.get("title",""), price=raw.get("price",""),
            images_json=json.dumps(raw.get("images",[])),
            raw_desc=raw.get("description",""),
            copy_ig=copies["ig"], copy_fb=copies["fb"]
        )
        db.add(d); db.commit()
        return {"draft_id": draft_id, "ai_copy": copies["fb"]}
    finally:
        db.close()

@app.delete("/api/drafts/{draft_id}")
def delete_draft(draft_id: str, telegram_id: int):
    db = SessionLocal()
    try:
        db.query(Draft).filter_by(id=draft_id, telegram_id=str(telegram_id)).delete()
        db.commit()
        return {"ok": True}
    finally:
        db.close()

# --- PUBLISH ---
@app.post("/api/publish")
def publish(req: PublishReq):
    db = SessionLocal()
    try:
        user  = db.query(FbUser).filter_by(telegram_id=str(req.telegram_id)).first()
        draft = db.query(Draft).filter_by(id=req.draft_id, telegram_id=str(req.telegram_id)).first()
        if not draft:
            raise HTTPException(404, "Borrador no encontrado")
        if not user:
            raise HTTPException(400, "No conectaste Facebook. Usa el boton Conectar del Dashboard.")
        pages = json.loads(user.pages_json or "[]")
        if not pages:
            raise HTTPException(400, "No hay paginas conectadas.")
        page       = pages[0]
        page_id    = page["id"]
        page_token = page["access_token"]
        images     = json.loads(draft.images_json or "[]")
        copy_text  = draft.copy_ig if "ig" in req.destination else draft.copy_fb
        if req.destination == "fb_feed":
            if images:
                media = requests.post(f"https://graph.facebook.com/v19.0/{page_id}/photos",
                    data={"url": images[0], "caption": copy_text, "access_token": page_token}).json()
            else:
                media = requests.post(f"https://graph.facebook.com/v19.0/{page_id}/feed",
                    data={"message": copy_text + f"\n\n{draft.source_url}", "access_token": page_token}).json()
            draft.status      = "publicado FB"
            draft.destination = "fb_feed"
            draft.published_at = datetime.now(timezone.utc)
            db.commit()
            return {"success": True, "post_id": media.get("id")}
        elif req.destination == "ig_feed":
            ig_res = requests.get(f"https://graph.facebook.com/v19.0/{page_id}?fields=instagram_business_account&access_token={page_token}").json()
            ig_id  = ig_res.get("instagram_business_account", {}).get("id")
            if not ig_id:
                raise HTTPException(400, "La pagina no tiene Instagram Business vinculado")
            cont = requests.post(f"https://graph.facebook.com/v19.0/{ig_id}/media",
                data={"image_url": images[0] if images else "", "caption": copy_text, "access_token": page_token}).json()
            pub  = requests.post(f"https://graph.facebook.com/v19.0/{ig_id}/media_publish",
                data={"creation_id": cont.get("id"), "access_token": page_token}).json()
            draft.status       = "publicado IG"
            draft.destination  = "ig_feed"
            draft.published_at = datetime.now(timezone.utc)
            db.commit()
            return {"success": True, "post_id": pub.get("id")}
        return {"success": False, "error": "Destino no soportado"}
    except HTTPException:
        raise
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        db.close()

# --- DASHBOARD ---
@app.get("/dashboard")
def dashboard(tid: str):
    db = SessionLocal()
    try:
        drafts  = db.query(Draft).filter_by(telegram_id=tid).order_by(Draft.created_at.desc()).all()
        perfil  = db.query(Profile).filter_by(telegram_id=tid).first()
        fb_user = db.query(FbUser).filter_by(telegram_id=tid).first()
        st      = stats_mes(db, tid)
    finally:
        db.close()

    nombre_cta = f"{perfil.nombre} · {perfil.whatsapp}" if perfil and perfil.whatsapp else "Sin CTA configurado — usa /perfil en Telegram"
    fb_status  = "● FB conectado" if fb_user else f'<a href="{APP_URL}/auth/login?telegram_id={tid}" style="color:#1877f2;font-weight:600;text-decoration:none">→ Conectar Facebook + Instagram</a>'

    # --- STATS HEADER ---
    stats_html = f"""
    <div style="display:flex;gap:24px;align-items:flex-end;margin-top:12px">
        <div>
            <div style="font-size:42px;font-weight:700;line-height:1;color:#fff">{st['total']}</div>
            <div style="font-size:11px;color:rgba(255,255,255,.6);text-transform:uppercase;letter-spacing:.6px;margin-top:2px">Avisos publicados en {st['mes']}</div>
        </div>
        <div style="padding-bottom:4px;display:flex;gap:20px">
            <div>
                <div style="font-size:20px;font-weight:600;color:#fff">{st['fb']}</div>
                <div style="font-size:11px;color:rgba(255,255,255,.55)">Facebook</div>
            </div>
            <div>
                <div style="font-size:20px;font-weight:600;color:#fff">{st['ig']}</div>
                <div style="font-size:11px;color:rgba(255,255,255,.55)">Instagram</div>
            </div>
        </div>
    </div>
    """

    # --- CARDS ---
    cards = ""
    for d in drafts:
        imgs = json.loads(d.images_json or "[]")
        img_tag = f'<img src="{imgs[0]}" style="width:100%;height:180px;object-fit:cover">' if imgs else \
                  '<div style="width:100%;height:50px;background:#f5f5f5;display:flex;align-items:center;justify-content:center;color:#bbb;font-size:12px">Sin imagen</div>'
        publicado  = "publicado" in (d.status or "")
        descartado = "descartado" in (d.status or "")
        if publicado:
            badge = "<span style='color:#27ae60;font-size:11px;font-weight:600'>PUBLICADO</span>"
        elif descartado:
            badge = "<span style='color:#bbb;font-size:11px'>DESCARTADO</span>"
        else:
            badge = "<span style='color:#e67e22;font-size:11px;font-weight:600'>PENDIENTE</span>"

        pub_fecha = f"<div style='font-size:11px;color:#bbb;margin-top:2px'>{d.published_at.strftime('%d/%m/%Y %H:%M')} UTC</div>" if d.published_at else ""

        cards += f"""
        <div style="border:1px solid #e5e5e5;border-radius:4px;overflow:hidden;margin-bottom:16px;background:#fff">
            {img_tag}
            <div style="padding:16px">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px">
                    <div style="font-weight:600;font-size:14px;line-height:1.3;max-width:72%">{d.title[:80]}</div>
                    <div style="text-align:right">{badge}{pub_fecha}</div>
                </div>
                <div style="color:#555;font-size:13px;margin-bottom:3px">{d.price}</div>
                <a href="{d.source_url}" target="_blank" style="color:#999;font-size:11px;text-decoration:none">Ver fuente →</a>

                <div style="margin-top:14px;border-top:1px solid #f0f0f0;padding-top:12px">
                    <div style="font-size:11px;font-weight:700;color:#333;margin-bottom:5px;text-transform:uppercase;letter-spacing:.5px">Copy Instagram</div>
                    <textarea id="ig_{d.id}" style="width:100%;height:95px;padding:8px;font-size:12px;border:1px solid #e0e0e0;border-radius:3px;resize:vertical;font-family:inherit;box-sizing:border-box">{d.copy_ig}</textarea>
                </div>
                <div style="margin-top:10px">
                    <div style="font-size:11px;font-weight:700;color:#333;margin-bottom:5px;text-transform:uppercase;letter-spacing:.5px">Copy Facebook</div>
                    <textarea id="fb_{d.id}" style="width:100%;height:115px;padding:8px;font-size:12px;border:1px solid #e0e0e0;border-radius:3px;resize:vertical;font-family:inherit;box-sizing:border-box">{d.copy_fb}</textarea>
                </div>
                <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
                    <button onclick="publicar('{d.id}','fb_feed','{tid}')"
                        style="background:#1877f2;color:#fff;border:none;padding:8px 14px;border-radius:3px;cursor:pointer;font-size:13px;font-weight:500">
                        Facebook
                    </button>
                    <button onclick="publicar('{d.id}','ig_feed','{tid}')"
                        style="background:#111;color:#fff;border:none;padding:8px 14px;border-radius:3px;cursor:pointer;font-size:13px;font-weight:500">
                        Instagram
                    </button>
                    <button onclick="descartar('{d.id}','{tid}')"
                        style="background:#fff;color:#888;border:1px solid #ddd;padding:8px 14px;border-radius:3px;cursor:pointer;font-size:13px">
                        Descartar
                    </button>
                </div>
                <div id="msg_{d.id}" style="margin-top:8px;font-size:13px"></div>
            </div>
        </div>"""

    if not cards:
        cards = '<div style="text-align:center;padding:60px 20px;color:#aaa;font-size:14px">Sin propiedades aun.<br>Manda un link al bot de Telegram para empezar.</div>'

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>InmoBot</title>
    <style>
        *{{box-sizing:border-box;margin:0;padding:0}}
        body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f7f7f7;color:#111}}
        .top{{background:#111;padding:18px 20px}}
        .top-meta{{display:flex;justify-content:space-between;align-items:center}}
        .logo{{font-size:14px;font-weight:700;color:#fff;letter-spacing:-.3px}}
        .top-right{{font-size:12px;color:rgba(255,255,255,.6);text-align:right;line-height:1.7}}
        .main{{max-width:560px;margin:0 auto;padding:20px 16px}}
        .sec{{font-size:11px;font-weight:700;color:#999;text-transform:uppercase;letter-spacing:.8px;margin-bottom:14px;margin-top:4px}}
    </style>
</head>
<body>
    <div class="top">
        <div class="top-meta">
            <div class="logo">InmoBot</div>
            <div class="top-right">{fb_status}<br><span style="color:rgba(255,255,255,.45);font-size:11px">{nombre_cta}</span></div>
        </div>
        {stats_html}
    </div>
    <div class="main">
        <div class="sec" style="margin-top:20px">Propiedades · {len(drafts)} registros</div>
        {cards}
    </div>
    <script>
    async function publicar(id,dest,tid){{
        const msg=document.getElementById('msg_'+id);
        msg.style.color='#999';msg.textContent='Publicando...';
        try{{
            const r=await fetch('/api/publish',{{method:'POST',headers:{{'Content-Type':'application/json'}},
                body:JSON.stringify({{telegram_id:parseInt(tid),draft_id:id,destination:dest}})}});
            const d=await r.json();
            if(d.success){{msg.style.color='#27ae60';msg.textContent='Publicado. ID: '+d.post_id;setTimeout(()=>location.reload(),2000);}}
            else{{msg.style.color='#c0392b';msg.textContent='Error: '+d.error;}}
        }}catch(e){{msg.style.color='#c0392b';msg.textContent='Error: '+e;}}
    }}
    async function descartar(id,tid){{
        if(!confirm('Descartar este borrador?'))return;
        await fetch('/api/drafts/'+id+'?telegram_id='+tid,{{method:'DELETE'}});
        location.reload();
    }}
    </script>
</body>
</html>""")

@app.get("/")
def home():
    return {"status": "InmoBot OK"}
