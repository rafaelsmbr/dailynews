import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify

app = Flask(__name__)

RSS_URL = "https://www.wired.com/feed/tag/ai/latest/rss"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

DB_PATH = os.environ.get("DB_PATH", "/tmp/wired_rss.db")

MEDIA_NS = "http://search.yahoo.com/mrss/"

# Registra namespaces para preservar prefixos no XML de saída
NS_MAP = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc":      "http://purl.org/dc/elements/1.1/",
    "media":   MEDIA_NS,
    "atom":    "http://www.w3.org/2005/Atom",
}
for prefix, uri in NS_MAP.items():
    ET.register_namespace(prefix, uri)

# ---------------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------------

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                guid          TEXT    UNIQUE NOT NULL,
                title         TEXT,
                link          TEXT,
                pub_date      TEXT,
                author        TEXT,
                description   TEXT,
                thumbnail_url TEXT,
                full_content  TEXT,
                raw_xml       TEXT,
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migração não-destrutiva: adiciona coluna se ainda não existir
        # (útil quando o banco já existe de uma versão anterior)
        try:
            conn.execute("ALTER TABLE articles ADD COLUMN thumbnail_url TEXT")
        except sqlite3.OperationalError:
            pass  # coluna já existe
        conn.commit()


init_db()

# ---------------------------------------------------------------------------
# Helpers de scraping
# ---------------------------------------------------------------------------

def fetch_article_content(url: str) -> str:
    """Busca e extrai o texto principal de um artigo."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "figure"]):
            tag.decompose()

        text_parts = []

        headline = soup.find(attrs={"data-testid": "ContentHeaderHed"})
        if headline:
            text_parts.append(headline.get_text(" ", strip=True))

        body = None
        for sel in [
            {"data-testid": "BodyWrapper"},
            {"class": re.compile(r"body__inner|article-body|content-body", re.I)},
            "article",
            "main",
        ]:
            body = soup.find(attrs=sel) if isinstance(sel, dict) else soup.find(sel)
            if body:
                break

        if body:
            text_parts.extend(
                p.get_text(" ", strip=True)
                for p in body.find_all("p")
                if p.get_text(strip=True)
            )

        if not text_parts:
            text_parts = [
                p.get_text(" ", strip=True)
                for p in soup.find_all("p")
                if p.get_text(strip=True)
            ]

        content = "\n\n".join(text_parts)
        return content if content.strip() else "Conteúdo não disponível."

    except Exception as exc:
        return f"Erro ao buscar conteúdo: {exc}"


def fetch_rss_xml() -> str:
    resp = requests.get(RSS_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text


def parse_items(raw_xml: str):
    """Faz parse do RSS e retorna (root, channel, [items])."""
    raw_ns = dict(re.findall(r'xmlns:?(\w*)=["\']([^"\']+)["\']', raw_xml))
    for prefix, uri in raw_ns.items():
        ET.register_namespace(prefix if prefix else "", uri)

    root    = ET.fromstring(raw_xml.encode("utf-8"))
    channel = root.find("channel")
    items   = channel.findall("item") if channel is not None else root.findall(".//item")
    return root, channel, items


def item_text(item, tag: str) -> str:
    el = item.find(tag)
    return (el.text or "").strip() if el is not None else ""


def extract_thumbnail(item) -> str:
    """
    Tenta extrair a URL do thumbnail de um <item> RSS.
    Verifica (em ordem de prioridade):
      1. <media:thumbnail url="..."/>
      2. <media:content url="..." medium="image"/>
      3. <enclosure url="..." type="image/..."/>
    """
    media_thumb = item.find(f"{{{MEDIA_NS}}}thumbnail")
    if media_thumb is not None:
        url = media_thumb.get("url", "").strip()
        if url:
            return url

    media_content = item.find(f"{{{MEDIA_NS}}}content")
    if media_content is not None:
        medium = media_content.get("medium", "")
        url    = media_content.get("url", "").strip()
        if url and medium == "image":
            return url

    enclosure = item.find("enclosure")
    if enclosure is not None:
        enc_type = enclosure.get("type", "")
        url      = enclosure.get("url", "").strip()
        if url and enc_type.startswith("image/"):
            return url

    return ""


# ---------------------------------------------------------------------------
# GET /new  – busca, scrapa e persiste
# ---------------------------------------------------------------------------

@app.route("/new")
def new_articles():
    try:
        raw_xml = fetch_rss_xml()
        _, _, items = parse_items(raw_xml)

        item_meta = []
        for item in items:
            guid      = item_text(item, "guid") or item_text(item, "link")
            link      = item_text(item, "link")
            title     = item_text(item, "title")
            pub       = item_text(item, "pubDate")
            author    = (item_text(item, f"{{{NS_MAP['dc']}}}creator")
                         or item_text(item, "author"))
            desc      = item_text(item, "description")
            thumbnail = extract_thumbnail(item)
            raw       = ET.tostring(item, encoding="unicode")
            item_meta.append(dict(
                guid=guid, link=link, title=title, pub_date=pub,
                author=author, description=desc, thumbnail_url=thumbnail, raw_xml=raw
            ))

        # Scraping paralelo
        def scrape(meta):
            meta["full_content"] = (
                fetch_article_content(meta["link"]) if meta["link"] else "Sem link."
            )
            return meta

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(scrape, m) for m in item_meta]
            results = [f.result() for f in as_completed(futures)]

        # Persiste
        saved = 0
        with get_db() as conn:
            for r in results:
                cursor = conn.execute("""
                    INSERT OR IGNORE INTO articles
                        (guid, title, link, pub_date, author, description,
                         thumbnail_url, full_content, raw_xml)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (r["guid"], r["title"], r["link"], r["pub_date"], r["author"],
                      r["description"], r["thumbnail_url"], r["full_content"], r["raw_xml"]))
                saved += cursor.rowcount
            conn.commit()

        return jsonify({
            "status":  "ok",
            "fetched": len(results),
            "saved":   saved,
            "skipped": len(results) - saved,
        })

    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /rss  – lê do banco e devolve XML enriquecido
# ---------------------------------------------------------------------------

@app.route("/rss")
def rss_feed():
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT title, link, guid, pub_date, author, description,
                       thumbnail_url, full_content
                FROM articles
                ORDER BY created_at DESC
            """).fetchall()

        rss = ET.Element("rss")
        rss.set("version", "2.0")

        channel = ET.SubElement(rss, "channel")
        ET.SubElement(channel, "title").text       = "Wired AI – enriched"
        ET.SubElement(channel, "link").text        = "https://www.wired.com/tag/ai/"
        ET.SubElement(channel, "description").text = "Wired AI feed with full article content"

        for row in rows:
            item = ET.SubElement(channel, "item")
            ET.SubElement(item, "title").text       = row["title"]       or ""
            ET.SubElement(item, "link").text        = row["link"]        or ""
            ET.SubElement(item, "guid").text        = row["guid"]        or ""
            ET.SubElement(item, "pubDate").text     = row["pub_date"]    or ""
            ET.SubElement(item, "author").text      = row["author"]      or ""
            ET.SubElement(item, "description").text = row["description"] or ""
            ET.SubElement(item, "fullContent").text = row["full_content"] or ""

            # Emite <media:thumbnail> somente se existir URL
            if row["thumbnail_url"]:
                thumb = ET.SubElement(item, f"{{{MEDIA_NS}}}thumbnail")
                thumb.set("url", row["thumbnail_url"])

        xml_str = ET.tostring(rss, encoding="unicode", xml_declaration=False)
        output  = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_str
        return Response(output, mimetype="application/xml; charset=utf-8")

    except Exception as exc:
        return Response(f"<e>{exc}</e>", status=500, mimetype="application/xml")


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    return jsonify({"status": "ok", "articles_in_db": count})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
