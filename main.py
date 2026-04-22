from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
import os
import json
from datetime import datetime

app = FastAPI()

# ── clients ──────────────────────────────────────────────────────────────────
twilio_client = Client(
    os.environ["TWILIO_ACCOUNT_SID"],
    os.environ["TWILIO_AUTH_TOKEN"],
)
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")

# ── in-memory conversation store (replace with DB later) ─────────────────────
# Structure: { phone_number: { "messages": [...], "profile": {...} } }
conversations: dict = {}

# ── Sona system prompt ────────────────────────────────────────────────────────
SONA_SYSTEM_PROMPT = """You are Sona, a warm and perceptive networking concierge. You help people meet other people who are genuinely relevant to them — professionally and personally. You work for a company that hosts in-person events where members can meet face-to-face.

Your personality:
- Warm, curious, and a little playful — like a smart friend who happens to know everyone
- You ask ONE question at a time, never overwhelm people with forms
- You remember everything someone tells you and reference it naturally
- You're honest if you don't have a match yet — you say "I'm keeping an eye out for you"

Your goals in early conversations:
1. Learn their name and what brings them here
2. Understand what they're looking for (professional connections, friendships, co-founders, mentors, romantic — whatever they're open to)
3. Learn their professional background (you can ask for LinkedIn)
4. Understand their personal interests and personality
5. Set expectations: you'll introduce them to relevant people via WhatsApp, and there are in-person events they can attend

When you have enough info, tell them:
- You'll be in touch when you find a relevant match
- Mention upcoming events if relevant
- Be specific: "I think you'd really click with someone who's also building in the health space — I'll keep an eye out"

When introducing matches (you'll receive a MATCH_INTRO message with details):
- Explain specifically WHY these two people should meet
- Give them a fun ice-breaker or talking point
- If there's an upcoming event, tell them to find each other there

Current date: """ + datetime.now().strftime("%B %d, %Y") + """

Keep responses SHORT — this is WhatsApp, not email. 2-4 sentences max per message. No bullet points or markdown formatting (it looks ugly on WhatsApp). Just natural, conversational text."""


# ── helpers ───────────────────────────────────────────────────────────────────
def get_or_create_session(phone: str) -> dict:
    if phone not in conversations:
        conversations[phone] = {
            "messages": [],
            "profile": {},
            "joined_at": datetime.now().isoformat(),
        }
    return conversations[phone]


def chat_with_sona(phone: str, user_message: str) -> str:
    session = get_or_create_session(phone)

    # Append user message
    session["messages"].append({"role": "user", "content": user_message})

    # Call Claude
    response = anthropic_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
        system=SONA_SYSTEM_PROMPT,
        messages=session["messages"],
    )

    reply = response.content[0].text

    # Append assistant message
    session["messages"].append({"role": "assistant", "content": reply})

    return reply


def send_whatsapp_message(to: str, body: str):
    """Proactively send a message (for match intros, event reminders, etc.)"""
    twilio_client.messages.create(
        from_=TWILIO_WHATSAPP_NUMBER,
        body=body,
        to=to,
    )


# ── webhook endpoint ──────────────────────────────────────────────────────────
@app.post("/webhook", response_class=PlainTextResponse)
async def webhook(
    From: str = Form(...),
    Body: str = Form(...),
):
    phone = From  # e.g. "whatsapp:+16263756580"
    user_message = Body.strip()

    print(f"[{phone}] → {user_message}")

    reply = chat_with_sona(phone, user_message)

    print(f"[Sona → {phone}] {reply}")

    # Reply via TwiML
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


# ── admin endpoints ───────────────────────────────────────────────────────────
@app.get("/admin/users")
async def list_users():
    """See all users and their profiles"""
    return {
        phone: {
            "joined_at": data["joined_at"],
            "message_count": len(data["messages"]),
            "profile": data["profile"],
        }
        for phone, data in conversations.items()
    }


@app.get("/admin/conversation/{phone_number}")
async def get_conversation(phone_number: str):
    """Read full conversation for a user (encode + as %2B in URL)"""
    key = f"whatsapp:+{phone_number}"
    return conversations.get(key, {"error": "not found"})


@app.post("/admin/introduce")
async def introduce_two_people(payload: dict):
    """
    Manually trigger a match introduction.
    POST body: {
        "phone_a": "+16263756580",
        "phone_b": "+447911123456",
        "name_a": "Joe",
        "name_b": "Sarah",
        "reason": "You're both building in the health tech space and Sarah is looking for a technical co-founder",
        "icebreaker": "Ask her about her time at Stanford — she has great stories"
    }
    """
    phone_a = f"whatsapp:{payload['phone_a']}"
    phone_b = f"whatsapp:{payload['phone_b']}"

    msg_to_a = (
        f"Hey! I've been thinking about you and I have someone you should meet. "
        f"{payload['name_b']} — {payload['reason']}. "
        f"Tip: {payload['icebreaker']} 🎯"
    )
    msg_to_b = (
        f"Hey! I want to introduce you to someone. "
        f"{payload['name_a']} — {payload['reason']}. "
        f"Tip: {payload['icebreaker']} 🎯"
    )

    send_whatsapp_message(phone_a, msg_to_a)
    send_whatsapp_message(phone_b, msg_to_b)

    return {"status": "sent", "to": [phone_a, phone_b]}


@app.post("/admin/event-reminder")
async def send_event_reminder(payload: dict):
    """
    Send event reminder to all users or specific ones.
    POST body: {
        "phones": ["+16263756580"],   // omit to send to all
        "event_name": "Sona Social #1",
        "date": "Saturday April 19th",
        "time": "7pm",
        "location": "The Lobby, Taipei"
    }
    """
    phones = payload.get("phones")
    if not phones:
        phones = [p.replace("whatsapp:", "") for p in conversations.keys()]

    msg = (
        f"🗓 Reminder: {payload['event_name']} is happening {payload['date']} at {payload['time']}! "
        f"Come to {payload['location']}. I'll send you a heads-up on who to look out for before the night. See you there!"
    )

    for phone in phones:
        send_whatsapp_message(f"whatsapp:{phone}", msg)

    return {"status": "sent", "count": len(phones)}


@app.get("/health")
async def health():
    return {"status": "ok", "users": len(conversations)}
