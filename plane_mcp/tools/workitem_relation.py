"""Relations between work items, and the workspace definitions that type them.

Two systems behind one tool: built-in dependencies (six fixed directional types)
and custom relations (workspace-defined, each with an outward and inward label).
`create` routes between them by which arguments are supplied.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from fastmcp import FastMCP
from plane.errors.errors import HttpError
from plane.models.work_item_relation_definitions import (
    CreateWorkItemRelationDefinition,
    PaginatedWorkItemRelationDefinitionResponse,
    UpdateWorkItemRelationDefinition,
    WorkItemRelationDefinition,
)
from plane.models.work_items import (
    CreateWorkItemCustomRelation,
    CreateWorkItemDependency,
    DependencyTypeEnum,
)

from plane_mcp.client import get_plane_client_context
from plane_mcp.toolkit import (
    Action,
    build_annotations,
    build_description,
    coerce_list,
    missing,
    needs,
    one_of,
    opt,
)

NAME = "workitem_relation"
TITLE = "Work item relations"

DEPENDENCY_TYPES: tuple[str, ...] = get_args(DependencyTypeEnum)

_OTHER_RELATIONS = (
    "For any other relationship pass relation_definition_id and "
    "relation_definition_label from the list_definitions action."
)

ACTIONS = (
    Action("list", ("project_id", "workitem_id"), read=True),
    Action(
        "create",
        ("project_id", "workitem_id", "workitem_ids"),
        ("relation_type", "relation_definition_id", "relation_definition_label"),
        note="pass relation_type for a dependency, or definition id + label for a custom relation",
    ),
    Action(
        "delete",
        ("project_id", "workitem_id", "related_workitem_id"),
        ("is_dependency",),
        note="removes one relation; dependencies and custom relations are independent, so "
        "is_dependency must match the kind that was created (default false)",
        destructive=True,
    ),
    Action("list_definitions", optional=("is_default", "is_active"), read=True),
    Action("create_definition", ("name",), ("outward", "inward", "is_active", "color")),
    Action("update_definition", ("definition_id",), ("name", "outward", "inward", "is_active", "color")),
    Action("delete_definition", ("definition_id",), destructive=True),
)

FOOTER = (
    "Call list_definitions first and match the user's wording to an entry. A "
    f"built_in_dependencies value ({', '.join(DEPENDENCY_TYPES)}) goes in relation_type; a "
    "custom definition needs its id in relation_definition_id and the matched outward or "
    "inward label in relation_definition_label, which sets direction."
)

LEGACY = {
    "list_work_item_relations": "list",
    "create_work_item_relation": "create",
    "remove_work_item_relation": "delete",
    "list_work_item_relation_definitions": "list_definitions",
    "create_work_item_relation_definition": "create_definition",
    "update_work_item_relation_definition": "update_definition",
    "delete_work_item_relation_definition": "delete_definition",
}


# Custom relations are optional: Community Edition serves none of their endpoints. A 404
# is the server saying the route does not exist, so it is reported rather than raised;
# any other failure (authentication, server error) is a real error and still propagates.
_NO_CUSTOM_DEFINITIONS = "this Plane server does not expose custom relation definitions"
_NO_CUSTOM_RELATIONS = "this Plane server does not expose custom relations; only built-in dependencies are shown"


def _route_is_absent(error: HttpError) -> bool:
    return error.status_code == 404


def _all_definitions(client, workspace_slug: str, is_default, is_active) -> list[WorkItemRelationDefinition]:
    """Definitions are a small set an agent must see whole, so page through them."""
    results: list[WorkItemRelationDefinition] = []
    cursor: str | None = None
    while True:
        page: PaginatedWorkItemRelationDefinitionResponse = client.work_item_relation_definitions.list(
            workspace_slug=workspace_slug,
            is_default=is_default,
            is_active=is_active,
            per_page=100,
            cursor=cursor,
        )
        results.extend(page.results)
        cursor = page.next_cursor
        if not page.next_page_results or not cursor:
            return results


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name=NAME,
        description=build_description(
            "Relations between work items, and the definitions that type them.", ACTIONS, FOOTER
        ),
        annotations=build_annotations(TITLE, ACTIONS),
    )
    def workitem_relation(
        action: Literal[
            "list",
            "create",
            "delete",
            "list_definitions",
            "create_definition",
            "update_definition",
            "delete_definition",
        ],
        project_id: str = "",
        workitem_id: str = "",
        workitem_ids: list[str] | None = None,
        related_workitem_id: str = "",
        relation_type: str = "",
        relation_definition_id: str = "",
        relation_definition_label: str = "",
        definition_id: str = "",
        name: str = "",
        outward: str = "",
        inward: str = "",
        color: str = "",
        # Tri-state: False is a real filter value, distinct from "no filter".
        is_default: bool | None = None,
        is_active: bool | None = None,
        is_dependency: bool = False,
    ) -> Any:
        client, workspace_slug = get_plane_client_context()

        if action == "list_definitions":
            result: dict[str, Any] = {"built_in_dependencies": list(DEPENDENCY_TYPES)}
            try:
                result["custom_definitions"] = [
                    d.model_dump() for d in _all_definitions(client, workspace_slug, is_default, is_active)
                ]
            except HttpError as error:
                if not _route_is_absent(error):
                    raise
                result["custom_definitions"] = []
                result["unsupported"] = {"custom_definitions": _NO_CUSTOM_DEFINITIONS}
            return result

        if action == "create_definition":
            if not name:
                return missing(action, "name")
            return client.work_item_relation_definitions.create(
                workspace_slug=workspace_slug,
                data=CreateWorkItemRelationDefinition(
                    name=name,
                    outward=opt(outward),
                    inward=opt(inward),
                    is_active=is_active,
                    color=opt(color),
                ),
            )

        if action in ("update_definition", "delete_definition"):
            if not definition_id:
                return missing(action, "definition_id")
            if action == "update_definition":
                return client.work_item_relation_definitions.update(
                    workspace_slug=workspace_slug,
                    definition_id=definition_id,
                    data=UpdateWorkItemRelationDefinition(
                        name=opt(name),
                        outward=opt(outward),
                        inward=opt(inward),
                        is_active=is_active,
                        color=opt(color),
                    ),
                )
            client.work_item_relation_definitions.delete(workspace_slug=workspace_slug, definition_id=definition_id)
            return None

        if error := needs(action, project_id=project_id, workitem_id=workitem_id):
            return error

        if action == "list":
            dependencies = client.work_items.dependencies.list(
                workspace_slug=workspace_slug, project_id=project_id, work_item_id=workitem_id
            )
            try:
                custom = client.work_items.custom_relations.list(
                    workspace_slug=workspace_slug, project_id=project_id, work_item_id=workitem_id
                )
            except HttpError as error:
                if not _route_is_absent(error):
                    raise
                return {
                    "dependencies": dependencies.model_dump(),
                    "custom": {},
                    "unsupported": {"custom_relations": _NO_CUSTOM_RELATIONS},
                }
            return {
                "dependencies": dependencies.model_dump(),
                "custom": {label: [item.model_dump() for item in items] for label, items in custom.items()},
            }

        if action == "create":
            targets = coerce_list(workitem_ids)
            if not targets:
                return missing(action, "workitem_ids")
            if relation_type:
                if error := one_of("relation_type", relation_type, DEPENDENCY_TYPES, _OTHER_RELATIONS):
                    return error
                return client.work_items.dependencies.create(
                    workspace_slug=workspace_slug,
                    project_id=project_id,
                    work_item_id=workitem_id,
                    data=CreateWorkItemDependency(
                        relation_type=relation_type,  # type: ignore[arg-type]
                        work_item_ids=targets,
                    ),
                )
            if relation_definition_id and relation_definition_label:
                try:
                    return client.work_items.custom_relations.create(
                        workspace_slug=workspace_slug,
                        project_id=project_id,
                        work_item_id=workitem_id,
                        data=CreateWorkItemCustomRelation(
                            relation_definition_id=relation_definition_id,
                            relation_definition_type=relation_definition_label,
                            work_item_ids=targets,
                        ),
                    )
                except HttpError as error:
                    if not _route_is_absent(error):
                        raise
                    return (
                        "Error: the custom relations endpoint answered 404, so either this Plane server "
                        "does not expose custom relations or the work item was not found. "
                        "Nothing was created."
                    )
            return (
                "Error: provide relation_type for a built-in dependency, or both "
                "relation_definition_id and relation_definition_label for a custom relation. "
                "Call the list_definitions action to find one."
            )

        if not related_workitem_id:
            return missing(action, "related_workitem_id")
        remove = client.work_items.dependencies.remove if is_dependency else client.work_items.custom_relations.remove
        remove(
            workspace_slug=workspace_slug,
            project_id=project_id,
            work_item_id=workitem_id,
            related_work_item_id=related_workitem_id,
        )
        return None
