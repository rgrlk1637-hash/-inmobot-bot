"""Claves y firmas compartidas entre bot.py y server.py (ambos leen el mismo entorno)."""
import os, hmac, hashlib


def _secreto() -> str:
    return os.getenv("SECRET_KEY") or os.getenv("TELEGRAM_BOT_TOKEN") or ""


def bot_key() -> str:
    """Clave que el bot envia al servidor en cada llamada interna. '' = sin proteccion (solo desarrollo)."""
    s = _secreto()
    return hashlib.sha256(("inmobot-bot:" + s).encode()).hexdigest() if s else ""


def firmar_state(tid: str, nonce: str) -> str:
    sig = hmac.new((_secreto() or "dev").encode(), f"{tid}|{nonce}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"{tid}|{nonce}|{sig}"


def state_valido(state: str):
    """Devuelve el telegram_id si la firma es correcta, si no None."""
    try:
        tid, nonce, sig = state.split("|")
    except ValueError:
        return None
    esperado = firmar_state(tid, nonce).split("|")[2]
    return tid if hmac.compare_digest(sig, esperado) else None
