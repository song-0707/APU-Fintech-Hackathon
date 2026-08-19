"""Gemini intelligence extraction (Phase 3, Task 3.1/3.2/3.3). Ported from
MEETINGS/pipeline.py's _call_agnes_api(), run_gemini_analysis(), and
_fallback_analysis().

Deviation from the source: the original prompt asked Gemini to flag
contradictions against a hardcoded two-sentence "historical context" string.
That was a demo shortcut, not a real comparison against stored decisions —
real contradiction detection is app.graph.contradiction_service (Task 4.4,
embeddings + a dedicated Gemini judge call), so this module only extracts
decisions and action items, not flags.
"""
import json
import logging
import re
import time
import urllib.request
from typing import List, Optional

from app.core.config import get_settings
from app.schemas.meeting_intelligence import (
    ActionItem,
    Decision,
    DecisionConfidence,
    Flag,
    FlagType,
    MeetingAnalysis,
    MeetingIntelligence,
    TranscriptLine,
)
from app.services.gemini_client import generate_content, has_gemini_credentials

logger = logging.getLogger(__name__)
settings = get_settings()

# Imported directly by app.api.live_meeting — both gate action items
# extracted by the *same* Gemini prompt (this module's canonical post-call
# pass and that file's per-minute provisional pass), so a verb missing here
# silently drops a valid action item on one path but not the other.
_ACTION_VERBS = (
    "present", "prepare", "send", "submit", "review", "draft", "share",
    "update", "complete", "create", "schedule", "follow up", "call",
    "email", "deliver", "finalize", "confirm", "check", "fix", "build",
    "assign", "analyze", "analyse", "brief",
    "investigate", "research", "document", "write", "design", "implement",
    "deploy", "test", "validate", "verify", "organize", "coordinate",
    "arrange", "contact", "notify", "escalate", "gather", "collect",
    "compile", "summarize", "outline", "publish", "launch", "release",
    "roll out", "onboard", "train", "monitor", "track", "audit",
    "negotiate", "sign", "handle", "resolve", "set up", "configure",
    "migrate", "upgrade", "install", "reach out", "circle back",
    "clean up", "refactor", "debug", "upload", "file", "order", "plan",
    "host", "kick off", "wrap up", "reschedule", "reply", "respond",
    "clarify", "consolidate",
)
_AGENDA_PHRASES = (
    "going to discuss",
    "going to talk about",
    "discuss about the task",
    "discuss the task",
    "today we are going to discuss",
)
_STOP_ENTITY_TOKENS = {
    "a", "an", "and", "are", "for", "in", "is", "next", "of", "on", "the",
    "to", "we", "week", "with",
}


def _has_action_verb(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(verb)}\b", lowered) for verb in _ACTION_VERBS)


def _is_agenda_statement(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _AGENDA_PHRASES)


def _clean_action_task(task: str) -> str:
    cleaned = task.strip()
    cleaned = re.sub(r"^\s*action item\s*:\s*", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^\s*(?:i am|i'm)\s+going\s+to\s+", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^\s*(?:i will|i'll|we will|we'll)\s+", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\bthe project of ([A-Za-z][A-Za-z\s-]*?) to\b", r"\1 project to", cleaned, flags=re.IGNORECASE)
    # "" (not the raw input) once boilerplate consumes everything — safe because
    # none of the stripped prefixes overlap an _ACTION_VERBS word, so every
    # caller's _has_action_verb gate already rejects whatever would land here.
    return (cleaned[:1].upper() + cleaned[1:]) if cleaned else ""


def _entity_tokens(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 1 and token not in _STOP_ENTITY_TOKENS
    ]


def _entity_supported_by_transcript(entity: str, transcript_text: str, detected_names: Optional[List[str]]) -> bool:
    entity = entity.strip()
    if not entity:
        return False
    transcript_lower = transcript_text.lower()
    detected_lower = {name.lower() for name in (detected_names or [])}
    if entity.lower() in transcript_lower or entity.lower() in detected_lower:
        return True

    transcript_tokens = set(_entity_tokens(transcript_text))
    tokens = _entity_tokens(entity)
    return bool(tokens and all(token in transcript_tokens for token in tokens))


def _speaker_names_from_transcript(transcript_text: str) -> list[str]:
    speakers: list[str] = []
    for line in transcript_text.split("\n"):
        match = re.match(r"\[[^\]]+\]\s*([^:]+):", line.strip())
        if not match:
            continue
        speaker = match.group(1).strip()
        if speaker and speaker not in speakers:
            speakers.append(speaker)
    real_names = [name for name in speakers if name.lower() != "speaker"]
    return real_names or speakers


def postprocess_analysis_dict(
    analysis: dict,
    transcript_text: str,
    detected_names: Optional[List[str]] = None,
) -> dict:
    """Drop weak or unsupported extractions before they become graph nodes."""
    cleaned = dict(analysis)

    action_items = []
    for item in cleaned.get("action_items") or []:
        if not isinstance(item, dict):
            continue
        task = _clean_action_task(str(item.get("task") or ""))
        if not task or _is_agenda_statement(task) or not _has_action_verb(task):
            continue
        next_item = dict(item)
        next_item["task"] = task
        action_items.append(next_item)
    cleaned["action_items"] = action_items[:5]

    triples = []
    for triple in cleaned.get("knowledge_triples") or []:
        if not isinstance(triple, dict):
            continue
        subject = str(triple.get("subject") or "")
        obj = str(triple.get("object") or "")
        if (
            _entity_supported_by_transcript(subject, transcript_text, detected_names)
            and _entity_supported_by_transcript(obj, transcript_text, detected_names)
        ):
            triples.append(triple)
    cleaned["knowledge_triples"] = triples[:12]

    return cleaned


def parse_analysis_response(raw: str) -> MeetingAnalysis:
    """Repair common JSON formatting mistakes, then validate the full schema."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        repaired = re.sub(r",\s*([}\]])", r"\1", cleaned)
        data = json.loads(repaired)
    return MeetingAnalysis.model_validate(data)


def call_agnes_api(messages: list, model: str = "agnes-2.0-flash") -> str:
    """OpenAI-compatible chat completion call to Agnes AI, with 429 backoff.
    Shared by gemini_service (text fallback) and vision_service (vision
    fallback)."""
    url = f"{settings.agnes_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.agnes_api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": messages}
    data_bytes = json.dumps(payload).encode("utf-8")

    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=data_bytes, headers=headers)
            with urllib.request.urlopen(req, timeout=35) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                return res["choices"][0]["message"]["content"]
        except Exception as e:
            err_str = str(e)
            if ("429" in err_str or "rate" in err_str.lower()) and attempt < 3:
                logger.warning(f"Agnes AI rate limited (429). Retrying in 2.5s (attempt {attempt + 1}/4)...")
                time.sleep(2.5)
                continue
            raise


def run_gemini_analysis(transcript_text: str, detected_names: Optional[List[str]] = None) -> dict:
    """Send transcript to Gemini (primary) / Agnes AI (fallback) for speaker
    name mapping + decision/action-item extraction. Returns a dict shaped
    like MeetingAnalysis (participants/speaker_map/decisions/action_items)."""
    names_str = ", ".join(detected_names) if detected_names else "None detected from video frames"

    prompt = f"""
You are the AI engine for 'Corporate Brain', an organizational intelligence platform.

**PART 1 — INFER & MAP SPEAKER NAMES**
The transcript currently has speaker IDs like SPEAKER_01, SPEAKER_02, etc.
The following participant names were DETECTED from video nameplates/tiles by Vision AI:
[{names_str}]

Analyze the conversation carefully to determine each speaker's real identity.
Assign each speaker ID to one of the detected participant names above (or infer their real name if not in the list).
Do NOT output generic labels like "Unknown Speaker X". Use the actual names detected above!

**PART 2 — EXTRACT INTELLIGENCE**
From the transcript, extract a concise summary, decisions, action items,
risks, and factual knowledge triples. Decision confidence must be one of
"firm_commitment" | "soft_agreement" | "unresolved".

Action-item rules:
- Only create an action item when a speaker clearly commits to concrete future work.
- Do not create action items from agenda/topic statements like
  "today we are going to discuss the task next week".
- Normalize first-person commitments into task text. For example,
  "I am going to present the project of customer feedback to CEO next week"
  should become "Present customer feedback project to CEO next week".
- Do not include filler, duplicated words, or partial ASR fragments in task text.

For each knowledge triple's subject and object, classify it with a
subject_type/object_type from this FIXED list — do not invent any other
category:
  "Person"        — a named individual
  "Project"       — a project or workstream
  "Organization"  — a company, vendor, department, or team
  "System"        — software, a platform, or a tool
  "Policy"        — a rule, guideline, or compliance requirement
  "Document"      — a contract, report, or file
  "Concept"       — anything else: an idea, risk, topic, or activity
If you are unsure which category fits, use "Concept" rather than guessing.
Only emit knowledge triples for entities explicitly supported by transcript
words or detected participant names. Do not invent model/system names.

**Meeting Transcript:**
{transcript_text}

**Return ONLY valid JSON** with this exact structure:
{{
  "summary": "Concise meeting summary",
  "participants": ["Real Name 1", "Real Name 2"],
  "speaker_map": {{"SPEAKER_01": "Real Name 1", "SPEAKER_02": "Real Name 2"}},
  "decisions": [
    {{
      "title": "Decision title",
      "reason": "Why this decision was made",
      "evidence": "Transcript evidence supporting the decision",
      "confidence": "firm_commitment",
      "timestamp": "00:00:14",
      "speaker": "Real Name 1"
    }}
  ],
  "action_items": [
    {{
      "task": "Task description",
      "assignee": "Real Name 2",
      "deadline": "2026-08-20",
      "priority": "high"
    }}
  ],
  "risks": ["A concrete risk raised in the meeting"],
  "knowledge_triples": [
    {{"subject": "Project Alpha", "subject_type": "Project", "predicate": "USES_VENDOR",
      "object": "Provider X", "object_type": "Organization"}}
  ]
}}
"""

    try:
        raw = ""
        if has_gemini_credentials():
            logger.info("Running Gemini analysis with %s...", settings.gemini_model)
            for attempt in range(2):
                try:
                    response = generate_content(
                        model=settings.gemini_model,
                        contents=prompt,
                        config={"response_mime_type": "application/json"},
                    )
                    analysis = parse_analysis_response(response.text).model_dump()
                    return postprocess_analysis_dict(analysis, transcript_text, detected_names)
                except Exception as gemini_ex:
                    logger.warning(
                        "Gemini analysis attempt %s/2 failed validation or request: %s",
                        attempt + 1,
                        gemini_ex,
                    )

        if not raw and settings.agnes_api_key:
            logger.info("Running Agnes AI Flash analysis...")
            messages = [{"role": "user", "content": prompt}]
            raw = call_agnes_api(messages, model="agnes-2.0-flash")

        if raw:
            analysis = parse_analysis_response(raw).model_dump()
            return postprocess_analysis_dict(analysis, transcript_text, detected_names)
        raise ValueError("No meeting-intelligence API is configured")
    except Exception as e:
        logger.warning(f"AI analysis failed: {e}. Using fallback extraction.")
        return fallback_analysis(transcript_text, detected_names)


def fallback_analysis(transcript_text: str, detected_names: Optional[List[str]] = None) -> dict:
    """Keyword-heuristic extraction when Gemini/Agnes both fail (rate limits,
    quota, network)."""
    participants = list(dict.fromkeys(detected_names or [])) or _speaker_names_from_transcript(transcript_text) or ["Speaker"]
    decisions = []
    action_items = []

    for line in transcript_text.split("\n"):
        if not line.strip():
            continue
        ts, spk, txt = "00:00:00", "Speaker", line
        if line.startswith("[") and "]" in line:
            parts = line.split("]", 1)
            ts = parts[0].replace("[", "").strip()
            rest = parts[1].strip()
            if ":" in rest:
                spk_parts = rest.split(":", 1)
                spk = spk_parts[0].strip()
                txt = spk_parts[1].strip()

        l_lower = txt.lower()
        if any(w in l_lower for w in ["decide", "agree", "confirm", "approve", "settle", "must", "wise", "compulsory"]):
            decisions.append({"text": txt, "confidence": "firm_commitment", "timestamp": ts, "speaker": spk})
        elif (
            not _is_agenda_statement(txt)
            and re.search(r"\b(action item|i will|i'll|i am going to|i'm going to|we will|we'll|need to|needs to|please)\b", l_lower)
            and _has_action_verb(txt)
        ):
            action_items.append({"task": _clean_action_task(txt), "assignee": spk, "deadline": "End of week", "priority": "high"})

    return postprocess_analysis_dict({
        "summary": "Meeting transcript processed with deterministic fallback extraction.",
        "participants": participants,
        "speaker_map": {},
        "decisions": decisions[:5],
        "action_items": action_items[:5],
        "risks": [],
        "knowledge_triples": [],
    }, transcript_text, detected_names)


# ── Demo mode — canned scenario, zero API calls ────────────────────────────
# Mirrors MEETINGS/pipeline.py's _demo_pipeline(). Used when settings.demo_mode
# is true so the full pipeline can be verified/demoed with no API keys and no
# network calls. The contradiction flag here is pre-baked (not produced by
# contradiction_service) — meeting_tasks.py wires it to a seeded prior
# decision via graph_builder.seed_demo_history() so a real CONTRADICTS edge
# still appears in Neo4j.
DEMO_SEED_MEETING_ID = "demo-seed-meeting"
DEMO_SEED_DECISION_TEXT = "Freeze all new vendor onboarding until Q4."


def demo_meeting_intelligence(meeting_id: str) -> MeetingIntelligence:
    transcript = [
        TranscriptLine(timestamp="00:00:08", speaker="Sarah Park", speaker_raw="SPEAKER_01", text="Good morning everyone. Let's start with the vendor evaluation."),
        TranscriptLine(timestamp="00:01:22", speaker="Tom Wright", speaker_raw="SPEAKER_02", text="I've reviewed all three providers. Provider X leads on cost and SLA."),
        TranscriptLine(timestamp="00:03:10", speaker="Alex Chen", speaker_raw="SPEAKER_03", text="What's the cost saving over the full contract period?"),
        TranscriptLine(timestamp="00:03:45", speaker="Tom Wright", speaker_raw="SPEAKER_02", text="22% over 36 months — roughly $340,000 total savings."),
        TranscriptLine(timestamp="00:05:20", speaker="Diana Ross", speaker_raw="SPEAKER_04", text="Are there integration risks for Project Alpha's Q4 deadline?"),
        TranscriptLine(timestamp="00:06:30", speaker="Tom Wright", speaker_raw="SPEAKER_02", text="6-week migration window. A security audit is the main precondition."),
        TranscriptLine(timestamp="00:08:15", speaker="Diana Ross", speaker_raw="SPEAKER_04", text="Did we formally lift the vendor freeze from the May all-hands?"),
        TranscriptLine(timestamp="00:09:40", speaker="Sarah Park", speaker_raw="SPEAKER_01", text="That freeze was a guideline. I'm comfortable proceeding. Decision made."),
        TranscriptLine(timestamp="00:11:05", speaker="Sarah Park", speaker_raw="SPEAKER_01", text="Official decision: switch to Provider X, effective Q4 2026. Tom, own the audit."),
        TranscriptLine(timestamp="00:11:30", speaker="Tom Wright", speaker_raw="SPEAKER_02", text="Confirmed. I'll target audit completion by August 20th."),
        TranscriptLine(timestamp="00:13:45", speaker="Alex Chen", speaker_raw="SPEAKER_03", text="The transition adds ~15% to Project Alpha's budget. I need that approved."),
        TranscriptLine(timestamp="00:14:55", speaker="Sarah Park", speaker_raw="SPEAKER_01", text="Approved in principle. Bring formal numbers to finance Thursday."),
    ]
    decisions = [
        Decision(text="Switch primary vendor to Provider X, effective Q4 2026", confidence=DecisionConfidence.firm_commitment, timestamp="00:11:05", speaker="Sarah Park"),
        Decision(text="Increase Project Alpha budget by 15% for vendor transition", confidence=DecisionConfidence.soft_agreement, timestamp="00:13:45", speaker="Alex Chen"),
        Decision(text="Security audit of Provider X is mandatory before contract signing", confidence=DecisionConfidence.firm_commitment, timestamp="00:06:30", speaker="Tom Wright"),
    ]
    action_items = [
        ActionItem(task="Complete security audit of Provider X", assignee="Tom Wright", deadline="2026-08-20", priority="high"),
        ActionItem(task="Submit 15% budget increase to finance committee", assignee="Alex Chen", deadline="2026-08-15", priority="high"),
        ActionItem(task="Begin vendor procurement negotiations with Provider X", assignee="Sarah Park", deadline="2026-09-01", priority="high"),
        ActionItem(task="Set up bi-weekly transition steering committee", assignee="Diana Ross", deadline=None, priority="medium"),
    ]
    flags = [
        Flag(
            type=FlagType.contradiction,
            message="Provider X decision may conflict with the vendor freeze agreed at the May 3rd All-Hands meeting.",
            severity="warning",
            source_decision_text="Switch primary vendor to Provider X, effective Q4 2026",
            contradicts_meeting_id=DEMO_SEED_MEETING_ID,
            contradicts_decision_text=DEMO_SEED_DECISION_TEXT,
        ),
    ]

    return MeetingIntelligence(
        meeting_id=meeting_id,
        duration="00:15:20",
        summary="The team selected Provider X for Q4, subject to a security audit and finance approval for the transition budget.",
        participants=["Sarah Park", "Tom Wright", "Alex Chen", "Diana Ross"],
        speaker_map={
            "SPEAKER_01": "Sarah Park",
            "SPEAKER_02": "Tom Wright",
            "SPEAKER_03": "Alex Chen",
            "SPEAKER_04": "Diana Ross",
        },
        transcript=transcript,
        decisions=decisions,
        action_items=action_items,
        flags=flags,
        risks=[
            "Provider X must pass a security audit before contract signing.",
            "The transition may increase Project Alpha's budget by 15%.",
        ],
        knowledge_triples=[
            {
                "subject": "Project Alpha", "subject_type": "Project",
                "predicate": "USES_VENDOR",
                "object": "Provider X", "object_type": "Organization",
            },
            {
                "subject": "Tom Wright", "subject_type": "Person",
                "predicate": "OWNS",
                "object": "Provider X security audit", "object_type": "Concept",
            },
        ],
    )
