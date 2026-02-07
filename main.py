import dataclasses
import datetime
import logging

from typing import Annotated
from pprint import pprint
from asyncio import sleep

from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from gql import Client, gql
from gql.transport.httpx import HTTPXAsyncTransport

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    event_name: str
    lead_html: str
    github_token: str
    github_org_name: str
    github_repo_name: str
    github_project_number: int

settings = Settings()  # Loads vars from env vars automatically

# ---

with open('schema.docs.graphql') as f:
    GITHUB_GQL_SCHEMA = f.read()

GQL_ASYNC_ARGS = {
    "transport": HTTPXAsyncTransport(
        url="https://api.github.com/graphql",
        headers={"Authorization": f"Bearer {settings.github_token}"},
    ),
    "schema": GITHUB_GQL_SCHEMA,
}

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

METADATA: "ProjectMetadata | None" = None
DEBUG: bool = False

# ---

WEEKDAYS = {
    0: "Poniedziałek",
    1: "Wtorek",
    2: "Środa",
    3: "Czwartek",
    4: "Piątek",
    5: "Sobota",
    6: "Niedziela",
}


@dataclasses.dataclass
class DeptConfig:
    id: str
    name: str
    color: str
    description: str


@dataclasses.dataclass
class ProjectMetadata:
    event_name: str
    lead_html: str

    org_name: str

    repo_id: str
    repo_name: str

    project_id: str
    project_number: int

    fields: dict[str, str]
    depts: dict[str, DeptConfig]


def debug_pprint(object):
    if DEBUG:
        pprint(object)


async def discover_metadata(event_name: str, lead_html: str, user: str, repo: str, project_number: int):
    query = gql("""query DiscoverMetadata($login: String!, $name: String!, $number: Int!) {
      organization(login: $login) {
        repository(name: $name) {
          id
          name
        }
        projectV2(number: $number) {
          id
          url
          fields(first: 100, orderBy: {field: POSITION, direction: DESC}) {
            nodes {
              __typename
              ... on ProjectV2Field {
                id
                name
                dataType
              }
              ... on ProjectV2SingleSelectField {
                id
                name
                options {
                  id
                  name
                  color
                  description
                }
              }
            }
          }
        }
      }
    }""")

    async with Client(**GQL_ASYNC_ARGS) as session:
        result = await session.execute(query, variable_values={
            "login": user,
            "name": repo,
            "number": project_number,
        })

    debug_pprint(result)
    repo_data = result['organization']['repository']
    project = result['organization']['projectV2']

    dept_field_options = None
    for n in project['fields']['nodes']:
        if n['name'] == "Dział":
            dept_field_options = n['options']
            break

    return ProjectMetadata(
        event_name=event_name,
        lead_html=lead_html,
        org_name=user,
        repo_id=repo_data['id'],
        repo_name=repo_data['name'],
        project_id=project['id'],
        project_number=project_number,
        fields={n['name']: n['id'] for n in project['fields']['nodes']},
        depts={n['name']: DeptConfig(**n) for n in dept_field_options}
    )


@app.middleware("http")
async def setup_app(request: Request, call_next):
    global METADATA

    if METADATA is None:
        METADATA = await discover_metadata(
            settings.event_name,
            settings.lead_html,
            settings.github_org_name,
            settings.github_repo_name,
            settings.github_project_number
        )

    return await call_next(request)


async def create_issue(title: str, body: str, dept: str, who: str, where: str) -> int:
    query = gql("""
        mutation CreateIssue($repoId: ID!, $projectIds: [ID!], $title: String!, $body: String!) {
            createIssue(input: {repositoryId: $repoId, projectV2Ids: $projectIds, title: $title, body: $body}) {
                issue {
                    id
                    number
                    projectItems(first: 1) {
                        nodes {
                            id
                        }
                    }
                }
            }
        }
    """)

    async with Client(**GQL_ASYNC_ARGS) as session:
        create_result = await session.execute(query, variable_values={
            "repoId": METADATA.repo_id,
            "projectIds": [METADATA.project_id],
            "title": title,
            "body": body,
        })

    issue = create_result["createIssue"]["issue"]
    debug_pprint(issue)

    return issue["number"]


async def get_project_item_id(issue_number: int):
    query = gql("""
        query GetProjectItemIdForIssue($owner: String!, $name: String!, $number: Int!) {
            repository(owner: $owner, name: $name) {
                issue(number: $number) {
                    id
                    number
                    projectItems(first: 10) {
                        nodes {
                            id
                        }
                    }
                }
            }
        }
    """)

    async with Client(**GQL_ASYNC_ARGS) as session:
        result = await session.execute(query, variable_values={
            "owner": METADATA.org_name,
            "name": METADATA.repo_name,
            "number": issue_number,
        })

    try:
        debug_pprint(result)
        return result["repository"]["issue"]["projectItems"]["nodes"][0]["id"]
    except:
        return None


async def update_issue_fields(item_id: str, dept: str, who: str, where: str):  # + when=
    UPDATE_TEXT_FIELDS_QUERY = gql("""
        mutation UpdateTextField($projectId: ID!, $itemId: ID!, $fieldId: ID!, $body: String!) {
            updateProjectV2ItemFieldValue(input: {
                projectId: $projectId
                itemId: $itemId
                fieldId: $fieldId
                value: { text: $body }
            }) {
                projectV2Item {
                    id
                }
            }
        }
    """)

    UPDATE_SELECT_FIELDS_QUERY = gql("""
        mutation UpdateSelectFields($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String) {
            updateProjectV2ItemFieldValue(input: {
                projectId: $projectId
                itemId: $itemId
                fieldId: $fieldId
                value: { singleSelectOptionId: $optionId }
            }) {
                projectV2Item {
                    id
                }
            }
        }
    """)

    now = datetime.datetime.now()

    fields = {
        "Kto": who,
        "Gdzie": where,
        "Kiedy": WEEKDAYS[now.weekday()] + now.strftime(" %H:%M"),
    }

    async with Client(**GQL_ASYNC_ARGS) as session:
        for field_name, field_value in fields.items():
            try:
                await session.execute(UPDATE_TEXT_FIELDS_QUERY, variable_values={
                    "projectId": METADATA.project_id,
                    "itemId": item_id,
                    "fieldId": METADATA.fields[field_name],
                    "body": field_value,
                })
            except:
                logging.exception("oh no")

        await session.execute(UPDATE_SELECT_FIELDS_QUERY, variable_values={
            "projectId": METADATA.project_id,
            "itemId": item_id,
            "fieldId": METADATA.fields['Dział'],
            "optionId": METADATA.depts[dept].id,
        })


async def create_issue_in_background(dept: str, who: str, what: str, where: str, description: str):
    body = description.strip()

    issue_number = await create_issue(what, body, dept, who, what)
    await sleep(5)  # Projects have a minor item creation delay

    project_item_id = await get_project_item_id(issue_number)

    await update_issue_fields(project_item_id, dept, who, where)


def get_context(**kwargs):
    return {
        "metadata": METADATA,
        "depts": [dept for dept in METADATA.depts.values() if dept.description.strip()],
        **kwargs,
    }


@app.post("/")
async def submit_issue(
        request: Request,
        dept: Annotated[str, Form()],
        who: Annotated[str, Form()],
        what: Annotated[str, Form()],
        where: Annotated[str, Form()],
        background_tasks: BackgroundTasks,
        description: Annotated[str | None, Form()] = "",
):
    background_tasks.add_task(create_issue_in_background, dept, who, what, where, description)
    return templates.TemplateResponse(request=request, name="index.html", context=get_context(thank_you=True))


@app.get("/")
async def root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context=get_context())
