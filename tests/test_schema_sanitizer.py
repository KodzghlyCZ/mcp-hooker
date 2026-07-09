from mcp_hooker.schema_sanitizer import (
    _rewrite_paginated_list_schema,
    sanitize_openapi_spec,
)


def test_rewrite_paginated_list_schema_wraps_array_items():
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
        },
    }

    rewritten = _rewrite_paginated_list_schema(schema)

    assert rewritten is not None
    assert rewritten["type"] == "object"
    assert rewritten["additionalProperties"] is True
    assert rewritten["required"] == ["page", "results"]
    assert rewritten["properties"]["results"]["type"] == "array"
    assert rewritten["properties"]["results"]["items"] == schema["items"]
    assert "prev_page" not in rewritten["properties"]


def test_rewrite_paginated_list_schema_ignores_non_array():
    assert _rewrite_paginated_list_schema({"type": "object"}) is None


def test_sanitize_rewrites_paginated_get_list_endpoints():
    spec = {
        "components": {
            "schemas": {
                "User": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
                }
            }
        },
        "paths": {
            "/api/v1/{account_id}/users": {
                "get": {
                    "parameters": [
                        {"name": "page", "in": "query", "schema": {"type": "integer"}},
                        {"name": "per", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/User"},
                                    }
                                }
                            }
                        }
                    },
                }
            },
            "/api/v1/{account_id}/widgets/{id}": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    }
                                }
                            }
                        }
                    }
                }
            },
        },
    }

    sanitized = sanitize_openapi_spec(
        spec,
        {
            "enabled": True,
            "paginated_lists": {"enabled": True},
        },
    )

    users_schema = sanitized["paths"]["/api/v1/{account_id}/users"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    widgets_schema = sanitized["paths"]["/api/v1/{account_id}/widgets/{id}"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]

    assert users_schema["type"] == "object"
    assert users_schema["properties"]["results"]["type"] == "array"
    assert users_schema["properties"]["results"]["items"]["type"] == "object"
    assert widgets_schema["type"] == "array"


def test_sanitize_paginated_lists_disabled_leaves_array_schema():
    spec = {
        "paths": {
            "/api/v1/{account_id}/users": {
                "get": {
                    "parameters": [{"name": "page", "in": "query", "schema": {"type": "integer"}}],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"type": "array", "items": {"type": "string"}}
                                }
                            }
                        }
                    },
                }
            }
        }
    }

    sanitized = sanitize_openapi_spec(spec, {"enabled": True, "paginated_lists": {"enabled": False}})

    schema = sanitized["paths"]["/api/v1/{account_id}/users"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert schema["type"] == "array"
