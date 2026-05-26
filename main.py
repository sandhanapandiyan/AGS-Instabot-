import json
import logging
import os
from collections import deque
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request, Response, status

load_dotenv()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("instagram-bot")

app = FastAPI(title="Instagram Webhook Service")

# --- Config: all secrets come from the environment / .env (never hardcoded) ---
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
GRAPH_VERSION = os.environ.get("IG_GRAPH_VERSION", "v23.0")  # not secret; safe default

_missing = [k for k in ("VERIFY_TOKEN", "IG_ACCESS_TOKEN") if not os.environ.get(k)]
if _missing:
    logger.warning("Missing required env vars (set them in .env): %s", ", ".join(_missing))

PRESET_COUNTS = range(0, 7)  # quick-tap buttons 0..6; "Custom" covers anything higher
MAX_TICKETS = 100
ADULT_PRICE = 800  # ₹ per adult ticket
CHILD_PRICE = 450  # ₹ per child ticket
# Steps that expect the user to type free text (not tap a button):
TEXT_STEPS = {"custom_adult", "custom_child", "await_name", "await_whatsapp"}
GREETINGS = {"hi", "hii", "hiii", "hai", "hello", "helo", "hey", "start", "menu"}
# Remember recently handled message IDs so duplicate webhook deliveries are ignored.
_processed_mids: deque = deque(maxlen=1000)

# Per-user state: adult, child, step, name, whatsapp.
# Persisted to disk so an in-progress booking survives a server restart.
SESSIONS_FILE = Path(__file__).parent / ".sessions.json"


def _load_sessions() -> dict:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text())
        except (ValueError, OSError):
            logger.warning("could not read %s — starting with empty sessions", SESSIONS_FILE)
    return {}


def _save_sessions() -> None:
    try:
        SESSIONS_FILE.write_text(json.dumps(SESSIONS))
    except OSError:
        logger.exception("could not persist sessions")


SESSIONS: dict[str, dict] = _load_sessions()


# --- Instagram send helpers ----------------------------------------------
async def send_message(recipient_id: str, message: dict) -> None:
    """POST a message object (text and/or quick_replies) to the Send API."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://graph.instagram.com/{GRAPH_VERSION}/me/messages",
            headers={"Authorization": f"Bearer {IG_ACCESS_TOKEN}"},
            json={"recipient": {"id": recipient_id}, "message": message},
        )
    if resp.status_code == 200:
        logger.info("➡️  sent to %s | message_id=%s", recipient_id, resp.json().get("message_id"))
    else:
        logger.error("❌ send to %s failed (%s): %s", recipient_id, resp.status_code, resp.json())


def quick_reply(title: str, payload: str) -> dict:
    """A tappable button (title max 20 chars) that posts `payload` back to us."""
    return {"content_type": "text", "title": title, "payload": payload}


def session(recipient_id: str) -> dict:
    return SESSIONS.setdefault(recipient_id, {"adult": 0, "child": 0, "step": None})


def price_summary(s: dict) -> str:
    """Price breakdown lines: per-type subtotals, ticket count, and grand total (₹)."""
    adult_amt = s["adult"] * ADULT_PRICE
    child_amt = s["child"] * CHILD_PRICE
    return (
        f"👤 Adult: {s['adult']} × ₹{ADULT_PRICE} = ₹{adult_amt:,}\n"
        f"🧒 Child: {s['child']} × ₹{CHILD_PRICE} = ₹{child_amt:,}\n"
        f"🎫 Total tickets: {s['adult'] + s['child']}\n"
        f"💰 Total amount: ₹{adult_amt + child_amt:,}"
    )


# --- Menus / pickers ------------------------------------------------------
async def send_main_menu(recipient_id: str) -> None:
    SESSIONS.pop(recipient_id, None)
    await send_message(
        recipient_id,
        {
            "text": (
                "Hi there! 🌸 Welcome to AGS — so happy you're here!\n\n"
                "Which experience would you like to explore today? 👇"
            ),
            "quick_replies": [
                quick_reply("🌴 AGS RESORT", "AGS_RESORT"),
                quick_reply("🎢 AGS WONDER WORLD", "AGS_WONDER_WORLD"),
            ],
        },
    )


def count_buttons(kind: str) -> list[dict]:
    """Number buttons 0..6 plus a Custom option for `kind` (adult/child)."""
    buttons = [quick_reply(str(i), f"SET|{kind}|{i}") for i in PRESET_COUNTS]
    buttons.append(quick_reply("✏️ Custom", f"CUSTOM|{kind}"))
    return buttons


async def send_adult_picker(recipient_id: str) -> None:
    session(recipient_id)["step"] = "adult"
    await send_message(
        recipient_id,
        {
            "text": (
                "🎢 AGS Wonder World 🎟️\n\n"
                "👤 How many ADULT tickets?\n"
                "Tap a number, or ✏️ Custom to enter any amount."
            ),
            "quick_replies": count_buttons("adult"),
        },
    )


async def send_child_picker(recipient_id: str) -> None:
    s = session(recipient_id)
    s["step"] = "child"
    await send_message(
        recipient_id,
        {
            "text": (
                f"Great — {s['adult']} adult 👤\n\n"
                "🧒 How many CHILD tickets?\n"
                "Tap a number, or ✏️ Custom to enter any amount."
            ),
            "quick_replies": count_buttons("child"),
        },
    )


async def send_ticket_confirm(recipient_id: str) -> None:
    s = session(recipient_id)
    s["step"] = "confirm"
    await send_message(
        recipient_id,
        {
            "text": (
                "🎟️ Your AGS Wonder World tickets\n\n"
                f"{price_summary(s)}\n\n"
                "All good?"
            ),
            "quick_replies": [
                quick_reply("✅ Confirm", "CONFIRM"),
                quick_reply("✏️ Edit", "EDIT"),
            ],
        },
    )


async def ask_custom(recipient_id: str, kind: str) -> None:
    session(recipient_id)["step"] = f"custom_{kind}"
    await send_message(
        recipient_id,
        {"text": f"✏️ Please type the number of {kind.upper()} tickets you'd like (e.g. 12):"},
    )


# --- Contact collection ---------------------------------------------------
async def ask_name(recipient_id: str) -> None:
    session(recipient_id)["step"] = "await_name"
    await send_message(
        recipient_id,
        {"text": "🎉 Almost there!\n\n📝 What name should we book the tickets under?"},
    )


async def ask_whatsapp(recipient_id: str) -> None:
    session(recipient_id)["step"] = "await_whatsapp"
    await send_message(
        recipient_id,
        {"text": "📱 Please type your WhatsApp number with country code (e.g. +91 98765 43210):"},
    )


async def send_contact_confirm(recipient_id: str) -> None:
    s = session(recipient_id)
    s["step"] = "confirm_contact"
    await send_message(
        recipient_id,
        {
            "text": (
                "Please confirm your details 👇\n\n"
                f"📝 Name: {s.get('name')}\n"
                f"📱 WhatsApp number: {s.get('whatsapp')}\n\n"
                "Is this correct?"
            ),
            "quick_replies": [
                quick_reply("✅ Confirm", "CONFIRM_CONTACT"),
                quick_reply("✏️ Edit", "EDIT_CONTACT"),
            ],
        },
    )


async def send_final_confirm(recipient_id: str) -> None:
    s = session(recipient_id)
    s["step"] = "final_confirm"
    await send_message(
        recipient_id,
        {
            "text": (
                "🧾 Please review your full booking 👇\n\n"
                "🎢 AGS Wonder World\n"
                f"{price_summary(s)}\n\n"
                f"📝 Name: {s.get('name')}\n"
                f"📱 WhatsApp number: {s.get('whatsapp')}\n\n"
                "Shall we confirm this booking?"
            ),
            "quick_replies": [
                quick_reply("✅ Confirm booking", "CONFIRM_ALL"),
                quick_reply("✏️ Edit", "EDIT_ALL"),
            ],
        },
    )


async def send_booking_done(recipient_id: str) -> None:
    s = session(recipient_id)
    whatsapp = s.get("whatsapp")
    await send_message(
        recipient_id,
        {
            "text": (
                "✅ Booking confirmed! 🎉\n\n"
                f"🎟️ Your tickets will be sent to your WhatsApp number {whatsapp}.\n\n"
                "Thank you for choosing AGS Wonder World! 💛"
            ),
            "quick_replies": [quick_reply("⬅️ Menu", "MENU")],
        },
    )
    SESSIONS.pop(recipient_id, None)


# --- Flow handlers --------------------------------------------------------
async def handle_payload(recipient_id: str, payload: str) -> None:
    """Drive the flow from a tapped quick-reply payload."""
    s = session(recipient_id)
    parts = payload.split("|")
    action = parts[0]

    if payload == "AGS_WONDER_WORLD":
        s.update(adult=0, child=0)
        await send_adult_picker(recipient_id)
    elif payload == "AGS_RESORT":
        await send_message(
            recipient_id,
            {
                "text": (
                    "🌴 AGS Resort — pure relaxation awaits! 🏖️\n"
                    "Our booking assistant for the resort is coming soon. 💛"
                ),
                "quick_replies": [
                    quick_reply("🎢 AGS WONDER WORLD", "AGS_WONDER_WORLD"),
                    quick_reply("⬅️ Menu", "MENU"),
                ],
            },
        )
    elif action == "SET":  # SET|adult|3
        kind, num = parts[1], int(parts[2])
        s[kind] = num
        await (send_child_picker if kind == "adult" else send_ticket_confirm)(recipient_id)
    elif action == "CUSTOM":  # CUSTOM|child
        await ask_custom(recipient_id, parts[1])
    elif payload == "CONFIRM":  # tickets confirmed → collect contact (or jump to review)
        if s["adult"] + s["child"] == 0:
            await send_message(recipient_id, {"text": "Please add at least one ticket 🎟️"})
            await send_adult_picker(recipient_id)
        elif s.get("name") and s.get("whatsapp"):
            await send_final_confirm(recipient_id)  # contact already given (tickets were edited)
        else:
            await ask_name(recipient_id)
    elif payload == "EDIT":  # edit tickets
        s.update(adult=0, child=0)
        await send_adult_picker(recipient_id)
    elif payload == "CONFIRM_CONTACT":  # name + WhatsApp confirmed → final review
        await send_final_confirm(recipient_id)
    elif payload == "EDIT_CONTACT":  # re-enter name + WhatsApp
        s["name"], s["whatsapp"] = None, None
        await ask_name(recipient_id)
    elif payload == "CONFIRM_ALL":  # everything confirmed → done
        await send_booking_done(recipient_id)
    elif payload == "EDIT_ALL":  # choose what to edit
        await send_message(
            recipient_id,
            {
                "text": "✏️ What would you like to edit?",
                "quick_replies": [
                    quick_reply("🎟️ Tickets", "EDIT"),
                    quick_reply("📝 Details", "EDIT_CONTACT"),
                ],
            },
        )
    else:  # "MENU" or anything unrecognized
        await send_main_menu(recipient_id)


async def handle_text(recipient_id: str, text: str) -> None:
    """Handle typed input while the session is on a text step."""
    s = session(recipient_id)
    step = s["step"]

    if step in ("custom_adult", "custom_child"):
        kind = "adult" if step == "custom_adult" else "child"
        digits = "".join(ch for ch in text if ch.isdigit())
        n = int(digits) if digits else None
        if n is None or n > MAX_TICKETS:
            await send_message(
                recipient_id,
                {"text": f"That doesn't look right 🙂 Please enter a number from 0 to {MAX_TICKETS}, or tap one below:"},
            )
            await (send_adult_picker if kind == "adult" else send_child_picker)(recipient_id)
            return
        s[kind] = n
        await (send_child_picker if kind == "adult" else send_ticket_confirm)(recipient_id)

    elif step == "await_name":
        name = text.strip()
        if not name:
            await send_message(recipient_id, {"text": "Please type your name 🙂"})
            return
        s["name"] = name
        await ask_whatsapp(recipient_id)

    elif step == "await_whatsapp":
        digits = "".join(ch for ch in text if ch.isdigit())
        if not (7 <= len(digits) <= 15):
            await send_message(
                recipient_id,
                {"text": "That doesn't look like a valid WhatsApp number 🙂 Please include the country code, e.g. +91 98765 43210:"},
            )
            return
        s["whatsapp"] = text.strip()
        await send_contact_confirm(recipient_id)


# --- Routes ---------------------------------------------------------------
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/webhook")
def verify_webhook(
    mode: str = Query(alias="hub.mode"),
    token: str = Query(alias="hub.verify_token"),
    challenge: str = Query(alias="hub.challenge"),
) -> Response:
    """Meta verification handshake: echo hub.challenge back when the token matches."""
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    return Response(content="Verification failed", status_code=status.HTTP_403_FORBIDDEN)


@app.post("/webhook")
async def receive_webhook(request: Request) -> Response:
    """Log each incoming DM and drive the AGS menu / ticket / booking flow.

    Always returns 200 so Meta does not retry (which would duplicate replies).
    """
    payload = await request.json()

    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            message = event.get("message")
            if not message or message.get("is_echo"):
                continue  # skip reads/deliveries/reactions and our own echoes

            mid = message.get("mid")
            if mid and mid in _processed_mids:
                logger.info("⏭️  duplicate delivery skipped (mid=%s)", mid)
                continue  # Instagram re-delivered the same message — ignore it
            if mid:
                _processed_mids.append(mid)

            sender_id = event.get("sender", {}).get("id")
            text = message.get("text")
            qr_payload = (message.get("quick_reply") or {}).get("payload")
            logger.info("📩 sender=%s | text=%r | payload=%s", sender_id, text, qr_payload)

            if not sender_id:
                continue
            try:
                step = SESSIONS.get(sender_id, {}).get("step")
                if qr_payload:
                    await handle_payload(sender_id, qr_payload)
                elif text and step in TEXT_STEPS:
                    await handle_text(sender_id, text)
                elif text and text.strip().lower() in GREETINGS:
                    await send_main_menu(sender_id)
                elif step:
                    # Mid-flow at a button step. Ignore stray text / duplicate events
                    # (e.g. Instagram's auto phone-number card fires a second event) so
                    # the prompt isn't duplicated and progress isn't wiped — the buttons
                    # stay tappable.
                    logger.info("⏭️  ignoring unexpected input at step=%s from %s", step, sender_id)
                else:
                    await send_main_menu(sender_id)  # no active flow → welcome menu
            except Exception:
                logger.exception("❌ error handling event from %s", sender_id)

    _save_sessions()  # persist so in-progress bookings survive a restart
    return Response(status_code=status.HTTP_200_OK)
