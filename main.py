"""
╔══════════════════════════════════════════════════════════╗
║          JUSTIA — FastAPI Backend Server                 ║
║  Run:  uvicorn main:app --reload --port 8000             ║
╚══════════════════════════════════════════════════════════╝
"""

import os
import json
import time
import random
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import anthropic  # pip install anthropic

# Import our legal data
import sys
sys.path.append(os.path.dirname(__file__))
from data.legal_data import (
    STATES, CASE_TYPES, MOCK_RESPONSES,
    NGOS, MOCK_COURT_CASES, PLATFORM_STATS
)

# ── APP SETUP ─────────────────────────────────────────────────────
app = FastAPI(
    title="JUSTIA API",
    description="Multilingual AI Legal Assistant for India",
    version="1.0.0",
)

# Allow your frontend to talk to backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # In production: set to your domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── CLAUDE CLIENT ─────────────────────────────────────────────────
# Get your free API key at: https://console.anthropic.com
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# ── SYSTEM PROMPT FOR JUSTIA ──────────────────────────────────────
JUSTIA_SYSTEM_PROMPT = """You are JUSTIA, an AI legal information assistant for India. You help ordinary citizens understand their legal rights and navigate the legal system.

CRITICAL RULES — follow these strictly:
1. You provide legal INFORMATION, never legal ADVICE. Always make this distinction clear.
2. Always end responses with: "⚠️ This is legal information, not legal advice. For binding legal counsel, consult a licensed advocate."
3. Cite the specific Indian law (Act name + Section) for every legal statement.
4. Keep language simple — assume user has 8th grade education. No jargon.
5. Always ask which STATE the user is in before giving specific information (laws vary by state).
6. If asked about urgent matters (domestic violence, criminal cases), immediately provide helpline numbers.
7. Never tell a user what they SHOULD do legally — only explain what the LAW SAYS and what OPTIONS EXIST.
8. Respond in the SAME LANGUAGE as the user's message (Hindi, Tamil, Telugu, Bengali, or English).

RESPONSE FORMAT:
- Use clear headings with emojis
- Bullet points for documents and steps
- Bold key legal terms
- Keep responses under 300 words unless user asks for detail

LEGAL DISCLAIMERS TO ADD:
- Consumer complaints → mention e-Daakhil portal (edaakhil.nic.in)
- Domestic violence → immediately give 181 helpline
- Labour disputes → mention free Labour Commissioner service
- Rental → mention Model Tenancy Act, 2021

You have access to state-specific legal information for all 28 Indian states."""

# ── REQUEST MODELS ─────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    language: str = "en"          # en, hi, ta, te, bn
    state: Optional[str] = None   # maharashtra, delhi, etc.
    case_type: Optional[str] = None
    conversation_history: list = []

class CourtLookupRequest(BaseModel):
    case_number: str
    state: Optional[str] = None

class NGOSearchRequest(BaseModel):
    state: str
    case_type: str

# ── HELPER: Build Context-Aware Prompt ────────────────────────────
def build_context_prompt(req: ChatRequest) -> str:
    context_parts = []

    if req.state and req.state in STATES:
        s = STATES[req.state]
        context_parts.append(f"""
STATE CONTEXT — {s['name']}:
- High Court: {s['high_court']}
- Rent Act: {s['rent_act']}
- Legal Aid: {s['legal_aid_authority']} | Helpline: {s['legal_aid_phone']}
- Free legal aid income limit: ₹{s['income_limit_legal_aid']:,}/year
- Consumer Forum: {s['consumer_forum']}
""")

    if req.case_type and req.case_type in CASE_TYPES:
        ct = CASE_TYPES[req.case_type]
        context_parts.append(f"""
CASE TYPE CONTEXT — {ct['name']}:
- Primary Laws: {', '.join(ct['primary_acts'])}
- Average resolution: {ct['avg_resolution_days']} days
- Success rate: {ct['success_rate_percent']}%
""")

    language_instruction = {
        "hi": "Respond ENTIRELY in Hindi (Devanagari script).",
        "ta": "Respond ENTIRELY in Tamil script.",
        "te": "Respond ENTIRELY in Telugu script.",
        "bn": "Respond ENTIRELY in Bengali script.",
        "en": "Respond in clear, simple English.",
    }.get(req.language, "Respond in English.")

    return f"{language_instruction}\n\n{''.join(context_parts)}"


# ══════════════════════════════════════════════════════════════════
#  API ENDPOINTS
# ══════════════════════════════════════════════════════════════════

# ── ROOT ──────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "service": "JUSTIA API",
        "version": "1.0.0",
        "status": "online",
        "endpoints": [
            "/api/chat",
            "/api/chat/stream",
            "/api/states",
            "/api/case-types",
            "/api/legal-info/{case_type}/{state}",
            "/api/court-lookup",
            "/api/ngos",
            "/api/stats",
            "/api/documents/{case_type}",
        ]
    }

# ── HEALTH CHECK ──────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {
        "status": "healthy",
        "claude_available": claude_client is not None,
        "timestamp": datetime.now().isoformat(),
    }

# ── CHAT ENDPOINT (Main AI) ───────────────────────────────────────
@app.post("/api/chat")
async def chat(req: ChatRequest):
    """
    Main AI chat endpoint.
    Uses Claude API if key is set, falls back to structured mock responses.
    """
    start_time = time.time()

    # Build message history for Claude
    messages = []
    for h in req.conversation_history[-10:]:  # last 10 messages for context
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": req.message})

    context = build_context_prompt(req)

    # ── Try Claude API ────────────────────────────────────────────
    if claude_client:
        try:
            response = claude_client.messages.create(
                model="claude-opus-4-6",
                max_tokens=1024,
                system=JUSTIA_SYSTEM_PROMPT + "\n\n" + context,
                messages=messages,
            )
            reply = response.content[0].text

            return {
                "reply": reply,
                "source": "claude",
                "language": req.language,
                "response_time_ms": round((time.time() - start_time) * 1000),
                "disclaimer": True,
            }

        except Exception as e:
            # Fall through to mock
            print(f"Claude API error: {e}")

    # ── Fallback: Smart Mock Response ────────────────────────────
    reply = generate_mock_response(req)
    return {
        "reply": reply,
        "source": "mock",
        "language": req.language,
        "response_time_ms": round((time.time() - start_time) * 1000),
        "disclaimer": True,
    }

# ── STREAMING CHAT ────────────────────────────────────────────────
@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """
    Streaming chat for real-time typewriter effect in frontend.
    """
    if not claude_client:
        # Mock streaming — yield character by character
        async def mock_stream():
            reply = generate_mock_response(req)
            for char in reply:
                yield f"data: {json.dumps({'delta': char})}\n\n"
                await asyncio.sleep(0.01)
            yield "data: [DONE]\n\n"
        return StreamingResponse(mock_stream(), media_type="text/event-stream")

    messages = []
    for h in req.conversation_history[-10:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": req.message})
    context = build_context_prompt(req)

    async def claude_stream():
        with claude_client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=1024,
            system=JUSTIA_SYSTEM_PROMPT + "\n\n" + context,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                yield f"data: {json.dumps({'delta': text})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(claude_stream(), media_type="text/event-stream")

# ── STATES LIST ───────────────────────────────────────────────────
@app.get("/api/states")
def get_states():
    """Returns all supported Indian states with metadata."""
    return {
        "states": [
            {
                "id": k,
                "name": v["name"],
                "high_court": v["high_court"],
                "legal_aid_phone": v["legal_aid_phone"],
            }
            for k, v in STATES.items()
        ],
        "total": len(STATES),
    }

# ── CASE TYPES ────────────────────────────────────────────────────
@app.get("/api/case-types")
def get_case_types():
    """Returns all supported legal case types."""
    return {
        "case_types": [
            {
                "id": k,
                "name": v["name"],
                "icon": v["icon"],
                "success_rate": v["success_rate_percent"],
                "avg_days": v["avg_resolution_days"],
            }
            for k, v in CASE_TYPES.items()
        ]
    }

# ── FULL LEGAL INFO ───────────────────────────────────────────────
@app.get("/api/legal-info/{case_type}/{state}")
def get_legal_info(case_type: str, state: str):
    """
    Returns complete legal information for a case type + state combination.
    This powers the document checklist and step-by-step guide.
    """
    if case_type not in CASE_TYPES:
        raise HTTPException(404, f"Case type '{case_type}' not found")
    if state not in STATES:
        raise HTTPException(404, f"State '{state}' not found")

    ct = CASE_TYPES[case_type]
    st = STATES[state]

    return {
        "case_type": {
            "id": case_type,
            "name": ct["name"],
            "primary_acts": ct["primary_acts"],
            "required_documents": ct["required_documents"],
            "steps": ct["steps"],
            "success_rate_percent": ct["success_rate_percent"],
            "avg_resolution_days": ct["avg_resolution_days"],
            "forums": ct.get("forums_by_amount") or ct.get("jurisdiction"),
        },
        "state": {
            "name": st["name"],
            "high_court": st["high_court"],
            "legal_aid_authority": st["legal_aid_authority"],
            "legal_aid_phone": st["legal_aid_phone"],
            "legal_aid_url": st["legal_aid_url"],
            "relevant_act": st.get("rent_act") if case_type == "rental_deposit" else None,
            "consumer_forum": st.get("consumer_forum"),
            "income_limit_legal_aid": st["income_limit_legal_aid"],
        },
        "disclaimer": "This information is sourced from India Code and official government websites. It is legal information, not legal advice.",
        "sources": [
            "https://indiacode.nic.in",
            "https://ecourts.gov.in",
            "https://nalsa.gov.in",
        ],
        "last_updated": "2025-01-01",
    }

# ── COURT CASE LOOKUP ─────────────────────────────────────────────
@app.post("/api/court-lookup")
def court_lookup(req: CourtLookupRequest):
    """
    Looks up court case status.
    In production: integrates with eCourts API (https://ecourts.gov.in/ecourts_home/api/)
    Currently returns realistic mock data.
    """
    # Simulate API delay
    time.sleep(0.5)

    # Search mock cases
    for case in MOCK_COURT_CASES:
        if req.case_number.upper() in case["case_number"].upper():
            return {
                "found": True,
                "case": case,
                "source": "eCourts (mock)",
                "disclaimer": "Case data is for demonstration. For live data, visit ecourts.gov.in",
            }

    # Not found — return realistic not-found response
    return {
        "found": False,
        "message": f"Case {req.case_number} not found in our demo database.",
        "suggestion": "Visit https://ecourts.gov.in for live case status.",
        "source": "eCourts (mock)",
    }

# ── NGO SEARCH ────────────────────────────────────────────────────
@app.post("/api/ngos")
def find_ngos(req: NGOSearchRequest):
    """Finds relevant NGOs based on state and case type."""
    matches = []
    for ngo in NGOS:
        state_match = req.state in ngo["states"] or "all" in ngo["states"]
        type_match = req.case_type in ngo["focus"] or "all" in ngo["focus"]
        if state_match or type_match:
            matches.append(ngo)

    # Always include NALSA (national)
    matches.append({
        "name": "NALSA (National Legal Services Authority)",
        "focus": ["all"],
        "states": ["all"],
        "phone": "15100",
        "email": "nalsa@nic.in",
        "url": "https://nalsa.gov.in",
        "free": True,
        "note": "Free legal aid for income below ₹3 lakh/year",
    })

    return {
        "ngos": matches[:5],  # Top 5 results
        "total_found": len(matches),
        "state": req.state,
        "case_type": req.case_type,
    }

# ── DOCUMENT CHECKLIST ────────────────────────────────────────────
@app.get("/api/documents/{case_type}")
def get_documents(case_type: str):
    """Returns document checklist for a case type."""
    if case_type not in CASE_TYPES:
        raise HTTPException(404, f"Case type not found")
    return {
        "case_type": case_type,
        "documents": CASE_TYPES[case_type]["required_documents"],
        "tip": "Collect ALL documents before approaching any forum. Missing documents = delayed resolution.",
    }

# ── PLATFORM STATS ────────────────────────────────────────────────
@app.get("/api/stats")
def get_stats():
    """Returns platform statistics (for hero section data)."""
    # Add small random variation for realistic feel
    stats = PLATFORM_STATS.copy()
    stats["total_queries"] += random.randint(-50, 200)
    stats["active_users"] += random.randint(-10, 50)
    stats["timestamp"] = datetime.now().isoformat()
    return stats


# ══════════════════════════════════════════════════════════════════
#  MOCK RESPONSE GENERATOR (No API key needed)
# ══════════════════════════════════════════════════════════════════
def generate_mock_response(req: ChatRequest) -> str:
    """
    Generates structured, realistic mock responses when Claude API is unavailable.
    Detects intent from message and returns appropriate legal information.
    """
    msg = req.message.lower()
    lang = req.language

    # Detect case type from message
    if any(w in msg for w in ["deposit", "rent", "landlord", "tenant", "किराया", "வாடகை", "అద్దె", "ভাড়া"]):
        return get_rental_response(req.state, lang)

    elif any(w in msg for w in ["salary", "wage", "job", "employer", "labour", "वेतन", "ஊதியம்", "జీతం", "মজুরি"]):
        return get_labour_response(req.state, lang)

    elif any(w in msg for w in ["consumer", "product", "refund", "defect", "ecommerce", "उत्पाद", "பொருள்"]):
        return get_consumer_response(req.state, lang)

    elif any(w in msg for w in ["violence", "domestic", "husband", "wife", "घरेलू", "வன்முறை"]):
        return get_dv_response(lang)

    else:
        return MOCK_RESPONSES["welcome"].get(lang, MOCK_RESPONSES["welcome"]["en"])


def get_rental_response(state: Optional[str], lang: str) -> str:
    state_info = ""
    if state and state in STATES:
        s = STATES[state]
        state_info = f"\n\n**{s['name']} Specific Law:** {s['rent_act']}"

    responses = {
        "en": f"""🏠 **Rental Deposit — Your Rights**

Under the **Model Tenancy Act, 2021**, your landlord MUST:
• Return your deposit within **30 days** of you vacating
• Pay **15% annual interest** for every month of delay
• Not deduct for normal wear and tear{state_info}

**📁 Documents to collect immediately:**
• Rent agreement (original)
• Deposit payment proof (bank transfer / receipt)
• Move-out notice (with delivery proof)
• Photos of property condition

**🗺️ Your next step:**
Send a **registered post legal notice** to your landlord demanding return within 15 days. Keep the tracking receipt.

⚠️ *This is legal information, not legal advice. Consult a licensed advocate for binding counsel.*""",

        "hi": f"""🏠 **किराया जमा — आपके अधिकार**

**मॉडल टेनेंसी एक्ट, 2021** के अनुसार मकान मालिक को:
• घर खाली करने के **30 दिन** के अंदर जमा वापस करना होगा
• देरी पर **15% वार्षिक ब्याज** देना होगा{state_info}

**📁 तुरंत इकट्ठा करें:**
• किराया समझौता (मूल)
• जमा भुगतान का प्रमाण
• घर खाली करने की सूचना

**🗺️ अगला कदम:**
**रजिस्टर्ड डाक** से मकान मालिक को 15 दिन का नोटिस भेजें।

⚠️ *यह कानूनी जानकारी है, कानूनी सलाह नहीं। बाध्यकारी परामर्श के लिए वकील से मिलें।*""",
    }
    return responses.get(lang, responses["en"])


def get_labour_response(state: Optional[str], lang: str) -> str:
    return {
        "en": """👷 **Labour / Wage Dispute — Your Rights**

Under the **Payment of Wages Act, 1936** and **Code on Wages, 2019**:
• Wages must be paid by **7th of next month** (for companies with 1000+ employees)
• Employer cannot deduct wages without written reason
• Wrongful termination requires **30-day notice** or equivalent pay

**📁 Documents needed:**
• Offer letter / appointment letter
• Salary slips (last 3 months)
• Bank statements showing salary credits
• Termination letter (if applicable)

**🗺️ First step (FREE):**
File a complaint with your **District Labour Commissioner** — it's free and often resolves in 45 days without going to court.

⚠️ *This is legal information, not legal advice.*""",
        "hi": """👷 **श्रम / वेतन विवाद — आपके अधिकार**

**वेतन भुगतान अधिनियम, 1936** के अनुसार:
• वेतन अगले महीने की 7 तारीख तक देना अनिवार्य है
• बिना कारण वेतन काटना अवैध है

**🗺️ पहला कदम (मुफ्त):**
अपने **जिला श्रम आयुक्त** कार्यालय में शिकायत दर्ज करें — यह मुफ्त है।

⚠️ *यह कानूनी जानकारी है, कानूनी सलाह नहीं।*""",
    }.get(lang, get_labour_response(state, "en"))


def get_consumer_response(state: Optional[str], lang: str) -> str:
    return {
        "en": """🛒 **Consumer Complaint — Your Rights**

Under the **Consumer Protection Act, 2019**:
• You can file a complaint for defective products, poor service, or unfair trade practices
• Online filing available at **edaakhil.nic.in** (no need to visit office)
• Companies must respond to complaints within **30 days** by law

**Jurisdiction:**
• Up to ₹50 lakhs → District Consumer Commission
• ₹50 lakhs – ₹2 crores → State Commission
• Above ₹2 crores → National Commission (NCDRC)

**🗺️ File online today:**
Visit **edaakhil.nic.in** — India's consumer complaint portal

⚠️ *This is legal information, not legal advice.*""",
        "ta": """🛒 **நுகர்வோர் புகார் — உங்கள் உரிமைகள்**

**நுகர்வோர் பாதுகாப்பு சட்டம், 2019** படி:
• குறைபாடுள்ள பொருட்கள் / மோசமான சேவைக்கு புகார் தாக்கல் செய்யலாம்
• **edaakhil.nic.in** இல் ஆன்லைனில் தாக்கல் செய்யலாம்

⚠️ *இது சட்ட தகவல், சட்ட ஆலோசனை அல்ல.*""",
    }.get(lang, get_consumer_response(state, "en"))


def get_dv_response(lang: str) -> str:
    return {
        "en": """🛡️ **Domestic Violence — Immediate Help**

**Emergency numbers — call NOW if you are in danger:**
• **Police Emergency: 100**
• **Women's Helpline: 181** (24/7, free, confidential)
• **NCW Helpline: 7827170170**

Under the **Protection of Women from Domestic Violence Act, 2005**, you have the right to:
• A Protection Order (stops abuser from contacting you)
• A Residence Order (right to stay in shared home)
• Monetary Relief
• Custody of children

**Your first step:**
Contact your district's **Protection Officer** — this service is completely FREE.

⚠️ *This is legal information. If you are in immediate danger, please call 100 immediately.*""",
        "hi": """🛡️ **घरेलू हिंसा — तत्काल सहायता**

**अभी कॉल करें:**
• **पुलिस: 100**
• **महिला हेल्पलाइन: 181** (24/7, मुफ्त)
• **NCW: 7827170170**

**घरेलू हिंसा अधिनियम, 2005** के तहत आपको सुरक्षा आदेश, निवास अधिकार और आर्थिक राहत मिल सकती है।

⚠️ *खतरे में हों तो तुरंत 100 पर कॉल करें।*""",
    }.get(lang, get_dv_response("en"))


# ── RUN ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
