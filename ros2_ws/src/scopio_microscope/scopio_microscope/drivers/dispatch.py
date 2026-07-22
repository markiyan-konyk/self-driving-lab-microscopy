"""Turn a driver class into a ROS service surface: call a method by name.

Shared by galvo_node (WaveGen) and temperature_node (TCLab). The node owns the
instrument object; a client sends `{method, args, kwargs}` as JSON strings
(scopio_interfaces/srv/InstrumentCall) and gets the JSON-encoded return value
back. That is the whole API -- every method the driver class has ever had, or
will have, is reachable the day it is written, with no new .srv files, no
gateway change and no client update.

Why JSON strings and not typed fields: ROS request fields are statically typed,
so a typed API would need one service per method signature -- exactly the
combinatorial explosion this design avoids (see docs/DECISIONS.md).

Safety rails, deliberately thin:
  * private methods (leading underscore) are not reachable;
  * BLOCKED holds the few public methods that would break the NODE rather than
    do instrument work (tearing down the session it owns);
  * everything else is fair game -- the client is trusted, it already has an
    API key, and a raw `.command("...")` escape hatch exists anyway.
"""

import inspect
import json
import math

# Public driver methods a client must NOT reach: close() drops the VISA session
# the node holds, and the driver cannot re-open it by itself (the node's own
# reconnect path does that -- exposed as the "reconnect" meta-method).
BLOCKED = frozenset({"close"})


class DispatchError(Exception):
    """Bad request: unknown method, unparseable args, wrong arity."""


def parse_args(args_json, kwargs_json):
    """'[25.0]' / '{"channel": 2}' -> ([25.0], {"channel": 2}). Empty -> ()/{}.

    A bare scalar is accepted as a single positional arg ("25.0" == "[25.0]"),
    because that is what people type by hand at a curl prompt.
    """
    args = _loads(args_json, "args", default=[])
    kwargs = _loads(kwargs_json, "kwargs", default={})
    if not isinstance(args, list):
        args = [args]
    if not isinstance(kwargs, dict):
        raise DispatchError(f"kwargs must be a JSON object, got {type(kwargs).__name__}")
    return args, kwargs


def _loads(raw, label, default):
    if raw is None or not str(raw).strip():
        return default
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise DispatchError(f"{label} is not valid JSON: {exc}") from exc


def to_json(value):
    """JSON-encode a return value; NaN/inf -> null so strict parsers survive."""
    return json.dumps(_sanitize(value), default=str)


def _sanitize(value):
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, dict):
        return {str(k): _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return list(value)
    return value


def resolve(driver, method):
    """Look up a callable public method, or raise DispatchError."""
    if not method or method.startswith("_"):
        raise DispatchError(f"'{method}' is not a public method")
    if method in BLOCKED:
        raise DispatchError(f"'{method}' is not callable through the node "
                            "(it would tear down the session the node owns)")
    attr = getattr(driver, method, None)
    if attr is None:
        raise DispatchError(f"no such method '{method}' on "
                            f"{type(driver).__name__} -- call 'list_methods' "
                            "to see what exists")
    if not callable(attr):
        raise DispatchError(f"'{method}' is an attribute, not a method")
    return attr


def call(driver, method, args_json="", kwargs_json=""):
    """Dispatch one call. Returns the JSON-encoded result.

    Raises DispatchError for bad requests; anything the instrument itself
    raises propagates to the caller (the node reports it as `error`).
    """
    fn = resolve(driver, method)
    args, kwargs = parse_args(args_json, kwargs_json)
    try:
        inspect.signature(fn).bind(*args, **kwargs)
    except TypeError as exc:      # wrong arity/name -- a request error, not a fault
        raise DispatchError(f"{method}{inspect.signature(fn)}: {exc}") from exc
    return to_json(fn(*args, **kwargs))


def is_link_error(exc):
    """True when an exception means "the instrument link is broken", false when
    it means "that call was wrong" (a float() on a garbled reply, a divide by
    zero in a helper...). Nodes use it to decide whether to tear the session
    down and reconnect, so one client's bad argument cannot knock the
    instrument offline for everyone.

    Matched by NAME for the VISA/driver types on purpose: this module stays
    import-free of pyvisa so it can be reasoned about (and unit-tested) without
    any hardware stack installed.
    """
    if isinstance(exc, (OSError, EOFError)):        # includes ConnectionError
        return True
    return type(exc).__name__ in (
        "VisaIOError", "InvalidSession", "LibraryError",   # pyvisa
        "TCLabError",                                      # drivers/tclab.py
    )


def describe(driver_cls, extra=()):
    """[{name, signature, doc}] for every callable a client may use.

    Introspects the CLASS, so `list_methods` still answers while the hardware is
    disconnected. `extra` documents the node's own meta-methods.
    """
    out = []
    # getmembers, not vars(): it walks the MRO, so a driver that subclasses
    # another (or gains a mixin) still lists everything a client can call.
    for name, member in inspect.getmembers(driver_cls, inspect.isroutine):
        if name.startswith("_") or name in BLOCKED:
            continue
        doc = (inspect.getdoc(member) or "").strip().splitlines()
        out.append({
            "name": name,
            "signature": _public_signature(member),
            "doc": doc[0] if doc else "",
        })
    out.extend(dict(e) for e in extra)
    return out


def _public_signature(member):
    """Signature as a CLIENT sees it -- i.e. without the bound `self`."""
    try:
        sig = inspect.signature(member)
    except (TypeError, ValueError):
        return "(...)"
    params = list(sig.parameters.values())
    if params and params[0].name == "self":
        sig = sig.replace(parameters=params[1:])
    return str(sig)
