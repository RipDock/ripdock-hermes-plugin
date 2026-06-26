"""RipDock Runtime content formatting helpers."""

import json


PROTOCOL_VERSION = "1"

RIPDOCK_RICH_TEXT_V1_CAPABILITIES = {
    "content_rendering": {
        "plain_text": True,
        "basic_markdown": True,
        "rich_text_v1": True,
        "json": True,
        "yaml": True,
        "code_blocks": True,
        "external_links": True,
    },
    "rich_text_v1": {
        "bold": True,
        "italic": True,
        "underline": True,
        "inline_code": True,
        "code_blocks": True,
        "lists": True,
        "quotes": True,
    },
}

def formatting_capability_summary(capabilities):
    payload = capabilities.get("payload") if isinstance(capabilities, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    client_capabilities = payload.get("client_capabilities")
    if not isinstance(client_capabilities, dict):
        client_capabilities = RIPDOCK_RICH_TEXT_V1_CAPABILITIES
    return "Advertised client capabilities: " + json.dumps(client_capabilities, sort_keys=True)


def classify_runtime_content(text):
    if not isinstance(text, str) or not text:
        return {"kind": "plain_text", "complete": True}

    fences = _fenced_blocks(text)
    if not fences:
        return {"kind": "plain_text", "complete": True}

    if len(fences) != 1 or not _is_single_fenced_block(text, fences[0]):
        return {"kind": "markdown", "complete": True}

    last = fences[-1]
    language = last["language"]
    if language in {"json", "jsonc"}:
        kind = "fenced_json"
    elif language in {"yaml", "yml"}:
        kind = "fenced_yaml"
    else:
        kind = "fenced_code"
    return {
        "kind": kind,
        "language": language,
        "complete": last["complete"],
    }


def _fenced_blocks(text):
    blocks = []
    active = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("```"):
            continue
        marker = stripped[3:].strip()
        if active is None:
            active = {
                "language": marker.split()[0].lower() if marker else "",
                "complete": False,
            }
        else:
            active["complete"] = True
            blocks.append(active)
            active = None
    if active is not None:
        blocks.append(active)
    return blocks


def _is_single_fenced_block(text, block):
    lines = text.splitlines()
    fence_lines = [
        index
        for index, line in enumerate(lines)
        if line.strip().startswith("```")
    ]
    if len(fence_lines) != 2:
        return False

    before = "\n".join(lines[: fence_lines[0]]).strip()
    after = "\n".join(lines[fence_lines[1] + 1 :]).strip()
    return not before and not after and bool(block.get("complete"))
