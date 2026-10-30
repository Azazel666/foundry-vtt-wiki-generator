"""Plain text from HTML descriptions and README Markdown."""

import html
import re

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_BLOCK_ELEMENT = re.compile(r"<(script|style)\b.*?</\1\s*>", re.S | re.I)
_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_BLOCK_END = re.compile(r"</(?:p|div|li|h[1-6]|blockquote)\s*>|<br\s*/?>", re.I)
_LINKED_IMAGE = re.compile(r"\[\s*!\[[^\]]*\]\([^)]*\)\s*\]\([^)]*\)")
_IMAGE = re.compile(r"!\[[^\]]*\](\([^)]*\)|\[[^\]]*\])")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_REFERENCE_LINK = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_REFERENCE_DEFINITION = re.compile(r"^\s*\[[^\]]+\]:\s*\S+.*$", re.M)
_BARE_URL = re.compile(r"\bhttps?://\S+")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)
_INSTALL_HEADING = re.compile(r"^(how to )?(install|installing|installation)\b", re.I)


def strip_html(value: str) -> str:
    """Manifest descriptions are HTML. The result is plain text, entities decoded: escape it on output."""
    text = _COMMENT.sub(" ", value)
    text = _BLOCK_ELEMENT.sub(" ", text)
    text = _BLOCK_END.sub(" ", text)
    text = _TAG.sub("", text)  # inline tags: a space here would split "<em>word</em>." from its full stop
    return " ".join(html.unescape(text).split())


def html_paragraphs(value: str) -> list[str]:
    """Rich text as plain paragraphs, for descriptions long enough that one block would be hard to read."""
    return [text for part in _BLOCK_END.split(value) if (text := strip_html(part))]


def _drop_install_sections(text: str) -> str:
    headings = list(_HEADING.finditer(text))
    keep, position = [], 0
    for index, heading in enumerate(headings):
        if heading.start() < position or not _INSTALL_HEADING.match(heading[2]):
            continue
        keep.append(text[position:heading.start()])
        level = len(heading[1])
        following = (h for h in headings[index + 1:] if len(h[1]) <= level)
        position = next((h.start() for h in following), len(text))
    keep.append(text[position:])
    return "".join(keep)


def clean_readme(text: str) -> str:
    """Images, badges, tags and URLs go; link text stays. Install instructions are of no use to a GM."""
    text = text.replace("\r\n", "\n")
    text = _COMMENT.sub("", text)
    text = _BLOCK_ELEMENT.sub("", text)
    text = _LINKED_IMAGE.sub("", text)
    text = _IMAGE.sub("", text)
    text = _REFERENCE_DEFINITION.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _REFERENCE_LINK.sub(r"\1", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _BARE_URL.sub("", text)
    text = _drop_install_sections(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
