#!/usr/bin/env python3
"""Vacature-radar: haalt vacatures op uit webpagina's en uit alert-mails,
filtert ze (titel + vacaturetekst) en stuurt een pushmelding (ntfy) bij iets nieuws.

Gebruik:
    python radar.py                 # normale run
    python radar.py --dry-run       # toont wat er verstuurd zou worden, verstuurt niets
    python radar.py --diagnose      # toont per bron wat er binnenkomt en wat de regex pakt
    python radar.py --probe URL     # toont alle links op een pagina (om link_regex te kiezen)
    python radar.py --probe-mail X  # toont links in recente mails van afzender X
"""
from __future__ import annotations

import argparse
import base64
import email
import imaplib
import json
import os
import re
import sys
import time
import zlib
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
    note: str = ""
    key: str = ""  # alternatieve ontdubbel-sleutel (bv. bron+titel als de link een unieke tracking-URL is)

    @property
    def id(self) -> str:
        return self.key or self.url


# --------------------------------------------------------------------------
# Hulpfuncties
# --------------------------------------------------------------------------
def normalize_url(url: str, keep_params: tuple[str, ...] = ()) -> str:
    """Verwijdert tracking (query/fragment) behalve de params in keep_params."""
    p = urlsplit(url)
    path = p.path.rstrip("/")
    if "linkedin.com" in p.netloc:
        path = path.replace("/comm/jobs/", "/jobs/")  # publieke weergave i.p.v. mail-tracking
    q = [(k, v) for k, v in parse_qsl(p.query) if k in keep_params]
    return urlunsplit((p.scheme, p.netloc.lower(), path, urlencode(q), ""))


def title_from_url(url: str) -> str:
    slug = urlsplit(url).path.rstrip("/").split("/")[-1]
    return re.sub(r"[-_]+", " ", slug).strip() or url


def anchor_title(a) -> str:
    """Eerste betekenisvolle tekstregel in een link. In alertmails is dat de functietitel;
    bedrijf, plaats en 'Easy Apply' staan op de volgende regels en horen niet in de titelcheck."""
    for line in a.get_text("\n").split("\n"):
        line = " ".join(line.split())
        if line and line.lower() not in GENERIC_TITLES:
            return line
    return ""


def decode_tracking(href: str) -> str:
    """Indeed-mails gebruiken cts.indeed.com/v3/<base64(gzip(...))>. Probeer die inhoud te lezen."""
    m = re.search(r"/v3/([A-Za-z0-9_\-]+)", href)
    if not m:
        return ""
    s = m.group(1)
    try:
        raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        return zlib.decompressobj(31).decompress(raw).decode("latin-1")
    except Exception:
        return ""


def extract_links(html: str, base_url: str, link_regex: str, source: str,
                  keep_params: tuple[str, ...] = (), fallback_title: str = "",
                  key_by_title: bool = False) -> list[Job]:
    """Zoekt alle <a>-links die aan link_regex voldoen. Per URL houden we de langste titel.

    key_by_title: voor bronnen waar elke link een unieke tracking-URL is (Indeed-mail).
    Dan ontdubbelen we op bron+titel en slaan we links zonder tekst over."""
    soup = BeautifulSoup(html, "html.parser")
    rx = re.compile(link_regex)
    best: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        if not rx.search(href):
            continue
        keep_raw = False
        if "cts.indeed.com/v3/" in href:
            jk = re.search(r"jk=([0-9a-f]{16})", decode_tracking(href))
            if jk:  # echte vacature-id gevonden in de tracking-link: nette URL gebruiken
                href = f"https://be.indeed.com/viewjob?jk={jk.group(1)}"
                keep_raw = True  # de ?jk= is het vacature-id: niet wegnormaliseren
            else:
                keep_raw = True  # tracking-URL heeft zijn query nodig om te werken
        url = href.split("#")[0] if keep_raw else normalize_url(href, keep_params)
        text = anchor_title(a)
        if len(text) > len(best.get(url, "")) or url not in best:
            best[url] = text
    jobs = []
    for url, text in best.items():
        if key_by_title and not text:
            continue
        title = text or fallback_title or title_from_url(url)
        key = f"{source}|{text.lower()}" if key_by_title and "viewjob?jk=" not in url else ""
        jobs.append(Job(source, title, url, key=key))
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
                         tuple(src.get("keep_params", [])), key_by_title=src.get("key_by_title", False))


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
                                  tuple(src.get("keep_params", [])), fallback_title=subject,
                                  key_by_title=src.get("key_by_title", False))
    return jobs


_imap_cache: dict = {}


def _imap():
    """Eén gedeelde IMAP-verbinding per run; een mislukte login wordt maar één keer gemeld."""
    if "err" in _imap_cache:
        raise _imap_cache["err"]
    if "conn" not in _imap_cache:
        try:
            user, pw = os.environ.get("IMAP_USER"), os.environ.get("IMAP_PASSWORD")
            if not (user and pw):
                raise RuntimeError("IMAP_USER / IMAP_PASSWORD niet ingesteld")
            M = imaplib.IMAP4_SSL(os.environ.get("IMAP_HOST", "imap.gmail.com"))
            M.login(user, pw.replace(" ", ""))
            M.select("INBOX", readonly=True)
            _imap_cache["conn"] = M
        except Exception as e:
            _imap_cache["err"] = e
            raise
    return _imap_cache["conn"]


def fetch_mails(sender: str, days: int = 3) -> list[bytes]:
    M = _imap()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
    _, data = M.search(None, "SINCE", since, "FROM", f'"{sender}"')
    out = []
    for num in data[0].split():
        _, msg_data = M.fetch(num, "(BODY.PEEK[])")
        out.append(msg_data[0][1])
    return out


def fetch_mail_source(src: dict) -> list[Job]:
    jobs: list[Job] = []
    for raw in fetch_mails(src["from_contains"], src.get("days", 3)):
        jobs += parse_mail(raw, src)
    return jobs


# --------------------------------------------------------------------------
# Titelfilter
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
# Tekstfilter: gevraagde ervaring in de vacaturetekst
# --------------------------------------------------------------------------
YEARS = r"(?:jaar|jaren|jr\.?|years?|yrs?|ans|années?)"
YEARS_RX = re.compile(rf"(\d{{1,2}})(?:\s*[-–—]\s*(\d{{1,2}})|\s*\+)?\s*{YEARS}\b", re.I)
EXP_RX = re.compile(r"ervaring|experience|expérience", re.I)
SOFT_RX = re.compile(
    r"pluspunt|\bplus\b|troef|nice[- ]to[- ]have|\basset\b|\bbonus\b|voordeel|advantage|"
    r"\bpré\b|\batout\b|\bis een pre\b", re.I)
STARTER_RX = re.compile(
    r"geen (?:werk)?ervaring (?:vereist|nodig|gevraagd)|zonder (?:werk)?ervaring|"
    r"(?:werk)?ervaring is (?:niet|geen) (?:vereist|nodig|noodzakelijk)|"
    r"no (?:prior |previous |professional )?experience (?:is )?(?:required|needed|necessary)|"
    r"(?:starters?|beginners?|young graduates?|fresh graduates?|schoolverlaters?|jonge afgestudeerden)\s+(?:zijn\s+|are\s+)?(?:welkom|welcome)|"
    r"(?:starters?|beginners?)\s+welkom|ook (?:voor|als) starter|sans expérience|débutants? (?:accepté|bienvenu)",
    re.I)


def experience_verdict(text: str, tf: dict) -> tuple[bool, str]:
    """Geeft (ok, reden). ok=False als de tekst duidelijk meer ervaring vraagt dan toegestaan."""
    if STARTER_RX.search(text):
        return True, "starters welkom / geen ervaring vereist"
    max_years = tf.get("max_years", 1)
    worst = 0
    for sentence in re.split(r"[.!?\n•·;]+", text):
        if not EXP_RX.search(sentence) or SOFT_RX.search(sentence):
            continue
        for m in YEARS_RX.finditer(sentence):
            worst = max(worst, int(m.group(1)))  # bij 2-4 jaar telt het minimum (2)
    if worst > max_years:
        return False, f"{worst}+ jaar ervaring gevraagd"
    low = text.lower()
    for phrase in tf.get("reject_phrases", []):
        if phrase.lower() in low:
            return False, f"bevat '{phrase}'"
    return True, f"{worst} jaar ervaring genoemd" if worst else ""


def fetch_text(url: str) -> str | None:
    """Tekst van de vacaturepagina, of None als ophalen mislukt (login-muur, 403, te weinig tekst)."""
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8"},
                         timeout=8)
        if r.status_code != 200 or any(x in r.url for x in ("authwall", "/login", "/uas/")):
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "nav", "footer", "header"]):
            t.decompose()
        text = soup.get_text("\n")
        return text if len(text) > 400 else None
    except Exception:
        return None


# --------------------------------------------------------------------------
# Melding
# --------------------------------------------------------------------------
def notify(topic: str, job: Job, priority: bool) -> None:
    msg = f"{job.source}\n{job.url}" + (f"\n{job.note}" if job.note else "")
    r = requests.post("https://ntfy.sh/", json={
        "topic": topic,
        "title": job.title[:150],
        "message": msg,
        "click": job.url,
        "priority": 4 if priority else 3,
        "tags": ["star" if priority else "briefcase"],
        "actions": [{"action": "view", "label": "Open vacature", "url": job.url}],
    }, timeout=15)
    r.raise_for_status()


# --------------------------------------------------------------------------
# Hoofdlogica
# --------------------------------------------------------------------------
def collect(cfg: dict, errors: list[str]) -> list[Job]:
    jobs: list[Job] = []
    mail_error_reported = False
    for src in cfg.get("sources", []):
        if not src.get("enabled", True):
            continue
        try:
            found = fetch_mail_source(src) if src["type"] == "mail" else fetch_html_source(src)
        except Exception as e:  # één kapotte bron mag de rest niet stoppen
            if src["type"] == "mail":
                if not mail_error_reported:
                    print(f"[FOUT] mail-login/lezen mislukt: {e}", file=sys.stderr)
                    errors.append("mail")
                    mail_error_reported = True
            else:
                print(f"[FOUT] {src['name']}: {e}", file=sys.stderr)
            continue
        flag = "" if found else "  <-- 0 links: geen mails, link_regex fout, pagina via JavaScript of bot geblokkeerd?"
        print(f"[{src['name']}] {len(found)} links{flag}")
        jobs += found
    return jobs


def _show(label: str, items: list[str], n: int = 8) -> None:
    if items:
        more = f" (+{len(items) - n} meer)" if len(items) > n else ""
        print(f"  {label} ({len(items)}): " + " | ".join(items[:n]) + more)


def run(dry: bool = False) -> int:
    cfg = load_config()
    matcher = Matcher(cfg)
    tf = cfg.get("text_filter", {})
    tf_on = tf.get("enabled", False)
    state = load_state()
    first_run = not state
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic and not dry:
        print("NTFY_TOPIC niet ingesteld", file=sys.stderr)
        return 1

    errors: list[str] = []
    now = time.time()
    seen_this_run: set[str] = set()
    candidates: list[tuple[Job, bool]] = []
    title_rejected: list[str] = []
    for job in collect(cfg, errors):
        if job.id in state or job.id in seen_this_run:
            continue
        seen_this_run.add(job.id)
        ok, prio = matcher.check(job)
        if ok:
            candidates.append((job, prio))
        else:
            title_rejected.append(job.title[:60])
            state[job.id] = now  # niet relevant: meteen als gezien markeren

    candidates.sort(key=lambda x: not x[1])  # prioriteit eerst
    to_push: list[tuple[Job, bool]] = []
    text_rejected: list[str] = []
    unchecked = 0
    checks = 0
    for job, prio in candidates:
        if tf_on:
            if checks >= tf.get("max_checks_per_run", 20):
                continue  # niet als gezien markeren: volgende run
            checks += 1
            text = fetch_text(job.url)
            if text is None:
                unchecked += 1
                if tf.get("on_fetch_fail", "notify") == "skip":
                    state[job.id] = now
                    continue
                job.note = "⚠ tekst niet gecontroleerd, lees zelf na op ervaring"
            else:
                ok, reason = experience_verdict(text, tf)
                if not ok:
                    text_rejected.append(f"{job.title[:45]} [{reason}]")
                    state[job.id] = now
                    continue
                job.note = f"✓ {reason}" if reason else ""
        to_push.append((job, prio))

    pushed = 0
    for i, (job, prio) in enumerate(to_push):
        if first_run and i >= MAX_FIRST_RUN_PUSHES:
            state[job.id] = now  # eerste run: niet je telefoon overspoelen
            continue
        if dry:
            print(f"  [DRY] {'*' if prio else ' '} {job.source}: {job.title} {job.note} -> {job.url}")
        else:
            try:
                notify(topic, job, prio)
            except Exception as e:
                print(f"[FOUT] melding mislukt voor {job.url}: {e}", file=sys.stderr)
                continue  # niet als gezien markeren, volgende run opnieuw proberen
        state[job.id] = now
        pushed += 1

    _show("Afgewezen op titel", title_rejected)
    _show("Afgewezen op tekst", text_rejected)
    if not dry:
        save_state(state)
    print(f"Klaar: {len(candidates)} titel-matches, {len(text_rejected)} afgewezen op tekst, "
          f"{unchecked} tekst niet te lezen, {pushed} gemeld.")
    return 2 if errors else 0  # niet-nul exitcode -> GitHub mailt je dat er iets stuk is


# --------------------------------------------------------------------------
# Hulpmiddelen om bronnen af te stellen
# --------------------------------------------------------------------------
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


def diagnose() -> None:
    """Per bron: hoeveel mails/links, welke titels, en voorbeeldlinks die NIET matchen."""
    cfg = load_config()
    matcher = Matcher(cfg)
    for src in cfg.get("sources", []):
        if not src.get("enabled", True):
            continue
        print(f"\n=== {src['name']} ({src['type']})")
        try:
            if src["type"] == "mail":
                raws = fetch_mails(src["from_contains"], src.get("days", 3))
                print(f"{len(raws)} mails van '{src['from_contains']}' in de laatste {src.get('days', 3)} dagen")
                jobs, other, tracking = [], {}, ""
                rx = re.compile(src["link_regex"])
                for raw in raws:
                    jobs += parse_mail(raw, src)
                    msg = email.message_from_bytes(raw)
                    for part in msg.walk():
                        if part.get_content_type() == "text/html":
                            for a in BeautifulSoup(_decode_part(part), "html.parser").find_all("a", href=True):
                                if "cts.indeed.com/v3/" in a["href"] and not tracking:
                                    tracking = decode_tracking(a["href"])
                                if not rx.search(a["href"]):
                                    other.setdefault(a["href"][:90], anchor_title(a)[:50])
                if other:
                    print(f"  {len(other)} links die NIET matchen met link_regex (voorbeelden, link | tekst):")
                    for h, t in list(other.items())[:8]:
                        print(f"    {h} | {t}")
                if tracking:
                    shown = "".join(c if c.isprintable() else "." for c in tracking)[:400]
                    print(f"  Inhoud van een Indeed-tracking-link (gedecodeerd): {shown}")
            else:
                jobs = fetch_html_source(src)
            uniq = {j.id: j for j in jobs}
            print(f"{len(uniq)} unieke vacatures")
            for j in list(uniq.values())[:25]:
                ok, prio = matcher.check(j)
                print(f"  {'JA ' if ok else 'nee'}{'*' if prio else ' '} {j.title[:70]}")
        except Exception as e:
            print(f"FOUT: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--diagnose", action="store_true")
    ap.add_argument("--probe")
    ap.add_argument("--probe-mail")
    args = ap.parse_args()
    if args.probe:
        probe(args.probe)
    elif args.probe_mail:
        probe_mail(args.probe_mail)
    elif args.diagnose:
        diagnose()
    else:
        sys.exit(run(dry=args.dry_run))
