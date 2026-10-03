"""The agent-facing surface of the system: approved capabilities as callable
tools. See capabilities/catalog.py."""
from capabilities.catalog import CapabilityNotFound, invoke, list_tools, tool_definition

__all__ = ["CapabilityNotFound", "invoke", "list_tools", "tool_definition"]
