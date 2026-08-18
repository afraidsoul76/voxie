"""Tool implementations that Claude can invoke via tool-use.

Each function returns a plain dict describing what happened so the LLM can
decide the next step. Errors are caught and returned as `{"ok": False, ...}`
instead of raising — that way Claude can react to failures conversationally.
"""
