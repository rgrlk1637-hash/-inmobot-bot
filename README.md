# InmoBot Telegram - Genérico para FB e IG

### Qué hace
Bot genérico para cualquier agente inmobiliario.
1. Usuario entra a Telegram -> /start
2. Conecta su cuenta con /conectar -> hace Login con Facebook (OAuth)
3. Reenvía 1 o 20 links de propiedades al bot
4. El bot scrapea cada link, baja fotos, genera copy con IA
5. Crea BORRADORES en tu base de datos
6. Le muestra preview en Telegram con botones: Publicar en FB Feed, IG Feed, IG Reel, IG Historia, Editar, Descartar
7. Solo cuando toca "Publicar", publica via Graph API en SU cuenta

### Stack
- Telegram Bot: python-telegram-bot v20
- Backend: FastAPI (auth, drafts, publish)
- DB: SQLite (puedes pasar a Postgres)
- Scraper: BeautifulSoup + Open Graph
- IA: OpenAI o Meta AI para reescribir copy
- Deploy: Railway / Render / Fly.io

### Permisos Facebook que necesitas pedir en developers.facebook.com
- pages_show_list, pages_read_engagement, pages_manage_posts
- instagram_basic, instagram_content_publish
- business_management (para detectar IG vinculado a Page)

Crea una App en developers.facebook.com > Tipo Negocio > Agrega producto Facebook Login

### Variables de entorno (.env)
TELEGRAM_BOT_TOKEN=...
FACEBOOK_APP_ID=...
FACEBOOK_APP_SECRET=...
APP_URL=https://tu-app.railway.app
OPENAI_API_KEY=...
DATABASE_URL=sqlite:///./inmobot.db
SECRET_KEY=una-clave-random-larga

### Instalación
pip install -r requirements.txt
uvicorn server:app --reload --port 8000
# en otra terminal
python bot.py
