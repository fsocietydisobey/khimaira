"""msgpack-aware state deserializer for AsyncPostgresSaver checkpoints.

LangGraph's AsyncPostgresSaver writes serialized state as `(type, data)`
pairs where `type` is a short string identifier ("msgpack", "json",
"pickle", etc.). This decoder handles the common types and degrades
gracefully for unknown types (returns an opaque-blob marker rather
than crashing — exactly what the inspector needs).
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

_OPAQUE = "<opaque blob — unknown encoding>"


def to_jsonable(value: Any, _depth: int = 0) -> Any:
    """Recursively convert a decoded state value into something FastAPI's
    `jsonable_encoder` can safely traverse.

    LangGraph state can contain types that defy default JSON encoding:
      - `langgraph.types.Send` / `Command` / `Interrupt` (dataclass-like
        primitives whose `__iter__` raises rather than yielding kv pairs;
        `jsonable_encoder` mistakes them for sequences and crashes)
      - LangChain `BaseMessage` subclasses
      - Pydantic models (handled by jsonable_encoder, but we normalize
        for cross-version safety)
      - Sets, tuples, bytes, datetimes — usually fine but vary by version

    Strategy: pass through known-safe primitives; recurse into dicts/
    lists/tuples/sets; for anything else, prefer `model_dump()`,
    `dict()`, `dataclasses.asdict()`, `__dict__`, then a typed marker.

    Depth-capped at 200 to short-circuit pathological cycles.
    """
    if _depth > 200:
        return {"__deep__": True, "type": type(value).__name__}

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        # Bytes inside state are usually serialization noise — represent
        # by a short prefix + length so the inspector can show something.
        try:
            return {"__bytes__": True, "size": len(value), "preview": value[:32].hex()}
        except Exception:
            return {"__bytes__": True}

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = k if isinstance(k, str) else str(k)
            out[key] = to_jsonable(v, _depth + 1)
        return out

    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v, _depth + 1) for v in value]

    # Pydantic v2 / v1
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return to_jsonable(dump(), _depth + 1)
        except Exception:
            pass
    dump_v1 = getattr(value, "dict", None)
    if callable(dump_v1):
        try:
            return to_jsonable(dump_v1(), _depth + 1)
        except Exception:
            pass

    # Dataclasses (LangGraph's Send/Command are dataclass-like)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return to_jsonable(dataclasses.asdict(value), _depth + 1)
        except Exception:
            pass

    # Last resort — read attribute dict, tag with the type name so the
    # UI can show "what was this".
    obj_dict = getattr(value, "__dict__", None)
    if isinstance(obj_dict, dict):
        return {
            "__type__": type(value).__name__,
            **{k: to_jsonable(v, _depth + 1) for k, v in obj_dict.items() if not k.startswith("_")},
        }

    # Truly unknown — repr it.
    return {"__type__": type(value).__name__, "repr": repr(value)[:200]}


def decode(serializer_type: str | None, data: Any) -> Any:
    """Decode a (type, data) pair from a checkpoint row.

    Returns a Python object suitable for JSON serialization (after
    redaction). Unknown types return a marker dict instead of raising.

    Pass-throughs:
      - `data is None` → None
      - `data` is already a dict / list (e.g. psycopg returned jsonb as
        a Python object) → returned as-is. The serializer_type is ignored
        in that case because there's nothing to decode.
    """
    if data is None:
        return None
    if isinstance(data, (dict, list)):
        return data

    serializer_type = (serializer_type or "").lower()

    if serializer_type in ("json", "application/json"):
        try:
            return json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return {"__opaque__": True, "encoding": serializer_type, "size": len(data)}

    if serializer_type in ("msgpack", "ormsgpack", "application/x-msgpack"):
        return _decode_msgpack(data, serializer_type)

    if serializer_type in ("", "raw", "bytes"):
        # No type hint — try JSON first (strict, won't accept non-JSON),
        # then msgpack, then opaque. JSON-first because msgpack will happily
        # decode arbitrary bytes that start with a valid format byte
        # (e.g. `{` is byte 0x7b which is a valid msgpack positive fixint).
        try:
            return json.loads(data)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            pass
        try:
            return _decode_msgpack(data, "msgpack")
        except _DecodeError:
            pass
        return {"__opaque__": True, "encoding": "raw", "size": len(data)}

    return {"__opaque__": True, "encoding": serializer_type, "size": len(data) if data else 0}


class _DecodeError(Exception):
    pass


def _decode_msgpack(data: bytes, encoding: str) -> Any:
    """Decode a msgpack-encoded checkpoint payload.

    LangGraph wraps payloads with a JsonPlus extension protocol that
    encodes Pydantic models, datetimes, sets, etc. as ext-typed msgpack
    values. Plain `ormsgpack.unpackb(data)` can't restore those types
    and will raise. The right deserializer is LangGraph's own
    `JsonPlusSerializer` — it ships with the framework and reverses
    every type the checkpointer wrote.

    Falls back to raw ormsgpack/msgpack only if JsonPlus isn't
    importable (which would mean langgraph itself is missing — caller
    will hit other errors first, but this gives a useful path for
    apps that wrote msgpack outside LangGraph).
    """
    # Primary path — LangGraph's deserializer.
    try:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        try:
            return JsonPlusSerializer().loads_typed((encoding, data))
        except Exception as exc:
            # Surface the real error rather than masking it with a
            # downstream import failure. Caller handles _DecodeError.
            raise _DecodeError(f"JsonPlus loads_typed failed: {exc}") from exc
    except ImportError:
        pass

    # Bare-msgpack fallback for data not produced by LangGraph.
    try:
        import ormsgpack  # type: ignore
        try:
            return ormsgpack.unpackb(data)
        except Exception as exc:
            raise _DecodeError(f"ormsgpack unpackb failed: {exc}") from exc
    except ImportError:
        pass

    try:
        import msgpack  # type: ignore
        try:
            return msgpack.unpackb(data, raw=False)
        except Exception as exc:
            raise _DecodeError(str(exc)) from exc
    except ImportError as exc:
        raise _DecodeError(f"no msgpack lib available: {exc}") from exc
