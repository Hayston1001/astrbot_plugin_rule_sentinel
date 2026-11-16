"""Declared model capabilities, mirroring AstrBot's provider modalities setting.

AstrBot stores the administrator's capability declaration in each provider's
``modalities`` list (text/image/audio/tool_use) and treats an empty list as
unconfigured for backward compatibility. A declaration is still only a claim
about the upstream model, so callers must treat these helpers as hints rather
than proof.
"""


def declared_modalities(provider_config):
    """Return the declared capability tuple, or None when nothing was declared."""
    if not isinstance(provider_config, dict):
        return None
    modalities = provider_config.get("modalities")
    if not isinstance(modalities, list) or not modalities:
        return None
    return tuple(value for value in modalities if isinstance(value, str))


def image_capability(provider_config):
    """Return 'image', 'no_image', or 'undeclared' for one provider config."""
    modalities = declared_modalities(provider_config)
    if modalities is None:
        return "undeclared"
    return "image" if "image" in modalities else "no_image"


def tool_capability(provider_config):
    """Return 'tool_use', 'no_tool_use', or 'undeclared' for one provider config."""
    modalities = declared_modalities(provider_config)
    if modalities is None:
        return "undeclared"
    return "tool_use" if "tool_use" in modalities else "no_tool_use"
