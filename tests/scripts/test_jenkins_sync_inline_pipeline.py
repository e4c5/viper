from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "jenkins_sync_inline_pipeline.py"


def load_module():
    spec = importlib.util.spec_from_file_location("jenkins_sync_inline_pipeline", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_patch_config_rewrites_legacy_param_block() -> None:
    module = load_module()
    xml = """
<flow-definition>
  <actions>
    <string>SCM_PROVIDER</string>
    <string>SCM_URL</string>
    <string>LLM_PROVIDER</string>
    <string>LLM_MODEL</string>
  </actions>
  <definition>
    <script>old script</script>
  </definition>
  <hudson.model.StringParameterDefinition>
    <name>SCM_PROVIDER</name>
  </hudson.model.StringParameterDefinition>
  <hudson.model.StringParameterDefinition>
    <name>SCM_URL</name>
  </hudson.model.StringParameterDefinition>
  <hudson.model.StringParameterDefinition>
    <name>LLM_PROVIDER</name>
  </hudson.model.StringParameterDefinition>
  <hudson.model.StringParameterDefinition>
    <name>LLM_MODEL</name>
  </hudson.model.StringParameterDefinition>
</flow-definition>
""".strip()

    out = module.patch_config(xml, "echo '<updated>'")

    assert "<script>echo &apos;&lt;updated&gt;&apos;</script>" in out
    assert "<name>SCM_PROVIDER_OVERRIDE</name>" in out
    assert "<name>SCM_URL_OVERRIDE</name>" in out
    assert "<name>LLM_PROVIDER_OVERRIDE</name>" in out
    assert "<name>LLM_MODEL</name>" not in out
    assert "<name>LLM_MODEL_OVERRIDE</name>" not in out
    assert "<string>SCM_PROVIDER</string>" not in out
    assert "<string>SCM_URL</string>" not in out
    assert "<string>LLM_PROVIDER</string>" not in out
    assert "<string>LLM_MODEL</string>" not in out


def test_patch_config_is_idempotent_for_modern_jobs() -> None:
    module = load_module()
    xml = """
<flow-definition>
  <actions>
    <string>SCM_PROVIDER_OVERRIDE</string>
    <string>SCM_URL_OVERRIDE</string>
    <string>LLM_PROVIDER_OVERRIDE</string>
    <string>LLM_MODEL</string>
  </actions>
  <definition>
    <script>old script</script>
  </definition>
  <hudson.model.StringParameterDefinition>
    <name>SCM_PROVIDER_OVERRIDE</name>
  </hudson.model.StringParameterDefinition>
  <hudson.model.StringParameterDefinition>
    <name>SCM_URL_OVERRIDE</name>
  </hudson.model.StringParameterDefinition>
  <hudson.model.StringParameterDefinition>
    <name>LLM_PROVIDER_OVERRIDE</name>
  </hudson.model.StringParameterDefinition>
</flow-definition>
""".strip()

    out = module.patch_config(xml, "println('modern')")

    assert "<script>println(&apos;modern&apos;)</script>" in out
    assert out.count("<name>SCM_PROVIDER_OVERRIDE</name>") == 1
    assert out.count("<name>SCM_URL_OVERRIDE</name>") == 1
    assert out.count("<name>LLM_PROVIDER_OVERRIDE</name>") == 1
    assert "<string>LLM_MODEL</string>" not in out


def test_default_comments_webhook_spec_includes_comment_context() -> None:
    module = load_module()

    full_review = module.default_full_review_webhook_spec()
    comments = module.default_comments_webhook_spec()

    assert full_review.filter_regex == "^pr:(opened|modified|from_ref_updated)$"
    assert comments.filter_regex == "^pr:comment:(added|edited|deleted)$"
    assert "CODE_REVIEW_EVENT_COMMENT_ID" not in full_review.post_content_params
    assert comments.post_content_params["CODE_REVIEW_EVENT_COMMENT_ID"] == "$.comment.id"
    assert comments.post_content_params["CODE_REVIEW_EVENT_ACTOR_LOGIN"] == "$.actor.slug"
    assert comments.post_content_params["CODE_REVIEW_EVENT_ACTOR_ID"] == "$.actor.id"


def test_build_inline_pipeline_job_config_contains_parameters_and_trigger() -> None:
    module = load_module()

    xml = module.build_inline_pipeline_job_config(
        "pipeline { agent any }",
        module.default_comments_webhook_spec(),
    )

    assert "<flow-definition>" in xml
    assert "<name>SCM_OWNER</name>" in xml
    assert "<name>SCM_PROVIDER_OVERRIDE</name>" in xml
    assert "<org.jenkinsci.plugins.gwt.GenericTrigger>" in xml
    assert "<key>CODE_REVIEW_EVENT_COMMENT_ID</key>" in xml
    assert "^pr:comment:(added|edited|deleted)$" in xml
    assert "<script>pipeline { agent any }</script>" in xml


def test_patch_config_upserts_trigger_property() -> None:
    module = load_module()
    xml = """
<flow-definition>
  <actions/>
  <properties>
    <hudson.model.ParametersDefinitionProperty>
      <parameterDefinitions/>
    </hudson.model.ParametersDefinitionProperty>
  </properties>
  <definition>
    <script>old script</script>
  </definition>
</flow-definition>
""".strip()

    out = module.patch_config(xml, "new script", module.default_full_review_webhook_spec())

    assert "<script>new script</script>" in out
    assert "<org.jenkinsci.plugins.gwt.GenericTrigger>" in out
    assert "^pr:(opened|modified|from_ref_updated)$" in out


def test_build_inline_pipeline_job_config_sets_override_defaults() -> None:
    module = load_module()

    xml = module.build_inline_pipeline_job_config(
        "pipeline { agent any }",
        module.default_full_review_webhook_spec(),
        parameter_defaults={
            "SCM_PROVIDER_OVERRIDE": "bitbucket_server",
            "SCM_URL_OVERRIDE": "http://localhost:7990/rest/api/1.0",
        },
    )

    assert "<name>SCM_PROVIDER_OVERRIDE</name>" in xml
    assert "<defaultValue>bitbucket_server</defaultValue>" in xml
    assert "<name>SCM_URL_OVERRIDE</name>" in xml
    assert "<defaultValue>http://localhost:7990/rest/api/1.0</defaultValue>" in xml


def test_build_folder_config_xml_contains_folder_properties() -> None:
    module = load_module()

    xml = module.build_folder_config_xml(
        {
            "SCM_PROVIDER": "bitbucket_server",
            "SCM_URL": "http://localhost:7990/rest/api/1.0",
        }
    )

    assert "<com.cloudbees.hudson.plugins.folder.Folder" in xml
    assert "<com.mig82.folders.properties.FolderProperties>" in xml
    assert "<key>SCM_PROVIDER</key>" in xml
    assert "<value>bitbucket_server</value>" in xml
    assert "<key>SCM_URL</key>" in xml
    assert "<value>http://localhost:7990/rest/api/1.0</value>" in xml


def test_patch_folder_config_upserts_folder_properties() -> None:
    module = load_module()
    xml = """
<com.cloudbees.hudson.plugins.folder.Folder>
  <actions/>
  <properties>
  </properties>
</com.cloudbees.hudson.plugins.folder.Folder>
""".strip()

    out = module.patch_folder_config(
        xml,
        {
            "SCM_PROVIDER": "bitbucket_server",
            "SCM_URL": "http://localhost:7990/rest/api/1.0",
        },
    )

    assert "<com.mig82.folders.properties.FolderProperties>" in out
    assert "<key>SCM_PROVIDER</key>" in out
    assert "<key>SCM_URL</key>" in out


def test_patch_config_sets_parameter_defaults_for_existing_jobs() -> None:
    module = load_module()
    xml = """
<flow-definition>
  <actions/>
  <properties>
    <hudson.model.ParametersDefinitionProperty>
      <parameterDefinitions>
        <hudson.model.StringParameterDefinition>
          <name>SCM_PROVIDER_OVERRIDE</name>
          <description>x</description>
          <defaultValue></defaultValue>
          <trim>false</trim>
        </hudson.model.StringParameterDefinition>
        <hudson.model.StringParameterDefinition>
          <name>SCM_URL_OVERRIDE</name>
          <description>x</description>
          <defaultValue></defaultValue>
          <trim>false</trim>
        </hudson.model.StringParameterDefinition>
      </parameterDefinitions>
    </hudson.model.ParametersDefinitionProperty>
  </properties>
  <definition>
    <script>old script</script>
  </definition>
</flow-definition>
""".strip()

    out = module.patch_config(
        xml,
        "new script",
        parameter_defaults={
            "SCM_PROVIDER_OVERRIDE": "bitbucket_server",
            "SCM_URL_OVERRIDE": "http://localhost:7990/rest/api/1.0",
        },
    )

    assert "<script>new script</script>" in out
    assert "<name>SCM_PROVIDER_OVERRIDE</name>" in out
    assert "<defaultValue>bitbucket_server</defaultValue>" in out
    assert "<name>SCM_URL_OVERRIDE</name>" in out
    assert "<defaultValue>http://localhost:7990/rest/api/1.0</defaultValue>" in out


def test_default_bootstrap_secret_text_credentials_requires_scm_token(monkeypatch) -> None:
    module = load_module()

    monkeypatch.delenv("SCM_TOKEN", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "llm-key")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    try:
        module.default_bootstrap_secret_text_credentials()
    except RuntimeError as exc:
        assert "SCM_TOKEN is required" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError when SCM_TOKEN is missing")


def test_default_bootstrap_secret_text_credentials_skips_llm_key_for_ollama(monkeypatch) -> None:
    module = load_module()

    monkeypatch.setenv("SCM_TOKEN", "scm-token")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    credentials = module.default_bootstrap_secret_text_credentials()

    assert [credential.credential_id for credential in credentials] == ["SCM_TOKEN"]


def test_build_secret_text_credentials_groovy_targets_global_secret_text_store() -> None:
    module = load_module()

    groovy = module.build_secret_text_credentials_groovy(
        [
            module.SecretTextCredential(
                credential_id="SCM_TOKEN",
                secret_text="secret-value",
                description="SCM API token",
            ),
            module.SecretTextCredential(
                credential_id="LLM_API_KEY",
                secret_text="llm-secret",
                description="LLM API key (used with LLM_PROVIDER)",
            ),
        ]
    )

    assert "SystemCredentialsProvider" in groovy
    assert "StringCredentialsImpl" in groovy
    assert "CREATED:" in groovy
    assert "EXISTS:" in groovy
    assert "U0NNX1RPS0VO" in groovy
    assert "TExNX0FQSV9LRVk=" in groovy


def test_approve_matching_script_uses_normalized_script_text(monkeypatch) -> None:
    module = load_module()
    session = object()
    approved_hashes: list[str] = []
    console_calls: list[str] = []

    monkeypatch.setattr(
        module,
        "get_pending_script_approvals",
        lambda _session: [
            module.PendingScriptApproval(hash="abc123", script="line1\r\nline2\r\n"),
        ],
    )
    monkeypatch.setattr(
        module,
        "approve_script_hash",
        lambda _session, script_hash: approved_hashes.append(script_hash),
    )
    monkeypatch.setattr(
        module,
        "preapprove_script_via_console",
        lambda _session, script: console_calls.append(script),
    )

    module.approve_matching_script(session, "line1\nline2\n")

    assert approved_hashes == ["abc123"]
    assert console_calls == []


def test_approve_matching_script_falls_back_to_console_when_not_pending(monkeypatch) -> None:
    module = load_module()
    console_calls: list[str] = []

    monkeypatch.setattr(module, "get_pending_script_approvals", lambda _session: [])
    monkeypatch.setattr(module, "approve_script_hash", lambda _session, _hash: None)
    monkeypatch.setattr(
        module,
        "preapprove_script_via_console",
        lambda _session, script: console_calls.append(script),
    )

    module.approve_matching_script(object(), "pipeline { agent any }")

    assert console_calls == ["pipeline { agent any }"]
