"""ADK Runner setup and programmatic invocation for code review."""

import uuid

from google.genai import types

from code_review.agent import create_review_agent
from code_review.config import get_scm_config
from code_review.providers import get_provider
from code_review.standards import detect_from_paths, get_review_standards


APP_NAME = "code_review"
USER_ID = "reviewer"


def run_review(owner: str, repo: str, pr_number: int, head_sha: str = "") -> None:
    """
    Run the code review agent on a PR.
    Creates provider, detects language, builds agent with review standards,
    runs the agent with PR metadata, and lets the agent use tools to post comments.
    """
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    cfg = get_scm_config()
    provider = get_provider(cfg.provider, cfg.url, cfg.token)

    # Detect language/framework from PR files (for review standards)
    files = provider.get_pr_files(owner, repo, pr_number)
    paths = [f.path for f in files]
    detected = detect_from_paths(paths)
    review_standards = get_review_standards(detected.language, detected.framework)

    agent = create_review_agent(provider, review_standards)

    session_id = f"{owner}_{repo}_{pr_number}_{uuid.uuid4().hex[:12]}"
    session_service = InMemorySessionService()
    session_service.create_session_sync(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=session_id,
    )

    runner = Runner(
        agent=agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    msg = (
        f"Review this PR: owner={owner}, repo={repo}, pr_number={pr_number}."
        + (f" head_sha={head_sha}." if head_sha else "")
    )
    content = types.Content(role="user", parts=[types.Part(text=msg)])

    for event in runner.run(
        user_id=USER_ID,
        session_id=session_id,
        new_message=content,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    print(part.text)
