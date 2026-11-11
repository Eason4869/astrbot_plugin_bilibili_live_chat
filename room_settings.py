"""Validated dialogue overrides; omitted fields inherit shared settings."""

import json
from pathlib import Path

TEXT_FIELDS = {
    "MODEL_PROVIDER_ID": 256,
    "ASTRBOT_PERSONA_ID": 256,
    "PERSONA_PROMPT": 4000,
    "REPLY_INSTRUCTION": 2000,
    "ECHO_INSTRUCTION": 2000,
    "ECHO_DECISION_PROVIDER_ID": 256,
}
LIST_FIELDS = ("KEYWORDS", "VOICE_KEYWORD_ALIASES")
BOOL_FIELDS = ("KEYWORD_MODE", "ECHO_ENABLED", "ECHO_DECISION_ENABLED")
NUMBER_FIELDS = {
    "CROSS_SOURCE_DEDUP_SECONDS": (0, 30),
    "ECHO_DECISION_THRESHOLD": (0.5, 1),
}
ENUM_FIELDS = {'LISTEN_MODE': ('poll', 'auto')}
DIALOGUE_FIELDS = (*TEXT_FIELDS, *LIST_FIELDS, *BOOL_FIELDS, *NUMBER_FIELDS, *ENUM_FIELDS)
SCHEMA = json.loads(
    Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8")
)


class RoomSettingsError(Exception):
    pass


def validate_dialogue(payload):
    if not isinstance(payload, dict) or not set(payload) <= set(DIALOGUE_FIELDS):
        raise RoomSettingsError("包含未知或不可覆盖的房间对话设置。")
    result = {}
    for key, value in payload.items():
        if value is None:
            result[key] = None
            continue
        if key in TEXT_FIELDS:
            valid = isinstance(value, str) and len(value) <= TEXT_FIELDS[key]
            if valid:
                value = value.strip()
        elif key in LIST_FIELDS:
            valid = (
                isinstance(value, list)
                and len(value) <= 100
                and all(isinstance(word, str) and len(word) <= 80 for word in value)
            )
            if valid:
                value = list(
                    dict.fromkeys(word.strip() for word in value if word.strip())
                )
        elif key in BOOL_FIELDS:
            valid = type(value) is bool
        elif key in ENUM_FIELDS:
            valid = isinstance(value, str) and value in ENUM_FIELDS[key]
        else:
            low, high = NUMBER_FIELDS[key]
            valid = type(value) in (int, float) and low <= value <= high
        if not valid:
            raise RoomSettingsError(f"请检查房间设置「{SCHEMA[key]['description']}」。")
        result[key] = value
    return result


def saved_overrides(value):
    result = {}
    if isinstance(value, dict):
        for key, item in value.items():
            try:
                if item is not None:
                    result.update(validate_dialogue({key: item}))
            except RoomSettingsError:
                continue
    return result
