"""Language and framework detection from file paths and content."""

import re
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

# Confidence thresholds: numeric 0.0-1.0 -> literal (plan: define thresholds and write tests)
CONFIDENCE_THRESHOLD_HIGH = 0.8
CONFIDENCE_THRESHOLD_MEDIUM = 0.5

# ---------------------------------------------------------------------------
# Test file detection
# ---------------------------------------------------------------------------

# Patterns that identify test files by path/filename convention across ecosystems.
# Ordered roughly by specificity (filename patterns before directory patterns).
_TEST_FILE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Python (pytest / unittest)
    re.compile(r"(?:^|/)test_[^/]+\.py$"),
    re.compile(r"(?:^|/)[^/]+_test\.py$"),
    # JavaScript / TypeScript (Jest, Mocha, Vitest)
    re.compile(r"\.(?:spec|test)\.[jt]sx?$"),
    # Java / Kotlin (JUnit, TestNG, Spock)
    re.compile(r"(?:^|/)[^/]+(?:Test|Tests|Spec|IT)\.(?:java|kt)$"),
    # Go (standard _test.go suffix)
    re.compile(r"(?:^|/)[^/]+_test\.go$"),
    # Ruby (RSpec / Minitest)
    re.compile(r"(?:^|/)[^/]+_(?:spec|test)\.rb$"),
    # Dart (flutter / dart test)
    re.compile(r"(?:^|/)[^/]+_test\.dart$"),
    # C# (.NET — xUnit, NUnit, MSTest)
    re.compile(r"(?:^|/)[^/]+(?:Test|Tests|Spec)\.cs$"),
    re.compile(r"(?:^|/).*\.Tests?/"),          # project directory: Foo.Tests/
    # C (check / cmocka / Unity)
    re.compile(r"(?:^|/)test_[^/]+\.(?:c|h)$"),
    re.compile(r"(?:^|/)[^/]+_test\.(?:c|h)$"),
    # C++ (Google Test, Catch2, doctest)
    re.compile(r"(?:^|/)test_[^/]+\.(?:cpp|cc|cxx|hpp)$"),
    re.compile(r"(?:^|/)[^/]+_test\.(?:cpp|cc|cxx|hpp)$"),
    re.compile(r"(?:^|/)[^/]+(?:Test|Tests|Spec)\.(?:cpp|cc|cxx)$"),
    # Generic directory conventions  (__tests__/, tests?/) — case-insensitive
    # so that Tests/, __Tests__/ etc. are also matched.
    # Note: spec/ is intentionally omitted — it is ambiguous (e.g. api/spec/, docs/spec/)
    # and all real "spec" test conventions are already caught by language-specific filename
    # rules above (e.g. _spec.rb, .spec.ts, *Spec.java, *Spec.cs).
    re.compile(r"(?:^|/)__tests__/", re.IGNORECASE),
    re.compile(r"(?:^|/)tests?/", re.IGNORECASE),
)


def is_test_file(path: str) -> bool:
    """Return True when *path* follows a recognised test-file naming convention.

    Detection is purely path-based (no file I/O).  Covers Python, JavaScript /
    TypeScript, Java, Kotlin, Go, Ruby, Dart, C#, C, and C++.

    Examples::

        >>> is_test_file("tests/agent/test_agent_debug_mode.py")
        True
        >>> is_test_file("src/code_review/agent/agent.py")
        False
        >>> is_test_file("com/example/FooTest.java")
        True
        >>> is_test_file("pkg/storage/storage_test.go")
        True
        >>> is_test_file("src/components/Button.spec.ts")
        True
        >>> is_test_file("Foo.Tests/FooTests.cs")
        True
    """
    normalised = path.replace("\\", "/")
    return any(p.search(normalised) is not None for p in _TEST_FILE_PATTERNS)


def _confidence_from_score(score: float) -> Literal["high", "medium", "low"]:
    """Map numeric confidence 0.0-1.0 to literal using thresholds."""
    if score >= CONFIDENCE_THRESHOLD_HIGH:
        return "high"
    if score >= CONFIDENCE_THRESHOLD_MEDIUM:
        return "medium"
    return "low"


class DetectedContext(BaseModel):
    """Result of language/framework detection."""

    language: str = Field(..., description="Primary language (e.g. python, javascript)")
    framework: str | None = Field(
        default=None, description="Detected framework (e.g. django, fastapi)"
    )
    confidence: Literal["high", "medium", "low"] = Field(
        ..., description="Confidence based on signal strength"
    )
    confidence_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Numeric confidence 0.0-1.0 for thresholds",
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
    ".dart": "dart",
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
    "meson.build": ("cpp", None),
    "pubspec.yaml": ("dart", None),
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
        # requirements.txt: package==version or package>=version.
        pkg = line.split("==")[0].split(">=")[0].split("[")[0].strip()
        if pkg in _PYTHON_FRAMEWORKS:
            found.append(pkg)
        # pyproject.toml: "django" or django = "^4.0".
        # Match as standalone token, not e.g. my-django-app.
        for fw in _PYTHON_FRAMEWORKS:
            if fw not in found and re.search(rf"(?<!-)\b{re.escape(fw)}\b(?!-)", line):
                found.append(fw)
    return found


def _extract_java_frameworks(content: str) -> list[str]:
    """Extract Java/Spring deps from pom.xml or build.gradle content."""
    found: list[str] = []
    for fw in _JAVA_FRAMEWORKS:
        if fw not in found and re.search(
            rf"(?<!-)\b{re.escape(fw)}(?:-|\b)", content, re.IGNORECASE
        ):
            found.append(fw)
    return found


def detect_from_paths(paths: list[str]) -> DetectedContext:
    """
    Infer language and framework from file paths only.
    Returns confidence_score in 0.0-1.0; confidence literal from thresholds.
    """
    if not paths:
        return DetectedContext(
            language="unknown", framework=None, confidence="low", confidence_score=0.0
        )

    path_set = {_normalize_path(p) for p in paths}
    lang_counts, fw_candidates = _language_and_framework_counts(path_set)

    if not lang_counts:
        return DetectedContext(
            language="unknown", framework=None, confidence="low", confidence_score=0.0
        )

    primary_lang, score = _primary_language_and_score(lang_counts)
    confidence = _confidence_from_score(score)
    framework = fw_candidates[0] if fw_candidates else None
    return DetectedContext(
        language=primary_lang, framework=framework, confidence=confidence, confidence_score=score
    )


def _language_and_framework_counts(
    path_set: set[str],
) -> tuple[Counter[str], list[str]]:
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
        lang = _EXT_LANGUAGE.get(ext)
        if lang:
            lang_counts[lang] += 1

    return lang_counts, fw_candidates


def _primary_language_and_score(lang_counts: Counter[str]) -> tuple[str, float]:
    primary_lang = lang_counts.most_common(1)[0][0]
    primary_count = lang_counts[primary_lang]
    secondary = lang_counts.most_common(2)
    # Numeric score: tie -> 0.5, 3+ files -> 0.9, 1-2 -> 0.6
    if len(secondary) >= 2 and secondary[0][1] == secondary[1][1]:
        score = 0.5
    elif primary_count >= 3:
        score = 0.9
    elif primary_count >= 1:
        score = 0.6
    else:
        score = 0.0
    return primary_lang, score


def detect_from_paths_and_content(
    paths: list[str], content_by_path: dict[str, str]
) -> DetectedContext:
    """
    Infer language and framework from paths and sampled content (e.g. deps from requirements.txt).
    Uses content to resolve framework when path signals are ambiguous.
    """
    base = detect_from_paths(paths)
    path_set = {_normalize_path(p): p for p in paths}
    fw_candidates = _framework_candidates_from_content(path_set, content_by_path)

    framework = fw_candidates[0] if fw_candidates else base.framework
    score = base.confidence_score
    if fw_candidates and base.confidence == "low":
        score = max(score, CONFIDENCE_THRESHOLD_MEDIUM)
    confidence = _confidence_from_score(score)
    return DetectedContext(
        language=base.language, framework=framework, confidence=confidence, confidence_score=score
    )


def _framework_candidates_from_content(
    path_set: dict[str, str], content_by_path: dict[str, str]
) -> list[str]:
    fw_candidates: list[str] = []

    for norm_path, orig_path in path_set.items():
        content = content_by_path.get(norm_path) or content_by_path.get(orig_path)
        if not content:
            continue
        name = Path(norm_path).name
        if name in ("requirements.txt", "pyproject.toml"):
            frameworks = _extract_python_frameworks(content)
        elif name in ("pom.xml", "build.gradle", "build.gradle.kts"):
            frameworks = _extract_java_frameworks(content)
        else:
            frameworks = []

        for fw in frameworks:
            if fw not in fw_candidates:
                fw_candidates.append(fw)

    return fw_candidates


def _folder_roots_from_paths(paths: list[str]) -> set[str]:
    """
    Return folder roots: dirname of any path whose basename is a config file
    (package.json, go.mod, etc.).
    """
    roots: set[str] = set()
    for p in paths:
        norm = _normalize_path(p)
        name = Path(norm).name
        if name in _PATH_SIGNALS:
            root = str(Path(norm).parent)
            if root == ".":
                root = ""
            roots.add(root)
    return roots


def detect_from_paths_per_folder_root(paths: list[str]) -> dict[str, DetectedContext]:
    """
    Monorepo mode: run detection per folder root (nearest package.json, go.mod, pom.xml, etc.).
    Paths are grouped by the longest matching folder root. The key "" represents only orphan
    paths (files not under any detected config root) and is omitted from the returned dict
    when there are no orphans. Returns dict mapping folder_root -> DetectedContext.
    """
    if not paths:
        return {}
    path_list = [_normalize_path(p) for p in paths]
    roots = _folder_roots_from_paths(path_list)
    roots.add("")  # repo root always exists as fallback
    # Sort roots by length descending so we match longest prefix first
    sorted_roots = sorted(roots, key=lambda r: len(r), reverse=True)

    groups: dict[str, list[str]] = {r: [] for r in sorted_roots}
    for p in path_list:
        assigned = False
        for root in sorted_roots:
            if root == "":
                continue
            if p == root or p.startswith(root + "/"):
                groups[root].append(p)
                assigned = True
                break
        if not assigned:
            groups[""].append(p)

    return {root: detect_from_paths(group) for root, group in groups.items() if group}
