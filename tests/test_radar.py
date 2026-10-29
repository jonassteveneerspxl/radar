import json
import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import radar  # noqa: E402

VDAB_HTML = """
<html><body>
  <a href="/vindeenjob/vacatures/73914373/java-developer-met-analytische-vaardigheden">
     <img src="x.png"></a>
  <a href="/vindeenjob/vacatures/73914373/java-developer-met-analytische-vaardigheden?utm=abc">
     Java developer met analytische vaardigheden</a>
  <a href="/vindeenjob/vacatures/111/junior-java-developer">Junior Java Developer</a>
  <a href="/vindeenjob/vacatures/222/senior-java-architect">Senior Java Architect</a>
  <a href="/vindeenjob/vacatures/333/marketing-medewerker">Marketing medewerker</a>
  <a href="/over-ons">Over ons</a>
</body></html>
"""

SRC = {"name": "VDAB", "link_regex": r"/vindeenjob/vacatures/\d+"}
CFG = {
    "include": ["java", "developer"],
    "exclude": ["senior", "architect"],
    "boost": ["junior"],
}


def test_extract_links_dedupes_and_keeps_best_title():
    jobs = radar.extract_links(VDAB_HTML, "https://www.vdab.be/", SRC["link_regex"], "VDAB")
    urls = {j.url for j in jobs}
    assert len(jobs) == 4  # /over-ons valt weg, dubbele link wordt samengevoegd
    assert "https://www.vdab.be/vindeenjob/vacatures/73914373/java-developer-met-analytische-vaardigheden" in urls
    first = next(j for j in jobs if "73914373" in j.url)
    assert first.title == "Java developer met analytische vaardigheden"  # tekst wint van lege img-link


def test_filter():
    m = radar.Matcher(CFG)
    jobs = radar.extract_links(VDAB_HTML, "https://www.vdab.be/", SRC["link_regex"], "VDAB")
    res = {j.title: m.check(j) for j in jobs}
    assert res["Junior Java Developer"] == (True, True)
    assert res["Java developer met analytische vaardigheden"] == (True, False)
    assert res["Senior Java Architect"] == (False, False)
    assert res["Marketing medewerker"] == (False, False)


def test_javascript_is_not_java():
    m = radar.Matcher({"include": ["java"]})
    assert m.check(radar.Job("x", "JavaScript Developer", "u"))[0] is False
    assert m.check(radar.Job("x", "Java Developer", "u"))[0] is True


def test_keep_params_for_indeed():
    u = radar.normalize_url("https://be.indeed.com/rc/clk?jk=abc123&fccid=zzz&from=ja#x", ("jk",))
    assert u == "https://be.indeed.com/rc/clk?jk=abc123"


def test_parse_mail_uses_subject_when_anchor_is_generic():
    msg = EmailMessage()
    msg["Subject"] = "Nieuwe vacature: Junior Java Developer bij Acme"
    msg["From"] = "jobs-noreply@linkedin.com"
    msg.set_content("tekst")
    msg.add_alternative(
        '<a href="https://www.linkedin.com/comm/jobs/view/123456?trk=email">View job</a>'
        '<a href="https://www.linkedin.com/comm/jobs/view/789?trk=email">Backend Developer</a>'
        '<a href="https://www.linkedin.com/help">Help</a>',
        subtype="html",
    )
    src = {"name": "LinkedIn (mail)", "link_regex": r"linkedin\.com/(comm/)?jobs/view/\d+"}
    jobs = {j.url: j.title for j in radar.parse_mail(msg.as_bytes(), src)}
    assert jobs["https://www.linkedin.com/jobs/view/123456"] == "Nieuwe vacature: Junior Java Developer bij Acme"
    assert jobs["https://www.linkedin.com/jobs/view/789"] == "Backend Developer"
    assert len(jobs) == 2


def test_full_run_first_run_then_only_new(tmp_path, monkeypatch):
    sent = []
    pages = {"html": VDAB_HTML}
    cfg = {**CFG, "sources": [{"name": "VDAB", "type": "html", "url": "https://x/", **SRC}]}

    monkeypatch.setattr(radar, "STATE_FILE", tmp_path / "state" / "seen.json")
    monkeypatch.setattr(radar, "load_config", lambda: cfg)
    monkeypatch.setattr(radar, "notify", lambda topic, job, prio: sent.append((job.title, prio)))
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")

    class R:
        status_code = 200

        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    monkeypatch.setattr(radar.requests, "get", lambda *a, **k: R(pages["html"]))

    assert radar.run() == 0
    assert sorted(t for t, _ in sent) == ["Java developer met analytische vaardigheden", "Junior Java Developer"]
    assert sent[0] == ("Junior Java Developer", True)  # prioriteit eerst

    sent.clear()
    assert radar.run() == 0
    assert sent == []  # niets nieuws -> geen meldingen

    pages["html"] = VDAB_HTML + '<a href="/vindeenjob/vacatures/444/java-starter">Java starter</a>'
    assert radar.run() == 0
    assert sent == [("Java starter", False)]

    state = json.loads((tmp_path / "state" / "seen.json").read_text())
    assert len(state) == 5


def test_failed_notification_is_retried(tmp_path, monkeypatch):
    cfg = {**CFG, "sources": [{"name": "VDAB", "type": "html", "url": "https://x/", **SRC}]}
    monkeypatch.setattr(radar, "STATE_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(radar, "load_config", lambda: cfg)
    monkeypatch.setenv("NTFY_TOPIC", "t")

    class R:
        text = VDAB_HTML

        def raise_for_status(self):
            pass

    monkeypatch.setattr(radar.requests, "get", lambda *a, **k: R())

    def boom(*a, **k):
        raise RuntimeError("ntfy down")

    monkeypatch.setattr(radar, "notify", boom)
    radar.run()
    state = json.loads((tmp_path / "seen.json").read_text())
    assert not any("111" in u for u in state)  # mislukte melding is niet als gezien gemarkeerd


# ---------------------------------------------------------------- tekstfilter
TF = {"max_years": 1, "reject_phrases": ["bewezen ervaring"]}


def v(text):
    return radar.experience_verdict(text, TF)[0]


def test_experience_rejects_required_years():
    assert v("Je hebt minimaal 3 jaar ervaring met Java.") is False
    assert v("Minimum 2 years of experience with Spring Boot") is False
    assert v("3+ years of relevant experience") is False
    assert v("Ervaring: 2-4 jaar in een vergelijkbare functie") is False
    assert v("Vous avez 5 ans d'expérience en Java") is False


def test_experience_accepts_juniors():
    assert v("0-2 jaar ervaring is voldoende") is True
    assert v("1 jaar ervaring is welkom") is True
    assert v("Je bent een starter met een bachelor in informatica.") is True
    assert v("Wij bestaan al 20 jaar en groeien snel.") is True  # jaren buiten ervaring-zin


def test_experience_soft_and_starter_overrides():
    assert v("3 jaar ervaring is een pluspunt.") is True
    assert v("5 years of experience is a plus") is True
    assert v("Geen ervaring vereist, starters welkom. Ervaring van 3 jaar met Java.") is True
    assert v("Starters zijn welkom! We vragen 4 jaar ervaring.") is True


def test_reject_phrases():
    assert v("Je beschikt over bewezen ervaring als developer.") is False


def test_title_excludes_experienced():
    m = radar.Matcher({"include": ["developer"], "exclude": ["ervaren", "medior"]})
    assert m.check(radar.Job("x", "Ervaren Java Developer", "u"))[0] is False
    assert m.check(radar.Job("x", "Medior Developer", "u"))[0] is False


def test_run_text_filter_and_fetch_failure(tmp_path, monkeypatch):
    sent = []
    pages = {
        "https://www.vdab.be/vindeenjob/vacatures/111/junior-java-developer": "Starters welkom. " * 40,
        "https://www.vdab.be/vindeenjob/vacatures/73914373/java-developer-met-analytische-vaardigheden":
            "Minimaal 4 jaar ervaring met Java. " * 20,
    }
    cfg = {**CFG, "text_filter": {"enabled": True, "max_years": 1, "on_fetch_fail": "notify"},
           "sources": [{"name": "VDAB", "type": "html", "url": "https://www.vdab.be/", **SRC}]}
    monkeypatch.setattr(radar, "STATE_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(radar, "load_config", lambda: cfg)
    monkeypatch.setattr(radar, "notify", lambda t, job, p: sent.append((job.title, job.note)))
    monkeypatch.setattr(radar, "fetch_text", lambda url: pages.get(url))
    monkeypatch.setenv("NTFY_TOPIC", "t")

    class R:
        text = VDAB_HTML

        def raise_for_status(self):
            pass

    monkeypatch.setattr(radar.requests, "get", lambda *a, **k: R())
    radar.run()
    assert [t for t, _ in sent] == ["Junior Java Developer"]  # 4 jaar ervaring afgewezen

    # fetch mislukt -> toch melden, met waarschuwing
    sent.clear()
    pages.clear()
    (tmp_path / "seen.json").unlink()
    radar.run()
    assert len(sent) == 2 and all("niet gecontroleerd" in n for _, n in sent)


def test_imap_login_failure_is_reported_once(monkeypatch, capsys):
    radar._imap_cache.clear()
    monkeypatch.delenv("IMAP_USER", raising=False)
    cfg = {"sources": [{"name": f"M{i}", "type": "mail", "from_contains": "x", "link_regex": "x"}
                       for i in range(3)]}
    errors = []
    radar.collect(cfg, errors)
    assert errors == ["mail"]  # één melding, niet drie
    assert capsys.readouterr().err.count("[FOUT]") == 1
    radar._imap_cache.clear()


# ------------------------------------------------- titel uit mail-kaartjes / Indeed
def test_card_anchor_uses_title_only_not_company():
    html = ('<a href="https://www.linkedin.com/comm/jobs/view/42?trk=x">'
            '<div>Data Analyst</div><div>NTek Software Solutions · Mechelen (Hybrid)</div>'
            '<span>Easy Apply</span></a>')
    jobs = radar.extract_links(html, "", r"linkedin\.com/(comm/)?jobs/view/\d+", "LinkedIn")
    assert [j.title for j in jobs] == ["Data Analyst"]
    m = radar.Matcher({"include": ["developer", "engineer"], "exclude": ["business developer"]})
    assert m.check(jobs[0])[0] is False  # "Software" in de bedrijfsnaam telt niet mee
    assert m.check(radar.Job("x", "Business Developer & Ondernemer", "u"))[0] is False
    assert m.check(radar.Job("x", "(Junior) Engineer", "u"))[0] is True


def test_indeed_tracking_links_dedupe_by_title():
    html = ('<a href="https://cts.indeed.com/v3/AAAA?x=1">Java Developer</a>'
            '<a href="https://cts.indeed.com/v3/BBBB?x=2">Java Developer</a>'
            '<a href="https://cts.indeed.com/v3/CCCC?x=3"><img src="logo.png"></a>'
            '<a href="https://cts.indeed.com/v3/DDDD?x=4">Backend Developer</a>')
    jobs = radar.extract_links(html, "", r"cts\.indeed\.com/v3/", "Indeed (mail)", key_by_title=True)
    ids = {j.id for j in jobs}
    assert ids == {"Indeed (mail)|java developer", "Indeed (mail)|backend developer"}
    assert all(j.url.startswith("https://cts.indeed.com/v3/") for j in jobs)  # link blijft klikbaar


def test_indeed_tracking_with_job_id_becomes_clean_url():
    import base64
    import gzip
    payload = base64.urlsafe_b64encode(gzip.compress(b"foo&jk=0123456789abcdef&bar")).decode().rstrip("=")
    html = f'<a href="https://cts.indeed.com/v3/{payload}?z=1">Java Developer</a>'
    jobs = radar.extract_links(html, "", r"cts\.indeed\.com/v3/", "Indeed (mail)", key_by_title=True)
    assert jobs[0].url == "https://be.indeed.com/viewjob?jk=0123456789abcdef"
    assert jobs[0].id == jobs[0].url
