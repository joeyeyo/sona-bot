import { useState, useRef, useEffect } from "react";

const WORKSTREAMS = [
  {
    id: "marketing",
    label: "Marketing",
    icon: "📣",
    color: "#FF6B35",
    templates: [
      "Write 3 Instagram posts for my event",
      "Create a posting schedule for the next 3 weeks",
      "Write DM outreach copy for my network",
      "Write an event description for the invite page",
    ],
  },
  {
    id: "vendors",
    label: "Vendors",
    icon: "🤝",
    color: "#00D4FF",
    templates: [
      "Draft a cold email to a photographer",
      "Write outreach to a catering vendor",
      "Draft a venue inquiry email",
      "Compare vendor quotes and recommend one",
    ],
  },
  {
    id: "guests",
    label: "Guests",
    icon: "👥",
    color: "#A8FF3E",
    templates: [
      "Write personalized invitation messages",
      "Draft a WhatsApp blast for my network",
      "Write an RSVP reminder message",
      "Create a waitlist response message",
    ],
  },
  {
    id: "logistics",
    label: "Logistics",
    icon: "📋",
    color: "#FF3E9A",
    templates: [
      "Generate a full planning timeline",
      "Create a day-of run-of-show",
      "Build a vendor confirmation checklist",
      "Write a day-of setup checklist",
    ],
  },
];

const EVENT_CONTEXT = `You are Sona, an expert AI event planner helping organize an intimate AI networking event for Asian tech founders in Los Angeles. 

Event details:
- Name: Sona AI Night — Asian Tech Founders LA
- Audience: Asian early-stage tech founders and entrepreneurs in LA
- Vibe: Intimate, curated, high-signal — not a big conference, more like an exclusive dinner party meets founder salon
- Size: ~50 people
- Host has an Instagram with 900 followers, many of whom are entrepreneurs
- Host's network is primarily Chinese and Korean founders but also cross-racial entrepreneurs

Your job is to generate ready-to-use content — copy they can send or post immediately, with minimal editing. Be specific, punchy, and authentic. Avoid corporate speak. Write like a founder, not a marketer. When writing Instagram content, make it feel real and personal, not promotional.`;

function TypingIndicator() {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "5px", padding: "14px 18px" }}>
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          style={{
            width: "6px",
            height: "6px",
            borderRadius: "50%",
            background: "#FF6B35",
            animation: "bounce 1.2s ease-in-out infinite",
            animationDelay: `${i * 0.2}s`,
          }}
        />
      ))}
    </div>
  );
}

function Message({ msg, workstreams }) {
  const ws = workstreams.find((w) => w.id === msg.workstream);
  return (
    <div
      style={{
        display: "flex",
        flexDirection: msg.role === "user" ? "row-reverse" : "row",
        gap: "12px",
        alignItems: "flex-start",
        marginBottom: "24px",
      }}
    >
      {msg.role === "assistant" && (
        <div
          style={{
            width: "32px",
            height: "32px",
            borderRadius: "50%",
            background: "linear-gradient(135deg, #FF6B35, #FF3E9A)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: "14px",
            flexShrink: 0,
            marginTop: "2px",
          }}
        >
          S
        </div>
      )}
      <div
        style={{
          maxWidth: "75%",
          background: msg.role === "user" ? "#1A1A2E" : "#0F0F1A",
          border: msg.role === "user"
            ? "1px solid #2A2A4A"
            : `1px solid ${ws?.color || "#FF6B35"}22`,
          borderRadius: msg.role === "user" ? "18px 18px 4px 18px" : "4px 18px 18px 18px",
          padding: "14px 18px",
        }}
      >
        {msg.workstream && msg.role === "assistant" && ws && (
          <div
            style={{
              fontSize: "10px",
              fontFamily: "'Space Mono', monospace",
              color: ws.color,
              letterSpacing: "0.1em",
              marginBottom: "8px",
              textTransform: "uppercase",
            }}
          >
            {ws.icon} {ws.label}
          </div>
        )}
        <div
          style={{
            fontSize: "14px",
            lineHeight: "1.7",
            color: "#E8E8F0",
            whiteSpace: "pre-wrap",
            fontFamily: "'DM Sans', sans-serif",
          }}
        >
          {msg.content}
        </div>
      </div>
    </div>
  );
}

export default function SonaAgent() {
  const [messages, setMessages] = useState([
    {
      role: "assistant",
      content: "Hey — I'm Sona, your AI event planner.\n\nI'm here to help you execute your AI Night for Asian tech founders in LA. Pick a workstream on the left, use a template, or just tell me what you need.\n\nLet's build something worth attending.",
      workstream: null,
    },
  ]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [activeWorkstream, setActiveWorkstream] = useState("marketing");
  const [history, setHistory] = useState([]);
  const bottomRef = useRef(null);
  const textareaRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  const sendMessage = async (text, workstreamId) => {
    const ws = workstreamId || activeWorkstream;
    const userMsg = { role: "user", content: text, workstream: ws };
    const newHistory = [...history, { role: "user", content: text }];

    setMessages((prev) => [...prev, userMsg]);
    setHistory(newHistory);
    setInput("");
    setLoading(true);

    const wsLabel = WORKSTREAMS.find((w) => w.id === ws)?.label || "";

    try {
      const response = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",

headers: {
  "Content-Type": "application/json",
  "x-api-key": import.meta.env.VITE_ANTHROPIC_API_KEY,
  "anthropic-version": "2023-06-01",
  "anthropic-dangerous-direct-browser-access": "true",
},       
 
        body: JSON.stringify({
          model: "claude-sonnet-4-20250514",
          max_tokens: 1000,
          system: EVENT_CONTEXT + `\n\nCurrent workstream focus: ${wsLabel}. Tailor your response to this area.`,
          messages: newHistory,
        }),
      });

      const data = await response.json();
      const reply = data.content?.[0]?.text || "Something went wrong. Try again.";
      const assistantMsg = { role: "assistant", content: reply, workstream: ws };

      setMessages((prev) => [...prev, assistantMsg]);
      setHistory((prev) => [...prev, { role: "assistant", content: reply }]);
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: "Error reaching the API. Check your connection.", workstream: ws },
      ]);
    } finally {
      setLoading(false);
    }
  };

  const handleKey = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (input.trim() && !loading) sendMessage(input.trim());
    }
  };

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;500;600&family=Bebas+Neue&display=swap');
        
        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        body { background: #07070F; }

        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #2A2A4A; border-radius: 2px; }

        @keyframes bounce {
          0%, 80%, 100% { transform: translateY(0); opacity: 0.4; }
          40% { transform: translateY(-6px); opacity: 1; }
        }

        @keyframes fadeIn {
          from { opacity: 0; transform: translateY(8px); }
          to { opacity: 1; transform: translateY(0); }
        }

        .ws-btn {
          background: transparent;
          border: 1px solid #1A1A2E;
          border-radius: 10px;
          padding: 12px 14px;
          cursor: pointer;
          transition: all 0.2s ease;
          text-align: left;
          width: 100%;
        }

        .ws-btn:hover {
          border-color: #2A2A4A;
          background: #0F0F1A;
        }

        .ws-btn.active {
          background: #0F0F1A;
        }

        .template-btn {
          background: #0A0A18;
          border: 1px solid #1A1A2E;
          border-radius: 8px;
          padding: 9px 12px;
          cursor: pointer;
          transition: all 0.15s ease;
          text-align: left;
          width: 100%;
          color: #8888AA;
          font-family: 'DM Sans', sans-serif;
          font-size: 12px;
          line-height: 1.4;
        }

        .template-btn:hover {
          background: #12121F;
          border-color: #2A2A4A;
          color: #E8E8F0;
        }

        .send-btn {
          background: linear-gradient(135deg, #FF6B35, #FF3E9A);
          border: none;
          border-radius: 10px;
          width: 40px;
          height: 40px;
          cursor: pointer;
          display: flex;
          align-items: center;
          justify-content: center;
          flex-shrink: 0;
          transition: opacity 0.2s;
          font-size: 16px;
        }

        .send-btn:hover { opacity: 0.85; }
        .send-btn:disabled { opacity: 0.3; cursor: not-allowed; }

        .msg-animate { animation: fadeIn 0.3s ease forwards; }

        textarea {
          background: transparent;
          border: none;
          outline: none;
          color: #E8E8F0;
          font-family: 'DM Sans', sans-serif;
          font-size: 14px;
          resize: none;
          width: 100%;
          line-height: 1.5;
        }

        textarea::placeholder { color: #3A3A5A; }
      `}</style>

      <div style={{
        display: "flex",
        height: "100vh",
        background: "#07070F",
        fontFamily: "'DM Sans', sans-serif",
      }}>
        {/* Sidebar */}
        <div style={{
          width: "260px",
          flexShrink: 0,
          borderRight: "1px solid #0F0F1A",
          display: "flex",
          flexDirection: "column",
          padding: "24px 16px",
          gap: "8px",
          overflowY: "auto",
        }}>
          {/* Logo */}
          <div style={{ marginBottom: "24px", paddingLeft: "4px" }}>
            <div style={{
              fontFamily: "'Bebas Neue', sans-serif",
              fontSize: "28px",
              letterSpacing: "0.08em",
              background: "linear-gradient(135deg, #FF6B35, #FF3E9A)",
              WebkitBackgroundClip: "text",
              WebkitTextFillColor: "transparent",
              lineHeight: 1,
            }}>SONA</div>
            <div style={{
              fontFamily: "'Space Mono', monospace",
              fontSize: "9px",
              color: "#3A3A5A",
              letterSpacing: "0.15em",
              marginTop: "3px",
            }}>EVENT INTELLIGENCE</div>
          </div>

          {/* Event badge */}
          <div style={{
            background: "#0F0F1A",
            border: "1px solid #1A1A2E",
            borderRadius: "10px",
            padding: "12px 14px",
            marginBottom: "16px",
          }}>
            <div style={{ fontSize: "10px", fontFamily: "'Space Mono', monospace", color: "#3A3A5A", letterSpacing: "0.1em", marginBottom: "6px" }}>ACTIVE EVENT</div>
            <div style={{ fontSize: "13px", fontWeight: 600, color: "#E8E8F0", lineHeight: 1.3 }}>AI Night — Asian Tech Founders LA</div>
            <div style={{ fontSize: "11px", color: "#5A5A7A", marginTop: "4px" }}>~50 guests · Los Angeles</div>
          </div>

          {/* Workstreams */}
          <div style={{ fontSize: "10px", fontFamily: "'Space Mono', monospace", color: "#3A3A5A", letterSpacing: "0.1em", marginBottom: "6px", paddingLeft: "4px" }}>WORKSTREAMS</div>

          {WORKSTREAMS.map((ws) => (
            <button
              key={ws.id}
              className={`ws-btn ${activeWorkstream === ws.id ? "active" : ""}`}
              onClick={() => setActiveWorkstream(ws.id)}
              style={{ borderColor: activeWorkstream === ws.id ? ws.color + "44" : undefined }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                <span style={{ fontSize: "16px" }}>{ws.icon}</span>
                <div>
                  <div style={{
                    fontSize: "13px",
                    fontWeight: 500,
                    color: activeWorkstream === ws.id ? ws.color : "#8888AA",
                    transition: "color 0.2s",
                  }}>{ws.label}</div>
                </div>
              </div>
            </button>
          ))}

          {/* Templates */}
          {(() => {
            const ws = WORKSTREAMS.find((w) => w.id === activeWorkstream);
            return ws ? (
              <div style={{ marginTop: "16px" }}>
                <div style={{ fontSize: "10px", fontFamily: "'Space Mono', monospace", color: "#3A3A5A", letterSpacing: "0.1em", marginBottom: "8px", paddingLeft: "4px" }}>QUICK PROMPTS</div>
                <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
                  {ws.templates.map((t, i) => (
                    <button
                      key={i}
                      className="template-btn"
                      onClick={() => sendMessage(t, ws.id)}
                      disabled={loading}
                    >
                      {t}
                    </button>
                  ))}
                </div>
              </div>
            ) : null;
          })()}
        </div>

        {/* Main chat */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
          {/* Header */}
          <div style={{
            padding: "20px 28px",
            borderBottom: "1px solid #0F0F1A",
            display: "flex",
            alignItems: "center",
            gap: "12px",
          }}>
            {(() => {
              const ws = WORKSTREAMS.find((w) => w.id === activeWorkstream);
              return ws ? (
                <>
                  <div style={{
                    width: "8px", height: "8px", borderRadius: "50%",
                    background: ws.color,
                    boxShadow: `0 0 8px ${ws.color}`,
                  }} />
                  <span style={{ fontSize: "13px", fontFamily: "'Space Mono', monospace", color: ws.color, letterSpacing: "0.08em" }}>
                    {ws.label.toUpperCase()}
                  </span>
                  <span style={{ color: "#2A2A4A", fontSize: "13px" }}>·</span>
                  <span style={{ fontSize: "13px", color: "#5A5A7A" }}>
                    {ws.id === "marketing" && "Instagram, DMs, event copy"}
                    {ws.id === "vendors" && "Photographers, catering, venues"}
                    {ws.id === "guests" && "Invitations, RSVPs, reminders"}
                    {ws.id === "logistics" && "Timelines, checklists, run-of-show"}
                  </span>
                </>
              ) : null;
            })()}
          </div>

          {/* Messages */}
          <div style={{ flex: 1, overflowY: "auto", padding: "28px" }}>
            {messages.map((msg, i) => (
              <div key={i} className="msg-animate">
                <Message msg={msg} workstreams={WORKSTREAMS} />
              </div>
            ))}
            {loading && (
              <div style={{ display: "flex", gap: "12px", alignItems: "flex-start", marginBottom: "24px" }}>
                <div style={{
                  width: "32px", height: "32px", borderRadius: "50%",
                  background: "linear-gradient(135deg, #FF6B35, #FF3E9A)",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  fontSize: "14px", flexShrink: 0,
                }}>S</div>
                <div style={{
                  background: "#0F0F1A",
                  border: "1px solid #FF6B3522",
                  borderRadius: "4px 18px 18px 18px",
                }}>
                  <TypingIndicator />
                </div>
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          {/* Input */}
          <div style={{
            padding: "16px 28px 24px",
            borderTop: "1px solid #0F0F1A",
          }}>
            <div style={{
              background: "#0F0F1A",
              border: "1px solid #1A1A2E",
              borderRadius: "14px",
              padding: "14px 14px 14px 18px",
              display: "flex",
              alignItems: "flex-end",
              gap: "12px",
              transition: "border-color 0.2s",
            }}
              onFocus={() => {}}
            >
              <textarea
                ref={textareaRef}
                rows={1}
                value={input}
                onChange={(e) => {
                  setInput(e.target.value);
                  e.target.style.height = "auto";
                  e.target.style.height = Math.min(e.target.scrollHeight, 120) + "px";
                }}
                onKeyDown={handleKey}
                placeholder={`Ask Sona about ${WORKSTREAMS.find(w => w.id === activeWorkstream)?.label.toLowerCase()}...`}
                style={{ maxHeight: "120px" }}
              />
              <button
                className="send-btn"
                onClick={() => input.trim() && !loading && sendMessage(input.trim())}
                disabled={!input.trim() || loading}
              >
                ↑
              </button>
            </div>
            <div style={{
              fontSize: "11px",
              fontFamily: "'Space Mono', monospace",
              color: "#2A2A4A",
              textAlign: "center",
              marginTop: "10px",
              letterSpacing: "0.05em",
            }}>
              SHIFT+ENTER FOR NEW LINE · ENTER TO SEND
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
