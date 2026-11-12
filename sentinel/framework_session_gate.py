"""Conservative AstrBot session and whitelist checks for future passive hooks.

This independently checks session settings without retaining the event object.
It does not claim equivalence to the complete framework pipeline. Session
configuration and message identifiers must still be verified in integration.
"""


class FrameworkSessionGate:
    def __init__(self, *, get_config, is_session_enabled, is_plugin_enabled, plugin_name):
        self.get_config = get_config
        self.is_session_enabled = is_session_enabled
        self.is_plugin_enabled = is_plugin_enabled
        self.plugin_name = plugin_name

    async def __call__(self, message):
        origin = message.session_origin
        if not isinstance(origin, str) or not origin.strip():
            return False
        parts = origin.split(":", 2)
        group_id = getattr(message, "group_id", None)
        if (len(parts) != 3 or parts[1] != "GroupMessage" or not parts[0].strip()
                or not parts[2].strip() or getattr(message, "platform_id", None) != parts[0]
                or not isinstance(group_id, str) or not group_id.strip() or parts[2] != group_id):
            return False
        try:
            config = self.get_config(origin)
            plugin_set = config.get("plugin_set", ["*"])
            if not isinstance(plugin_set, list) or (plugin_set != ["*"] and self.plugin_name not in plugin_set):
                return False
            platform_settings = config.get("platform_settings")
            if not isinstance(platform_settings, dict):
                return False
            enabled = platform_settings.get("enable_id_white_list")
            whitelist = platform_settings.get("id_whitelist")
            if type(enabled) is not bool or not isinstance(whitelist, list):
                return False
            normalized = {str(value).strip() for value in whitelist if str(value).strip()}
            # Do not use the framework's optional admin bypass: a stricter
            # whitelist avoids assuming that platform sender roles are reliable.
            if enabled and normalized and origin not in normalized and group_id not in normalized:
                return False
            if await self.is_session_enabled(origin) is not True:
                return False
            if await self.is_plugin_enabled(origin, self.plugin_name) is not True:
                return False
            return True
        except Exception:
            return False


def build_astrbot_session_gate(context, plugin_name):
    from astrbot.core.star.session_llm_manager import SessionServiceManager
    from astrbot.core.star.session_plugin_manager import SessionPluginManager
    return FrameworkSessionGate(
        get_config=lambda origin: context.get_config(umo=origin),
        is_session_enabled=SessionServiceManager.is_session_enabled,
        is_plugin_enabled=SessionPluginManager.is_plugin_enabled_for_session,
        plugin_name=plugin_name,
    )
