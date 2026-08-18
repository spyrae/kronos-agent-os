"""Tests for secure parsing of Yandex Search API XML responses."""

from __future__ import annotations

import base64

from kronos.seo_geo.trackers.yandex import _parse_xml_results


def _encode_xml(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def test_parse_xml_results_returns_ranked_urls() -> None:
    payload = _encode_xml(
        "<yandexsearch><response><results><grouping>"
        "<group><doc><url>https://example.com/one</url></doc></group>"
        "<group><doc><url>https://example.com/two</url></doc></group>"
        "</grouping></results></response></yandexsearch>"
    )

    assert _parse_xml_results(payload) == [
        "https://example.com/one",
        "https://example.com/two",
    ]


def test_parse_xml_results_rejects_external_entities() -> None:
    payload = _encode_xml(
        "<!DOCTYPE yandexsearch [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]>"
        "<yandexsearch><doc><url>&xxe;</url></doc></yandexsearch>"
    )

    assert _parse_xml_results(payload) == []
