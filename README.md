# Instagram Webhook Service

A minimal FastAPI service that receives webhook events from an Instagram app (Meta Graph API).
No token or signature verification — it accepts everything.

## Endpoints

| Method | Path       | Purpose                                                  |
| ------ | ---------- | -------------------------------------------------------- |
| GET    | `/webhook` | Handshake — echoes `hub.challenge` so Meta can register. |
| POST   | `/webhook` | Receives event notifications and logs them.              |
| GET    | `/health`  | Liveness check.                                          |

## Run

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

## Register with Meta

1. Expose it publicly (e.g. `ngrok http 8000`).
2. Meta App Dashboard → Webhooks → Instagram → Callback URL: `https://<your-host>/webhook`
   (the verify-token field can be any value; this service does not check it).

Put your own logic inside `handle_entry()` in `main.py`.
