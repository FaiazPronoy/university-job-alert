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
FAILS_BEFORE_ALERT = 6
FLOOD_LIMIT = 15
STALE_DAYS = 45
PRUNE_DAYS = 180
MAX_PAGE_BYTES = 5_000_000
MAX_PDF_BYTES = 8_000_000
HOST_DELAY = 1.0
MAX_DETAIL_CHECKS = 150
PDF_PAGES = 10
DETAIL_VERSION = 2  # raise when circular reading improves, so already-read circulars are read again
REPLACE_OLD_BOARD = True  # keep only the latest two boards in the chat
RULES_VERSION = 4  # raise whenever detection rules change
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
    ("dMy", rx(rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th|ই|শে|লা|রা)?[\s\-]*(?:of\s+)?({MONTH})[\s,.\-]*(20\d{{2}}|\d{{2}})(?!\d)")),
    ("Mdy", rx(rf"(?<![a-z])({MONTH})[\s.]*(\d{{1,2}})(?:st|nd|rd|th)?[\s,]+(20\d{{2}})(?!\d)")),
]
DEADLINE_WORDS = rx(
    r"dead\s*-?\s*line|last\s+date|closing\s+date|apply\s+by|on\s+or\s+before|submission\s+date|"
    r"\b(?:apply|submit(?:ted)?|reach|send)\b.{0,60}?\b(?:by|within|before)(?:\s+the)?\b|"
    r"application\s+(?:end|close)s?|expires?\s+on|শেষ\s*তারিখ|আবেদনের\s*শেষ|সর্বশেষ\s*তারিখ|আবেদন\s*গ্রহণের\s*শেষ")
BN_DEADLINE_AFTER = rx(r"^\D{0,6}(তারিখের\s*মধ্যে|তারিখ\s*পর্যন্ত|এর\s*মধ্যে|\d+\s*days?\s*remaining)")


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
           r"accountant|engineer\b|programmer|developer|analyst|specialist|হিসাবরক্ষক|"
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
NOT_JOB = rx(r"office\s*order|evaluation|research\s*proposal|chartered\s*accountant|duly\s+completed|"
             r"casino|gamstop|\bslot\b|gacor|\bjudi\b|togel|putaran\s*gratis|pg\s*soft|"
             r"গবেষণা\s*প্রস্তাব|বহিঃনিরীক্ষক|নিয়োগ\s*কার্যক্রম\s*বন্ধ")
FORM_WORD = rx(r"application\s*form|আবেদন\s*ফরম")
ANNOUNCEMENT = rx(r"circular|recruitment|বিজ্ঞপ্তি")
DEGREE_NAME = rx(r"(?:(?:master|bachelor)\s+of\s+)?(?:business|public)\s+administration|"
                 r"(?:master|bachelor)\s+of\s+\w+")
OFFICE_NAME = rx(r"office\s+of(\s+the)?\s+[^,\-–(]+")
NO_VACANCY = rx(r"no\s+(job\s+)?vacanc|no\s+open\s+positions?|no\s+current\s+(openings?|vacanc)|"
                r"building\s+this\s+page|কোন\s+নিয়োগ\s+নেই")
SCHOOL = rx(r"school\s*(and|&)\s*college|স্কুল\s*ও\s*কলেজ")
NAV_ONLY = rx(r"^(faculty\s*(&|and)\s*staff|faculty|faculties|officers?|registrar|administration|lecturers?|teachers?|career|careers|jobs?|"
              r"notice|notices|directors?|executive|management|শিক্ষক|কর্মকর্তা|নিয়োগ)(\s+(list|page))?$")
CAREER_URL = rx(r"career|job|vacanc|recruit|circular|employment")
TABLE_HEADER = rx(r"^(available\s+)?job\s+listings?\b")
PAGE_COPY = rx(r"^(explore|browse|discover|find|join|we\s|our\s|please|learn|build|submit|select|apply\s+now)\b")


WANTED_SUBJECT = rx(r"\bcse\b|(?-i:\bCS\b)|(?-i:\bIT\b)|\bict\b|computer|software|information\s*technology|data\s*science|"
                    r"artificial\s*intelligence|machine\s*learning|cyber|\bbba\b|\bmba\b|business|management|marketing|"
                    r"finance|banking|accounting|\bais\b|\bhrm\b|human\s*resource|supply\s*chain|"
                    r"কম্পিউটার|ব্যবসায়|ব্যবস্থাপনা|মার্কেটিং|ফিন্যান্স|হিসাববিজ্ঞান")
OTHER_SUBJECT = rx(r"\blaw\b|english|\bbangla\b|pharmacy|civil|\bce\b|\beee\b|electrical|mechanical|textile|architecture|"
                   r"physics|chemistry|mathematics|statistics|economics|sociology|political|history|philosophy|islamic|"
                   r"arabic|biology|biochem|microbiology|genetics|public\s*health|nursing|medicine|clinical|environmental|"
                   r"japanese|journalism|\bjms\b|fashion|agricultur|fisher|aquaculture|psychology|education|geography|"
                   r"music|fine\s*art|আইন|ইংরেজি|ফার্মেসি|পদার্থ|রসায়ন|গণিত|ইসলামিক|আরবি")
ABOVE_LECTURER = rx(r"professor|demonstrator|অধ্যাপক|ডেমোনস্ট্রেটর")
LECTURER_OR_RA = rx(r"lecturer|research|teaching\s*assistant|প্রভাষক|গবেষণা")
OFFICER_EXCLUDED = rx(r"medical|nurse|doctor|physician")
ENGINEER = rx(r"\bengineer\b")


def wanted_post(title: str, context: str) -> bool:
    """Keep CSE/BBA lecturer and RA posts, plus university officer-level posts."""
    title, context = clean(title), clean(context)
    if SCHOOL.search(f"{title} {context}"):
        return False
    role = role_of(OFFICE_NAME.sub(" ", title))
    if role == "Officer/Admin":
        if OFFICER_EXCLUDED.search(title):
            return False
        return not ENGINEER.search(title) or bool(WANTED_SUBJECT.search(title))
    if role in ("Teaching", "Research"):
        if ABOVE_LECTURER.search(title) and not LECTURER_OR_RA.search(title):
            return False
        for text in (title, context):
            if WANTED_SUBJECT.search(text):
                return True
            if OTHER_SUBJECT.search(text):
                return False
        return True  # a general faculty circular may include CSE or BBA
    return bool(WANTED_SUBJECT.search(title)) or not OTHER_SUBJECT.search(title)


LECTURER_IN_TEXT = rx(r"lecturer|research\s*(assistant|associate)|teaching\s*assistant|প্রভাষক|গবেষণা\s*সহকারী")
# A bare "Registrar" is usually the circular's signature, so only named posts count.
OFFICER_IN_TEXT = rx(r"officer|(?:assistant|deputy|additional)\s+registrar|administrat\w*\s+(?:post|position)|coordinator|"
                     r"কর্মকর্তা|(?:সহকারী|উপ|অতিরিক্ত)\s*রেজিস্ট্রার")


def circular_facts(text: str) -> dict:
    """What a circular's own page or PDF says: its deadline and which board(s) it belongs on."""
    text = clean(text)
    groups = []
    if LECTURER_IN_TEXT.search(text) and WANTED_SUBJECT.search(text):
        groups.append("lecturer")
    if OFFICER_IN_TEXT.search(OFFICE_NAME.sub(" ", text)) and not OFFICER_EXCLUDED.fullmatch(text):
        groups.append("officer")
    deadline = find_deadline(text)
    return {"deadline": deadline.isoformat() if deadline else None, "groups": groups}


def not_job(title: str) -> bool:
    return bool(NOT_JOB.search(title) or (FORM_WORD.search(title) and not ANNOUNCEMENT.search(title)))


def role_of(text: str) -> str | None:
    text = DEGREE_NAME.sub(" ", text)  # "Business Administration" is a subject, not an admin post
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
            or AFTER_CIRCULAR.search(title) or TABLE_HEADER.search(title) or not_job(title):
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

GENERIC_LINK = rx(r"^(view job advertisement|job advertisement|view circular|view|read|see|click|more|details?|browse|"
                  r"detail information|view details?|read more|learn more|"
                  r"download|apply|apply now|apply link|open|pdf|here|click here|see openings?|বিস্তারিত|ডাউনলোড|আবেদন করুন)"
                  r"(\s+(more|details?|here|now|pdf|file|notice))*\W*$")
META_LINE = rx(r"^(posting time|posted|published|publish date|application deadline|deadline|dead line|last date|"
               r"apply|position|permanent|contractual|প্রকাশের তারিখ|শেষ তারিখ)\b")
CIRCULAR_LINK = rx(r"job\s*circular|recruitment\s*(circular|notice)|vacancy\s*(circular|notice)|job\s*advert|"
                   r"নিয়োগ\s*বিজ্ঞপ্তি")
POSITION_LABEL = rx(r"^position\s*:\s*(.{4,})$")
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
    route = parts.fragment if parts.fragment.startswith("/") else ""  # "#/jobs/12" is a real page in a web app
    return urlunparse(("https", host, path, "", query, route))


def absolute_url(page_url: str, href: str) -> str | None:
    """Resolve a link. One malformed href on a page must not lose the rest of that page."""
    try:
        url = urljoin(page_url, href.strip())
        base, _, fragment = url.partition("#")
        urlparse(base)
    except ValueError:
        return None
    return f"{base}#{fragment}" if fragment.startswith("/") else base


def tidy_title(title: str) -> str:
    title = re.sub("[\U0001F000-\U0001FAFF☀-➿️]", " ", clean(title))
    title = re.sub(r"\[[^\]]*\d{4}[^\]]*\]", " ", title)
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
    return Item(university, title, url, page_url, context, find_deadline(context) or table_deadline(node),
                [value for _, value in find_dates(context)], bool(named))


def table_deadline(node) -> date | None:
    """Read a date from a row when the deadline word exists only in the table header."""
    row = node if node.name == "tr" else node.find_parent("tr")
    table = row.find_parent("table") if row else None
    header = table.find("tr") if table else None
    if not header or header is row or not DEADLINE_WORDS.search(clean(header.get_text(" "))):
        return None
    dates = find_dates(row.get_text(" "))
    if len(dates) == 1:
        return dates[0][1]
    titles = [clean(cell.get_text(" ")) for cell in header.find_all(["th", "td"])]
    cells = row.find_all(["td", "th"], recursive=False)
    for index, label in enumerate(titles):
        if DEADLINE_WORDS.search(label) and index < len(cells):
            found = find_dates(cells[index].get_text(" "))
            if found:
                return found[0][1]
    return None


def tidy_key(title: str) -> str:
    return re.sub(r"\W+", " ", clean(title).lower()).strip()


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
        new_title, old_title = tidy_key(item.title), tidy_key(current.title)
        same_post = new_title.startswith(old_title) or old_title.startswith(new_title)  # card and carousel copy
        if item.named and current.named and not same_post:
            output.append(item)  # different posts can share one application form
            continue
        if item.named and (not current.named or len(new_title) < len(old_title)):
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
        if not href or (href.startswith("#") and not href.startswith("#/")) \
                or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        url = absolute_url(page_url, href)
        if not url or urlparse(url).scheme not in ("http", "https") or canonical_url(url) == page_key:
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
    items += position_items(university, soup, page_url) + image_circular_items(university, soup, page_url)
    return merge_same_link(items + menu_circulars, page_url)


def position_items(university: str, soup, page_url: str) -> list[Item]:
    """Accordion career pages: 'Position: Lecturer (CSE...)' with a 'Deadline:' line after it."""
    found = []
    for label in soup.find_all(["strong", "b", "h2", "h3", "h4", "h5", "h6"]):
        match = POSITION_LABEL.match(clean(label.get_text(" ")))
        if not match or not role_of(match.group(1)):
            continue
        title = tidy_title(match.group(1))
        mark = label.find_next(string=DEADLINE_WORDS)
        note = clean(mark.parent.get_text(" "))[:160] if mark else ""
        context = f"{title} {note}"
        found.append(Item(university, title, page_url, page_url, context, find_deadline(note),
                          [value for _, value in find_dates(note)], True))
    return found


def image_circular_items(university: str, soup, page_url: str) -> list[Item]:
    """A circular published only as a picture, with its deadline printed beside the image."""
    found = []
    for image in soup.find_all("img", alt=CIRCULAR_LINK):
        for parent in image.parents:
            if parent.name in ("body", "html", "[document]"):
                break
            text = clean(parent.get_text(" "))
            if len(text) > 800:
                break
            deadline = find_deadline(text)
            if deadline:
                title = f"Job Circular (deadline {deadline.strftime('%d %b %Y')})"
                found.append(Item(university, title, page_url, page_url, f"{title} {text}", deadline, [deadline], True))
                break
    return found


def menu_circular_items(university: str, soup, page_url: str) -> list[Item]:
    """Some portals keep the actual circular PDF in the top menu, e.g. 'Download Job Circular'."""
    found = []
    for zone in soup(["nav", "header", "footer", "aside"]):
        for anchor in zone.find_all("a", href=True):
            text = clean(anchor.get_text(" "))
            url = absolute_url(page_url, anchor["href"])
            if not url or not CIRCULAR_LINK.search(text) or not DOCUMENT_URL.search(urlparse(url).path):
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
        if NOISE.search(line) or AFTER_CIRCULAR.search(line) or NO_VACANCY.search(line) or not_job(line):
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

    def detail_info(self, urls: list[str]) -> dict[str, dict]:
        """Many list pages hide the deadline, so read the circular's own page or PDF.

        Returns {url: {"deadline": date | None, "groups": [...]}} for every circular that could be read.
        """
        documents = [u for u in urls if DOCUMENT_URL.search(urlparse(u).path)]
        by_host: dict[str, list[str]] = defaultdict(list)
        for url in urls:
            if url not in documents:
                by_host[urlparse(url).netloc].append(url)
        found: dict[str, dict] = {}
        thin: list[str] = []
        with ThreadPoolExecutor(8) as pool:
            for group in pool.map(self.fetch_host, by_host.values()):
                for result in group:
                    if not result.ok:
                        continue
                    # Site-wide banners such as "admission deadline" must not become the job deadline.
                    text = " ".join(x for x in page_lines(result.html) if not NOISE.search(x))
                    if len(text) < 300:
                        thin.append(result.url)
                    else:
                        found[result.url] = circular_facts(text)
        for result in self.browser(thin):
            if result.ok:
                found[result.url] = circular_facts(" ".join(x for x in page_lines(result.html) if not NOISE.search(x)))
        for url in documents:
            if url.lower().split("?")[0].endswith(".pdf") and (text := self.pdf_text(url)):
                found[url] = circular_facts(text)
        return found

    def pdf_deadline(self, url: str) -> date | None:
        text = self.pdf_text(url)
        return find_deadline(text) if text else None

    def pdf_text(self, url: str) -> str | None:
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
            # The "apply by" line is usually at the very end, after the list of posts.
            text = " ".join((page.extract_text() or "") for page in reader.pages[:PDF_PAGES])
            return text if text.strip() else None
        except Exception as error:  # malformed PDFs must never stop the run
            log.info("PDF skipped: %s (%s)", url, type(error).__name__)
            return None


# ---------------------------------------------------------------- Telegram

class TelegramError(RuntimeError):
    pass


def split_message(text: str, limit: int = 3900) -> list[str]:
    """Split at blank lines first, so one circular's lines never land in two messages."""
    parts, current = [], ""
    for block in text.split("\n\n"):
        whole = len(block) <= limit
        for piece in [block] if whole else [line[:limit] for line in block.split("\n")]:
            joined = f"{current}{chr(10) * 2 if whole else chr(10)}{piece}" if current else piece
            if len(joined) > limit:
                parts.append(current)
                current = piece
            else:
                current = joined
    if current:
        parts.append(current)
    return parts


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.api = f"https://api.telegram.org/bot{token}"
        self.url = f"{self.api}/sendMessage"
        self.chat_id = chat_id

    def send(self, text: str, silent: bool = False) -> list[int]:
        ids = []
        for part in split_message(text):
            ids.append(self._send_part(part, silent))
            time.sleep(1.05)
        return ids

    def delete(self, ids: list[int]) -> None:
        """Remove the previous hourly board. Failures are harmless: the old message just stays."""
        for message_id in ids:
            try:
                requests.post(f"{self.api}/deleteMessage", json={"chat_id": self.chat_id, "message_id": message_id},
                              timeout=20)
            except requests.RequestException:
                pass
            time.sleep(0.4)

    def _send_part(self, text: str, silent: bool = False) -> int:
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
                   "disable_notification": silent}
        for _ in range(4):
            try:
                response = requests.post(self.url, json=payload, timeout=20)
            except requests.RequestException as error:
                # The request URL contains the token, so never log the original message.
                last = f"network error ({type(error).__name__})"
                time.sleep(3)
                continue
            if response.status_code == 200:
                return response.json().get("result", {}).get("message_id", 0)
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

    def send(self, text: str, silent: bool = False) -> list[int]:
        self.messages.append(text)
        plain = re.sub(r"<[^>]+>", "", text)
        sound = "(silent)" if silent else "(notification sound)"
        sys.stdout.buffer.write((f"{sound}\n" + html_lib.unescape(plain) + "\n" + "-" * 50 + "\n").encode("utf-8", "replace"))
        sys.stdout.flush()
        return [len(self.messages)]

    def delete(self, ids: list[int]) -> None:
        pass


# ---------------------------------------------------------------- messages

def esc(value: str) -> str:
    return html_lib.escape(value or "", quote=True)


HEALTH_REASONS = {"blocked": "site block korche", "bot_check": "bot check dekhacche", "timeout": "site response dicchhe na",
                  "conn_error": "site-e connect hocchhe na", "cert_error": "site-er certificate somossa",
                  "server_error": "site-e error", "not_found": "page ar nei", "parse_error": "page porte somossa",
                  "robots_disallowed": "site auto-check allow kore na"}


def days_text(days: int) -> str:
    if days == 0:
        return "aj shesh din"
    return "kal shesh din" if days == 1 else f"{days} din baki"


def days_left(record: dict, today: date) -> int | None:
    return (date.fromisoformat(record["deadline"]) - today).days if record.get("deadline") else None


def is_new(record: dict, today: date) -> bool:
    """Published today. Posts that were already up when the bot started watching a page are not new."""
    return record.get("first_seen") == today.isoformat() and not record.get("baseline", True)


def title_of(record: dict, limit: int = 180) -> str:
    title = clean(record["title"]).rstrip(" .-|:")
    return title if len(title) <= limit else title[:limit - 1].rstrip() + "…"


def unique_records(records: list[dict]) -> list[dict]:
    seen, output = set(), []
    for record in records:
        mark = fingerprint(record) or (record["university"], tidy_key(record["title"]), record.get("deadline"))
        if mark not in seen:
            seen.add(mark)
            output.append(record)
    return output


LECTURER_ROLES = {"Teaching", "Research"}
SEEN_GRACE_DAYS = 2  # an undated post stays listed while it is still on its page
UNDATED_DAYS = 7  # ...for at most a week after it first appeared
CANCELLED = rx(r"cancel|suspend|postpone|withdrawn|বাতিল|স্থগিত")
QUOTE_LIMIT = 3000
TYPE_TAGS = {"private": "Private", "public": "Public"}
NEW_TAG = "🟢 <b>NEW</b> · "


def board_groups(record: dict) -> list[str]:
    """Where a record is listed. A circular whose text names both kinds of post goes on both boards."""
    return record.get("groups") or [board_group(record)]


def board_group(record: dict) -> str:
    """lecturer, officer, or general (a circular whose post the title does not name)."""
    role = record.get("category")
    if role not in ("Teaching", "Research", "Officer/Admin"):
        role = role_of(OFFICE_NAME.sub(" ", clean(record["title"])))
    if role in LECTURER_ROLES:
        return "lecturer"
    return "officer" if role == "Officer/Admin" else "general"


def on_board(record: dict, today: date) -> bool:
    title = clean(record["title"])
    if record["level"] != "strong" or not_job(title) or CANCELLED.search(title):
        return False
    if NO_VACANCY.search(title) and "sore geche" not in title:
        return False
    if record.get("deadline"):
        return record["deadline"] >= today.isoformat()
    if (record.get("last_seen") or record["first_seen"]) < (today - timedelta(days=SEEN_GRACE_DAYS)).isoformat():
        return False  # no longer on its page
    # Without a deadline: a fresh post for a week, or a linked circular dated in the last 45 days.
    # Bare page lines that were there when the bot started watching are mostly menus.
    fresh = not record.get("baseline", True) and record["first_seen"] >= (today - timedelta(days=UNDATED_DAYS)).isoformat()
    recent = (record.get("latest_date") or "") >= (today - timedelta(days=STALE_DAYS)).isoformat()
    return fresh or (recent and canonical_url(record["url"]) != canonical_url(record.get("page") or ""))


def short_date(value: str, today: date) -> str:
    day = date.fromisoformat(value)
    return day.strftime("%d %b" if day.year == today.year else "%d %b %Y")


def place(record: dict, types: dict, show_type: bool) -> str:
    kind = TYPE_TAGS.get(types.get(record["university"], ""))
    return f"<b>{esc(record['university'])}</b>" + (f" [{kind}]" if show_type and kind else "")


def board_line(record: dict, today: date, types: dict, show_type: bool) -> str:
    """Two lines: NEW tag, university and post, then the deadline with an urgency mark."""
    tag = NEW_TAG if is_new(record, today) else ""
    head = f'{tag}{place(record, types, show_type)} — <a href="{esc(record["url"])}">{esc(title_of(record, 110))}</a>'
    days = days_left(record, today)
    mark = "🔴" if days <= 3 else "🟠" if days <= 7 else "⏰"
    extended = " · ⏳ deadline bereche" if record.get("extended_on") == today.isoformat() else ""
    return f"{head}\n      {mark} {short_date(record['deadline'], today)} ({days_text(days)}){extended}"


def undated_line(record: dict, today: date, types: dict, show_type: bool) -> str:
    tag = NEW_TAG if is_new(record, today) else "• "
    return f'{tag}{place(record, types, show_type)} — <a href="{esc(record["url"])}">{esc(title_of(record, 110))}</a>'


def quote_blocks(heading: str, lines: list[str]) -> list[str]:
    """A tap-to-expand list. Long lists become several quotes so no tag is ever cut in half."""
    chunks, current = [], []
    for line in lines:
        if current and len("\n".join(current + [line])) > QUOTE_LIMIT:
            chunks.append(current)
            current = []
        current.append(line)
    if current:
        chunks.append(current)
    return [f"{heading if i == 0 else heading + ' (aro)'}\n<blockquote expandable>{chr(10).join(c)}</blockquote>"
            for i, c in enumerate(chunks)]


def latest_versions(records: list[dict]) -> list[dict]:
    """A post whose deadline changed on the same page is one post: keep the newest reading."""
    newest: dict[tuple, dict] = {}
    for record in records:
        mark = (record["university"], tidy_key(record["title"]), canonical_url(record["url"]))
        seen = (record.get("last_seen") or record["first_seen"], record["first_seen"])
        old = newest.get(mark)
        if not old or seen > (old.get("last_seen") or old["first_seen"], old["first_seen"]):
            newest[mark] = record
    return [r for r in records if newest.get((r["university"], tidy_key(r["title"]), canonical_url(r["url"]))) is r]


def by_deadline(records: list[dict], today: date) -> list[dict]:
    return sorted(records, key=lambda r: (r["deadline"], not is_new(r, today), r["university"]))


def section(heading: str, records: list[dict], today: date, types: dict, show_type: bool, empty: str) -> list[str]:
    """Soonest deadline first. Posts without a deadline sit in a collapsed list under them."""
    dated = by_deadline([r for r in records if r.get("deadline")], today)
    undated = sorted((r for r in records if not r.get("deadline")), key=lambda r: (not is_new(r, today), r["university"]))
    fresh = sum(is_new(r, today) for r in records)
    title = f"{heading} ({len(records)})" + (f" · 🟢 NEW {fresh}" if fresh else "")
    if not records:
        return [f"{title}\n<i>{empty}</i>"]
    lines = [board_line(r, today, types, show_type) for r in dated]
    blocks = [f"{title}\n{lines[0]}", *lines[1:]] if lines else [title]
    if undated:
        blocks += quote_blocks(f"📄 <i>Deadline lekha nai, circular khule dekho ({len(undated)})</i>",
                               [undated_line(r, today, types, show_type) for r in undated])
    return blocks


def board_header(heading: str, records: list[dict], now: datetime) -> str:
    local = now.astimezone(DHAKA)
    today = local.date()
    urgent = sum(1 for r in records if r.get("deadline") and days_left(r, today) <= 3)
    stats = [f"Mot {len(records)} ta"]
    if sum(is_new(r, today) for r in records):
        stats.append(f"🟢 NEW {sum(is_new(r, today) for r in records)}")
    if urgent:
        stats.append(f"🔴 {urgent} ta 3 diner moddhe")
    return f"{heading}\n🕐 {local.strftime('%d %b, %I:%M %p')} · " + " · ".join(stats)


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
            "first_seen": today.isoformat(), "last_seen": today.isoformat(), "notified": notified, "baseline": notified, "digested": level != "possible" or notified,
            "reminded": [], "latest_date": max(item.dates).isoformat() if item.dates else None}


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

    accepted = {k: v for k, v in accepted.items() if v[2] == "Career page update" and "no vacancy" in v[0].title
                or wanted_post(v[0].title, v[0].context)}
    info["zero_runs"] = info.get("zero_runs", 0) + 1 if info.get("count", 0) >= 3 and not accepted else 0
    if accepted or info["zero_runs"] >= FAILS_BEFORE_ALERT:
        info["count"] = len(accepted)

    new = {k: v for k, v in accepted.items() if k not in state["items"]}
    for key, (item, _, _) in accepted.items():
        record = state["items"].get(key)
        if record:
            record["last_seen"] = today.isoformat()
        if record and item.dates:
            record["latest_date"] = max(item.dates).isoformat()
        fresh = valid_deadline(item, today)
        if record and fresh and record.get("deadline") and fresh > record["deadline"] and record["level"] == "strong":
            moved = (date.fromisoformat(fresh) - date.fromisoformat(record["deadline"])).days
            record["deadline"] = fresh
            if moved >= 2:  # a one-day shift is usually a timezone difference, not an extension
                record["reminded"] = []
                record["extended_on"] = today.isoformat()
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
        floods.append(f'• <b>{esc(source["university"])}</b>: {len(active)} ta notun link, site hoyto bodleche. '
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


def check_pdf_deadlines(state: dict, fetcher, today: date) -> None:
    """A new PDF circular whose page shows no deadline: read the deadline from the PDF itself."""
    if not hasattr(fetcher, "pdf_deadline"):
        return
    for record in state["items"].values():
        if record["notified"] or record["level"] != "strong" or record.get("deadline") or ".pdf" not in record["url"].lower():
            continue
        found = fetcher.pdf_deadline(record["url"])
        if found and found <= today + timedelta(days=365):
            record["deadline"] = found.isoformat()  # a past date simply keeps it off the board


def enrich_deadlines(state: dict, fetcher, today: date) -> None:
    """Open each circular that is missing its deadline or its post type, and read them from it."""
    if not hasattr(fetcher, "detail_info"):
        return
    failing = {urlparse(u).netloc for u, info in state["sources"].items() if info.get("state") != "ok"}
    source_pages = {canonical_url(u) for u in state["sources"]}

    def is_circular_link(url: str) -> bool:
        parts = urlparse(url)
        return bool(DOCUMENT_URL.search(parts.path) or CAREER_URL.search(f"{parts.path}?{parts.query}"))

    def needs_reading(r: dict) -> bool:
        missing = not r.get("deadline") or (r["category"] == "General circular" and "groups" not in r)
        return missing and (r.get("deadline") or "9999") >= today.isoformat()

    todo = [r for r in state["items"].values()
            if r["level"] == "strong" and needs_reading(r) and r.get("detail_checked") != DETAIL_VERSION
            and r["category"] != "Career page update" and not not_job(r["title"])
            and canonical_url(r["url"]) not in source_pages and is_circular_link(r["url"])
            and urlparse(r["url"]).netloc not in failing]
    todo.sort(key=lambda r: r.get("latest_date") or r["first_seen"], reverse=True)
    todo.sort(key=lambda r: r["notified"])
    todo = todo[:MAX_DETAIL_CHECKS]
    if not todo:
        return
    found = fetcher.detail_info(list(dict.fromkeys(r["url"] for r in todo)))
    limit = (today + timedelta(days=365)).isoformat()
    for record in todo:
        record["detail_checked"] = DETAIL_VERSION
        facts = found.get(record["url"])
        if not facts:
            continue
        if facts["deadline"] and not record.get("deadline") and facts["deadline"] <= limit:
            record["deadline"] = facts["deadline"]
        if facts["groups"]:
            record["groups"] = facts["groups"]


def failing_sources(state: dict) -> list[tuple[str, str, str]]:
    shown = {}
    for url, info in state["sources"].items():
        failed = info["fails"] >= FAILS_BEFORE_ALERT
        if failed or info.get("zero_runs", 0) >= FAILS_BEFORE_ALERT:
            reason = HEALTH_REASONS.get(info.get("state"), info.get("state")) if failed else "page-e ar circular pawa jachhe na"
            shown.setdefault(info["university"], (url, reason))
    return [(name, url, reason) for name, (url, reason) in shown.items()]


def manual_lines(config: dict, state: dict | None = None) -> list[str]:
    lines = []
    sources = (state or {}).get("sources", {})
    for university in config["universities"]:
        monitored = university.get("monitor_urls", [])
        all_failing = all(sources.get(u, {}).get("fails", 0) >= FAILS_BEFORE_ALERT for u in monitored)
        if monitored and not all_failing and not university.get("manual_check"):
            continue
        if university.get("status") in NO_LINK_STATUSES or university["name"] in NO_LINK_NAMES:
            lines.append(f"• {esc(university['name'])}: official site confirm kore dekho")
        elif university.get("urls"):
            lines.append(f'• <a href="{esc(university["urls"][0])}">{esc(university["name"])}</a>')
    return lines


LEGEND = "<i>🟢 NEW = aj publish hoyeche · 🔴 3 diner moddhe · 🟠 ei shoptahe · nam-e tap korle circular khulbe</i>"


def build_boards(state: dict, config: dict, now: datetime, weekly: bool = False) -> tuple[list[str], list[dict]]:
    """The two hourly messages: lecturer/RA (private, then public) and officer-level posts."""
    today = now.astimezone(DHAKA).date()
    types = {u["name"]: u.get("type", "") for u in config["universities"]}
    records = latest_versions(unique_records([r for r in state["items"].values() if on_board(r, today)]))
    group = {g: [r for r in records if g in board_groups(r)] for g in ("lecturer", "officer", "general")}
    lecturers = group["lecturer"]
    public = [r for r in lecturers if types.get(r["university"]) == "public"]
    private = [r for r in lecturers if types.get(r["university"]) != "public"]
    none = "Ekhon kono open circular nei."

    first = [board_header("🎓 <b>LECTURER &amp; RESEARCH · CSE / BBA</b>", lecturers, now)]
    first += section("🏫 <b>PRIVATE UNIVERSITY</b>", private, today, types, False, none)
    first += section("🏛 <b>PUBLIC UNIVERSITY</b>", public, today, types, False, none)
    first.append(LEGEND)

    officers = group["officer"] + group["general"]
    second = [board_header("🏢 <b>OFFICER · ADMIN · HR</b>", officers, now)]
    second += section("💼 <b>OFFICER-LEVEL POST</b>", group["officer"], today, types, True, none)
    if group["general"]:
        second += section("📋 <b>GENERAL JOB CIRCULAR</b> <i>(post-er nam circular-e)</i>", group["general"],
                          today, types, True, none)
    broken = failing_sources(state)
    if broken:
        second += quote_blocks(f"⚠️ <b>Ei site gulo ekhon check kora jachhe na ({len(broken)})</b>",
                               [f'• <a href="{esc(u)}">{esc(n)}</a>: {esc(r)}' for n, u, r in broken])
    if weekly:
        lines = manual_lines(config, state)
        if lines:
            second += quote_blocks("🗂 <b>Weekly manual check</b> · bot egulo porte pare na, ekbar khule dekho", lines)
    second.append(LEGEND)
    return ["\n\n".join(first), "\n\n".join(second)], records


def send_boards(state: dict, config: dict, notifier, now: datetime) -> bool:
    """Send both boards, silently unless something new arrived, then remove the previous hour's boards."""
    local = now.astimezone(DHAKA)
    today = local.date()
    weekly = local.weekday() == MANUAL_WEEKDAY and local.hour >= DIGEST_HOUR and state.get("last_manual") != today.isoformat()
    messages, records = build_boards(state, config, now, weekly)
    loud = any(not r["notified"] for r in records)
    old, sent = state.get("board_messages", []), []
    try:
        for text in messages:
            sent += notifier.send(text, silent=not loud)
    except TelegramError:
        state["board_messages"] = old + sent  # removed together after the next successful run
        raise
    if REPLACE_OLD_BOARD and old:
        notifier.delete(old)
    state["board_messages"] = sent if REPLACE_OLD_BOARD else []
    for record in state["items"].values():
        record["notified"] = True
    if weekly:
        state["last_manual"] = today.isoformat()
    return loud


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
    if state.get("rules_version") != RULES_VERSION:
        # New detection rules must not mark old page content as new circulars.
        for info in state["sources"].values():
            info["baselined"] = False
        state["items"] = {k: r for k, r in state["items"].items() if wanted_post(r["title"], r["title"])}
        state["rules_version"] = RULES_VERSION
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
    for line in floods:
        log.warning("Many new links, kept out of the board: %s", re.sub(r"<[^>]+>", "", line))
    enrich_deadlines(state, fetcher, today)
    check_pdf_deadlines(state, fetcher, today)
    loud = send_boards(state, config, notifier, now)
    prune(state, today)
    ok_count = sum(1 for s in sources if results.get(s["url"]) and results[s["url"]].ok)
    return {"sources": len(sources), "ok": ok_count, "new": loud, "items": len(state["items"])}


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
