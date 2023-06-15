from loguru import logger
import modal
from pydantic import ValidationError  # type: ignore

from src.handlers.on_ticket import on_ticket
from src.handlers.on_comment import on_comment
from src.utils.constants import API_NAME, BOT_TOKEN_NAME, DB_NAME, LABEL_COLOR, LABEL_DESCRIPTION, LABEL_NAME, SWEEP_LOGIN
from src.events import (
    CommentCreatedRequest,
    InstallationCreatedRequest,
    IssueCommentRequest,
    IssueRequest,
    ReposAddedRequest,
)
from src.utils.event_logger import posthog
from src.utils.github_utils import get_github_client, index_full_repository
from fastapi import HTTPException, Request

stub = modal.Stub(API_NAME)
image = (
    modal.Image.debian_slim()
    .apt_install("git")
    .pip_install(
        "openai",
        "anthropic",
        "PyGithub",
        "loguru",
        "docarray",
        "backoff",
        "tiktoken",
        "highlight-io",
        "GitPython",
        "posthog",
        "tqdm"
    )
)
secrets = [
    modal.Secret.from_name(BOT_TOKEN_NAME),
    modal.Secret.from_name("openai-secret"),
    modal.Secret.from_name("anthropic"),
    modal.Secret.from_name("posthog"),
    modal.Secret.from_name("highlight"),
]

FUNCTION_SETTINGS = {
    "image": image,
    "secrets": secrets,
    "timeout": 15 * 60,
}
retries = modal.Retries(
    max_retries=2,
    backoff_coefficient=2,
    initial_delay=60,
)

handle_ticket = stub.function(**FUNCTION_SETTINGS, retries=retries)(on_ticket)
handle_comment = stub.function(**FUNCTION_SETTINGS, retries=retries)(on_comment)
update_index = modal.Function.lookup(DB_NAME, "update_index")


@stub.function(**FUNCTION_SETTINGS)
@modal.web_endpoint(method="POST")
async def webhook(raw_request: Request):
    """Handle a webhook request from GitHub."""
    try:
        request_dict = await raw_request.json()
        logger.info(f"Received request: {request_dict.keys()}")
        event = raw_request.headers.get("X-GitHub-Event")
        assert event is not None
        match event, request_dict.get("action", None):
            case "issues", "opened":
                request = IssueRequest(**request_dict)
                issue_title_lower = request.issue.title.lower()
                if issue_title_lower.startswith("sweep") or "sweep:" in issue_title_lower:
                    g = get_github_client(request.installation.id)
                    repo = g.get_repo(request.repository.full_name)

                    labels = repo.get_labels()
                    label_names = [label.name for label in labels]

                    if LABEL_NAME not in label_names:
                        repo.create_label(
                            name=LABEL_NAME,
                            color=LABEL_COLOR,
                            description=LABEL_DESCRIPTION,
                        )
                    # TODO(sweep): figure out why this is breaking
                    # else:
                    #     label = repo.get_label(LABEL_NAME)
                    #     label.edit(
                    #         name=LABEL_NAME,
                    #         color=LABEL_COLOR, 
                    #         description=LABEL_DESCRIPTION
                    #     )
                    
                    current_issue = repo.get_issue(number=request.issue.number)
                    current_issue.add_to_labels(LABEL_NAME)
            case "issues", "labeled":
                request = IssueRequest(**request_dict)
                if request.issue is not None and (
                    "sweep" in [label.name.lower() for label in request.issue.labels]
                ):
                    request.issue.body = request.issue.body or ""
                    request.repository.description = (
                        request.repository.description or ""
                    )
                    # Update before we handle the ticket to make sure index is up to date
                    # other ways suboptimal
                    handle_ticket.spawn(
                        request.issue.title,
                        request.issue.body,
                        request.issue.number,
                        request.issue.html_url,
                        request.issue.user.login,
                        request.repository.full_name,
                        request.repository.description,
                        request.installation.id,
                    )
            case "issue_comment", "created":
                request = IssueCommentRequest(**request_dict)
                if request.issue is not None \
                    and "sweep" in [label.name.lower() for label in request.issue.labels] \
                    and request.comment.user.type == "User":
                    request.issue.body = request.issue.body or ""
                    request.repository.description = (
                        request.repository.description or ""
                    )
                    # Update before we handle the ticket to make sure index is up to date
                    # other ways suboptimal
                    handle_ticket.spawn(
                        request.issue.title,
                        request.issue.body,
                        request.issue.number,
                        request.issue.html_url,
                        request.issue.user.login,
                        request.repository.full_name,
                        request.repository.description,
                        request.installation.id,
                        request.comment.id
                    )
                elif request.issue.pull_request and request.issue.user.login == SWEEP_LOGIN: # TODO(sweep): set a limit                    
                    logger.info(f"Handling comment on PR: {request.issue.pull_request}")
                    handle_comment.spawn(
                        repo_full_name=request.repository.full_name,
                        repo_description=request.repository.description,
                        comment=request.comment.body,
                        pr_path=None,
                        pr_line_position=None,
                        username=request.comment.user.login,
                        installation_id=request.installation.id,
                        pr_number=request.issue.number,
                    )
            case "pull_request_review_comment", "created":
                request = CommentCreatedRequest(**request_dict)
                if "sweep/" in request.pull_request.head.ref.lower():
                    handle_comment.spawn(
                        repo_full_name=request.repository.full_name,
                        repo_description=request.repository.description,
                        comment=request.comment.body,
                        pr_path=request.comment.path,
                        pr_line_position=request.comment.original_line,
                        username=request.comment.user.login,
                        installation_id=request.installation.id,
                        pr_number=request.pull_request.number,
                    )
                # Todo: update index on comments
            case "pull_request_review", "submitted":
                request = ReviewSubmittedRequest(**request_dict)
            case "installation_repositories", "added":
                repos_added_request = ReposAddedRequest(**request_dict)
                metadata = {
                    "installation_id": repos_added_request.installation.id,
                    "repositories": [
                        repo.full_name
                        for repo in repos_added_request.repositories_added
                    ],
                }
                posthog.capture("installation_repositories", "started", properties={
                    **metadata
                })
                for repo in repos_added_request.repositories_added:
                    organization, repo_name = repo.full_name.split("/")
                    posthog.capture(
                        organization,
                        "installed_repository",
                        properties={
                            "repo_name": repo_name,
                            "organization": organization,
                            "repo_full_name": repo.full_name
                        }
                    )
                    index_full_repository(
                        repo.full_name,
                        installation_id=repos_added_request.installation.id,
                    )
            case "installation", "created":
                repos_added_request = InstallationCreatedRequest(**request_dict)
                for repo in repos_added_request.repositories:
                    index_full_repository(
                        repo.full_name,
                        installation_id=repos_added_request.installation.id,
                    )
            case ("push", None) | ("pull_request", "closed"):
                if event != "pull_request" or request_dict["base"]["merged"] == True:
                    update_index.spawn(
                        request_dict["repository"]["full_name"],
                        installation_id=request_dict["installation"]["id"],
                    )
            case "ping", None:
                return {"message": "pong"}
            case _:
                logger.warning(
                    f"Unhandled event: {event} {request_dict.get('action', None)}"
                )
                raise HTTPException(
                    status_code=422,
                    detail=f"Unhandled event: {event} {request_dict.get('action', None)}",
                )
    except ValidationError as e:
        logger.error(f"Failed to parse request: {e}")
        raise HTTPException(status_code=422, detail="Failed to parse request")
    return {"success": True}