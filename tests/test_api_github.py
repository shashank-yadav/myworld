"""The GitHub REST (v3) and GraphQL (v4) surfaces, including Actions."""

import base64
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from toolsim.host import HostConfig, create_app

SPEC = {"name": "w", "servers": {"github": {}}, "agents": {"alex": {"as": "alex@acme.com"},
                                                         "john": {"as": "john@acme.com"}}}


@pytest.fixture
def gh():
    c = TestClient(create_app(config=HostConfig()))
    run = c.post("/envs", json={"spec": SPEC, "id": "w"}).json()

    def as_(agent):
        h = {"Authorization": f"token {run['credentials'][agent]['github_token']}"}

        def call(method, path, **kw):
            return c.request(method, f"/gw/api.github.com{path}", headers={**h, **kw.pop("headers", {})}, **kw)

        def gql(query, **variables):
            return call("POST", "/graphql", json={"query": query, "variables": variables}).json()
        call.gql = gql
        return call
    return c, as_("alex"), as_("john")


def test_rest_basics_and_headers(gh):
    _, alex, _ = gh
    me = alex("GET", "/user")
    assert me.json()["login"] == "alex-rivera" and "repo" in me.headers["x-oauth-scopes"]
    assert me.headers["x-ratelimit-limit"] == "5000"
    issues = alex("GET", "/repos/acme/api/issues", params={"per_page": 1})
    assert len(issues.json()) == 1 and 'rel="next"' in issues.headers["link"]
    diff = alex("GET", "/repos/acme/api/pulls/4", headers={"Accept": "application/vnd.github.v3.diff"})
    assert diff.text.startswith("diff --git a/src/retry.py b/src/retry.py")
    assert alex("GET", "/repos/acme/nope").json() == {"message": "Not Found", "documentation_url":
                                                      "https://docs.github.com/rest", "status": "404"}
    bad = TestClient(gh[0].app).get("/gw/api.github.com/user", headers={"Authorization": "token ghp_fake"})
    assert bad.status_code == 401 and bad.json()["message"] == "Bad credentials"


def test_graphql_queries_like_gh(gh):
    _, alex, _ = gh
    q = """query PullRequestList($owner: String!, $repo: String!) {
      repository(owner: $owner, name: $repo) {
        pullRequests(states: [OPEN], first: 30) { totalCount nodes { number title state isDraft headRefName
          author { login ...on User { name } } reviewDecision mergeStateStatus
          statusCheckRollup: commits(last: 1) { nodes { commit { statusCheckRollup { state
            contexts(first: 100) { nodes { __typename ...on CheckRun { name status conclusion }
                                            ...on StatusContext { context state } } } } } } } } } } }"""
    data = alex.gql(q, owner="acme", repo="api")["data"]["repository"]["pullRequests"]
    pr = data["nodes"][0]
    assert data["totalCount"] == 1 and pr["author"] == {"login": "john-park", "name": "John Park"}
    ctx = pr["statusCheckRollup"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"][0]
    assert ctx["__typename"] == "CheckRun" and ctx["conclusion"] == "SUCCESS"
    missing = alex.gql('{ repository(owner: "acme", name: "nope") { id } }')
    assert missing["data"]["repository"] is None and missing["errors"][0]["type"] == "NOT_FOUND"
    typo = alex.gql('{ viewer { loginn } }')
    assert "doesn't exist on type 'User'" in typo["errors"][0]["message"]
    page1 = alex.gql('{ repository(owner: "acme", name: "api") { issues(first: 1) { nodes { number } '
                     'pageInfo { hasNextPage endCursor } } } }')["data"]["repository"]["issues"]
    page2 = alex.gql('query($c: String) { repository(owner: "acme", name: "api") { issues(first: 1, after: $c) '
                     '{ nodes { number } } } }', c=page1["pageInfo"]["endCursor"])
    assert page1["pageInfo"]["hasNextPage"] and page2["data"]["repository"]["issues"]["nodes"][0]["number"] != \
        page1["nodes"][0]["number"]


def test_graphql_mutations_and_rollback(gh):
    c, alex, john = gh
    repo_id = alex.gql('{ repository(owner: "acme", name: "api") { id } }')["data"]["repository"]["id"]
    made = alex.gql('mutation($r: ID!) { createIssue(input: {repositoryId: $r, title: "From GraphQL"}) '
                    '{ issue { number url } } }', r=repo_id)["data"]["createIssue"]["issue"]
    node = alex.gql('{ repository(owner: "acme", name: "api") { issue(number: %d) { id } } }' % made["number"])
    iid = node["data"]["repository"]["issue"]["id"]
    alex.gql('mutation($i: ID!) { addComment(input: {subjectId: $i, body: "hi"}) { commentEdge { node { url } } } }', i=iid)
    alex.gql('mutation($i: ID!) { closeIssue(input: {issueId: $i, stateReason: NOT_PLANNED}) { issue { id } } }', i=iid)
    got = alex("GET", f"/repos/acme/api/issues/{made['number']}").json()
    assert got["state"] == "closed" and got["comments"] == 1
    pr = alex.gql('{ repository(owner: "acme", name: "api") { pullRequest(number: 4) { id } } }')
    pid = pr["data"]["repository"]["pullRequest"]["id"]
    before = len(c.get("/envs/w/calls").json()["calls"])
    bad = john.gql('mutation($p: ID!) { addPullRequestReview(input: {pullRequestId: $p, event: APPROVE}) '
                   '{ pullRequestReview { id } } }', p=pid)
    assert "Can not approve your own pull request" in bad["errors"][0]["message"], "john wrote PR #4"
    ok = alex.gql('mutation($p: ID!) { addPullRequestReview(input: {pullRequestId: $p, event: APPROVE, body: "lgtm"}) '
                  '{ pullRequestReview { state } } }', p=pid)
    assert ok["data"]["addPullRequestReview"]["pullRequestReview"]["state"] == "APPROVED"
    assert len(c.get("/envs/w/calls").json()["calls"]) == before + 2


def test_actions_runs_logs_and_reruns(gh):
    c, alex, _ = gh
    sha = alex("GET", "/repos/acme/api/git/ref/heads/main").json()["object"]["sha"]
    alex("POST", "/repos/acme/api/git/refs", json={"ref": "refs/heads/feat", "sha": sha})
    content = base64.b64encode(b"def test_x():\n    assert False\n").decode()
    alex("PUT", "/repos/acme/api/contents/tests/test_new.py", json={"message": "add test", "content": content,
                                                                    "branch": "feat"})
    runs = alex("GET", "/repos/acme/api/actions/runs", params={"branch": "feat"}).json()["workflow_runs"]
    assert runs[0]["status"] == "in_progress"
    assert alex("GET", f"/repos/acme/api/actions/runs/{runs[0]['id']}/logs").status_code == 404, "no logs yet"
    c.post("/envs/w/advance", json={"seconds": 300})
    run = alex("GET", f"/repos/acme/api/actions/runs/{runs[0]['id']}").json()
    assert (run["status"], run["conclusion"]) == ("completed", "failure")
    jobs = alex("GET", f"/repos/acme/api/actions/runs/{run['id']}/jobs").json()["jobs"]
    assert [s["conclusion"] for s in jobs[0]["steps"] if s["name"] == "Run tests"] == ["failure"]
    z = zipfile.ZipFile(io.BytesIO(alex("GET", f"/repos/acme/api/actions/runs/{run['id']}/logs").content))
    assert "0_ci.txt" in z.namelist() and b"assert False" in z.read("ci/5_Run tests.txt")
    assert alex("POST", f"/repos/acme/api/actions/runs/{run['id']}/rerun-failed-jobs").status_code == 201
    again = alex("GET", f"/repos/acme/api/actions/runs/{run['id']}").json()
    assert again["run_attempt"] == 2 and again["status"] == "in_progress"
    assert alex("POST", f"/repos/acme/api/actions/runs/{run['id']}/rerun").status_code == 403, "already running"
