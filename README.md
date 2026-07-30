# Resumen diario de noticias de Maria Leiva

Automatizacion en Python para enviar cada dia a las 07:30 (hora peninsular espanola) un correo con las noticias publicadas por Maria Leiva en SPORT durante las 24 horas anteriores.

## Que hace

- Lee `https://www.sport.es/es/autor/maria-leiva/`.
- Revisa las paginas de la autora y extrae los articulos.
- Filtra noticias con fecha entre las 07:30 del dia anterior y las 07:30 del dia actual.
- Envia un correo con titular, fecha, enlace y resumen.
- Guarda `sent_history.json` para evitar duplicados.
- Si un articulo ya enviado reaparece con una fecha nueva, lo incluye y anade un aviso.
- Si falla la automatizacion, intenta enviarte un correo de fallo.

## Opcion recomendada para que funcione con el PC apagado

La forma mas sencilla es GitHub Actions:

1. Crea un repositorio privado en GitHub.
2. Sube todos estos archivos al repositorio.
3. En GitHub, ve a `Settings > Secrets and variables > Actions`.
4. Crea el secreto `GMAIL_APP_PASSWORD`.
5. Opcionalmente crea el secreto `OPENAI_API_KEY` para resumenes mejores.
6. En `Settings > Actions > General`, confirma que `Workflow permissions` permite `Read and write permissions`.
7. Activa el workflow `Daily Sport digest`.

GitHub ejecuta horarios en UTC. Por eso el workflow corre dos veces al dia, a las 05:30 y 06:30 UTC. El script solo envia cuando en Madrid son las 07:30, asi funciona tambien con el cambio de horario de verano/invierno.

## Gmail

Para enviar desde `alexrsen100@gmail.com` a `alexrsen100@gmail.com`, Gmail necesita una contrasena de aplicacion:

1. Activa la verificacion en dos pasos en tu cuenta de Google.
2. Crea una contrasena de aplicacion para correo.
3. Guarda esa contrasena como secreto `GMAIL_APP_PASSWORD` en GitHub.

No pongas la contrasena real de Gmail en el codigo.

## Resumenes

El proyecto funciona sin OpenAI, usando un resumen basico extraido del texto de cada noticia.

Para resumenes mas naturales y detallados, anade el secreto `OPENAI_API_KEY`. Puedes cambiar el modelo con una variable de repositorio llamada `OPENAI_MODEL`.

## Probar en local

Instala dependencias:

```bash
pip install -r requirements.txt
```

Copia `.env.example` a `.env` si quieres ejecutar con variables locales. En PowerShell tambien puedes definirlas manualmente.

Prueba sin enviar correo:

```bash
python daily_sport_digest.py --dry-run
```

Enviar correo manualmente:

```bash
python daily_sport_digest.py
```

## Configuracion util

- `MAX_INDEX_PAGES`: numero de paginas de autora que revisa. Por defecto, `4`.
- `MAX_ARTICLES`: limite maximo de noticias por correo. Vacio significa todas.
- `SEND_EMPTY_EMAIL`: si es `true`, envia correo aunque no haya noticias nuevas.
- `SEND_TIME`: hora local de cierre del periodo. Por defecto, `07:30`.
