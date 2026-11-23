#!/usr/bin/env python3
"""Watch Dhaka university career pages and send new circulars to Telegram.

The bot identifies itself honestly, keeps TLS verification on, obeys robots.txt,
and reports blocked sites instead of trying to get around them.

  python watcher.py                 normal run
  python watcher.py --dry-run       print alerts, send nothing
  python watcher.py --test-message  send one Telegram test message
  python watcher.py --find-chat-id  show chat ids that messaged the bot
"""
from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import io
import json
import logging
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
SOURCES_FILE = BASE / "sources.final.json"
STATE_FILE = BASE / "state" / "state.json"

USER_AGENT = "UniversityCircularWatcher/1.0 (personal job-alert bot; read-only; hourly)"
DHAKA = timezone(timedelta(hours=6))
DIGEST_HOUR = 9
MANUAL_WEEKDAY = 6  # Sunday
REMINDER_DAYS = (7, 3, 1, 0)
FAILS_BEFORE_ALERT = 6
FLOOD_LIMIT = 15
STALE_DAYS = 45
PRUNE_DAYS = 180
MAX_PAGE_BYTES = 5_000_000
MAX_PDF_BYTES = 8_000_000
HOST_DELAY = 1.0
NO_LINK_STATUSES = {"Suspicious site", "Site down"}
NO_LINK_NAMES = {"IBAIS University"}

log = logging.getLogger("watcher")


# ---------------------------------------------------------------- text and dates

def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value or "")


BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


def clean(value: str) -> str:
    return " ".join(nfc(value).translate(BN_DIGITS).split())


def rx(pattern: str) -> re.Pattern:
    return re.compile(nfc(pattern), re.I)


BN_MONTHS = {nfc(k): v for k, v in {
    "জানুয়ারি": 1, "জানুয়ারী": 1, "ফেব্রুয়ারি": 2, "ফেব্রুয়ারী": 2, "মার্চ": 3, "এপ্রিল": 4,
    "মে": 5, "জুন": 6, "জুলাই": 7, "আগস্ট": 8, "সেপ্টেম্বর": 9, "অক্টোবর": 10, "নভেম্বর": 11, "ডিসেম্বর": 12,
}.items()}
EN_MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
MONTH = ("jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
         "sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
         + "|".join(sorted(BN_MONTHS, key=len, reverse=True)))
DATE_PATTERNS = [
    ("ymd", rx(r"(?<!\d)(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")),
    ("dmy", rx(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2}|\d{2})(?!\d)")),
    ("dMy", rx(rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th|ই|শে|লা|রা)?[\s\-]*(?:of\s+)?({MONTH})[\s,.\-]*(20\d{{2}})(?!\d)")),
    ("Mdy", rx(rf"(?<![a-z])({MONTH})[\s.]*(\d{{1,2}})(?:st|nd|rd|th)?[\s,]+(20\d{{2}})(?!\d)")),
]
DEADLINE_WORDS = rx(
    r"dead\s*-?\s*line|last\s+date|closing\s+date|apply\s+by|on\s+or\s+before|submission\s+date|"
    r"application\s+(?:end|close)s?|expires?\s+on|শেষ\s*তারিখ|আবেদনের\s*শেষ|সর্বশেষ\s*তারিখ|আবেদন\s*গ্রহণের\s*শেষ")
BN_DEADLINE_AFTER = rx(r"^\D{0,6}(তারিখের\s*মধ্যে|তারিখ\s*পর্যন্ত|এর\s*মধ্যে)")


def month_number(token: str) -> int:
    token = nfc(token)
    return BN_MONTHS.get(token) or EN_MONTHS[token[:3].lower()]


def make_date(year: int, month: int, day: int) -> date | None:
    if year < 100:
        year += 2000
    if month > 12 and day <= 12:
        month, day = day, month
    try:
        return date(year, month, day)
    except ValueError:
        return None


def find_dates(text: str) -> list[tuple[int, date]]:
    text = clean(text)
    found: list[tuple[int, int, date]] = []
    for kind, pattern in DATE_PATTERNS:
        for m in pattern.finditer(text):
            if any(m.start() < end and start < m.end() for start, end, _ in found):
                continue
            a, b, c = m.groups()
            if kind == "ymd":
                value = make_date(int(a), int(b), int(c))
            elif kind == "dmy":
                value = make_date(int(c), int(b), int(a))
            elif kind == "dMy":
                value = make_date(int(c), month_number(b), int(a))
            else:
                value = make_date(int(c), month_number(a), int(b))
            if value:
                found.append((m.start(), m.end(), value))
    return [(start, value) for start, _, value in sorted(found, key=lambda x: x[0])]


def find_deadline(text: str) -> date | None:
    text = clean(text)
    dates = find_dates(text)
    if not dates:
        return None
    for word in DEADLINE_WORDS.finditer(text):
        for start, value in dates:
            if word.end() <= start <= word.end() + 70:
                return value
    for pattern_kind, pattern in DATE_PATTERNS:
        for m in pattern.finditer(text):
            if BN_DEADLINE_AFTER.search(text[m.end():m.end() + 30]):
                for start, value in dates:
                    if start == m.start():
                        return value
    return None


# ---------------------------------------------------------------- classification

RESEARCH = rx(r"research\s*(assistant|associate|officer|fellow)|গবেষণা\s*(সহকারী|কর্মকর্তা|সহযোগী)")
TEACHING = rx(r"lecturer|faculty|professor|teacher|teaching\s*assistant|adjunct|demonstrator|"
              r"প্রভাষক|শিক্ষক|অধ্যাপক|ডেমোনস্ট্রেটর")
ADMIN = rx(r"officer|registrar|administrat|\badmin\b|coordinator|manager|director|controller|librarian|executive|"
           r"কর্মকর্তা|রেজিস্ট্রার|পরিচালক|গ্রন্থাগারিক|প্রশাসন|নিয়ন্ত্রক")
HIRE = rx(r"recruit|vacanc|job\s*(circular|opportunit|opening|notice|advert)|career\s*opportunit|"
          r"faculty\s*(search|position|recruit)|open\s*(rank|position)|positions?\s*(available|open)|"
          r"applications?\s*(are|is)\s*invited|hiring|employment\s*(opportunit|circular|notice)|"
          r"নিয়োগ|পদে\s*আবেদন|শূন্য\s*পদ")
NOISE = rx(r"career\s*(fair|talk|counsel|guidance|develop|service|festival|launch|session|accelerator)|job\s*fair|"
           r"placement|internship|admission|tender|e-?gp|quotation|exam\s*(routine|schedule)|mid-?term|final\s*exam|"
           r"routine|convocation|holiday|workshop|seminar|webinar|conference|training|orientation|scholarship|"
           r"alumni|\bmou\b|club|faculty\s+(of|members?|profile|list|development|directory)|our\s+faculty|"
           r"ভর্তি|দরপত্র|পরীক্ষার\s*(সময়সূচী|রুটিন|ফল)|ছুটি|সেমিনার|কর্মশালা|সমাবর্তন|বৃত্তি")
AFTER_CIRCULAR = rx(r"\bresults?\b|\bviva\b|admit\s*card|written\s*(test|exam)|\binterview\b|practical\s*exam|"
                    r"\bselected\b|merit\s*list|short-?list|\bexpired\b|"
                    r"ফলাফল|মৌখিক|প্রবেশপত্র|সাক্ষাৎকার|লিখিত\s*পরীক্ষা")
LOW_RANK = rx(r"electrician|driver|peon|guard|cleaner|\bmlss\b|helper|gardener|\bcook\b|plumber|"
              r"upper\s*division|lower\s*division|office\s*assistant|computer\s*operator|data\s*entry|"
              r"ইলেকট্রিশিয়ান|ইলেক্ট্রিশিয়ান|ড্রাইভার|গাড়ি\s*চালক|গাড়িচালক|প্রহরী|পিয়ন|ঝাড়ুদার|মালী|"
              r"উচ্চমান\s*সহকারী|নিম্নমান\s*সহকারী|অফিস\s*সহায়ক|অফিস\s*সহকারী|কম্পিউটার\s*অপারেটর")
OFFICE_NAME = rx(r"office\s+of(\s+the)?\s+[^,\-–(]+")
NO_VACANCY = rx(r"no\s+(job\s+)?vacanc|no\s+open\s+positions?|no\s+current\s+(openings?|vacanc)|"
                r"building\s+this\s+page|কোন\s+নিয়োগ\s+নেই")
SCHOOL = rx(r"school\s*(and|&)\s*college|স্কুল\s*ও\s*কলেজ")
NAV_ONLY = rx(r"^(faculty|faculties|officers?|registrar|administration|lecturers?|teachers?|career|careers|jobs?|"
              r"notice|notices|directors?|executive|management|শিক্ষক|কর্মকর্তা|নিয়োগ)(\s+(list|page))?$")
CAREER_URL = rx(r"career|job|vacanc|recruit|circular|employment")
TABLE_HEADER = rx(r"^(available\s+)?job\s+listings?\b")
PAGE_COPY = rx(r"^(explore|browse|discover|find|join|we\s|our\s|please|learn|build|submit|select|apply\s+now)\b")


def role_of(text: str) -> str | None:
    if RESEARCH.search(text):
        return "Research"
    if TEACHING.search(text):
        return "Teaching"
    if ADMIN.search(text):
        return "Officer/Admin"
    return None


def is_career_page(url: str) -> bool:
    parts = urlparse(url)
    return bool(CAREER_URL.search(f"{parts.path}?{parts.query}") or CAREER_URL.search(parts.hostname or ""))


def classify(title: str, context: str, page_url: str) -> tuple[str, str] | None:
    """Return (level, category). `strong` alerts now; `possible` waits for the daily digest."""
    title, context = clean(title), clean(context)
    has_deadline = bool(find_deadline(context))
    # A bare "Lecturer" is a menu item unless it carries a deadline.
    if len(title) < 6 or "@" in title or (NAV_ONLY.match(title) and not has_deadline) \
            or AFTER_CIRCULAR.search(title) or TABLE_HEADER.search(title):
        return None
    title_role = role_of(OFFICE_NAME.sub(" ", title))  # "Office of the Registrar" is a place, not the post
    title_hire = bool(HIRE.search(title))
    if NOISE.search(title) and not title_hire and not (title_role and has_deadline):
        return None
    if LOW_RANK.search(title) and not title_role:
        return None
    if SCHOOL.search(f"{title} {context}") and (title_role or title_hire):
        return "possible", "School/College"
    role = title_role or (role_of(context) if has_deadline else None)
    career_page = is_career_page(page_url)
    if title_hire:
        return "strong", role or "General circular"
    if role and has_deadline:
        return "strong", role
    if title_role and career_page:
        return "strong", title_role
    if title_role and len(title.split()) >= 3:
        return "possible", title_role
    if has_deadline and career_page:
        return "possible", "General circular"
    return None


# ---------------------------------------------------------------- HTML extraction

GENERIC_LINK = rx(r"^(view job advertisement|job advertisement|view circular|view|read|see|click|more|details?|"
                  r"detail information|view details?|read more|learn more|"
                  r"download|apply|apply now|apply link|open|pdf|here|click here|see openings?|বিস্তারিত|ডাউনলোড|আবেদন করুন)"
                  r"(\s+(more|details?|here|now|pdf|file|notice))*\W*$")
META_LINE = rx(r"^(posting time|posted|published|publish date|application deadline|deadline|dead line|last date|"
               r"apply|position|permanent|contractual|প্রকাশের তারিখ|শেষ তারিখ)\b")
CIRCULAR_LINK = rx(r"job\s*circular|recruitment\s*(circular|notice)|vacancy\s*(circular|notice)|job\s*advert|"
                   r"নিয়োগ\s*বিজ্ঞপ্তি")
DOCUMENT_URL = re.compile(r"\.(pdf|docx?)$", re.I)
TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|gsessionid|phpsessid|sessionid|sid|from)$", re.I)


@dataclass
class Item:
    university: str
    title: str
    url: str
    page_url: str
    context: str
    deadline: date | None = None
    dates: list[date] = field(default_factory=list)
    named: bool = False


def canonical_url(url: str) -> str:
    """Comparison key only: http/https and www variants count as the same page."""
    parts = urlparse((url or "").strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.match(k)])
    path = parts.path.rstrip("/") or "/"
    host = parts.netloc.lower().removeprefix("www.")
    return urlunparse(("https", host, path, "", query, ""))


def tidy_title(title: str) -> str:
    title = re.sub(r"\[[^\]]*\d{4}[^\]]*\]", " ", clean(title))
    title = re.split(r"\s+(?:published on|posted on|application deadline|dead\s*line\s*:|deadline\s*:)", title,
                     maxsplit=1, flags=re.I)[0]
    return clean(title).strip(" -|:")[:180]


def item_container(anchor):
    node = anchor
    for parent in anchor.parents:
        if parent.name in ("body", "html", "[document]", "main"):
            break
        hrefs = {a.get("href") for a in parent.find_all("a", href=True)}
        if len(parent.get_text(" ", strip=True)) > 600 or len(hrefs) > 3:
            break
        node = parent
        if parent.name in ("tr", "li", "article"):
            break
    return node


def meaningful_lines(node) -> list[str]:
    lines = []
    for raw in node.get_text("\n").split("\n"):
        line = clean(raw)
        if len(line) < 3 or GENERIC_LINK.match(line) or META_LINE.match(line):
            continue
        without_dates = line
        for pattern in (p for _, p in DATE_PATTERNS):
            without_dates = pattern.sub("", without_dates)
        if len(re.sub(r"[\W\d_]", "", without_dates)) < 3:
            continue
        lines.append(line)
    return lines


def anchor_rank(entry: tuple[str, str]) -> int:
    text, url = entry
    if ".pdf" in url.lower():
        return 0
    if re.search(r"detail|view|read|download|বিস্তারিত", text, re.I):
        return 1
    if not GENERIC_LINK.match(text):
        return 2
    return 3


def make_item(university: str, node, page_url: str, anchors: list[tuple[str, str]]) -> Item | None:
    lines = meaningful_lines(node)
    named = [text for text, _ in anchors if len(text) >= 6 and not GENERIC_LINK.match(text)]
    title = tidy_title(named[0] if named else " - ".join(lines[:2]))
    context = clean(node.get_text(" "))[:700]
    url = sorted(anchors, key=anchor_rank)[0][1] if anchors else page_url
    if not title and anchors and find_deadline(context):
        heading = node.find_previous(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"])
        title = tidy_title(heading.get_text(" ")) if heading else ""
        title = title or "Job advertisement"
    if not title:
        return None
    if NAV_ONLY.match(title) and url != page_url:
        # Several posts can share the title "Lecturer"; the link slug tells them apart.
        slug = re.sub(r"\.\w+$", "", urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
        slug = re.sub(r"[-_]+", " ", slug).strip()
        if re.search(r"[A-Za-z]{3}", slug) and slug.lower() != title.lower():
            title = f"{title} ({slug[:60]})"
    return Item(university, title, url, page_url, context, find_deadline(context),
                [value for _, value in find_dates(context)], bool(named))


def merge_same_link(items: list[Item], page_url: str) -> list[Item]:
    """A heading and its 'View details' button often sit in separate blocks but are one circular."""
    page_key = canonical_url(page_url)
    merged: dict[str, Item] = {}
    output: list[Item] = []
    for item in items:
        key = canonical_url(item.url)
        if key == page_key:
            output.append(item)
            continue
        current = merged.get(key)
        if current is None:
            merged[key] = item
            output.append(item)
            continue
        if item.named and not current.named:
            current.title, current.named = item.title, True
        if item.context not in current.context:
            current.context = clean(f"{current.context} {item.context}")[:1000]
        current.deadline = current.deadline or item.deadline
        current.dates = sorted(set(current.dates + item.dates))
    return output


def extract_items(university: str, page_html: str, page_url: str) -> list[Item]:
    soup = BeautifulSoup(page_html, "html.parser")
    menu_circulars = menu_circular_items(university, soup, page_url)
    for tag in soup(["script", "style", "noscript", "template", "svg", "nav", "footer", "aside", "header",
                     "select", "option"]):
        if not tag.decomposed:
            tag.decompose()
    groups: dict[int, tuple[object, list[tuple[str, str]]]] = {}
    page_key = canonical_url(page_url)
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(page_url, href).split("#")[0]
        if urlparse(url).scheme not in ("http", "https") or canonical_url(url) == page_key:
            continue
        node = item_container(anchor)
        groups.setdefault(id(node), (node, []))[1].append((clean(anchor.get_text(" ")), url))
    items = [i for node, links in groups.values() if (i := make_item(university, node, page_url, links))]

    def walk(element):
        for child in element.find_all(recursive=False):
            if child.name == "a" or child.find("a", href=True):
                if child.name != "a":
                    walk(child)
                continue
            text = clean(child.get_text(" "))
            if 12 <= len(text) <= 350 and find_dates(text) and (role_of(text) or HIRE.search(text)):
                made = make_item(university, child, page_url, [])
                # A linkless bare "Teacher" block is metadata of the linked heading next to it.
                if made and not NAV_ONLY.match(made.title):
                    items.append(made)
                continue
            walk(child)

    walk(soup.body or soup)
    return merge_same_link(items + menu_circulars, page_url)


def menu_circular_items(university: str, soup, page_url: str) -> list[Item]:
    """Some portals keep the actual circular PDF in the top menu, e.g. 'Download Job Circular'."""
    found = []
    for zone in soup(["nav", "header", "footer", "aside"]):
        for anchor in zone.find_all("a", href=True):
            text = clean(anchor.get_text(" "))
            url = urljoin(page_url, anchor["href"].strip()).split("#")[0]
            if not CIRCULAR_LINK.search(text) or not DOCUMENT_URL.search(urlparse(url).path):
                continue
            filename = unquote(urlparse(url).path.rsplit("/", 1)[-1])
            filename = re.sub(r"(\d{1,2})[_ ](\d{1,2})[_ ](20\d{2})", r"\1-\2-\3", filename)
            dates = [value for _, value in find_dates(filename)]
            title = f"Job Circular ({dates[-1].strftime('%d %b %Y')})" if dates else "Job Circular"
            found.append(Item(university, title, url, page_url, f"{text} {filename}", find_deadline(text), dates, True))
    return found


def page_lines(page_html: str) -> list[str]:
    soup = BeautifulSoup(page_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg", "nav", "footer", "aside", "header",
                     "select", "option"]):
        if not tag.decomposed:
            tag.decompose()
    return [line for raw in soup.get_text("\n").split("\n") if (line := clean(raw))]


def job_lines(lines: list[str]) -> list[str]:
    """Safety net for career pages whose layout the item parser does not understand."""
    found = []
    for line in lines:
        if not 8 <= len(line) <= 140 or "@" in line or NAV_ONLY.match(line):
            continue
        if len(line.split()) > 12 or PAGE_COPY.match(line):  # page copy, not a post title
            continue
        if NOISE.search(line) or AFTER_CIRCULAR.search(line) or NO_VACANCY.search(line):
            continue
        if (role_of(OFFICE_NAME.sub(" ", line)) or HIRE.search(line)) and line not in found:
            found.append(line)
    return found


def fingerprint(record: dict) -> str | None:
    if not record.get("deadline"):
        return None
    title = re.sub(r"\W+", " ", clean(record["title"]).lower()).strip()
    if len(title.split()) < 4:  # short titles like "Faculty Search" can be different circulars
        return None
    return f"{record['university']}|{title}|{record['deadline']}"


def item_key(item: Item) -> str:
    title = re.sub(r"\W+", " ", clean(item.title).lower()).strip()
    url = canonical_url(item.url)
    parts = [item.university, title, url]
    if url == canonical_url(item.page_url):
        parts.append(item.deadline.isoformat() if item.deadline else "")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:20]


# ---------------------------------------------------------------- fetching

CHALLENGE = rx(r"request is being verified|checking your browser|just a moment|cf-chl|verify you are human|captcha")


@dataclass
class FetchResult:
    url: str
    ok: bool
    state: str
    html: str = ""
    final_url: str = ""
    detail: str = ""


def has_circulars(source: dict, result: FetchResult) -> bool:
    try:
        page_url = result.final_url or source["url"]
        return any(classify(i.title, i.context, page_url)
                   for i in extract_items(source["university"], result.html, page_url))
    except Exception:
        return False


class Fetcher:
    def __init__(self):
        self.robots: dict[str, RobotFileParser | None] = {}

    def allowed(self, session: requests.Session, url: str) -> bool:
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            parser = None
            try:
                response = session.get(origin + "/robots.txt", timeout=(8, 12))
                if response.status_code == 200 and "html" not in response.headers.get("Content-Type", "").lower():
                    parser = RobotFileParser()
                    parser.parse(response.text.splitlines())
            except requests.RequestException:
                parser = None
            self.robots[origin] = parser
        parser = self.robots[origin]
        return parser.can_fetch(USER_AGENT, url) if parser else True

    @staticmethod
    def read(response: requests.Response, limit: int) -> bytes:
        chunks, total = [], 0
        for chunk in response.iter_content(65536):
            chunks.append(chunk)
            total += len(chunk)
            if total >= limit:
                break
        return b"".join(chunks)

    def http(self, session: requests.Session, url: str) -> FetchResult:
        if not self.allowed(session, url):
            return FetchResult(url, False, "robots_disallowed")
        last = FetchResult(url, False, "conn_error")
        for attempt in range(3):
            try:
                with session.get(url, timeout=(10, 30), stream=True) as response:
                    if response.status_code in (401, 403, 429):
                        return FetchResult(url, False, "blocked", detail=f"HTTP {response.status_code}")
                    if response.status_code in (404, 410):
                        return FetchResult(url, False, "not_found", detail=f"HTTP {response.status_code}")
                    if response.status_code >= 500:
                        last = FetchResult(url, False, "server_error", detail=f"HTTP {response.status_code}")
                    else:
                        raw = self.read(response, MAX_PAGE_BYTES)
                        text = raw.decode(response.encoding or "utf-8", "replace") if response.encoding and \
                            response.encoding.lower() != "iso-8859-1" else raw.decode("utf-8", "replace")
                        if CHALLENGE.search(text[:20000]):
                            return FetchResult(url, False, "bot_check")
                        return FetchResult(url, True, "ok", text, response.url)
            except requests.exceptions.SSLError:
                return FetchResult(url, False, "cert_error")
            except requests.exceptions.Timeout:
                last = FetchResult(url, False, "timeout")
            except requests.RequestException as error:
                last = FetchResult(url, False, "conn_error", detail=type(error).__name__)
            time.sleep(2 * (attempt + 1))
        return last

    def fetch_host(self, urls: list[str]) -> list[FetchResult]:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*"})
        results = []
        for url in urls:
            results.append(self.http(session, url))
            time.sleep(HOST_DELAY)
        return results

    def browser(self, urls: list[str]) -> list[FetchResult]:
        if not urls:
            return []
        try:
            from playwright.sync_api import Error as PlaywrightError, sync_playwright
        except ImportError:
            return [FetchResult(u, False, "browser_missing") for u in urls]
        results = []
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        try:
            with sync_playwright() as p:
                instance = p.chromium.launch()
                # GitHub runs in UTC; JavaScript sites would otherwise show deadlines one day early.
                context = instance.new_context(user_agent=USER_AGENT, timezone_id="Asia/Dhaka")
                for url in urls:
                    if not self.allowed(session, url):
                        results.append(FetchResult(url, False, "robots_disallowed"))
                        continue
                    page = context.new_page()
                    try:
                        response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        try:
                            page.wait_for_load_state("networkidle", timeout=15000)
                        except PlaywrightError:
                            pass
                        page.wait_for_timeout(1500)
                        content = page.content()
                        status = response.status if response else 0
                        if status in (401, 403, 429):
                            results.append(FetchResult(url, False, "blocked", detail=f"HTTP {status}"))
                        elif status >= 400:
                            results.append(FetchResult(url, False, "server_error", detail=f"HTTP {status}"))
                        elif CHALLENGE.search(content[:20000]):
                            results.append(FetchResult(url, False, "bot_check"))
                        else:
                            results.append(FetchResult(url, True, "ok", content, page.url))
                    except PlaywrightError as error:
                        results.append(FetchResult(url, False, "browser_error", detail=str(error)[:100]))
                    finally:
                        page.close()
                instance.close()
        except PlaywrightError as error:
            done = {r.url for r in results}
            results += [FetchResult(u, False, "browser_error", detail=str(error)[:100]) for u in urls if u not in done]
        return results

    def __call__(self, sources: list[dict]) -> dict[str, FetchResult]:
        browser_urls = [s["url"] for s in sources if s.get("browser")]
        by_host: dict[str, list[str]] = defaultdict(list)
        for source in sources:
            if not source.get("browser"):
                by_host[urlparse(source["url"]).netloc].append(source["url"])
        results: dict[str, FetchResult] = {}
        with ThreadPoolExecutor(8) as pool:
            for group in pool.map(self.fetch_host, by_host.values()):
                results.update({r.url: r for r in group})
        results.update({r.url: r for r in self.browser(browser_urls)})
        # Pages with no recognised circular may be JavaScript-rendered, so render them once.
        retry = []
        for source in sources:
            result = results.get(source["url"])
            if not source.get("browser") and result and result.ok and not has_circulars(source, result):
                retry.append(source["url"])
        for rendered in self.browser(retry):
            if rendered.ok:
                results[rendered.url] = rendered
        return results

    def pdf_deadline(self, url: str) -> date | None:
        """Best effort: text PDFs only. Scanned PDFs return None."""
        try:
            from pypdf import PdfReader
            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
            if not self.allowed(session, url):
                return None
            with session.get(url, timeout=(10, 40), stream=True) as response:
                if response.status_code != 200:
                    return None
                raw = self.read(response, MAX_PDF_BYTES + 1)
            if len(raw) > MAX_PDF_BYTES:
                return None
            reader = PdfReader(io.BytesIO(raw))
            text = " ".join((page.extract_text() or "") for page in reader.pages[:3])
            return find_deadline(text)
        except Exception as error:  # malformed PDFs must never stop the run
            log.info("PDF skipped: %s (%s)", url, type(error).__name__)
            return None


# ---------------------------------------------------------------- Telegram

class TelegramError(RuntimeError):
    pass


def split_message(text: str, limit: int = 3900) -> list[str]:
    parts, current = [], ""
    for line in text.split("\n"):
        line = line[:limit]
        if current and len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id

    def send(self, text: str) -> None:
        for part in split_message(text):
            self._send_part(part)
            time.sleep(1.05)

    def _send_part(self, text: str) -> None:
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        for _ in range(4):
            try:
                response = requests.post(self.url, json=payload, timeout=20)
            except requests.RequestException as error:
                # The request URL contains the token, so never log the original message.
                last = f"network error ({type(error).__name__})"
                time.sleep(3)
                continue
            if response.status_code == 200:
                return
            if response.status_code == 429:
                wait = response.json().get("parameters", {}).get("retry_after", 5)
                last = "rate limited"
                time.sleep(min(int(wait) + 1, 60))
                continue
            try:
                description = response.json().get("description", "")
            except ValueError:
                description = ""
            raise TelegramError(f"Telegram HTTP {response.status_code}: {description}")
        raise TelegramError(f"Telegram send failed: {last}")


class DryRun:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(text)
        plain = re.sub(r"<[^>]+>", "", text)
        sys.stdout.buffer.write((html_lib.unescape(plain) + "\n" + "-" * 50 + "\n").encode("utf-8", "replace"))
        sys.stdout.flush()


# ---------------------------------------------------------------- messages

def esc(value: str) -> str:
    return html_lib.escape(value or "", quote=True)


def nice_date(value: str | None) -> str:
    return date.fromisoformat(value).strftime("%d %b %Y") if value else "Not found, circular dekhun"


def days_text(days: int) -> str:
    return "aj shesh din" if days == 0 else f"{days} din baki"


def deadline_line(record: dict, today: date) -> str:
    if not record.get("deadline"):
        return "Deadline: Not found, circular dekhun"
    days = (date.fromisoformat(record["deadline"]) - today).days
    suffix = f" ({days_text(days)})" if days >= 0 else ""
    return f"Deadline: <b>{nice_date(record['deadline'])}</b>{suffix}"


def alert_message(record: dict, today: date, heading: str = "New circular") -> str:
    return (f"<b>{heading}: {esc(record['university'])}</b>\n"
            f"{esc(record['title'])}\n"
            f"Type: {esc(record['category'])}\n"
            f"{deadline_line(record, today)}\n"
            f'<a href="{esc(record["url"])}">Circular khulun</a>')


def short_line(record: dict, today: date) -> str:
    deadline = ""
    if record.get("deadline"):
        days = (date.fromisoformat(record["deadline"]) - today).days
        deadline = f" | {nice_date(record['deadline'])} ({days_text(days)})" if days >= 0 else ""
    return f'- <b>{esc(record["university"])}</b>: <a href="{esc(record["url"])}">{esc(record["title"][:110])}</a>{deadline}'


# ---------------------------------------------------------------- core run

def new_state() -> dict:
    return {"version": 1, "last_run": None, "last_digest": None, "last_manual": None, "sources": {}, "items": {}}


def load_state(path: Path) -> dict:
    if not path.exists():
        return new_state()
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=0, sort_keys=True)
    os.replace(temp, path)


def list_sources(config: dict) -> list[dict]:
    sources, seen = [], set()
    for university in config["universities"]:
        browser_urls = set(university.get("browser_urls", []))
        for url in university.get("monitor_urls", []):
            if url not in seen:
                seen.add(url)
                sources.append({"university": university["name"], "url": url, "browser": url in browser_urls})
    for backup in config.get("backup_sources", []):
        for url in backup.get("monitor_urls", []):
            if url not in seen:
                seen.add(url)
                sources.append({"university": backup["name"], "url": url, "browser": bool(backup.get("browser")),
                                "backup": True})
    return sources


def is_stale(item: Item, today: date) -> bool:
    if item.deadline:
        return item.deadline < today
    return bool(item.dates) and max(item.dates) < today - timedelta(days=STALE_DAYS)


def valid_deadline(item: Item, today: date) -> str | None:
    if item.deadline and item.deadline <= today + timedelta(days=365):
        return item.deadline.isoformat()
    return None


def record_of(item: Item, level: str, category: str, today: date, notified: bool) -> dict:
    return {"university": item.university, "title": item.title, "url": item.url, "page": item.page_url,
            "level": level, "category": category, "deadline": valid_deadline(item, today),
            "first_seen": today.isoformat(), "notified": notified, "digested": level != "possible" or notified,
            "reminded": []}


def backup_match(item: Item, names: list[str]) -> str | None:
    text = clean(f"{item.title} {item.context}").lower()
    return next((name for name in names if name.lower() in text), None)


def process_source(source: dict, result: FetchResult, state: dict, today: date, names: list[str],
                   open_now: list[str], floods: list[str]) -> None:
    info = state["sources"].setdefault(source["url"], {"fails": 0, "count": 0, "zero_runs": 0, "alerted": False,
                                                       "baselined": False, "university": source["university"]})
    info["state"] = result.state
    if not result.ok:
        info["fails"] += 1
        return
    info["fails"] = 0
    info["last_ok"] = today.isoformat()
    page_url = result.final_url or source["url"]
    accepted: dict[str, tuple[Item, str, str]] = {}
    for item in extract_items(source["university"], result.html, page_url):
        if source.get("backup"):
            matched = backup_match(item, names)
            if not matched:
                continue
            item.university = matched
        verdict = classify(item.title, item.context, page_url)
        if verdict:
            accepted[item_key(item)] = (item, *verdict)

    if is_career_page(page_url) and not source.get("backup"):
        lines = page_lines(result.html)
        covered = " ".join(i.context.lower() + " " + i.title.lower() for i, _, _ in accepted.values())
        for line in job_lines(lines):
            if line.lower() in covered:
                continue
            item = Item(source["university"], tidy_title(line) or line[:180], page_url, page_url, line,
                        find_deadline(line), [value for _, value in find_dates(line)])
            accepted[item_key(item)] = (item, "strong", "Career page update")
        says_empty = any(NO_VACANCY.search(line) for line in lines)
        if info.get("no_vacancy") and not says_empty and info["baselined"]:
            title = f"Career page-er 'no vacancy' lekha sore geche ({today.strftime('%d %b %Y')})"
            item = Item(source["university"], title, page_url, page_url, title)
            accepted[item_key(item)] = (item, "strong", "Career page update")
        info["no_vacancy"] = says_empty

    info["zero_runs"] = info.get("zero_runs", 0) + 1 if info.get("count", 0) >= 3 and not accepted else 0
    if accepted or info["zero_runs"] >= FAILS_BEFORE_ALERT:
        info["count"] = len(accepted)

    new = {k: v for k, v in accepted.items() if k not in state["items"]}
    for key, (item, _, _) in accepted.items():
        record = state["items"].get(key)
        fresh = valid_deadline(item, today)
        if record and fresh and record.get("deadline") and fresh > record["deadline"] and record["level"] == "strong":
            moved = (date.fromisoformat(fresh) - date.fromisoformat(record["deadline"])).days
            record["deadline"] = fresh
            if moved >= 2:  # a one-day shift is usually a timezone difference, not an extension
                record["reminded"] = []
                record["extended"] = True
        elif record and fresh and not record.get("deadline"):
            record["deadline"] = fresh

    if not info["baselined"]:
        known = {fingerprint(r) for r in state["items"].values()} - {None}
        for key, (item, level, category) in new.items():
            record = record_of(item, level, category, today, True)
            state["items"][key] = record
            mark = fingerprint(record)
            if level == "strong" and record["deadline"] and item.deadline >= today and mark not in known:
                open_now.append(key)
                if mark:
                    known.add(mark)
        info["baselined"] = True
        return

    active = {k: v for k, v in new.items() if not is_stale(v[0], today)}
    for key, (item, level, category) in new.items():
        if key not in active:
            state["items"][key] = record_of(item, level, category, today, True)
    if len(active) > FLOOD_LIMIT:
        floods.append(f'- <b>{esc(source["university"])}</b>: {len(active)} ta notun link, site hoyto bodleche. '
                      f'<a href="{esc(page_url)}">Page dekhun</a>')
        for key, (item, _, category) in active.items():
            record = record_of(item, "possible", category, today, True)
            record["digested"] = False
            state["items"][key] = record
        return
    known = {fingerprint(r) for r in state["items"].values()} - {None}
    for key, (item, level, category) in active.items():
        record = record_of(item, level, category, today, False)
        mark = fingerprint(record)
        if mark and mark in known:  # same circular already announced from another page
            record["notified"] = True
            record["digested"] = True
        elif mark:
            known.add(mark)
        state["items"][key] = record


def send_pending(state: dict, notifier, fetcher, today: date) -> int:
    sent = 0
    pending = [(k, r) for k, r in state["items"].items() if not r["notified"] and r["level"] == "strong"]
    for key, record in sorted(pending, key=lambda x: (x[1].get("deadline") or "9999", x[1]["university"])):
        if not record.get("deadline") and ".pdf" in record["url"].lower() and hasattr(fetcher, "pdf_deadline"):
            found = fetcher.pdf_deadline(record["url"])
            if found and today <= found <= today + timedelta(days=365):
                record["deadline"] = found.isoformat()
            elif found and found < today:
                record["notified"] = True
                continue
        notifier.send(alert_message(record, today))
        record["notified"] = True
        sent += 1
    for record in state["items"].values():
        if record.get("extended"):
            if record.get("deadline") and date.fromisoformat(record["deadline"]) >= today:
                notifier.send(alert_message(record, today, "Deadline bereche"))
                sent += 1
            del record["extended"]
    for key, record in state["items"].items():
        if not record["notified"] and record["level"] == "possible":
            record["notified"] = True
    return sent


def send_open_now(keys: list[str], state: dict, notifier, today: date) -> None:
    # Saved in state first, so a Telegram outage cannot lose the first summary.
    pending = set(state.get("pending_open", [])) | set(keys)
    state["pending_open"] = sorted(pending)
    records = [state["items"][k] for k in pending if k in state["items"] and state["items"][k].get("deadline")
               and date.fromisoformat(state["items"][k]["deadline"]) >= today]
    if records:
        records.sort(key=lambda r: r["deadline"])
        lines = [f"<b>Ekhon open circular ({len(records)})</b>"] + [short_line(r, today) for r in records]
        notifier.send("\n".join(lines))
    state["pending_open"] = []


def send_health(state: dict, notifier) -> None:
    broken, recovered = [], []
    for url, info in state["sources"].items():
        failed = info["fails"] >= FAILS_BEFORE_ALERT
        empty = info.get("zero_runs", 0) >= FAILS_BEFORE_ALERT
        if (failed or empty) and not info.get("alerted"):
            reason = info.get("state") if failed else "page-e ar circular pawa jachhe na"
            broken.append((info["university"], url, reason, info))
        elif not failed and not empty and info.get("alerted"):
            recovered.append((info["university"], url, info))
    if broken:
        lines = ["<b>Ei page gulo check kora jachhe na</b>"]
        lines += [f'- {esc(n)}: {esc(r)}. <a href="{esc(u)}">Page</a>' for n, u, r, _ in broken]
        notifier.send("\n".join(lines))
        for *_, info in broken:
            info["alerted"] = True
    if recovered:
        notifier.send("\n".join(["<b>Ei page gulo abar kaj korche</b>"] + [f"- {esc(n)}" for n, _, _ in recovered]))
        for *_, info in recovered:
            info["alerted"] = False


def send_digest(state: dict, config: dict, notifier, now: datetime, checked: int, ok_count: int) -> None:
    local = now.astimezone(DHAKA)
    today = local.date()
    if local.hour < DIGEST_HOUR or state.get("last_digest") == today.isoformat():
        return
    reminders, shown = [], set()
    hidden = []
    for record in state["items"].values():
        if record["level"] != "strong" or not record.get("deadline") or not record["notified"]:
            continue
        days = (date.fromisoformat(record["deadline"]) - today).days
        if days < 0:
            continue
        due = [d for d in REMINDER_DAYS if days <= d and d not in record.get("reminded", [])]
        if due:
            mark = fingerprint(record)
            if mark and mark in shown:  # same circular listed on another page
                hidden.append((record, due))
                continue
            if mark:
                shown.add(mark)
            reminders.append((record, due))
    possible = [r for r in state["items"].values() if r["level"] == "possible" and not r.get("digested")]
    lines = [f"<b>Daily update, {today.strftime('%d %b %Y')}</b>",
             f"Bot cholche. Ei run-e {ok_count}/{checked} page check hoyeche."]
    if reminders:
        lines += ["", "<b>Deadline reminder</b>"]
        lines += [short_line(r, today) for r, _ in sorted(reminders, key=lambda x: x[0]["deadline"])]
    if possible:
        lines += ["", "<b>Dekhe nin (possible circular)</b>"]
        lines += [short_line(r, today) for r in possible[:40]]
        if len(possible) > 40:
            lines.append(f"Aro {len(possible) - 40} ta.")
    notifier.send("\n".join(lines))
    for record, due in reminders + hidden:
        record["reminded"] = sorted(set(record.get("reminded", []) + due))
    for record in possible:
        record["digested"] = True
    state["last_digest"] = today.isoformat()
    if local.weekday() == MANUAL_WEEKDAY and state.get("last_manual") != today.isoformat():
        send_manual(config, notifier, state)
        state["last_manual"] = today.isoformat()


def send_manual(config: dict, notifier, state: dict | None = None) -> None:
    lines = ["<b>Weekly manual check</b>", "Ei university gulo bot automatically porte pare na:"]
    sources = (state or {}).get("sources", {})
    for university in config["universities"]:
        monitored = university.get("monitor_urls", [])
        if monitored and not all(sources.get(u, {}).get("fails", 0) >= FAILS_BEFORE_ALERT for u in monitored):
            continue
        if university.get("status") in NO_LINK_STATUSES or university["name"] in NO_LINK_NAMES:
            lines.append(f"- {esc(university['name'])}: official site confirm kore check korun")
        elif university.get("urls"):
            lines.append(f'- <a href="{esc(university["urls"][0])}">{esc(university["name"])}</a>')
    notifier.send("\n".join(lines))


def prune(state: dict, today: date) -> None:
    cutoff = (today - timedelta(days=PRUNE_DAYS)).isoformat()
    for key in [k for k, r in state["items"].items() if r.get("deadline") and r["deadline"] < cutoff]:
        del state["items"][key]


def run(config: dict, state: dict, fetcher, notifier, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(DHAKA).date()
    sources = list_sources(config)
    results = fetcher(sources)
    names = [u["name"] for u in config["universities"]]
    open_now: list[str] = []
    floods: list[str] = []
    for source in sources:
        result = results.get(source["url"]) or FetchResult(source["url"], False, "not_checked")
        try:
            process_source(source, result, state, today, names, open_now, floods)
        except Exception:  # one bad page must not stop the other universities
            log.exception("Could not process %s", source["url"])
            state["sources"].setdefault(source["url"], {"fails": 0, "count": 0, "zero_runs": 0, "alerted": False,
                                                        "baselined": False, "university": source["university"]})
            state["sources"][source["url"]]["fails"] += 1
            state["sources"][source["url"]]["state"] = "parse_error"
    state["last_run"] = now.isoformat()
    send_open_now(open_now, state, notifier, today)
    sent = send_pending(state, notifier, fetcher, today)
    if floods:
        notifier.send("\n".join(["<b>Onek notun link peyechi</b>", *floods, "Egulo daily update-e thakbe."]))
    send_health(state, notifier)
    ok_count = sum(1 for s in sources if results.get(s["url"]) and results[s["url"]].ok)
    send_digest(state, config, notifier, now, len(sources), ok_count)
    prune(state, today)
    return {"sources": len(sources), "ok": ok_count, "sent": sent, "open_now": len(set(open_now)),
            "items": len(state["items"])}


# ---------------------------------------------------------------- command line

def find_chat_id(token: str) -> int:
    try:
        response = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
    except requests.RequestException as error:
        print(f"Telegram connect hoyni ({type(error).__name__}).")
        return 1
    if response.status_code != 200:
        print(f"Telegram error HTTP {response.status_code}. Token check korun.")
        return 1
    chats = {}
    for update in response.json().get("result", []):
        chat = (update.get("message") or {}).get("chat")
        if chat:
            chats[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    if not chats:
        print("Kono chat pawa jayni. Bot-ke Telegram-e /start pathiye abar run korun.")
        return 1
    for chat_id, label in chats.items():
        print(f"Chat ID: {chat_id}  ({label})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-message", action="store_true")
    parser.add_argument("--find-chat-id", action="store_true")
    parser.add_argument("--state", type=Path, default=STATE_FILE)
    parser.add_argument("--sources", type=Path, default=SOURCES_FILE)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if args.find_chat_id:
        if not token:
            print("TELEGRAM_BOT_TOKEN set kora nei.")
            return 2
        return find_chat_id(token)
    if args.dry_run:
        notifier = DryRun()
    else:
        if not token or not chat_id:
            print("TELEGRAM_BOT_TOKEN ar TELEGRAM_CHAT_ID dorkar.", file=sys.stderr)
            return 2
        notifier = Telegram(token, chat_id)
    if args.test_message:
        try:
            notifier.send("<b>Test successful</b>\nUniversity circular bot apnar Telegram-e connect hoyeche.")
        except TelegramError as error:
            print(error, file=sys.stderr)
            return 1
        print("Test message pathano hoyeche.")
        return 0

    with open(args.sources, encoding="utf-8") as handle:
        config = json.load(handle)
    state = load_state(args.state)
    try:
        summary = run(config, state, Fetcher(), notifier)
    except TelegramError as error:
        # Unsent items stay pending and are retried next hour.
        log.error("%s", error)
        save_state(args.state, state)
        return 1
    save_state(args.state, state)
    log.info("Done: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
