"""Declarative, core-owned provider storage contracts. Data only; no I/O.

Storage selects persistence behavior only from the contract resolved for a
captured snapshot provider and captured source. Records are frozen, slotted
and exact-typed, so they cannot carry callables, hooks or subclass overrides.
Unreserved providers (outside every declared namespace) keep the historical
unreserved storage contract.

The only executable provider-specific behavior reachable from storage is the
closed Revision 3 compatibility exception: ``LEGACY_POLICIES`` binds exactly
two historical policy IDs to one stream and one exact source contract each,
and ``run_legacy_policy`` invokes the frozen Revision 3 validator.

CHANGE CONTROL: any change to this module or to ``legacy_cheaper_inference.py``
requires storage-intensity review. ``LEGACY_POLICIES`` may only shrink.
``legacy_cheaper_inference.py`` must remain byte-identical to
``2fb4be6:src/model_price_watcher/providers/cheaper_inference.py`` until a
reviewed gate says otherwise. Adding a provider record is a storage-intensity
change and must check for collisions with existing unreserved provider rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from model_price_watcher import legacy_cheaper_inference as _legacy
from model_price_watcher.models import SourceMetadata


class RegistryError(RuntimeError):
    """Internal: the installed provider registry is invalid. Never a stored-row fault."""


CAPTURE_STRICT, CAPTURE_LEGACY = "strict", "legacy"
QUOTE_FORBIDDEN, QUOTE_LEGACY_POLICY = "forbidden", "legacy_policy"
PRICES_FORBIDDEN, PRICES_PERMITTED = "forbidden", "permitted"

_CAPTURES = (CAPTURE_STRICT, CAPTURE_LEGACY)
_QUOTES = (QUOTE_FORBIDDEN, QUOTE_LEGACY_POLICY)
_PRICES = (PRICES_FORBIDDEN, PRICES_PERMITTED)


def _exact_str(value, name):
    if type(value) is not str:
        raise TypeError(f"{name} must be exact str")


def _exact_tuple(value, name):
    if type(value) is not tuple:
        raise TypeError(f"{name} must be exact tuple")


def _choice(value, name, allowed):
    _exact_str(value, name)
    if value not in allowed:
        raise ValueError(f"unsupported {name}")


def _check_contract(contract):
    """Raise TypeError/ValueError unless ``contract`` is a valid SourceContract."""
    if type(contract) is not SourceContract:
        raise TypeError("contract must be exact SourceContract")
    _exact_str(contract.contract_id, "contract_id")
    _exact_str(contract.location, "location")
    if contract.location == "" or "\0" in contract.location:
        raise ValueError("location must be nonempty and NUL-free")
    _exact_tuple(contract.metadata, "metadata")
    keys = []
    for pair in contract.metadata:
        _exact_tuple(pair, "metadata pair")
        if len(pair) != 2:
            raise ValueError("metadata pairs must have two items")
        _exact_str(pair[0], "metadata key")
        _exact_str(pair[1], "metadata value")
        keys.append(pair[0])
    if any(not a < b for a, b in zip(keys, keys[1:])):
        raise ValueError("metadata keys must be strictly sorted and unique")
    values = dict(contract.metadata)
    if "source_contract" not in values:
        raise ValueError("metadata must declare source_contract")
    if values["source_contract"] != contract.contract_id:
        raise ValueError("contract_id must equal metadata source_contract")
    _choice(contract.capture, "capture", _CAPTURES)
    _choice(contract.quote_column, "quote_column", _QUOTES)
    _choice(contract.price_columns, "price_columns", _PRICES)
    if contract.legacy_policy is not None:
        _exact_str(contract.legacy_policy, "legacy_policy")
    elif contract.capture == CAPTURE_LEGACY or contract.quote_column == QUOTE_LEGACY_POLICY:
        raise ValueError("legacy capture or quote rules require a legacy policy")


def _check_record(record):
    """Raise TypeError/ValueError unless ``record`` is a valid ProviderRecord."""
    if type(record) is not ProviderRecord:
        raise TypeError("record must be exact ProviderRecord")
    provider_id = record.provider_id
    _exact_str(provider_id, "provider_id")
    if (not provider_id or provider_id.strip() != provider_id
            or "\0" in provider_id or "." in provider_id):
        raise ValueError("provider_id must be nonblank, unpadded, NUL-free and undotted")
    prefix = provider_id + "."
    _exact_tuple(record.streams, "streams")
    if not record.streams:
        raise ValueError("streams must be nonempty")
    for stream in record.streams:
        _exact_str(stream, "stream")
        if not stream.startswith(prefix) or stream == prefix or "\0" in stream:
            raise ValueError("stream must be inside the record namespace")
    if len(set(record.streams)) != len(record.streams):
        raise ValueError("streams must be unique")
    _exact_tuple(record.contracts, "contracts")
    if not record.contracts:
        raise ValueError("contracts must be nonempty")
    for contract in record.contracts:
        _check_contract(contract)
    ids = [c.contract_id for c in record.contracts]
    sources = [(c.location, c.metadata) for c in record.contracts]
    if len(set(ids)) != len(ids) or len(set(sources)) != len(sources):
        raise ValueError("contracts must be unique")


@dataclass(frozen=True, slots=True)
class SourceContract:
    contract_id: str
    location: str
    metadata: tuple[tuple[str, str], ...]
    capture: str
    quote_column: str
    price_columns: str
    legacy_policy: str | None = None

    def __post_init__(self):
        _check_contract(self)


@dataclass(frozen=True, slots=True)
class ProviderRecord:
    provider_id: str
    streams: tuple[str, ...]
    contracts: tuple[SourceContract, ...]

    def __post_init__(self):
        _check_record(self)


def _pairs(metadata):
    return tuple(sorted(metadata.items()))


LEGACY_POLICIES = MappingProxyType({
    _legacy.V1_INTERPRETATION_POLICY: (_legacy.STREAM_ID, _legacy.V1_SOURCE_METADATA["source_contract"]),
    _legacy.INTERPRETATION_POLICY: (_legacy.STREAM_ID, _legacy.SOURCE_METADATA["source_contract"]),
})

CHEAPER_INFERENCE_RECORD = ProviderRecord(
    provider_id="cheaper_inference",
    streams=(_legacy.STREAM_ID,),
    contracts=(
        SourceContract(
            contract_id=_legacy.V1_SOURCE_METADATA["source_contract"],
            location=_legacy.SOURCE_URL,
            metadata=_pairs(_legacy.V1_SOURCE_METADATA),
            capture=CAPTURE_LEGACY, quote_column=QUOTE_LEGACY_POLICY,
            price_columns=PRICES_FORBIDDEN,
            legacy_policy=_legacy.V1_INTERPRETATION_POLICY),
        SourceContract(
            contract_id=_legacy.SOURCE_METADATA["source_contract"],
            location=_legacy.SOURCE_URL,
            metadata=_pairs(_legacy.SOURCE_METADATA),
            capture=CAPTURE_STRICT, quote_column=QUOTE_FORBIDDEN,
            price_columns=PRICES_FORBIDDEN,
            legacy_policy=_legacy.INTERPRETATION_POLICY),
    ),
)

PROVIDER_RECORDS = (CHEAPER_INFERENCE_RECORD,)


def _validate_registry():
    """Validate the installed registry, read at call time; raise RegistryError."""
    records = PROVIDER_RECORDS
    policies = LEGACY_POLICIES
    if type(records) is not tuple:
        raise RegistryError("PROVIDER_RECORDS must be an exact tuple")
    if type(policies) is not MappingProxyType:
        raise RegistryError("LEGACY_POLICIES must be a MappingProxyType")
    for key, binding in policies.items():
        if (type(key) is not str or type(binding) is not tuple or len(binding) != 2
                or any(type(part) is not str for part in binding)):
            raise RegistryError("LEGACY_POLICIES entries must be exact str -> (str, str)")
    for record in records:
        if type(record) is not ProviderRecord:
            raise RegistryError("registry entries must be exact ProviderRecord")
        try:
            _check_record(record)
        except (TypeError, ValueError) as error:
            raise RegistryError("invalid provider record") from error
    namespaces = [record.provider_id for record in records]
    for index, first in enumerate(namespaces):
        for second in namespaces[index + 1:]:
            if (first == second or first.startswith(second + ".")
                    or second.startswith(first + ".")):
                raise RegistryError("provider namespaces must be unique and non-overlapping")
    contract_ids = [c.contract_id for record in records for c in record.contracts]
    if len(set(contract_ids)) != len(contract_ids):
        raise RegistryError("contract_id must be unique across the registry")
    used = set()
    for record in records:
        for contract in record.contracts:
            policy = contract.legacy_policy
            if policy is None:
                continue
            if policy not in policies or policy in used:
                raise RegistryError("unknown, unbound or reused legacy policy")
            used.add(policy)
            stream, contract_id = policies[policy]
            if (record.streams != (stream,) or contract.contract_id != contract_id
                    or dict(contract.metadata).get("interpretation_policy") != policy):
                raise RegistryError("legacy policy binding mismatch")
    return records


def reserved_record(provider):
    """Return the record whose namespace contains ``provider``, or None.

    Matching uses built-in string content only, never caller overrides. A
    non-exact ``str`` with reserved content raises TypeError.
    """
    records = _validate_registry()
    if not isinstance(provider, str):
        return None
    for record in records:
        provider_id = record.provider_id
        if str.__eq__(provider, provider_id) or str.startswith(provider, provider_id + "."):
            if type(provider) is not str:
                raise TypeError("reserved provider must be exact str")
            return record
    return None


def resolve_contract(record, provider, source):
    """Return the declared contract for an exact stream and exact source."""
    _validate_registry()
    if type(record) is not ProviderRecord:
        raise RegistryError("record must be exact ProviderRecord")
    if not any(str.__eq__(provider, stream) is True for stream in record.streams):
        raise ValueError("unsupported reserved advertised stream")
    if type(source) is not SourceMetadata:
        raise TypeError("source must be SourceMetadata")
    if type(source.location) is not str or type(source.metadata) is not dict:
        raise TypeError("invalid source fields")
    if any(type(k) is not str or type(v) is not str for k, v in source.metadata.items()):
        raise TypeError("source metadata must contain exact strings")
    for contract in record.contracts:
        if source.location == contract.location and source.metadata == dict(contract.metadata):
            return contract
    raise ValueError("source must declare an exact public catalog contract")


def run_legacy_policy(contract, observation, *, source):
    """Run the closed Revision 3 validator for a bound legacy contract."""
    _validate_registry()
    if (type(contract) is not SourceContract or contract.legacy_policy is None
            or contract.legacy_policy not in LEGACY_POLICIES):
        raise RegistryError("contract has no bound legacy policy")
    # Module-attribute lookup at call time: the name production resolves.
    _legacy.validate_public_observation(observation, source=source)
