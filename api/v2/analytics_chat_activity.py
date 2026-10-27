"""
Chat Activity table endpoint.

Same data and parameters as analytics_chats, served from its own route so the table keeps a
cache entry and URL separate from the Top Chats chart.
"""

try:
    from tools import api_tools, auth, config as c, register_openapi
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False


if _API_AVAILABLE:
    from .analytics_chats import PromptLibAPI as ChatsListAPI

    class PromptLibAPI(ChatsListAPI):
        """Paginated, sortable chat activity rows for the Chat Activity table."""

        @register_openapi(
            name="List Chat Activity",
            description=(
                "Returns the paginated Chat Activity table rows. Same data and query parameters "
                "as List Chat Analytics, served from a separate route."
            ),
            tags=["usage/analytics"],
            parameters=getattr(ChatsListAPI.get, "_openapi", {}).get("parameters", []),
            responses={
                "200": {"description": "Paginated chat activity"},
                "401": {"description": "Unauthorized"},
                "500": {"description": "Internal server error"},
            },
            available_to_users=True,
        )
        @auth.decorators.check_api({
            "permissions": ["models.monitoring.tracing.view"],
            "recommended_roles": {
                c.DEFAULT_MODE: {"admin": True, "editor": True, "viewer": True},
            }
        })
        @api_tools.endpoint_metrics
        def get(self, project_id: int, **kwargs):
            """
            GET /api/v2/usage/analytics_chat_activity/prompt_lib/<project_id>
            """
            return self._list_chats(project_id)


    class API(api_tools.APIBase):
        url_params = api_tools.with_modes([
            '<int:project_id>',
        ])
        mode_handlers = {
            'prompt_lib': PromptLibAPI,
        }
else:
    API = None
