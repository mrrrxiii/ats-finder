from __future__ import annotations

import html
import re
from html.parser import HTMLParser


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "li", "div", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "li", "div", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")


def html_to_text(value: object) -> str:
    if value is None:
        return ""
    decoded = html.unescape(html.unescape(str(value)))
    parser = _TextExtractor()
    parser.feed(decoded)
    parser.close()
    return re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts)).strip()


def require_mapping(payload: object, provider: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError(f"{provider} response must be a JSON object")
    return payload


def require_list(payload: object, provider: str) -> list[object]:
    if not isinstance(payload, list):
        raise ValueError(f"{provider} response must be a JSON array")
    return payload

