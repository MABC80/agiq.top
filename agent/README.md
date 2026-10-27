# Agente A2A de agiq

Servidor pequeño en Python. Usa LangGraph (`create_react_agent`) y `langchain-openai` con el modelo `gpt-4o-mini`. Habla el protocolo A2A 0.3 por JSON-RPC: la tarjeta del agente está en `/.well-known/agent-card.json` (y en `/.well-known/agent.json`) y el método `message/send` responde en la URL de la tarjeta.

https://agiq.top es una página estática: no envía mensajes. La tarjeta también está en `/.well-known/agent-card.json` de ese dominio (y en `agent.json`), y la ficha NANDA en `/agentfacts.json`. El archivo `.nojekyll` evita que GitHub Pages oculte `.well-known`. La clave de OpenAI vive solo en el entorno del servidor (`OPENAI_API_KEY`). No la pongas en el repositorio.

Cada `message/send` y `message/stream` cuenta para dos topes, en memoria y por proceso: `MAX_DAILY_MESSAGES` (por defecto 30, día UTC) y `MAX_MESSAGES_PER_IP_PER_HOUR` (por defecto 5, ventana de una hora; la IP sale de `X-Forwarded-For`). Al pasar el tope la respuesta es un error JSON-RPC `-32029`. Un POST que no es JSON-RPC recibe HTTP 429. La tarjeta, `/.well-known/agent.json` y `/health` no cuentan. Un reinicio del proceso (también al despertar el plan gratuito) pone los contadores a cero.

## Ejecutar en local

Hace falta Python 3.12 (el blueprint fija `3.12.3`).

```bash
cd agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...          # no la subas a git
export OPENAI_MODEL=gpt-4o-mini       # opcional; este es el valor por defecto
export PUBLIC_URL=http://127.0.0.1:8000  # opcional; por defecto https://agent.agiq.top
uvicorn app:app --host 0.0.0.0 --port 8000
```

Sin clave, para comprobar solo el protocolo, arranca con un modelo falso que responde `eco: <texto>`:

```bash
A2A_FAKE_LLM=1 uvicorn app:app --host 127.0.0.1 --port 8000
```

`A2A_FAKE_LLM` no debe activarse en Render.

Las pruebas de los topes no llaman a OpenAI:

```bash
A2A_FAKE_LLM=1 python -m unittest test_limits
```

## Probar con curl

Tarjeta del agente (las dos rutas devuelven lo mismo):

```bash
curl -s http://127.0.0.1:8000/.well-known/agent-card.json
curl -s http://127.0.0.1:8000/.well-known/agent.json
curl -s http://127.0.0.1:8000/health
```

Enviar un mensaje. La respuesta es una tarea A2A con `status.state` en `completed` y el texto en `artifacts` y en `status.message`:

```bash
curl -s http://127.0.0.1:8000/ \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "message/send",
    "params": {
      "message": {
        "role": "user",
        "messageId": "m1",
        "parts": [{ "kind": "text", "text": "Hola" }]
      }
    }
  }'
```

## Desplegar en Render

1. En el dashboard de Render, crea un Blueprint desde este repositorio. El archivo `render.yaml` de la raíz define un servicio web Python gratuito (`plan: free`), con `rootDir: agent`, que instala `requirements.txt` y arranca con `uvicorn app:app --host 0.0.0.0 --port $PORT`.
2. Cuando Render lo pida, pega el valor de `OPENAI_API_KEY`. Está declarado con `sync: false`, así que no viaja en git. `OPENAI_MODEL` queda en `gpt-4o-mini`, `PUBLIC_URL` en `https://agent.agiq.top` y `MAX_DAILY_MESSAGES` en `30`.
3. En el servicio, añade el dominio personalizado `agent.agiq.top` y crea en el DNS el CNAME que indique Render.
4. El sitio de GitHub Pages sigue sirviendo `index.html` en https://agiq.top. No borres el archivo `CNAME` ni `.nojekyll`.

El plan gratuito se duerme tras un rato sin tráfico. La primera petición puede tardar cerca de un minuto. El chequeo de salud es `GET /health`.

Una vez desplegado:

```bash
curl -s https://agent.agiq.top/health
curl -s https://agent.agiq.top/.well-known/agent-card.json
curl -s https://agent.agiq.top/.well-known/agent.json

curl -s https://agent.agiq.top/ \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "message/send",
    "params": {
      "message": {
        "role": "user",
        "messageId": "m1",
        "parts": [{ "kind": "text", "text": "Hola" }]
      }
    }
  }'
```
