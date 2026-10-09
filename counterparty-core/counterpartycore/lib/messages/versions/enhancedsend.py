#! /usr/bin/python3

import logging
import struct

import cbor2

from counterpartycore.lib import config, exceptions, ledger
from counterpartycore.lib.messages.versions import send1
from counterpartycore.lib.parser import messagetype, protocol
from counterpartycore.lib.utils import address, helpers

logger = logging.getLogger(config.LOGGER_NAME)

FORMAT = ">QQ21s"
LENGTH = 8 + 8 + 21
MAX_MEMO_LENGTH = 34
ID = 2  # 0x02


def new_unpack(message, block_index=None):
    try:
        unpacked = cbor2.loads(message)
        if not isinstance(unpacked, list) or len(unpacked) not in (4, 5):
            raise exceptions.UnpackError("invalid message length")
        # The 5th element (the source holder) is only honoured once the
        # protocol change is active; a pre-activation 5-element message must be
        # rejected rather than silently accepted.
        if len(unpacked) == 5 and not protocol.enabled(
            "assets_hold_assets", block_index=block_index
        ):
            raise exceptions.UnpackError("invalid message length")

        asset_id, quantity, destination, memo_bytes = unpacked[0:4]
        source_asset_id = unpacked[4] if len(unpacked) == 5 else None

        asset = ledger.issuances.generate_asset_name(asset_id)
        if asset == config.BTC:
            raise exceptions.AssetNameError(f"{config.BTC} not allowed")

        # CBOR preserves the type: a packed-address is bytes, a holder is a
        # (numeric) asset-name string.
        destination_is_holder = isinstance(destination, str)
        if destination_is_holder:
            full_address = destination
        else:
            full_address = address.unpack(destination)

        source_asset = None
        if source_asset_id is not None:
            if source_asset_id in (0, 1):
                raise exceptions.AssetNameError("invalid source asset id")
            source_asset = ledger.issuances.generate_asset_name(source_asset_id)

        result = {
            "asset": asset,
            "quantity": quantity,
            "address": full_address,
            "memo": None if memo_bytes == b"" else memo_bytes,
        }
        if destination_is_holder:
            result["destination_is_holder"] = True
        if source_asset is not None:
            result["source_asset"] = source_asset
        return result
    except Exception as e:  # pylint: disable=broad-exception-caught
        raise exceptions.UnpackError(f"could not unpack: {e}") from e


def unpack(message, block_index=None):
    if protocol.enabled("taproot_support", block_index=block_index):
        try:
            return new_unpack(message, block_index=block_index)
        except exceptions.UnpackError:
            pass  # fallback to legacy unpacking

    try:
        # account for memo bytes
        memo_bytes_length = len(message) - LENGTH
        if memo_bytes_length < 0:
            raise exceptions.UnpackError("invalid message length")
        if memo_bytes_length > MAX_MEMO_LENGTH:
            raise exceptions.UnpackError("memo too long")

        struct_format = FORMAT + f"{memo_bytes_length}s"
        asset_id, quantity, short_address_bytes, memo_bytes = struct.unpack(struct_format, message)
        if len(memo_bytes) == 0:
            memo_bytes = None

        # unpack address
        full_address = address.unpack(short_address_bytes)

        # asset id to name
        asset = ledger.issuances.generate_asset_name(asset_id)
        if asset == config.BTC:
            raise exceptions.AssetNameError(f"{config.BTC} not allowed")

    except (struct.error, TypeError) as e:
        logger.trace(f"enhanced send unpack error: {e}")
        raise exceptions.UnpackError(f"could not unpack: {e}")  # noqa: B904

    except (exceptions.AssetNameError, exceptions.AssetIDError) as e:
        logger.trace(f"enhanced send invalid asset id: {e}")
        raise exceptions.UnpackError("asset id invalid")  # noqa: B904

    unpacked = {
        "asset": asset,
        "quantity": quantity,
        "address": full_address,
        "memo": memo_bytes,
    }
    return unpacked


def validate(db, destination, asset, quantity, memo_bytes, source_asset=None):
    problems = []

    if asset == config.BTC:
        problems.append(f"cannot send {config.BTC}")

    if not isinstance(quantity, int):
        problems.append("quantity must be in satoshis")
        return problems

    if quantity < 0:
        problems.append("negative quantity")

    if quantity == 0:
        problems.append("zero quantity")

    # For SQLite3
    if quantity > config.MAX_INT:
        problems.append("integer overflow")

    # destination is always required
    if not destination:
        problems.append("destination is required")
    elif isinstance(destination, str) and ledger.holders.is_valid_asset_name(destination):
        # A holder destination only needs to be eligible; ownership is not
        # required to receive.
        if not ledger.holders.is_asset_holder_eligible(db, destination):
            problems.append(f"{destination} is not an eligible asset holder")

    # A send from an asset to itself is a no-op and is rejected.
    if source_asset is not None and destination == source_asset:
        problems.append("source and destination cannot be the same asset")

    # check memo
    if memo_bytes is not None:
        # CBOR-encoded messages can carry any type here; reject anything
        # that is not bytes-like before calling len() on it.
        if not isinstance(memo_bytes, (bytes, bytearray)):
            problems.append("memo must be bytes")
        elif len(memo_bytes) > MAX_MEMO_LENGTH:
            problems.append("memo is too long")

    if protocol.enabled("options_require_memo"):
        cursor = db.cursor()
        try:
            results = ledger.other.get_addresses(db, address=destination)
            if results:
                result = results[0]
                if result and helpers.active_options(
                    result["options"], config.ADDRESS_OPTION_REQUIRE_MEMO
                ):
                    if memo_bytes is None or (len(memo_bytes) == 0):
                        problems.append("destination requires memo")
        finally:
            cursor.close()

    return problems


def compose(
    db,
    source: str,
    destination: str,
    asset: str,
    quantity: int,
    memo: str,
    memo_is_hex: bool,
    skip_validation: bool = False,
    no_dispense: bool = False,
):
    send1.validate_compose_quantity(quantity)

    if memo_is_hex and not isinstance(memo_is_hex, bool):
        raise exceptions.ComposeError("`memo_is_hex` must be a boolean")

    cursor = db.cursor()

    # Just send BTC?
    if asset == config.BTC:
        # try to compose a dispense instead
        return send1.compose_send_btc(db, source, destination, quantity, no_dispense)

    # resolve subassets
    asset = ledger.issuances.resolve_subasset_longname(db, asset)

    # A holder may be the logical source: control belongs to the 1-of-1 owner,
    # who signs the transaction.
    source_asset = None
    signing_source = source
    if isinstance(source, str) and ledger.holders.is_valid_asset_name(source):
        source_asset = ledger.holders.resolve_holder(db, source)
        owner = ledger.holders.get_holder_owner(db, source_asset)
        if owner is None:
            if not skip_validation:
                raise exceptions.ComposeError(f"cannot resolve the 1-of-1 owner of {source_asset}")
        else:
            signing_source = owner

    # A holder may be the destination.
    destination_is_holder = isinstance(
        destination, str
    ) and ledger.holders.is_valid_asset_name(destination)
    if destination_is_holder:
        destination = ledger.holders.resolve_holder(db, destination)

    # Only for outgoing (incoming will overburn).
    balance = ledger.balances.get_balance(db, source_asset or signing_source, asset)
    if balance < quantity and not skip_validation:
        raise exceptions.ComposeError("insufficient funds")

    if memo is not None and not isinstance(memo, str):
        raise exceptions.ComposeError("memo must be a string")

    # convert memo to memo_bytes based on memo_is_hex setting
    if memo is None:
        memo_bytes = b""
    elif memo_is_hex:
        try:
            memo_bytes = bytes.fromhex(memo)
        except (TypeError, ValueError) as e:
            raise exceptions.ComposeError("memo must be valid hexadecimal") from e
    else:
        memo_bytes = memo.encode("utf-8")
        memo_bytes = struct.pack(f">{len(memo_bytes)}s", memo_bytes)

    problems = validate(db, destination, asset, quantity, memo_bytes, source_asset=source_asset)
    if problems and not skip_validation:
        raise exceptions.ComposeError(problems)

    if not skip_validation:
        asset_id = ledger.issuances.get_asset_id(db, asset)
    else:
        asset_id = ledger.issuances.generate_asset_id(asset)

    if destination_is_holder:
        short_address_bytes = destination
    else:
        short_address_bytes = address.pack(destination)

    if protocol.enabled("taproot_support"):
        data = struct.pack(config.SHORT_TXTYPE_FORMAT, ID)
        payload = [
            asset_id,
            quantity,
            short_address_bytes,
            memo_bytes,
        ]
        if source_asset is not None:
            if not skip_validation:
                source_asset_id = ledger.issuances.get_asset_id(db, source_asset)
            else:
                source_asset_id = ledger.issuances.generate_asset_id(source_asset)
            payload.append(source_asset_id)
        data += cbor2.dumps(payload)
    else:
        if destination_is_holder or source_asset is not None:
            raise exceptions.ComposeError(
                "asset holders require the `assets_hold_assets` protocol change"
            )
        data = messagetype.pack(ID)
        data += struct.pack(FORMAT, asset_id, quantity, short_address_bytes)
        data += memo_bytes

    cursor.close()
    # return an empty array as the second argument because we don't need to send BTC dust to the recipient
    return (signing_source, [], data)


def parse(db, tx, message):
    cursor = db.cursor()

    # Unpack message.
    try:
        unpacked = unpack(message, block_index=tx["block_index"])
        asset, quantity, destination, memo_bytes = (
            unpacked["asset"],
            unpacked["quantity"],
            unpacked["address"],
            unpacked["memo"],
        )
        destination_is_holder = unpacked.get("destination_is_holder", False)
        source_asset = unpacked.get("source_asset")

        status = "valid"

    except (exceptions.UnpackError, exceptions.AssetNameError, struct.error) as e:
        asset, quantity, destination, memo_bytes = None, None, None, None
        destination_is_holder, source_asset = False, None
        status = f"invalid: could not unpack ({e})"
    except:  # noqa: E722 # pylint: disable=bare-except
        asset, quantity, destination, memo_bytes = None, None, None, None
        destination_is_holder, source_asset = False, None
        status = "invalid: could not unpack"

    if status == "valid":
        # CBOR-encoded messages can carry any type for these fields;
        # normalise unexpected types to None so downstream code (validate,
        # logging, DB insert) never sees a non-int quantity or non-bytes
        # memo. validate() will then mark the tx invalid.
        if not isinstance(quantity, int):
            quantity = None
        if memo_bytes is not None and not isinstance(memo_bytes, (bytes, bytearray)):
            memo_bytes = None

        # don't allow sends over MAX_INT at all
        if quantity is not None and quantity > config.MAX_INT:
            status = "invalid: quantity is too large"
            quantity = None

    # Resolve holder names to their numeric asset name before any ledger op.
    if status == "valid" and destination_is_holder:
        try:
            destination = ledger.holders.resolve_holder(db, destination)
        except exceptions.AssetNameError as e:
            status = f"invalid: {e}"

    if status == "valid" and source_asset is not None:
        try:
            source_asset = ledger.holders.resolve_holder(db, source_asset)
        except exceptions.AssetNameError as e:
            status = f"invalid: {e}"

    if status == "valid":
        problems = validate(db, destination, asset, quantity, memo_bytes, source_asset=source_asset)
        if problems:
            status = "invalid: " + "; ".join(problems)

    if status == "valid" and source_asset is not None:
        ok, problem = ledger.holders.check_source_holder(db, tx["source"], source_asset)
        if not ok:
            status = f"invalid: {problem}"

    if status == "valid":
        # verify balance is present
        debit_source = source_asset if source_asset is not None else tx["source"]
        balance = ledger.balances.get_balance(db, debit_source, asset)
        if balance == 0 or balance < quantity:
            status = "invalid: insufficient funds"

    if status == "valid":
        debit_source = source_asset if source_asset is not None else tx["source"]
        ledger.events.debit(
            db, debit_source, asset, quantity, tx["tx_index"], action="send", event=tx["tx_hash"]
        )
        ledger.events.credit(
            db, destination, asset, quantity, tx["tx_index"], action="send", event=tx["tx_hash"]
        )

    # Add parsed transaction to message-type–specific table.
    bindings = {
        "tx_index": tx["tx_index"],
        "tx_hash": tx["tx_hash"],
        "block_index": tx["block_index"],
        "source": source_asset if source_asset is not None else tx["source"],
        "destination": destination,
        "asset": asset,
        "quantity": quantity,
        "status": status,
        "memo": memo_bytes,
        "msg_index": ledger.other.get_send_msg_index(db, tx["tx_hash"]),
        "send_type": "send",
    }
    if "integer overflow" not in status and "quantity must be in satoshis" not in status:
        ledger.events.insert_record(db, "sends", bindings, "ENHANCED_SEND")

    logger.info(
        "Send (Enhanced) %(asset)s from %(source)s to %(destination)s (%(tx_hash)s) [%(status)s]",
        bindings,
    )

    cursor.close()

    ledger.blocks.set_transaction_status(
        db,
        tx["tx_index"],
        status == "valid",
    )


# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
