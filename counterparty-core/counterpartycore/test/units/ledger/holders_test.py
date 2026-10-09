import pytest
from counterpartycore.lib import exceptions
from counterpartycore.lib.ledger import holders


def test_is_valid_asset_name():
    # base26: 4-12 uppercase letters, not starting with A
    assert holders.is_valid_asset_name("FOOBAR") is True
    assert holders.is_valid_asset_name("ABCD") is False  # A is reserved for numerics
    assert holders.is_valid_asset_name("XYZ") is False  # too short
    assert holders.is_valid_asset_name("BCDEFGHIJKLMN") is False  # too long
    assert holders.is_valid_asset_name("FOOBAR1") is False  # digit not allowed in base26

    # numeric: A + 17-20 digits
    assert holders.is_valid_asset_name("A95428959342453541") is True
    assert holders.is_valid_asset_name("A123") is False  # too few digits

    # subasset longnames
    assert holders.is_valid_asset_name("PARENT.child") is True
    assert holders.is_valid_asset_name("PARENT.") is False  # ends with a period
    assert holders.is_valid_asset_name("PARENT..child") is False  # consecutive periods

    # BTC/XCP are never holders
    assert holders.is_valid_asset_name("BTC") is False
    assert holders.is_valid_asset_name("XCP") is False

    # addresses and non-strings
    assert holders.is_valid_asset_name("mn6q3dS2EnDUx3bmyWc6D4szJNVGtaR7zc") is False
    assert holders.is_valid_asset_name(123) is False
    assert holders.is_valid_asset_name(None) is False


def test_resolve_holder(monkeypatch):
    monkeypatch.setattr(holders, "resolve_subasset_longname", lambda db, name: name)
    assert holders.resolve_holder(None, "FOOBAR") == "FOOBAR"
    assert holders.resolve_holder(None, "A95428959342453541") == "A95428959342453541"

    # a longname is resolved to its numeric name
    monkeypatch.setattr(
        holders, "resolve_subasset_longname", lambda db, name: "A95428959342453541"
    )
    assert holders.resolve_holder(None, "PARENT.child") == "A95428959342453541"

    with pytest.raises(exceptions.AssetNameError):
        holders.resolve_holder(None, "not an asset")

    with pytest.raises(exceptions.AssetNameError):
        holders.resolve_holder(None, 123)


def test_is_asset_holder_eligible(monkeypatch):
    monkeypatch.setattr(
        holders,
        "get_asset",
        lambda db, asset: {"locked": True, "supply": 1, "divisible": False},
    )
    assert holders.is_asset_holder_eligible(None, "FOOBAR") is True

    monkeypatch.setattr(
        holders,
        "get_asset",
        lambda db, asset: {"locked": False, "supply": 1, "divisible": False},
    )
    assert holders.is_asset_holder_eligible(None, "FOOBAR") is False

    monkeypatch.setattr(
        holders,
        "get_asset",
        lambda db, asset: {"locked": True, "supply": 2, "divisible": False},
    )
    assert holders.is_asset_holder_eligible(None, "FOOBAR") is False

    monkeypatch.setattr(
        holders,
        "get_asset",
        lambda db, asset: {"locked": True, "supply": 1, "divisible": True},
    )
    assert holders.is_asset_holder_eligible(None, "FOOBAR") is False

    monkeypatch.setattr(holders, "get_asset", lambda db, asset: None)
    assert holders.is_asset_holder_eligible(None, "FOOBAR") is False

    # BTC/XCP are rejected before any lookup
    assert holders.is_asset_holder_eligible(None, "BTC") is False
    assert holders.is_asset_holder_eligible(None, "XCP") is False


def test_check_source_holder(monkeypatch):
    monkeypatch.setattr(
        holders,
        "get_asset",
        lambda db, asset: {"locked": True, "supply": 1, "divisible": False},
    )
    monkeypatch.setattr(holders, "get_balance", lambda db, address, asset: 1)
    assert holders.check_source_holder(None, "addr", "FOOBAR") == (True, None)

    # the signer does not hold the 1-of-1
    monkeypatch.setattr(holders, "get_balance", lambda db, address, asset: 0)
    ok, problem = holders.check_source_holder(None, "addr", "FOOBAR")
    assert ok is False
    assert "does not hold the 1-of-1" in problem

    # the asset is not an eligible holder
    monkeypatch.setattr(
        holders,
        "get_asset",
        lambda db, asset: {"locked": False, "supply": 1, "divisible": False},
    )
    ok, problem = holders.check_source_holder(None, "addr", "FOOBAR")
    assert ok is False
    assert "not an eligible asset holder" in problem


def test_get_holder_owner(monkeypatch):
    monkeypatch.setattr(
        holders,
        "get_asset_balances",
        lambda db, asset, exclude_zero_balances=True: [{"address": "addr1", "quantity": 1}],
    )
    assert holders.get_holder_owner(None, "FOOBAR") == "addr1"

    # ambiguous ownership
    monkeypatch.setattr(
        holders,
        "get_asset_balances",
        lambda db, asset, exclude_zero_balances=True: [
            {"address": "addr1", "quantity": 1},
            {"address": "addr2", "quantity": 1},
        ],
    )
    assert holders.get_holder_owner(None, "FOOBAR") is None

    # UTXO-held 1-of-1 (no address)
    monkeypatch.setattr(
        holders,
        "get_asset_balances",
        lambda db, asset, exclude_zero_balances=True: [{"address": None, "quantity": 1}],
    )
    assert holders.get_holder_owner(None, "FOOBAR") is None
