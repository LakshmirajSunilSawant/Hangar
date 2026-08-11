"""What each role may do (PRD §7: owner / editor / viewer).

Capabilities are listed explicitly rather than derived from a rank, because
"editor is viewer plus more" is the kind of assumption that quietly grants
deletion to the wrong people the first time someone reorders an enum. The
table below is the whole authorisation model, and it is meant to be readable
in one screen.
"""

from __future__ import annotations

from enum import Enum

from .store import Role


class Action(str, Enum):
    VIEW = "view"            # see the app, its status and URL
    VIEW_LOGS = "view_logs"  # build and container logs, security findings
    DEPLOY = "deploy"        # redeploy, stop, restart
    SHARE = "share"          # grant and revoke other people's access
    DELETE = "delete"        # destroy the app and its data


CAPABILITIES: dict[Role, frozenset[Action]] = {
    Role.OWNER: frozenset(
        {Action.VIEW, Action.VIEW_LOGS, Action.DEPLOY, Action.SHARE, Action.DELETE}
    ),
    Role.EDITOR: frozenset({Action.VIEW, Action.VIEW_LOGS, Action.DEPLOY}),
    # Logs can contain anything the app decided to print, including data the
    # viewer was never meant to see, so read access to the app is not read
    # access to its logs.
    Role.VIEWER: frozenset({Action.VIEW}),
}


def role_of(value: str | None) -> Role | None:
    if value is None:
        return None
    try:
        return Role(value)
    except ValueError:
        return None


def allows(role: Role | str | None, action: Action) -> bool:
    resolved = role if isinstance(role, Role) else role_of(role)
    if resolved is None:
        return False
    return action in CAPABILITIES[resolved]


def describe(role: Role | str) -> list[str]:
    resolved = role if isinstance(role, Role) else Role(role)
    return sorted(action.value for action in CAPABILITIES[resolved])
