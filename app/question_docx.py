"""
Parse multi-question quiz scripts from .docx files.

Each question block has:
  Question N
  <question body, one or more paragraphs>
  a. <choice>
  b. <choice>
  c. <choice>
  d. <choice>
  Correct Answer: <letter>. <text>
  Explanation: Option <letter> is correct because <reason for correct>
  Option <letter> is incorrect because <reason>
  Option <letter> is incorrect because <reason>
  Option <letter> is incorrect because <reason>
"""
from __future__ import annotations

import io
import re
from typing import Iterator, List, Optional

from docx import Document
from docx.document import Document as DocumentObject
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

MAX_QUESTIONS = 500

_QUESTION_HEADER_RE = re.compile(r"^\s*Question\s+(\d+)\b", re.IGNORECASE)
_ANSWER_RE = re.compile(r"^\s*([a-dA-D])\s*[\.\)]\s+(.+?)\s*$")
_CORRECT_ANSWER_RE = re.compile(r"^\s*Correct\s+Answer\s*:\s*(.*)$", re.IGNORECASE)
_EXPLANATION_PREFIX_RE = re.compile(r"^\s*Explanation\s*:\s*(.*)$", re.IGNORECASE)
_INCORRECT_LINE_RE = re.compile(r"^\s*Option\s+[a-dA-D]\b.*\bis\s+incorrect\b", re.IGNORECASE)
_OPTION_CORRECT_RE = re.compile(r"^\s*Option\s+[a-dA-D]\b.*\bis\s+correct\b", re.IGNORECASE)


def _normalized(raw: str) -> str:
    if not raw:
        return ""
    s = (
        raw.replace(" ", " ")
        .replace(" ", " ")
        .replace(" ", " ")
        .replace(" ", " ")
        .replace("﻿", "")
    )
    return s.strip()


def _iter_paragraphs(doc: DocumentObject) -> Iterator[str]:
    """Yield paragraph text in document order, including those inside tables."""
    body = doc.element.body
    for child in body:
        if child.tag == qn("w:p"):
            p = Paragraph(child, doc)
            yield p.text
        elif child.tag == qn("w:tbl"):
            tbl = Table(child, doc)
            for row in tbl.rows:
                for cell in row.cells:
                    for p in cell.paragraphs:
                        yield p.text


def _build_voice_texts(q: dict) -> dict:
    """Pre-compute the four voice scripts so the frontend can preview / generation can reuse."""
    number = q["number"]
    qbody = (q.get("question_text") or "").strip()
    voice1 = f"Question {number}. {qbody}".strip()

    answer_lines = q.get("answers") or []
    correct_line = (q.get("correct_answer_line") or "").strip()
    voice2_parts: List[str] = [a.strip() for a in answer_lines if a.strip()]
    if correct_line:
        voice2_parts.append(correct_line)
    voice2 = "\n".join(voice2_parts)

    voice3 = (q.get("correct_explanation") or "").strip()

    incorrect = [s.strip() for s in (q.get("incorrect_explanations") or []) if s.strip()]
    # Short pause between the three incorrect option explanations.
    voice4 = '\n<break time="0.7s" />\n'.join(incorrect)

    return {"voice1": voice1, "voice2": voice2, "voice3": voice3, "voice4": voice4}


def _parse_one(block: dict) -> Optional[dict]:
    number = block["number"]
    lines = block["lines"]

    # Find the first answer line (must be 'a.').
    first_a_idx = None
    for i, ln in enumerate(lines):
        m = _ANSWER_RE.match(ln or "")
        if m and m.group(1).lower() == "a":
            first_a_idx = i
            break
    if first_a_idx is None:
        return None

    # Question body = paragraphs above the first answer line, blanks dropped.
    qtext_lines = [(ln or "").strip() for ln in lines[:first_a_idx] if (ln or "").strip()]
    qtext = " ".join(qtext_lines).strip()
    if not qtext:
        return None

    # Up to four answer lines starting at first_a_idx, in order a/b/c/d.
    answers: List[str] = []
    expected = ["a", "b", "c", "d"]
    i = first_a_idx
    while i < len(lines) and len(answers) < 4:
        ln = (lines[i] or "").strip()
        if not ln:
            i += 1
            continue
        m = _ANSWER_RE.match(ln)
        if m and m.group(1).lower() == expected[len(answers)]:
            answers.append(ln)
            i += 1
            continue
        # Stop trying to read answers once the sequence breaks.
        break
    if len(answers) != 4:
        return None

    # Locate "Correct Answer: …" line.
    correct_line = None
    correct_idx = i
    for j in range(i, len(lines)):
        if _CORRECT_ANSWER_RE.match(lines[j] or ""):
            correct_line = (lines[j] or "").strip()
            correct_idx = j
            break

    # Correct explanation: paragraph that starts with "Explanation:" (prefix stripped),
    # plus any continuation paragraphs until the first "Option X is incorrect" line.
    correct_explanation = ""
    explanation_idx = correct_idx + 1
    expl_start = None
    for j in range(correct_idx + 1, len(lines)):
        ln = lines[j] or ""
        m = _EXPLANATION_PREFIX_RE.match(ln)
        if m:
            head = m.group(1).strip()
            expl_start = j
            buf: List[str] = []
            if head:
                buf.append(head)
            k = j + 1
            while k < len(lines):
                nxt = (lines[k] or "").strip()
                if not nxt:
                    k += 1
                    continue
                if _INCORRECT_LINE_RE.match(nxt):
                    break
                buf.append(nxt)
                k += 1
            correct_explanation = " ".join(buf).strip()
            explanation_idx = k
            break
        # Some docs may skip the literal "Explanation:" prefix and jump straight to "Option X is correct because…"
        if _OPTION_CORRECT_RE.match(ln):
            buf = [ln.strip()]
            k = j + 1
            while k < len(lines):
                nxt = (lines[k] or "").strip()
                if not nxt:
                    k += 1
                    continue
                if _INCORRECT_LINE_RE.match(nxt):
                    break
                buf.append(nxt)
                k += 1
            correct_explanation = " ".join(buf).strip()
            explanation_idx = k
            break

    # Three (or however many) "Option X is incorrect …" paragraphs follow.
    incorrect_explanations: List[str] = []
    j = explanation_idx
    while j < len(lines):
        ln = (lines[j] or "").strip()
        if not ln:
            j += 1
            continue
        if _INCORRECT_LINE_RE.match(ln):
            buf = [ln]
            k = j + 1
            while k < len(lines):
                nxt = (lines[k] or "").strip()
                if not nxt:
                    k += 1
                    continue
                if _INCORRECT_LINE_RE.match(nxt):
                    break
                buf.append(nxt)
                k += 1
            incorrect_explanations.append(" ".join(buf).strip())
            j = k
            continue
        j += 1

    q = {
        "number": number,
        "question_text": qtext,
        "answers": answers,
        "correct_answer_line": correct_line,
        "correct_explanation": correct_explanation,
        "incorrect_explanations": incorrect_explanations,
    }
    q["voice_texts"] = _build_voice_texts(q)
    # Lightweight validation flags so the UI can warn instead of fail.
    q["warnings"] = []
    if not correct_line:
        q["warnings"].append("Missing 'Correct Answer:' line.")
    if not correct_explanation:
        q["warnings"].append("Missing correct-answer explanation.")
    if len(incorrect_explanations) < 3:
        q["warnings"].append(f"Found {len(incorrect_explanations)} incorrect explanation(s); expected 3.")
    return q


def parse_question_docx(file_bytes: bytes) -> dict:
    """
    Returns:
      {
        "count": int,
        "questions": [ ... ],
      }
    """
    doc = Document(io.BytesIO(file_bytes))
    raw_paragraphs = [_normalized(p) for p in _iter_paragraphs(doc)]

    blocks: List[dict] = []
    current: Optional[dict] = None
    for line in raw_paragraphs:
        m = _QUESTION_HEADER_RE.match(line)
        if m:
            if current is not None:
                blocks.append(current)
            current = {"number": int(m.group(1)), "lines": []}
            continue
        if current is not None:
            current["lines"].append(line)
    if current is not None:
        blocks.append(current)

    if not blocks:
        raise ValueError("No 'Question N' headers found in document.")
    if len(blocks) > MAX_QUESTIONS:
        raise ValueError(f"Too many questions ({len(blocks)}); maximum is {MAX_QUESTIONS}.")

    questions: List[dict] = []
    for blk in blocks:
        parsed = _parse_one(blk)
        if parsed is not None:
            questions.append(parsed)

    if not questions:
        raise ValueError(
            "No fully-formed questions parsed. Each block needs a 'Question N' header, "
            "four 'a./b./c./d.' answer lines, a 'Correct Answer:' line, and an 'Explanation:' paragraph."
        )

    return {"count": len(questions), "questions": questions}
