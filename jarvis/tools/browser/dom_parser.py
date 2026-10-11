"""DOM parsing + compression — how JARVIS "sees" a page without a vision model.

The pipeline is: HTML → tree → (interactive elements + readable text) → a
**budgeted, numbered listing** that fits a small model's context.

That numbering is the key trick: instead of asking the LLM for a CSS selector
(which small models are bad at), JARVIS hands it a menu::

    [1] <a> Gmail
    [2] <input:text> Search Google or type a URL
    [3] <button> Search

and the model replies ``click_index(3)``.  The harness keeps the index →
selector/coordinates map, so the click is exact.

Everything is stdlib-only (``html.parser``); BeautifulSoup is used when present
because it handles malformed real-world HTML better.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urljoin

from ...utils.logger import get_logger
from ...utils.sanitizer import sanitize_text, truncate

log = get_logger("dom")

__all__ = ["DOMElement", "DOMSnapshot", "parse_html", "render_listing",
           "selector_for"]

_INTERACTIVE_TAGS = {
    "a", "button", "input", "select", "textarea", "option", "label",
    "summary", "details",
}
_INTERACTIVE_ROLES = {
    "button", "link", "menuitem", "tab", "checkbox", "radio", "switch",
    "textbox", "searchbox", "combobox", "option", "submit",
}
# Tags whose *content* is never page text.  Only container tags appear here —
# a void tag such as <meta> has no closing tag, so treating it as skippable
# would swallow the rest of the document.
_SKIP_TAGS = {"script", "style", "noscript", "svg", "path", "template"}
_VOID_TAGS = {"meta", "link", "br", "img", "hr", "input", "source", "track",
              "wbr", "area", "base", "col", "embed", "param"}
_BLOCK_TAGS = {"p", "div", "section", "article", "li", "br", "tr", "h1", "h2",
               "h3", "h4", "h5", "h6", "header", "footer", "nav", "main",
               "table", "ul", "ol", "form", "figure", "blockquote", "pre"}


@dataclass
class DOMElement:
    """One interactive element on the page."""

    index: int
    tag: str
    selector: str
    text: str = ""
    kind: str = "element"            # link | button | input | select | checkbox…
    href: str = ""
    placeholder: str = ""
    input_type: str = ""
    name: str = ""
    element_id: str = ""
    classes: List[str] = field(default_factory=list)
    attrs: Dict[str, str] = field(default_factory=dict)
    depth: int = 0
    box: Optional[Dict[str, float]] = None      # filled after a live locate
    visible: bool = True

    def label(self, max_text: int = 60) -> str:
        text = self.text or self.placeholder or self.name or self.element_id or ""
        text = truncate(re.sub(r"\s+", " ", text).strip(), max_text)
        if self.kind == "input":
            what = self.input_type or "text"
            return f"<input:{what}> {text}" if text else f"<input:{what}>"
        if self.kind == "link" and self.href:
            return f"<a> {text or self.href}"
        return f"<{self.tag}> {text}".strip()

    def describe(self) -> Dict[str, Any]:
        return {"index": self.index, "kind": self.kind, "text": self.text,
                "href": self.href, "selector": self.selector}


@dataclass
class DOMSnapshot:
    """Everything the agent gets to know about a page in one shot."""

    url: str = ""
    title: str = ""
    text: str = ""
    elements: List[DOMElement] = field(default_factory=list)
    forms: List[Dict[str, Any]] = field(default_factory=list)
    meta: Dict[str, str] = field(default_factory=dict)
    truncated: bool = False

    def by_index(self, index: int) -> Optional[DOMElement]:
        for element in self.elements:
            if element.index == index:
                return element
        return None

    def find(self, needle: str) -> List[DOMElement]:
        low = needle.lower()
        return [e for e in self.elements
                if low in (e.text or "").lower() or low in (e.href or "").lower()
                or low in (e.name or "").lower() or low in (e.element_id or "").lower()]

    def as_text(self, *, max_chars: int = 6000) -> str:
        listing = render_listing(self.elements)
        body = truncate(self.text, max(0, max_chars - len(listing) - 200))
        parts = [f"**Page:** {self.title or '(untitled)'}",
                 f"**URL:** {self.url}"]
        if listing:
            parts.append(f"**Interactive elements:**\n{listing}")
        if body:
            parts.append(f"**Page text:**\n{body}")
        parts.append("_Tip: act on elements by their [index] — "
                     "click_index / type_index._")
        return "\n\n".join(parts)


class _TreeBuilder(HTMLParser):
    """Stdlib HTML → element list with CSS-selector paths."""

    def __init__(self, base_url: str = "", *, max_elements: int = 120,
                 max_text_chars: int = 8000):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.max_elements = max_elements
        self.max_text_chars = max_text_chars
        self.elements: List[DOMElement] = []
        self.text_parts: List[str] = []
        self.forms: List[Dict[str, Any]] = []
        self.meta: Dict[str, str] = {}
        self.title = ""
        self._stack: List[Dict[str, Any]] = []
        self._skip_depth = 0
        self._in_title = False
        self._current_form: Optional[Dict[str, Any]] = None
        self._text_len = 0

    # ── helpers ────────────────────────────────────────────────────────────
    def _selector(self, tag: str, attrs: Mapping[str, str]) -> str:
        if attrs.get("id"):
            return f"#{attrs['id']}"
        parts: List[str] = []
        for frame in self._stack[-6:]:
            position = frame.get("count", 1)
            parts.append(f"{frame['tag']}:nth-of-type({position})")
        parts.append(f"{tag}:nth-of-type({self._sibling_count(tag) + 1})")
        return " > ".join(parts) if parts else tag

    def _sibling_count(self, tag: str) -> int:
        if not self._stack:
            return 0
        return self._stack[-1].get("children", {}).get(tag, 0)

    def _bump_sibling(self, tag: str) -> None:
        if self._stack:
            children = self._stack[-1].setdefault("children", {})
            children[tag] = children.get(tag, 0) + 1

    def _add_text(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        if self._text_len >= self.max_text_chars:
            return
        text = re.sub(r"\s+", " ", data)
        if not text.strip():
            return
        self.text_parts.append(text)
        self._text_len += len(text) + 1

    # ── parser hooks ───────────────────────────────────────────────────────
    def handle_starttag(self, tag: str, attrs_list: List[Tuple[str, Optional[str]]]) -> None:
        attrs = {k.lower(): (v or "") for k, v in attrs_list}
        if tag in _SKIP_TAGS:
            if tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        if self._skip_depth:
            return
        self._bump_sibling(tag)
        depth = len(self._stack)
        frame = {"tag": tag, "count": self._sibling_count(tag) + 1,
                 "children": {}, "attrs": attrs}
        self._stack.append(frame)

        if tag == "title":
            self._in_title = True
        if tag == "meta":
            key = attrs.get("name") or attrs.get("property")
            if key and attrs.get("content"):
                self.meta[key] = attrs["content"]
            self._stack.pop()          # void element: nothing to close
            return
        if tag == "form":
            self._current_form = {"action": attrs.get("action", ""),
                                  "method": attrs.get("method", "get"),
                                  "fields": []}
            self.forms.append(self._current_form)
        if tag in _VOID_TAGS and tag not in _INTERACTIVE_TAGS:
            self._stack.pop()
            return
        if tag in _BLOCK_TAGS:
            self.text_parts.append("\n")

        role = attrs.get("role", "")
        input_type = attrs.get("type", "text").lower()
        is_interactive = (
            tag in _INTERACTIVE_TAGS or role.lower() in _INTERACTIVE_ROLES
            or attrs.get("onclick") or attrs.get("contenteditable") == "true"
        )
        # ignore hidden inputs and disabled controls
        if is_interactive and input_type != "hidden" and "disabled" not in attrs:
            if len(self.elements) < self.max_elements:
                kind = self._kind_for(tag, input_type, role)
                element = DOMElement(
                    index=len(self.elements) + 1, tag=tag,
                    selector=self._selector(tag, attrs),
                    text=attrs.get("aria-label", "") or attrs.get("title", ""),
                    kind=kind,
                    href=urljoin(self.base_url, attrs["href"]) if attrs.get("href") else "",
                    placeholder=attrs.get("placeholder", ""),
                    input_type=input_type if tag == "input" else "",
                    name=attrs.get("name", ""),
                    element_id=attrs.get("id", ""),
                    classes=[c for c in attrs.get("class", "").split() if c][:6],
                    attrs={k: v for k, v in attrs.items()
                           if k in ("type", "value", "aria-label", "title", "alt",
                                    "role", "data-testid", "for", "checked")},
                    depth=depth,
                )
                self.elements.append(element)
                if self._current_form is not None and tag in ("input", "select", "textarea"):
                    self._current_form["fields"].append(
                        element.name or element.placeholder or f"field{element.index}")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if tag not in _VOID_TAGS:
                self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        if tag == "form":
            self._current_form = None
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i]["tag"] == tag:
                del self._stack[i:]
                break
        if tag in _BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data.strip() + " "
            return
        self._add_text(data)
        # attach visible text to the element we are inside (for labels/buttons)
        if self.elements and data.strip():
            innermost = self._innermost_interactive_index()
            if innermost is not None:
                element = self.elements[innermost]
                combined = f"{element.text} {data.strip()}".strip()
                element.text = truncate(combined, 160)

    def _innermost_interactive_index(self) -> Optional[int]:
        if not self._stack:
            return None
        # find the last element whose selector prefix matches the open stack
        for element in reversed(self.elements[-12:]):
            if element.tag in _INTERACTIVE_TAGS:
                return element.index - 1
        return None

    def _kind_for(self, tag: str, input_type: str, role: str) -> str:
        if tag == "a":
            return "link"
        if tag == "button" or role in ("button", "menuitem", "tab", "submit"):
            return "button"
        if tag in ("input", "textarea", "select"):
            if input_type in ("checkbox", "radio", "submit", "button", "range"):
                return "input" if input_type not in ("submit", "button") else "button"
            return "input"
        if tag in ("option", "label", "summary"):
            return "text"
        return "element"

    def finish(self) -> Tuple[str, str]:
        text = "".join(self.text_parts)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
        return self.title.strip(), text


def parse_html(html: str, *, url: str = "", max_elements: int = 120,
               max_text_chars: int = 8000) -> DOMSnapshot:
    """Parse HTML into a :class:`DOMSnapshot` (BeautifulSoup-accelerated if present)."""
    if not html:
        return DOMSnapshot(url=url)
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    snapshot: Optional[DOMSnapshot] = None
    try:
        snapshot = _parse_with_bs4(html, url=url, max_elements=max_elements,
                                   max_text_chars=max_text_chars)
    except Exception:
        snapshot = None
    if snapshot is None:
        parser = _TreeBuilder(url, max_elements=max_elements,
                              max_text_chars=max_text_chars)
        try:
            parser.feed(html)
            parser.close()
        except Exception as exc:  # malformed HTML — keep whatever we got
            log.debug(f"html parser stopped early: {exc}")
        title, text = parser.finish()
        snapshot = DOMSnapshot(url=url, title=title, text=text,
                               elements=parser.elements, forms=parser.forms,
                               meta=parser.meta)
    snapshot.truncated = len(html) > max_text_chars * 3
    snapshot.text = sanitize_text(snapshot.text, max_chars=max_text_chars * 2,
                                  wrap_untrusted=False)
    return snapshot


def _parse_with_bs4(html: str, *, url: str, max_elements: int,
                    max_text_chars: int) -> Optional[DOMSnapshot]:
    """Optional fast path when BeautifulSoup is installed."""
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except Exception:
        return None

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()

    elements: List[DOMElement] = []
    for node in soup.find_all(True):
        if len(elements) >= max_elements:
            break
        name = node.name.lower()
        attrs = {k.lower(): (v if isinstance(v, str) else " ".join(v))
                 for k, v in node.attrs.items()}
        role = attrs.get("role", "").lower()
        input_type = attrs.get("type", "text").lower()
        if not (name in _INTERACTIVE_TAGS or role in _INTERACTIVE_ROLES
                or "onclick" in attrs or attrs.get("contenteditable") == "true"):
            continue
        if input_type == "hidden" or "disabled" in attrs:
            continue
        text = node.get_text(" ", strip=True)
        if name == "input" and not text:
            text = attrs.get("value", "") or attrs.get("aria-label", "") \
                or attrs.get("placeholder", "")
        kind = "link" if name == "a" else (
            "button" if name == "button" or input_type in ("submit", "button") else
            "input" if name in ("input", "textarea", "select") else "element")
        elements.append(DOMElement(
            index=len(elements) + 1, tag=name,
            selector=_bs4_selector(node),
            text=truncate(text, 160),
            kind=kind,
            href=urljoin(url, attrs.get("href", "")) if attrs.get("href") else "",
            placeholder=attrs.get("placeholder", ""),
            input_type=input_type if name == "input" else "",
            name=attrs.get("name", ""), element_id=attrs.get("id", ""),
            classes=(attrs.get("class", "").split() if attrs.get("class") else [])[:6],
            attrs={k: v for k, v in attrs.items()
                   if k in ("type", "value", "aria-label", "title", "alt", "role",
                            "data-testid", "for", "checked")},
        ))

    forms = []
    for form in soup.find_all("form"):
        forms.append({
            "action": form.attrs.get("action", ""),
            "method": form.attrs.get("method", "get"),
            "fields": [field.attrs.get("name", "") for field in
                       form.find_all(["input", "select", "textarea"])
                       if field.attrs.get("name")],
        })
    meta = {}
    for tag in soup.find_all("meta"):
        key = tag.attrs.get("name") or tag.attrs.get("property")
        if key and tag.attrs.get("content"):
            meta[str(key)] = str(tag.attrs["content"])
    title = soup.title.get_text(strip=True) if soup.title else ""
    text = soup.get_text("\n", strip=True)
    return DOMSnapshot(url=url, title=title, text=text, elements=elements,
                       forms=forms, meta=meta)


def _bs4_selector(node: Any) -> str:
    if node.get("id"):
        return f"#{node['id']}"
    path: List[str] = []
    current = node
    for _ in range(6):
        if current is None or getattr(current, "name", None) is None:
            break
        parent = getattr(current, "parent", None)
        siblings = [c for c in getattr(parent, "children", []) or []
                    if getattr(c, "name", None) == current.name]
        position = siblings.index(current) + 1 if current in siblings else 1
        path.insert(0, f"{current.name}:nth-of-type({position})")
        current = parent
    return " > ".join(path) or "body"


def selector_for(element: DOMElement) -> str:
    """Best selector for clicking an element (id wins, then path)."""
    if element.element_id:
        return f"#{element.element_id}"
    if element.attrs.get("data-testid"):
        return f"[data-testid=\"{element.attrs['data-testid']}\"]"
    return element.selector or element.tag


def render_listing(elements: Sequence[DOMElement], *, max_items: int = 60,
                   max_chars: int = 2200) -> str:
    """The numbered menu handed to the LLM."""
    if not elements:
        return ""
    lines: List[str] = []
    used = 0
    seen: set[str] = set()
    for element in elements[:max_items]:
        label = element.label()
        if not label or label in seen:
            continue
        seen.add(label)
        line = f"[{element.index}] {label}"
        if used + len(line) > max_chars:
            lines.append(f"… {len(elements) - element.index + 1} more elements")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def extract_links(snapshot: DOMSnapshot, *, limit: int = 40) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for element in snapshot.elements:
        if element.kind == "link" and element.href:
            out.append({"text": element.text or element.href, "href": element.href,
                        "index": element.index})
            if len(out) >= limit:
                break
    return out
