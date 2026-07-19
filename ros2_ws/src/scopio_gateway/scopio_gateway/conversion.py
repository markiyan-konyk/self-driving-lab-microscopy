"""JSON <-> ROS 2 message conversion for the gateway.

Two directions:

  build_msg(cls, data)  -- JSON dict -> ROS message instance (for service
                           requests, published messages and action goals).
  msg_to_jsonable(msg)  -- ROS message -> plain JSON-safe python structure.

THE NaN RULE (important, documented in docs/API.md):
Several SCOPIO interfaces (SetCameraControls, CalibrationSet) use NaN as the
"leave this field unchanged" sentinel. JSON has no NaN, and a float field
omitted from the request would be built as 0.0 -- actively clobbering the
setting. So on the way IN, every float/double field that is absent or null in
the JSON body becomes NaN? No -- absent stays at the message default (0.0 for
plain floats), because most interfaces (MoveAbs, StageJog fields...) expect 0
defaults. Instead the rule is:

  * JSON null on a float/double field  -> NaN  ("leave unchanged")
  * absent float field                 -> message default (0.0)

and the scopio_client SDK convenience methods pre-fill null for you on the
NaN-sentinel services, so `scope.camera.set_controls(contrast=1.2)` never
clobbers the other controls. Raw callers of the generic endpoint must send
null (or the string "nan") explicitly for fields they want left alone.

On the way OUT, NaN/inf become null (JSON-safe).
"""

import math
from collections import OrderedDict

from rosidl_runtime_py import message_to_ordereddict, set_message_fields
from rosidl_runtime_py.utilities import get_message

FLOAT_TYPES = ("float", "double", "float32", "float64")

# Topics whose messages are far too large to serialize as JSON over the
# generic WebSocket path. Video is served properly at /api/v1/stream.mjpg.
BULKY_TYPES = {
    "sensor_msgs/msg/CompressedImage",
    "sensor_msgs/msg/Image",
}


def normalize_msg_type(type_str):
    """Accept 'pkg/msg/Name' or 'pkg/Name' and return 'pkg/msg/Name'."""
    parts = type_str.split("/")
    if len(parts) == 2:
        return f"{parts[0]}/msg/{parts[1]}"
    return type_str


def _is_float_field(field_type):
    return field_type in FLOAT_TYPES


def _is_float_sequence(field_type):
    # e.g. "sequence<double>", "double[3]"
    for f in FLOAT_TYPES:
        if field_type.startswith(f"sequence<{f}") or field_type.startswith(f"{f}["):
            return True
    return False


def _nested_msg_type(field_type):
    """Return the message type string of a nested/sequence message field, else None."""
    inner = field_type
    if inner.startswith("sequence<"):
        inner = inner[len("sequence<"):].split(",")[0].rstrip(">")
    inner = inner.split("[")[0]
    return inner if "/" in inner else None


def _prepare(data, msg_cls):
    """Recursively map JSON null -> NaN on float fields (the NaN rule)."""
    if not isinstance(data, dict):
        return data
    out = {}
    fields = msg_cls.get_fields_and_field_types()
    for key, value in data.items():
        ftype = fields.get(key)
        if ftype is None:
            out[key] = value  # unknown field: let set_message_fields raise
            continue
        if _is_float_field(ftype):
            if value is None or (isinstance(value, str) and value.lower() == "nan"):
                out[key] = float("nan")
            else:
                out[key] = value
        elif _is_float_sequence(ftype) and isinstance(value, list):
            out[key] = [float("nan") if v is None else v for v in value]
        else:
            nested = _nested_msg_type(ftype)
            if nested is not None:
                nested_cls = get_message(normalize_msg_type(nested))
                if isinstance(value, list):
                    out[key] = [_prepare(v, nested_cls) for v in value]
                else:
                    out[key] = _prepare(value, nested_cls)
            else:
                out[key] = value
    return out


def build_msg(msg_cls, data):
    """Build a ROS message of type msg_cls from a JSON-derived dict.

    Raises ValueError with a readable message on bad/unknown fields.
    """
    msg = msg_cls()
    if data:
        try:
            set_message_fields(msg, _prepare(data, msg_cls))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(str(exc)) from exc
    return msg


def _sanitize(value, byte_cap=65536):
    """Make a message_to_ordereddict result JSON-safe."""
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, (bytes, bytearray)):
        return list(value[:byte_cap])
    if isinstance(value, (OrderedDict, dict)):
        return {k: _sanitize(v, byte_cap) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v, byte_cap) for v in value]
    if hasattr(value, "tolist"):  # array.array / numpy arrays
        return _sanitize(value.tolist(), byte_cap)
    return value


def msg_to_jsonable(msg):
    """ROS message -> JSON-safe dict (NaN/inf -> null, byte arrays -> lists)."""
    return _sanitize(message_to_ordereddict(msg))


def field_schema(msg_cls):
    """{field: type} description of a message class, for /interfaces."""
    return dict(msg_cls.get_fields_and_field_types())
