import json
from loguru import logger
import github
from github.Repository import Repository
from github.ContentFile import ContentFile
from github.GithubException import GithubException
from pydantic import BaseModel


from src.core.models import (
    ChatGPT,
    FileChange,
    FileChangeRequest,
    FilesToChange,
    PullRequest,
    RegexMatchError,
    Function
)
from src.core.prompts import (
    files_to_change_prompt,
    pull_request_prompt,
    create_file_prompt,
    modify_file_prompt,
    cot_retrieval_prompt
)
from src.core.react import REACT_INITIAL_PROMPT, ReadFiles, Finish, Toolbox
from src.utils.diff import fuse_files


class CodeGenBot(ChatGPT):

    def get_files_to_change(self):
        file_change_requests: list[FileChangeRequest] = []
        for count in range(5):
            try:
                logger.info(f"Generating for the {count}th time...")
                files_to_change_response = self.chat(files_to_change_prompt, message_key="files_to_change") # Dedup files to change here
                files_to_change = FilesToChange.from_string(files_to_change_response)
                files_to_create: list[str] = files_to_change.files_to_create.split("*")
                files_to_modify: list[str] = files_to_change.files_to_modify.split("*")
                for file_change_request, change_type in zip(
                    files_to_create + files_to_modify,
                    ["create"] * len(files_to_create)
                    + ["modify"] * len(files_to_modify),
                ):
                    file_change_request = file_change_request.strip()
                    if not file_change_request or file_change_request == "None":
                        continue
                    logger.debug(file_change_request)
                    logger.debug(change_type)
                    file_change_requests.append(
                        FileChangeRequest.from_string(
                            file_change_request, change_type=change_type
                        )
                    )
                if file_change_requests:
                    return file_change_requests
            except RegexMatchError:
                logger.warning("Failed to parse! Retrying...")
                self.delete_messages_from_chat("files_to_change")
                continue
        raise Exception("Could not generate files to change")

    def generate_pull_request(self):
        pull_request = None
        for count in range(5):
            try:
                logger.info(f"Generating for the {count}th time...")
                pr_text_response = self.chat(pull_request_prompt, message_key="pull_request")
            except Exception:
                logger.warning("Failed to parse! Retrying...")
                self.undo()
                continue
            pull_request = PullRequest.from_string(pr_text_response)
            pull_request.branch_name = "sweep/" + pull_request.branch_name[:250]
            return pull_request
        raise Exception("Could not generate PR text")


class GithubBot(BaseModel):
    class Config:
        arbitrary_types_allowed = True  # for repo: Repository

    repo: Repository

    def get_contents(self, path: str, branch: str = ""):
        if not branch:
            branch = self.repo.default_branch
        try:
            return self.repo.get_contents(path, ref=branch)
        except Exception as e:
            logger.error(path)
            raise e


    def get_file(self, file_path: str, branch: str = "") -> ContentFile:
        content = self.get_contents(file_path, branch)
        assert not isinstance(content, list)
        return content

    def check_path_exists(self, path: str, branch: str = ""):
        try:
            self.get_contents(path, branch)
            return True
        except Exception:
            return False

    def create_branch(self, branch: str) -> str:
        # Generate PR if nothing is supplied maybe
        base_branch = self.repo.get_branch(self.repo.default_branch)
        try:
            self.repo.create_git_ref(f"refs/heads/{branch}", base_branch.commit.sha)
            return branch
        except GithubException as e:
            logger.error(f"Error: {e}, trying with other branch names...")
            for i in range(1, 100):
                try:
                    logger.warning(f"Retrying {branch}_{i}...")
                    self.repo.create_git_ref(
                        f"refs/heads/{branch}_{i}", base_branch.commit.sha
                    )
                    return f"{branch}_{i}"
                except GithubException:
                    pass
            raise e


class SweepBot(CodeGenBot, GithubBot):
    def cot_retrieval(self):
        # TODO(sweep): add semantic search using vector db
        # TODO(sweep): add search using webpilot + github
        functions = [
            Function(
                name="cat",
                description="Cat files. Max 3 files per request.",
                parameters={
                    "properties": {
                        "filepath": {
                            "type": "string",
                            "description": "Paths to files. One per line."
                        },
                    }
                } # manage file too large
            ),
            Function(
                name="finish",
                description="Indicate you have sufficient data to proceed.",
                parameters={"properties": {}} 
            ),
        ]


        response = self.chat(
            cot_retrieval_prompt, 
            message_key="cot_retrieval",
            functions=functions
        )
        is_function_call = self.messages[-1].is_function_call
        for _retry in range(3):
            logger.info("Got response.")
            if not is_function_call:
                break

            response = json.loads(response)
            function_name = response["name"]
            arguments = response["arguments"]
            logger.info(f"Fetching file {function_name} with arguments {arguments}.")
            arguments = json.loads(arguments)
            if function_name == "finish":
                return
            elif function_name == "cat":
                path = arguments["filepath"]
                try:
                    logger.info("Retrieving file...")
                    content = self.get_file(path).decoded_content.decode("utf-8")
                    logger.info("Received file")
                except github.GithubException:
                    response = self.chat(
                        f"File not found: {path}",
                        message_key=path,
                        functions=functions,
                    )
                else:
                    response = self.chat(
                        f"Here is the file: <file path=\"{path}\">\n\n{content[:10000]}</file>. Fetch more content or call finish.", 
                        message_key=path,
                        functions=functions
                    ) # update this constant
        return

    def create_file(self, file_change_request: FileChangeRequest) -> FileChange:
        file_change: FileChange | None = None
        for count in range(5):
            create_file_response = self.chat(
                create_file_prompt.format(
                    filename=file_change_request.filename,
                    instructions=file_change_request.instructions,
                ),
                message_key=f"file_change_{file_change_request.filename}",
            )
            # Add file to list of changed_files
            self.file_change_paths.append(file_change_request.filename)
            # self.delete_file_from_system_message(file_path=file_change_request.filename)
            try:
                file_change = FileChange.from_string(create_file_response)
                assert file_change is not None
                file_change.commit_message = f"sweep: {file_change.commit_message[:50]}"
                return file_change
            except Exception:
                logger.warning(f"Failed to parse. Retrying for the {count}th time...")
                self.undo()
                continue
        raise Exception("Failed to parse response after 5 attempts.")

    def modify_file(
        self, file_change_request: FileChangeRequest, contents: str = ""
    ) -> FileChange:
        if not contents:
            contents = self.get_file(
                file_change_request.filename
            ).decoded_content.decode("utf-8")
        file_change: FileChange | None = None
        for count in range(5):
            modify_file_response = self.chat(
                modify_file_prompt.format(
                    filename=file_change_request.filename,
                    instructions=file_change_request.instructions,
                    code=contents,
                ),
                message_key=f"file_change_{file_change_request.filename}",
            )
            # Add file to list of changed_files
            self.file_change_paths.append(file_change_request.filename)
            # self.delete_file_from_system_message(file_path=file_change_request.filename)
            try:
                file_change = FileChange.from_string(modify_file_response)
                file_change.code = fuse_files(contents, file_change.code)
            except Exception as e:
                logger.warning(f"Recieved error {e}")
                logger.warning(
                    f"Failed to parse. Retrying for the {count}th time..."
                )
                self.undo()
                continue
            else:
                assert file_change is not None
                file_change.commit_message = f"sweep: {file_change.commit_message[:50]}"
                return file_change
                 
        raise Exception("Failed to parse response after 5 attempts.")
 
    def change_file(self, file_change_request: FileChangeRequest):
        if file_change_request.change_type == "create":
            return self.create_file(file_change_request)
        elif file_change_request.change_type == "modify":
            return self.create_file(file_change_request)
        else:
            raise Exception("Not a valid file type")

    def change_files_in_github(
        self,
        file_change_requests: list[FileChangeRequest],
        branch: str,
    ):
        # should check if branch exists, if not, create it
        logger.debug(file_change_requests)
        for file_change_request in file_change_requests:
            if file_change_request.change_type == "create":
                try: # Try to create
                    file_change = self.create_file(file_change_request)
                    logger.debug(
                        f"{file_change_request.filename}, {file_change.commit_message}, {file_change.code}, {branch}"
                    )
                    self.repo.create_file(
                        file_change_request.filename,
                        file_change.commit_message,
                        file_change.code,
                        branch=branch,
                    )
                except github.GithubException as e:
                    logger.info(e)
                    try: # Try to modify
                        contents = self.get_file(file_change_request.filename, branch=branch)
                        self.repo.update_file(
                            file_change_request.filename,
                            file_change.commit_message,
                            file_change.code,
                            contents.sha,
                            branch=branch,
                        )
                    except:
                        pass
            elif file_change_request.change_type == "modify":
                # TODO(sweep): Cleanup this
                try:
                    contents = self.get_file(file_change_request.filename, branch=branch)
                except github.UnknownObjectException as e:
                    logger.warning(f"Received error {e}, trying creating file...")
                    file_change_request.change_type = "create"
                    self.create_file(file_change_request)
                    file_change = self.create_file(file_change_request)
                    logger.debug(
                        f"{file_change_request.filename}, {file_change.commit_message}, {file_change.code}, {branch}"
                    )
                    self.repo.create_file(
                        file_change_request.filename,
                        file_change.commit_message,
                        file_change.code,
                        branch=branch,
                    )
                else:
                    file_change = self.modify_file(
                        file_change_request, contents.decoded_content.decode("utf-8")
                    )
                    logger.debug(
                        f"{file_change_request.filename}, {file_change.commit_message}, {file_change.code}, {branch}"
                    )
                    self.repo.update_file(
                        file_change_request.filename,
                        file_change.commit_message,
                        file_change.code,
                        contents.sha,
                        branch=branch,
                    )
            else:
                raise Exception("Invalid change type")