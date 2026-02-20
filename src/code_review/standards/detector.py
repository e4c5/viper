"""Language and framework detection from file paths and content."""

import re
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class DetectedContext(BaseModel):
    """Result of language/framework detection."""

    language: str = Field(..., description="Primary language (e.g. python, javascript)")
    framework: str | None = Field(default=None, description="Detected framework (e.g. django, fastapi)")
    confidence: Literal["high", "medium", "low"] = Field(
        ..., description="Confidence based on signal strength"
    )


# File extension -> language
_EXT_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
}

# Path/config pattern -> (language, framework | None)
_PATH_SIGNALS: dict[str, tuple[str, str | None]] = {
    "requirements.txt": ("python", None),
    "pyproject.toml": ("python", None),
    "package.json": ("javascript", None),
    "next.config.js": ("javascript", "nextjs"),
    "next.config.mjs": ("javascript", "nextjs"),
    "next.config.ts": ("javascript", "nextjs"),
    "go.mod": ("go", None),
    "pom.xml": ("java", None),
    "build.gradle": ("java", None),
    "build.gradle.kts": ("java", None),
    "CMakeLists.txt": ("cpp", None),
    "Makefile": ("cpp", None),
    "meson.build": ("cpp", None),
}

# Dependency names (lowercase) -> framework
_PYTHON_FRAMEWORKS: set[str] = {"django", "flask", "fastapi", "starlette"}
_JAVA_FRAMEWORKS: set[str] = {"spring-boot", "springboot", "jakarta"}


def _normalize_path(p: str) -> str:
    return p.replace("\\", "/").strip("/")


def _extract_python_frameworks(content: str) -> list[str]:
    """Extract Python package names from requirements.txt or pyproject.toml content."""
    found: list[str] = []
    for line in content.splitlines():
        line = line.strip().lower()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        # requirements.txt: package==version or package>=version
        pkg = line.split("==")[0].split(">=")[0].split("[")[0].strip()
        if pkg in _PYTHON_FRAMEWORKS:
            found.append(pkg)
        # pyproject.toml: "django" or django = "^4.0" (match as standalone token, not e.g. my-django-app)
        for fw in _PYTHON_FRAMEWORKS:
            if fw not in found and re.search(rf"(?<!-)\b{re.escape(fw)}\b(?!-)", line):
                found.append(fw)
    return found


def _extract_java_frameworks(content: str) -> list[str]:
    """Extract Java/Spring deps from pom.xml or build.gradle content."""
    found: list[str] = []
    content_lower = content.lower()
    for fw in _JAVA_FRAMEWORKS:
        if fw in content_lower and fw not in found:
            found.append(fw)
    return found


def detect_from_paths(paths: list[str]) -> DetectedContext:
    """
    Infer language and framework from file paths only.
    """
    if not paths:
        return DetectedContext(language="unknown", framework=None, confidence="low")

    path_set = {_normalize_path(p) for p in paths}
    lang_counts: Counter[str] = Counter()
    fw_candidates: list[str] = []

    for p in path_set:
        path_obj = Path(p)
        # Check path signals (exact filename match)
        for filename, (lang, fw) in _PATH_SIGNALS.items():
            if path_obj.name == filename or p.endswith("/" + filename):
                lang_counts[lang] += 1
                if fw:
                    fw_candidates.append(fw)
                break
        # Check extensions
        ext = path_obj.suffix.lower()
        if ext in _EXT_LANGUAGE:
            lang_counts[_EXT_LANGUAGE[ext]] += 1

    if not lang_counts:
        return DetectedContext(language="unknown", framework=None, confidence="low")

    primary_lang = lang_counts.most_common(1)[0][0]
    secondary = lang_counts.most_common(2)
    if len(secondary) >= 2 and secondary[0][1] == secondary[1][1]:
        confidence: Literal["high", "medium", "low"] = "medium"
    elif secondary[0][1] >= 3:
        confidence = "high"
    elif secondary[0][1] >= 1:
        confidence = "medium"
    else:
        confidence = "low"

    framework = fw_candidates[0] if fw_candidates else None
    return DetectedContext(language=primary_lang, framework=framework, confidence=confidence)


def detect_from_paths_and_content(
    paths: list[str], content_by_path: dict[str, str]
) -> DetectedContext:
    """
    Infer language and framework from paths and sampled content (e.g. deps from requirements.txt).
    Uses content to resolve framework when path signals are ambiguous.
    """
    base = detect_from_paths(paths)
    path_set = {_normalize_path(p): p for p in paths}
    fw_candidates: list[str] = []

    for norm_path, orig_path in path_set.items():
        content = content_by_path.get(norm_path) or content_by_path.get(orig_path)
        if not content:
            continue
        name = Path(norm_path).name
        if name == "requirements.txt" or name == "pyproject.toml":
            for fw in _extract_python_frameworks(content):
                if fw not in fw_candidates:
                    fw_candidates.append(fw)
        elif name in ("pom.xml", "build.gradle", "build.gradle.kts"):
            for fw in _extract_java_frameworks(content):
                if fw not in fw_candidates:
                    fw_candidates.append(fw)

    framework = fw_candidates[0] if fw_candidates else base.framework
    confidence = base.confidence
    if fw_candidates and base.confidence == "low":
        confidence = "medium"
    return DetectedContext(language=base.language, framework=framework, confidence=confidence)
