"""Yandex Search API v2 tracker — async submit + poll for organic results.

Uses Yandex Cloud Search API v2 (https://yandex.cloud/docs/search-api).
Requires service account API key with `search-api.executor` role and
the folder ID.

Env vars:
- YANDEX_SEARCH_API_KEY
- YANDEX_FOLDER_ID
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

log = logging.getLogger("kronos.seo_geo.trackers.yandex")

_TIMEOUT = 20
_POLL_INTERVAL = 2
_POLL_MAX = 30  # max 60s wait


def _api_key() -> str:
    return os.environ.get("YANDEX_SEARCH_API_KEY") or ""


def _folder_id() -> str:
    return os.environ.get("YANDEX_FOLDER_ID") or ""


def _submit_search(query: str, locale: str = "ru") -> str | None:
    """Submit async search, return operation id."""
    api_key = _api_key()
    folder_id = _folder_id()
    if not api_key or not folder_id:
        return None

    # Region-aware: RU for ru-locale (yandex.ru), TR/COM for global.
    search_type = "SEARCH_TYPE_RU" if locale == "ru" else "SEARCH_TYPE_COM"

    body = json.dumps(
        {
            "query": {
                "search_type": search_type,
                "query_text": query,
                "page": 0,
            },
            "folder_id": folder_id,
            # FORMAT_XML returns structured data (<yandexsearch>/<doc>/<url>);
            # FORMAT_HTML returns a full HTML SERP page that is brittle to scrape.
            "response_format": "FORMAT_XML",
        }
    ).encode()

    req = urllib.request.Request(
        "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Api-Key {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read())
        return data.get("id")
    except HTTPError as e:
        body = e.read()[:200].decode("utf-8", errors="replace")
        log.warning("Yandex submit HTTP %d: %s", e.code, body)
        return None
    except Exception as e:
        log.warning("Yandex submit failed: %s", e)
        return None


def _poll_operation(op_id: str) -> dict | None:
    """Poll the operation until done. Return result payload or None."""
    api_key = _api_key()
    url = f"https://operation.api.cloud.yandex.net/operations/{op_id}"
    req_factory = lambda: urllib.request.Request(  # noqa: E731
        url,
        headers={"Authorization": f"Api-Key {api_key}"},
    )

    for _ in range(_POLL_MAX):
        try:
            with urllib.request.urlopen(req_factory(), timeout=_TIMEOUT) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            log.debug("Yandex poll error: %s", e)
            time.sleep(_POLL_INTERVAL)
            continue
        if data.get("done"):
            return data
        time.sleep(_POLL_INTERVAL)
    log.warning("Yandex operation timed out: %s", op_id)
    return None


def _parse_xml_results(b64_xml: str) -> list[str]:
    """Decode base64 XML response → list of organic result URLs in order."""
    try:
        xml = base64.b64decode(b64_xml).decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Yandex base64 decode failed: %s", e)
        return []
    try:
        root = ElementTree.fromstring(xml)
    except (DefusedXmlException, ElementTree.ParseError) as e:
        log.warning("Yandex XML parse failed: %s", e)
        return []
    urls: list[str] = []
    for doc in root.iter("doc"):
        url_el = doc.find("url")
        if url_el is not None and url_el.text:
            urls.append(url_el.text.strip())
    return urls


def find_position(target_url: str, query: str, locale: str = "ru") -> tuple[int | None, str | None]:
    """Return (position_1_indexed, ranked_url) or (None, None) if not in top results."""
    target_host = urllib.parse.urlparse(target_url).netloc.lower().replace("www.", "")
    if not target_host:
        return None, None

    op_id = _submit_search(query, locale=locale)
    if not op_id:
        return None, None
    op = _poll_operation(op_id)
    if not op or op.get("error"):
        if op and op.get("error"):
            log.warning("Yandex error: %s", op["error"].get("message", ""))
        return None, None
    response = op.get("response", {})
    b64_xml = response.get("rawData") or response.get("raw_data") or ""
    if not b64_xml:
        return None, None
    urls = _parse_xml_results(b64_xml)
    for idx, u in enumerate(urls, start=1):
        if target_host in u.lower():
            return idx, u
    return None, None


def engine_id(locale: str = "ru") -> str:
    return "yandex_ru"
