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
    assert jobs["https://www.linkedin.com/comm/jobs/view/123456"] == "Nieuwe vacature: Junior Java Developer bij Acme"
    assert jobs["https://www.linkedin.com/comm/jobs/view/789"] == "Backend Developer"
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
