#!/usr/bin/env python3
"""Sync or bootstrap inline Jenkins Pipeline jobs from docker/jenkins/Jenkinsfile.

By default this script updates existing inline Pipeline jobs' ``config.xml`` so the
embedded Jenkinsfile stays in sync with ``docker/jenkins/Jenkinsfile``. When syncing
the documented default Bitbucket jobs, it also enforces the matching Generic Webhook
Trigger mappings and parameter defaults.

It can also bootstrap the default Bitbucket two-job setup used by the docs:

- ``job/bitbucket/job/bitbucket`` for PR lifecycle reviews
- ``job/bitbucket/job/comments`` for comment add/edit/delete review-decision runs

Bootstrap mode is API-driven: it creates missing folders/jobs through Jenkins
HTTP endpoints, seeds the required Jenkins secret-text credentials, and then
patches job ``config.xml`` for the inline Jenkinsfile and Generic Webhook
Trigger configuration. No Playwright or UI automation is required.

Script Security approvals are also automated through Jenkins admin endpoints.

Usage:
  # Repo-root .env is auto-loaded for local use when present.
  export JENKINS_URL=http://127.0.0.1:8080 JENKINS_USERNAME=admin JENKINS_PASSWORD=admin
  python scripts/jenkins_sync_inline_pipeline.py
  python scripts/jenkins_sync_inline_pipeline.py job/myfolder/job/myjob
  python scripts/jenkins_sync_inline_pipeline.py --ensure-default-bitbucket-setup
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from local_env import load_local_env

REPO_ROOT = Path(__file__).resolve().parents[1]
JENKINSFILE = REPO_ROOT / "docker" / "jenkins" / "Jenkinsfile"

DEFAULT_FOLDER_NAME = "bitbucket"
DEFAULT_REVIEW_JOB_NAME = "bitbucket"
DEFAULT_COMMENTS_JOB_NAME = "comments"

PIPELINE_JOB_MODE = "org.jenkinsci.plugins.workflow.job.WorkflowJob"
FOLDER_MODE = "com.cloudbees.hudson.plugins.folder.Folder"
DEFAULT_BITBUCKET_SCM_PROVIDER = "bitbucket_server"
DEFAULT_BITBUCKET_SCM_URL = "http://localhost:7990/rest/api/1.0"

FORM_URLENCODED = "application/x-www-form-urlencoded"

MODERN_OVERRIDE_PARAM_BLOCK = """        <hudson.model.StringParameterDefinition>
          <name>SCM_PROVIDER_OVERRIDE</name>
          <description>Optional: overrides env SCM_PROVIDER (set provider via folder/global env)</description>
          <trim>false</trim>
        </hudson.model.StringParameterDefinition>
        <hudson.model.StringParameterDefinition>
          <name>SCM_URL_OVERRIDE</name>
          <description>Optional: overrides env SCM_URL</description>
          <trim>false</trim>
        </hudson.model.StringParameterDefinition>
        <hudson.model.StringParameterDefinition>
          <name>LLM_PROVIDER_OVERRIDE</name>
          <description>Optional: overrides env LLM_PROVIDER (folder/global env)</description>
          <trim>false</trim>
        </hudson.model.StringParameterDefinition>"""

LEGACY_PARAMS_RE = re.compile(
    r"<hudson\.model\.StringParameterDefinition>\s*"
    r"<name>SCM_PROVIDER</name>.*?</hudson\.model\.StringParameterDefinition>\s*"
    r"<hudson\.model\.StringParameterDefinition>\s*"
    r"<name>SCM_URL</name>.*?</hudson\.model\.StringParameterDefinition>\s*"
    r"<hudson\.model\.StringParameterDefinition>\s*"
    r"<name>LLM_PROVIDER</name>.*?</hudson\.model\.StringParameterDefinition>\s*"
    r"<hudson\.model\.StringParameterDefinition>\s*"
    r"<name>LLM_MODEL</name>.*?</hudson\.model\.StringParameterDefinition>",
    re.DOTALL,
)

PIPELINE_TRIGGER_PROPERTY_RE = re.compile(
    r"\s*<org\.jenkinsci\.plugins\.workflow\.job\.properties\.PipelineTriggersJobProperty>.*?"
    r"</org\.jenkinsci\.plugins\.workflow\.job\.properties\.PipelineTriggersJobProperty>",
    re.DOTALL,
)
PROPERTIES_RE = re.compile(r"<properties>(?P<inner>.*?)</properties>", re.DOTALL)
FOLDER_PROPERTIES_BLOCK_RE = re.compile(
    r"\s*<com\.mig82\.folders\.properties\.FolderProperties>.*?"
    r"</com\.mig82\.folders\.properties\.FolderProperties>",
    re.DOTALL,
)


@dataclass(frozen=True)
class WebhookTriggerSpec:
    post_content_params: dict[str, str]
    filter_text: str
    filter_regex: str
    token: str = ""


@dataclass
class JenkinsSession:
    """Cookie-backed Jenkins HTTP session with crumb support."""

    base_url: str
    user: str
    password: str
    opener: urllib.request.OpenerDirector
    crumb_headers: dict[str, str]


@dataclass(frozen=True)
class PendingScriptApproval:
    """A pending Jenkins Script Security approval entry."""

    hash: str
    script: str


@dataclass(frozen=True)
class SecretTextCredential:
    """A Jenkins Secret text credential to seed during bootstrap."""

    credential_id: str
    secret_text: str
    description: str


PARAM_DEFINITIONS: tuple[dict[str, str], ...] = (
    {"name": "SCM_OWNER", "description": "Repo owner / project key", "default": ""},
    {"name": "SCM_REPO", "description": "Repo name / slug", "default": ""},
    {"name": "SCM_PR_NUM", "description": "PR number / id", "default": ""},
    {"name": "SCM_HEAD_SHA", "description": "Head commit SHA", "default": ""},
    {
        "name": "SCM_BASE_SHA",
        "description": "Optional review base SHA for incremental PR updates",
        "default": "",
    },
    {"name": "PR_ACTION", "description": "Webhook action or eventKey", "default": ""},
    {
        "name": "SCM_PROVIDER_OVERRIDE",
        "description": (
            "Optional: overrides env SCM_PROVIDER (set provider via folder/global env; "
            "do not use a param named SCM_PROVIDER - it shadows env)"
        ),
        "default": "",
    },
    {"name": "SCM_URL_OVERRIDE", "description": "Optional: overrides env SCM_URL", "default": ""},
    {
        "name": "LLM_PROVIDER_OVERRIDE",
        "description": (
            "Optional: overrides env LLM_PROVIDER (folder/global env; "
            "LLM_API_KEY credential unchanged)"
        ),
        "default": "",
    },
    {"name": "COMPOSE_PROJECT_NAME", "description": "Docker Compose project name", "default": "code-review"},
    {
        "name": "IMAGE_NAME",
        "description": (
            "Container image name (e.g. user/name:tag). Defaults to the local "
            "code-review-agent tag used throughout the docs; set a pinned remote image explicitly if you prefer."
        ),
        "default": "code-review-agent",
    },
)

BITBUCKET_PR_PARAMS = {
    "SCM_OWNER": "$.pullRequest.toRef.repository.project.key",
    "SCM_REPO": "$.pullRequest.toRef.repository.slug",
    "SCM_PR_NUM": "$.pullRequest.id",
    "SCM_HEAD_SHA": "$.pullRequest.fromRef.latestCommit",
    "SCM_BASE_SHA": "$.previousFromHash",
    "PR_ACTION": "$.eventKey",
}

BITBUCKET_COMMENT_CONTEXT_PARAMS = {
    "CODE_REVIEW_EVENT_COMMENT_ID": "$.comment.id",
    "CODE_REVIEW_EVENT_ACTOR_LOGIN": "$.actor.slug",
    "CODE_REVIEW_EVENT_ACTOR_ID": "$.actor.id",
}


def jenkins_escape_xml(text: str) -> str:
    """Escape text for Jenkins config.xml."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def build_default_job_paths(
    folder_name: str = DEFAULT_FOLDER_NAME,
    review_job_name: str = DEFAULT_REVIEW_JOB_NAME,
    comments_job_name: str = DEFAULT_COMMENTS_JOB_NAME,
) -> list[str]:
    """Return the default two-job Bitbucket setup paths."""
    return [
        f"job/{folder_name}/job/{review_job_name}",
        f"job/{folder_name}/job/{comments_job_name}",
    ]


def default_full_review_webhook_spec() -> WebhookTriggerSpec:
    """Bitbucket Server/DC PR lifecycle webhook mapping."""
    return WebhookTriggerSpec(
        post_content_params=dict(BITBUCKET_PR_PARAMS),
        filter_text="$PR_ACTION",
        filter_regex="^pr:(opened|modified|from_ref_updated)$",
    )


def default_comments_webhook_spec() -> WebhookTriggerSpec:
    """Bitbucket Server/DC comment-event webhook mapping."""
    params = dict(BITBUCKET_PR_PARAMS)
    params.update(BITBUCKET_COMMENT_CONTEXT_PARAMS)
    return WebhookTriggerSpec(
        post_content_params=params,
        filter_text="$PR_ACTION",
        filter_regex="^pr:comment:(added|edited|deleted)$",
    )


def default_trigger_specs_by_job_path(
    folder_name: str = DEFAULT_FOLDER_NAME,
    review_job_name: str = DEFAULT_REVIEW_JOB_NAME,
    comments_job_name: str = DEFAULT_COMMENTS_JOB_NAME,
) -> dict[str, WebhookTriggerSpec]:
    review_path, comments_path = build_default_job_paths(folder_name, review_job_name, comments_job_name)
    return {
        review_path: default_full_review_webhook_spec(),
        comments_path: default_comments_webhook_spec(),
    }


def default_parameter_defaults(
    *,
    scm_provider: str,
    scm_url: str,
) -> dict[str, str]:
    """Return default Jenkins parameter values for generated Bitbucket jobs."""
    return {
        name: value
        for name, value in {
            "SCM_PROVIDER_OVERRIDE": scm_provider.strip(),
            "SCM_URL_OVERRIDE": scm_url.strip(),
        }.items()
        if value
    }


def default_bootstrap_secret_text_credentials() -> list[SecretTextCredential]:
    """Return the Jenkins secret-text credentials required by the bundled Jenkinsfile."""
    scm_token = os.environ.get("SCM_TOKEN", "").strip()
    if not scm_token:
        raise RuntimeError(
            "SCM_TOKEN is required to bootstrap Jenkins credentials. Export SCM_TOKEN and rerun "
            "--ensure-default-bitbucket-setup."
        )

    llm_provider = (os.environ.get("LLM_PROVIDER", "").strip() or "gemini").lower()
    llm_api_key = os.environ.get("LLM_API_KEY", "").strip()

    credentials = [
        SecretTextCredential(
            credential_id="SCM_TOKEN",
            secret_text=scm_token,
            description="SCM API token",
        )
    ]
    if llm_provider != "ollama":
        if not llm_api_key:
            raise RuntimeError(
                "LLM_API_KEY is required to bootstrap Jenkins credentials unless LLM_PROVIDER=ollama. "
                "Export LLM_API_KEY and rerun --ensure-default-bitbucket-setup."
            )
        credentials.append(
            SecretTextCredential(
                credential_id="LLM_API_KEY",
                secret_text=llm_api_key,
                description="LLM API key (used with LLM_PROVIDER)",
            )
        )
    elif llm_api_key:
        credentials.append(
            SecretTextCredential(
                credential_id="LLM_API_KEY",
                secret_text=llm_api_key,
                description="LLM API key (used with LLM_PROVIDER)",
            )
        )
    return credentials


def build_parameter_definitions_xml(defaults: dict[str, str] | None = None) -> str:
    """Return the Jenkins ParametersDefinitionProperty block for inline jobs."""
    defaults = defaults or {}
    parts = [
        "    <hudson.model.ParametersDefinitionProperty>",
        "      <parameterDefinitions>",
    ]
    for param in PARAM_DEFINITIONS:
        default_value = defaults.get(param["name"], param["default"])
        parts.extend(
            [
                "        <hudson.model.StringParameterDefinition>",
                f"          <name>{jenkins_escape_xml(param['name'])}</name>",
                f"          <description>{jenkins_escape_xml(param['description'])}</description>",
                f"          <defaultValue>{jenkins_escape_xml(default_value)}</defaultValue>",
                "          <trim>false</trim>",
                "        </hudson.model.StringParameterDefinition>",
            ]
        )
    parts.extend(
        [
            "      </parameterDefinitions>",
            "    </hudson.model.ParametersDefinitionProperty>",
        ]
    )
    return "\n".join(parts)


def build_folder_properties_xml(properties: dict[str, str], indent: str = "  ") -> str:
    """Return Folder Properties plugin XML for a Jenkins folder."""
    parts = [
        f"{indent}<com.mig82.folders.properties.FolderProperties>",
        f"{indent}  <properties>",
    ]
    for key, value in properties.items():
        parts.extend(
            [
                f"{indent}    <com.mig82.folders.properties.StringProperty>",
                f"{indent}      <key>{jenkins_escape_xml(key)}</key>",
                f"{indent}      <value>{jenkins_escape_xml(value)}</value>",
                f"{indent}    </com.mig82.folders.properties.StringProperty>",
            ]
        )
    parts.extend(
        [
            f"{indent}  </properties>",
            f"{indent}</com.mig82.folders.properties.FolderProperties>",
        ]
    )
    return "\n".join(parts)


def build_folder_config_xml(folder_properties: dict[str, str]) -> str:
    """Return a minimal Jenkins folder config.xml with Folder Properties."""
    return "\n".join(
        [
            '<com.cloudbees.hudson.plugins.folder.Folder plugin="cloudbees-folder">',
            "  <actions/>",
            "  <description></description>",
            build_folder_properties_xml(folder_properties),
            "  <folderViews class=\"com.cloudbees.hudson.plugins.folder.views.DefaultFolderViewHolder\">",
            "    <views>",
            "      <hudson.model.AllView>",
            "        <owner class=\"com.cloudbees.hudson.plugins.folder.Folder\" reference=\"../../..\"/>",
            "        <name>All</name>",
            "        <filterExecutors>false</filterExecutors>",
            "        <filterQueue>false</filterQueue>",
            "        <properties class=\"hudson.model.View$PropertyList\"/>",
            "      </hudson.model.AllView>",
            "    </views>",
            "    <tabBar class=\"hudson.views.DefaultViewsTabBar\"/>",
            "  </folderViews>",
            "  <healthMetrics/>",
            "  <icon class=\"com.cloudbees.hudson.plugins.folder.icons.StockFolderIcon\"/>",
            "  <orphanedItemStrategy class=\"com.cloudbees.hudson.plugins.folder.computed.DefaultOrphanedItemStrategy\">",
            "    <pruneDeadBranches>true</pruneDeadBranches>",
            "    <daysToKeep>-1</daysToKeep>",
            "    <numToKeep>-1</numToKeep>",
            "  </orphanedItemStrategy>",
            "</com.cloudbees.hudson.plugins.folder.Folder>",
        ]
    )


def build_trigger_property_xml(spec: WebhookTriggerSpec, indent: str = "    ") -> str:
    """Return the PipelineTriggersJobProperty XML for Generic Webhook Trigger."""
    parts = [
        f"{indent}<org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty>",
        f"{indent}  <triggers>",
        f"{indent}    <org.jenkinsci.plugins.gwt.GenericTrigger>",
        f"{indent}      <spec></spec>",
        f"{indent}      <genericVariables>",
    ]
    for key, value in spec.post_content_params.items():
        parts.extend(
            [
                f"{indent}        <org.jenkinsci.plugins.gwt.GenericVariable>",
                f"{indent}          <key>{jenkins_escape_xml(key)}</key>",
                f"{indent}          <value>{jenkins_escape_xml(value)}</value>",
                f"{indent}          <expressionType>JSONPath</expressionType>",
                f"{indent}          <regexpFilter></regexpFilter>",
                f"{indent}          <defaultValue></defaultValue>",
                f"{indent}        </org.jenkinsci.plugins.gwt.GenericVariable>",
            ]
        )
    parts.extend(
        [
            f"{indent}      </genericVariables>",
            f"{indent}      <genericRequestVariables/>",
            f"{indent}      <genericHeaderVariables/>",
            f"{indent}      <token>{jenkins_escape_xml(spec.token)}</token>",
            f"{indent}      <tokenCredentialId></tokenCredentialId>",
            f"{indent}      <causeString></causeString>",
            f"{indent}      <printContributedVariables>false</printContributedVariables>",
            f"{indent}      <printPostContent>false</printPostContent>",
            f"{indent}      <silentResponse>false</silentResponse>",
            f"{indent}      <regexpFilterText>{jenkins_escape_xml(spec.filter_text)}</regexpFilterText>",
            f"{indent}      <regexpFilterExpression>{jenkins_escape_xml(spec.filter_regex)}</regexpFilterExpression>",
            f"{indent}      <shouldNotFlatten>false</shouldNotFlatten>",
            f"{indent}      <allowSeveralTriggersPerBuild>false</allowSeveralTriggersPerBuild>",
            f"{indent}      <overrideQuietPeriod>false</overrideQuietPeriod>",
            f"{indent}    </org.jenkinsci.plugins.gwt.GenericTrigger>",
            f"{indent}  </triggers>",
            f"{indent}</org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty>",
        ]
    )
    return "\n".join(parts)


def build_inline_pipeline_job_config(
    script_body: str,
    trigger_spec: WebhookTriggerSpec,
    *,
    parameter_defaults: dict[str, str] | None = None,
) -> str:
    """Return a complete inline Pipeline config.xml for a WorkflowJob."""
    return "\n".join(
        [
            "<flow-definition>",
            "  <actions/>",
            "  <description></description>",
            "  <keepDependencies>false</keepDependencies>",
            "  <properties>",
            build_parameter_definitions_xml(parameter_defaults),
            build_trigger_property_xml(trigger_spec),
            "  </properties>",
            '  <definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition">',
            f"    <script>{jenkins_escape_xml(script_body)}</script>",
            "    <sandbox>false</sandbox>",
            "  </definition>",
            "  <triggers/>",
            "  <disabled>false</disabled>",
            "</flow-definition>",
        ]
    )


def patch_tracker_parameters(xml: str) -> str:
    """Update or remove stale parameter-tracker references in config.xml."""
    for old, new in [
        ("SCM_PROVIDER", "SCM_PROVIDER_OVERRIDE"),
        ("SCM_URL", "SCM_URL_OVERRIDE"),
        ("LLM_PROVIDER", "LLM_PROVIDER_OVERRIDE"),
    ]:
        xml = xml.replace(f"<string>{old}</string>", f"<string>{new}</string>")
    xml = xml.replace("<string>LLM_MODEL</string>", "")
    xml = xml.replace("<string>LLM_MODEL_OVERRIDE</string>", "")
    return xml


def set_string_parameter_default(xml: str, name: str, value: str) -> str:
    """Set a specific Jenkins string parameter default value inside config.xml."""
    pattern = re.compile(
        r"(<hudson\.model\.StringParameterDefinition>\s*"
        rf"<name>{re.escape(name)}</name>.*?<defaultValue>)"
        r"(.*?)"
        r"(</defaultValue>)",
        re.DOTALL,
    )
    escaped_value = jenkins_escape_xml(value)
    new_xml, replaced = pattern.subn(rf"\1{escaped_value}\3", xml, count=1)
    if replaced != 1:
        raise ValueError(f"Could not find parameter {name} in config.xml")
    return new_xml


def upsert_trigger_property(xml: str, trigger_spec: WebhookTriggerSpec) -> str:
    """Insert or replace the Generic Webhook Trigger property in config.xml."""
    trigger_block = "\n" + build_trigger_property_xml(trigger_spec) + "\n"
    if PIPELINE_TRIGGER_PROPERTY_RE.search(xml):
        return PIPELINE_TRIGGER_PROPERTY_RE.sub(trigger_block, xml, count=1)

    match = PROPERTIES_RE.search(xml)
    if not match:
        raise ValueError("Expected a <properties>...</properties> block in WorkflowJob config.xml")

    inner = match.group("inner")
    replacement_inner = inner
    if replacement_inner.strip():
        if not replacement_inner.endswith("\n"):
            replacement_inner += "\n"
        replacement_inner += build_trigger_property_xml(trigger_spec) + "\n"
    else:
        replacement_inner = "\n" + build_trigger_property_xml(trigger_spec) + "\n"

    return xml[: match.start("inner")] + replacement_inner + xml[match.end("inner") :]


def patch_folder_config(xml: str, folder_properties: dict[str, str]) -> str:
    """Insert or replace the Folder Properties plugin block in a folder config.xml."""
    properties_block = "\n" + build_folder_properties_xml(folder_properties) + "\n"
    if FOLDER_PROPERTIES_BLOCK_RE.search(xml):
        return FOLDER_PROPERTIES_BLOCK_RE.sub(properties_block, xml, count=1)

    match = PROPERTIES_RE.search(xml)
    if not match:
        raise ValueError("Expected a <properties>...</properties> block in Folder config.xml")

    inner = match.group("inner")
    replacement_inner = inner
    if replacement_inner.strip():
        if not replacement_inner.endswith("\n"):
            replacement_inner += "\n"
        replacement_inner += build_folder_properties_xml(folder_properties) + "\n"
    else:
        replacement_inner = "\n" + build_folder_properties_xml(folder_properties) + "\n"

    return xml[: match.start("inner")] + replacement_inner + xml[match.end("inner") :]


def patch_config(
    xml: str,
    script_body: str,
    trigger_spec: WebhookTriggerSpec | None = None,
    parameter_defaults: dict[str, str] | None = None,
) -> str:
    """Replace the inline Pipeline script and optionally enforce trigger config."""
    escaped = jenkins_escape_xml(script_body)
    xml, replaced = re.subn(
        r"<script>.*?</script>",
        f"<script>{escaped}</script>",
        xml,
        count=1,
        flags=re.DOTALL,
    )
    if replaced != 1:
        raise ValueError("Expected exactly one <script>...</script> block")

    if LEGACY_PARAMS_RE.search(xml):
        xml = LEGACY_PARAMS_RE.sub(MODERN_OVERRIDE_PARAM_BLOCK + "\n", xml, count=1)

    xml = patch_tracker_parameters(xml)
    for name, value in (parameter_defaults or {}).items():
        xml = set_string_parameter_default(xml, name, value)
    if trigger_spec is not None:
        xml = upsert_trigger_property(xml, trigger_spec)
    return xml


def auth_header(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


def create_jenkins_session(base: str, user: str, password: str) -> JenkinsSession:
    """Create an authenticated Jenkins session with cookies and crumb headers."""
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    auth = auth_header(user, password)

    root_req = urllib.request.Request(base)
    root_req.add_header("Authorization", auth)
    with opener.open(root_req, timeout=30) as response:
        response.read()

    crumb_headers: dict[str, str] = {}
    req = urllib.request.Request(f"{base}/crumbIssuer/api/json")
    req.add_header("Authorization", auth)
    try:
        with opener.open(req, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return JenkinsSession(base, user, password, opener, crumb_headers)
        raise
    crumb_headers[payload["crumbRequestField"]] = payload["crumb"]
    return JenkinsSession(base, user, password, opener, crumb_headers)


def http_get_text(session: JenkinsSession, url: str, *, timeout: int = 60) -> str:
    req = urllib.request.Request(url)
    req.add_header("Authorization", auth_header(session.user, session.password))
    with session.opener.open(req, timeout=timeout) as response:
        return response.read().decode("utf-8")


def http_get_json(session: JenkinsSession, url: str, *, timeout: int = 60) -> dict:
    """GET JSON from Jenkins with the authenticated session."""
    return json.loads(http_get_text(session, url, timeout=timeout))


def http_post(
    session: JenkinsSession,
    url: str,
    data: bytes,
    *,
    content_type: str,
    timeout: int = 120,
) -> str:
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", auth_header(session.user, session.password))
    req.add_header("Content-Type", content_type)
    for key, value in session.crumb_headers.items():
        req.add_header(key, value)
    with session.opener.open(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def http_post_form(
    session: JenkinsSession,
    url: str,
    data: dict[str, str],
    *,
    timeout: int = 120,
) -> str:
    """POST form-encoded data to Jenkins."""
    return http_post(
        session,
        url,
        urllib.parse.urlencode(data).encode(),
        content_type=FORM_URLENCODED,
        timeout=timeout,
    )


def jenkins_item_exists(session: JenkinsSession, item_path: str) -> bool:
    """Return True when a Jenkins folder/job path exists."""
    item_path = item_path.strip("/")
    url = f"{session.base_url}/{item_path}/api/json"
    try:
        http_get_text(session, url, timeout=30)
    except HTTPError as exc:
        if exc.code == 404:
            return False
        raise
    return True


def create_folder(session: JenkinsSession, folder_name: str, parent_path: str | None = None) -> None:
    """Create a Jenkins folder using the createItem HTTP endpoint."""
    parent_url = f"{session.base_url}/{parent_path.strip('/')}" if parent_path else session.base_url
    payload = urllib.parse.urlencode(
        {
            "name": folder_name,
            "mode": FOLDER_MODE,
            "from": "",
            "Submit": "OK",
        }
    ).encode()
    http_post(
        session,
        f"{parent_url}/createItem",
        payload,
        content_type=FORM_URLENCODED,
    )


def create_workflow_job(session: JenkinsSession, parent_path: str, job_name: str) -> None:
    """Create an empty WorkflowJob so config.xml can be patched immediately after."""
    parent_url = f"{session.base_url}/{parent_path.strip('/')}" if parent_path else session.base_url
    payload = urllib.parse.urlencode(
        {
            "name": job_name,
            "mode": PIPELINE_JOB_MODE,
            "from": "",
            "Submit": "OK",
        }
    ).encode()
    http_post(
        session,
        f"{parent_url}/createItem",
        payload,
        content_type=FORM_URLENCODED,
    )


def post_config_xml(session: JenkinsSession, url: str, body: str) -> None:
    """POST config.xml using Jenkins HTTP API."""
    http_post(
        session,
        url,
        body.encode("utf-8"),
        content_type="application/xml; charset=UTF-8",
    )


def sync_folder_config(
    session: JenkinsSession,
    folder_path: str,
    *,
    folder_properties: dict[str, str],
) -> None:
    """Ensure the Jenkins folder config contains the expected Folder Properties block."""
    config_url = f"{session.base_url}/{folder_path.strip('/')}/config.xml"
    try:
        xml = http_get_text(session, config_url)
        new_xml = patch_folder_config(xml, folder_properties)
    except HTTPError as exc:
        if exc.code != 404:
            raise
        new_xml = build_folder_config_xml(folder_properties)
    post_config_xml(session, config_url, new_xml)


def get_pending_script_approvals(session: JenkinsSession) -> list[PendingScriptApproval]:
    """Return pending script approvals from Jenkins Script Security."""
    url = (
        f"{session.base_url}/scriptApproval/api/json"
        "?tree=pendingScripts[hash,script]"
    )
    try:
        payload = http_get_json(session, url, timeout=30)
    except HTTPError as exc:
        if exc.code == 404:
            return []
        raise

    pending = payload.get("pendingScripts", [])
    out: list[PendingScriptApproval] = []
    if not isinstance(pending, list):
        return out
    for item in pending:
        if not isinstance(item, dict):
            continue
        script_hash = str(item.get("hash", "")).strip()
        script = str(item.get("script", ""))
        if script_hash and script:
            out.append(PendingScriptApproval(hash=script_hash, script=script))
    return out


def approve_script_hash(session: JenkinsSession, script_hash: str) -> None:
    """Approve a pending script hash via Jenkins Script Security."""
    http_post_form(
        session,
        f"{session.base_url}/scriptApproval/approveScriptHash",
        {"hash": script_hash},
        timeout=30,
    )


def preapprove_script_via_console(session: JenkinsSession, script_body: str) -> None:
    """Preapprove the exact Jenkinsfile text through Jenkins's admin Groovy endpoint."""
    encoded = base64.b64encode(script_body.encode("utf-8")).decode("ascii")
    groovy = "\n".join(
        [
            "import java.util.Base64",
            "import org.jenkinsci.plugins.scriptsecurity.scripts.ScriptApproval",
            "import org.jenkinsci.plugins.scriptsecurity.scripts.languages.GroovyLanguage",
            f'def script = new String(Base64.decoder.decode("{encoded}"), "UTF-8")',
            "def sa = ScriptApproval.get()",
            "sa.preapprove(script, GroovyLanguage.get())",
            "sa.save()",
            'println("APPROVED")',
        ]
    )
    http_post_form(
        session,
        f"{session.base_url}/scriptText",
        {"script": groovy},
        timeout=60,
    )


def build_secret_text_credentials_groovy(credentials: list[SecretTextCredential]) -> str:
    """Return a Groovy script that idempotently seeds Jenkins Secret text credentials."""
    entries = ",\n".join(
        [
            "    ["
            f'id: "{base64.b64encode(c.credential_id.encode("utf-8")).decode("ascii")}", '
            f'secretText: "{base64.b64encode(c.secret_text.encode("utf-8")).decode("ascii")}", '
            f'description: "{base64.b64encode(c.description.encode("utf-8")).decode("ascii")}"'
            "]"
            for c in credentials
        ]
    )
    return "\n".join(
        [
            "import java.util.Base64",
            "import com.cloudbees.plugins.credentials.CredentialsScope",
            "import com.cloudbees.plugins.credentials.SystemCredentialsProvider",
            "import com.cloudbees.plugins.credentials.domains.Domain",
            "import org.jenkinsci.plugins.plaincredentials.impl.StringCredentialsImpl",
            "import hudson.util.Secret",
            "",
            'def decode = { String value -> new String(Base64.decoder.decode(value), "UTF-8") }',
            "def creds = SystemCredentialsProvider.getInstance().getStore()",
            "def domain = Domain.global()",
            "def ensureSecretText = { String id, String secretText, String description ->",
            "    def existing = creds.getCredentials(domain).find { it.id == id }",
            "    if (existing != null) {",
            '        println("EXISTS:" + id)',
            "        return",
            "    }",
            "    def c = new StringCredentialsImpl(",
            "        CredentialsScope.GLOBAL,",
            "        id,",
            "        description,",
            "        Secret.fromString(secretText)",
            "    )",
            "    creds.addCredentials(domain, c)",
            '    println("CREATED:" + id)',
            "}",
            "def entries = [",
            entries,
            "]",
            "entries.each { entry ->",
            "    ensureSecretText(",
            "        decode(entry.id),",
            "        decode(entry.secretText),",
            "        decode(entry.description)",
            "    )",
            "}",
        ]
    )


def ensure_secret_text_credentials(
    session: JenkinsSession,
    credentials: list[SecretTextCredential],
) -> None:
    """Seed Jenkins Secret text credentials through the admin Groovy endpoint."""
    if not credentials:
        return
    http_post_form(
        session,
        f"{session.base_url}/scriptText",
        {"script": build_secret_text_credentials_groovy(credentials)},
        timeout=60,
    )


def approve_matching_script(session: JenkinsSession, script_body: str) -> None:
    """Approve the exact inline Jenkinsfile, using pending-list lookup or Groovy fallback."""
    normalized_target = script_body.replace("\r\n", "\n").strip()
    for pending in get_pending_script_approvals(session):
        if pending.script.replace("\r\n", "\n").strip() == normalized_target:
            approve_script_hash(session, pending.hash)
            return
    preapprove_script_via_console(session, script_body)


def ensure_default_bitbucket_setup(
    base: str,
    user: str,
    password: str,
    script_body: str,
    folder_name: str = DEFAULT_FOLDER_NAME,
    review_job_name: str = DEFAULT_REVIEW_JOB_NAME,
    comments_job_name: str = DEFAULT_COMMENTS_JOB_NAME,
) -> list[str]:
    """Create the default Bitbucket folder/jobs when missing via Jenkins API."""
    session = create_jenkins_session(base, user, password)
    folder_path = f"job/{folder_name}"
    review_path, comments_path = build_default_job_paths(folder_name, review_job_name, comments_job_name)
    created_paths: list[str] = []
    credentials = default_bootstrap_secret_text_credentials()
    folder_properties = {
        "SCM_PROVIDER": os.environ.get("SCM_PROVIDER", DEFAULT_BITBUCKET_SCM_PROVIDER).strip()
        or DEFAULT_BITBUCKET_SCM_PROVIDER,
        "SCM_URL": os.environ.get("SCM_URL", DEFAULT_BITBUCKET_SCM_URL).strip()
        or DEFAULT_BITBUCKET_SCM_URL,
    }
    parameter_defaults = default_parameter_defaults(
        scm_provider=folder_properties["SCM_PROVIDER"],
        scm_url=folder_properties["SCM_URL"],
    )

    print(
        "Ensuring Jenkins credentials: "
        + ", ".join(credential.credential_id for credential in credentials)
    )
    ensure_secret_text_credentials(session, credentials)

    if not jenkins_item_exists(session, folder_path):
        print(f"Creating folder: {folder_name}")
        create_folder(session, folder_name)
    sync_folder_config(session, folder_path, folder_properties=folder_properties)

    if not jenkins_item_exists(session, review_path):
        print(f"Creating PR review job shell: {review_path}")
        create_workflow_job(session, folder_path, review_job_name)
        post_config_xml(
            session,
            f"{base}/{review_path}/config.xml",
            build_inline_pipeline_job_config(
                script_body,
                default_full_review_webhook_spec(),
                parameter_defaults=parameter_defaults,
            ),
        )
        created_paths.append(review_path)

    if not jenkins_item_exists(session, comments_path):
        print(f"Creating comment-events job shell: {comments_path}")
        create_workflow_job(session, folder_path, comments_job_name)
        post_config_xml(
            session,
            f"{base}/{comments_path}/config.xml",
            build_inline_pipeline_job_config(
                script_body,
                default_comments_webhook_spec(),
                parameter_defaults=parameter_defaults,
            ),
        )
        created_paths.append(comments_path)

    return created_paths


def sync_jobs(
    base: str,
    user: str,
    password: str,
    jobs: list[str],
    script_body: str,
    *,
    trigger_specs_by_job: dict[str, WebhookTriggerSpec] | None = None,
    parameter_defaults_by_job: dict[str, dict[str, str]] | None = None,
) -> None:
    """Sync the inline Pipeline script into the provided Jenkins jobs."""
    session = create_jenkins_session(base, user, password)
    trigger_specs_by_job = trigger_specs_by_job or {}
    parameter_defaults_by_job = parameter_defaults_by_job or {}
    changed_any = False
    for job_path in jobs:
        job_path = job_path.strip("/")
        get_url = f"{base}/{job_path}/config.xml"
        xml = http_get_text(session, get_url)
        if "<script>" not in xml:
            print(f"skip (no inline script): {job_path}", file=sys.stderr)
            continue
        new_xml = patch_config(
            xml,
            script_body,
            trigger_specs_by_job.get(job_path),
            parameter_defaults=parameter_defaults_by_job.get(job_path),
        )
        post_url = f"{base}/{job_path}/config.xml"
        print(f"POST {post_url} ...")
        post_config_xml(session, post_url, new_xml)
        print(f"  ok: {job_path}")
        changed_any = True

    if changed_any:
        approve_matching_script(session, script_body)
        print("Approved inline Jenkinsfile in Script Security.")


def select_default_job_settings(
    jobs: list[str],
    trigger_specs_by_job: dict[str, WebhookTriggerSpec],
    parameter_defaults_by_job: dict[str, dict[str, str]],
) -> tuple[dict[str, WebhookTriggerSpec], dict[str, dict[str, str]]]:
    """Return trigger/default mappings only for the selected jobs.

    This keeps custom ad-hoc job syncs script-only, while ensuring the documented
    default Bitbucket jobs always receive their webhook/parameter configuration.
    """
    selected_jobs = {job.strip("/") for job in jobs}
    selected_trigger_specs = {
        job_path: spec
        for job_path, spec in trigger_specs_by_job.items()
        if job_path in selected_jobs
    }
    selected_parameter_defaults: dict[str, dict[str, str]] = {}
    for job_path, defaults in parameter_defaults_by_job.items():
        if job_path not in selected_jobs:
            continue
        filtered_defaults = {
            name: value
            for name, value in defaults.items()
            if not (
                name in {"SCM_PROVIDER_OVERRIDE", "SCM_URL_OVERRIDE"}
                and not value.strip()
            )
        }
        if filtered_defaults:
            selected_parameter_defaults[job_path] = filtered_defaults
    return selected_trigger_specs, selected_parameter_defaults


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Sync docker/jenkins/Jenkinsfile into inline Pipeline jobs. "
            "Optional bootstrap mode can create the default Bitbucket two-job setup."
        )
    )
    parser.add_argument(
        "jobs",
        nargs="*",
        help="Job paths relative to Jenkins root (default: sync the default Bitbucket jobs)",
    )
    parser.add_argument(
        "--ensure-default-bitbucket-setup",
        action="store_true",
        help=(
            "Create missing default Bitbucket folder/jobs and enforce Generic Webhook Trigger "
            "configuration for the default Bitbucket setup."
        ),
    )
    parser.add_argument(
        "--folder-name",
        default=DEFAULT_FOLDER_NAME,
        help=f"Folder name for default setup (default: {DEFAULT_FOLDER_NAME})",
    )
    parser.add_argument(
        "--review-job-name",
        default=DEFAULT_REVIEW_JOB_NAME,
        help=f"PR review job name for default setup (default: {DEFAULT_REVIEW_JOB_NAME})",
    )
    parser.add_argument(
        "--comments-job-name",
        default=DEFAULT_COMMENTS_JOB_NAME,
        help=f"Comment-events job name for default setup (default: {DEFAULT_COMMENTS_JOB_NAME})",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    load_local_env()
    base_url = os.environ.get("JENKINS_URL", "http://127.0.0.1:8080").rstrip("/")
    user = (
        os.environ.get("JENKINS_USER", "").strip()
        or os.environ.get("JENKINS_USERNAME", "").strip()
        or "admin"
    )
    password = (
        os.environ.get("JENKINS_PASS", "").strip()
        or os.environ.get("JENKINS_PASSWORD", "").strip()
        or "admin"
    )
    script_body = JENKINSFILE.read_text(encoding="utf-8")

    default_jobs = build_default_job_paths(
        args.folder_name,
        args.review_job_name,
        args.comments_job_name,
    )
    trigger_specs = default_trigger_specs_by_job_path(
        args.folder_name,
        args.review_job_name,
        args.comments_job_name,
    )
    job_parameter_defaults = default_parameter_defaults(
        scm_provider=os.environ.get("SCM_PROVIDER", DEFAULT_BITBUCKET_SCM_PROVIDER).strip()
        or DEFAULT_BITBUCKET_SCM_PROVIDER,
        scm_url=os.environ.get("SCM_URL", DEFAULT_BITBUCKET_SCM_URL).strip() or DEFAULT_BITBUCKET_SCM_URL,
    )
    parameter_defaults_by_job = {job_path: dict(job_parameter_defaults) for job_path in default_jobs}

    if args.ensure_default_bitbucket_setup:
        created = ensure_default_bitbucket_setup(
            base_url,
            user,
            password,
            script_body,
            folder_name=args.folder_name,
            review_job_name=args.review_job_name,
            comments_job_name=args.comments_job_name,
        )
        if created:
            print("Created jobs:")
            for path in created:
                print(f"  - {path}")
        else:
            print("Default Bitbucket folder/jobs already existed; enforcing webhook config via config.xml sync.")

    jobs = [job.strip("/") for job in (args.jobs or default_jobs)]
    selected_trigger_specs, selected_parameter_defaults = select_default_job_settings(
        jobs,
        trigger_specs,
        parameter_defaults_by_job,
    )

    sync_jobs(
        base_url,
        user,
        password,
        jobs,
        script_body,
        trigger_specs_by_job=selected_trigger_specs or None,
        parameter_defaults_by_job=selected_parameter_defaults or None,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
