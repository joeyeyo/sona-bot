from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
import os
import json
import httpx
from datetime import datetime

app = FastAPI()

# ── CORS (allow sona-frontend on Vercel to call this backend) ─────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://sona-frontend-lyart.vercel.app", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── clients ───────────────────────────────────────────────────────────────────
twilio_client = Client(
    os.environ["TWILIO_ACCOUNT_SID"],
    os.environ["TWILIO_AUTH_TOKEN"],
)
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
YELP_API_KEY = os.environ.get("YELP_API_KEY", "")

# ── in-memory conversation store (replace with DB later) ──────────────────────
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
    session["messages"].append({"role": "user", "content": user_message})

    response = anthropic_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
        system=SONA_SYSTEM_PROMPT,
        messages=session["messages"],
    )

    reply = response.content[0].text
    session["messages"].append({"role": "assistant", "content": reply})
    return reply


def send_whatsapp_message(to: str, body: str):
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
    phone = From
    user_message = Body.strip()
    print(f"[{phone}] → {user_message}")
    reply = chat_with_sona(phone, user_message)
    print(f"[Sona → {phone}] {reply}")
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


# ── vendor search endpoint ────────────────────────────────────────────────────
@app.post("/search-vendors")
async def search_vendors(payload: dict):
    """
    Search for vendors via Yelp based on event requirements.
    POST body: {
        "category": "caterers",          // e.g. caterers, photographers, venues
        "location": "Los Angeles, CA",
        "budget": 600,                   // max budget in dollars
        "guest_count": 50,
        "min_rating": 4.25,
        "keywords": "asian food"         // optional extra search terms
    }
    """
    category = payload.get("category", "caterers")
    location = payload.get("location", "Los Angeles, CA")
    budget = payload.get("budget")
    guest_count = payload.get("guest_count")
    min_rating = payload.get("min_rating", 4.0)
    keywords = payload.get("keywords", "")

    search_term = f"{keywords} {category}".strip()

    # Call Yelp Fusion API
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.yelp.com/v3/businesses/search",
            headers={"Authorization": f"Bearer {YELP_API_KEY}"},
            params={
                "term": search_term,
                "location": location,
                "limit": 20,
                "sort_by": "rating",
            },
        )

    if response.status_code != 200:
        return {"error": "Yelp API error", "detail": response.text}

    data = response.json()
    businesses = data.get("businesses", [])

    # Filter by minimum rating
    filtered = [b for b in businesses if b.get("rating", 0) >= min_rating]

    # Shape the response for the frontend
    vendors = []
    for b in filtered:
        vendors.append({
            "id": b.get("id"),
            "name": b.get("name"),
            "rating": b.get("rating"),
            "review_count": b.get("review_count"),
            "price": b.get("price", "N/A"),
            "phone": b.get("display_phone", "N/A"),
            "address": ", ".join(b.get("location", {}).get("display_address", [])),
            "url": b.get("url"),
            "image_url": b.get("image_url"),
            "categories": [c["title"] for c in b.get("categories", [])],
        })

    # Use Claude to rank and summarize the best matches given the budget + guest count
    if vendors:
        summary_prompt = f"""I'm looking for {category} for an event with {guest_count} guests, budget under ${budget}.
Here are the Yelp results: {json.dumps(vendors[:10], indent=2)}

Pick the top 5 best matches and for each one write a 1-sentence reason why they're a good fit.
Respond ONLY with a JSON array like:
[{{"id": "...", "reason": "..."}}]
No markdown, no explanation, just the JSON array."""

        claude_response = anthropic_client.messages.create(
            model="claude-opus-4-5",
            max_tokens=500,
            messages=[{"role": "user", "content": summary_prompt}],
        )

        try:
            rankings = json.loads(claude_response.content[0].text)
            ranked_ids = {r["id"]: r["reason"] for r in rankings}

            # Merge Claude's reasons into vendor list and sort
            for v in vendors:
                v["ai_reason"] = ranked_ids.get(v["id"], "")

            vendors = sorted(vendors, key=lambda v: 1 if v["ai_reason"] else 0, reverse=True)
        except Exception:
            pass  # If Claude parsing fails, just return raw Yelp results

    return {
        "vendors": vendors,
        "total": len(vendors),
        "query": {
            "category": category,
            "location": location,
            "budget": budget,
            "guest_count": guest_count,
            "min_rating": min_rating,
        }
    }


# ── admin endpoints ───────────────────────────────────────────────────────────
@app.get("/admin/users")
async def list_users():
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
    key = f"whatsapp:+{phone_number}"
    return conversations.get(key, {"error": "not found"})


@app.post("/admin/introduce")
async def introduce_two_people(payload: dict):
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
