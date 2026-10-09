import logging
import struct

import cbor2

from counterpartycore.lib import (
    config,
    exceptions,
    ledger,
)
from counterpartycore.lib.ledger.currentstate import CurrentState
from counterpartycore.lib.parser import messagetype, protocol
from counterpartycore.lib.utils import address, helpers

logger = logging.getLogger(config.LOGGER_NAME)

FORMAT = ">21sB"
LENGTH = 22
MAX_MEMO_LENGTH = 34  # Could be higher, but we will keep it consistent with enhanced send
ID = 4
ANTISPAM_FEE_DECIMAL = 0.5
ANTISPAM_FEE = ANTISPAM_FEE_DECIMAL * config.UNIT

FLAG_BALANCES = 1
FLAG_OWNERSHIP = 2
FLAG_BINARY_MEMO = 4
FLAG_ASSET_HOLDER_PAYS = 8

FLAGS_ALL = FLAG_BINARY_MEMO | FLAG_BALANCES | FLAG_OWNERSHIP


def allowed_flags(block_index=None):
    """The set of flags accepted at ``block_index``.

    ``FLAG_ASSET_HOLDER_PAYS`` is only meaningful once ``assets_hold_assets``
    is active; before that a sweep with ``flags=9`` must be rejected, so the
    mask must not include bit 3.
    """
    if protocol.enabled("assets_hold_assets", block_index=block_index):
        return FLAGS_ALL | FLAG_ASSET_HOLDER_PAYS
    return FLAGS_ALL


def get_sweep_balances(db, source, block_index):
    balances = ledger.balances.get_address_balances(db, source)
    if protocol.enabled("sweep_skip_zero_balances", block_index):
        return [balance for balance in balances if balance["quantity"] > 0]
    return balances


def get_total_fee(db, source, block_index, flags=None):
    total_fee = ANTISPAM_FEE
    antispamfee = protocol.get_value_by_block_index("sweep_antispam_fee", block_index) * config.UNIT
    if antispamfee > 0:
        if protocol.enabled("sweep_skip_zero_balances", block_index):
            if (
                not isinstance(flags, int)
                or isinstance(flags, bool)
                or flags > allowed_flags(block_index)
                or not flags & (FLAG_BALANCES | FLAG_OWNERSHIP)
            ):
                flags = FLAGS_ALL
            balances_count = len(get_sweep_balances(db, source, block_index))
            if not flags & FLAG_BALANCES:
                balances_count = 0
            issuances_count = ledger.issuances.get_issuances_count(db, source)
            if not flags & FLAG_OWNERSHIP:
                issuances_count = 0
        else:
            balances_count = ledger.balances.get_balances_count(db, source)[0]["cnt"]
            issuances_count = ledger.issuances.get_issuances_count(db, source)
        total_fee = int(balances_count * antispamfee * 2 + issuances_count * antispamfee * 4)
    return total_fee


def _is_holder_source(source):
    return isinstance(source, str) and ledger.holders.is_valid_asset_name(source)


def resolve_fee_payer(db, source, flags, signer, total_fee):
    """Return the address/asset the antispam fee is debited from.

    A normal sweep is paid by its source. A holder source pays from the asset's
    own XCP balance when it can; otherwise, if ``FLAG_ASSET_HOLDER_PAYS`` is
    set, the 1-of-1 owner (the signer) pays. When neither can pay, the source is
    returned so the balance check reports the shortfall.
    """
    if not _is_holder_source(source):
        return source
    if ledger.balances.get_balance(db, source, "XCP") >= total_fee:
        return source
    if isinstance(flags, int) and not isinstance(flags, bool) and flags & FLAG_ASSET_HOLDER_PAYS:
        return signer
    return source


def validate(db, source, destination, flags, memo, block_index, signer=None):
    problems = []

    if signer is None:
        signer = source

    if source == destination:
        problems.append("destination cannot be the same as source")

    cursor = db.cursor()

    total_fee = get_total_fee(db, source, block_index, flags)

    fee_payer = resolve_fee_payer(db, source, flags, signer, total_fee)
    result = ledger.balances.get_balance(db, fee_payer, "XCP")

    if result < total_fee:
        problems.append(
            f"insufficient XCP balance for sweep. Need {total_fee / config.UNIT} XCP for antispam fee"
        )

    cursor.close()

    # CBOR-encoded messages can carry any type for flags; reject anything
    # that is not int (excluding bool, which is an int subclass and
    # meaningless as flags).
    if not isinstance(flags, int) or isinstance(flags, bool):
        problems.append("flags must be an int")
    elif flags > allowed_flags(block_index):
        problems.append(f"invalid flags {flags}")
    elif not flags & (FLAG_BALANCES | FLAG_OWNERSHIP):
        problems.append("must specify which kind of transfer in flags")

    if (
        isinstance(destination, str)
        and ledger.holders.is_valid_asset_name(destination)
        and not ledger.holders.is_asset_holder_eligible(db, destination)
    ):
        problems.append(f"{destination} is not an eligible asset holder")

    # CBOR-encoded messages can carry any type for memo; len() requires a
    # bytes-like (or str) value.
    if memo and not isinstance(memo, (bytes, bytearray, str)):
        problems.append("memo must be bytes")
    elif memo and len(memo) > MAX_MEMO_LENGTH:
        problems.append("memo too long")

    return problems, total_fee


def has_asset_ownership_to_sweep(db, source, block_index):
    for asset_issued in ledger.issuances.get_asset_issued(db, source):
        issuances = ledger.issuances.get_issuances(
            db,
            asset=asset_issued["asset"],
            status="valid",
            first=True,
            current_block_index=block_index,
        )
        if issuances and issuances[-1]["issuer"] == source:
            return True
    return False


def empty_sweep_problem(db, source, flags, block_index):
    if not isinstance(flags, int) or isinstance(flags, bool):
        return None

    requested = []
    has_sweepable_content = False

    if flags & FLAG_BALANCES:
        requested.append("balances")
        balances = ledger.balances.get_address_balances(db, source)
        has_sweepable_content = any(balance["quantity"] > 0 for balance in balances)

    if flags & FLAG_OWNERSHIP:
        requested.append("asset ownerships")
        has_sweepable_content = has_sweepable_content or has_asset_ownership_to_sweep(
            db, source, block_index
        )

    if has_sweepable_content or not requested:
        return None
    if len(requested) == 1:
        return f"address has no {requested[0]} to sweep"
    return "address has no balances or asset ownerships to sweep"


def compose(
    db, source: str, destination: str, flags: int, memo: str, skip_validation: bool = False
):
    flags_is_int = isinstance(flags, int) and not isinstance(flags, bool)
    if memo is not None and not isinstance(memo, str):
        raise exceptions.ComposeError("memo must be a string")

    if memo is None:
        memo_bytes = b""
    elif flags_is_int and flags & FLAG_BINARY_MEMO:
        try:
            memo_bytes = bytes.fromhex(memo)
        except (TypeError, ValueError) as e:
            raise exceptions.ComposeError("memo must be valid hexadecimal") from e
    else:
        memo_bytes = memo.encode("utf-8")
        memo_bytes = struct.pack(f">{len(memo_bytes)}s", memo_bytes)

    block_index = CurrentState().current_block_index()

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

    logical_source = source_asset if source_asset is not None else source

    problems, _total_fee = validate(
        db, logical_source, destination, flags, memo_bytes, block_index, signer=signing_source
    )
    if not problems:
        empty_problem = empty_sweep_problem(db, logical_source, flags, block_index)
        if empty_problem:
            problems.append(empty_problem)
    if problems and not skip_validation:
        raise exceptions.ComposeError(problems)

    if destination_is_holder:
        short_address_bytes = destination
    else:
        short_address_bytes = address.pack(destination)

    if protocol.enabled("taproot_support"):
        data = struct.pack(config.SHORT_TXTYPE_FORMAT, ID)
        payload = [
            short_address_bytes,
            flags,
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
        data += struct.pack(FORMAT, short_address_bytes, flags)
        data += memo_bytes

    return (signing_source, [], data)


def new_unpack(message, block_index=None):
    try:
        unpacked = cbor2.loads(message)
        if not isinstance(unpacked, list) or len(unpacked) not in (3, 4):
            raise exceptions.UnpackError("could not unpack")
        # The 4th element (the source holder) is only honoured once the
        # protocol change is active; a pre-activation 4-element message must be
        # rejected rather than silently accepted.
        if len(unpacked) == 4 and not protocol.enabled(
            "assets_hold_assets", block_index=block_index
        ):
            raise exceptions.UnpackError("could not unpack")

        short_address_bytes, flags, memo_bytes = unpacked[0:3]
        source_asset_id = unpacked[3] if len(unpacked) == 4 else None

        if source_asset_id is not None:
            if not isinstance(source_asset_id, int) or isinstance(source_asset_id, bool):
                raise exceptions.UnpackError("could not unpack")
            if source_asset_id in (0, 1):
                raise exceptions.UnpackError("could not unpack")

        if len(memo_bytes) == 0:
            memo_bytes = None
        elif not flags & FLAG_BINARY_MEMO:
            memo_bytes = helpers.bytes_to_string(memo_bytes)

        # CBOR preserves the type: a packed-address is bytes, a holder is a
        # (numeric) asset-name string.
        if isinstance(short_address_bytes, str):
            if not protocol.enabled("assets_hold_assets", block_index=block_index):
                raise exceptions.UnpackError("could not unpack")
            full_address = short_address_bytes
        else:
            full_address = address.unpack(short_address_bytes)

        result = {
            "destination": full_address,
            "flags": flags,
            "memo": memo_bytes,
        }
        if source_asset_id is not None:
            result["source_asset_id"] = source_asset_id
        return result
    except Exception as e:  # pylint: disable=broad-exception-caught
        raise exceptions.UnpackError("could not unpack") from e


def unpack(message, block_index=None):
    if protocol.enabled("taproot_support", block_index=block_index):
        try:
            return new_unpack(message, block_index=block_index)
        except exceptions.UnpackError:
            pass  # fall back to legacy unpacking

    try:
        memo_bytes_length = len(message) - LENGTH
        if memo_bytes_length < 0:
            raise exceptions.UnpackError("invalid message length")
        if memo_bytes_length > MAX_MEMO_LENGTH:
            raise exceptions.UnpackError("memo too long")

        struct_format = FORMAT + f"{memo_bytes_length}s"
        short_address_bytes, flags, memo_bytes = struct.unpack(struct_format, message)
        if len(memo_bytes) == 0:
            memo_bytes = None
        elif not flags & FLAG_BINARY_MEMO:
            memo_bytes = memo_bytes.decode("utf-8")
        # unpack address
        full_address = address.unpack(short_address_bytes)
    except struct.error as e:
        raise exceptions.UnpackError("could not unpack") from e

    unpacked = {
        "destination": full_address,
        "flags": flags,
        "memo": memo_bytes,
    }
    return unpacked


def parse(db, tx, message):
    cursor = db.cursor()

    fee_paid = round(config.UNIT / 2)

    # Unpack message.
    try:
        unpacked = unpack(message, block_index=tx["block_index"])
        destination, flags, memo_bytes = (
            unpacked["destination"],
            unpacked["flags"],
            unpacked["memo"],
        )
        source_asset_id = unpacked.get("source_asset_id")

        status = "valid"
    except (exceptions.UnpackError, exceptions.AssetNameError, struct.error) as e:
        destination, flags, memo_bytes = None, None, None
        source_asset_id = None
        status = f"invalid: could not unpack ({e})"
    except exceptions.BalanceError:  # noqa: F405
        destination, flags, memo_bytes = None, None, None
        source_asset_id = None
        status = "invalid: insufficient balance for antispam fee for sweep"
    except Exception as err:  # pylint: disable=broad-exception-caught
        destination, flags, memo_bytes = None, None, None
        source_asset_id = None
        status = "invalid: could not unpack, " + str(err)

    # Resolve the logical source (a holder) and destination (a holder) to their
    # numeric asset names before any ledger operation.
    source_asset = None
    if status == "valid" and source_asset_id is not None:
        try:
            source_asset = ledger.holders.resolve_holder(
                db, ledger.issuances.generate_asset_name(source_asset_id)
            )
        except (exceptions.AssetNameError, exceptions.AssetIDError) as e:
            status = f"invalid: {e}"

    if (
        status == "valid"
        and isinstance(destination, str)
        and ledger.holders.is_valid_asset_name(destination)
    ):
        try:
            destination = ledger.holders.resolve_holder(db, destination)
        except exceptions.AssetNameError as e:
            status = f"invalid: {e}"

    if status == "valid" and source_asset is not None:
        ok, problem = ledger.holders.check_source_holder(db, tx["source"], source_asset)
        if not ok:
            status = f"invalid: {problem}"

    debit_source = source_asset if source_asset is not None else tx["source"]

    if status == "valid":
        # CBOR-encoded messages can carry any type for these fields;
        # normalise unexpected types so downstream code (validate, flag
        # checks below, logging, DB insert) never sees a non-int flags or
        # non-bytes memo. validate() will then mark the tx invalid.
        if not isinstance(flags, int) or isinstance(flags, bool):
            flags = None
        # Also drop ints outside SQLite's signed 64-bit range; otherwise
        # the invalid-record insert below raises OverflowError. validate()
        # would already mark such flags invalid (flags > allowed_flags), but
        # the value still reaches the bindings dict.
        elif flags > config.MAX_INT or flags < -config.MAX_INT:
            flags = None
        if memo_bytes is not None and not isinstance(memo_bytes, (bytes, bytearray, str)):
            memo_bytes = None

        problems, total_fee = validate(
            db, debit_source, destination, flags, memo_bytes, tx["block_index"], signer=tx["source"]
        )
        if problems:
            status = "invalid: " + "; ".join(problems)

    if status == "valid":
        try:
            antispamfee = (
                protocol.get_value_by_block_index("sweep_antispam_fee", tx["block_index"])
                * config.UNIT
            )

            if antispamfee > 0:
                # Under the gate, a sweep with no sweepable balances/ownership has
                # total_fee == 0; charge nothing rather than falling back to the
                # legacy flat fee_paid (which would charge for a no-op sweep).
                if total_fee > 0 or not protocol.enabled(
                    "sweep_skip_zero_balances", tx["block_index"]
                ):
                    fee_amount = total_fee
                else:
                    fee_amount = 0
            else:
                fee_amount = fee_paid

            if fee_amount > 0:
                fee_payer = resolve_fee_payer(db, debit_source, flags, tx["source"], fee_amount)
                ledger.events.debit(
                    db,
                    fee_payer,
                    "XCP",
                    fee_amount,
                    tx["tx_index"],
                    action="sweep fee",
                    event=tx["tx_hash"],
                )
        except exceptions.BalanceError:  # noqa: F405
            destination, flags, memo_bytes = None, None, None
            status = "invalid: insufficient balance for antispam fee for sweep"

    if status == "valid":
        balances = get_sweep_balances(db, debit_source, tx["block_index"])

        if flags & FLAG_BALANCES:
            for balance in balances:
                ledger.events.debit(
                    db,
                    debit_source,
                    balance["asset"],
                    balance["quantity"],
                    tx["tx_index"],
                    action="sweep",
                    event=tx["tx_hash"],
                )
                ledger.events.credit(
                    db,
                    destination,
                    balance["asset"],
                    balance["quantity"],
                    tx["tx_index"],
                    action="sweep",
                    event=tx["tx_hash"],
                )

        if flags & FLAG_OWNERSHIP:
            sweep_pos = 0

            assets_issued = balances
            if protocol.enabled("zero_balance_ownership_sweep_fix", tx["block_index"]):
                assets_issued = ledger.issuances.get_asset_issued(db, debit_source)

            for next_asset_issued in assets_issued:
                issuances = ledger.issuances.get_issuances(
                    db,
                    asset=next_asset_issued["asset"],
                    status="valid",
                    first=True,
                    current_block_index=tx["block_index"],
                )
                if len(issuances) > 0:
                    last_issuance = issuances[-1]
                    if last_issuance["issuer"] == debit_source:
                        bindings = {
                            "tx_index": tx["tx_index"],
                            "tx_hash": tx["tx_hash"],
                            "msg_index": sweep_pos,
                            "block_index": tx["block_index"],
                            "asset": next_asset_issued["asset"],
                            "quantity": 0,
                            "divisible": last_issuance["divisible"],
                            "source": last_issuance["source"],
                            "issuer": destination,
                            "transfer": True,
                            "callable": last_issuance["callable"],
                            "call_date": last_issuance["call_date"],
                            "call_price": last_issuance["call_price"],
                            "description": last_issuance["description"],
                            "fee_paid": 0,
                            "locked": last_issuance["locked"],
                            "status": status,
                            "asset_longname": last_issuance["asset_longname"],
                            "reset": False,
                            "asset_events": "transfer",
                        }
                        ledger.events.insert_record(db, "issuances", bindings, "ASSET_TRANSFER")
                        sweep_pos += 1

        bindings = {
            "tx_index": tx["tx_index"],
            "tx_hash": tx["tx_hash"],
            "block_index": tx["block_index"],
            "source": debit_source,
            "destination": destination,
            "flags": flags,
            "status": status,
            "memo": memo_bytes,
            "fee_paid": total_fee if antispamfee > 0 else fee_paid,
        }
        ledger.events.insert_record(db, "sweeps", bindings, "SWEEP")

        logger.info("Sweep from %(source)s to %(destination)s (%(tx_hash)s) [%(status)s]", bindings)
    elif protocol.enabled("persist_invalid_sweep", block_index=tx["block_index"]):
        # Persist an invalid sweep row + INVALID_SWEEP event so API consumers
        # can enumerate failed sweeps; mirrors the cancel.py / utxo.py pattern.
        bindings = {
            "tx_index": tx["tx_index"],
            "tx_hash": tx["tx_hash"],
            "block_index": tx["block_index"],
            "source": tx["source"],
            "destination": destination,
            "flags": flags,
            "status": status,
            "memo": memo_bytes,
            "fee_paid": 0,
        }
        ledger.events.insert_record(db, "sweeps", bindings, "INVALID_SWEEP")
        logger.info("Invalid sweep from %(source)s (%(tx_hash)s) [%(status)s]", bindings)
    else:
        logger.info(
            "Invalid sweep from %(source)s (%(tx_hash)s) [%(status)s]",
            {
                "source": tx["source"],
                "tx_hash": tx["tx_hash"],
                "status": status,
            },
        )

    ledger.blocks.set_transaction_status(db, tx["tx_index"], status == "valid")

    cursor.close()
