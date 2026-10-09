import logging
import struct
from functools import reduce
from itertools import groupby

from bitstring import ReadError

from counterpartycore.lib import config, exceptions, ledger
from counterpartycore.lib.parser import messagetype, protocol
from counterpartycore.lib.utils import address, helpers
from counterpartycore.lib.utils.mpmaencoding import (
    _decode_mpma_send_decode,
    _decode_mpma_send_decode_full,
    _encode_mpma_send,
)

logger = logging.getLogger(config.LOGGER_NAME)

ID = 3  # 0x03 is this specific message type


def py34_tuple_append(first_elem, t):
    # Had to do it this way to support python 3.4, if we start
    # using the 3.5 runtime this can be replaced by:
    #  (first_elem, *t)

    l = list(t)  # noqa: E741
    l.insert(0, first_elem)
    return tuple(l)


def _resolve_holder_destinations(db, asset_credits):
    """Resolve any holder destination (longname -> numeric name)."""
    resolved = []
    for credit in asset_credits:
        destination = credit[0]
        if isinstance(destination, str) and ledger.holders.is_valid_asset_name(destination):
            destination = ledger.holders.resolve_holder(db, destination)
        resolved.append((destination,) + tuple(credit[1:]))
    return resolved


## expected functions for message version
def unpack(message, block_index=None):
    try:
        unpacked = _decode_mpma_send_decode(message, block_index=block_index)
    except struct.error as e:
        raise exceptions.UnpackError("could not unpack") from e
    except (exceptions.AssetNameError, exceptions.AssetIDError) as e:
        raise exceptions.UnpackError("invalid asset in mpma send") from e
    except ReadError as e:
        raise exceptions.UnpackError("truncated data") from e

    return unpacked


def unpack_full(message, block_index=None):
    """Like ``unpack`` but also returns the source holder's asset id (or None)."""
    try:
        return _decode_mpma_send_decode_full(message, block_index=block_index)
    except struct.error as e:
        raise exceptions.UnpackError("could not unpack") from e
    except (exceptions.AssetNameError, exceptions.AssetIDError) as e:
        raise exceptions.UnpackError("invalid asset in mpma send") from e
    except ReadError as e:
        raise exceptions.UnpackError("truncated data") from e


def validate(db, asset_dest_quant_list, source_asset=None):
    problems = []

    if len(asset_dest_quant_list) == 0:
        problems.append("send list cannot be empty")

    if len(asset_dest_quant_list) == 1:
        problems.append("send list cannot have only one element")

    if len(asset_dest_quant_list) > 0:
        # Need to manually unpack the tuple to avoid errors on scenarios where no memo is specified
        grpd = groupby([(t[0], t[1]) for t in asset_dest_quant_list])
        lengrps = [len(list(grpr)) for (group, grpr) in grpd]
        cardinality = max(lengrps)
        if cardinality > 1:
            problems.append("cannot specify more than once a destination per asset")

    cursor = db.cursor()
    for t in asset_dest_quant_list:
        # Need to manually unpack the tuple to avoid errors on scenarios where no memo is specified
        asset = t[0]
        destination = t[1]
        quantity = t[2]

        send_memo = None
        if len(t) > 3:
            send_memo = t[3]

        if asset == config.BTC:
            problems.append(f"cannot send {config.BTC} to {destination}")

        if not isinstance(quantity, int):
            problems.append(f"quantities must be an int (in satoshis) for {asset} to {destination}")

        if quantity < 0:
            problems.append(f"negative quantity for {asset} to {destination}")

        if quantity == 0:
            problems.append(f"zero quantity for {asset} to {destination}")

        # For SQLite3
        if quantity > config.MAX_INT:
            problems.append(f"integer overflow for {asset} to {destination}")

        # destination is always required
        if not destination:
            problems.append(f"destination is required for {asset}")
        elif isinstance(destination, str) and ledger.holders.is_valid_asset_name(destination):
            # A holder destination only needs to be eligible; ownership is not
            # required to receive.
            if not ledger.holders.is_asset_holder_eligible(db, destination):
                problems.append(f"{destination} is not an eligible asset holder")
            if source_asset is not None and destination == source_asset:
                problems.append(f"source and destination cannot be the same asset for {asset}")

        if protocol.enabled("options_require_memo"):
            results = ledger.other.get_addresses(db, address=destination) if destination else None
            if results:
                result = results[0]
                if (
                    result
                    and result["options"] & config.ADDRESS_OPTION_REQUIRE_MEMO
                    and (send_memo is None)
                ):
                    problems.append(f"destination {destination} requires memo")

    cursor.close()

    return problems


def validate_compose(db, asset_dest_quant_list, source_asset=None):
    problems = validate(db, asset_dest_quant_list, source_asset=source_asset)
    seen = set()
    duplicate_problem = "cannot specify more than once a destination per asset"
    for send in asset_dest_quant_list:
        send_key = (send[0], send[1])
        if send_key in seen and duplicate_problem not in problems:
            problems.append(duplicate_problem)
            break
        seen.add(send_key)

    return problems


def compose(
    db,
    source: str,
    asset_dest_quant_list: list,
    memo: str = None,
    memo_is_hex: bool = None,
    skip_validation: bool = False,
):
    """
    Compose a MPMA send message.
    :param db: sqlite3 database
    :param source: source address
    :param asset_dest_quant_list: list of tuples of the form (asset, destination, quantity, memo, is_hex), where memo and is_hex are optional; if not specified for a send, memo is used.
    :param memo: optional memo for the entire send
    :param memo_is_hex: optional boolean indicating if the memo is in hex format
    """
    for send in asset_dest_quant_list:
        if not send[1] and not skip_validation:
            raise exceptions.ComposeError([f"destination is required for {send[0]}"])

    cursor = db.cursor()

    # The transaction can be mined at the earliest in the next block, so it is encoded under that
    # block's rules: a message composed just below the activation height must not use the legacy
    # table if it is going to be parsed with the new one.
    next_block_index = ledger.blocks.last_db_index(db) + 1

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

    # Resolve holder destinations (longnames -> numeric names) before encoding.
    resolved_list = []
    for send in asset_dest_quant_list:
        destination = send[1]
        if isinstance(destination, str) and ledger.holders.is_valid_asset_name(destination):
            destination = ledger.holders.resolve_holder(db, destination)
        resolved_list.append((send[0], destination) + tuple(send[2:]))
    asset_dest_quant_list = resolved_list

    # Until `mpma_taproot_support` activates the lookup table cannot carry a witness program
    # longer than 20 bytes, so a compose that accepted one would produce a message the network
    # cannot parse. After activation the Rust packer decides what is representable.
    if not protocol.enabled("mpma_taproot_support", block_index=next_block_index):
        for send in asset_dest_quant_list:
            destination = send[1]

            if isinstance(destination, str) and ledger.holders.is_valid_asset_name(destination):
                raise exceptions.ComposeError(
                    "asset holders require the `assets_hold_assets` protocol change"
                )
            if len(address.pack(destination)) > 22:
                raise exceptions.ComposeError(f"Address not supported by MPMA send: {destination}")

    if memo and not isinstance(memo, str):
        raise exceptions.ComposeError("`memo` must be a string")
    if memo_is_hex and not isinstance(memo_is_hex, bool):
        raise exceptions.ComposeError("`memo_is_hex` must be a boolean")

    out_balances = helpers.accumulate([(t[0], t[2]) for t in asset_dest_quant_list])
    for asset, quantity in out_balances:
        if protocol.enabled("mpma_subasset_support"):
            # resolve subassets
            asset = ledger.issuances.resolve_subasset_longname(db, asset)  # noqa: PLW2901

        if not isinstance(quantity, int):
            raise exceptions.ComposeError(f"quantities must be an int (in satoshis) for {asset}")

        balance = ledger.balances.get_balance(db, source_asset or signing_source, asset)
        if balance < quantity and not skip_validation:
            raise exceptions.ComposeError(f"insufficient funds for {asset}")

    cursor.close()

    problems = validate_compose(db, asset_dest_quant_list, source_asset=source_asset)
    if problems and not skip_validation:
        raise exceptions.ComposeError(problems)

    source_asset_id = None
    if source_asset is not None:
        if not skip_validation:
            source_asset_id = ledger.issuances.get_asset_id(db, source_asset)
        else:
            source_asset_id = ledger.issuances.generate_asset_id(source_asset)

    data = messagetype.pack(ID)

    try:
        data += _encode_mpma_send(
            db,
            asset_dest_quant_list,
            memo=memo,
            memo_is_hex=memo_is_hex,
            block_index=next_block_index,
            source_asset_id=source_asset_id,
        )
    except Exception as e:  # pylint: disable=broad-except
        raise exceptions.ComposeError(f"couldn't encode MPMA send: {e}") from e

    return (signing_source, [], data)


def parse(db, tx, message):
    block_index = tx["block_index"]
    if block_index == config.MEMPOOL_BLOCK_INDEX:
        # The mempool is parsed in a fake block above every activation height; decode a pending
        # transaction under the rules of the block it can next be mined in.
        block_index = ledger.blocks.last_db_index(db) + 1

    source_asset = None
    try:
        unpacked, source_asset_id = unpack_full(message, block_index=block_index)
        if source_asset_id is not None:
            if source_asset_id in (0, 1):
                raise exceptions.AssetNameError("invalid source asset id")
            source_asset = ledger.issuances.generate_asset_name(source_asset_id)
        status = "valid"
    except struct.error:
        status = "invalid: truncated message"
    except (exceptions.AssetNameError, exceptions.AssetIDError):
        status = "invalid: invalid asset name/id"
    except Exception as e:  # pylint: disable=broad-except
        status = f"invalid: couldn't unpack; {e}"

    cursor = db.cursor()

    if status == "valid" and source_asset is not None:
        try:
            source_asset = ledger.holders.resolve_holder(db, source_asset)
        except exceptions.AssetNameError as e:
            status = f"invalid: {e}"
        else:
            ok, problem = ledger.holders.check_source_holder(db, tx["source"], source_asset)
            if not ok:
                status = f"invalid: {problem}"

    debit_source = source_asset if source_asset is not None else tx["source"]

    plain_sends = []
    all_debits = []
    all_credits = []
    if status == "valid":
        for asset_id, asset_credits in unpacked.items():
            try:
                ledger.issuances.get_asset_name(db, asset_id)
            except exceptions.AssetNameError:
                status = f"invalid: asset {asset_id} invalid at block index {tx['block_index']}"
                break

            # Resolve holder destinations (longnames -> numeric names).
            try:
                asset_credits = _resolve_holder_destinations(db, asset_credits)
            except exceptions.AssetNameError as e:
                status = f"invalid: {e}"
                break

            balance = ledger.balances.get_balance(db, debit_source, asset_id)
            if not balance:
                status = f"invalid: insufficient funds for asset {asset_id}, address {debit_source} has no balance"
                break

            total_sent = reduce(lambda p, t: p + t[1], asset_credits, 0)

            if balance < total_sent:
                status = f"invalid: insufficient funds for asset {asset_id}, needs {total_sent}"
                break

            if status == "valid":
                plain_sends += map(lambda t: py34_tuple_append(asset_id, t), asset_credits)  # pylint: disable=cell-var-from-loop
                all_credits += map(
                    lambda t: {"asset": asset_id, "destination": t[0], "quantity": t[1]},  # pylint: disable=cell-var-from-loop
                    asset_credits,
                )
                all_debits.append({"asset": asset_id, "quantity": total_sent})

    if status == "valid":
        problems = validate(db, plain_sends, source_asset=source_asset)

        if problems:
            status = "invalid:" + "; ".join(problems)

    if status == "valid":
        for op in all_credits:
            ledger.events.credit(
                db,
                op["destination"],
                op["asset"],
                op["quantity"],
                tx["tx_index"],
                action="mpma send",
                event=tx["tx_hash"],
            )

        for op in all_debits:
            ledger.events.debit(
                db,
                debit_source,
                op["asset"],
                op["quantity"],
                tx["tx_index"],
                action="mpma send",
                event=tx["tx_hash"],
            )

        # Enumeration of the plain sends needs to be deterministic, so we sort them by asset and then by address
        plain_sends = sorted(plain_sends, key=lambda x: "".join([x[0], x[1]]))
        for op in plain_sends:
            if len(op) > 3:
                memo_bytes = op[3]
            else:
                memo_bytes = None

            bindings = {
                "tx_index": tx["tx_index"],
                "tx_hash": tx["tx_hash"],
                "block_index": tx["block_index"],
                "source": debit_source,
                "asset": op[0],
                "destination": op[1],
                "quantity": op[2],
                "status": status,
                "memo": memo_bytes,
                "msg_index": ledger.other.get_send_msg_index(db, tx["tx_hash"]),
                "send_type": "send",
            }

            ledger.events.insert_record(db, "sends", bindings, "MPMA_SEND")

            logger.info(
                "Send (MPMA) %(asset)s from %(source)s to %(destination)s (%(tx_hash)s) [%(status)s]",
                bindings,
            )

    if status != "valid":
        logger.info(
            "Invalid MPMA Send (%(tx_hash)s) [%(status)s]",
            {"tx_hash": tx["tx_hash"], "status": status},
        )

    cursor.close()

    ledger.blocks.set_transaction_status(
        db,
        tx["tx_index"],
        status == "valid",
    )


# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
