"""Conservative HKJC all-results staging; never writes official racing records.

``collect_results(url, *, terms_reviewed=False)`` returns a list of saved
Observation objects, one per recognized race. The caller must independently
authorize the source and review its terms before passing ``True``. Failures
raise CollectionError with a fixed ``code`` and no underlying error text.
This manual one-off has no finish-time gate; do not use it as scheduled polling.
The shared fetcher serializes participating collectors only in this process;
callers must not overlap collector processes.
Only explicitly labelled HTML tables are recognized; JS-only, changed or
ambiguous layouts fail closed. Request parameters identify only the request,
not the page's actual meeting. Explicitly labelled displayed dates/venues are
checked when recognized; missing labels do not establish meeting identity.
Values remain unverified source text. No failure observations are saved.
"""
import re
from html.parser import HTMLParser
from datetime import datetime
from urllib.parse import parse_qsl, urlunsplit

from django.db import transaction
from django.utils import timezone

from .fetch import FetchError, get_shared_fetcher, validate_url
from .services import save_observation


SOURCE = "hkjc-resultsall"
_RACE = re.compile(r"(?:第\s*(1[0-4]|[1-9])\s*場|race\s*(1[0-4]|[1-9]))", re.I)
_BLOCKS = {"div", "p", "span", "h1", "h2", "h3", "h4", "h5", "h6", "strong"}


class CollectionError(Exception):
    """Fixed public error code; no URL, response text or internal exceptions."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _text(value):
    return " ".join(value.split())


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events = []
        self.blocks = []
        self.tables = []
        self.ignored = 0

    def _marker(self, text):
        self.events.append(("text", _text(text)))
        match = _RACE.fullmatch(_text(text))
        if match:
            self.events.append(("race", int(match.group(1) or match.group(2))))

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.ignored += 1
        if self.ignored:
            return
        if tag == "table":
            if self.tables:
                parent = self.tables[-1]
                parent["invalid"] = True
                if not parent["data_table"]:
                    for marker in parent["markers"]:
                        self._marker(marker)
                parent["markers"] = []
                if parent["cell"] is not None:
                    if not parent["data_table"]:
                        self._marker(parent["cell"][0])
                    parent["cell"][0] = ""
                for block in parent["blocks"]:
                    block[1] = ""
            self.tables.append({"rows": [], "row": None, "cell": None, "invalid": False,
                                "blocks": [], "markers": [], "data_table": False})
        elif self.tables:
            table = self.tables[-1]
            if tag == "tr":
                table["row"] = []
            elif tag in {"td", "th"} and table["row"] is not None:
                attrs = dict(attrs)
                try:
                    rowspan = int(attrs.get("rowspan", "1"))
                    colspan = int(attrs.get("colspan", "1"))
                    if not 1 <= rowspan <= 30 or colspan != 1:
                        raise ValueError
                except ValueError:
                    table["invalid"] = True
                    rowspan = 1
                table["cell"] = ["", rowspan]
                table["blocks"] = []
            elif tag == "br" and table["cell"] is not None:
                table["cell"][0] += " "
            elif tag in _BLOCKS and table["cell"] is not None:
                table["blocks"].append([tag, ""])
        elif tag in _BLOCKS:
            self.blocks.append([tag, ""])

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.ignored:
            self.ignored -= 1
            return
        if self.ignored:
            return
        if self.tables:
            table = self.tables[-1]
            if tag in {"td", "th"} and table["cell"] is not None:
                value, rowspan = table["cell"]
                value = _text(value)
                self.events.append(("text", value))
                table["row"].append((value, rowspan))
                table["cell"] = None
                table["blocks"] = []
                if len(table["row"]) > 20:
                    table["invalid"] = True
            elif tag == "tr" and table["row"] is not None:
                field, _ = _extract_table([table["row"]])
                if field:
                    table["data_table"] = True
                if len(table["row"]) == 1 and not table["data_table"]:
                    self._marker(table["row"][0][0])
                table["rows"].append(table["row"])
                table["row"] = None
                table["markers"] = []
                if len(table["rows"]) > 100:
                    table["invalid"] = True
            elif tag == "table":
                self.tables.pop()
                if not table["invalid"]:
                    self.events.append(("table", table["rows"]))
            elif table["blocks"] and tag == table["blocks"][-1][0]:
                _, text = table["blocks"].pop()
                self.events.append(("text", _text(text)))
                if not table["data_table"] and _RACE.fullmatch(_text(text)):
                    table["markers"].append(_text(text))
        elif self.blocks and tag == self.blocks[-1][0]:
            _, text = self.blocks.pop()
            self._marker(text)

    def handle_data(self, data):
        if self.ignored:
            return
        if self.tables and self.tables[-1]["cell"] is not None:
            cell = self.tables[-1]["cell"]
            cell[0] += data
            for block in self.tables[-1]["blocks"]:
                block[1] = (block[1] + data)[:500]
            if len(cell[0]) > 500:
                self.tables[-1]["invalid"] = True
        elif not self.tables:
            for block in self.blocks:
                block[1] = (block[1] + data)[:500]


def _rows(rows):
    """Expand only explicit rowspans, not visually adjacent empty cells."""
    spans = {}
    expanded = []
    for row in rows:
        output = []
        col = 0
        for value, count in row:
            while col in spans:
                text, remaining = spans[col]
                output.append(text)
                if remaining == 1:
                    del spans[col]
                else:
                    spans[col] = (text, remaining - 1)
                col += 1
            output.append(value)
            if count > 1:
                spans[col] = (value, count - 1)
            col += 1
        while col in spans:
            text, remaining = spans[col]
            output.append(text)
            if remaining == 1:
                del spans[col]
            else:
                spans[col] = (text, remaining - 1)
            col += 1
        expanded.append(output)
    return expanded


def _label(value):
    return re.sub(r"[\s.：:()（）]+", "", value).casefold()


def _find(row, labels):
    indexes = [i for i, cell in enumerate(row) if _label(cell) in labels]
    return indexes[0] if len(indexes) == 1 else None


def _extract_table(rows):
    rows = _rows(rows)
    for index, row in enumerate(rows):
        placing = _find(row, {"名次", "placing", "place"})
        horse = _find(row, {"馬號", "horseno", "horsenumber"})
        if placing is not None and horse is not None:
            name = _find(row, {"馬名", "horsename", "horse"})
            records = []
            for item in rows[index + 1:]:
                if max(placing, horse) >= len(item) or not item[placing] or not item[horse]:
                    continue
                entry = {"placing": item[placing], "horse_no": item[horse]}
                if name is not None and name < len(item) and item[name]:
                    entry["horse_name"] = item[name]
                records.append(entry)
            return "results", records
        bet = _find(row, {"彩池", "投注種類", "pool", "bettype", "bet"})
        combo = _find(row, {"勝出組合", "中獎組合", "winningcombination", "combination"})
        dividend_indexes = []
        explicit_bases = {}
        for column, cell in enumerate(row):
            label = _label(cell)
            if label in {"派彩", "派彩hk$", "dividend", "dividendhk$"}:
                dividend_indexes.append(column)
            else:
                match = re.fullmatch(
                    r"(?:派彩每(?:注)?(?:hk)?\$|dividendper(?:hk)?\$)(\d+(?:\.\d+)?)", label
                )
                if match:
                    dividend_indexes.append(column)
                    explicit_bases[column] = match.group(1)
        dividend = dividend_indexes[0] if len(dividend_indexes) == 1 else None
        if bet is not None and combo is not None and dividend is not None:
            base = _find(row, {"投注額", "基數", "base", "stake", "unitstake"})
            records = []
            for item in rows[index + 1:]:
                if max(bet, combo, dividend) >= len(item) or not item[bet]:
                    continue
                entry = {"pool": item[bet], "combination": item[combo], "dividend": item[dividend]}
                if base is not None and base < len(item) and item[base]:
                    entry["base"] = item[base]
                elif base is None and dividend in explicit_bases:
                    entry["base"] = explicit_bases[dividend]
                records.append(entry)
            return "dividends", records
    return None, []


def _review_flags(race):
    flags = []
    placing = [entry["placing"] for entry in race["results"]]
    result_text = " ".join(value for entry in race["results"] for value in entry.values())
    dividend_text = " ".join(value for entry in race["dividends"] for value in entry.values())
    normalized = [value.strip() for value in placing if re.fullmatch(r"\d+", value.strip())]
    if (len(normalized) != len(set(normalized))
            or re.search(r"\b(?:dh|dead\s*heat)\b|同頭|平頭", result_text, re.I)):
        flags.append("possible_dead_heat")
    if re.search(r"退出|退賽|withdraw|scratch", result_text, re.I):
        flags.append("possible_withdrawal")
    if re.search(r"退款|退回|退還|refund", result_text + " " + dividend_text, re.I):
        flags.append("possible_refund")
    return flags


def _extract(html, parameters):
    page = _Page()
    page.feed(html)
    races = {}
    current = None
    displayed_dates = set()
    displayed_venues = set()
    for kind, value in page.events:
        if kind == "text":
            date_match = re.fullmatch(
                r"(?:賽事日期|race\s*date)\s*[:：]\s*(\d{2}/\d{2}/\d{4}|\d{4}/\d{2}/\d{2})",
                value, re.I,
            )
            if date_match:
                date_text = date_match.group(1)
                try:
                    fmt = "%Y/%m/%d" if len(date_text.split("/")[0]) == 4 else "%d/%m/%Y"
                    displayed_dates.add(datetime.strptime(date_text, fmt).strftime("%Y/%m/%d"))
                except ValueError:
                    raise CollectionError("parse_error") from None
            venue_match = re.fullmatch(
                r"(?:馬場|賽馬地點|racecourse|venue)\s*[:：]\s*"
                r"(ST|HV|Sha Tin|Happy Valley|沙田|跑馬地)", value, re.I,
            )
            if venue_match:
                venue = venue_match.group(1).casefold()
                displayed_venues.add("ST" if venue in {"st", "sha tin", "沙田"} else "HV")
        elif kind == "race":
            current = value
            races.setdefault(current, {"race_number": current, "results": [], "dividends": []})
        elif current is not None:
            field, records = _extract_table(value)
            if field and records:
                races[current][field].extend(records)
    if (len(displayed_dates) > 1 or len(displayed_venues) > 1
            or displayed_dates and parameters.get("racedate") not in (None, *displayed_dates)
            or displayed_venues and parameters.get("racecourse") not in (None, *displayed_venues)):
        raise CollectionError("parse_error")
    result = []
    for race in races.values():
        if not race["results"] and not race["dividends"]:
            continue
        race["request_parameters"] = parameters
        race["content_identity_verified"] = False
        race["request_label_comparison"] = {
            "date": ("matched" if displayed_dates and parameters.get("racedate") else "unavailable"),
            "venue": ("matched" if displayed_venues and parameters.get("racecourse") else "unavailable"),
        }
        if displayed_dates:
            race["displayed_date"] = next(iter(displayed_dates))
        if displayed_venues:
            race["displayed_venue"] = next(iter(displayed_venues))
        race["review_required"] = True
        race["extraction_completeness"] = "unknown"
        race["extraction_limitations"] = [
            "Only recognized labelled tables and race markers are extracted; rows or races may be missing.",
            "Request parameters are not verified meeting identity; label comparisons are limited.",
            "Flags are potential indicators only; absence of a flag does not exclude an issue.",
        ]
        race["review_flags"] = _review_flags(race)
        result.append(race)
    return result


def collect_results(url, *, terms_reviewed=False):
    """Return saved unverified Observations or raise a sanitized CollectionError."""
    if terms_reviewed is not True:
        raise CollectionError("not_authorized")
    try:
        parts = validate_url(url, SOURCE)
    except Exception:
        raise CollectionError("invalid_url") from None
    params = {key.lower(): value for key, value in parse_qsl(parts.query)}
    safe_url = urlunsplit(("https", parts.hostname, parts.path, "", ""))
    try:
        html = get_shared_fetcher(SOURCE).fetch(SOURCE, url)
    except FetchError as exc:
        from .management.commands.collect_sources import _failure_code
        raise CollectionError(_failure_code(exc)) from None
    except Exception:
        raise CollectionError("collection_error") from None
    try:
        payloads = _extract(html, params)
    except Exception:
        raise CollectionError("parse_error") from None
    try:
        if not payloads:
            raise CollectionError("parse_error")
        with transaction.atomic():
            now = timezone.now()
            return [save_observation(safe_url, SOURCE, payload, now)[0] for payload in payloads]
    except CollectionError:
        raise
    except Exception:
        raise CollectionError("collection_error") from None
