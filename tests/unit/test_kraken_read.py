from __future__ import annotations

import base64
import itertools
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest

from kraken_knight.kraken_read import (
    CONSERVATIVE_PRIVATE_COST_LIMIT,
    PRIVATE_ENDPOINT_COSTS,
    ApiKeyInfoSnapshot,
    KrakenApiError,
    KrakenReadClient,
    KrakenReadError,
    KrakenRequest,
    KrakenRequestBudget,
    KrakenResponseError,
    KrakenSystemStatus,
    KrakenTransportError,
    MonotonicNonce,
    WalletAccountsSnapshot,
    _default_transport,
    sign_read_only_request,
)

NOW = datetime(2026, 9, 1, 0, 15, tzinfo=UTC)
API_KEY = "fixture-public-key"
API_SECRET_BYTES = b"fixture-private-secret"
API_SECRET = base64.b64encode(API_SECRET_BYTES).decode("ascii")
CLIENT_IDS = itertools.count()


def _response(result: object, *, errors: list[object] | None = None) -> bytes:
    return json.dumps({"error": errors or [], "result": result}).encode()


class QueueTransport:
    def __init__(self, *responses: bytes | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[KrakenRequest] = []

    def __call__(self, request: KrakenRequest) -> bytes:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected transport call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _client(
    transport: Callable[[KrakenRequest], bytes],
    *,
    nonce: Callable[[], int] = lambda: 1_700_000_000_000,
    budget: KrakenRequestBudget | None = None,
    pacing_hook: Callable[[str, int], None] | None = None,
    api_key: str | None = None,
) -> KrakenReadClient:
    return KrakenReadClient(
        api_key=api_key or f"{API_KEY}-{next(CLIENT_IDS)}",
        api_secret=API_SECRET,
        transport=transport,
        clock=lambda: NOW,
        nonce=nonce,
        request_budget=budget,
        pacing_hook=pacing_hook,
    )


def _pair_result() -> dict[str, object]:
    return {
        "XXBTZCAD": {
            "altname": "XBTCAD",
            "wsname": "XBT/CAD",
            "base": "XXBT",
            "quote": "ZCAD",
            "status": "online",
            "ordermin": "0.0001",
            "costmin": "0.5",
            "tick_size": "0.1",
            "cost_decimals": 5,
            "pair_decimals": 1,
            "lot_decimals": 8,
            "fees": [[0, "0.40"], [10_000, "0.35"]],
            "fees_maker": [[0, "0.25"], [10_000, "0.20"]],
        }
    }


def _api_key_info_result(*, raw_key: str, snake_case: bool = False) -> dict[str, object]:
    common: dict[str, object] = {
        "iban": "AA88 REDACTED FIXTURE",
        "nonce": "1772627060997",
        "permissions": [
            "query-closed-trades",
            "query-funds",
            "query-ledger",
            "query-open-trades",
        ],
    }
    if snake_case:
        return {
            **common,
            "api_key_name": "kraken-knight-readonly-2026-08",
            "api_key": raw_key,
            "nonce_window": "0",
            "created_time": "1772542900",
            "modified_time": "1772543095",
            "valid_until": "0",
            "query_from": "0",
            "query_to": "0",
            "ip_allowlist": ["203.0.113.9/32"],
            "last_used": "1772627061",
        }
    return {
        **common,
        "apiKeyName": "kraken-knight-read",
        "apiKey": raw_key,
        "nonceWindow": 0,
        "createdTime": "1772542900",
        "modifiedTime": "1772543095",
        "validUntil": "0",
        "queryFrom": "0",
        "queryTo": "1773000000",
        "ipAllowlist": ["203.0.113.9/32"],
        "lastUsed": "1772627061",
    }


def _order(*, status: str = "open", closed: bool = False) -> dict[str, object]:
    result: dict[str, object] = {
        "refid": None,
        "userref": 7,
        "cl_ord_id": "kk1234567890123456",
        "status": status,
        "reason": None,
        "opentm": "1788220800.25",
        "starttm": 0,
        "expiretm": 0,
        "descr": {
            "pair": "XBTCAD",
            "type": "buy",
            "ordertype": "limit",
            "price": "100000.0",
            "price2": "0",
            "leverage": "none",
        },
        "vol": "0.005",
        "vol_exec": "0.001",
        "cost": "100.0",
        "fee": "0.25",
        "price": "100000.0",
        "stopprice": "0",
        "limitprice": "0",
        "oflags": "post,fciq",
        "trades": ["T-ONE"],
    }
    if closed:
        result["closetm"] = "1788220900.5"
    return result


def _trade() -> dict[str, object]:
    return {
        "ordertxid": "O-ONE",
        "postxid": None,
        "pair": "XXBTZCAD",
        "time": "1788220900.5",
        "type": "buy",
        "ordertype": "limit",
        "price": "100000.0",
        "cost": "100.0",
        "fee": "0.25",
        "vol": "0.001",
        "margin": "0",
        "maker": True,
        "trade_id": 42,
        "ledgers": ["L-CAD", "L-XBT"],
    }


def _ledger() -> dict[str, object]:
    return {
        "refid": "T-ONE",
        "time": "1788220900.5",
        "type": "trade",
        "subtype": "",
        "aclass": "currency",
        "asset": "ZCAD",
        "amount": "-100.0",
        "fee": "0.25",
        "balance": "899.75",
    }


def test_signing_is_deterministic_for_an_allowlisted_read_endpoint() -> None:
    secret = base64.b64decode(
        "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
    )
    body = b"nonce=1616492376594"

    signature = sign_read_only_request(
        endpoint="BalanceEx",
        body=body,
        nonce=1616492376594,
        secret=secret,
    )

    assert signature == (
        "1j2wTVcCjQvueYQKkvwE3KMKUKZ2X44OnlJOYr2nPt3WwRIl69rXv3dyHxdoxHIc88DGbIR+WwRtR0qudfAL+w=="
    )


def test_server_time_uses_public_get_and_reports_clock_skew() -> None:
    transport = QueueTransport(_response({"unixtime": int(NOW.timestamp()) - 2}))
    client = KrakenReadClient(transport=transport, clock=lambda: NOW)

    snapshot = client.get_server_time()

    assert snapshot.clock_skew.total_seconds() == -2
    request = transport.requests[0]
    assert request.method == "GET"
    assert request.url == "https://api.kraken.com/0/public/Time"
    assert request.body is None


@pytest.mark.parametrize(
    ("raw_status", "expected", "is_online"),
    [
        ("online", KrakenSystemStatus.ONLINE, True),
        ("maintenance", KrakenSystemStatus.MAINTENANCE, False),
        ("cancel_only", KrakenSystemStatus.CANCEL_ONLY, False),
        ("post_only", KrakenSystemStatus.POST_ONLY, False),
    ],
)
def test_system_status_is_strict_and_only_online_is_safe(
    raw_status: str, expected: KrakenSystemStatus, is_online: bool
) -> None:
    transport = QueueTransport(
        _response({"status": raw_status, "timestamp": "2026-09-01T00:14:59Z"})
    )

    snapshot = KrakenReadClient(transport=transport, clock=lambda: NOW).get_system_status()

    assert snapshot.status is expected
    assert snapshot.status_at == datetime(2026, 9, 1, 0, 14, 59, tzinfo=UTC)
    assert snapshot.is_online is is_online


def test_system_status_rejects_unknown_modes_and_naive_timestamps() -> None:
    unknown = QueueTransport(_response({"status": "mystery", "timestamp": "2026-09-01Z"}))
    naive = QueueTransport(_response({"status": "online", "timestamp": "2026-09-01T00:00:00"}))

    with pytest.raises(KrakenResponseError, match="status is unsupported"):
        KrakenReadClient(transport=unknown, clock=lambda: NOW).get_system_status()
    with pytest.raises(KrakenResponseError, match="timezone-aware"):
        KrakenReadClient(transport=naive, clock=lambda: NOW).get_system_status()


def test_asset_pair_is_frozen_to_btc_cad_and_parses_decimal_rules() -> None:
    transport = QueueTransport(_response(_pair_result()))
    client = KrakenReadClient(transport=transport, clock=lambda: NOW)

    snapshot = client.get_asset_pair(pair="BTC/CAD")

    assert snapshot.pair.exchange_pair == "XXBTZCAD"
    assert snapshot.pair.tick_size == Decimal("0.1")
    assert snapshot.pair.maker_schedule[0].fee_percent == Decimal("0.25")
    assert parse_qs(urlparse(transport.requests[0].url).query) == {"pair": ["XBTCAD"]}
    with pytest.raises(ValueError, match="BTC/CAD"):
        client.get_asset_pair(pair="ETH/CAD")


def test_extended_balance_is_typed_and_private_request_is_signed() -> None:
    transport = QueueTransport(
        _response(
            {
                "ZCAD": {"balance": "1000", "hold_trade": "100"},
                "XXBT": {
                    "balance": "0.01",
                    "credit": "0",
                    "credit_used": "0",
                    "hold_trade": "0.002",
                },
            }
        )
    )
    client = _client(transport, api_key=API_KEY)

    snapshot = client.get_extended_balances()

    assert [balance.asset for balance in snapshot.balances] == ["XXBT", "ZCAD"]
    assert snapshot.balances[1].available == Decimal("900")
    request = transport.requests[0]
    assert request.url.endswith("/0/private/BalanceEx")
    assert request.body == b"nonce=1700000000000"
    headers = dict(request.headers)
    assert headers["API-Key"] == API_KEY
    assert headers["API-Sign"] == sign_read_only_request(
        endpoint="BalanceEx",
        body=request.body,
        nonce=1_700_000_000_000,
        secret=API_SECRET_BYTES,
    )


def test_trade_volume_parses_current_maker_and_taker_fees() -> None:
    fee = {
        "fee": "0.40",
        "minfee": "0.10",
        "maxfee": "0.40",
        "nextfee": "0.35",
        "tiervolume": "0",
        "nextvolume": "10000",
    }
    transport = QueueTransport(
        _response(
            {
                "currency": "ZUSD",
                "volume": "123.45",
                "fees": {"XXBTZCAD": fee},
                "fees_maker": {"XXBTZCAD": {**fee, "fee": "0.25"}},
            }
        )
    )

    snapshot = _client(transport).get_trade_volume()

    assert snapshot.rolling_volume == Decimal("123.45")
    assert snapshot.taker_fees[0].fee_percent == Decimal("0.40")
    assert snapshot.maker_fees[0].fee_percent == Decimal("0.25")


def test_open_closed_and_query_orders_use_only_read_endpoints() -> None:
    transport = QueueTransport(
        _response({"open": {"O-OPEN": _order()}}),
        _response({"closed": {"O-CLOSED": _order(status="closed", closed=True)}, "count": 1}),
        _response({"O-CLOSED": _order(status="closed", closed=True)}),
    )
    nonce_values = iter((100, 101, 102))
    client = _client(transport, nonce=lambda: next(nonce_values))

    open_snapshot = client.get_open_orders(client_order_id="kk1234567890123456")
    closed_page = client.get_closed_orders(start=10, end=20, offset=50)
    query_snapshot = client.query_orders(["O-CLOSED"])

    assert open_snapshot.orders[0].status == "open"
    assert closed_page.orders[0].closed_at is not None
    assert closed_page.total_count == 1
    assert query_snapshot.requested_order_ids == ("O-CLOSED",)
    assert [urlparse(request.url).path for request in transport.requests] == [
        "/0/private/OpenOrders",
        "/0/private/ClosedOrders",
        "/0/private/QueryOrders",
    ]
    closed_body = transport.requests[1].body
    assert closed_body is not None
    assert parse_qs(closed_body.decode()) == {
        "nonce": ["101"],
        "trades": ["true"],
        "consolidate_taker": ["false"],
        "closetime": ["both"],
        "ofs": ["50"],
        "start": ["10"],
        "end": ["20"],
    }
    query_body = transport.requests[2].body
    assert query_body is not None
    assert parse_qs(query_body.decode()) == {
        "nonce": ["102"],
        "trades": ["true"],
        "consolidate_taker": ["false"],
        "txid": ["O-CLOSED"],
    }


def test_trade_and_ledger_history_preserve_ids_fees_and_paging() -> None:
    transport = QueueTransport(
        _response({"trades": {"T-ONE": _trade()}, "count": 1}),
        _response({"ledger": {"L-ONE": _ledger()}, "count": 1}),
    )
    nonces = iter((200, 201))
    client = _client(transport, nonce=lambda: next(nonces))

    trades = client.get_trades_history(offset=5, limit=75, pair="XBT/CAD")
    ledgers = client.get_ledgers(
        account_id="WX6V-JUKW-KKPB-QE36",
        entry_type="all",
        offset=10,
    )

    assert trades.total_count == 1
    assert trades.trades[0].trade_id == "T-ONE"
    assert trades.trades[0].fee == Decimal("0.25")
    assert trades.trades[0].ledger_ids == ("L-CAD", "L-XBT")
    assert ledgers.entries[0].amount == Decimal("-100.0")
    assert ledgers.entries[0].balance == Decimal("899.75")
    assert parse_qs(urlparse(transport.requests[1].url).query) == {
        "account_id": ["WX6V-JUKW-KKPB-QE36"]
    }
    ledger_request = transport.requests[1]
    assert ledger_request.body is not None
    assert "WX6V-JUKW-KKPB-QE36" not in ledger_request.body.decode()
    assert dict(ledger_request.headers)["API-Sign"] == sign_read_only_request(
        endpoint="Ledgers",
        body=ledger_request.body,
        nonce=201,
        secret=API_SECRET_BYTES,
    )
    assert [
        PRIVATE_ENDPOINT_COSTS[urlparse(item.url).path.rsplit("/", 1)[-1]]
        for item in transport.requests
    ] == [4, 4]


def test_ledger_wallet_scope_is_validated_when_supplied_before_transport() -> None:
    transport = QueueTransport(_response({"ledger": {}, "count": 0}))
    client = _client(transport)

    with pytest.raises(ValueError, match="wallet-account format"):
        client.get_ledgers(account_id="not-a-wallet")

    assert transport.requests == []


def test_ledger_default_wallet_scope_is_explicit_and_omits_query_parameter() -> None:
    transport = QueueTransport(_response({"ledger": {}, "count": 0}))

    page = _client(transport).get_ledgers(account_id=None)

    assert page.entries == ()
    assert page.total_count == 0
    assert urlparse(transport.requests[0].url).query == ""
    assert transport.requests[0].body is not None
    assert b"account_id" not in transport.requests[0].body


def test_fractional_timestamp_beyond_microseconds_fails_closed() -> None:
    trade = {**_trade(), "time": "1788220900.0000001"}
    transport = QueueTransport(_response({"trades": {"T-ONE": trade}, "count": 1}))

    with pytest.raises(KrakenResponseError, match="microsecond timestamp precision"):
        _client(transport).get_trades_history()


def test_api_key_info_discards_raw_key_from_model_repr_and_error() -> None:
    returned_raw_key = "returned-raw-key-must-not-survive"
    transport = QueueTransport(_response(_api_key_info_result(raw_key=returned_raw_key)))

    snapshot = _client(transport, api_key=returned_raw_key).get_api_key_info()

    assert isinstance(snapshot, ApiKeyInfoSnapshot)
    assert snapshot.key_name == "kraken-knight-read"
    assert snapshot.ip_allowlist == ("203.0.113.9/32",)
    assert snapshot.valid_until is None
    assert snapshot.query_from is None
    assert snapshot.query_to is not None
    assert returned_raw_key not in repr(snapshot)
    assert "AA88 REDACTED FIXTURE" not in repr(snapshot)
    assert returned_raw_key not in snapshot.__dataclass_fields__
    assert "api_key" not in snapshot.__dataclass_fields__
    assert "iban" not in snapshot.__dataclass_fields__
    assert returned_raw_key not in repr(_client(transport))


def test_api_key_info_accepts_live_snake_case_without_weakening_attestation() -> None:
    returned_raw_key = "live-shape-public-key"
    transport = QueueTransport(
        _response(_api_key_info_result(raw_key=returned_raw_key, snake_case=True))
    )

    snapshot = _client(transport, api_key=returned_raw_key).get_api_key_info()

    assert snapshot.key_name == "kraken-knight-readonly-2026-08"
    assert snapshot.permissions == (
        "query-closed-trades",
        "query-funds",
        "query-ledger",
        "query-open-trades",
    )
    assert snapshot.ip_allowlist == ("203.0.113.9/32",)
    assert snapshot.nonce_window == 0
    assert snapshot.valid_until is None
    assert snapshot.query_from is None
    assert snapshot.query_to is None
    assert returned_raw_key not in repr(snapshot)


@pytest.mark.parametrize(
    ("snake_case", "key_field"),
    [(False, "apiKey"), (True, "api_key")],
)
def test_api_key_info_malformed_key_value_is_rejected_without_leaking(
    snake_case: bool,
    key_field: str,
) -> None:
    configured_key = f"malformed-value-key-{key_field}"
    secret_sentinel = f"nested-secret-sentinel-{key_field}"
    result = _api_key_info_result(raw_key=configured_key, snake_case=snake_case)
    result[key_field] = {"secret": secret_sentinel}

    with pytest.raises(KrakenResponseError, match="API-key identifier is invalid") as caught:
        _client(
            QueueTransport(_response(result)),
            api_key=configured_key,
        ).get_api_key_info()
    assert secret_sentinel not in str(caught.value)
    assert secret_sentinel not in repr(caught.value)


@pytest.mark.parametrize(
    ("snake_case", "key_field"),
    [(False, "apiKey"), (True, "api_key")],
)
def test_api_key_info_non_ascii_key_is_rejected_as_safe_adapter_error(
    snake_case: bool,
    key_field: str,
) -> None:
    configured_key = f"non-ascii-response-key-{key_field}"
    non_ascii_sentinel = "non-ascii-\u00e9-key"
    result = _api_key_info_result(raw_key=configured_key, snake_case=snake_case)
    result[key_field] = non_ascii_sentinel

    with pytest.raises(KrakenResponseError, match="API-key identifier is invalid") as caught:
        _client(
            QueueTransport(_response(result)),
            api_key=configured_key,
        ).get_api_key_info()
    assert non_ascii_sentinel not in str(caught.value)
    assert non_ascii_sentinel not in repr(caught.value)


def test_api_key_info_camel_and_snake_profiles_map_to_equal_snapshots() -> None:
    camel_key = "equivalent-camel-key"
    snake_key = "equivalent-snake-key"
    camel_result = _api_key_info_result(raw_key=camel_key)
    aliases = {
        "apiKey": "api_key",
        "apiKeyName": "api_key_name",
        "createdTime": "created_time",
        "ipAllowlist": "ip_allowlist",
        "lastUsed": "last_used",
        "modifiedTime": "modified_time",
        "nonceWindow": "nonce_window",
        "queryFrom": "query_from",
        "queryTo": "query_to",
        "validUntil": "valid_until",
    }
    snake_result = {aliases.get(field, field): value for field, value in camel_result.items()}
    snake_result["api_key"] = snake_key

    camel_snapshot = _client(
        QueueTransport(_response(camel_result)),
        api_key=camel_key,
    ).get_api_key_info()
    snake_snapshot = _client(
        QueueTransport(_response(snake_result)),
        api_key=snake_key,
    ).get_api_key_info()

    assert snake_snapshot == camel_snapshot


def test_api_key_info_rejects_ambiguous_aliases_and_key_identity_mismatch() -> None:
    returned_raw_key = "returned-key"
    base = _api_key_info_result(raw_key=returned_raw_key)
    ambiguous = QueueTransport(_response({**base, "api_key": returned_raw_key}))

    with pytest.raises(KrakenResponseError, match="schema is unsupported"):
        _client(ambiguous, api_key=returned_raw_key).get_api_key_info()

    mismatched = QueueTransport(_response(base))
    with pytest.raises(KrakenResponseError, match="identity does not match") as caught:
        _client(mismatched, api_key="configured-different-key").get_api_key_info()
    assert returned_raw_key not in repr(caught.value)


@pytest.mark.parametrize(
    ("documented", "observed"),
    [
        ("apiKey", "api_key"),
        ("apiKeyName", "api_key_name"),
        ("nonceWindow", "nonce_window"),
        ("ipAllowlist", "ip_allowlist"),
        ("createdTime", "created_time"),
        ("modifiedTime", "modified_time"),
        ("lastUsed", "last_used"),
        ("validUntil", "valid_until"),
        ("queryFrom", "query_from"),
        ("queryTo", "query_to"),
    ],
)
def test_api_key_info_rejects_every_dual_alias(documented: str, observed: str) -> None:
    returned_raw_key = f"dual-alias-key-{documented}"
    result = _api_key_info_result(raw_key=returned_raw_key)
    result[observed] = result[documented]

    with pytest.raises(KrakenResponseError, match="schema is unsupported"):
        _client(
            QueueTransport(_response(result)),
            api_key=returned_raw_key,
        ).get_api_key_info()


@pytest.mark.parametrize(
    "missing",
    sorted(_api_key_info_result(raw_key="fixture-key")),
)
def test_api_key_info_rejects_every_missing_field(missing: str) -> None:
    fixture_key = f"fixture-key-{missing}"
    result = _api_key_info_result(raw_key=fixture_key)
    del result[missing]

    with pytest.raises(KrakenResponseError, match="schema is unsupported"):
        _client(
            QueueTransport(_response(result)),
            api_key=fixture_key,
        ).get_api_key_info()


def test_api_key_info_rejects_mixed_or_extended_profiles_without_leaking() -> None:
    injected = "unknown-field-secret"
    mixed_key = "profile-mixed-returned-key"
    mixed = _api_key_info_result(raw_key=mixed_key)
    mixed["api_key_name"] = mixed.pop("apiKeyName")
    extended_key = "profile-extended-returned-key"
    extended = {**_api_key_info_result(raw_key=extended_key), "future": injected}

    for api_key, result in ((mixed_key, mixed), (extended_key, extended)):
        with pytest.raises(KrakenResponseError, match="schema is unsupported") as caught:
            _client(
                QueueTransport(_response(result)),
                api_key=api_key,
            ).get_api_key_info()
        assert api_key not in repr(caught.value)
        assert injected not in repr(caught.value)


def test_wallet_accounts_return_public_identity_and_page_completeness() -> None:
    transport = QueueTransport(
        _response(
            {
                "accounts": [
                    {
                        "account_id": "WX6V JUKW KKPB QE36",
                        "flags": {"active": True, "user_defined": False},
                        "status": "active",
                        "type": "main",
                        "name": None,
                    }
                ],
                "cursor": {"next": None},
            }
        )
    )

    snapshot = _client(transport).get_wallet_accounts()

    assert isinstance(snapshot, WalletAccountsSnapshot)
    assert snapshot.complete is True
    assert snapshot.accounts[0].account_id == "WX6V-JUKW-KKPB-QE36"
    assert snapshot.accounts[0].active is True
    assert snapshot.accounts[0].account_type == "main"
    assert "name" not in snapshot.accounts[0].__dataclass_fields__
    assert transport.requests[0].url.endswith("/0/private/ListWalletAccounts")
    assert PRIVATE_ENDPOINT_COSTS["ListWalletAccounts"] == 1


@pytest.mark.parametrize(
    "raw_account_id",
    ["WX6V-JUKW-KKPB-QE36", "WX6V JUKW KKPB QE36"],
)
def test_wallet_account_id_accepts_documented_and_live_shapes_as_one_canonical_id(
    raw_account_id: str,
) -> None:
    transport = QueueTransport(
        _response(
            {
                "accounts": [
                    {
                        "account_id": raw_account_id,
                        "flags": {"active": True, "user_defined": False},
                        "status": "active",
                        "type": "main",
                        "name": None,
                    }
                ],
                "cursor": {"next": None},
            }
        )
    )

    snapshot = _client(transport).get_wallet_accounts()

    assert snapshot.accounts[0].account_id == "WX6V-JUKW-KKPB-QE36"


@pytest.mark.parametrize(
    "raw_account_id",
    [
        "wx6v-jukw-kkpb-qe36",
        "WX6V-JUKW KKPB-QE36",
        "WX6V  JUKW KKPB QE36",
        "WX6V\tJUKW\tKKPB\tQE36",
        " WX6V-JUKW-KKPB-QE36",
        "WX6V-JUKW-KKPB-QE36 ",
        "WX6V\u00a0JUKW\u00a0KKPB\u00a0QE36",
        "WX6V-JUKW-KKPB-QE3_",
        "WX6V-JUKW-KKPB-QE3\u00c9",
        "not-a-wallet-id",
        "",
        None,
    ],
)
def test_wallet_account_id_rejects_every_noncanonical_shape_without_reflection(
    raw_account_id: object,
) -> None:
    response = _response(
        {
            "accounts": [
                {
                    "account_id": raw_account_id,
                    "flags": {"active": True, "user_defined": False},
                    "status": "active",
                    "type": "main",
                    "name": None,
                }
            ],
            "cursor": {"next": None},
        }
    )

    with pytest.raises(KrakenResponseError, match="wallet account ID is invalid") as caught:
        _client(QueueTransport(response)).get_wallet_accounts()
    rendered = str(raw_account_id)
    if rendered:
        assert rendered not in str(caught.value)


def test_wallet_account_id_aliases_cannot_bypass_uniqueness() -> None:
    response = _response(
        {
            "accounts": [
                {
                    "account_id": account_id,
                    "flags": {"active": True, "user_defined": False},
                    "status": "active",
                    "type": "main",
                    "name": None,
                }
                for account_id in ("WX6V-JUKW-KKPB-QE36", "WX6V JUKW KKPB QE36")
            ],
            "cursor": {"next": None},
        }
    )

    with pytest.raises(KrakenResponseError, match="wallet account IDs are not unique"):
        _client(QueueTransport(response)).get_wallet_accounts()


def test_live_wallet_id_flows_to_ledger_query_in_canonical_form() -> None:
    transport = QueueTransport(
        _response(
            {
                "accounts": [
                    {
                        "account_id": "WX6V JUKW KKPB QE36",
                        "flags": {"active": True, "user_defined": False},
                        "status": "active",
                        "type": "main",
                        "name": None,
                    }
                ],
                "cursor": {"next": None},
            }
        ),
        _response({"ledger": {}, "count": 0}),
    )
    nonces = iter((300, 301))
    client = _client(transport, nonce=lambda: next(nonces))

    wallets = client.get_wallet_accounts()
    canonical_id = wallets.accounts[0].account_id
    client.get_ledgers(account_id=canonical_id)

    ledger_request = transport.requests[1]
    assert canonical_id == "WX6V-JUKW-KKPB-QE36"
    assert parse_qs(urlparse(ledger_request.url).query) == {"account_id": [canonical_id]}
    assert ledger_request.body is not None
    assert canonical_id not in ledger_request.body.decode()
    assert "WX6V JUKW KKPB QE36" not in ledger_request.body.decode()
    assert dict(ledger_request.headers)["API-Sign"] == sign_read_only_request(
        endpoint="Ledgers",
        body=ledger_request.body,
        nonce=301,
        secret=API_SECRET_BYTES,
    )


def test_default_transport_rejects_any_non_pinned_origin_before_network() -> None:
    request = KrakenRequest(
        method="POST",
        url="https://example.invalid/0/private/BalanceEx",
        headers=(("API-Key", "must-not-leave"),),
        body=b"nonce=1",
        timeout_seconds=1,
        endpoint_label="private:BalanceEx",
        max_response_bytes=1024,
    )

    with pytest.raises(KrakenTransportError, match="pinned HTTPS origin"):
        _default_transport(request)


def test_top_level_api_errors_are_fail_closed_and_raw_values_are_not_retained() -> None:
    injected = "EGeneral:Invalid arguments:super-secret-request-value"
    transport = QueueTransport(_response({}, errors=[injected]))

    with pytest.raises(KrakenApiError) as caught:
        _client(transport).get_extended_balances()

    assert caught.value.categories == ("request",)
    assert injected not in repr(caught.value)
    assert "super-secret" not in str(caught.value)


@pytest.mark.parametrize(
    "response",
    [
        b"not-json",
        b'{"error": [], "result": null}',
        b'{"error": "not-an-array", "result": {}}',
        b'{"error": [], "wrong": {}}',
        b'{"error": [], "result": {"unixtime": NaN}}',
        b'{"error": [], "error": [], "result": {}}',
    ],
)
def test_malformed_envelopes_fail_closed(response: bytes) -> None:
    client = KrakenReadClient(transport=QueueTransport(response), clock=lambda: NOW)

    with pytest.raises(KrakenResponseError):
        client.get_server_time()


def test_request_and_client_reprs_redact_url_headers_and_body() -> None:
    request = KrakenRequest(
        method="POST",
        url="https://example.invalid/private?secret=query-secret",
        headers=(("API-Key", "header-secret"),),
        body=b"nonce=123&otp=body-secret",
        timeout_seconds=1,
        endpoint_label="private:fixture",
        max_response_bytes=100,
    )
    rendered = repr(request)

    assert "query-secret" not in rendered
    assert "header-secret" not in rendered
    assert "body-secret" not in rendered
    assert "<redacted>" in rendered
    assert API_KEY not in repr(_client(QueueTransport()))
    assert API_SECRET not in repr(_client(QueueTransport()))


def test_untrusted_transport_exception_is_not_chained_or_exposed() -> None:
    dangerous = "https://example.invalid/?api_key=secret-value"

    def transport(_: KrakenRequest) -> bytes:
        raise RuntimeError(dangerous)

    with pytest.raises(KrakenTransportError) as caught:
        _client(transport).get_extended_balances()

    assert dangerous not in repr(caught.value)
    assert caught.value.__cause__ is None


def test_untrusted_clock_and_nonce_exceptions_are_redacted() -> None:
    dangerous = "request-body-or-secret-must-not-escape"

    def broken_clock() -> datetime:
        raise RuntimeError(dangerous)

    public = KrakenReadClient(
        transport=QueueTransport(_response({"unixtime": int(NOW.timestamp())})),
        clock=broken_clock,
    )
    with pytest.raises(KrakenReadError, match="clock failed") as clock_error:
        public.get_server_time()
    assert dangerous not in repr(clock_error.value)
    assert clock_error.value.__cause__ is None

    def broken_nonce() -> int:
        raise RuntimeError(dangerous)

    with pytest.raises(KrakenReadError, match="nonce source failed") as nonce_error:
        _client(QueueTransport(), nonce=broken_nonce).get_extended_balances()
    assert dangerous not in repr(nonce_error.value)
    assert nonce_error.value.__cause__ is None


def test_private_requests_are_serialized_across_nonce_and_transport() -> None:
    entered_first = threading.Event()
    release_first = threading.Event()
    entered_second = threading.Event()
    bodies: list[bytes] = []

    def transport(request: KrakenRequest) -> bytes:
        assert request.body is not None
        bodies.append(request.body)
        if len(bodies) == 1:
            entered_first.set()
            assert release_first.wait(timeout=2)
        else:
            entered_second.set()
        return _response({})

    nonces = iter((500, 501))
    client = _client(transport, nonce=lambda: next(nonces))
    failures: list[BaseException] = []

    def call() -> None:
        try:
            client.get_extended_balances()
        except BaseException as exc:  # pragma: no branch - assertion captures thread failures
            failures.append(exc)

    first = threading.Thread(target=call)
    second = threading.Thread(target=call)
    first.start()
    assert entered_first.wait(timeout=2)
    second.start()
    assert not entered_second.wait(timeout=0.05)
    assert bodies == [b"nonce=500"]
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not failures
    assert bodies == [b"nonce=500", b"nonce=501"]


def test_nonincreasing_nonce_is_rejected_without_a_second_transport_call() -> None:
    transport = QueueTransport(_response({}), _response({}))
    client = _client(transport, nonce=lambda: 42)

    client.get_extended_balances()
    with pytest.raises(KrakenReadError, match="did not increase"):
        client.get_extended_balances()

    assert len(transport.requests) == 1


def test_nonce_state_is_shared_by_same_api_key_across_client_instances() -> None:
    first_transport = QueueTransport(_response({}))
    second_transport = QueueTransport(_response({}))
    shared_key = "shared-process-fixture-key"
    first = _client(first_transport, nonce=lambda: 700, api_key=shared_key)
    second = _client(second_transport, nonce=lambda: 700, api_key=shared_key)

    first.get_extended_balances()
    with pytest.raises(KrakenReadError, match="did not increase"):
        second.get_extended_balances()

    assert len(first_transport.requests) == 1
    assert not second_transport.requests


def test_transport_failure_is_not_retried_and_consumes_nonce_and_budget() -> None:
    calls = 0

    def transport(_: KrakenRequest) -> bytes:
        nonlocal calls
        calls += 1
        raise TimeoutError("body=must-not-be-logged")

    budget = KrakenRequestBudget()
    client = _client(transport, budget=budget)

    with pytest.raises(KrakenTransportError):
        client.get_extended_balances()

    assert calls == 1
    assert budget.private_cost_spent == 1


def test_request_budget_uses_conservative_endpoint_costs_and_fails_before_io() -> None:
    assert CONSERVATIVE_PRIVATE_COST_LIMIT == 20
    assert PRIVATE_ENDPOINT_COSTS["ClosedOrders"] == 4
    budget = KrakenRequestBudget(private_cost_limit=4)
    transport = QueueTransport(_response({"closed": {}, "count": 0}))
    client = _client(transport, budget=budget)

    client.get_closed_orders()
    assert client.private_cost_spent == 4
    assert client.private_cost_remaining == 0
    with pytest.raises(KrakenReadError, match="budget is exhausted"):
        client.get_extended_balances()

    assert budget.private_cost_remaining == 0
    assert len(transport.requests) == 1


def test_pacing_hook_observes_public_and_private_cost_without_request_data() -> None:
    observations: list[tuple[str, int]] = []
    transport = QueueTransport(
        _response({"unixtime": int(NOW.timestamp())}),
        _response({}),
    )
    client = _client(
        transport, pacing_hook=lambda endpoint, cost: observations.append((endpoint, cost))
    )

    client.get_server_time()
    client.get_extended_balances()

    assert observations == [("public:Time", 1), ("private:BalanceEx", 1)]


def test_monotonic_nonce_advances_when_clock_stalls_or_moves_backwards() -> None:
    values = iter((2_000_000, 2_000_000, 1_000_000))
    nonce = MonotonicNonce(time_ns=lambda: next(values))

    assert [nonce(), nonce(), nonce()] == [2, 3, 4]


def test_private_calls_require_credentials_and_inputs_are_bounded() -> None:
    client = KrakenReadClient(transport=QueueTransport(), clock=lambda: NOW)

    with pytest.raises(KrakenReadError, match="credentials"):
        client.get_extended_balances()
    with pytest.raises(ValueError, match="1-50"):
        _client(QueueTransport()).query_orders([])
    with pytest.raises(ValueError, match="1 and 100"):
        _client(QueueTransport()).get_trades_history(limit=101)
    with pytest.raises(ValueError, match="later"):
        _client(QueueTransport()).get_closed_orders(start=20, end=10)
    with pytest.raises(ValueError, match="ASCII"):
        _client(QueueTransport()).get_open_orders(client_order_id="line\nbreak")
    with pytest.raises(ValueError, match="valid Kraken"):
        _client(QueueTransport()).query_orders(["O-ONE\n"])


@pytest.mark.parametrize(
    ("api_key", "api_secret", "message"),
    [
        ("line\nbreak", API_SECRET, "printable ASCII"),
        ("public-fixture-credential", "definitely-not-base64!", "valid base64"),
        ("public-fixture-credential", "", "non-empty"),
        ("public-fixture-credential", "A" * 4097, "too long"),
    ],
)
def test_credentials_are_validated_without_echoing_them(
    api_key: str, api_secret: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message) as caught:
        KrakenReadClient(api_key=api_key, api_secret=api_secret)

    rendered = str(caught.value)
    assert api_key not in rendered
    if api_secret:
        assert api_secret not in rendered


def test_injected_oversize_response_is_rejected() -> None:
    client = KrakenReadClient(
        transport=QueueTransport(b"x" * 11),
        clock=lambda: NOW,
        max_response_bytes=10,
    )

    with pytest.raises(KrakenTransportError, match="size limit"):
        client.get_server_time()
