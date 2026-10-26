#!/usr/bin/env python3
"""Vacature-radar: haalt vacatures op uit webpagina's en uit alert-mails,
filtert ze en stuurt een pushmelding (ntfy) zodra er iets nieuws is.

Gebruik:
    python radar.py                 # normale run
    python radar.py --dry-run       # toont wat er verstuurd zou worden, verstuurt niets
    python radar.py --probe URL     # toont alle links op een pagina (om link_regex te kiezen)
    python radar.py --probe-mail X  # toont links in recente mails van afzender X
"""
from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state" / "seen.json"
CONFIG_FILE = ROOT / "config.yaml"
UA = "Mozilla/5.0 (compatible; JobRadar/1.0; persoonlijk gebruik)"
GENERIC_TITLES = {
    "", "view job", "bekijk vacature", "bekijk job", "apply", "solliciteer",
    "solliciteer nu", "see job", "toon vacature", "meer info", "read more",
}
MAX_FIRST_RUN_PUSHES = 10


@dataclass
class Job:
    source: str
    title: str
    url: str


# --------------------------------------------------------------------------
# Hulpfuncties
# --------------------------------------------------------------------------
def normalize_url(url: str, keep_params: tuple[str, ...] = ()) -> str:
    """Verwijdert tracking (query/fragment) behalve de params in keep_params."""
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query) if k in keep_params]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), urlencode(q), ""))


def title_from_url(url: str) -> str:
    slug = urlsplit(url).path.rstrip("/").split("/")[-1]
    return re.sub(r"[-_]+", " ", slug).strip() or url


def extract_links(html: str, base_url: str, link_regex: str, source: str,
                  keep_params: tuple[str, ...] = (), fallback_title: str = "") -> list[Job]:
    """Zoekt alle <a>-links die aan link_regex voldoen. Per URL houden we de langste tekst."""
    soup = BeautifulSoup(html, "html.parser")
    rx = re.compile(link_regex)
    best: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        if not rx.search(href):
            continue
        url = normalize_url(href, keep_params)
        text = " ".join(a.get_text(" ").split())
        if text.lower() in GENERIC_TITLES:
            text = ""
        if len(text) > len(best.get(url, "")) or url not in best:
            best[url] = text
    jobs = []
    for url, text in best.items():
        title = text or fallback_title or title_from_url(url)
        jobs.append(Job(source, title, url))
    return jobs


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    cutoff = time.time() - 90 * 86400  # na 90 dagen opruimen
    state = {k: v for k, v in state.items() if v > cutoff}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=0, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------
# Bronnen
# --------------------------------------------------------------------------
def fetch_html_source(src: dict) -> list[Job]:
    r = requests.get(src["url"], headers={"User-Agent": UA, "Accept-Language": "nl-BE,nl;q=0.9"},
                     timeout=25)
    r.raise_for_status()
    return extract_links(r.text, src["url"], src["link_regex"], src["name"],
                         tuple(src.get("keep_params", [])))


def _decode_part(part) -> str:
    payload = part.get_payload(decode=True) or b""
    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def parse_mail(raw: bytes, src: dict) -> list[Job]:
    msg = email.message_from_bytes(raw)
    subject = str(make_header(decode_header(msg.get("Subject", ""))))
    jobs: list[Job] = []
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            jobs += extract_links(_decode_part(part), "", src["link_regex"], src["name"],
                                  tuple(src.get("keep_params", [])), fallback_title=subject)
    return jobs


def _imap_connect():
    user, pw = os.environ.get("IMAP_USER"), os.environ.get("IMAP_PASSWORD")
    if not (user and pw):
        raise RuntimeError("IMAP_USER / IMAP_PASSWORD niet ingesteld")
    host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    M = imaplib.IMAP4_SSL(host)
    M.login(user, pw)
    M.select("INBOX", readonly=True)
    return M


def fetch_mails(sender: str, days: int = 3) -> list[bytes]:
    M = _imap_connect()
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
        _, data = M.search(None, "SINCE", since, "FROM", f'"{sender}"')
        out = []
        for num in data[0].split():
            _, msg_data = M.fetch(num, "(RFC822)")
            out.append(msg_data[0][1])
        return out
    finally:
        M.logout()


def fetch_mail_source(src: dict) -> list[Job]:
    jobs: list[Job] = []
    for raw in fetch_mails(src["from_contains"], src.get("days", 3)):
        jobs += parse_mail(raw, src)
    return jobs


# --------------------------------------------------------------------------
# Filter
# --------------------------------------------------------------------------
def compile_words(words: list[str]) -> re.Pattern | None:
    if not words:
        return None
    return re.compile(r"(?<![\w])(" + "|".join(re.escape(w.lower()) for w in words) + r")(?![\w])")


class Matcher:
    def __init__(self, cfg: dict):
        self.include = compile_words(cfg.get("include", []))
        self.exclude = compile_words(cfg.get("exclude", []))
        self.boost = compile_words(cfg.get("boost", []))

    def check(self, job: Job) -> tuple[bool, bool]:
        """Geeft (is_match, is_priority) terug."""
        t = job.title.lower()
        if self.exclude and self.exclude.search(t):
            return False, False
        if self.include and not self.include.search(t):
            return False, False
        return True, bool(self.boost and self.boost.search(t))


# --------------------------------------------------------------------------
# Melding
# --------------------------------------------------------------------------
def notify(topic: str, job: Job, priority: bool) -> None:
    r = requests.post("https://ntfy.sh/", json={
        "topic": topic,
        "title": job.title[:150],
        "message": f"{job.source}\n{job.url}",
        "click": job.url,
        "priority": 4 if priority else 3,
        "tags": ["star" if priority else "briefcase"],
        "actions": [{"action": "view", "label": "Open vacature", "url": job.url}],
    }, timeout=15)
    r.raise_for_status()


# --------------------------------------------------------------------------
# Hoofdlogica
# --------------------------------------------------------------------------
def collect(cfg: dict) -> list[Job]:
    jobs: list[Job] = []
    for src in cfg.get("sources", []):
        if not src.get("enabled", True):
            continue
        try:
            found = fetch_mail_source(src) if src["type"] == "mail" else fetch_html_source(src)
        except Exception as e:  # één kapotte bron mag de rest niet stoppen
            print(f"[FOUT] {src['name']}: {e}", file=sys.stderr)
            continue
        flag = "" if found else "  <-- 0 links: link_regex fout, pagina via JavaScript, of bot geblokkeerd?"
        print(f"[{src['name']}] {len(found)} links{flag}")
        jobs += found
    return jobs


def run(dry: bool = False) -> int:
    cfg = load_config()
    matcher = Matcher(cfg)
    state = load_state()
    first_run = not state
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic and not dry:
        print("NTFY_TOPIC niet ingesteld", file=sys.stderr)
        return 1

    now = time.time()
    seen_this_run: set[str] = set()
    to_push: list[tuple[Job, bool]] = []
    for job in collect(cfg):
        if job.url in state or job.url in seen_this_run:
            continue
        seen_this_run.add(job.url)
        ok, prio = matcher.check(job)
        if ok:
            to_push.append((job, prio))
        else:
            state[job.url] = now  # niet relevant: meteen als gezien markeren

    to_push.sort(key=lambda x: not x[1])  # prioriteit eerst
    pushed = 0
    for i, (job, prio) in enumerate(to_push):
        if first_run and i >= MAX_FIRST_RUN_PUSHES:
            state[job.url] = now  # eerste run: niet je telefoon overspoelen
            continue
        if dry:
            print(f"  [DRY] {'*' if prio else ' '} {job.source}: {job.title} -> {job.url}")
        else:
            try:
                notify(topic, job, prio)
            except Exception as e:
                print(f"[FOUT] melding mislukt voor {job.url}: {e}", file=sys.stderr)
                continue  # niet als gezien markeren, volgende run opnieuw proberen
        state[job.url] = now
        pushed += 1

    if not dry:
        save_state(state)
    print(f"Klaar: {len(to_push)} nieuwe matches, {pushed} gemeld.")
    return 0


def probe(url: str) -> None:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
    print(f"HTTP {r.status_code}, {len(r.text)} tekens")
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.find_all("a", href=True)[:300]:
        text = " ".join(a.get_text(" ").split())[:70]
        print(f"{urljoin(url, a['href'])}   |   {text}")


def probe_mail(sender: str) -> None:
    for raw in fetch_mails(sender, days=7)[:3]:
        msg = email.message_from_bytes(raw)
        print("=== ", str(make_header(decode_header(msg.get("Subject", "")))))
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                soup = BeautifulSoup(_decode_part(part), "html.parser")
                for a in soup.find_all("a", href=True):
                    print(f"{a['href'][:150]}   |   {' '.join(a.get_text(' ').split())[:60]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--probe")
    ap.add_argument("--probe-mail")
    args = ap.parse_args()
    if args.probe:
        probe(args.probe)
    elif args.probe_mail:
        probe_mail(args.probe_mail)
    else:
        sys.exit(run(dry=args.dry_run))
