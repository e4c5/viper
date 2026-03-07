"""Reusable Jenkins UI actions for Playwright.

Uses Jenkins classic UI (Manage Jenkins, New Item, Credentials, etc.).
Secrets are supplied from EnvLoader (same names as Jenkins credential IDs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page


class JenkinsUI:
    """Encapsulates Jenkins UI flows: login, folders, credentials, jobs, env, webhooks."""

    def __init__(
        self,
        page: Page,
        base_url: str,
        username: str = "admin",
        password: str = "admin",
    ) -> None:
        self._page = page
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password

    def login(self) -> None:
        """Log in to Jenkins (classic UI: j_username, j_password)."""
        self._page.goto(f"{self._base_url}/login")
        self._page.get_by_label("User").fill(self._username)
        self._page.get_by_label("Password").fill(self._password)
        self._page.get_by_role("button", name="Sign in").click()
        self._page.wait_for_url(f"{self._base_url}/**", wait_until="networkidle")

    def create_folder(self, name: str) -> None:
        """Create a top-level folder (from dashboard: New Item -> Folder -> OK)."""
        self._page.goto(self._base_url)
        self._page.get_by_role("link", name="New Item").click()
        self._page.get_by_role("textbox", name="Enter an item name").fill(name)
        self._page.get_by_role("link", name="Folder").click()
        self._page.get_by_role("button", name="OK").click()
        self._page.wait_for_url(f"{self._base_url}/job/{name}/**", wait_until="networkidle")

    def add_credential_global(self, credential_id: str, secret: str, description: str = "") -> None:
        """Add a Secret text credential in Global credentials (unrestricted)."""
        self._page.goto(f"{self._base_url}/manage")
        self._page.get_by_role("link", name="Credentials").click()
        self._page.get_by_role("link", name="System").click()
        self._page.get_by_role("link", name="Global credentials (unrestricted)").click()
        self._page.get_by_role("link", name="Add Credentials").click()
        self._page.get_by_label("Kind").select_option("Secret text")
        self._page.get_by_label("Scope").select_option("Global")
        self._page.get_by_label("Secret").fill(secret)
        self._page.get_by_label("ID").fill(credential_id)
        if description:
            self._page.get_by_label("Description").fill(description)
        self._page.get_by_role("button", name="Create").click()

    def add_credential_in_folder(
        self,
        folder_name: str,
        credential_id: str,
        secret: str,
        description: str = "",
    ) -> None:
        """Add a Secret text credential in a folder (Folder -> Credentials -> Global -> Add)."""
        self._page.goto(f"{self._base_url}/job/{folder_name}/credentials")
        # Click the "Global" domain row to get to Add credentials
        self._page.get_by_role("link", name="Global").click()
        self._page.get_by_role("link", name="Add Credentials").click()
        self._page.get_by_label("Kind").select_option("Secret text")
        self._page.get_by_label("Scope").select_option("Global")
        self._page.get_by_label("Secret").fill(secret)
        self._page.get_by_label("ID").fill(credential_id)
        if description:
            self._page.get_by_label("Description").fill(description)
        self._page.get_by_role("button", name="Create").click()

    def set_global_env_vars(self, env_vars: dict[str, str]) -> None:
        """Set global environment variables (Manage Jenkins -> System -> Global properties).

        Jenkins UI for key/value list varies by version; this uses a generic approach.
        If it fails, selectors in this method may need adjustment for your Jenkins.
        """
        self._page.goto(f"{self._base_url}/configure")
        cb = self._page.get_by_role("checkbox", name="Environment variables")
        if cb.is_visible():
            cb.check()
        for i, (key, value) in enumerate(env_vars.items()):
            if i > 0:
                add_btn = self._page.get_by_role("button", name="Add").first
                if add_btn.is_visible():
                    add_btn.click()
            # Try common patterns for key/value inputs in Global properties
            key_inputs = self._page.locator("input[name*='name'], input[placeholder*='Key']")
            val_inputs = self._page.locator("input[name*='value'], input[placeholder*='Value']")
            if key_inputs.count() > i and val_inputs.count() > i:
                key_inputs.nth(i).fill(key)
                val_inputs.nth(i).fill(value)
        self._page.get_by_role("button", name="Save").click()

    def create_pipeline_job(
        self,
        name: str,
        script_path: str,
        repo_url: str | None = None,
        branch: str = "main",
        inside_folder: str | None = None,
    ) -> None:
        """Create a Pipeline job (Pipeline script from SCM or inline)."""
        prefix = f"{self._base_url}/job/{inside_folder}" if inside_folder else self._base_url
        self._page.goto(prefix)
        self._page.get_by_role("link", name="New Item").click()
        self._page.get_by_role("textbox", name="Enter an item name").fill(name)
        self._page.get_by_role("link", name="Pipeline").click()
        self._page.get_by_role("button", name="OK").click()
        self._page.wait_for_url(
            f"**/job/{name}/**",
            wait_until="networkidle",
        )
        # Pipeline script from SCM
        self._page.get_by_label("Pipeline script from SCM").check()
        if repo_url:
            self._page.get_by_label("Repository URL").fill(repo_url)
        self._page.get_by_label("Branch").fill(branch)
        self._page.get_by_label("Script Path").fill(script_path)
        self._page.get_by_role("button", name="Save").click()

    def configure_webhook_trigger(
        self,
        job_name: str,
        folder_name: str | None = None,
        post_content_params: dict[str, str] | None = None,
        filter_text: str = "$PR_ACTION",
        filter_regex: str = "^(opened|synchronize)$",
    ) -> None:
        """Enable Generic Webhook Trigger and set Post content parameters and optional filter."""
        if folder_name:
            url = f"{self._base_url}/job/{folder_name}/job/{job_name}/configure"
        else:
            url = f"{self._base_url}/job/{job_name}/configure"
        self._page.goto(url)
        self._page.get_by_role("checkbox", name="Generic Webhook Trigger").check()
        if post_content_params:
            for var_name, jsonpath_expr in post_content_params.items():
                self._page.get_by_role("button", name="Add Parameter").click()
                self._page.locator("input[name*='variable']").last.fill(var_name)
                self._page.locator("input[name*='expression']").last.fill(jsonpath_expr)
        self._page.locator("input[name*='filterExpression']").fill(filter_text)
        self._page.locator("input[name*='filterRegex']").fill(filter_regex)
        self._page.get_by_role("button", name="Save").click()

    def open_job(self, job_name: str, folder_name: str | None = None) -> None:
        """Navigate to job page."""
        if folder_name:
            self._page.goto(f"{self._base_url}/job/{folder_name}/job/{job_name}/")
        else:
            self._page.goto(f"{self._base_url}/job/{job_name}/")

    def move_job_into_folder(self, job_name: str, folder_name: str) -> None:
        """Move an existing job into a folder (job -> Move -> select folder)."""
        self._page.goto(f"{self._base_url}/job/{job_name}/move")
        self._page.get_by_role("combobox", name="Destination").select_option(folder_name)
        self._page.get_by_role("button", name="Move").click()
