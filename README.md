# infrarot-fussboden.de - napi ár- és készletfeed

Forrás: a shop saját `llms-full.txt` katalógusa + sitemap (napi 2-3 kérés, nincs HTML-scraping).

Állandó linkek (raw, nem változik):
- `feed/feed.json` / `feed/feed.csv` - teljes katalógus (egyedi cikkszámok)
- `feed/unter-teppich-heizmatte.json` / `.csv` - csak a "Unter Teppich-Heizmatte" kategória
- `feed/changes.json` / `changes.csv` - napi változásnapló (ár, név, leírás, készlet, új/kivezetett cikkszám), 180 nap
- `data/descriptions.json` - a leírás-összehasonlításhoz használt szövegek

Ütemezés: GitHub Actions, naponta 05:30 UTC (07:30 nyári / 06:30 téli budapesti idő).
Hiba esetén a futás leáll és az utolsó jó feed marad kint.
