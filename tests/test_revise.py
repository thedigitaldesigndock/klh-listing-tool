"""Unit tests for pipeline.lister.build_revise_item_xml + merge_specifics."""

import pytest

from pipeline import lister


def test_revise_xml_title_only():
    xml = lister.build_revise_item_xml("123", new_title="Clean Title")
    assert "<ItemID>123</ItemID>" in xml
    assert "<Title>Clean Title</Title>" in xml
    assert "ItemSpecifics" not in xml


def test_revise_xml_specifics_only():
    xml = lister.build_revise_item_xml(
        "123", new_specifics_replace={"Sport": "Football", "Signed": "Yes"}
    )
    assert "<ItemID>123</ItemID>" in xml
    assert "Title" not in xml
    assert "<Name>Signed</Name>" in xml   # sorted alphabetically
    assert "<Value>Yes</Value>" in xml
    assert "<Name>Sport</Name>" in xml
    assert "<Value>Football</Value>" in xml


def test_revise_xml_both():
    xml = lister.build_revise_item_xml(
        "123",
        new_title="X",
        new_specifics_replace={"Sport": "Football"},
    )
    assert "<Title>X</Title>" in xml
    assert "<Name>Sport</Name>" in xml


def test_revise_xml_requires_something():
    with pytest.raises(lister.ListerError):
        lister.build_revise_item_xml("123")


def test_revise_xml_refuses_blank_title():
    with pytest.raises(lister.ListerError):
        lister.build_revise_item_xml("123", new_title="")


def test_revise_xml_refuses_long_title():
    with pytest.raises(lister.ListerError):
        lister.build_revise_item_xml("123", new_title="X" * 81)


def test_revise_xml_requires_item_id():
    with pytest.raises(lister.ListerError):
        lister.build_revise_item_xml("", new_title="whatever")


def test_revise_xml_escapes_ampersand_in_title():
    xml = lister.build_revise_item_xml("123", new_title="Tom & Jerry Signed")
    assert "Tom &amp; Jerry" in xml


def test_merge_specifics_adds_and_overwrites():
    merged = lister.merge_specifics(
        {"Sport": "Football", "Signed": "Yes"},
        {"Team": "Man Utd", "Sport": "Rugby"},
    )
    assert merged == {"Sport": "Rugby", "Signed": "Yes", "Team": "Man Utd"}


def test_merge_specifics_deletes_on_none():
    merged = lister.merge_specifics(
        {"Sport": "Football", "StaleKey": "junk"},
        {"StaleKey": None},
    )
    assert merged == {"Sport": "Football"}


def test_revise_listing_refuses_without_confirm():
    with pytest.raises(lister.ListerError):
        lister.revise_listing("123", new_title="whatever")
