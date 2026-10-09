"""Helpers for assets that hold other assets ("holders").

A *holder* is a locked, indivisible, supply-1 asset whose single unit is held
as a regular address balance. Control of the holder belongs to whoever holds
that single unit (the 1-of-1 owner), not to the issuer. A holder may be the
source and/or the destination of an ``enhanced_send``, an MPMA send or a
``sweep``.

The ledger keys everything by the numeric asset name (``A<digits>``), never the
user-facing longname (``PARENT.child``); any longname is resolved to the numeric
name before a ledger operation. A holder name is therefore at most 21 chars.
"""

import logging

from counterpartycore.lib import config, exceptions
from counterpartycore.lib.ledger.balances import get_asset_balances, get_balance
from counterpartycore.lib.ledger.issuances import get_asset, resolve_subasset_longname
from counterpartycore.lib.utils import assetnames

logger = logging.getLogger(config.LOGGER_NAME)


def is_valid_asset_name(name):
    """Pure format check for an asset name (no DB handle available).

    Accepts base26 names (4-12 uppercase letters, not starting with ``A``),
    numeric names (``A<digits>`` in range) and subasset longnames
    (``PARENT.child``). ``BTC``/``XCP`` are not holders.
    """
    if not isinstance(name, str):
        return False
    if name in (config.BTC, config.XCP):
        return False
    if "." in name:
        try:
            assetnames.parse_subasset_from_asset_name(name, True)
            return True
        except Exception:  # pylint: disable=broad-except
            return False
    if assetnames.is_numeric(name):
        return True
    if 4 <= len(name) <= 12 and name[0] != "A" and all(c in assetnames.B26_DIGITS for c in name):
        return True
    return False


def resolve_holder(db, name):
    """Resolve a holder name (possibly a subasset longname) to its numeric name.

    Raises ``AssetNameError`` when ``name`` is not a valid asset name.
    """
    if not isinstance(name, str):
        raise exceptions.AssetNameError("holder must be an asset name")
    resolved = resolve_subasset_longname(db, name)
    if not is_valid_asset_name(resolved):
        raise exceptions.AssetNameError(f"invalid asset name: {name}")
    return resolved


def is_asset_holder_eligible(db, asset):
    """A holder must be a locked, indivisible, supply-1 asset (not BTC/XCP)."""
    if asset in (config.BTC, config.XCP):
        return False
    asset_info = get_asset(db, asset)
    if asset_info is None:
        return False
    return bool(asset_info["locked"]) and asset_info["supply"] == 1 and not asset_info["divisible"]


def check_source_holder(db, tx_source, source_asset):
    """Check that ``tx_source`` (the signer) holds the 1-of-1 ``source_asset``.

    Returns ``(ok, problem)``. Nested holders (a 1-of-1 held by another asset)
    and UTXO-held 1-of-1s fail the balance check automatically.
    """
    if not is_asset_holder_eligible(db, source_asset):
        return False, f"{source_asset} is not an eligible asset holder"
    if get_balance(db, tx_source, source_asset) != 1:
        return False, f"source does not hold the 1-of-1 {source_asset}"
    return True, None


def get_holder_owner(db, asset):
    """Return the address currently holding the single unit of ``asset``.

    Returns ``None`` when the 1-of-1 is not held as a regular address balance
    (e.g. attached to a UTXO) or when the ownership is ambiguous.
    """
    owners = [
        balance["address"]
        for balance in get_asset_balances(db, asset, exclude_zero_balances=True)
        if balance["quantity"] == 1 and balance["address"] is not None
    ]
    if len(owners) == 1:
        return owners[0]
    return None
