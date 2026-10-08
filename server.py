import os, json, uuid, re, time, threading, calendar, secrets, hmac
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse, Response, JSONResponse
from pydantic import BaseModel
import requests
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, String, Text, DateTime, Integer, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from copywriter import extraer_datos, generar_variantes, evaluar, normalizar_wa
from seguridad import bot_key, firmar_state, state_valido

load_dotenv()
APP_URL = os.getenv("APP_URL", "http://localhost:8000")
FB_APP_ID = os.getenv("FACEBOOK_APP_ID")
FB_SECRET = os.getenv("FACEBOOK_APP_SECRET")
DATABASE_URL = os.getenv("DATABASE_URL", "")
GRAPH = "https://graph.facebook.com/v19.0"
# Si se define, solo estos Telegram IDs pueden registrarse (separados por coma). Vacio = abierto.
ALLOWED_IDS = {x.strip() for x in os.getenv("ALLOWED_IDS", "").split(",") if x.strip()}
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE_DIR, "static")

# Paraguay usa UTC-3 todo el año (sin horario de verano desde 2024).
TZ = timezone(timedelta(hours=-3))
MESES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto",
         "Septiembre", "Octubre", "Noviembre", "Diciembre"]

# --- BASE DE DATOS ---
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL if DATABASE_URL else "sqlite:///./inmobot.db", echo=False)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


def ahora():
    return datetime.now(timezone.utc)


class Profile(Base):
    __tablename__ = "profiles"
    telegram_id = Column(String, primary_key=True)
    nombre = Column(String, default="")
    whatsapp = Column(String, default="")
    username = Column(String, default="")
    created_at = Column(DateTime, default=ahora)
    token = Column(String, default="")      # codigo secreto del enlace al tablero


class FbUser(Base):
    __tablename__ = "fb_users"
    telegram_id = Column(String, primary_key=True)
    access_token = Column(Text)
    pages_json = Column(Text, default="[]")


class Draft(Base):
    __tablename__ = "drafts"
    id = Column(String, primary_key=True)
    telegram_id = Column(String, index=True)
    source_url = Column(Text)
    title = Column(Text)
    price = Column(String)
    images_json = Column(Text, default="[]")
    raw_desc = Column(Text)           # ya no se guarda el texto original (queda vacio)
    copy_ig = Column(Text)
    copy_fb = Column(Text)
    status = Column(String, default="pendiente")   # pendiente | publicado
    destination = Column(String, default="")
    created_at = Column(DateTime, default=ahora)
    published_at = Column(DateTime, nullable=True)
    datos_json = Column(Text, default="")          # hechos extraidos (tipo, zona, ...)
    variantes_json = Column(Text, default="")      # emocional / directa x ig / fb


class Publicacion(Base):
    """Una fila por cada vez que un aviso se publica en una red."""
    __tablename__ = "publicaciones"
    id = Column(Integer, primary_key=True, autoincrement=True)
    draft_id = Column(String, index=True)
    telegram_id = Column(String, index=True)
    destino = Column(String)          # fb_feed | ig_feed
    post_id = Column(String, default="")
    published_at = Column(DateTime, default=ahora)
    likes = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    shares = Column(Integer, default=0)
    metrics_at = Column(DateTime, nullable=True)


def migrar():
    """Crea tablas y agrega columnas nuevas a tablas existentes (sin perder datos)."""
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    with engine.begin() as con:
        for tabla in Base.metadata.sorted_tables:
            existentes = {c["name"] for c in insp.get_columns(tabla.name)}
            for col in tabla.columns:
                if col.name not in existentes:
                    tipo = col.type.compile(dialect=engine.dialect)
                    con.execute(text(f'ALTER TABLE {tabla.name} ADD COLUMN {col.name} {tipo}'))
    # Avisos publicados antes de existir la tabla de publicaciones
    db = SessionLocal()
    try:
        con_pub = {p[0] for p in db.query(Publicacion.draft_id).distinct()}
        for d in db.query(Draft).filter(Draft.published_at.isnot(None)).all():
            if d.id not in con_pub:
                db.add(Publicacion(draft_id=d.id, telegram_id=d.telegram_id,
                                   destino=d.destination or "fb_feed", published_at=d.published_at))
        db.commit()
    finally:
        db.close()


migrar()


# --- ACCESO ---
def nuevo_token() -> str:
    return secrets.token_urlsafe(12)


def link_tablero(tid, token) -> str:
    return f"{APP_URL}/dashboard?tid={tid}&k={token}"


def link_conectar(tid, token) -> str:
    return f"{APP_URL}/auth/login?telegram_id={tid}&k={token}"


def asegurar_token(db, p):
    if not p.token:
        p.token = nuevo_token()
        db.add(p)
        db.commit()
    return p.token


def requiere_bot(x_bot_key: str = Header(default="")):
    """Los endpoints que usa el bot exigen su clave interna (si el servidor tiene SECRET_KEY o token de bot)."""
    esperada = bot_key()
    if esperada and not hmac.compare_digest(x_bot_key, esperada):
        raise HTTPException(403, "No autorizado")


def verificar(db, tid, k):
    p = db.query(Profile).filter_by(telegram_id=str(tid)).first()
    if not p or not p.token or not hmac.compare_digest(p.token, k or ""):
        raise HTTPException(403, "Enlace invalido o vencido. Escribile /start al bot para recibir uno nuevo.")
    return p


_db = SessionLocal()
try:
    for _p in _db.query(Profile).filter((Profile.token.is_(None)) | (Profile.token == "")).all():
        _p.token = nuevo_token()
    _db.commit()
finally:
    _db.close()

# --- PERFILES PRECARGADOS ---
# perfiles.json (o env PERFILES_JSON): [{"telegram_id": "123"|"PENDIENTE", "nombre": "...", "whatsapp": "..."}]
# Con telegram_id numerico se fija el perfil. Sin ID, el nombre se asigna cuando la persona
# se registra con /start y escribe ese mismo WhatsApp.
NOMBRES_POR_WA = {}


def cargar_perfiles():
    raw = os.getenv("PERFILES_JSON")
    if not raw:
        ruta = os.path.join(BASE_DIR, "perfiles.json")
        raw = open(ruta, encoding="utf-8").read() if os.path.exists(ruta) else "[]"
    try:
        items = json.loads(raw)
    except ValueError:
        return
    db = SessionLocal()
    try:
        for it in items:
            wa = normalizar_wa(it.get("whatsapp", ""))
            if not wa:
                continue
            tid = str(it.get("telegram_id", "")).strip()
            if tid.isdigit():
                p = db.query(Profile).filter_by(telegram_id=tid).first() or Profile(telegram_id=tid)
                p.nombre, p.whatsapp = it.get("nombre", ""), wa
                p.token = p.token or nuevo_token()
                db.add(p)
            else:
                NOMBRES_POR_WA[wa] = it.get("nombre", "")
        db.commit()
    finally:
        db.close()


cargar_perfiles()

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
    texto: Optional[str] = None
    k: str = ""


class PerfilReq(BaseModel):
    telegram_id: int
    nombre: str = ""
    whatsapp: str


class RegistroReq(BaseModel):
    telegram_id: int
    first_name: str = ""
    username: str = ""


class RefreshReq(BaseModel):
    telegram_id: int
    k: str = ""


# --- FECHAS ---
def local(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def mes_key(dt) -> str:
    l = local(dt)
    return f"{l.year:04d}-{l.month:02d}"


def mes_label(key: str) -> str:
    a, m = key.split("-")
    return f"{MESES[int(m) - 1]} {a}"


def mes_prev(key: str) -> str:
    a, m = int(key[:4]), int(key[5:])
    return f"{a - 1}-12" if m == 1 else f"{a}-{m - 1:02d}"


def _json(s, defecto):
    try:
        return json.loads(s) if s else defecto
    except ValueError:
        return defecto


# --- REGISTRO / PERFIL ---
@app.post("/api/registro", dependencies=[Depends(requiere_bot)])
def registro(req: RegistroReq):
    tid = str(req.telegram_id)
    if ALLOWED_IDS and tid not in ALLOWED_IDS:
        raise HTTPException(403, "Acceso restringido")
    db = SessionLocal()
    try:
        p = db.query(Profile).filter_by(telegram_id=tid).first()
        nuevo = p is None
        if nuevo:
            p = Profile(telegram_id=tid, nombre=req.first_name, whatsapp="", username=req.username)
            db.add(p)
            db.commit()
        tk = asegurar_token(db, p)
        return {"nuevo": nuevo, "nombre": p.nombre, "whatsapp": p.whatsapp, "tiene_wa": bool(p.whatsapp),
                "dash": link_tablero(tid, tk), "login_url": link_conectar(tid, tk)}
    finally:
        db.close()


@app.post("/api/perfil", dependencies=[Depends(requiere_bot)])
def set_perfil(req: PerfilReq):
    wa = normalizar_wa(req.whatsapp)
    if not wa:
        raise HTTPException(400, "WhatsApp invalido. Ejemplo: 0981 123 456")
    tid = str(req.telegram_id)
    if ALLOWED_IDS and tid not in ALLOWED_IDS:
        raise HTTPException(403, "Acceso restringido")
    db = SessionLocal()
    try:
        p = db.query(Profile).filter_by(telegram_id=tid).first() or Profile(telegram_id=tid)
        # nombre: el indicado, o el precargado para ese WhatsApp, o el que ya tenia
        p.nombre = req.nombre.strip() or NOMBRES_POR_WA.get(wa) or p.nombre or ""
        p.whatsapp = wa
        db.add(p)
        db.commit()
        return {"ok": True, "nombre": p.nombre, "whatsapp": wa, "dash": link_tablero(tid, asegurar_token(db, p))}
    finally:
        db.close()


# --- AUTH FACEBOOK ---
@app.get("/auth/login")
def fb_login(telegram_id: str, k: str = ""):
    db = SessionLocal()
    try:
        verificar(db, telegram_id, k)
    finally:
        db.close()
    if not FB_APP_ID or FB_APP_ID == "placeholder":
        return HTMLResponse("<html><body style='font-family:sans-serif;padding:40px'><h2>App de Meta no configurada aun.</h2></body></html>")
    scopes = "pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish,business_management"
    redirect_uri = f"{APP_URL}/auth/callback"
    state = firmar_state(telegram_id, uuid.uuid4().hex[:12])
    url = f"https://www.facebook.com/v19.0/dialog/oauth?client_id={FB_APP_ID}&redirect_uri={redirect_uri}&scope={scopes}&state={state}&response_type=code"
    return RedirectResponse(url)


@app.get("/auth/callback")
def fb_callback(code: str, state: str):
    telegram_id = state_valido(state)
    if not telegram_id:
        raise HTTPException(400, "Solicitud invalida. Volve a intentar desde el bot con /conectar.")
    redirect_uri = f"{APP_URL}/auth/callback"
    tok = requests.get(
        f"{GRAPH}/oauth/access_token"
        f"?client_id={FB_APP_ID}&redirect_uri={redirect_uri}&client_secret={FB_SECRET}&code={code}"
    ).json()
    access_token = tok.get("access_token")
    if not access_token:
        raise HTTPException(400, f"Error: {tok}")
    pages = requests.get(f"{GRAPH}/me/accounts?access_token={access_token}").json().get("data", [])
    db = SessionLocal()
    try:
        u = db.query(FbUser).filter_by(telegram_id=telegram_id).first()
        if not u:
            u = FbUser(telegram_id=telegram_id)
        u.access_token = access_token
        u.pages_json = json.dumps(pages)
        db.add(u)
        db.commit()
    finally:
        db.close()
    return HTMLResponse(f"<html><body style='font-family:sans-serif;text-align:center;padding:60px'><h2>Conectado</h2><p>{len(pages)} pagina(s) vinculadas. Podes cerrar esta ventana.</p><script>setTimeout(()=>window.close(),2000)</script></body></html>")


# --- DRAFTS ---
@app.post("/api/drafts", dependencies=[Depends(requiere_bot)])
def create_draft(payload: CreateDraft):
    raw = payload.raw_data
    tid = str(payload.telegram_id)
    datos = extraer_datos(raw)
    imagenes = [u for u in raw.get("images", []) if isinstance(u, str) and u.startswith("http")][:10]
    db = SessionLocal()
    try:
        perfil = db.query(Profile).filter_by(telegram_id=tid).first()
        nombre = perfil.nombre if perfil else ""
        wa = perfil.whatsapp if perfil else ""
        variantes = generar_variantes(datos, nombre, wa)
        base = variantes["emocional"]
        score, tips = evaluar(datos, len(imagenes), base["ig"], bool(wa))
        draft_id = str(uuid.uuid4())[:8]
        db.add(Draft(
            id=draft_id, telegram_id=tid, source_url=payload.source_url,
            title=datos["titulo"], price=datos["precio"],
            images_json=json.dumps(imagenes), raw_desc="",
            copy_ig=base["ig"], copy_fb=base["fb"],
            datos_json=json.dumps(datos, ensure_ascii=False),
            variantes_json=json.dumps(variantes, ensure_ascii=False),
        ))
        db.commit()
        dash = link_tablero(tid, asegurar_token(db, perfil)) if perfil else ""
        return {"draft_id": draft_id, "dash": dash, "ai_copy": base["fb"], "score": score, "tips": tips[:3],
                "titulo": datos["titulo"], "n_fotos": len(imagenes)}
    finally:
        db.close()


@app.delete("/api/drafts/{draft_id}")
def delete_draft(draft_id: str, telegram_id: int, k: str = ""):
    db = SessionLocal()
    try:
        verificar(db, telegram_id, k)
        db.query(Draft).filter_by(id=draft_id, telegram_id=str(telegram_id)).delete()
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# --- PUBLISH ---
def _error_graph(resp: dict, defecto: str) -> str:
    return (resp.get("error") or {}).get("message") or defecto


@app.post("/api/publish")
def publish(req: PublishReq):
    db = SessionLocal()
    try:
        verificar(db, req.telegram_id, req.k)
        user = db.query(FbUser).filter_by(telegram_id=str(req.telegram_id)).first()
        draft = db.query(Draft).filter_by(id=req.draft_id, telegram_id=str(req.telegram_id)).first()
        if not draft:
            raise HTTPException(404, "Borrador no encontrado")
        if not user:
            raise HTTPException(400, "No conectaste Facebook. Usa el boton Conectar del Dashboard.")
        pages = _json(user.pages_json, [])
        if not pages:
            raise HTTPException(400, "No hay paginas conectadas.")
        page = pages[0]
        page_id, page_token = page["id"], page["access_token"]
        images = _json(draft.images_json, [])
        es_ig = req.destination.startswith("ig")
        copy_text = (req.texto or "").strip() or (draft.copy_ig if es_ig else draft.copy_fb)

        if req.destination == "fb_feed":
            if images:
                media = requests.post(f"{GRAPH}/{page_id}/photos",
                                      data={"url": images[0], "caption": copy_text, "access_token": page_token}).json()
            else:
                media = requests.post(f"{GRAPH}/{page_id}/feed",
                                      data={"message": copy_text, "access_token": page_token}).json()
            post_id = media.get("post_id") or media.get("id")
        elif req.destination == "ig_feed":
            if not images:
                return {"success": False, "error": "Instagram necesita al menos una foto."}
            ig_res = requests.get(f"{GRAPH}/{page_id}?fields=instagram_business_account&access_token={page_token}").json()
            ig_id = (ig_res.get("instagram_business_account") or {}).get("id")
            if not ig_id:
                raise HTTPException(400, "La pagina no tiene Instagram Business vinculado")
            cont = requests.post(f"{GRAPH}/{ig_id}/media",
                                 data={"image_url": images[0], "caption": copy_text, "access_token": page_token}).json()
            if not cont.get("id"):
                return {"success": False, "error": _error_graph(cont, "No se pudo preparar la publicacion en Instagram")}
            media = requests.post(f"{GRAPH}/{ig_id}/media_publish",
                                  data={"creation_id": cont["id"], "access_token": page_token}).json()
            post_id = media.get("id")
        else:
            return {"success": False, "error": "Destino no soportado"}

        if not post_id:
            return {"success": False, "error": _error_graph(media, "Meta no confirmo la publicacion")}

        ahora_ = ahora()
        db.add(Publicacion(draft_id=draft.id, telegram_id=draft.telegram_id, destino=req.destination,
                           post_id=str(post_id), published_at=ahora_))
        draft.status = "publicado"
        draft.destination = req.destination
        draft.published_at = ahora_
        db.commit()
        return {"success": True, "post_id": post_id}
    except HTTPException:
        raise
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        db.close()


# --- INTERACCIONES ---
def _leer_metricas(pub: Publicacion, token: str):
    """Devuelve (likes, comments, shares) o None si Meta no respondio."""
    if pub.destino == "fb_feed":
        r = requests.get(f"{GRAPH}/{pub.post_id}", params={
            "fields": "reactions.summary(true).limit(0),comments.summary(true).limit(0),shares",
            "access_token": token}, timeout=15).json()
        if "error" in r:
            return None
        return (((r.get("reactions") or {}).get("summary") or {}).get("total_count", 0),
                ((r.get("comments") or {}).get("summary") or {}).get("total_count", 0),
                (r.get("shares") or {}).get("count", 0))
    r = requests.get(f"{GRAPH}/{pub.post_id}", params={
        "fields": "like_count,comments_count", "access_token": token}, timeout=15).json()
    if "error" in r:
        return None
    return r.get("like_count", 0), r.get("comments_count", 0), 0


def refrescar_metricas(tid: str, dias: int = 40) -> dict:
    db = SessionLocal()
    ok = fallos = 0
    try:
        user = db.query(FbUser).filter_by(telegram_id=tid).first()
        pages = _json(user.pages_json, []) if user else []
        if not pages:
            return {"actualizadas": 0, "errores": 0, "motivo": "Facebook no conectado"}
        token = pages[0]["access_token"]
        desde = ahora() - timedelta(days=dias)
        for pub in db.query(Publicacion).filter(Publicacion.telegram_id == tid, Publicacion.published_at >= desde):
            if not pub.post_id:
                continue
            try:
                m = _leer_metricas(pub, token)
            except Exception:
                m = None
            if m is None:
                fallos += 1
                continue
            pub.likes, pub.comments, pub.shares = m
            pub.metrics_at = ahora()
            ok += 1
        db.commit()
        return {"actualizadas": ok, "errores": fallos}
    finally:
        db.close()


@app.post("/api/metricas/refresh")
def api_refresh(req: RefreshReq):
    db = SessionLocal()
    try:
        verificar(db, req.telegram_id, req.k)
    finally:
        db.close()
    return refrescar_metricas(str(req.telegram_id))


def _loop_metricas():
    while True:
        time.sleep(3 * 3600)
        db = SessionLocal()
        try:
            ids = [u.telegram_id for u in db.query(FbUser).all()]
        finally:
            db.close()
        for tid in ids:
            try:
                refrescar_metricas(tid)
            except Exception:
                pass


@app.on_event("startup")
def _arrancar_metricas():
    if os.getenv("METRICAS_AUTO", "1") != "0":
        threading.Thread(target=_loop_metricas, daemon=True).start()


# --- DATOS DEL TABLERO ---
def _interacciones(p: Publicacion) -> int:
    return (p.likes or 0) + (p.comments or 0) + (p.shares or 0)


@app.get("/api/dashboard")
def api_dashboard(tid: str, k: str = "", mes: Optional[str] = None):
    actual = mes_key(ahora())
    if not (mes and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", mes)):
        mes = actual
    db = SessionLocal()
    try:
        perfil = verificar(db, tid, k)
        fb_user = db.query(FbUser).filter_by(telegram_id=tid).first()
        drafts = db.query(Draft).filter_by(telegram_id=tid).order_by(Draft.created_at.desc()).limit(500).all()
        pubs = db.query(Publicacion).filter_by(telegram_id=tid).all()
    finally:
        db.close()

    pubs_por_draft = {}
    for p in pubs:
        pubs_por_draft.setdefault(p.draft_id, []).append(p)
    nombre = perfil.nombre if perfil else ""
    wa = perfil.whatsapp if perfil else ""

    def kpis(key):
        del_mes = [p for p in pubs if p.published_at and mes_key(p.published_at) == key]
        fb = [p for p in del_mes if p.destino == "fb_feed"]
        ig = [p for p in del_mes if p.destino == "ig_feed"]
        return {"publicados": len(del_mes), "fb": len(fb), "ig": len(ig),
                "interacciones": sum(_interacciones(p) for p in del_mes),
                "int_fb": sum(_interacciones(p) for p in fb), "int_ig": sum(_interacciones(p) for p in ig)}

    k, k_prev = kpis(mes), kpis(mes_prev(mes))

    # --- tarjetas de avisos ---
    lista = []
    for d in drafts:
        imgs = _json(d.images_json, [])
        datos = _json(d.datos_json, None) or extraer_datos({"title": d.title or "", "price": d.price or ""})
        variantes = _json(d.variantes_json, None) or generar_variantes(datos, nombre, wa)
        score, tips = evaluar(datos, len(imgs), d.copy_ig or "", bool(wa))
        mis_pubs = sorted(pubs_por_draft.get(d.id, []), key=lambda p: p.published_at or ahora())
        lista.append({
            "id": d.id, "title": d.title or "Propiedad", "price": d.price or "",
            "zona": datos.get("zona", ""), "tipo": datos.get("tipo", ""), "operacion": datos.get("operacion", ""),
            "img": imgs[0] if imgs else "", "n_fotos": len(imgs), "source_url": d.source_url or "",
            "estado": "publicado" if mis_pubs else "pendiente",
            "mes_creado": mes_key(d.created_at), "meses_pub": sorted({mes_key(p.published_at) for p in mis_pubs if p.published_at}),
            "creado": local(d.created_at).strftime("%d/%m/%Y %H:%M"),
            "copy_ig": d.copy_ig or "", "copy_fb": d.copy_fb or "", "variantes": variantes,
            "score": score, "tips": tips,
            "pubs": [{"destino": p.destino, "likes": p.likes or 0, "comments": p.comments or 0, "shares": p.shares or 0,
                      "fecha": local(p.published_at).strftime("%d/%m %H:%M") if p.published_at else "",
                      "mes": mes_key(p.published_at) if p.published_at else ""} for p in mis_pubs],
        })

    # --- graficos ---
    en_mes = [p for p in pubs if p.published_at and mes_key(p.published_at) == mes]
    a, m = int(mes[:4]), int(mes[5:])
    dias = calendar.monthrange(a, m)[1]
    rangos = [("1–7", 1, 7), ("8–14", 8, 14), ("15–21", 15, 21), ("22–28", 22, 28)] + ([(f"29–{dias}", 29, dias)] if dias > 28 else [])
    semanal = []
    for etiqueta, d0, d1 in rangos:
        en = [p for p in en_mes if d0 <= local(p.published_at).day <= d1]
        semanal.append({"label": etiqueta, "fb": sum(p.destino == "fb_feed" for p in en),
                        "ig": sum(p.destino == "ig_feed" for p in en)})

    historico, key = [], mes
    for _ in range(6):
        kk = kpis(key)
        historico.append({"mes": key, "label": MESES[int(key[5:]) - 1][:3], "publicados": kk["publicados"], "interacciones": kk["interacciones"]})
        key = mes_prev(key)
    historico.reverse()

    por_draft = {}
    for p in en_mes:
        por_draft[p.draft_id] = por_draft.get(p.draft_id, 0) + _interacciones(p)
    titulos = {d.id: d.title for d in drafts}
    top = sorted(({"id": i, "title": titulos.get(i, "Propiedad"), "interacciones": n} for i, n in por_draft.items() if n > 0),
                 key=lambda x: -x["interacciones"])[:5]

    pendientes = sum(1 for x in lista if x["estado"] == "pendiente" and (x["mes_creado"] == mes or mes == actual))
    meses = sorted({x["mes_creado"] for x in lista} | {p["mes"] for x in lista for p in x["pubs"] if p["mes"]} | {actual}, reverse=True)
    ult = [p.metrics_at for p in pubs if p.metrics_at]

    return {
        "perfil": {"nombre": nombre, "whatsapp": wa, "fb_conectado": bool(fb_user),
                   "login_url": link_conectar(tid, k)},
        "mes": mes, "mes_label": mes_label(mes), "mes_actual": actual, "meses": meses,
        "kpis": {**k, "pendientes": pendientes, "prev": k_prev},
        "semanal": semanal, "historico": historico, "top": top, "avisos": lista,
        "metricas_actualizadas": local(max(ult)).strftime("%d/%m %H:%M") if ult else "",
    }


# --- PAGINAS Y ARCHIVOS ---
@app.get("/dashboard")
def dashboard(tid: str = ""):
    return FileResponse(os.path.join(STATIC, "dashboard.html"), media_type="text/html")


def _archivo_logo(nombre="logo"):
    for ext in ("png", "svg", "jpg", "webp"):
        f = os.path.join(STATIC, f"{nombre}.{ext}")
        if os.path.exists(f):
            return f
    return None


@app.get("/logo")
def logo():
    f = _archivo_logo()
    return FileResponse(f, headers={"Cache-Control": "public, max-age=86400"}) if f else Response(status_code=404)


@app.get("/icon-{size}.png")
def icono(size: int):
    f = os.path.join(STATIC, f"icon-{size}.png")
    if size in (32, 180, 192, 512) and os.path.exists(f):
        return FileResponse(f, headers={"Cache-Control": "public, max-age=86400"})
    return Response(status_code=404)


@app.get("/favicon.ico")
def favicon():
    f = os.path.join(STATIC, "icon-32.png")
    return FileResponse(f) if os.path.exists(f) else Response(status_code=404)


@app.get("/manifest.json")
def manifest(tid: str = "", k: str = ""):
    inicio = f"/dashboard?tid={tid}&k={k}" if tid.isdigit() and re.fullmatch(r"[\w-]{1,40}", k) else "/dashboard"
    return JSONResponse({
        "name": "InmoBot · LLAVE.IA", "short_name": "InmoBot", "start_url": inicio, "display": "standalone",
        "background_color": "#06194a", "theme_color": "#06194a",
        "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}],
    }, media_type="application/manifest+json")


@app.get("/")
def home():
    return {"status": "InmoBot OK"}
