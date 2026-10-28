# Vacature-radar

Controleert om de ~15 minuten vacaturesites en je alert-mails, filtert op jouw zoektermen en stuurt een pushmelding naar je gsm zodra er iets nieuws is. Draait gratis op GitHub Actions. Je solliciteert zelf; de radar zorgt alleen dat je er als eerste bij bent.

## Installatie (±20 min)

### 1. Meldingen op je gsm (ntfy, gratis, geen account)
1. Installeer de app **ntfy** (Android/iOS).
2. Kies een lange, willekeurige topicnaam, bv. `jonas-radar-k39xq82vlm`. Wie de naam kent kan meelezen, dus niet iets raadbaars.
3. In de app: *Subscribe to topic* en vul die naam in.

### 2. GitHub-repo
1. Maak een nieuwe repo en zet deze map erin.
2. Kies **public** voor onbeperkte gratis Actions-minuten. Je topic en wachtwoord staan niet in de code maar in *Secrets*. Een private repo kan ook, maar zet dan de cron op `*/30` (gratis limiet: 2000 min/maand).
3. *Settings → Secrets and variables → Actions → New repository secret*:
   - `NTFY_TOPIC`: je topicnaam
   - `IMAP_USER`: je Gmail-adres
   - `IMAP_PASSWORD`: een **app-wachtwoord** van Google (Google-account → Beveiliging → 2-stapsverificatie aan → App-wachtwoorden). Niet je gewone wachtwoord. In Gmail moet IMAP aan staan.
4. Tabblad *Actions* → workflow **radar** → *Run workflow* om te testen.

De eerste run meldt maximaal 10 vacatures en markeert de rest als gezien, zodat je telefoon niet ontploft.

### 3. Alerts op de sites (voor LinkedIn, Indeed, Jobat, ICTjob, VDAB)
Maak per site een jobalert met je zoektermen (bv. "java junior", "software developer") en laat die naar je Gmail sturen. De radar leest die mails en meldt de vacatures meteen. Let op: de **snelheid hangt af van de alertfrequentie** van de site; kies overal de snelste optie ("direct"/"zodra beschikbaar" als die er is, anders dagelijks).

## Tekstfilter (geen vacatures die ervaring eisen)

Na de titelcheck leest de radar de vacaturepagina en wijst hij af wat meer ervaring vraagt dan `max_years` (standaard 1) in `config.yaml`. "3 jaar ervaring", "minimum 2 years of experience" en "2-4 jaar" worden afgewezen (ook in het Frans). "Is een pluspunt" of "nice to have" telt niet mee, en "starters welkom" of "geen ervaring vereist" laat de vacature altijd door.

Let op: LinkedIn en Indeed blokkeren vaak de servers van GitHub. Dan kan de tekst niet gelezen worden en krijg je de melding toch, met de waarschuwing "⚠ tekst niet gecontroleerd". Wil je die liever niet, zet dan `on_fetch_fail: skip` (je mist dan wel vacatures).

## Controleren wat er binnenkomt

*Actions → radar → Run workflow → mode: diagnose.* De log toont per bron hoeveel mails en vacatures er zijn, welke titels door het filter komen (JA/nee) en, als een mail geen enkele herkende link bevat, voorbeeldlinks om de `link_regex` aan te passen. Een normale run toont daarnaast welke titels en teksten zijn afgewezen, zodat je ziet of het filter te streng is.

Meldt een run `[FOUT] mail-login/lezen mislukt`, dan krijg je ook een mail van GitHub, want de run eindigt dan als mislukt.

## Afstellen

Alles staat in `config.yaml`:
- `include` / `exclude` / `boost`: woorden in de titel.
- `sources`: pagina's en mail-afzenders.

Hulpmiddelen (lokaal draaien, `pip install -r requirements.txt`):
```
python radar.py --probe "<zoekpagina-url>"   # toont alle links op de pagina -> kies link_regex
python radar.py --probe-mail linkedin.com    # toont links in je recente alert-mails
python radar.py --dry-run                    # toont wat er gemeld zou worden, zonder te sturen
python -m pytest tests                       # tests
```
(`--probe-mail` en `--dry-run` met mail-bronnen vragen `IMAP_USER` en `IMAP_PASSWORD` als omgevingsvariabelen.)

## Bekende beperkingen
- **Directe pagina's** (VDAB, evt. ICTjob/Jobat): werken alleen als de lijst zonder JavaScript in de HTML staat. Toont de log "0 links", gebruik dan de mailalert van die site.
- **LinkedIn en Indeed** staan scraping niet toe en blokkeren bots. Daarom enkel via e-mailalerts.
- De `link_regex` voor LinkedIn/Indeed/ICTjob/Jobat-mails is een eerste gok. Controleer met `--probe-mail` en pas aan.
- GitHub Actions voert `*/15` soms enkele minuten later uit. Ook pauzeert GitHub geplande workflows in repo's zonder activiteit; controleer af en toe in het tabblad Actions of de radar nog draait.

## Tip
Een melding is pas waardevol als je er snel op reageert. Zet een korte standaardmail en je cv klaar, zodat je binnen 10 minuten kan solliciteren, en pas de eerste twee zinnen aan per vacature.
