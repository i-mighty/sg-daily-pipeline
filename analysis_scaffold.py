"""
Platform-level analysis scaffold.

This is the SYSTEM prompt the platform injects into every prospect analysis,
independent of the campaign's user-editable analysis_prompt. It governs how
emails are resolved and written, and the email-related JSON output fields.

Keep this focused on OUTPUT POLICY (emails, formatting). Targeting, scoring,
and research instructions belong in the campaign's analysis_prompt, which the
user controls.

Campaign prompts are split into four labelled sections so each analysis stage
(and each analysis path — live agentic vs pre-fetched batch) can use just the
part it needs. See split_sections() below.
"""

import re

# ── Section convention ──────────────────────────────────────────────────────────
#
# Campaign analysis_prompt files are divided into four sections, each introduced
# by a delimiter line:  === SECTION NAME ===
#
#   === RESEARCH ===   how to gather company intel (live path drives tools here)
#   === CONTACT ===    how to find the decision maker + their real email
#   === SCORING ===    the ICP definition and scoring rubric
#   === OUTPUT ===     the JSON output schema
#
# Each maps to a stage in the analysis pipeline. Missing sections come back as "".

SECTION_NAMES = ["RESEARCH", "CONTACT", "SCORING", "OUTPUT"]

_SECTION_RE = re.compile(r"^[ \t]*===[ \t]*([A-Z][A-Z &]*?)[ \t]*===[ \t]*$", re.MULTILINE)


def split_sections(prompt: str) -> dict[str, str]:
    """
    Parse a sectioned campaign prompt into its labelled parts.

    Returns {preamble, research, contact, scoring, output}. The "preamble" is any
    text before the first === marker (shared product/lens context that every stage
    needs). Keys are always present; absent sections are "".

    A prompt with no === markers returns empty sections — callers should check
    has_sections() to decide on the legacy whole-prompt fallback.
    """
    result = {"preamble": ""}
    result.update({name.lower(): "" for name in SECTION_NAMES})
    matches = list(_SECTION_RE.finditer(prompt))
    if not matches:
        return result

    result["preamble"] = prompt[: matches[0].start()].strip()

    for i, m in enumerate(matches):
        name = m.group(1).strip().upper()
        # Normalise "TARGETING & SCORING" -> "SCORING" etc. by matching known names
        key = next((n for n in SECTION_NAMES if n in name), None)
        if key is None:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(prompt)
        result[key.lower()] = prompt[start:end].strip()
    return result


def has_sections(prompt: str) -> bool:
    """True if the prompt uses the === SECTION === convention."""
    return bool(_SECTION_RE.search(prompt))


def compose_prompt(template: str, research_override: str | None = None) -> str:
    """
    Rebuild a runnable prompt from a sectioned template, markers stripped.

    research_override:
      None -> keep the RESEARCH section as written (live agentic path, has tools)
      str  -> replace the RESEARCH section with this text, e.g. pre-fetched data
              for the no-tools batch path

    Legacy prompts without === markers are returned unchanged.
    """
    if not has_sections(template):
        return template
    s = split_sections(template)
    research = s["research"] if research_override is None else research_override
    parts = [s["preamble"], research, s["contact"], s["scoring"], s["output"]]
    return "\n\n".join(p for p in parts if p.strip())

EMAIL_SYSTEM_PROMPT = """You are the output-quality layer for a B2B sales intelligence platform.
The user message contains the campaign's analysis instructions (targeting + scoring).
You must follow those instructions, and you must ALSO obey the rules below, which
override any conflicting instruction in the user message.

## EMAIL ADDRESS RESOLUTION (actual first, pattern only as fallback)

1. ACTUAL FIRST: Look hard for the decision maker's real, verifiable email address
   in the research available to you — team/contact/about pages, press releases,
   public profiles, directory listings, signatures. If you find a real address, use it.
2. PATTERN FALLBACK: Only if no actual address can be found, derive the most likely
   address by applying the company's confirmed email pattern to the person's real
   full name. Example: pattern "first.last@acme.com" + "Jane Doe" -> "jane.doe@acme.com".
   Never mangle the name (no "ja.ne@..."). Never output a raw pattern as the address.
3. In the JSON output, key_decision_maker MUST include:
     "email":        the actual or derived address (a concrete address, never a pattern)
     "email_source": "found" if taken from a real source, "derived" if built from a pattern
   And outreach_email.to_email MUST equal that same address.

## EMAIL WRITING RULES (apply to outreach_email.body)

- 60-110 words. Detailed enough to feel researched, short enough to read in 15 seconds.
- Start with the person's first name.
- Reference ONE specific, real detail about their company from the research
  (a product, funding round, hire, expansion, customer, or market move). Generic praise is banned.
- Give ONE clear reason you are reaching out now.
- End with one specific, low-friction ask (a 15-minute call, or a question answerable in one line).
- NO em dashes. Use commas, periods, or rewrite the sentence.
- BANNED phrases: "I hope this email/message finds you well", "we help companies like yours",
  "I wanted to reach out", "I came across", "synergies", "revolutionise/transform your X".
- NO placeholders. Never output [Your Company], [Your Name], [Your Position]. Sign off as "Best,"
  with no bracketed tokens.
- Plain, direct, human. Vary sentence length. Sound like one founder emailing another.

Produce every field the campaign's JSON schema asks for, plus the email/email_source fields above.
"""
