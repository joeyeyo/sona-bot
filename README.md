# Sona — WhatsApp Networking Concierge

## Setup in 4 steps

### 1. Deploy to Railway
1. Push this folder to a GitHub repo
2. Go to railway.app → New Project → Deploy from GitHub
3. Select your repo

### 2. Set environment variables in Railway
In your Railway project → Variables tab, add:
```
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token_here
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxx
TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886
```

### 3. Point Twilio to your server
1. Railway will give you a URL like `https://sona-production.up.railway.app`
2. Go to Twilio Console → Messaging → Try it out → WhatsApp Sandbox Settings
3. Set "When a message comes in" webhook to:
   `https://your-railway-url.up.railway.app/webhook`
4. Method: HTTP POST

### 4. Test it
Send any message to your sandbox number on WhatsApp. Sona will respond!

---

## Admin API

Once deployed, you can use these endpoints:

### See all users
GET /admin/users

### Read a conversation
GET /admin/conversation/16263756580  (no + prefix)

### Introduce two people
POST /admin/introduce
```json
{
  "phone_a": "+16263756580",
  "phone_b": "+447911123456",
  "name_a": "Joe",
  "name_b": "Sarah",
  "reason": "You're both building in the health tech space and Sarah is looking for a co-founder",
  "icebreaker": "Ask her about her time at Stanford"
}
```

### Send event reminder to everyone
POST /admin/event-reminder
```json
{
  "event_name": "Sona Social #1",
  "date": "Saturday April 19th",
  "time": "7pm",
  "location": "The Lobby, Taipei"
}
```

---

## File structure
```
sona/
├── main.py           # All server logic
├── requirements.txt  # Python dependencies
├── railway.toml      # Railway deployment config
└── README.md
```

## Next steps (when you're ready)
- Replace in-memory `conversations` dict with a real database (Supabase is easiest)
- Add LinkedIn scraping/summarization
- Build a simple admin UI to view users and trigger matches
- Set up Meta Business verification for a real WhatsApp number
