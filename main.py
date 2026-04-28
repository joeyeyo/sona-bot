from fastapi import FastAPI, Form, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
import os
import json
import httpx
import re
import base64
from datetime import datetime
from statistics import mean
from urllib.parse import quote_plus, unquote, urlparse, quote
from email.mime.text import MIMEText

app = FastAPI()

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://sona-frontend-lyart.vercel.app", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── env vars ──────────────────────────────────────────────────────────────────
twilio_client = Client(
    os.environ["TWILIO_ACCOUNT_SID"],
    os.environ["TWILIO_AUTH_TOKEN"],
)
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
YELP_API_KEY = os.environ.get("YELP_API_KEY", "")
SERP_API_KEY = os.environ.get("SERP_API_KEY", "")
HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY", "")
APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN", "")
GMAIL_SENDER = os.environ.get("GMAIL_SENDER", "yeh.joseph@gmail.com")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()


# ── Phone normalization ────────────────────────────────────────────────────────
def normalize_phone(phone: str) -> str:
    phone = phone.strip()
    if not phone.startswith("whatsapp:"):
        phone = f"whatsapp:{phone}"
    return phone


def clean_message(text: str) -> str:
    """Strip any XML/TwiML wrapper that may have leaked into message content."""
    if not text:
        return text
    text = re.sub(r'<\?xml[^>]*\?>', '', text)
    text = re.sub(r'</?Response>', '', text)
    text = re.sub(r'<Message>(.*?)</Message>', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    return text.strip()


# ── Supabase helpers ──────────────────────────────────────────────────────────
def supa_headers():
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def supa_get(table: str, filters: dict) -> list:
    params = "&".join(f"{k}=eq.{quote(str(v), safe='')}" for k, v in filters.items())
    url = f"{SUPABASE_URL}/rest/v1/{table}?{params}"
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(url, headers=supa_headers())
        if resp.status_code == 200:
            return resp.json()
    return []


async def supa_insert(table: str, data: dict) -> dict | None:
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=supa_headers(),
            json=data,
        )
        if resp.status_code in (200, 201):
            result = resp.json()
            return result[0] if result else None
        print(f"[supa_insert error] {resp.status_code}: {resp.text}")
    return None


async def supa_update(table: str, filters: dict, data: dict) -> dict | None:
    params = "&".join(f"{k}=eq.{quote(str(v), safe='')}" for k, v in filters.items())
    data["updated_at"] = datetime.now().isoformat()
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.patch(
            f"{SUPABASE_URL}/rest/v1/{table}?{params}",
            headers=supa_headers(),
            json=data,
        )
        if resp.status_code == 200:
            result = resp.json()
            return result[0] if result else None
        print(f"[supa_update error] {resp.status_code}: {resp.text}")
    return None


async def supa_delete(table: str, filters: dict):
    params = "&".join(f"{k}=eq.{quote(str(v), safe='')}" for k, v in filters.items())
    async with httpx.AsyncClient(timeout=8.0) as client:
        await client.delete(
            f"{SUPABASE_URL}/rest/v1/{table}?{params}",
            headers=supa_headers(),
        )


async def get_or_create_guest(phone: str) -> dict:
    phone = normalize_phone(phone)
    rows = await supa_get("guests", {"phone": phone})
    if rows:
        return rows[0]
    new_guest = await supa_insert("guests", {"phone": phone})
    if new_guest:
        return new_guest
    print(f"[guest] Insert failed for {phone}, using in-memory fallback")
    return {"phone": phone}


async def get_or_create_conversation(phone: str) -> dict:
    phone = normalize_phone(phone)
    rows = await supa_get("conversations", {"phone": phone})
    if rows:
        conv = rows[0]
        messages = conv.get("messages") or []
        cleaned = [{"role": m.get("role", "assistant"), "content": clean_message(m.get("content", ""))} for m in messages]
        conv["messages"] = cleaned
        return conv
    new_conv = await supa_insert("conversations", {"phone": phone, "messages": [], "stage": "intro"})
    if new_conv:
        return new_conv
    print(f"[conv] Insert failed for {phone}, using in-memory fallback")
    return {"phone": phone, "messages": [], "stage": "intro"}


async def save_conversation(phone: str, messages: list, stage: str):
    phone = normalize_phone(phone)
    clean_messages = [{"role": m["role"], "content": clean_message(m["content"])} for m in messages]
    rows = await supa_get("conversations", {"phone": phone})
    if rows:
        await supa_update("conversations", {"phone": phone}, {"messages": clean_messages, "stage": stage})
    else:
        await supa_insert("conversations", {"phone": phone, "messages": clean_messages, "stage": stage})


async def get_active_event() -> dict | None:
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/events?order=created_at.desc&limit=1",
            headers=supa_headers(),
        )
        if resp.status_code == 200:
            rows = resp.json()
            return rows[0] if rows else None
    return None


async def get_confirmed_guests(event_id: str) -> list:
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/guests?event_id=eq.{event_id}&rsvp_status=eq.confirmed",
            headers=supa_headers(),
        )
        if resp.status_code == 200:
            return resp.json()
    return []


# ── LinkedIn URL extraction ────────────────────────────────────────────────────
def extract_linkedin_url(text: str) -> str | None:
    patterns = [
        r'https?://(?:www\.)?linkedin\.com/in/[\w\-]+/?',
        r'linkedin\.com/in/[\w\-]+/?',
        r'linked\.in/[\w\-]+/?',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            url = match.group()
            if not url.startswith("http"):
                url = "https://" + url
            return url
    return None


# ── Stage detection ────────────────────────────────────────────────────────────
def detect_next_stage(current_stage: str, user_message: str, guest: dict, linkedin_found: bool) -> str:
    msg_lower = user_message.lower().strip()

    if current_stage == "intro":
        return "what_they_do" if linkedin_found else "intro"

    if current_stage == "what_they_do":
        if len(user_message.strip()) > 10 and "linkedin" not in msg_lower:
            return "who_to_meet"
        return "what_they_do"

    if current_stage == "who_to_meet":
        return "interests" if len(user_message.strip()) > 3 else "who_to_meet"

    if current_stage == "interests":
        return "generate_invite" if len(user_message.strip()) > 3 else "interests"

    if current_stage == "generate_invite":
        return "rsvp"

    if current_stage == "rsvp":
        yes_signals = ["yes", "yeah", "yep", "sure", "in", "absolutely", "definitely",
                       "count me", "i'm in", "im in", "sounds good", "let's go", "lets go", "👍", "yea"]
        no_signals = ["no", "nope", "can't", "cant", "won't", "wont", "pass",
                      "not this time", "maybe next", "👎"]
        if any(s in msg_lower for s in yes_signals):
            return "confirmed"
        if any(s in msg_lower for s in no_signals):
            return "declined"
        return "rsvp"

    return current_stage


# ── Onboarding system prompt ───────────────────────────────────────────────────
def build_onboarding_prompt(event: dict | None, stage: str, guest: dict) -> str:
    event_info = ""
    if event:
        event_info = f"""
ACTIVE EVENT:
- Name: {event.get('name', 'An exclusive event')}
- Date: {event.get('date', 'TBD')}
- Venue: {event.get('venue', 'TBD')} in {event.get('city', 'Los Angeles')}
- Expected guests: {event.get('guest_count', '~30')}
- Vibe: {event.get('vibe', 'Intimate, curated networking')}
- Audience: {event.get('audience', 'Asian tech founders and operators')}
- Hosts: {event.get('host_name', 'Joe and Eric')}
"""

    guest_info = f"""
WHAT WE ALREADY KNOW (do NOT ask again):
- LinkedIn: {guest.get('linkedin_url') or 'not yet provided'}
- What they do: {guest.get('what_they_do') or 'not yet answered'}
- Who they want to meet: {guest.get('who_they_want_to_meet') or 'not yet answered'}
- Interests: {guest.get('interests') or 'not yet answered'}
- RSVP: {guest.get('rsvp_status', 'pending')}
"""

    stage_instructions = {
        "intro": "You just sent the intro. Wait for their LinkedIn URL. If they gave you one, acknowledge it and ask what they're working on. Do NOT ask for LinkedIn again if you already have it.",
        "what_they_do": "You have their LinkedIn. Ask ONE question: what are they currently working on or building?",
        "who_to_meet": "You know what they do. Ask ONE question: who are they most hoping to connect with?",
        "interests": "Ask ONE lightweight personal question — what do they do outside of work?",
        "generate_invite": "Write a compelling personalized 3-4 sentence invite. Reference exactly what they told you. End with 'You in?'",
        "rsvp": "Handle their yes/no response warmly.",
        "confirmed": "They said yes! Confirm their spot. Give date and venue. Say you'll send who to look for closer to the event.",
        "declined": "They said no. Be gracious. Tell them you'll keep them in mind for future events.",
    }

    return f"""You are Sona, an AI concierge for an exclusive invite-only event community in Los Angeles run by Joe Yeh and Eric Tsai.
{event_info}
{guest_info}
CURRENT STAGE: {stage}
WHAT TO DO NOW: {stage_instructions.get(stage, 'Continue naturally.')}

STRICT RULES:
- ONE question per message maximum
- SHORT messages — 2-3 sentences max, WhatsApp style
- NEVER repeat a question already answered
- NEVER ask for LinkedIn if you already have it
- NEVER ask what they do if you already have it
- Warm, human tone
- No bullet points, no markdown
- Do not reveal you are an AI unless asked"""


# ── Main onboarding handler ────────────────────────────────────────────────────
async def handle_guest_onboarding(phone: str, user_message: str) -> str:
    phone = normalize_phone(phone)

    conv = await get_or_create_conversation(phone)
    guest = await get_or_create_guest(phone)
    event = await get_active_event()

    messages = conv.get("messages") or []
    current_stage = conv.get("stage") or "intro"

    linkedin_url = extract_linkedin_url(user_message)

    if linkedin_url and not guest.get("linkedin_url"):
        await supa_update("guests", {"phone": phone}, {"linkedin_url": linkedin_url})
        guest["linkedin_url"] = linkedin_url
        print(f"[linkedin] Saved for {phone}: {linkedin_url}")

    next_stage = detect_next_stage(current_stage, user_message, guest, bool(linkedin_url))

    if current_stage == "what_they_do" and len(user_message.strip()) > 10 and "linkedin" not in user_message.lower():
        await supa_update("guests", {"phone": phone}, {"what_they_do": user_message})
        guest["what_they_do"] = user_message

    elif current_stage == "who_to_meet" and len(user_message.strip()) > 3:
        await supa_update("guests", {"phone": phone}, {"who_they_want_to_meet": user_message})
        guest["who_they_want_to_meet"] = user_message

    elif current_stage == "interests" and len(user_message.strip()) > 3:
        await supa_update("guests", {"phone": phone}, {"interests": user_message})
        guest["interests"] = user_message

    system_prompt = build_onboarding_prompt(event, next_stage, guest)
    if next_stage == "generate_invite" and event:
        confirmed = await get_confirmed_guests(event.get("id", ""))
        if confirmed:
            summaries = [f"- {g.get('name', 'Guest')}: {g.get('what_they_do', '')}" for g in confirmed[:5]]
            system_prompt += f"\n\nCONFIRMED GUESTS SO FAR:\n" + "\n".join(summaries)

    messages.append({"role": "user", "content": clean_message(user_message)})

    response = anthropic_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
        system=system_prompt,
        messages=messages[-20:],
    )
    reply = clean_message(response.content[0].text)
    messages.append({"role": "assistant", "content": reply})

    if next_stage == "confirmed":
        await supa_update("guests", {"phone": phone}, {
            "rsvp_status": "confirmed",
            "onboarding_complete": True,
            "event_id": event.get("id") if event else None,
        })
    elif next_stage == "declined":
        await supa_update("guests", {"phone": phone}, {"rsvp_status": "declined"})
    elif next_stage == "generate_invite":
        await supa_update("guests", {"phone": phone}, {"personalized_invite": reply})

    await save_conversation(phone, messages, next_stage)
    print(f"[stage] {phone}: {current_stage} → {next_stage}")
    return reply


# ── Gmail ─────────────────────────────────────────────────────────────────────
async def get_gmail_access_token() -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GMAIL_CLIENT_ID,
                "client_secret": GMAIL_CLIENT_SECRET,
                "refresh_token": GMAIL_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
        )
        data = resp.json()
        if "access_token" not in data:
            raise Exception(f"Failed to get access token: {data}")
        return data["access_token"]


async def send_gmail(to: str, subject: str, body: str) -> dict:
    access_token = await get_gmail_access_token()
    msg = MIMEText(body, "plain")
    msg["To"] = to
    msg["From"] = GMAIL_SENDER
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"raw": raw},
        )
    if resp.status_code == 200:
        return {"status": "sent", "message_id": resp.json().get("id")}
    raise Exception(f"Gmail send failed: {resp.status_code} {resp.text}")


# ── Vendor ranking ─────────────────────────────────────────────────────────────
def bayesian_score(rating: float, review_count: int, mean_rating: float, C: int = 50) -> float:
    return (C * mean_rating + rating * review_count) / (C + review_count)


def rank_vendors(vendors: list, search_params: dict) -> list:
    if not vendors:
        return vendors
    ratings = [v.get("rating", 0) for v in vendors if v.get("rating")]
    mean_rating = mean(ratings) if ratings else 4.5
    keyword = search_params.get("keywords", "").lower()
    price_boosts = {"$": 1.0, "$$": 0.9, "$$$": 0.7, "$$$$": 0.5}
    for v in vendors:
        b_score = bayesian_score(v.get("rating", 0), v.get("review_count", 0), mean_rating)
        price_boost = price_boosts.get(v.get("price", "N/A"), 0.85)
        keyword_boost = 1.1 if keyword and keyword in v.get("name", "").lower() else 1.0
        n = v.get("review_count", 0)
        review_boost = 1.05 if n >= 200 else (1.02 if n >= 100 else 1.0)
        email_boost = 1.05 if v.get("email") else 1.0
        v["score"] = round(b_score * price_boost * keyword_boost * review_boost * email_boost, 4)
    return sorted(vendors, key=lambda v: v["score"], reverse=True)


# ── Email extraction ───────────────────────────────────────────────────────────
EMAIL_PATTERN = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')

IGNORE_DOMAINS = {
    "sentry.io", "mapquest.com", "doubleclick.net", "googletagmanager.com",
    "googleapis.com", "gstatic.com", "cloudflare.com", "akamai.com",
    "newrelic.com", "segment.com", "mixpanel.com", "amplitude.com",
    "intercom.io", "zendesk.com", "hubspot.com", "mailchimp.com",
    "wixpress.com", "squarespace.com", "wordpress.com", "shopify.com",
    "weebly.com", "webflow.io", "godaddy.com",
    "example.com", "yourdomain.com", "email.com", "domain.com",
    "yoursite.com", "company.com",
    "google.com", "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "me.com", "mac.com", "live.com",
    "yelp.com", "facebook.com", "instagram.com", "twitter.com",
    "tiktok.com", "linkedin.com", "pinterest.com",
    "doordash.com", "grubhub.com", "ubereats.com", "tripadvisor.com",
    "opentable.com", "resy.com", "thumbtack.com",
    "duckduckgo.com", "bing.com",
    "giftly.com", "giggster.com", "heathercoros.com", "pandaexpressed.com",
    "escholarship.org", "partyslate.com",
    "png", "jpg", "jpeg", "gif", "webp", "svg", "pdf", "css", "js",
}

DIRECTORY_DOMAINS = {
    "partyslate.com", "theknot.com", "weddingwire.com", "thumbtack.com", "bark.com",
    "houzz.com", "expertise.com", "gigsalad.com", "gigmasters.com", "styleseat.com",
    "vagaro.com", "booksy.com", "eventective.com", "peerspace.com", "tagvenue.com",
    "yelp.com", "tripadvisor.com", "google.com", "bing.com", "yellowpages.com",
    "bbb.org", "manta.com", "zoominfo.com", "chamberofcommerce.com", "mapquest.com",
    "doordash.com", "grubhub.com", "ubereats.com", "postmates.com", "toasttab.com",
    "facebook.com", "instagram.com", "twitter.com", "linkedin.com", "pinterest.com", "tiktok.com",
    "escholarship.org", "academia.edu", "giftly.com", "giggster.com", "heathercoros.com",
}

PRIORITY_LOCAL_PARTS = ["contact", "info", "hello", "events", "booking", "catering",
                         "hire", "enquir", "inquir", "photo", "studio"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def is_directory_site(url: str) -> bool:
    if not url:
        return False
    domain = urlparse(url).netloc.replace("www.", "")
    return any(d in domain for d in DIRECTORY_DOMAINS)


def extract_emails_from_text(text: str) -> list:
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'\[at\]|\(at\)| at ', '@', text, flags=re.IGNORECASE)
    text = re.sub(r'\[dot\]|\(dot\)| dot ', '.', text, flags=re.IGNORECASE)
    mailto = re.findall(r'mailto:([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})', text)
    plain = EMAIL_PATTERN.findall(text)
    all_emails = mailto + plain
    clean = []
    seen = set()
    for email in all_emails:
        email = email.lower().strip().rstrip(".,;")
        parts = email.split("@")
        if len(parts) == 2 and parts[1].startswith("www."):
            email = parts[0] + "@" + parts[1][4:]
        domain = email.split("@")[-1]
        if domain in IGNORE_DOMAINS:
            continue
        if any(ignored in domain for ignored in IGNORE_DOMAINS if "." in ignored):
            continue
        if email in seen or len(email) > 80:
            continue
        seen.add(email)
        clean.append(email)

    def priority(e):
        local = e.split("@")[0]
        return 0 if any(kw in local for kw in PRIORITY_LOCAL_PARTS) else 1

    return sorted(clean, key=priority)


def email_matches_website(email: str, website: str) -> bool:
    if not email or not website:
        return False
    email_domain = email.split("@")[-1].replace("www.", "")
    site_domain = urlparse(website).netloc.replace("www.", "")
    return email_domain in site_domain or site_domain in email_domain


def is_valid_email(email: str, website: str | None) -> bool:
    if not email or not website:
        return False
    if is_directory_site(website):
        return False
    return email_matches_website(email, website)


def get_website_from_results(results: list) -> str | None:
    for result in results:
        link = result.get("link", "")
        if link and not is_directory_site(link):
            return link.split("?")[0].rstrip("/")
    return None


# ── Search APIs ────────────────────────────────────────────────────────────────
async def serp_search(query: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://serpapi.com/search",
            params={"q": query, "api_key": SERP_API_KEY, "engine": "google", "num": 5},
        )
        if resp.status_code == 200:
            return resp.json()
    return {}


async def serp_yelp_search(vendor_name: str, city: str) -> dict:
    if not SERP_API_KEY:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://serpapi.com/search",
                params={"engine": "yelp", "find_desc": vendor_name, "find_loc": city, "api_key": SERP_API_KEY},
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        print(f"[serp-yelp error] {e}")
    return {}


async def google_maps_find_website(vendor_name: str, address: str) -> str | None:
    if not GOOGLE_MAPS_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            search_resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={"query": f"{vendor_name} {address}", "key": GOOGLE_MAPS_API_KEY},
            )
            if search_resp.status_code != 200:
                return None
            results = search_resp.json().get("results", [])
            if not results:
                return None
            place_id = results[0].get("place_id")
            if not place_id:
                return None
            details_resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/details/json",
                params={"place_id": place_id, "fields": "website,name", "key": GOOGLE_MAPS_API_KEY},
            )
            if details_resp.status_code != 200:
                return None
            website = details_resp.json().get("result", {}).get("website")
            if website:
                website = website.rstrip("/")
                if is_directory_site(website):
                    return None
                return website
    except Exception as e:
        print(f"[gmaps error] {vendor_name}: {e}")
    return None


async def apollo_find_email(vendor_name: str, address: str) -> tuple:
    if not APOLLO_API_KEY:
        return None, None
    city = address.split(",")[-2].strip() if "," in address else ""
    state = address.split(",")[-1].strip().split()[0] if "," in address else ""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.apollo.io/v1/mixed_companies/search",
                headers={"Content-Type": "application/json", "Cache-Control": "no-cache", "X-Api-Key": APOLLO_API_KEY},
                json={"q_organization_name": vendor_name, "organization_locations": [f"{city}, {state}"] if city else [], "page": 1, "per_page": 3},
            )
            if resp.status_code in (403, 402):
                return None, None
            if resp.status_code != 200:
                return None, None
            organizations = resp.json().get("organizations", [])
            if not organizations:
                return None, None
            best_org = None
            for org in organizations:
                org_name = org.get("name", "").lower()
                vendor_words = [w for w in vendor_name.lower().split() if len(w) > 3]
                if any(w in org_name for w in vendor_words):
                    best_org = org
                    break
            if not best_org:
                best_org = organizations[0]
            website = best_org.get("website_url") or best_org.get("primary_domain")
            if website and not website.startswith("http"):
                website = f"https://{website}"
            if website and is_directory_site(website):
                website = None
            email = None
            if best_org.get("contact_emails"):
                email = best_org["contact_emails"][0]
            if not email and best_org.get("id"):
                people_resp = await client.post(
                    "https://api.apollo.io/v1/mixed_people/search",
                    headers={"Content-Type": "application/json", "Cache-Control": "no-cache", "X-Api-Key": APOLLO_API_KEY},
                    json={"organization_ids": [best_org["id"]], "page": 1, "per_page": 5,
                          "person_titles": ["owner", "manager", "catering manager", "event coordinator", "director"]},
                )
                if people_resp.status_code == 200:
                    for person in people_resp.json().get("people", []):
                        person_email = person.get("email")
                        if person_email and "@" in person_email and "apollo" not in person_email:
                            email = person_email
                            break
            return email, website
    except Exception as e:
        print(f"[apollo error] {vendor_name}: {e}")
        return None, None


async def hunter_find_email(domain: str) -> str | None:
    if not HUNTER_API_KEY or not domain:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": domain, "api_key": HUNTER_API_KEY, "limit": 5, "type": "generic"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            emails = data.get("data", {}).get("emails", [])
            if not emails:
                pattern = data.get("data", {}).get("pattern")
                if pattern:
                    for prefix in ["info", "contact", "hello", "events", "book"]:
                        candidate = f"{prefix}@{domain}"
                        if candidate.split("@")[-1] not in IGNORE_DOMAINS:
                            return candidate
                return None

            def email_priority(e):
                local = e.get("value", "").split("@")[0]
                score = e.get("confidence", 0)
                if any(kw in local for kw in PRIORITY_LOCAL_PARTS):
                    score += 50
                return score

            emails_sorted = sorted(emails, key=email_priority, reverse=True)
            best = emails_sorted[0].get("value")
            if best and best.split("@")[-1] in IGNORE_DOMAINS:
                return None
            return best
    except Exception as e:
        print(f"[hunter error] {domain}: {e}")
        return None


async def scrape_email_from_website(website_url: str) -> str | None:
    if not website_url or is_directory_site(website_url):
        return None
    base = website_url.rstrip("/")
    pages_to_check = [base, f"{base}/contact", f"{base}/contact-us", f"{base}/about"]
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        for page_url in pages_to_check:
            try:
                resp = await client.get(page_url, headers=HEADERS)
                if resp.status_code == 200:
                    emails = extract_emails_from_text(resp.text)
                    if emails:
                        return emails[0]
            except Exception:
                continue
    return None


async def find_vendor_email(vendor_name: str, address: str) -> tuple:
    city = address.split(",")[-2].strip() if "," in address else ""
    website = None

    apollo_email, apollo_website = await apollo_find_email(vendor_name, address)
    if apollo_website and not is_directory_site(apollo_website):
        website = apollo_website
    if apollo_email and is_valid_email(apollo_email, website):
        return apollo_email, website

    if not website:
        website = await google_maps_find_website(vendor_name, address)

    if not website:
        yelp_data = await serp_yelp_search(vendor_name, city)
        for result in yelp_data.get("organic_results", []):
            result_name = result.get("name", "").lower()
            vendor_words = [w for w in vendor_name.lower().split() if len(w) > 3]
            if any(w in result_name for w in vendor_words):
                candidate = result.get("website", "")
                if candidate and not is_directory_site(candidate):
                    website = candidate.rstrip("/")
                    break

    if not website:
        serp_site = await serp_search(f'"{vendor_name}" {city} official website')
        website = get_website_from_results(serp_site.get("organic_results", []))

    if not website:
        return None, None

    email = await scrape_email_from_website(website)
    if email and is_valid_email(email, website):
        return email, website

    domain = urlparse(website).netloc.replace("www.", "")
    if domain:
        email = await hunter_find_email(domain)
        if email and is_valid_email(email, website):
            return email, website

    serp_data = await serp_search(f'"{vendor_name}" {city} email contact')
    for result in serp_data.get("organic_results", []):
        for email in extract_emails_from_text(result.get("snippet", "") + " " + result.get("link", "")):
            if is_valid_email(email, website):
                return email, website

    try:
        ddg_url = f"https://html.duckduckgo.com/html/?q={quote_plus(f'{vendor_name} {city} email contact')}"
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(ddg_url, headers={**HEADERS, "Referer": "https://duckduckgo.com/"})
            if resp.status_code == 200:
                snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
                for snippet in snippets:
                    for email in extract_emails_from_text(re.sub(r'<[^>]+>', '', snippet)):
                        if is_valid_email(email, website):
                            return email, website
    except Exception as e:
        print(f"[ddg error] {e}")

    return None, website


def send_whatsapp_message(to: str, body: str):
    twilio_client.messages.create(from_=TWILIO_WHATSAPP_NUMBER, body=body, to=to)


# ── Webhook — returns application/xml so Twilio parses TwiML correctly ─────────
@app.post("/webhook")
async def webhook(From: str = Form(...), Body: str = Form(...)):
    phone = From
    user_message = Body.strip()
    print(f"[{phone}] → {user_message}")
    try:
        reply = await handle_guest_onboarding(phone, user_message)
    except Exception as e:
        print(f"[webhook error] {e}")
        import traceback
        traceback.print_exc()
        reply = "Hey! Something went wrong on my end. Give me a moment and try again."
    print(f"[Sona → {phone}] {reply}")
    resp = MessagingResponse()
    resp.message(reply)
    # Return as application/xml — this is critical for Twilio to parse correctly
    return Response(content=str(resp), media_type="application/xml")


# ── Vendor search ──────────────────────────────────────────────────────────────
@app.post("/search-vendors")
async def search_vendors(payload: dict):
    category = payload.get("category", "caterers")
    location = payload.get("location", "Los Angeles, CA")
    budget = payload.get("budget")
    guest_count = payload.get("guest_count")
    min_rating = payload.get("min_rating", 4.0)
    keywords = payload.get("keywords", "")
    scrape_emails = payload.get("scrape_emails", True)
    search_term = f"{keywords} {category}".strip()

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            "https://api.yelp.com/v3/businesses/search",
            headers={"Authorization": f"Bearer {YELP_API_KEY}"},
            params={"term": search_term, "location": location, "limit": 20, "sort_by": "rating"},
        )

    if response.status_code != 200:
        return {"error": "Yelp API error", "detail": response.text}

    businesses = response.json().get("businesses", [])
    filtered = [b for b in businesses if b.get("rating", 0) >= min_rating]

    vendors = []
    for b in filtered:
        vendors.append({
            "id": b.get("id"), "name": b.get("name"), "rating": b.get("rating"),
            "review_count": b.get("review_count"), "price": b.get("price", "N/A"),
            "phone": b.get("display_phone", "N/A"),
            "address": ", ".join(b.get("location", {}).get("display_address", [])),
            "url": b.get("url"), "image_url": b.get("image_url"),
            "categories": [c["title"] for c in b.get("categories", [])],
            "coordinates": b.get("coordinates", {}), "email": None, "website": None,
        })

    if scrape_emails and vendors:
        import asyncio

        async def enrich_vendor(vendor):
            try:
                email, website = await find_vendor_email(vendor["name"], vendor["address"])
                vendor["email"] = email
                vendor["website"] = website
            except Exception as e:
                print(f"[email error] {vendor['name']}: {e}")
            return vendor

        top = vendors[:10]
        rest = vendors[10:]
        enriched = await asyncio.gather(*[enrich_vendor(v) for v in top])
        vendors = list(enriched) + rest

    vendors = rank_vendors(vendors, {"keywords": keywords})

    if vendors:
        try:
            summary_prompt = f"""I'm looking for {category} for an event with {guest_count} guests, budget under ${budget}.
Here are the top vendors: {json.dumps([{"id": v["id"], "name": v["name"], "rating": v["rating"], "review_count": v["review_count"], "price": v["price"], "categories": v["categories"], "email": v.get("email")} for v in vendors[:5]], indent=2)}
For each vendor write a 1-sentence reason why they're a good fit.
Respond ONLY with a JSON array: [{{"id": "...", "reason": "..."}}]
No markdown, no explanation."""
            claude_response = anthropic_client.messages.create(
                model="claude-opus-4-5", max_tokens=500,
                messages=[{"role": "user", "content": summary_prompt}],
            )
            rankings = json.loads(claude_response.content[0].text)
            ranked_ids = {r["id"]: r["reason"] for r in rankings}
            for v in vendors:
                v["ai_reason"] = ranked_ids.get(v["id"], "")
        except Exception:
            pass

    return {
        "vendors": vendors, "total": len(vendors),
        "emails_found": sum(1 for v in vendors if v.get("email")),
        "query": {"category": category, "location": location, "budget": budget,
                  "guest_count": guest_count, "min_rating": min_rating}
    }


# ── Send vendor email ──────────────────────────────────────────────────────────
@app.post("/send-vendor-email")
async def send_vendor_email(payload: dict):
    to = payload.get("to")
    subject = payload.get("subject")
    body = payload.get("body")
    vendor_name = payload.get("vendor_name", "vendor")
    if not all([to, subject, body]):
        return {"error": "Missing required fields: to, subject, body"}
    if not all([GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN]):
        return {"error": "Gmail not configured in Railway env vars"}
    try:
        result = await send_gmail(to, subject, body)
        print(f"[gmail] Sent to {vendor_name} <{to}>: {subject}")
        return {"status": "sent", "to": to, "vendor_name": vendor_name,
                "subject": subject, "message_id": result.get("message_id")}
    except Exception as e:
        print(f"[gmail error] {e}")
        return {"error": str(e)}


# ── Guest endpoints ────────────────────────────────────────────────────────────
@app.get("/guests")
async def list_guests():
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/guests?order=created_at.desc",
            headers=supa_headers(),
        )
        if resp.status_code == 200:
            return resp.json()
    return []


@app.get("/guests/{phone}")
async def get_guest(phone: str):
    phone = normalize_phone(phone)
    rows = await supa_get("guests", {"phone": phone})
    return rows[0] if rows else {"error": "not found"}


@app.patch("/guests/{phone}")
async def update_guest(phone: str, payload: dict):
    phone = normalize_phone(phone)
    result = await supa_update("guests", {"phone": phone}, payload)
    return result or {"error": "update failed"}


# ── Event endpoints ────────────────────────────────────────────────────────────
@app.get("/events")
async def list_events():
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/events?order=created_at.desc",
            headers=supa_headers(),
        )
        if resp.status_code == 200:
            return resp.json()
    return []


@app.post("/events")
async def create_event(payload: dict):
    result = await supa_insert("events", payload)
    return result or {"error": "create failed"}


@app.patch("/events/{event_id}")
async def update_event(event_id: str, payload: dict):
    result = await supa_update("events", {"id": event_id}, payload)
    return result or {"error": "update failed"}


# ── Admin endpoints ────────────────────────────────────────────────────────────
@app.post("/admin/send-intro")
async def send_intro(payload: dict):
    phone = payload.get("phone")
    from_name = payload.get("from_name", "Joe")
    if not phone:
        return {"error": "phone required"}
    phone = normalize_phone(phone)
    intro_msg = f"Hey! I'm Sona, {from_name}'s AI concierge. He asked me to reach out about something exclusive he's putting together in LA. Before I tell you about it — what's your LinkedIn? Want to make sure this is the right fit for you."
    send_whatsapp_message(phone, intro_msg)
    await save_conversation(phone, [{"role": "assistant", "content": intro_msg}], "intro")
    return {"status": "sent", "to": phone}


@app.post("/admin/reset-conversation")
async def reset_conversation(payload: dict):
    phone = normalize_phone(payload.get("phone", ""))
    if not phone:
        return {"error": "phone required"}
    await supa_delete("conversations", {"phone": phone})
    await supa_delete("guests", {"phone": phone})
    return {"status": "reset", "phone": phone}


@app.post("/admin/event-reminder")
async def send_event_reminder(payload: dict):
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/rest/v1/guests?rsvp_status=eq.confirmed",
            headers=supa_headers(),
        )
        confirmed = resp.json() if resp.status_code == 200 else []
    phones = [g["phone"] for g in confirmed if g.get("phone")]
    msg = f"🗓 Reminder: {payload.get('event_name', 'the event')} is happening {payload.get('date', 'soon')}! Come to {payload.get('location', '')}. See you there!"
    for phone in phones:
        send_whatsapp_message(phone, msg)
    return {"status": "sent", "count": len(phones)}


# ── Debug / health ─────────────────────────────────────────────────────────────
@app.get("/debug-env")
async def debug_env():
    return {
        "supabase_url_preview": SUPABASE_URL[:40] if SUPABASE_URL else "EMPTY",
        "supabase_url_len": len(SUPABASE_URL),
        "supabase_url_starts_with_https": SUPABASE_URL.startswith("https://"),
        "supabase_service_key_set": bool(SUPABASE_SERVICE_KEY),
        "supabase_service_key_len": len(SUPABASE_SERVICE_KEY),
    }


@app.post("/debug-email")
async def debug_email(payload: dict):
    name = payload.get("name", "Rutt's Catering")
    address = payload.get("address", "Los Angeles, CA")
    results = {
        "supabase_url": SUPABASE_URL[:30] if SUPABASE_URL else "EMPTY",
        "apollo_key_set": bool(APOLLO_API_KEY),
        "serp_key_set": bool(SERP_API_KEY),
        "hunter_key_set": bool(HUNTER_API_KEY),
        "gmaps_key_set": bool(GOOGLE_MAPS_API_KEY),
    }
    apollo_email, apollo_website = await apollo_find_email(name, address)
    results["apollo_email"] = apollo_email
    results["apollo_website"] = apollo_website
    results["google_maps_website"] = await google_maps_find_website(name, address)
    email, website = await find_vendor_email(name, address)
    results["final_email"] = email
    results["final_website"] = website
    return results


@app.get("/health")
async def health():
    guest_count = 0
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/rest/v1/guests?select=count",
                headers={**supa_headers(), "Prefer": "count=exact"},
            )
            if resp.status_code == 200:
                guest_count = int(resp.headers.get("content-range", "0/0").split("/")[-1])
    except Exception:
        pass
    return {"status": "ok", "guests_in_db": guest_count}
