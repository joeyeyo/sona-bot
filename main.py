from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
import os
import json
import httpx
import re
from datetime import datetime
from statistics import mean

app = FastAPI()

# ── CORS ──────────────────────────────────────────────────────────────────────
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

# ── in-memory store ───────────────────────────────────────────────────────────
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

Keep responses SHORT — this is WhatsApp, not email. 2-4 sentences max per message. No bullet points or markdown formatting. Just natural, conversational text."""


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


# ── vendor ranking ────────────────────────────────────────────────────────────
def bayesian_score(rating: float, review_count: int, mean_rating: float, C: int = 50) -> float:
    """
    Bayesian average — blends vendor rating with global mean.
    Vendors with few reviews get pulled toward the mean.
    C = how many reviews needed to fully trust a rating.
    """
    return (C * mean_rating + rating * review_count) / (C + review_count)


def rank_vendors(vendors: list, search_params: dict) -> list:
    if not vendors:
        return vendors

    ratings = [v.get("rating", 0) for v in vendors if v.get("rating")]
    mean_rating = mean(ratings) if ratings else 4.5
    keyword = search_params.get("keywords", "").lower()

    price_boosts = {"$": 1.0, "$$": 0.9, "$$$": 0.7, "$$$$": 0.5}

    for v in vendors:
        # Step 1: Bayesian base score
        b_score = bayesian_score(
            v.get("rating", 0),
            v.get("review_count", 0),
            mean_rating,
        )

        # Step 2: Price boost
        price = v.get("price", "N/A")
        price_boost = price_boosts.get(price, 0.85)

        # Step 3: Keyword boost — does vendor name contain search keywords?
        keyword_boost = 1.1 if keyword and keyword in v.get("name", "").lower() else 1.0

        # Step 4: Review volume boost — reward vendors with lots of reviews
        review_boost = 1.0
        n = v.get("review_count", 0)
        if n >= 200:
            review_boost = 1.05
        elif n >= 100:
            review_boost = 1.02

        # Step 5: Email found boost — reward vendors we found contact info for
        email_boost = 1.05 if v.get("email") else 1.0

        v["score"] = round(b_score * price_boost * keyword_boost * review_boost * email_boost, 4)

    return sorted(vendors, key=lambda v: v["score"], reverse=True)


# ── email scraper ─────────────────────────────────────────────────────────────
async def scrape_email_from_website(website_url: str) -> str | None:
    """
    Visit a business website and look for email addresses.
    Checks homepage, /contact, and /about pages.
    """
    if not website_url:
        return None

    email_pattern = re.compile(
        r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'
    )

    # Domains to ignore (privacy/legal emails, not contact emails)
    ignore_domains = {
        "sentry.io", "wixpress.com", "squarespace.com", "wordpress.com",
        "shopify.com", "example.com", "yourdomain.com", "email.com",
    }

    # Pages to check
    base = website_url.rstrip("/")
    pages_to_check = [base, f"{base}/contact", f"{base}/contact-us", f"{base}/about"]

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    found_emails = []

    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        for page_url in pages_to_check:
            try:
                response = await client.get(page_url, headers=headers)
                if response.status_code == 200:
                    text = response.text

                    # Find mailto: links first (most reliable)
                    mailto_matches = re.findall(r'mailto:([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})', text)
                    found_emails.extend(mailto_matches)

                    # Also find plain email patterns
                    plain_matches = email_pattern.findall(text)
                    found_emails.extend(plain_matches)

                    if found_emails:
                        break  # Stop once we find emails on a page
            except Exception:
                continue

    # Filter and deduplicate
    clean_emails = []
    seen = set()
    for email in found_emails:
        email = email.lower().strip()
        domain = email.split("@")[-1]
        if domain not in ignore_domains and email not in seen:
            seen.add(email)
            clean_emails.append(email)

    # Prefer emails with contact/info/events/booking keywords
    priority_keywords = ["contact", "info", "hello", "events", "booking", "catering", "hire"]
    for email in clean_emails:
        local = email.split("@")[0]
        if any(kw in local for kw in priority_keywords):
            return email

    # Otherwise return first found
    return clean_emails[0] if clean_emails else None


async def get_website_from_yelp_page(yelp_url: str) -> str | None:
    """
    Visit a Yelp business page and extract their website URL.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            response = await client.get(yelp_url, headers=headers)
            if response.status_code == 200:
                # Look for website link pattern in Yelp page
                matches = re.findall(
                    r'href="https://www\.yelp\.com/biz_redir\?url=([^"&]+)',
                    response.text
                )
                if matches:
                    from urllib.parse import unquote
                    website = unquote(matches[0])
                    # Clean up any tracking params
                    website = website.split("?")[0]
                    return website
    except Exception:
        pass
    return None


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
    Search for vendors via Yelp, scrape emails, and rank results.
    POST body: {
        "category": "caterers",
        "location": "Los Angeles, CA",
        "budget": 600,
        "guest_count": 50,
        "min_rating": 4.25,
        "keywords": "asian food"
    }
    """
    category = payload.get("category", "caterers")
    location = payload.get("location", "Los Angeles, CA")
    budget = payload.get("budget")
    guest_count = payload.get("guest_count")
    min_rating = payload.get("min_rating", 4.0)
    keywords = payload.get("keywords", "")
    scrape_emails = payload.get("scrape_emails", True)

    search_term = f"{keywords} {category}".strip()

    # Step 1: Yelp search
    async with httpx.AsyncClient(timeout=10.0) as client:
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

    businesses = response.json().get("businesses", [])

    # Step 2: Filter by minimum rating
    filtered = [b for b in businesses if b.get("rating", 0) >= min_rating]

    # Step 3: Shape the data
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
            "coordinates": b.get("coordinates", {}),
            "email": None,
            "website": None,
        })

    # Step 4: Scrape emails (top 10 only to keep it fast)
    if scrape_emails and vendors:
        import asyncio

        async def enrich_vendor(vendor):
            try:
                # Try to get website from Yelp page
                website = await get_website_from_yelp_page(vendor["url"])
                if website:
                    vendor["website"] = website
                    email = await scrape_email_from_website(website)
                    vendor["email"] = email
            except Exception:
                pass
            return vendor

        # Run email scraping concurrently for top 10 vendors
        top_vendors = vendors[:10]
        rest_vendors = vendors[10:]
        enriched = await asyncio.gather(*[enrich_vendor(v) for v in top_vendors])
        vendors = list(enriched) + rest_vendors

    # Step 5: Rank with Bayesian algorithm
    vendors = rank_vendors(vendors, {"keywords": keywords})

    # Step 6: Claude AI reasons for top 5
    if vendors:
        summary_prompt = f"""I'm looking for {category} for an event with {guest_count} guests, budget under ${budget}.
Here are the top vendors: {json.dumps([{
    "id": v["id"], "name": v["name"], "rating": v["rating"],
    "review_count": v["review_count"], "price": v["price"],
    "categories": v["categories"], "email": v.get("email")
} for v in vendors[:5]], indent=2)}

For each vendor write a 1-sentence reason why they're a good fit for this event.
Respond ONLY with a JSON array: [{{"id": "...", "reason": "..."}}]
No markdown, no explanation."""

        try:
            claude_response = anthropic_client.messages.create(
                model="claude-opus-4-5",
                max_tokens=500,
                messages=[{"role": "user", "content": summary_prompt}],
            )
            rankings = json.loads(claude_response.content[0].text)
            ranked_ids = {r["id"]: r["reason"] for r in rankings}
            for v in vendors:
                v["ai_reason"] = ranked_ids.get(v["id"], "")
        except Exception:
            pass

    return {
        "vendors": vendors,
        "total": len(vendors),
        "emails_found": sum(1 for v in vendors if v.get("email")),
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
