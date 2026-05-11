#!/usr/bin/env python3
"""
Generate a PDF prospect report from markdown + structured JSON data.
Uses reportlab. Called automatically by run_batch.py, or standalone:

    python scripts/generate_pdf.py results/m-kopa/PROSPECT-ANALYSIS.md \
                                   results/m-kopa/prospect-analysis.pdf \
                                   results/m-kopa/prospect-data.json
"""

import json
import sys
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
    TableStyle,
)

# ── Palette ───────────────────────────────────────────────────────────────────

C_DARK      = colors.HexColor("#1a1a2e")
C_NAVY      = colors.HexColor("#16213e")
C_BLUE      = colors.HexColor("#0f3460")
C_GREEN     = colors.HexColor("#27ae60")
C_AMBER     = colors.HexColor("#f39c12")
C_RED       = colors.HexColor("#e74c3c")
C_STEEL     = colors.HexColor("#2980b9")
C_LIGHT     = colors.HexColor("#f8f9fa")
C_BORDER    = colors.HexColor("#dee2e6")
C_SUBTEXT   = colors.HexColor("#666666")
C_BODY      = colors.HexColor("#333333")


def _grade_hex(grade: str) -> str:
    return {"A+": "#27ae60", "A": "#27ae60", "B": "#2980b9",
            "C": "#f39c12", "D": "#e74c3c"}.get(grade, "#0f3460")


def _score_hex(score) -> str:
    try:
        s = int(score)
    except (TypeError, ValueError):
        return "#666666"
    if s >= 75: return "#27ae60"
    if s >= 60: return "#2980b9"
    if s >= 40: return "#f39c12"
    return "#e74c3c"


def _score_color(score) -> colors.Color:
    return colors.HexColor(_score_hex(score))


# ── Style factory ─────────────────────────────────────────────────────────────

def _styles():
    base = getSampleStyleSheet()

    def s(name, **kw):
        return ParagraphStyle(name, parent=base["Normal"], **kw)

    return {
        "title":    s("title",  fontSize=20, textColor=C_DARK,    fontName="Helvetica-Bold", spaceAfter=4),
        "meta":     s("meta",   fontSize=9,  textColor=C_SUBTEXT,  spaceAfter=10),
        "h1":       s("h1",     fontSize=13, textColor=C_DARK,     fontName="Helvetica-Bold",
                                spaceBefore=14, spaceAfter=5),
        "h2":       s("h2",     fontSize=10, textColor=C_BLUE,     fontName="Helvetica-Bold",
                                spaceBefore=8,  spaceAfter=3),
        "body":     s("body",   fontSize=9,  textColor=C_BODY,     leading=13, spaceAfter=4),
        "small":    s("small",  fontSize=8,  textColor=C_BODY,     leading=11, spaceAfter=2),
        "bullet":   s("bullet", fontSize=9,  textColor=C_BODY,     leading=13, leftIndent=10, spaceAfter=2),
        "signal":   s("signal", fontSize=8,  textColor=C_GREEN,    leading=11, leftIndent=10, spaceAfter=2),
        "flag":     s("flag",   fontSize=8,  textColor=C_RED,      leading=11, leftIndent=10, spaceAfter=2),
        "email_to": s("email_to", fontSize=9, textColor=C_BLUE,    fontName="Helvetica-Bold", spaceAfter=3),
        "email_body": s("email_body", fontSize=9, textColor=C_BODY, leading=13,
                        borderWidth=1, borderColor=C_BORDER, borderPad=8, backColor=C_LIGHT),
        "footer":   s("footer", fontSize=7,  textColor=C_SUBTEXT,  spaceAfter=0),
    }


def _tbl_style(header_bg=None) -> TableStyle:
    bg = header_bg or C_DARK
    return TableStyle([
        ("BACKGROUND",   (0, 0), (-1,  0), bg),
        ("TEXTCOLOR",    (0, 0), (-1,  0), colors.white),
        ("FONTNAME",     (0, 0), (-1,  0), "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C_LIGHT, colors.white]),
        ("GRID",         (0, 0), (-1, -1), 0.5, C_BORDER),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",   (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 4),
    ])


def _total_row_style() -> list:
    return [
        ("BACKGROUND",  (0, -1), (-1, -1), C_NAVY),
        ("TEXTCOLOR",   (0, -1), (-1, -1), colors.white),
        ("FONTNAME",    (0, -1), (-1, -1), "Helvetica-Bold"),
    ]


# ── Section builders ──────────────────────────────────────────────────────────

def _header_section(d: dict, st: dict) -> list:
    company   = d.get("company_name", "Unknown")
    score     = d.get("prospect_score", 0)
    grade     = d.get("grade", "?")
    label     = d.get("label", "")
    url       = d.get("url", "")
    date      = d.get("analysis_date", datetime.now().strftime("%Y-%m-%d"))
    confidence= d.get("confidence", "")

    g_hex = _grade_hex(grade)
    s_hex = _score_hex(score)

    name_cell  = Paragraph(f"<b>{company}</b>", st["title"])
    score_cell = Paragraph(
        f"<font size='32' color='{s_hex}'><b>{score}</b></font>"
        f"<font size='11' color='#666666'>/100</font><br/>"
        f"<font size='10' color='{g_hex}'><b>Grade {grade}</b></font>"
        f"<font size='9' color='#666666'> — {label}</font>",
        ParagraphStyle("sc", parent=getSampleStyleSheet()["Normal"],
                       alignment=TA_CENTER, leading=16),
    )

    tbl = Table([[name_cell, score_cell]], colWidths=["72%", "28%"])
    tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",  (1, 0), (1,  0),  "CENTER"),
    ]))

    return [
        tbl,
        Paragraph(f"{url}  |  {date}  |  Confidence: {confidence}", st["meta"]),
        HRFlowable(width="100%", thickness=2, color=C_BLUE),
        Spacer(1, 6),
    ]


def _score_table(d: dict, st: dict) -> list:
    weights = [
        ("company_fit",          "Company Fit",         0.25),
        ("contact_access",       "Contact Access",      0.20),
        ("opportunity_quality",  "Opportunity Quality", 0.20),
        ("competitive_position", "Competitive Position",0.15),
        ("outreach_readiness",   "Outreach Readiness",  0.20),
    ]
    scores = d.get("scores", {})
    rows   = [["Category", "Score", "Weight", "Weighted", "Key Finding"]]
    total  = 0.0

    for key, label, w in weights:
        entry = scores.get(key, {})
        sc    = entry.get("score", 0) or 0
        wt    = round(sc * w, 1)
        total += wt
        rows.append([
            label, str(sc), f"{int(w*100)}%", str(wt),
            Paragraph((entry.get("key_finding") or "")[:90],
                      ParagraphStyle("sf", parent=getSampleStyleSheet()["Normal"],
                                     fontSize=7, leading=10)),
        ])
    rows.append(["TOTAL", "", "100%", str(round(total, 1)), ""])

    tbl = Table(rows, colWidths=[38*mm, 14*mm, 14*mm, 18*mm, 71*mm])
    style = _tbl_style()
    style.add(*_total_row_style()[0])
    style.add(*_total_row_style()[1])
    style.add(*_total_row_style()[2])
    style.add("ALIGN", (1, 0), (3, -1), "CENTER")
    tbl.setStyle(style)

    return [Paragraph("Score Breakdown", st["h1"]), tbl, Spacer(1, 8)]


def _snapshot_table(d: dict, st: dict) -> list:
    dm    = d.get("key_decision_maker", {}) or {}
    dm_str = f"{dm.get('name','—')} — {dm.get('title','')}" if isinstance(dm, dict) else "—"

    items = [
        ("Industry",        d.get("industry", "—")),
        ("Company Type",    d.get("company_type", "—")),
        ("Founded",         d.get("founded", "—")),
        ("Employees",       d.get("employees", "—")),
        ("Funding",         d.get("funding", "—")),
        ("Revenue Est.",    d.get("revenue_estimate", "—")),
        ("HQ",              d.get("hq_location", "—")),
        ("Key DM",          dm_str[:70]),
        ("Next Action",     (d.get("recommended_action") or "—")[:80]),
    ]
    rows = [
        [Paragraph(f"<b>{k}</b>",
                   ParagraphStyle("sk", parent=getSampleStyleSheet()["Normal"],
                                  fontSize=8, textColor=C_BODY)),
         Paragraph(v,
                   ParagraphStyle("sv", parent=getSampleStyleSheet()["Normal"],
                                  fontSize=8, textColor=C_BODY, leading=11))]
        for k, v in items
    ]
    tbl = Table(rows, colWidths=[36*mm, 119*mm])
    tbl.setStyle(TableStyle([
        ("ROWBACKGROUNDS",  (0, 0), (-1, -1), [C_LIGHT, colors.white]),
        ("FONTSIZE",        (0, 0), (-1, -1), 8),
        ("TOPPADDING",      (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING",   (0, 0), (-1, -1), 4),
        ("GRID",            (0, 0), (-1, -1), 0.5, C_BORDER),
    ]))
    return [Paragraph("Prospect Snapshot", st["h1"]), tbl, Spacer(1, 8)]


def _bant_table(d: dict, st: dict) -> list:
    bant  = d.get("bant", {}) or {}
    dims  = ["budget", "authority", "need", "timeline"]
    rows  = [["Dimension", "Score /25", "Evidence", "Conf."]]

    for dim in dims:
        entry = bant.get(dim, {}) or {}
        rows.append([
            dim.capitalize(),
            str(entry.get("score", 0)),
            Paragraph((entry.get("evidence") or "")[:110],
                      ParagraphStyle("be", parent=getSampleStyleSheet()["Normal"],
                                     fontSize=8, leading=10)),
            "",
        ])
    rows.append(["TOTAL", f"{bant.get('total', 0)}/100", "", ""])

    tbl = Table(rows, colWidths=[25*mm, 18*mm, 100*mm, 12*mm])
    style = _tbl_style()
    style.add(*_total_row_style()[0])
    style.add(*_total_row_style()[1])
    style.add(*_total_row_style()[2])
    style.add("ALIGN", (1, 0), (1, -1), "CENTER")
    tbl.setStyle(style)

    return [Paragraph("BANT Scorecard", st["h1"]), tbl, Spacer(1, 8)]


def _committee_table(d: dict, st: dict) -> list:
    committee = d.get("buying_committee", []) or []
    if not committee:
        return []

    rows = [["Name", "Title", "Role", "Personalization Anchor"]]
    ns = getSampleStyleSheet()["Normal"]
    for p in committee[:8]:
        rows.append([
            Paragraph(f"<b>{p.get('name','')}</b>",
                      ParagraphStyle("cn", parent=ns, fontSize=8)),
            Paragraph((p.get("title") or "")[:40],
                      ParagraphStyle("ct", parent=ns, fontSize=8, leading=10)),
            Paragraph((p.get("role") or "")[:30],
                      ParagraphStyle("cr", parent=ns, fontSize=8, leading=10)),
            Paragraph((p.get("personalization_anchor") or "")[:85],
                      ParagraphStyle("ca", parent=ns, fontSize=8, leading=10)),
        ])

    tbl = Table(rows, colWidths=[32*mm, 40*mm, 28*mm, 55*mm])
    tbl.setStyle(_tbl_style())
    return [Paragraph("Buying Committee", st["h1"]), tbl, Spacer(1, 8)]


def _signals_flags(d: dict, st: dict) -> list:
    signals = d.get("buying_signals", []) or []
    flags   = d.get("red_flags", []) or []
    if not signals and not flags:
        return []

    ns  = getSampleStyleSheet()["Normal"]
    hdr = ParagraphStyle("hdr", parent=ns, fontSize=8, fontName="Helvetica-Bold")

    col1 = ([Paragraph("Buying Signals", ParagraphStyle("sh", parent=hdr, textColor=C_GREEN))] +
            [Paragraph(f"+ {s}", ParagraphStyle("s", parent=ns, fontSize=8, textColor=C_GREEN, leading=11))
             for s in signals[:6]])
    col2 = ([Paragraph("Red Flags", ParagraphStyle("rh", parent=hdr, textColor=C_RED))] +
            [Paragraph(f"! {f}", ParagraphStyle("f", parent=ns, fontSize=8, textColor=C_RED, leading=11))
             for f in flags[:6]])

    max_len = max(len(col1), len(col2))
    empty   = Paragraph("", ParagraphStyle("empty", parent=ns, fontSize=8))
    col1 += [empty] * (max_len - len(col1))
    col2 += [empty] * (max_len - len(col2))

    rows = [[c1, c2] for c1, c2 in zip(col1, col2)]
    tbl  = Table(rows, colWidths=[82*mm, 73*mm])
    tbl.setStyle(TableStyle([
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",   (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
    ]))
    return [Paragraph("Signals & Red Flags", st["h1"]), tbl, Spacer(1, 8)]


def _email_section(d: dict, st: dict) -> list:
    email = d.get("outreach_email", {}) or {}
    if not email:
        return []

    to_str   = f"To: {email.get('to_name','')} &lt;{email.get('to_email','')}&gt; — {email.get('to_title','')}"
    body_raw = (email.get("body") or "").replace("\n", "<br/>")

    return [
        PageBreak(),
        Paragraph("Ready-to-Send Email", st["h1"]),
        Paragraph(to_str, st["email_to"]),
        Spacer(1, 3),
        Paragraph(f"Subject A: {email.get('subject_a','')}", st["body"]),
        Paragraph(f"Subject B: {email.get('subject_b','')}", st["body"]),
        Spacer(1, 6),
        Paragraph(body_raw, st["email_body"]),
        Spacer(1, 12),
    ]


def _actions_section(d: dict, st: dict) -> list:
    actions = d.get("immediate_actions", []) or []
    if not actions:
        return []
    items = [Paragraph("Immediate Actions (Next 24-48 Hours)", st["h1"])]
    for a in actions:
        items.append(Paragraph(f"• {a}", st["bullet"]))
    items.append(Spacer(1, 6))
    return items


def _footer(d: dict, st: dict) -> list:
    date = d.get("analysis_date", datetime.now().strftime("%Y-%m-%d"))
    return [
        HRFlowable(width="100%", thickness=0.5, color=C_BORDER),
        Paragraph(f"Generated by AI Sales Batch Analyzer | {date}", st["footer"]),
    ]


# ── Main PDF builder ──────────────────────────────────────────────────────────

def generate_pdf(markdown_text: str, json_data: dict, output_path: str):
    """Build a PDF report and save to output_path."""
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        topMargin=14*mm, bottomMargin=14*mm,
        leftMargin=18*mm, rightMargin=18*mm,
    )

    st    = _styles()
    story = []

    story += _header_section(json_data, st)
    story += _score_table(json_data, st)
    story += _snapshot_table(json_data, st)
    story += _bant_table(json_data, st)
    story += _committee_table(json_data, st)
    story += _signals_flags(json_data, st)
    story += _email_section(json_data, st)
    story += _actions_section(json_data, st)
    story += _footer(json_data, st)

    doc.build(story)


# ── Standalone usage ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python generate_pdf.py <input.md> <output.pdf> [data.json]")
        sys.exit(1)

    md_path  = Path(sys.argv[1])
    pdf_path = sys.argv[2]
    js_path  = Path(sys.argv[3]) if len(sys.argv) > 3 else None

    md_text   = md_path.read_text(encoding="utf-8")
    data      = json.loads(js_path.read_text(encoding="utf-8")) if js_path and js_path.exists() else {}

    generate_pdf(md_text, data, pdf_path)
    print(f"Saved: {pdf_path}")
