"""github: the GraphQL API (v4) that ``gh`` uses for pull requests and issues.

Objects are built on demand from the same state as REST (see ``myworld.api.graphql``). Node IDs
encode what they point to (``PR_kwDO`` + repo#number...), so ``node(id:)`` and mutation inputs
resolve without a lookup table.
"""

from __future__ import annotations

import base64
from typing import Any

from ...api import Request, Response, operation
from ...api.graphql import Executor, QueryError, connection
from ...core.instance import Instance
from ...core.tools import ToolError
from . import issues as issue_tools
from . import pulls as pull_tools
from .model import _approvals, _changes, _merge_base, _pr_state, _search_terms

HOSTS = ("api.github.com",)
INTERFACES = {
    "Actor": {"User", "Bot", "Organization", "Mannequin", "EnterpriseUserAccount"},
    "RepositoryOwner": {"User", "Organization"},
    "Node": {"User", "Organization", "Repository", "Issue", "PullRequest", "Label", "IssueComment",
             "PullRequestReview", "Commit", "CheckRun", "StatusContext", "Ref", "Bot"},
    "Assignable": {"Issue", "PullRequest"}, "Labelable": {"Issue", "PullRequest"}, "Comment": {"IssueComment"},
    "UniformResourceLocatable": {"Issue", "PullRequest", "Repository", "User"},
    "RequestedReviewer": {"User", "Team", "Bot", "Mannequin"},
    "SearchResultItem": {"Issue", "PullRequest", "Repository", "User"},
    "StatusCheckRollupContext": {"CheckRun", "StatusContext"}, "GitObject": {"Commit"},
    "IssueOrPullRequest": {"Issue", "PullRequest"}, "Closable": {"Issue", "PullRequest"},
}


# -- ids ------------------------------------------------------------------------------------------

def _id(prefix: str, key: str) -> str:
    return prefix + "_kwDO" + base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")


def _unid(gid: str) -> tuple[str, str]:
    prefix, _, rest = str(gid).partition("_kwDO")
    try:
        return prefix, base64.urlsafe_b64decode(rest + "=" * (-len(rest) % 4)).decode()
    except Exception:
        raise QueryError(f"Could not resolve to a node with the global id of '{gid}'", "NOT_FOUND") from None


# what introspection reports (``gh`` feature-detects a few fields this way)
TYPE_FIELDS = {
    "Issue": ["id", "number", "title", "body", "state", "stateReason", "author", "assignees", "labels", "comments",
              "blockedBy", "blocking", "parent", "subIssues", "subIssuesSummary", "issueType", "isPinned"],
    "PullRequest": ["id", "number", "title", "body", "state", "isDraft", "mergeable", "mergeStateStatus",
                    "reviewDecision", "statusCheckRollup", "isInMergeQueue", "isMergeQueueEnabled", "autoMergeRequest"],
    "StatusCheckRollupContextConnection": ["nodes", "edges", "pageInfo", "totalCount", "checkRunCount",
                                           "checkRunCountsByState", "statusContextCount", "statusContextCountsByState"],
    "WorkflowRun": ["databaseId", "event", "workflow", "url", "runNumber"],
    "Repository": ["id", "name", "nameWithOwner", "visibility", "autoMergeAllowed", "pullRequestTemplates",
                   "issueTemplates", "defaultBranchRef", "viewerPermission"],
    "ProjectV2": [],
}


class GitHubGraph:
    def __init__(self, ctx: Instance):
        self.ctx = ctx
        self.s = ctx.state

    # -- lookups -------------------------------------------------------------------------------------

    def repo_by_name(self, owner: str, name: str) -> dict[str, Any]:
        r = self.s["repos"].get(f"{owner}/{name}")
        if r is None:
            raise QueryError(f"Could not resolve to a Repository with the name '{owner}/{name}'.", "NOT_FOUND")
        return r

    def resolve_id(self, gid: str) -> tuple[str, Any]:
        prefix, key = _unid(gid)
        if prefix == "R":
            owner, _, name = key.partition("/")
            return "Repository", self.repo_by_name(owner, name)
        if prefix in ("PR", "I"):
            full, _, n = key.rpartition("#")
            r = self.repo_by_name(*full.split("/", 1))
            i = r["issues"].get(int(n))
            if i is None:
                raise QueryError(f"Could not resolve to a node with the global id of '{gid}'", "NOT_FOUND")
            return ("PullRequest" if i["is_pull"] else "Issue"), (r, i)
        if prefix == "U":
            if key not in self.s["users"]:
                raise QueryError(f"Could not resolve to a node with the global id of '{gid}'", "NOT_FOUND")
            return "User", key
        if prefix == "LA":
            full, _, name = key.partition("#")
            r = self.repo_by_name(*full.split("/", 1))
            if name not in r["labels"]:
                raise QueryError(f"Could not resolve to a node with the global id of '{gid}'", "NOT_FOUND")
            return "Label", (r, name)
        raise QueryError(f"Could not resolve to a node with the global id of '{gid}'", "NOT_FOUND")

    def node(self, gid: str) -> dict[str, Any]:
        kind, x = self.resolve_id(gid)
        if kind == "Repository":
            return self.repository(x)
        if kind in ("PullRequest", "Issue"):
            r, i = x
            return self.pull(r, i) if kind == "PullRequest" else self.issue(r, i)
        if kind == "User":
            return self.user(x)
        return self.label(*x)

    # -- objects -------------------------------------------------------------------------------------

    def user(self, login: str | None) -> dict[str, Any] | None:
        if not login:
            return None
        u = self.s["users"].get(login) or {"login": login, "name": login, "id": 0, "type": "User"}
        t = {"Organization": "Organization", "Bot": "Bot"}.get(u.get("type", "User"), "User")
        return {"__typename": t, "id": _id("U", login), "login": login, "name": u.get("name"),
                "databaseId": u.get("id"), "url": f"https://github.com/{login}", "email": u.get("email") or "",
                "avatarUrl": lambda a: f"https://avatars.githubusercontent.com/u/{u.get('id')}?v=4",
                "isViewer": login == self.s["viewer"], "company": None, "bio": None, "location": None}

    def label(self, r: dict[str, Any], name: str) -> dict[str, Any]:
        lab = r["labels"].get(name) or {"color": "ededed", "default": False}
        return {"__typename": "Label", "id": _id("LA", f"{r['full_name']}#{name}"), "name": name,
                "color": lab["color"], "description": lab.get("description"), "isDefault": lab.get("default", False),
                "url": f"https://github.com/{r['full_name']}/labels/{name}"}

    def repository(self, r: dict[str, Any]) -> dict[str, Any]:
        s = self.s
        owner_type = s["users"].get(r["owner"], {}).get("type", "User")
        admin = r["owner"] == s["viewer"] or owner_type == "Organization"
        default = r["default_branch"]

        def pulls(a: dict[str, Any]) -> dict[str, Any]:
            states = a.get("states") or ["OPEN", "CLOSED", "MERGED"]
            items = [(r, i) for i in r["issues"].values() if i["is_pull"] and self.pr_state(r, i) in states
                     and (not a.get("headRefName") or r["pulls"][i["number"]]["head"] == a["headRefName"])
                     and (not a.get("baseRefName") or r["pulls"][i["number"]]["base"] == a["baseRefName"])
                     and all(lab in i["labels"] for lab in a.get("labels") or [])]
            return connection([self.pull(*x) for x in self._order(items, a.get("orderBy"))], a)

        def issues(a: dict[str, Any]) -> dict[str, Any]:
            f = a.get("filterBy") or {}
            states = a.get("states") or f.get("states") or ["OPEN", "CLOSED"]
            items = [(r, i) for i in r["issues"].values() if not i["is_pull"] and i["state"].upper() in states
                     and all(lab in i["labels"] for lab in a.get("labels") or f.get("labels") or [])
                     and (not f.get("assignee") or f["assignee"] in i["assignees"])
                     and (not f.get("createdBy") or f["createdBy"] == i["user"])]
            return connection([self.issue(*x) for x in self._order(items, a.get("orderBy"))], a)

        def number(kind: str) -> Any:
            def get(a: dict[str, Any]) -> Any:
                i = r["issues"].get(int(a.get("number", 0)))
                if i is None or (kind == "PullRequest" and not i["is_pull"]) or (kind == "Issue" and i["is_pull"]):
                    what = {"PullRequest": "PullRequest", "Issue": "Issue"}.get(kind, "issue or pull request")
                    raise QueryError(f"Could not resolve to {'an' if what[0] in 'Ii' else 'a'} {what} with the "
                                     f"number of {a.get('number')}.", "NOT_FOUND")
                return self.pull(r, i) if i["is_pull"] else self.issue(r, i)
            return get

        def labels(a: dict[str, Any]) -> dict[str, Any]:
            names = sorted(r["labels"])
            if a.get("query"):
                names = [n for n in names if a["query"].lower() in n.lower()]
            return connection([self.label(r, n) for n in names], a)

        def users(a: dict[str, Any]) -> dict[str, Any]:
            logins = sorted(u for u, x in s["users"].items() if x.get("type", "User") == "User")
            if a.get("query"):
                logins = [u for u in logins if a["query"].lower() in u.lower()]
            return connection([self.user(u) for u in logins], a)

        def ref(a: dict[str, Any]) -> dict[str, Any] | None:
            name = str(a.get("qualifiedName", "")).removeprefix("refs/heads/")
            return self.ref(r, name) if name in r["branches"] else None

        return {"__typename": "Repository", "id": _id("R", r["full_name"]), "databaseId": r["id"], "name": r["name"],
                "nameWithOwner": r["full_name"], "owner": self.user(r["owner"]), "description": r["description"],
                "url": f"https://github.com/{r['full_name']}", "homepageUrl": "", "isPrivate": r["private"],
                "visibility": "PRIVATE" if r["private"] else "PUBLIC", "isFork": r["fork"], "isArchived": r["archived"],
                "isInOrganization": owner_type == "Organization", "isTemplate": False, "isEmpty": not r["commits"],
                "isMirror": False, "isLocked": False, "viewerPermission": "ADMIN" if admin else "WRITE",
                "viewerCanAdminister": admin, "viewerCanPush": True, "viewerSubscription": "SUBSCRIBED",
                "viewerHasStarred": False, "defaultBranchRef": self.ref(r, default) if default in r["branches"] else None,
                "hasIssuesEnabled": True, "hasWikiEnabled": False, "hasProjectsEnabled": True,
                "hasDiscussionsEnabled": False, "mergeCommitAllowed": True, "squashMergeAllowed": True,
                "rebaseMergeAllowed": True, "deleteBranchOnMerge": False, "autoMergeAllowed": False,
                "viewerDefaultMergeMethod": "MERGE", "viewerDefaultCommitEmail": "",
                "parent": None, "stargazerCount": 0, "forkCount": 0, "watchers": {"totalCount": 0},
                "createdAt": r["created_at"], "updatedAt": r["updated_at"], "pushedAt": r["updated_at"],
                "sshUrl": f"git@github.com:{r['full_name']}.git", "primaryLanguage": {"name": "Python"},
                "languages": lambda a: connection([{"__typename": "Language", "name": "Python"}], a),
                "repositoryTopics": lambda a: connection([], a), "licenseInfo": None, "securityPolicyUrl": None,
                "templateRepository": None, "diskUsage": len(r["commits"]) * 12,
                "pullRequests": pulls, "issues": issues, "pullRequest": number("PullRequest"),
                "issue": number("Issue"), "issueOrPullRequest": number("IssueOrPullRequest"),
                "labels": labels, "label": lambda a: self.label(r, a["name"]) if a.get("name") in r["labels"] else None,
                "assignableUsers": users, "mentionableUsers": users, "collaborators": users,
                "milestones": lambda a: connection([], a), "milestone": lambda a: None,
                "projects": lambda a: connection([], a), "projectsV2": lambda a: connection([], a),
                "ref": ref, "refs": lambda a: connection([self.ref(r, b) for b in sorted(r["branches"])], a),
                "discussions": lambda a: connection([], a), "releases": lambda a: connection([], a),
                "latestRelease": None, "issueTemplates": [], "pullRequestTemplates": [],
                "hasVulnerabilityAlertsEnabled": False, "isSecurityPolicyEnabled": False,
                "mergeQueue": lambda a: None}

    def ref(self, r: dict[str, Any], name: str) -> dict[str, Any]:
        sha = r["branches"][name]
        return {"__typename": "Ref", "id": _id("REF", f"{r['full_name']}#{name}"), "name": name,
                "prefix": "refs/heads/", "target": self.commit(r, sha),
                "branchProtectionRule": {"requiredStatusCheckContexts": r["protected"][name].get("required_checks", [])}
                if name in r["protected"] else None}

    def commit(self, r: dict[str, Any], sha: str | None, pr: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if not sha or sha not in r["commits"]:
            return None
        c = r["commits"][sha]
        head, _, body = c["message"].partition("\n")
        return {"__typename": "Commit", "id": _id("C", f"{r['full_name']}#{sha}"), "oid": sha,
                "abbreviatedOid": sha[:7], "messageHeadline": head, "messageBody": body.strip(), "message": c["message"],
                "committedDate": c["author"]["date"], "authoredDate": c["author"]["date"],
                "url": f"https://github.com/{r['full_name']}/commit/{sha}",
                "author": {"name": c["author"]["name"], "email": c["author"]["email"], "date": c["author"]["date"],
                           "user": self.user(c["author"].get("login"))},
                "authors": lambda a: connection([{"__typename": "GitActor", "name": c["author"]["name"],
                                                  "email": c["author"]["email"],
                                                  "user": self.user(c["author"].get("login"))}], a),
                "statusCheckRollup": self.rollup(r, sha, pr), "status": self.status(r, sha),
                "checkSuites": lambda a: connection([], a), "parents": lambda a: connection(
                    [self.commit(r, p) for p in c["parents"] if p in r["commits"]], a),
                "tree": {"oid": sha[::-1]}}

    def status(self, r: dict[str, Any], sha: str) -> dict[str, Any] | None:
        sts = r["statuses"].get(sha, [])
        if not sts:
            return None
        return {"__typename": "Status", "state": self._combined([x["state"] for x in sts]),
                "contexts": [self._status_context(r, sha, x, None) for x in sts]}

    @staticmethod
    def _combined(states: list[str]) -> str:
        if any(x in ("failure", "error") for x in states):
            return "FAILURE"
        if any(x == "pending" for x in states):
            return "PENDING"
        return "SUCCESS"

    def _status_context(self, r: dict[str, Any], sha: str, x: dict[str, Any], pr: dict[str, Any] | None) -> dict[str, Any]:
        required = set(r["protected"].get(pr["base"], {}).get("required_checks", [])) if pr else set()
        return {"__typename": "StatusContext", "id": _id("SC", f"{r['full_name']}#{sha}#{x['context']}"),
                "context": x["context"], "state": x["state"].upper(), "targetUrl": None,
                "description": x["description"], "createdAt": r["commits"][sha]["author"]["date"],
                "isRequired": lambda a: x["context"] in required, "avatarUrl": None}

    def _check_run(self, r: dict[str, Any], run: dict[str, Any], job: dict[str, Any], pr: dict[str, Any] | None
                   ) -> dict[str, Any]:
        required = set(r["protected"].get(pr["base"], {}).get("required_checks", [])) if pr else set()
        url = f"https://github.com/{r['full_name']}/actions/runs/{run['id']}/job/{job['id']}"
        return {"__typename": "CheckRun", "id": _id("CR", str(job["id"])), "databaseId": job["id"], "name": job["name"],
                "status": job["status"].upper(), "conclusion": (job["conclusion"] or "").upper() or None,
                "startedAt": job["started_at"], "completedAt": job["completed_at"], "detailsUrl": url,
                "url": url, "title": None, "summary": None,
                "checkSuite": {"__typename": "CheckSuite", "workflowRun": {
                    "__typename": "WorkflowRun", "databaseId": run["id"], "event": run["event"],
                    "workflow": {"__typename": "Workflow", "name": run["name"]}},
                    "app": {"name": "GitHub Actions", "slug": "github-actions"}},
                "isRequired": lambda a: job["name"] in required}

    def rollup(self, r: dict[str, Any], sha: str, pr: dict[str, Any] | None) -> dict[str, Any] | None:
        runs = [x for x in r.get("runs", {}).values() if x["head_sha"] == sha]
        from_runs = {x["context"] for x in runs}
        checks = [self._check_run(r, run, job, pr) for run in sorted(runs, key=lambda x: x["id"]) for job in run["jobs"]]
        statuses = [self._status_context(r, sha, x, pr) for x in r["statuses"].get(sha, []) if x["context"] not in from_runs]
        nodes = checks + statuses
        if not nodes:
            return None
        states = [("pending" if c["status"] != "COMPLETED" else "success" if c["conclusion"] in ("SUCCESS", "NEUTRAL",
                                                                                               "SKIPPED") else "failure")
                  for c in checks] + [x["state"] for x in r["statuses"].get(sha, []) if x["context"] not in from_runs]

        def counts(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
            out: dict[str, int] = {}
            for c in items:
                out[c[key]] = out.get(c[key], 0) + 1
            return [{"state": k, "count": v} for k, v in out.items()]

        return {"__typename": "StatusCheckRollup", "state": self._combined(states),
                "contexts": lambda a: {**connection(nodes, a), "checkRunCount": len(checks), "statusContextCount": len(statuses),
                                       "checkRunCountsByState": counts([{"s": c["conclusion"] or c["status"]} for c in checks], "s"),
                                       "statusContextCountsByState": counts(statuses, "state")}}

    def pr_state(self, r: dict[str, Any], i: dict[str, Any]) -> str:
        if not i["is_pull"]:
            return i["state"].upper()
        return "MERGED" if r["pulls"][i["number"]]["merged"] else i["state"].upper()

    def _order(self, items: list[tuple[dict[str, Any], dict[str, Any]]], order: dict[str, Any] | None
               ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        order = order or {"field": "CREATED_AT", "direction": "DESC"}
        key = {"CREATED_AT": "created_at", "UPDATED_AT": "updated_at", "COMMENTS": "comments"}.get(order.get("field"),
                                                                                                  "created_at")
        return sorted(items, key=lambda x: (x[1][key], x[1]["number"]), reverse=order.get("direction", "DESC") == "DESC")

    def _shared(self, r: dict[str, Any], i: dict[str, Any], kind: str) -> dict[str, Any]:
        s = self.s
        full = r["full_name"]

        def comments(a: dict[str, Any]) -> dict[str, Any]:
            items = [{"__typename": "IssueComment", "id": _id("IC", f"{full}#{i['number']}#{c['id']}"),
                      "databaseId": c["id"], "author": self.user(c["user"]), "authorAssociation": "MEMBER",
                      "body": c["body"], "createdAt": c["created_at"], "updatedAt": c["updated_at"],
                      "includesCreatedEdit": False, "isMinimized": False, "minimizedReason": None,
                      "reactionGroups": [], "viewerDidAuthor": c["user"] == s["viewer"],
                      "url": f"https://github.com/{full}/{'pull' if i['is_pull'] else 'issues'}/{i['number']}"
                             f"#issuecomment-{c['id']}"} for c in r["comments"].get(i["number"], [])]
            return connection(items, a)

        return {"__typename": kind, "id": _id("PR" if kind == "PullRequest" else "I", f"{full}#{i['number']}"),
                "databaseId": i["id"], "fullDatabaseId": str(i["id"]), "number": i["number"], "title": i["title"],
                "body": i["body"] or "", "bodyText": i["body"] or "", "state": self.pr_state(r, i),
                "closed": i["state"] == "closed", "closedAt": i["closed_at"], "createdAt": i["created_at"],
                "updatedAt": i["updated_at"], "locked": i["locked"],
                "url": f"https://github.com/{full}/{'pull' if i['is_pull'] else 'issues'}/{i['number']}",
                "author": self.user(i["user"]), "authorAssociation": "MEMBER", "viewerDidAuthor": i["user"] == s["viewer"],
                "assignees": lambda a: connection([self.user(x) for x in i["assignees"] if x in s["users"]], a),
                "assignedActors": lambda a: connection([self.user(x) for x in i["assignees"] if x in s["users"]], a),
                "labels": lambda a: connection([self.label(r, x) for x in i["labels"]], a),
                "milestone": None, "comments": comments, "reactionGroups": [],
                "projectCards": lambda a: connection([], a), "projectItems": lambda a: connection([], a),
                "repository": self.repository(r), "viewerCanUpdate": True, "viewerSubscription": "SUBSCRIBED",
                "participants": lambda a: connection([self.user(u) for u in dict.fromkeys(
                    [i["user"], *(c["user"] for c in r["comments"].get(i["number"], []))])], a),
                "timelineItems": lambda a: connection([], a)}

    def issue(self, r: dict[str, Any], i: dict[str, Any]) -> dict[str, Any]:
        out = self._shared(r, i, "Issue")
        reason = i.get("state_reason")
        out.update(stateReason=reason.upper() if reason and i["state"] == "closed" else None, isPinned=False,
                   issueType=None, parent=None, subIssues=lambda a: connection([], a),
                   subIssuesSummary={"total": 0, "completed": 0, "percentCompleted": 0},
                   blockedBy=lambda a: connection([], a), blocking=lambda a: connection([], a),
                   closedByPullRequestsReferences=lambda a: connection([], a),
                   trackedIssues=lambda a: connection([], a), trackedInIssues=lambda a: connection([], a))
        return out

    def pull(self, r: dict[str, Any], i: dict[str, Any]) -> dict[str, Any]:
        s = self.s
        pr = r["pulls"][i["number"]]
        out = self._shared(r, i, "PullRequest")
        st = _pr_state(s, r, pr) if not pr["merged"] else {"head_sha": r["branches"].get(pr["head"]) or pr.get("merge_commit_sha"),
                                                           "base_sha": pr["base_sha"], "conflicts": [],
                                                           "mergeable_state": "unknown"}
        head_sha = st["head_sha"]
        base_tree = r["commits"][_merge_base(r, head_sha, st["base_sha"]) or st["base_sha"]]["tree"] \
            if head_sha and st.get("base_sha") and head_sha in r["commits"] and st["base_sha"] in r["commits"] else {}
        changes = _changes(s, base_tree, r["commits"][head_sha]["tree"]) if head_sha in r["commits"] else []
        commits = []
        if head_sha in r["commits"] and st.get("base_sha") in r["commits"]:
            from .model import _ancestors
            base_anc = set(_ancestors(r, st["base_sha"]))
            commits = [x for x in reversed(_ancestors(r, head_sha)) if x not in base_anc]
        reviews = r["reviews"].get(i["number"], [])

        def review_nodes(latest: bool) -> list[dict[str, Any]]:
            items = reviews
            if latest:
                last: dict[str, Any] = {}
                for rv in reviews:
                    last[rv["user"]["login"]] = rv
                items = list(last.values())
            return [{"__typename": "PullRequestReview", "id": _id("PRR", str(rv["id"])), "databaseId": rv["id"],
                     "author": self.user(rv["user"]["login"]), "authorAssociation": "MEMBER",
                     "submittedAt": rv["submitted_at"], "createdAt": rv["submitted_at"], "body": rv["body"],
                     "state": rv["state"], "commit": {"oid": rv["commit_id"]}, "reactionGroups": [],
                     "url": rv.get("html_url")} for rv in items]

        need = int(r["protected"].get(pr["base"], {}).get("required_approvals", 0))
        approved, changes_requested = _approvals(r, pr, head_sha) if head_sha else (0, False)
        decision = ("CHANGES_REQUESTED" if changes_requested else "APPROVED" if approved and approved >= need
                    else "REVIEW_REQUIRED" if need else None)
        merge_state = {"clean": "CLEAN", "dirty": "DIRTY", "blocked": "BLOCKED", "behind": "BEHIND", "draft": "DRAFT",
                       "unknown": "UNKNOWN"}.get(st.get("mergeable_state", "unknown"), "UNKNOWN")

        def commit_nodes(a: dict[str, Any]) -> dict[str, Any]:
            return connection([{"__typename": "PullRequestCommit", "id": _id("PRC", c),
                                "commit": self.commit(r, c, pr), "url": f"https://github.com/{r['full_name']}/commit/{c}"}
                               for c in commits], a)

        out.update(
            isDraft=pr["draft"], baseRefName=pr["base"], baseRefOid=st.get("base_sha"), headRefName=pr["head"],
            headRefOid=head_sha, headRepository={"__typename": "Repository", "id": _id("R", r["full_name"]),
                                                  "name": r["name"], "nameWithOwner": r["full_name"]},
            headRepositoryOwner=self.user(r["owner"]), isCrossRepository=False,
            maintainerCanModify=pr["maintainer_can_modify"], merged=pr["merged"], mergedAt=pr["merged_at"],
            mergedBy=self.user(pr["merged_by"]), mergeCommit={"oid": pr["merge_commit_sha"]} if pr["merge_commit_sha"] else None,
            potentialMergeCommit=None if pr["merged"] else {"oid": (head_sha or "")[::-1][:40]},
            mergeable="UNKNOWN" if pr["merged"] else ("CONFLICTING" if st["conflicts"] else "MERGEABLE"),
            mergeStateStatus=merge_state, additions=sum(c["additions"] for c in changes),
            deletions=sum(c["deletions"] for c in changes), changedFiles=len(changes),
            files=lambda a: connection([{"__typename": "PullRequestChangedFile", "path": c["filename"],
                                          "additions": c["additions"], "deletions": c["deletions"],
                                          "changeType": {"added": "ADDED", "removed": "DELETED"}.get(c["status"], "MODIFIED")}
                                         for c in changes], a),
            commits=commit_nodes, reviews=lambda a: connection(review_nodes(False), a),
            latestReviews=lambda a: connection(review_nodes(True), a),
            latestOpinionatedReviews=lambda a: connection([x for x in review_nodes(True) if x["state"] != "COMMENTED"], a),
            reviewRequests=lambda a: connection([{"__typename": "ReviewRequest", "requestedReviewer": self.user(u)}
                                                 for u in pr.get("requested_reviewers", [])], a),
            reviewDecision=decision, closingIssuesReferences=lambda a: connection([], a), autoMergeRequest=None,
            isInMergeQueue=False, isMergeQueueEnabled=False, mergeQueueEntry=None,
            baseRef=self.ref(r, pr["base"]) if pr["base"] in r["branches"] else None,
            headRef=self.ref(r, pr["head"]) if pr["head"] in r["branches"] else None,
            viewerCanMergeAsAdmin=True, viewerCanEnableAutoMerge=False, viewerCanDisableAutoMerge=False,
            reviewThreads=lambda a: connection([], a), totalCommentsCount=i["comments"])
        return out

    # -- search ---------------------------------------------------------------------------------------

    def search(self, a: dict[str, Any]) -> dict[str, Any]:
        quals, words = _search_terms(a.get("query") or "")
        kind = a.get("type", "ISSUE")
        if kind not in ("ISSUE", "ISSUE_ADVANCED"):
            return {**connection([], a), "issueCount": 0, "repositoryCount": 0, "userCount": 0}
        repos = [self.s["repos"][x] for x in quals.get("repo", []) if x in self.s["repos"]] if "repo" in quals \
            else list(self.s["repos"].values())
        want_pr = {"pr": True, "issue": False}
        hits = []
        for r in repos:
            for i in r["issues"].values():
                ok = True
                for v in quals.get("is", []) + quals.get("type", []):
                    if v in want_pr and i["is_pull"] != want_pr[v]:
                        ok = False
                    if v in ("open", "closed") and i["state"] != v:
                        ok = False
                    if v == "merged" and not (i["is_pull"] and r["pulls"][i["number"]]["merged"]):
                        ok = False
                    if v == "draft" and not (i["is_pull"] and r["pulls"][i["number"]]["draft"]):
                        ok = False
                for v in quals.get("state", []):
                    ok &= i["state"] == v
                for v in quals.get("author", []):
                    ok &= i["user"].lower() == (self.s["viewer"] if v == "@me" else v)
                for v in quals.get("assignee", []):
                    ok &= (self.s["viewer"] if v == "@me" else v) in [x.lower() for x in i["assignees"]]
                for v in quals.get("label", []):
                    ok &= v in [x.lower() for x in i["labels"]]
                if i["is_pull"]:
                    pr = r["pulls"][i["number"]]
                    for v in quals.get("head", []):
                        ok &= pr["head"].lower() == v
                    for v in quals.get("base", []):
                        ok &= pr["base"].lower() == v
                text = f"{i['title']} {i['body'] or ''}".lower()
                ok &= all(w in text for w in words)
                if ok:
                    hits.append((r, i))
        hits = self._order(hits, {"field": "CREATED_AT", "direction": "DESC"})
        nodes = [self.pull(r, i) if i["is_pull"] else self.issue(r, i) for r, i in hits]
        return {**connection(nodes, a), "issueCount": len(nodes)}

    # -- mutations ------------------------------------------------------------------------------------

    def _call(self, fn: Any, **kw: Any) -> Any:
        try:
            return fn(self.ctx, **kw)
        except ToolError as e:
            p = e.payload if isinstance(e.payload, dict) else {"message": str(e.payload)}
            msg = p.get("message", "Something went wrong")
            errs = p.get("errors") or []
            detail = "; ".join(x.get("message", "") if isinstance(x, dict) else str(x) for x in errs if x)
            raise QueryError(detail or msg, "UNPROCESSABLE") from None

    def _issue_ref(self, gid: str) -> tuple[dict[str, Any], dict[str, Any]]:
        kind, x = self.resolve_id(gid)
        if kind not in ("Issue", "PullRequest"):
            raise QueryError(f"Could not resolve to a node with the global id of '{gid}'", "NOT_FOUND")
        return x

    def _users(self, ids: list[str] | None) -> list[str] | None:
        return None if ids is None else [self.resolve_id(u)[1] for u in ids]

    def mutations(self) -> dict[str, Any]:
        def inp(a: dict[str, Any]) -> dict[str, Any]:
            return a.get("input") or {}

        def owner_repo(r: dict[str, Any]) -> dict[str, str]:
            return {"owner": r["owner"], "repo": r["name"]}

        def create_issue(a: dict[str, Any]) -> dict[str, Any]:
            x = inp(a)
            kind, r = self.resolve_id(x.get("repositoryId", ""))
            labels = [self.resolve_id(lid)[1][1] for lid in x.get("labelIds") or []]
            out = self._call(issue_tools.create_issue.fn, **owner_repo(r), title=x.get("title", ""), body=x.get("body"),
                             assignees=self._users(x.get("assigneeIds")), labels=labels or None)
            return {"issue": self.issue(r, r["issues"][out["number"]]), "clientMutationId": x.get("clientMutationId")}

        def set_state(state: str, key: str) -> Any:
            def run(a: dict[str, Any]) -> dict[str, Any]:
                x = inp(a)
                r, i = self._issue_ref(x.get(key, ""))
                self._call(issue_tools.update_issue.fn, **owner_repo(r), issue_number=i["number"], state=state)
                if state == "closed" and x.get("stateReason"):
                    i["state_reason"] = x["stateReason"].lower()
                obj = self.pull(r, i) if i["is_pull"] else self.issue(r, i)
                return {"issue" if key == "issueId" else "pullRequest": obj}
            return run

        def add_comment(a: dict[str, Any]) -> dict[str, Any]:
            x = inp(a)
            r, i = self._issue_ref(x.get("subjectId", ""))
            c = self._call(issue_tools.add_issue_comment.fn, **owner_repo(r), issue_number=i["number"],
                           body=x.get("body", ""))
            node = {"__typename": "IssueComment", "id": _id("IC", f"{r['full_name']}#{i['number']}#{c['id']}"),
                    "url": c["html_url"], "body": c["body"]}
            return {"commentEdge": {"node": node}, "subject": {"id": x.get("subjectId")}}

        def update(key: str) -> Any:
            def run(a: dict[str, Any]) -> dict[str, Any]:
                x = inp(a)
                r, i = self._issue_ref(x.get(key, ""))
                labels = [self.resolve_id(lid)[1][1] for lid in x["labelIds"]] if "labelIds" in x else None
                state = x.get("state", "").lower() or None
                self._call(issue_tools.update_issue.fn, **owner_repo(r), issue_number=i["number"], title=x.get("title"),
                           body=x.get("body"), labels=labels, assignees=self._users(x.get("assigneeIds")), state=state)
                if x.get("baseRefName") and i["is_pull"]:
                    r["pulls"][i["number"]]["base"] = x["baseRefName"]
                obj = self.pull(r, i) if i["is_pull"] else self.issue(r, i)
                return {"issue" if key == "id" and not i["is_pull"] else "pullRequest": obj, "issue": obj}
            return run

        def create_pr(a: dict[str, Any]) -> dict[str, Any]:
            x = inp(a)
            _, r = self.resolve_id(x.get("repositoryId", ""))
            out = self._call(pull_tools.create_pull_request.fn, **owner_repo(r), title=x.get("title", ""),
                             head=x.get("headRefName", ""), base=x.get("baseRefName", ""), body=x.get("body"),
                             draft=x.get("draft"), maintainer_can_modify=x.get("maintainerCanModify"))
            return {"pullRequest": self.pull(r, r["issues"][out["number"]])}

        def merge_pr(a: dict[str, Any]) -> dict[str, Any]:
            x = inp(a)
            r, i = self._issue_ref(x.get("pullRequestId", ""))
            pr = r["pulls"][i["number"]]
            if x.get("expectedHeadOid") and r["branches"].get(pr["head"]) != x["expectedHeadOid"]:
                raise QueryError("Head branch was modified. Review and try the merge again.", "UNPROCESSABLE")
            self._call(pull_tools.merge_pull_request.fn, **owner_repo(r), pull_number=i["number"],
                       commit_title=x.get("commitHeadline"), commit_message=x.get("commitBody"),
                       merge_method=(x.get("mergeMethod") or "MERGE").lower())
            return {"pullRequest": self.pull(r, i)}

        def ready(draft: bool) -> Any:
            def run(a: dict[str, Any]) -> dict[str, Any]:
                x = inp(a)
                r, i = self._issue_ref(x.get("pullRequestId", ""))
                r["pulls"][i["number"]]["draft"] = draft
                return {"pullRequest": self.pull(r, i)}
            return run

        def labels(add: bool) -> Any:
            def run(a: dict[str, Any]) -> dict[str, Any]:
                x = inp(a)
                r, i = self._issue_ref(x.get("labelableId", ""))
                names = [self.resolve_id(lid)[1][1] for lid in x.get("labelIds") or []]
                new = list(dict.fromkeys(i["labels"] + names)) if add else [n for n in i["labels"] if n not in names]
                self._call(issue_tools.update_issue.fn, **owner_repo(r), issue_number=i["number"], labels=new)
                return {"labelable": self.pull(r, i) if i["is_pull"] else self.issue(r, i)}
            return run

        def request_reviews(a: dict[str, Any]) -> dict[str, Any]:
            x = inp(a)
            r, i = self._issue_ref(x.get("pullRequestId", ""))
            pr = r["pulls"][i["number"]]
            users = self._users(x.get("userIds")) or []
            if i["user"] in users:
                raise QueryError("Review cannot be requested from pull request author.", "UNPROCESSABLE")
            pr["requested_reviewers"] = users if x.get("union") is False else list(dict.fromkeys(
                pr.get("requested_reviewers", []) + users))
            return {"pullRequest": self.pull(r, i)}

        def add_review(a: dict[str, Any]) -> dict[str, Any]:
            x = inp(a)
            r, i = self._issue_ref(x.get("pullRequestId", ""))
            rv = self._call(pull_tools.create_pull_request_review.fn, **owner_repo(r), pull_number=i["number"],
                            body=x.get("body") or "", event=x.get("event") or "COMMENT")
            return {"pullRequestReview": {"__typename": "PullRequestReview", "id": _id("PRR", str(rv["id"])),
                                          "state": rv["state"], "url": rv["html_url"]}}

        def replace_assignees(a: dict[str, Any]) -> dict[str, Any]:
            x = inp(a)
            r, i = self._issue_ref(x.get("assignableId", ""))
            self._call(issue_tools.update_issue.fn, **owner_repo(r), issue_number=i["number"],
                       assignees=self._users(x.get("actorIds") or []))
            return {"assignable": self.pull(r, i) if i["is_pull"] else self.issue(r, i)}

        def auto_merge(a: dict[str, Any]) -> dict[str, Any]:
            raise QueryError("Pull request Auto merge is not allowed for this repository", "UNPROCESSABLE")

        def payload(name: str, fn: Any) -> Any:
            def run(a: dict[str, Any]) -> dict[str, Any]:
                out = fn(a)
                return {"__typename": name[0].upper() + name[1:] + "Payload",
                        "clientMutationId": (a.get("input") or {}).get("clientMutationId"), **out}
            return run

        return {k: payload(k, v) for k, v in {"createIssue": create_issue, "closeIssue": set_state("closed", "issueId"),
                "reopenIssue": set_state("open", "issueId"), "addComment": add_comment, "updateIssue": update("id"),
                "createPullRequest": create_pr, "updatePullRequest": update("pullRequestId"),
                "closePullRequest": set_state("closed", "pullRequestId"),
                "reopenPullRequest": set_state("open", "pullRequestId"), "mergePullRequest": merge_pr,
                "markPullRequestReadyForReview": ready(False), "convertPullRequestToDraft": ready(True),
                "addLabelsToLabelable": labels(True), "removeLabelsFromLabelable": labels(False),
                "requestReviews": request_reviews, "addPullRequestReview": add_review,
                "replaceActorsForAssignable": replace_assignees, "enablePullRequestAutoMerge": auto_merge}.items()}

    def root(self) -> dict[str, Any]:
        s = self.s

        def repository(a: dict[str, Any]) -> dict[str, Any]:
            return self.repository(self.repo_by_name(a.get("owner", ""), a.get("name", "")))

        def owner(a: dict[str, Any]) -> dict[str, Any] | None:
            login = a.get("login", "")
            if login not in s["users"]:
                raise QueryError(f"Could not resolve to a User with the login of '{login}'.", "NOT_FOUND")
            u = self.user(login)
            u["repositories"] = lambda b: connection([self.repository(r) for r in s["repos"].values()
                                                      if r["owner"] == login], b)
            return u

        viewer = self.user(s["viewer"])
        viewer["repositories"] = lambda b: connection([self.repository(r) for r in s["repos"].values()], b)
        viewer["organizations"] = lambda b: connection([self.user(u) for u, x in s["users"].items()
                                                        if x.get("type") == "Organization"], b)
        reset = self.ctx.now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
        def type_(a: dict[str, Any]) -> dict[str, Any] | None:
            name = a.get("name", "")
            if name not in TYPE_FIELDS:
                return None
            return {"__typename": "__Type", "name": name, "kind": "OBJECT", "description": None,
                    "fields": lambda b: [{"__typename": "__Field", "name": f, "args": [], "description": None}
                                         for f in TYPE_FIELDS[name]]}

        return {"__type": type_, "viewer": viewer, "repository": repository, "user": owner, "organization": owner,
                "repositoryOwner": owner, "node": lambda a: self.node(a.get("id", "")),
                "nodes": lambda a: [self.node(x) for x in a.get("ids") or []], "search": self.search,
                "rateLimit": {"__typename": "RateLimit", "limit": 5000, "cost": 1, "remaining": 4999,
                              "used": 1, "resetAt": reset, "nodeCount": 0}}


@operation("github.graphql", "POST", "/graphql", hosts=HOSTS)
def graphql(ctx: Instance, req: Request) -> Any:
    b = req.json()
    if not b.get("query"):
        return Response(400, {"message": "A query attribute must be specified and must be a string.",
                              "documentation_url": "https://docs.github.com/graphql"})
    g = GitHubGraph(ctx)
    result = Executor(g.root(), g.mutations(), INTERFACES).execute(b["query"], b.get("variables"), b.get("operationName"))
    if result.get("errors") and "mutation" in b["query"].lstrip()[:20].lower():
        # a failed mutation changes nothing (the call is rolled back)
        raise ToolError(result, status=200)
    return result
