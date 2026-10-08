"""Logica pura (sin red ni base de datos) para armar publicaciones.

Principio: del aviso original SOLO se extraen datos objetivos (tipo, zona, precio,
dormitorios, etc.). Ningun texto libre del portal se copia, asi no pasan telefonos,
firmas ni CTAs del agente que lo publico originalmente.
"""
import re

_RE_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_RE_MAIL = re.compile(r"\S+@\S+\.\S+")
_RE_PHONE = re.compile(r"\+?\d[\d\s\-().]{7,}\d")
_PORTALES = re.compile(r"\b(century\s?21|c21|remax|re/max|infocasas|mercadolibre|olx|paraguay)\b.*$", re.I)

TIPOS = {
    "casa": "Casa", "departamento": "Departamento", "depto": "Departamento",
    "duplex": "Dúplex", "dúplex": "Dúplex", "terreno": "Terreno", "lote": "Lote",
    "local comercial": "Local comercial", "oficina": "Oficina", "galpon": "Galpón",
    "galpón": "Galpón", "quinta": "Quinta", "edificio": "Edificio", "chacra": "Chacra",
    "estancia": "Estancia", "monoambiente": "Monoambiente", "loft": "Loft",
    "penthouse": "Penthouse",
}
_RE_TIPO = re.compile(r"\b(" + "|".join(sorted(TIPOS, key=len, reverse=True)) + r")\b", re.I)

COMODIDADES = [
    (r"piscina|pileta", "piscina"), (r"quincho", "quincho"), (r"jard[ií]n", "jardín"),
    (r"seguridad|vigilancia|barrio cerrado|condominio", "seguridad"),
    (r"balc[oó]n", "balcón"), (r"terraza", "terraza"),
    (r"aire acondicionado|split|a/a\b", "aire acondicionado"),
    (r"amoblad|equipad", "amoblado"), (r"parrilla|churrasquera", "parrilla"),
    (r"ascensor", "ascensor"), (r"gimnasio", "gimnasio"), (r"lavadero", "lavadero"),
]


# ---------------------------------------------------------------- utilidades
def normalizar_wa(raw: str) -> str:
    """Devuelve +595XXXXXXXXX (celular paraguayo) o +<pais><num>; '' si no es valido."""
    raw = (raw or "").strip()
    d = re.sub(r"\D", "", raw)
    if d.startswith("00"):
        d = d[2:]
    if d.startswith("595"):
        d = d[3:]
    elif d.startswith("0"):
        d = d[1:]
    elif raw.startswith("+") and 8 <= len(d) <= 15:
        return "+" + d  # otro pais
    if len(d) == 9 and d.startswith("9"):
        return "+595" + d
    return ""


def _sin_contacto(texto: str) -> str:
    for rx in (_RE_URL, _RE_MAIL, _RE_PHONE):
        texto = rx.sub(" ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def limpiar_titulo(titulo: str) -> str:
    t = _sin_contacto(re.sub(r"<[^>]*>", " ", titulo or ""))
    t = re.split(r"\s[|\-–—]\s", t)[0]
    t = _PORTALES.sub("", t).strip(" -|,.")
    return t[:90]


def normalizar_precio(texto: str) -> str:
    m = re.search(r"(USD|U\$S|US\$|\$|Gs\.?|₲)\s*([\d.,]+)", texto or "", re.I)
    if not m:
        return ""
    digitos = re.sub(r"\D", "", m.group(2))
    if len(digitos) < 4:
        return ""
    moneda = "Gs." if m.group(1).lower().startswith(("gs", "₲")) else "USD"
    return f"{moneda} {int(digitos):,}".replace(",", ".")


def _num(rx, texto):
    m = re.search(rx, texto, re.I)
    return m.group(1) if m else ""


def _zona(titulo: str) -> str:
    """Toma lo que sigue al ultimo 'en' del titulo, ej: 'Casa en venta en Luque'."""
    candidatos = re.findall(r"\ben\s+(?!venta\b|alquiler\b)([^|\-–,.;:]+)", titulo, re.I)
    if not candidatos:
        return ""
    zona = re.split(r"\s+(?:con|de|para|ideal|a\s)\b", candidatos[-1].strip(), flags=re.I)[0]
    return zona.strip()[:40]


# ---------------------------------------------------------------- extraccion
def extraer_datos(raw: dict) -> dict:
    """raw: {title, description, price, images}. Devuelve solo hechos."""
    titulo_raw = raw.get("title") or ""
    titulo = limpiar_titulo(titulo_raw)
    texto = f"{titulo_raw} {raw.get('description') or ''}"

    m_tipo = _RE_TIPO.search(titulo_raw) or _RE_TIPO.search(texto)
    tipo = TIPOS.get(m_tipo.group(1).lower(), "") if m_tipo else ""
    bajo = texto.lower()
    operacion = "alquiler" if re.search(r"alquil", bajo) else "venta" if re.search(r"\bvent|vend", bajo) else ""

    comodidades = []
    for rx, etiqueta in COMODIDADES:
        if re.search(rx, bajo) and etiqueta not in comodidades:
            comodidades.append(etiqueta)

    return {
        "titulo": titulo or "Propiedad",
        "tipo": tipo,
        "operacion": operacion,
        "zona": _zona(titulo_raw),
        "dormitorios": _num(r"(\d{1,2})\s*(?:dorm|habitac|cuartos?)", texto),
        "banos": _num(r"(\d{1,2})\s*ba[ñn]os?", texto),
        "cocheras": _num(r"(\d{1,2})\s*(?:cocheras?|garage|garaje|estacionamientos?)", texto),
        "m2": _num(r"(\d[\d.,]{1,8})\s*(?:m2|m²|mts2?|metros)", texto),
        "precio": normalizar_precio(raw.get("price") or "") or normalizar_precio(texto),
        "comodidades": comodidades[:4],
    }


# ---------------------------------------------------------------- redaccion
def _plural(n, uno, varios):
    return f"{n} {uno if str(n) == '1' else varios}"


def _ficha(d: dict) -> list:
    out = []
    if d.get("dormitorios"):
        out.append(("🛏", _plural(d["dormitorios"], "dormitorio", "dormitorios")))
    if d.get("banos"):
        out.append(("🛁", _plural(d["banos"], "baño", "baños")))
    if d.get("cocheras"):
        out.append(("🚗", _plural(d["cocheras"], "cochera", "cocheras")))
    if d.get("m2"):
        out.append(("📐", f"{d['m2']} m²"))
    return out


def _hashtags(d: dict, n: int) -> str:
    tags = ["#InmueblesParaguay"]
    zona = re.sub(r"[^\wáéíóúñÁÉÍÓÚÑ]", "", (d.get("zona") or "").title())
    if zona:
        tags.append(f"#{zona}")
    if d.get("tipo"):
        op = "Alquiler" if d.get("operacion") == "alquiler" else "Venta"
        tags.append(f"#{d['tipo'].replace(' ', '').replace('ú', 'u')}En{op}")
    tags += ["#BienesRaicesPy", "#PropiedadesPy"]
    return " ".join(tags[:n])


def _gancho(d: dict) -> str:
    en = f" en {d['zona']}" if d.get("zona") else ""
    alq = d.get("operacion") == "alquiler"
    tipo = (d.get("tipo") or "").lower()
    if tipo in ("casa", "quinta", "chacra"):
        return f"Tu próximo hogar te espera{en}" if not alq else f"Tu próximo hogar en alquiler{en}"
    if tipo in ("departamento", "monoambiente", "loft", "penthouse", "dúplex"):
        return f"El lugar que imaginaste para vivir{en}"
    if tipo in ("terreno", "lote"):
        return f"El terreno ideal para tu proyecto{en}"
    if tipo in ("local comercial", "oficina", "galpón", "edificio"):
        return f"El espacio donde tu negocio puede crecer{en}"
    return f"Una oportunidad que no querés perderte{en}"


def _cta(nombre: str, wa: str, con_link: bool) -> str:
    if not wa:
        return "📲 Consultas por WhatsApp"
    linea = f"📲 Consultas con {nombre}: {wa}" if nombre else f"📲 Consultas: {wa}"
    if con_link:
        numero = re.sub(r"\D", "", wa)
        linea += f"\n👉 https://wa.me/{numero}"
    return linea


def generar_variantes(d: dict, nombre: str, wa: str) -> dict:
    """Dos estilos (emocional / directa) x dos redes (ig / fb)."""
    ficha = _ficha(d)
    amen = d.get("comodidades") or []
    precio = f"💰 {d['precio']}" if d.get("precio") else ""
    sin_datos = not (d.get("tipo") or d.get("zona") or ficha)
    ficha_linea = " · ".join(f"{i} {t}" for i, t in ficha)
    amen_txt = ("Con " + ", ".join(amen[:-1]) + (" y " if len(amen) > 1 else "") + amen[-1] + ".") if amen else ""

    def unir(*bloques):
        return "\n\n".join(b for b in bloques if b)

    if sin_datos:
        base = d["titulo"]
        ig_e = unir(f"✨ {base}", precio, _cta(nombre, wa, False), _hashtags(d, 3))
        fb_e = unir(f"✨ {base}", precio, _cta(nombre, wa, True), _hashtags(d, 3))
        ig_d = unir(base.upper(), precio, _cta(nombre, wa, False), _hashtags(d, 3))
        fb_d = unir(base.upper(), precio, _cta(nombre, wa, True), _hashtags(d, 3))
    else:
        op = f" en {d['operacion']}" if d.get("operacion") else ""
        encabezado_d = f"{(d.get('tipo') or 'Propiedad').upper()}{op.upper()}" + (f" · {d['zona'].upper()}" if d.get("zona") else "")
        lista_d = "\n".join([f"✔ {t}" for _, t in ficha] + [f"✔ {a.capitalize()}" for a in amen])

        ig_e = unir(f"✨ {_gancho(d)}", ficha_linea, amen_txt, precio, _cta(nombre, wa, False), _hashtags(d, 5))
        fb_e = unir(
            f"✨ {_gancho(d)}",
            (f"{d.get('tipo') or 'Propiedad'}{op}." if d.get("tipo") else "") + (f" {amen_txt}" if amen_txt else ""),
            "\n".join(f"{i} {t}" for i, t in ficha), precio, _cta(nombre, wa, True), _hashtags(d, 3))
        ig_d = unir(encabezado_d, lista_d, precio, _cta(nombre, wa, False), _hashtags(d, 5))
        fb_d = unir(encabezado_d, lista_d, precio, _cta(nombre, wa, True), _hashtags(d, 3))

    return {"emocional": {"ig": ig_e, "fb": fb_e}, "directa": {"ig": ig_d, "fb": fb_d}}


# ---------------------------------------------------------------- calidad
def evaluar(d: dict, n_fotos: int, copy_ig: str, tiene_wa: bool):
    """Puntaje 0-100 y consejos ordenados por impacto."""
    pts, tips = 0, []

    def sumar(ok_pts, max_pts, consejo=None, impacto=None):
        nonlocal pts
        pts += ok_pts
        if consejo and ok_pts < max_pts:
            tips.append((impacto if impacto is not None else max_pts - ok_pts, consejo))

    sumar(15 if d.get("precio") else 0, 15, "Falta el precio: los avisos con precio generan más consultas.")
    fotos_pts = 20 if n_fotos >= 8 else 15 if n_fotos >= 5 else 8 if n_fotos >= 3 else 3 if n_fotos >= 1 else 0
    sumar(fotos_pts, 20, f"Sumá más fotos (hoy {n_fotos}): con 8 o más el aviso se ve más completo.")
    sumar(15 if d.get("zona") else 0, 15, "Indicá la zona o barrio en el título.")
    sumar(10 if (d.get("dormitorios") or d.get("tipo") in ("Terreno", "Lote")) and (d.get("banos") or d.get("tipo") in ("Terreno", "Lote")) else 0, 10,
          "Agregá dormitorios y baños.")
    sumar(10 if d.get("m2") else 0, 10, "Agregá los metros cuadrados.")
    sumar(5 if d.get("tipo") else 0, 5, "Aclará el tipo de propiedad (casa, departamento, terreno…).")
    sumar(5 if d.get("operacion") else 0, 5, "Indicá si es venta o alquiler.")
    sumar(10 if tiene_wa else 0, 10, "Configurá tu WhatsApp en el bot para que el contacto salga en cada aviso.", 12)
    largo = len(copy_ig or "")
    sumar(5 if 150 <= largo <= 900 else 0, 5,
          "Acortá el texto de Instagram." if largo > 900 else "El texto está corto: sumá detalles que ayuden a decidir.")
    n_tags = len(re.findall(r"#\w+", copy_ig or ""))
    sumar(5 if 3 <= n_tags <= 8 else 0, 5, "Usá entre 3 y 8 hashtags.")
    tips.sort(key=lambda x: -x[0])
    return min(pts, 100), [t for _, t in tips]
