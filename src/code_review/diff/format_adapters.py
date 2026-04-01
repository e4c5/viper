"""Adapters for converting provider-specific diff payloads into unified diff text."""

from __future__ import annotations

_DEV_NULL = "/dev/null"


def _diff_file_headers(src_path: str, dst_path: str) -> list[str]:
    """Return the three header lines for one file in a unified diff."""
    src_header = _DEV_NULL if src_path == _DEV_NULL else f"a/{src_path}"
    dst_header = _DEV_NULL if dst_path == _DEV_NULL else f"b/{dst_path}"
    effective_path = dst_path if dst_path != _DEV_NULL else src_path
    return [
        f"diff --git a/{effective_path} b/{effective_path}",
        f"--- {src_header}",
        f"+++ {dst_header}",
    ]


def _hunk_header(hunk: dict) -> str:
    """Return the ``@@ -old +new @@`` header line for one hunk dict."""
    src_start = hunk.get("sourceLine", 0)
    src_span = hunk.get("sourceSpan", 0)
    dst_start = hunk.get("destinationLine", 0)
    dst_span = hunk.get("destinationSpan", 0)
    return f"@@ -{src_start},{src_span} +{dst_start},{dst_span} @@"


def _segment_lines(segment: dict) -> list[str]:
    """Return unified diff lines for one Bitbucket Server diff segment."""
    seg_type = segment.get("type", "CONTEXT")
    prefixes = {"ADDED": "+", "REMOVED": "-"}
    prefix = prefixes.get(seg_type, " ")
    return [f"{prefix}{entry.get('line', '')}" for entry in segment.get("lines") or []]


def bitbucket_json_diff_to_unified(data: dict) -> str:
    """Convert a Bitbucket Server JSON diff response to unified diff text.

    Bitbucket Server returns structured JSON under ``diffs`` instead of a
    standard unified diff string. This adapter converts that payload into the
    format expected by the shared diff parser.
    """
    output: list[str] = []
    for file_diff in data.get("diffs") or []:
        src_path = (file_diff.get("source") or {}).get("toString") or _DEV_NULL
        dst_path = (file_diff.get("destination") or {}).get("toString") or _DEV_NULL
        output.extend(_diff_file_headers(src_path, dst_path))
        for hunk in file_diff.get("hunks") or []:
            output.append(_hunk_header(hunk))
            for segment in hunk.get("segments") or []:
                output.extend(_segment_lines(segment))
    return "\n".join(output)


__all__ = ["bitbucket_json_diff_to_unified"]
