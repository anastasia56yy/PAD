"""HTML/PDF -> clean structured text.

The result keeps the section structure as markdown-like headings ("## Title"),
because the heading chunking strategy relies on it.
"""
from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup, NavigableString

HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "####", "h6": "####"}
BLOCKS = {"p", "li", "td", "th", "blockquote", "pre", "dd", "dt", "figcaption"}

# Romanian texts often mix the correct comma-below letters with legacy cedilla ones
DIACRITICS = str.maketrans({"ş": "ș", "Ş": "Ș", "ţ": "ț", "Ţ": "Ț"})

# Boilerplate that WordPress themes/plugins inject into the content field
NOISE_PATTERNS = [
    r"^(share|distribuie|partajează|tweet)\b.*$",
    r"^(citește|citeste|read) (mai mult|more).*$",
    r"^\s*(previous|next|următorul|precedentul)\s*$",
    r"^\s*\[?\s*(pdf|download|descarcă|descarca)\s*\]?\s*$",
]
NOISE_RE = re.compile("|".join(NOISE_PATTERNS), re.IGNORECASE | re.MULTILINE)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text).translate(DIACRITICS)
    text = text.replace(" ", " ").replace("​", "").replace("﻿", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_text(html: str, drop_selectors: list[str] | None = None) -> str:
    soup = BeautifulSoup(html, "lxml")
    for selector in drop_selectors or []:
        for node in soup.select(selector):
            node.decompose()

    lines: list[str] = []
    for node in soup.find_all(list(HEADINGS) + list(BLOCKS)):
        # nested blocks (p inside li/td) would be emitted twice
        if node.find_parent(list(BLOCKS)) is not None:
            continue
        text = " ".join(s.strip() for s in node.find_all(string=True) if isinstance(s, NavigableString) and s.strip())
        if not text:
            continue
        if node.name in HEADINGS:
            lines.append(f"\n{HEADINGS[node.name]} {text}\n")
        elif node.name == "li":
            lines.append(f"- {text}")
        else:
            lines.append(text)

    if not lines:  # page builders sometimes put text straight into <div>s
        lines = [soup.get_text("\n")]
    return "\n".join(lines)


def pdf_text_cleanup(text: str) -> str:
    # words split by a hyphen at the line end: "regula-\nment" -> "regulament"
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    # page numbers / running footers that are just a number
    text = re.sub(r"^\s*(pag(ina)?\.?\s*)?\d{1,3}(\s*(/|din)\s*\d{1,3})?\s*$", "", text, flags=re.IGNORECASE | re.MULTILINE)
    # PDF lines are broken by layout; glue lines that do not end a sentence
    text = re.sub(r"(?<![.:;!?])\n(?=[a-zăâîșț(])", " ", text)
    # numbered articles become headings so heading-based chunking works for regulations
    text = re.sub(r"^\s*((Art(icolul)?\.?|Capitolul|CAPITOLUL|Secțiunea)\s+[\dIVXLC]+\.?.*)$", r"\n## \1\n",
                  text, flags=re.MULTILINE)
    return text


def clean_document(body: str, body_format: str, drop_selectors: list[str] | None = None) -> str:
    text = html_to_text(body, drop_selectors) if body_format == "html" else pdf_text_cleanup(body)
    text = NOISE_RE.sub("", text)
    return normalize_text(text)
