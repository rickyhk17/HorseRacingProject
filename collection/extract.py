"""Extract small, unverified observations without inferring meanings from page layout."""
from html.parser import HTMLParser
import json
from urllib.parse import urljoin, urlsplit, urlunsplit


def _short(text, limit):
    return " ".join(text.split())[:limit]


class _Page(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.description = ""
        self.in_title = False
        self.ignored = 0
        self.depth = 0
        self.tables = []
        self.table = None
        self.row = None
        self.cell = None
        self.heading = 0
        self.link = None
        self.headlines = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self.ignored += 1
        if self.ignored:
            return
        if tag == "title":
            self.in_title = True
        elif tag in {"h1", "h2", "h3"}:
            self.heading += 1
        elif tag == "a" and self.heading and len(self.headlines) < 10:
            self.link = [attrs.get("href", ""), ""]
        elif tag == "meta" and (attrs.get("name", "").lower() == "description"
                                or attrs.get("property", "").lower() == "og:description"):
            self.description = self.description or _short(attrs.get("content", ""), 180)
        elif tag == "table":
            self.depth += 1
            if self.depth == 1:
                self.table = []
        elif self.depth == 1 and tag == "tr":
            self.row = []
        elif self.depth == 1 and tag in {"th", "td"} and self.row is not None:
            self.cell = ""

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.ignored:
            self.ignored -= 1
            return
        if self.ignored:
            return
        if tag == "title":
            self.in_title = False
        elif self.depth == 1 and tag in {"th", "td"} and self.cell is not None:
            if len(self.row) < 20:
                self.row.append(_short(self.cell, 60))
            self.cell = None
        elif self.depth == 1 and tag == "tr" and self.row is not None:
            if len(self.table) < 30 and len(self.row) >= 2:
                self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.depth:
            if self.depth == 1 and self.table and len(self.table) >= 2:
                self.tables.append(self.table)
            self.depth -= 1
        elif tag == "a" and self.link is not None:
            href, text = self.link
            try:
                target = urlsplit(urljoin(self.base_url, href))
                base = urlsplit(self.base_url)
                if (target.scheme == "https" and target.hostname == base.hostname
                        and target.port is None and target.username is None and target.password is None
                        and _short(text, 120)):
                    url = urlunsplit(("https", target.hostname, target.path, "", ""))
                    self.headlines.append({"title": _short(text, 120), "url": url[:1000]})
            except ValueError:
                pass
            self.link = None
        elif tag in {"h1", "h2", "h3"}:
            self.heading = max(0, self.heading - 1)

    def handle_data(self, data):
        if self.ignored:
            return
        if self.in_title:
            self.title = _short(self.title + " " + data, 160)
        if self.cell is not None:
            self.cell = (self.cell + " " + data)[:500]
        if self.link is not None:
            self.link[1] = (self.link[1] + " " + data)[:250]


def extract_page(html, *, base_url="", tables=False):
    parser = _Page(base_url)
    parser.feed(html)
    result = {"title": parser.title, "description": parser.description}
    if tables:
        # These are page cells, not mapped racing facts or verified results.
        keywords = ("三重彩", "單t", "tierce", "trio", "名次", "馬號", "馬名",
                    "placing", "horse no", "horse name", "dividend", "派彩")
        def priority(table):
            text = " ".join(cell for row in table for cell in row).casefold()
            if any(word in text for word in ("三重彩", "單t", "tierce", "trio")):
                return (2, len(table))
            return (int(any(word in text for word in keywords)), len(table))
        result["tables"] = sorted(parser.tables, key=priority, reverse=True)[:5]
        result["tables_truncated"] = True
        while len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 47000:
            if result["tables"][-1]:
                result["tables"][-1].pop()
            else:
                result["tables"].pop()
    else:
        result["headlines"] = parser.headlines
    return result
