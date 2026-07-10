"""
Deterministic email verification + confidence scoring for outreach.

The LLM contact stage PROPOSES candidate emails; this module DECIDES what is
actually sendable. Checks run cheapest-first:

  1. syntax + pattern-literal detection      (pure — catches "first.last@acme.com")
  2. role-inbox classification               (pure — partnerships@ ok, noreply@ never)
  3. junk / disposable / free-provider flags (pure)
  4. MX lookup                               (DNS — works on Railway)
  5. optional HTTPS verifier API             (MillionVerifier / ZeroBounce, if a key is set)

SMTP handshake verification is intentionally NOT attempted: Railway blocks
outbound SMTP, and HTTPS verifier APIs are more reliable anyway.

Confidence scoring follows the outreach playbook: an address is only sent when
BOTH deliverability and the evidence behind it are strong. Signals:

  +40 published on the company's official site      -15 catch-all domain
  +30 found in an off-site public source w/ URL     -15 no source URL
  +25 verifier API says deliverable                 -15/-20 domain mismatch
  +20 derived from a pattern confirmed by evidence  reject: invalid / bad role /
  +15 domain has MX records                                 pattern literal
  +20 named decision maker (+10 net for role inbox)

Send threshold: EMAIL_MIN_CONFIDENCE env var, default 60.
"""

import os
import re
import socket
import urllib.parse

import requests

# ── Classification data ─────────────────────────────────────────────────────────

RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_SYNTAX_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")

# Local parts that are an uninstantiated pattern, not a person: "first.last@",
# "flast@", "{first}@", "firstname.lastname@" — the exact bug behind bounced sends.
_PATTERN_WORDS = {
    "first", "last", "firstname", "lastname", "fname", "lname", "flast", "firstl",
    "f", "l", "initial", "firstinitial", "name", "fullname", "firstname.lastname",
    "first.last", "first_last", "first-last", "f.last", "flastname",
}

# Role inboxes that are legitimate outreach fallbacks (relevant team inboxes).
GOOD_ROLE = {
    "partnerships", "partnership", "partner", "sales", "hello", "hi", "marketing",
    "business", "biz", "bizdev", "bd", "press", "media", "growth", "founders",
    "team", "info", "contact", "enquiries", "inquiries", "enquiry", "collab",
    "collabs", "reservations", "events",
}

# Role inboxes that must never be primary outreach.
BAD_ROLE = {
    "support", "help", "helpdesk", "noreply", "no-reply", "no_reply", "donotreply",
    "do-not-reply", "privacy", "legal", "abuse", "billing", "accounts", "admin",
    "webmaster", "postmaster", "hostmaster", "security", "unsubscribe", "dpo",
    "careers", "jobs", "hr", "recruiting", "recruitment", "compliance",
}

FREE_PROVIDERS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.com.sg", "hotmail.com",
    "outlook.com", "live.com", "icloud.com", "me.com", "aol.com", "proton.me",
    "protonmail.com", "qq.com", "163.com",
}

DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "yopmail.com",
    "tempmail.com", "temp-mail.org", "getnada.com", "dispostable.com",
    "sharklasers.com", "trashmail.com",
}

# Domains that show up in page templates / tracking snippets, never real contacts.
JUNK_DOMAINS = {
    "example.com", "example.org", "email.com", "domain.com", "yourcompany.com",
    "yourdomain.com", "company.com", "acme.com", "sentry.io", "wixpress.com",
    "sentry.wixpress.com", "godaddy.com", "mysite.com", "test.com", "site.com",
}

_ASSET_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js",
               ".woff", ".woff2", ".ico", ".mp4")


# ── Small helpers ───────────────────────────────────────────────────────────────

def company_domain(url: str) -> str:
    """Registrable-ish domain for a company URL: 'https://www.acme.com/x' -> 'acme.com'."""
    if not url:
        return ""
    netloc = urllib.parse.urlparse(url if "://" in url else "https://" + url).netloc
    return netloc.split(":")[0].removeprefix("www.").lower()


def _sld(domain: str) -> str:
    """Second-level label used for loose brand matching: 'mail.acme.co.uk' -> 'acme'-ish."""
    parts = [p for p in domain.split(".") if p]
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")


def _domains_related(email_dom: str, comp_dom: str) -> bool:
    if not email_dom or not comp_dom:
        return False
    if email_dom == comp_dom:
        return True
    if email_dom.endswith("." + comp_dom) or comp_dom.endswith("." + email_dom):
        return True
    return _sld(email_dom) == _sld(comp_dom)  # acme.com vs acme.sg


def looks_like_pattern(local: str) -> bool:
    """True when the local part is a pattern template rather than a real mailbox."""
    l = local.lower()
    if any(ch in l for ch in "{}[]<>%"):
        return True
    if l in _PATTERN_WORDS:
        return True
    # composite forms: every dot/underscore/hyphen-separated token is a pattern word
    tokens = [t for t in re.split(r"[._-]", l) if t]
    return bool(tokens) and all(t in _PATTERN_WORDS for t in tokens) and len(tokens) > 1


def is_junk_email(email: str) -> bool:
    """Filter template/tracking noise harvested from raw HTML."""
    email = email.lower()
    if email.endswith(_ASSET_EXTS):
        return True
    local, _, dom = email.partition("@")
    if dom in JUNK_DOMAINS or dom in DISPOSABLE_DOMAINS:
        return True
    if local in {"user", "example", "test", "email", "someone", "your", "you", "me"}:
        return True
    # Composite never-outreach locals ("anthropicprivacy@", "acme-noreply@"): these
    # words are long enough that a substring match cannot hit a person's name.
    if any(w in local for w in ("privacy", "noreply", "no-reply", "donotreply",
                                "unsubscribe", "postmaster", "webmaster")):
        return True
    return looks_like_pattern(local)


# ── MX lookup (cached) ──────────────────────────────────────────────────────────

_MX_CACHE: dict[str, bool | None] = {}


def has_mx(domain: str) -> bool | None:
    """True: MX (or implicit-MX A record) exists. False: domain cannot receive mail.
    None: could not determine (resolver unavailable / timeout) — don't punish."""
    domain = domain.lower()
    if domain in _MX_CACHE:
        return _MX_CACHE[domain]
    result: bool | None
    try:
        import dns.resolver
        try:
            result = len(dns.resolver.resolve(domain, "MX", lifetime=6)) > 0
        except dns.resolver.NXDOMAIN:
            result = False
        except dns.resolver.NoAnswer:
            # RFC 5321 implicit MX: fall back to an A/AAAA record.
            try:
                dns.resolver.resolve(domain, "A", lifetime=6)
                result = True
            except Exception:
                result = False
        except Exception:
            result = None
    except ImportError:
        # dnspython missing: an A record at least proves the domain exists.
        try:
            socket.getaddrinfo(domain, None)
            result = None  # exists, but MX unconfirmed
        except socket.gaierror:
            result = False
        except Exception:
            result = None
    _MX_CACHE[domain] = result
    return result


# ── Optional HTTPS verifier APIs ────────────────────────────────────────────────

_API_CACHE: dict[str, str | None] = {}


def api_verify(email: str) -> str | None:
    """Verify via MillionVerifier or ZeroBounce when a key is configured.
    Returns 'valid' | 'invalid' | 'catch_all' | 'disposable' | 'unknown',
    or None when no verifier is configured / the call failed."""
    if email in _API_CACHE:
        return _API_CACHE[email]
    result: str | None = None

    mv_key = os.environ.get("MILLIONVERIFIER_API_KEY")
    zb_key = os.environ.get("ZEROBOUNCE_API_KEY")
    try:
        if mv_key:
            r = requests.get("https://api.millionverifier.com/api/v3/",
                             params={"api": mv_key, "email": email, "timeout": 10}, timeout=15)
            res = (r.json().get("result") or "").lower()
            result = {"ok": "valid", "catch_all": "catch_all", "invalid": "invalid",
                      "disposable": "disposable"}.get(res, "unknown")
        elif zb_key:
            r = requests.get("https://api.zerobounce.net/v2/validate",
                             params={"api_key": zb_key, "email": email}, timeout=15)
            res = (r.json().get("status") or "").lower()
            result = {"valid": "valid", "catch-all": "catch_all", "invalid": "invalid",
                      "spamtrap": "invalid", "abuse": "invalid",
                      "do_not_mail": "invalid"}.get(res, "unknown")
    except Exception:
        result = None

    _API_CACHE[email] = result
    return result


# ── Core verification ───────────────────────────────────────────────────────────

def verify_email(email: str, comp_dom: str = "") -> dict:
    """Classify one address. status: valid | catch_all | risky | unknown | invalid.
      valid     verifier API confirmed deliverable
      catch_all domain accepts everything — inbox unprovable, rely on evidence
      risky     MX exists but inbox unproven (no verifier configured)
      unknown   could not check deliverability at all
      invalid   do not send (with 'reason')"""
    email = (email or "").strip().strip(".;,<>()[]").lower()
    v = {"email": email, "status": "unknown", "reason": "", "is_role": False,
         "role_ok": None, "domain_match": False, "free_provider": False,
         "mx": None, "api": None}

    if not email or not _SYNTAX_RE.match(email):
        v.update(status="invalid", reason="bad syntax")
        return v
    local, dom = email.rsplit("@", 1)

    if looks_like_pattern(local):
        v.update(status="invalid", reason="pattern template, not a real address")
        return v
    if dom in DISPOSABLE_DOMAINS or dom in JUNK_DOMAINS:
        v.update(status="invalid", reason=f"junk/disposable domain {dom}")
        return v
    if local in BAD_ROLE:
        v.update(is_role=True, role_ok=False, status="invalid",
                 reason=f"unsuitable role inbox {local}@")
        return v
    if local in GOOD_ROLE:
        v.update(is_role=True, role_ok=True)

    v["free_provider"] = dom in FREE_PROVIDERS
    v["domain_match"]  = _domains_related(dom, comp_dom)

    v["mx"] = has_mx(dom)
    if v["mx"] is False:
        v.update(status="invalid", reason=f"domain {dom} has no mail server")
        return v

    v["api"] = api_verify(email)
    if v["api"] in ("invalid", "disposable"):
        v.update(status="invalid", reason=f"verifier: {v['api']}")
    elif v["api"] == "valid":
        v["status"] = "valid"
    elif v["api"] == "catch_all":
        v["status"] = "catch_all"
    else:
        v["status"] = "risky" if v["mx"] else "unknown"
    return v


def score_candidate(v: dict, source: str, source_url: str, comp_dom: str,
                    pattern_evidence: bool = False) -> int:
    """Confidence 0-100 for one verified candidate. 0 means reject."""
    if v["status"] == "invalid":
        return 0
    pts = 0

    src_host = urllib.parse.urlparse(source_url).netloc.lower() if source_url else ""
    on_site  = bool(comp_dom) and comp_dom in src_host

    if source == "site":                       # deterministically scraped from their site
        pts += 40
    elif source in ("found", "generic"):       # model claims a real source
        pts += 40 if on_site else (30 if source_url else 15)
    elif source == "derived":                  # pattern inference
        pts += 20 if pattern_evidence else 5

    if v["api"] == "valid":
        pts += 25
    elif v["mx"]:
        pts += 15

    # Role relevance: a named decision maker picked for this campaign (+20), or a
    # relevant team inbox (+20 relevance, -10 for being generic).
    pts += 10 if v["is_role"] else 20

    if v["status"] == "catch_all":
        pts -= 15
    if source in ("found", "derived") and not source_url:
        pts -= 15
    if comp_dom and not v["domain_match"]:
        pts -= 15 if v["free_provider"] else 20

    return max(0, min(100, pts))


# ── Pipeline hooks ──────────────────────────────────────────────────────────────

def send_threshold() -> int:
    try:
        return int(os.environ.get("EMAIL_MIN_CONFIDENCE", "60"))
    except ValueError:
        return 60


def finalize_contact_email(contact: dict, dossier: dict, lead_url: str,
                           site_emails: list[dict] | None = None) -> dict:
    """Post-contact-stage resolver: verify every candidate the stage surfaced plus
    the emails scraped from the official site, score them, and settle on the best.

    Mutating keys set on the returned copy:
      email, email_source, email_source_url  — the winning candidate ("" if none)
      email_status                            — verify_email status / "no_valid_email"
      email_confidence                        — 0-100 int
      email_candidates_checked                — audit trail of everything considered
    """
    contact  = dict(contact or {})
    comp_dom = company_domain(lead_url)
    pattern_evidence = bool((dossier or {}).get("email_pattern", "").strip())

    cands: list[dict] = []

    def add(email, source, source_url=""):
        email = (email or "").strip().lower()
        if email and "@" in email and all(email != c["email"] for c in cands):
            cands.append({"email": email, "source": source, "source_url": source_url or ""})

    add(contact.get("email"), contact.get("email_source") or "found",
        contact.get("email_source_url", ""))
    add(contact.get("fallback_generic_email"), "generic")
    for se in site_emails or []:
        add(se.get("email"), "site", se.get("source_url", ""))

    best = None  # (candidate, score, verification)
    checked = []
    for c in cands:
        v = verify_email(c["email"], comp_dom)
        pts = score_candidate(v, c["source"], c["source_url"], comp_dom, pattern_evidence)
        checked.append({"email": c["email"], "source": c["source"],
                        "status": v["status"], "score": pts,
                        **({"reason": v["reason"]} if v["reason"] else {})})
        if v["status"] != "invalid" and (best is None or pts > best[1]):
            best = (c, pts, v)

    if best:
        c, pts, v = best
        contact["email"]            = c["email"]
        contact["email_source"]     = "generic" if c["source"] == "site" else c["source"]
        contact["email_source_url"] = c["source_url"]
        contact["email_status"]     = v["status"]
        contact["email_confidence"] = pts
    else:
        contact["email"]            = ""
        contact["email_status"]     = "no_valid_email"
        contact["email_confidence"] = 0
    contact["email_candidates_checked"] = checked
    return contact


def normalize_confidence(value) -> int | None:
    """Accept the finalizer's int, or the model's high/medium/low, else None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    return {"high": 80, "medium": 60, "low": 35}.get(str(value).strip().lower())


def check_sendable(email: str, company_url: str = "", confidence=None) -> tuple[bool, str]:
    """Send-time gate. Returns (ok, reason). Blocks undeliverable/pattern/bad-role
    addresses outright, and anything below the confidence threshold."""
    v = verify_email(email, company_domain(company_url))
    if v["status"] == "invalid":
        return False, v["reason"]
    thr  = send_threshold()
    conf = normalize_confidence(confidence)
    if conf is not None and conf < thr:
        return False, f"confidence {conf} below threshold {thr}"
    return True, v["status"]
