# Galería completa desde un enlace

## Cambio

El bot descarga automáticamente las fotos de la propiedad, conserva su orden y elimina duplicados, con un máximo de 10 fotos por aviso. El panel muestra la galería completa y permite recorrerla desde el celular. Los borradores anteriores pueden recuperar sus fotos mediante **Completar fotos del enlace**.

Facebook recibe todas las fotos adjuntas en una única publicación. Instagram recibe un carrusel, o una publicación individual si la propiedad tiene una sola foto. La opción **Publicar en ambas** informa el resultado de cada red y permite reintentar únicamente la que falló. Los archivos originales, las vistas previas y las versiones para Instagram quedan guardados en la base de datos. Las fotos con distintas proporciones se ajustan con márgenes para conservar la imagen completa.

Se bloquea la publicación cuando falta una descarga o el portal solamente entrega una portada sin una galería verificable. Una respuesta incierta de Meta también bloquea el reintento automático para evitar duplicados. Se corrigió el enlace de conexión del panel, que perdía su clave de acceso, y la conversión de precios que multiplicaba importes con decimales.

## Verificación

- 55 pruebas automatizadas: descarga de 1, 3, 7, 10 y más de 10 fotos; orden, duplicados, archivos inválidos, proporciones, aislamiento por usuario, migración de borradores anteriores y publicación de todas las fotos mediante respuestas simuladas de Meta.
- Navegador: galería de 7 fotos en escritorio y celular, navegación, selección de página y recuperación de un fallo parcial al publicar en ambas redes. Sin publicaciones reales.
- CENTURY 21, propiedad 52924: 7 fotos encontradas y descargadas.
- RE/MAX, propiedad 143098114-12: 3 fotos encontradas y descargadas.
- RE/MAX, propiedad 143094043-49: 18 fotos encontradas; 10 descargadas conforme al límite acordado.

Las evidencias locales están en `analisis/qa-galeria/`. No se incorporan al repositorio los archivos de fotos, bases de prueba, respaldos ni credenciales.

## Alcance y configuración

La extracción completa está verificada con los enlaces de CENTURY 21 Paraguay y RE/MAX Paraguay indicados. Otros sitios se leen mediante sus galerías explícitas y datos estructurados. No se garantiza cualquier enlace de Internet; algunos portales requieren un extractor específico o impiden la descarga.

El servicio necesita `DATABASE_URL` persistente y `APP_URL` con una dirección HTTPS accesible para que Instagram pueda descargar las fotos. `.python-version` fija Python 3.12.14, compatible con las dependencias existentes y usado en las pruebas; el Python 3.13 predeterminado de Railway no compila `psycopg2-binary==2.9.9`. La migración agrega la tabla `draft_photos` y columnas auxiliares a los borradores sin borrar los registros existentes. `META_GRAPH_VERSION` permite configurar la versión de Meta; el valor predeterminado es `v26.0`.

La app de Elvio existe en Meta, pero la instalación consultada conserva `FACEBOOK_APP_ID=placeholder` y no registra ninguna cuenta de Facebook conectada. Deben configurarse la app real, sus permisos y el retorno OAuth en `/auth/callback`, y completar la autorización de la página y la cuenta profesional de Instagram. Las pruebas de publicación ejecutadas aquí fueron simuladas: no se publicó contenido en ninguna cuenta real.

Los textos del aviso siguen requiriendo revisión, especialmente cuando una fuente ofrece varios lotes o contiene superficies y precios ambiguos. Esta entrega corrige la galería y la publicación de múltiples fotos; no completa por sí sola la vinculación de Meta.

## Comprobaciones reproducibles

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
python scripts/qa_galeria_local.py https://century21.com.py/propiedad/52924
node tests/dashboard.spec.cjs
```

La última comprobación requiere Playwright y Chromium instalados para pruebas. `PLAYWRIGHT_MODULE` permite indicar una instalación existente del paquete y `PLAYWRIGHT_BROWSERS_PATH` su caché. El navegador de prueba intercepta toda la red y no usa sesiones del usuario.

## Referencias de las API

- [Publicación de Instagram](https://developers.facebook.com/documentation/instagram-platform/content-publishing).
- [Fotos de una página de Facebook](https://developers.facebook.com/docs/graph-api/reference/page/photos/).
- [Versiones de Graph API](https://developers.facebook.com/docs/graph-api/changelog/versions/).
