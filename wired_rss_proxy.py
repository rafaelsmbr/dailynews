import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response

app = Flask(__name__)

RSS_URL = "https://www.wired.com/feed/tag/ai/latest/rss"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Namespaces commonly found in RSS feeds
NAMESPACES = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "media": "http://search.yahoo.com/mrss/",
    "atom": "http://www.w3.org/2005/Atom",
}

# Register namespaces so they are preserved in output
for prefix, uri in NAMESPACES.items():
    ET.register_namespace(prefix, uri)


def fetch_article_content(url: str) -> str:
    """Fetch and extract the main text content of an article page."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove noise elements
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "figure"]):
            tag.decompose()

        # Try common article body selectors
        selectors = [
            {"data-testid": "ContentHeaderHed"},  # Wired headline
            "article",
            {"class": re.compile(r"article[-_]?body|story[-_]?body|post[-_]?body", re.I)},
            "main",
        ]

        text_parts = []

        # Extract headline
        headline = soup.find(attrs={"data-testid": "ContentHeaderHed"})
        if headline:
            text_parts.append(headline.get_text(" ", strip=True))

        # Extract article body
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
            paragraphs = body.find_all("p")
            text_parts.extend(p.get_text(" ", strip=True) for p in paragraphs if p.get_text(strip=True))
        
        if not text_parts:
            # Fallback: grab all paragraphs
            paragraphs = soup.find_all("p")
            text_parts = [p.get_text(" ", strip=True) for p in paragraphs if p.get_text(strip=True)]

        content = "\n\n".join(text_parts)
        return content if content.strip() else "Conteúdo não disponível."

    except Exception as exc:
        return f"Erro ao buscar conteúdo: {exc}"


def fetch_rss() -> str:
    """Fetch the raw RSS XML from Wired."""
    resp = requests.get(RSS_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text


def enrich_rss(raw_xml: str) -> str:
    """Parse RSS XML, fetch each article's content and inject it as a new field."""
    # Register all namespaces found in the document to avoid ns0/ns1 renaming
    raw_namespaces = dict(re.findall(r'xmlns:?(\w*)=["\']([^"\']+)["\']', raw_xml))
    for prefix, uri in raw_namespaces.items():
        ET.register_namespace(prefix if prefix else "", uri)

    root = ET.fromstring(raw_xml.encode("utf-8"))
    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else root.findall(".//item")

    # Collect links to fetch in parallel
    links = {}
    for item in items:
        link_el = item.find("link")
        if link_el is not None and link_el.text:
            links[id(item)] = link_el.text.strip()

    # Fetch all article pages concurrently
    contents = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_to_item = {
            executor.submit(fetch_article_content, url): item_id
            for item_id, url in links.items()
        }
        for future in as_completed(future_to_item):
            item_id = future_to_item[future]
            contents[item_id] = future.result()

    # Inject <fullContent> element into each item
    for item in items:
        content_el = ET.SubElement(item, "fullContent")
        content_el.text = contents.get(id(item), "Link não encontrado.")

    # Serialize back to XML string
    xml_bytes = ET.tostring(root, encoding="unicode", xml_declaration=False)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_bytes


@app.route("/rss")
def rss_feed():
    try:
        raw_xml = fetch_rss()
        enriched_xml = enrich_rss(raw_xml)
        return Response(enriched_xml, mimetype="application/xml; charset=utf-8")
    except Exception as exc:
        return Response(f"<error>{exc}</error>", status=500, mimetype="application/xml")


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(debug=True, port=5000)